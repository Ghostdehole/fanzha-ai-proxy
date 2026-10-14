import asyncio
import base64
import io
import json
import os
from types import SimpleNamespace
import wave

import httpx
import pytest

from client import AntiFraudClient
from errors import APIError


def config(**overrides):
    values = dict(base_url="https://upstream.test", access_token="old-token",
                  refresh_token="refresh-token", token_state_file="",
                  max_upstream_response_bytes=8 * 1024 * 1024, max_tts_chars=20000)
    values.update(overrides)
    return SimpleNamespace(**values)


def wav_audio():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 20)
    return buffer.getvalue()


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("endpoint", ["session", "usage", "tts", "asr", "chat"])
@pytest.mark.parametrize("business_401", [False, True])
def test_server_credentials_refresh_on_every_endpoint(endpoint, business_401):
    async def scenario():
        requests = []

        def handler(request):
            requests.append((request.url.path, request.headers["authorization"]))
            if request.url.path.endswith("/refresh"):
                return httpx.Response(200, json={"code": 200, "data": {
                    "accessToken": "new-token", "refreshToken": "new-refresh"}})
            if request.headers["authorization"] == "Bearer old-token":
                return httpx.Response(200 if business_401 else 401, json={"code": "401"})
            if endpoint == "session":
                return httpx.Response(200, json={"code": 200, "data": {"session_id": "fresh-session"}})
            if endpoint == "usage":
                return httpx.Response(200, json={"code": 200, "data": {"remaining": 5}})
            if endpoint == "tts":
                return httpx.Response(200, json={"code": 200, "data": {"audio": base64.b64encode(wav_audio()).decode()}})
            if endpoint == "asr":
                return httpx.Response(200, json={"code": 200, "data": "recognized"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            if endpoint == "session":
                assert await service.create_session() == "fresh-session"
            elif endpoint == "usage":
                assert (await service.fetch_usage())["data"]["remaining"] == 5
            elif endpoint == "tts":
                assert await service.synthesize_speech("hello") == wav_audio()
            elif endpoint == "asr":
                assert await service.transcribe_audio(b"audio", "test.wav", "audio/wav") == "recognized"
            else:
                async with await service.request_chat_stream("s", "hello", []) as response:
                    assert "[DONE]" in (await response.aread()).decode()
            assert [token for _, token in requests] == ["Bearer old-token", "Bearer refresh-token", "Bearer new-token"]
            assert service.refresh_token == "new-refresh"
    asyncio.run(scenario())


def test_chat_uses_latest_server_token_after_session_refresh():
    async def scenario():
        authorizations = []

        def handler(request):
            authorizations.append(request.headers["authorization"])
            if request.url.path.endswith("/refresh"):
                return httpx.Response(200, json={"data": {"access_token": "new-token"}})
            if request.url.path.endswith("/create_session"):
                if request.headers["authorization"] == "Bearer old-token":
                    return httpx.Response(401)
                return httpx.Response(200, json={"data": "session"})
            payload = json.loads(request.content)
            assert payload["temperature"] == "0.7"
            assert payload["max_tokens"] == 777
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            session = await service.create_session()
            async with await service.request_chat_stream(session, "hello", [], temperature=0.7, max_tokens=777) as response:
                await response.aread()
        assert authorizations == ["Bearer old-token", "Bearer refresh-token", "Bearer new-token", "Bearer new-token"]
    asyncio.run(scenario())


def test_concurrent_expired_requests_refresh_only_once():
    async def scenario():
        old_requests = 0
        refresh_requests = 0
        both_old = asyncio.Event()

        async def handler(request):
            nonlocal old_requests, refresh_requests
            if request.url.path.endswith("/refresh"):
                refresh_requests += 1
                return httpx.Response(200, json={"data": {"access_token": "new-token"}})
            if request.headers["authorization"] == "Bearer old-token":
                old_requests += 1
                if old_requests == 2:
                    both_old.set()
                await both_old.wait()
                return httpx.Response(401)
            return httpx.Response(200, json={"data": {"remaining": 3}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            results = await asyncio.wait_for(asyncio.gather(service.fetch_usage(), service.fetch_usage()), timeout=2)
            assert len(results) == 2
        assert old_requests == 2
        assert refresh_requests == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("external_token", ["external-token", "old-token"])
def test_external_credentials_never_borrow_server_refresh(external_token):
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(401, json={"code": 401})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            with pytest.raises(APIError) as caught:
                await service.fetch_usage(external_token)
            assert caught.value.status_code == 401
        assert len(requests) == 1
        assert not requests[0].endswith("/refresh")
    asyncio.run(scenario())


def test_authentication_retry_is_bounded():
    async def scenario():
        refresh_requests = 0
        calls = 0

        def handler(request):
            nonlocal refresh_requests, calls
            if request.url.path.endswith("/refresh"):
                refresh_requests += 1
                return httpx.Response(200, json={"data": {"access_token": "new-token"}})
            calls += 1
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).fetch_usage()
            assert caught.value.status_code == 401
        assert calls == 2
        assert refresh_requests == 1
    asyncio.run(scenario())


def test_refresh_without_access_token_does_not_claim_success():
    async def scenario():
        def handler(request):
            if request.url.path.endswith("/refresh"):
                return httpx.Response(200, json={"code": 200, "data": {"refresh_token": "new-refresh"}})
            return httpx.Response(401)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            with pytest.raises(APIError) as caught:
                await service.fetch_usage()
            assert caught.value.status_code == 502
            assert service.access_token == "old-token"
            assert service.refresh_token == "refresh-token"
    asyncio.run(scenario())


@pytest.mark.parametrize("status,expected", [(403, 403), (429, 429), (500, 502), (503, 502), (302, 502)])
def test_http_errors_are_normalized_without_upstream_body(status, expected):
    async def scenario():
        def handler(request):
            return httpx.Response(status, headers={"Retry-After": "12"}, content="secret-token-error-body")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).fetch_usage()
            assert caught.value.status_code == expected
            assert "secret-token" not in caught.value.message
            if status == 429:
                assert caught.value.headers == {"Retry-After": "12"}
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{"data": {}}, {"data": None}, {"data": {"data": {"unexpected": 1}}}, {"data": "  "}])
def test_session_failure_does_not_invent_uuid(body):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).create_session()
            assert caught.value.status_code == 502
    asyncio.run(scenario())


def test_same_anchor_creates_independent_sessions_without_global_lock():
    async def scenario():
        calls = 0
        both_started = asyncio.Event()

        async def handler(request):
            nonlocal calls
            calls += 1
            session = f"session-{calls}"
            if calls == 2:
                both_started.set()
            await both_started.wait()
            return httpx.Response(200, json={"data": session})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            sessions = await asyncio.wait_for(asyncio.gather(service.get_session_id("same"), service.get_session_id("same")), timeout=2)
            assert len(set(sessions)) == 2
    asyncio.run(scenario())


def test_stream_failure_after_start_closes_without_retry():
    async def scenario():
        stream = TrackedStream([b'data: {"data":"part"}\n\n'], httpx.ReadTimeout("sensitive detail"))
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(APIError) as caught:
                async with await AntiFraudClient(config(), http).request_chat_stream("s", "hello", []) as response:
                    async for _ in response.aiter_lines():
                        pass
            assert caught.value.status_code == 504
            assert "sensitive" not in caught.value.message
        assert stream.closed
        assert calls == 1
    asyncio.run(scenario())


def test_stream_cancellation_closes_response():
    async def scenario():
        stream = TrackedStream([b"data: [DONE]\n\n"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream))) as http:
            with pytest.raises(asyncio.CancelledError):
                async with await AntiFraudClient(config(), http).request_chat_stream("s", "hello", []):
                    raise asyncio.CancelledError()
        assert stream.closed
    asyncio.run(scenario())


def test_non_sse_success_is_protocol_error_and_closed():
    async def scenario():
        stream = TrackedStream([b'{"code":200,"data":"not an SSE response"}'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream))) as http:
            with pytest.raises(APIError) as caught:
                async with await AntiFraudClient(config(), http).request_chat_stream("s", "hello", []):
                    pytest.fail("invalid stream should not be yielded")
            assert caught.value.status_code == 502
        assert stream.closed
    asyncio.run(scenario())


def test_json_response_limit_closes_response():
    async def scenario():
        stream = TrackedStream([b'{"data":"', b"x" * 100, b'"}'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(max_upstream_response_bytes=32), http).fetch_usage()
            assert caught.value.status_code == 502
        assert stream.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("audio", [None, "!!!", ""])
def test_tts_does_not_skip_bad_chunks(audio):
    async def scenario():
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            value = base64.b64encode(wav_audio()).decode() if calls == 1 else audio
            return httpx.Response(200, json={"data": {"audio": value}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).synthesize_speech("hello." * 60)
            assert caught.value.status_code == 502
        assert calls == 2
    asyncio.run(scenario())


def test_asr_rejects_non_text_data():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"data": {"unexpected": "value"}}))) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).transcribe_audio(b"audio", "test.wav", "audio/wav")
            assert caught.value.status_code == 502
    asyncio.run(scenario())


def test_rotated_tokens_persist_atomically_and_bind_original_credentials(tmp_path):
    async def scenario():
        state = tmp_path / "tokens.json"
        cfg = config(token_state_file=str(state))
        def handler(request):
            if request.url.path.endswith("/refresh"):
                return httpx.Response(200, json={"data": {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}})
            if request.headers["authorization"] == "Bearer old-token":
                return httpx.Response(401)
            return httpx.Response(200, json={"data": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await AntiFraudClient(cfg, http).fetch_usage()
            restored = AntiFraudClient(cfg, http)
            assert restored.access_token == "rotated-access"
            assert restored.refresh_token == "rotated-refresh"
            different = AntiFraudClient(config(access_token="other-account", token_state_file=str(state)), http)
            assert different.access_token == "other-account"
        assert list(tmp_path.iterdir()) == [state]
        if os.name != "nt":
            assert state.stat().st_mode & 0o777 == 0o600
    asyncio.run(scenario())


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_upstream_json_rejects_non_finite_numbers_in_nested_metadata(number):
    async def scenario():
        body = ('{"code":200,"data":{"remaining":3},"metadata":[{"value":' + number + '}]}').encode()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as http:
            with pytest.raises(APIError) as caught:
                await AntiFraudClient(config(), http).fetch_usage()
            assert caught.value.status_code == 502
            assert caught.value.message == "Upstream returned invalid JSON."
    asyncio.run(scenario())


def test_concurrent_refresh_failures_have_short_credential_bound_cooldown(monkeypatch):
    async def scenario():
        clock = [100.0]
        monkeypatch.setattr("client.time", SimpleNamespace(monotonic=lambda: clock[0]))
        old_requests = 0
        refresh_requests = 0
        both_old = asyncio.Event()

        async def handler(request):
            nonlocal old_requests, refresh_requests
            if request.url.path.endswith("/refresh"):
                refresh_requests += 1
                if request.headers["authorization"] == "Bearer replaced-refresh":
                    return httpx.Response(200, json={"data": {"access_token": "new-token"}})
                return httpx.Response(401, content="secret-upstream-error")
            if request.headers["authorization"] == "Bearer old-token":
                old_requests += 1
                if old_requests >= 2:
                    both_old.set()
                await both_old.wait()
                return httpx.Response(401)
            return httpx.Response(200, json={"data": {"remaining": 1}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(), http)
            results = await asyncio.wait_for(asyncio.gather(service.fetch_usage(), service.fetch_usage(), return_exceptions=True), timeout=2)
            assert all(isinstance(error, APIError) and error.status_code == 401 for error in results)
            assert results[0].as_dict() == results[1].as_dict()
            assert refresh_requests == 1
            with pytest.raises(APIError):
                await service.fetch_usage()
            assert refresh_requests == 1
            clock[0] += 5.01
            with pytest.raises(APIError):
                await service.fetch_usage()
            assert refresh_requests == 2
            service.refresh_token = "replaced-refresh"
            assert (await service.fetch_usage())["data"]["remaining"] == 1
            assert refresh_requests == 3
            assert service._refresh_failure is None
    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["json", "chat"])
@pytest.mark.parametrize("cancel_at_eof", [False, True])
def test_anyio_cancel_scope_shields_response_cleanup(endpoint, cancel_at_eof):
    import anyio

    async def scenario():
        class CancellableStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                if cancel_at_eof:
                    yield b'{"data":{}}' if endpoint == "json" else b"data: [DONE]\n\n"
                    cancel_scope.cancel()
                    return
                cancel_scope.cancel()
                await anyio.sleep(0)
                yield b"unreachable"

            async def aclose(self):
                await anyio.sleep(0)
                self.closed = True

        upstream_stream = CancellableStream()
        headers = {"content-type": "text/event-stream" if endpoint == "chat" else "application/json"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers=headers, stream=upstream_stream))) as http:
            service = AntiFraudClient(config(), http)
            with anyio.CancelScope() as cancel_scope:
                if endpoint == "json":
                    await service.fetch_usage()
                else:
                    async with await service.request_chat_stream("s", "hello", []) as response:
                        async for _ in response.aiter_lines():
                            pass
            assert upstream_stream.closed
    anyio.run(scenario)


def official_response(inner_code="10000", payload="wrapped-session", **outer):
    return {"result": True, "succeed": True, "resultCode": 0,
            "data": {"code": inner_code, "data": payload}, **outer}


@pytest.mark.parametrize("chat_key", ["example-chat-key", ""])
def test_chat_key_is_optional_and_scoped_to_chat_only(chat_key):
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            path = request.url.path
            if path.endswith("/refresh"):
                return httpx.Response(200, json={"data": {"access_token": "new-token"}})
            if path.endswith("/create_session"):
                return httpx.Response(200, json={"data": "session"})
            if path.endswith("/usage"):
                return httpx.Response(200, json={"data": {"remaining": 5}})
            if path.endswith("/volcengine"):
                return httpx.Response(200, json={"data": {"audio": base64.b64encode(wav_audio()).decode()}})
            if path.endswith("/audio"):
                return httpx.Response(200, json={"data": "recognized"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(chat_key=chat_key), http)
            assert await service.create_session() == "session"
            await service.fetch_usage()
            await service.synthesize_speech("hello")
            await service.transcribe_audio(b"audio", "test.wav", "audio/wav")
            await service.refresh_token_if_needed()
            async with await service.request_chat_stream("session", "hello", []) as response:
                await response.aread()
            assert "x-chat-key" not in {key.lower() for key in service.build_headers("token")}
        assert len(requests) == 6
        for request in requests:
            expected = chat_key if request.url.path.endswith("/chat") and chat_key else None
            assert request.headers.get("x-chat-key") == expected
    asyncio.run(scenario())


def test_unset_chat_key_preserves_legacy_configuration_behavior():
    async def scenario():
        def handler(request):
            assert "x-chat-key" not in request.headers
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: [DONE]\n\n")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with await AntiFraudClient(config(), http).request_chat_stream("session", "hello", []) as response:
                await response.aread()
    asyncio.run(scenario())


def test_recognized_official_session_wrapper_is_accepted():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=official_response())
        )) as http:
            assert await AntiFraudClient(config(), http).create_session() == "wrapped-session"
    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["session", "chat"])
@pytest.mark.parametrize("source", ["inner", "outer"])
def test_official_wrapper_401_refreshes_and_chat_key_survives_retry(endpoint, source):
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/refresh"):
                assert "x-chat-key" not in request.headers
                return httpx.Response(200, json={"data": {"access_token": "new-token"}})
            if request.headers["authorization"] == "Bearer old-token":
                body = official_response("401", None) if source == "inner" else official_response(
                    result=False, succeed=False, resultCode=401)
                return httpx.Response(200, json=body)
            if endpoint == "session":
                return httpx.Response(200, json=official_response())
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: [DONE]\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(chat_key="example-chat-key"), http)
            if endpoint == "session":
                assert await service.create_session() == "wrapped-session"
            else:
                async with await service.request_chat_stream("session", "hello", []) as response:
                    await response.aread()
        assert [request.headers["authorization"] for request in requests] == ["Bearer old-token", "Bearer refresh-token", "Bearer new-token"]
        for request in requests:
            assert request.headers.get("x-chat-key") == ("example-chat-key" if request.url.path.endswith("/chat") else None)
    asyncio.run(scenario())


@pytest.mark.parametrize("body,expected", [
    (official_response("403", None), 403),
    (official_response("429", None), 429),
    (official_response(result=False, succeed=False, resultCode=403), 403),
    (official_response(result=False), 502),
    (official_response(succeed=False), 502),
    ({"result": False, "data": "should-not-be-used"}, 502),
    ({"succeed": False, "data": "should-not-be-used"}, 502),
    ({"resultCode": 500, "data": "should-not-be-used"}, 502),
    (official_response(True, None), 502),
    (official_response(401.5, None), 502),
])
@pytest.mark.parametrize("endpoint", ["session", "chat"])
def test_official_wrapper_failures_do_not_refresh_or_disclose_messages(body, expected, endpoint):
    async def scenario():
        calls = []
        message = "example-chat-key private-upstream-message"

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={**body, "message": message}, headers={"retry-after": "12"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            service = AntiFraudClient(config(chat_key="example-chat-key"), http)
            with pytest.raises(APIError) as caught:
                if endpoint == "session":
                    await service.create_session()
                else:
                    async with await service.request_chat_stream("session", "hello", []):
                        pytest.fail("failed chat response must not be opened")
            assert caught.value.status_code == expected
            assert "example-chat-key" not in str(caught.value)
            assert "private-upstream-message" not in json.dumps(caught.value.as_dict())
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"data": {"code": 401, "error": "ordinary-domain-value"}},
    {"code": 200, "data": {"code": 403}},
    {"data": {"result": False, "succeed": False, "resultCode": 401}},
    official_response(payload={"code": 401, "error": "ordinary-domain-value"}),
    {"result": {"code": 403}, "succeed": "domain-label", "data": {"code": 401}},
])
def test_arbitrary_payload_fields_are_not_recursively_treated_as_errors(body):
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await AntiFraudClient(config(), http).fetch_usage() == body
        assert len(calls) == 1
    asyncio.run(scenario())
