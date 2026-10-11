"""Exercise real middleware, authentication, routes and client with a fake network."""
import base64
import io
import json
import wave
from dataclasses import replace

import httpx
import pytest

from client import AntiFraudClient
from config import AppConfig
from main import create_app


def configuration():
    return replace(
        AppConfig(), base_url="https://upstream.test", host="127.0.0.1",
        access_token="server-credential", refresh_token="",
        proxy_api_key="proxy-credential", cors_origins=(), token_state_file="",
    )


def pcm_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * 10)
    return output.getvalue()


@pytest.mark.parametrize("override", [None, "external-credential"])
async def test_audio_and_dashboard_through_real_authenticated_app(override):
    """Body replay, multipart parsing, rendering and credential routing coexist."""
    audio = pcm_wav()
    observed = []

    async def upstream(request):
        path = request.url.path
        observed.append((path, request.headers["authorization"]))
        if path.endswith("/tts/volcengine"):
            assert json.loads(await request.aread())["text"] == "Hello"
            data = {"audio": base64.b64encode(audio).decode("ascii")}
        elif path.endswith("/asr/audio"):
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert audio in await request.aread()
            data = "Recognized speech"
        else:
            assert path == "/api/v1/conversations/usage"
            data = {"remaining": 15, "used": 5, "limit": 20,
                    "banned": False, "exceeded": False, "role": "USER"}
        return httpx.Response(200, json={"code": 200, "data": data})

    cfg = configuration()
    headers = {"Authorization": "Bearer " + cfg.proxy_api_key}
    if override is not None:
        headers["X-Upstream-Token"] = override
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as raw:
        app = create_app(cfg)
        app.state.service = AntiFraudClient(cfg, raw)
        # A non-loopback peer proves the configured key is actually checked.
        transport = httpx.ASGITransport(app, client=("198.51.100.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as api:
            speech = await api.post("/v1/audio/speech", json={"input": "Hello"}, headers=headers)
            assert speech.status_code == 200, speech.text
            assert speech.headers["content-type"] == "audio/wav"
            assert speech.content == audio

            transcription = await api.post(
                "/v1/audio/transcriptions", headers=headers,
                files={"file": ("sample.wav", audio, "audio/wav")},
            )
            assert transcription.status_code == 200, transcription.text
            assert transcription.json() == {"text": "Recognized speech"}

            dashboard = await api.get("/usage", headers={**headers, "Accept": "text/html"})
            assert dashboard.status_code == 200, dashboard.text
            assert dashboard.headers["content-type"].startswith("text/html")
            assert 'data-state="normal"' in dashboard.text
            assert 'id="metric-remaining-val" class="metric-value">15<' in dashboard.text
            assert "Content-Security-Policy" in dashboard.headers
            for credential in (cfg.proxy_api_key, cfg.access_token, override):
                if credential:
                    assert credential not in dashboard.text

            usage = await api.get(
                "/v1/dashboard/billing/usage", headers={**headers, "Accept": "application/json"},
            )
            assert usage.status_code == 200, usage.text
            assert usage.json()["data"]["remaining"] == 15
            assert usage.headers["cache-control"] == "no-store"

    assert len(observed) == 4
    assert {authorization for _, authorization in observed} == {
        "Bearer " + (override if override is not None else cfg.access_token)
    }


async def test_identical_chat_openers_remain_isolated_across_accounts_and_repeated_requests():
    """Real client session creation cannot reuse another request's conversation."""
    sessions = {}
    chats = []

    async def upstream(request):
        credential = request.headers["authorization"]
        if request.url.path.endswith("/create_session"):
            session_id = "session-" + str(len(sessions) + 1)
            sessions[session_id] = credential
            return httpx.Response(200, json={"code": 200, "data": session_id})
        assert request.url.path == "/api/ai/chat"
        payload = json.loads(await request.aread())
        session_id = payload["session_id"]
        assert sessions[session_id] == credential
        assert payload["conversation_id"] == session_id
        chats.append((session_id, credential))
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            text='data: {"data":{"answer":"Answer"}}\n\ndata: [DONE]\n\n',
        )

    cfg = configuration()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as raw:
        app = create_app(cfg)
        app.state.service = AntiFraudClient(cfg, raw)
        transport = httpx.ASGITransport(app, client=("198.51.100.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as api:
            for override in (None, "external-credential", None, "external-credential"):
                headers = {"Authorization": "Bearer " + cfg.proxy_api_key}
                if override is not None:
                    headers["X-Upstream-Token"] = override
                response = await api.post(
                    "/v1/chat/completions", headers=headers,
                    json={"messages": [{"role": "user", "content": "Same opener"}]},
                )
                assert response.status_code == 200, response.text
                assert response.json()["choices"][0]["message"]["content"] == "Answer"

    assert len(sessions) == len(chats) == 4
    assert len({session_id for session_id, _ in chats}) == 4
    assert [credential for _, credential in chats] == [
        "Bearer server-credential", "Bearer external-credential",
        "Bearer server-credential", "Bearer external-credential",
    ]
