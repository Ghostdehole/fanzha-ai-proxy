import json
from dataclasses import replace

import httpx
import pytest
from fastapi import Request

from auth import get_upstream_token
from client import AntiFraudClient
from config import AppConfig, clean_token
from errors import APIError
from main import create_app


def configuration(**kwargs):
    defaults = dict(access_token="test-upstream", refresh_token="", proxy_api_key="", cors_origins=(), token_state_file="", host="127.0.0.1")
    defaults.update(kwargs)
    return replace(AppConfig(), **defaults)


async def request(cfg=None, *, host="127.0.0.1", client="127.0.0.1", path="/v1/models", headers=None, method="GET", **kwargs):
    app = create_app(cfg or configuration())
    transport = httpx.ASGITransport(app, client=(client, 1234), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://" + host) as api:
        return await api.request(method, path, headers=headers, **kwargs)


async def test_local_legacy_models_and_health():
    assert (await request()).status_code == 200
    assert (await request(path="/models")).status_code == 200
    health = await request(path="/health", client="198.51.100.1")
    assert health.status_code == 200
    assert health.json()["upstream_checked"] is False


@pytest.mark.parametrize("overrides", [{"client": "198.51.100.1"}, {"host": "attacker.invalid"}])
async def test_keyless_remote_and_dns_rebinding_denied(overrides):
    assert (await request(**overrides)).status_code == 403


@pytest.mark.parametrize("key", [None, "sk-no-key-needed", "wrong"])
async def test_remote_requires_matching_proxy_key(key):
    headers = {"Authorization": "Bearer " + key} if key is not None else {}
    result = await request(configuration(proxy_api_key="test-proxy"), client="198.51.100.1", headers=headers)
    assert result.status_code == 401
    assert result.headers["www-authenticate"] == "Bearer"


async def test_correct_key_allows_remote_and_keeps_credentials_separate():
    cfg = configuration(proxy_api_key="test-proxy")
    assert (await request(cfg, client="198.51.100.1", headers={"Authorization": "Bearer test-proxy"})).status_code == 200
    observed = []

    async def upstream(req):
        observed.append(req.headers["authorization"])
        if req.url.path.endswith("create_session"):
            return httpx.Response(200, json={"code": 200, "data": "session"})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text='data: {"data":{"answer":"OK"}}\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as raw:
        app = create_app(cfg)
        app.state.service = AntiFraudClient(cfg, raw)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as api:
            payload = {"messages": [{"role": "user", "content": "test"}]}
            for override, expected in [(None, "test-upstream"), ("separate-account", "separate-account")]:
                headers = {"Authorization": "Bearer test-proxy"}
                if override:
                    headers["X-Upstream-Token"] = override
                result = await api.post("/v1/chat/completions", json=payload, headers=headers)
                assert result.status_code == 200, result.text
                assert observed[-2:] == ["Bearer " + expected] * 2
    assert "Bearer test-proxy" not in observed


async def test_duplicate_authorization_rejected():
    result = await request(configuration(proxy_api_key="key"), headers=[("Authorization", "Bearer key"), ("Authorization", "Bearer other")])
    assert result.status_code == 400


async def test_origins_and_preflight():
    cfg = configuration(proxy_api_key="key", cors_origins=("https://trusted.example",))
    forbidden = await request(cfg, headers={"Origin": "https://evil.example", "Authorization": "Bearer key"})
    assert forbidden.status_code == 403
    headers = {"Origin": "https://trusted.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}
    allowed = await request(cfg, method="OPTIONS", headers=headers)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://trusted.example"
    assert "access-control-allow-credentials" not in allowed.headers
    headers["Origin"] = "https://evil.example"
    assert (await request(cfg, method="OPTIONS", headers=headers)).status_code == 400


async def test_advertised_and_chunked_body_limits():
    cfg = configuration(max_request_body_bytes=5)
    assert (await request(cfg, method="POST", content=b"123456")).status_code == 413
    async def chunks():
        yield b"123"
        yield b"456"
    assert (await request(cfg, method="POST", content=chunks())).status_code == 413
    assert (await request(cfg, headers={"Content-Length": "9" * 5000})).status_code == 413
    assert (await request(cfg, headers={"Content-Length": "-1"})).status_code == 400


async def test_validation_and_unexpected_errors_are_safe():
    cfg = configuration()
    app = create_app(cfg)
    @app.get("/test-error")
    async def error():
        raise ValueError("private-credential")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://127.0.0.1") as api:
        result = await api.get("/test-error")
        assert result.status_code == 500
        assert "private-credential" not in result.text
        assert result.json()["error"]["type"] == "server_error"
        missing = await api.post("/v1/audio/transcriptions", files={"other": ("a.wav", b"x")})
        assert missing.status_code == 400
        assert "error" in missing.json()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_unsafe_listen_requires_key(host):
    with pytest.raises(ValueError, match="PROXY_API_KEY"):
        configuration(host=host).validate_server()
    configuration(host=host, proxy_api_key="key").validate_server()


async def test_lifespan_rejects_unsafe_launch_and_closes_pool():
    unsafe = create_app(configuration(host="0.0.0.0"))
    with pytest.raises(ValueError):
        async with unsafe.router.lifespan_context(unsafe):
            pass
    app = create_app(configuration())
    async with app.router.lifespan_context(app):
        client = app.state.service.client
        assert not client.is_closed
    assert client.is_closed


def test_configuration_bounds_and_token_normalization():
    with pytest.raises(ValueError):
        configuration(port=65536).validate_server()
    with pytest.raises(ValueError):
        configuration(cors_origins=("*",)).validate_server()
    assert clean_token(" bearer\t abc ") == "abc"
    assert clean_token("Bearer") == ""


@pytest.mark.parametrize("path,body", [
    ("/v1/audio/speech", b'{"input":"hi","\\ud800":1}'),
    ("/v1/chat/completions", b'{"messages":[{"role":"user","content":"hi"}],"\\ud800":1}'),
])
async def test_invalid_unicode_field_names_are_400_not_500(path, body):
    cfg = configuration()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail("Invalid requests must not reach upstream"))) as raw:
        app = create_app(cfg)
        app.state.service = AntiFraudClient(cfg, raw)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://127.0.0.1") as api:
            result = await api.post(path, content=body, headers={"Content-Type": "application/json"})
            assert result.status_code == 400, result.text
            assert "error" in result.json()
