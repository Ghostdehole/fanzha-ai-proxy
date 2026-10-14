"""Protocol regressions run offline against the real ASGI chat router."""
import asyncio
import json
import unittest
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from adapter import CHAT_MODELS, OpenAIAdapter
from errors import APIError
from client import AntiFraudClient
from config import AppConfig
from routers.chat import ClosingStreamingResponse, StreamState, _chat_text, _frame, _lines, router


class FakeResponse:
    def __init__(self, lines, raw_chunks=None):
        self.lines = lines
        self.raw_chunks = raw_chunks
        self.exhausted = False

    async def aiter_bytes(self, chunk_size=None):
        if self.raw_chunks is not None:
            for chunk in self.raw_chunks:
                yield chunk
            self.exhausted = True
            return
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield (line + "\n").encode("utf-8")
        self.exhausted = True


class FakeContext:
    def __init__(self, service):
        self.service = service

    async def __aenter__(self):
        if self.service.enter_error:
            raise self.service.enter_error
        self.service.opened += 1
        self.service.last_response = FakeResponse(self.service.lines, self.service.raw_chunks)
        return self.service.last_response

    async def __aexit__(self, *args):
        self.service.closed += 1


class FakeService:
    def __init__(self):
        self.cfg = SimpleNamespace(default_model="fanzha-ai", max_chat_chars=128000, max_upstream_response_bytes=8 * 1024 * 1024, require_stream_done=False)
        self.access_token = "server-token"
        self.lines = ['data: {"data":{"answer":"你好"}}', '', 'data: [DONE]', '']
        self.raw_chunks = None
        self.sessions = []
        self.calls = []
        self.opened = self.closed = 0
        self.enter_error = None
        self.session_error = None

    async def create_session(self, token=None):
        if self.session_error:
            raise self.session_error
        self.sessions.append((f"session-{len(self.sessions)}", token))
        return self.sessions[-1][0]

    async def request_chat_stream(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeContext(self)


def official_sse_lines(*nodes):
    """Synthetic public protocol shapes, without captured account identifiers."""
    lines = []
    for node in nodes:
        lines.extend(["data: " + json.dumps({"code": 200, "data": node}, ensure_ascii=False), ""])
    return lines


def sse_frames(text):
    return [json.loads(block[6:]) for block in text.split("\n\n") if block.startswith("data: ") and block != "data: [DONE]"]


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.service = FakeService()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.service = self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.auth = patch("routers.chat.get_upstream_token", return_value=None)
        self.auth.start()

    async def asyncTearDown(self):
        self.auth.stop()
        await self.client.aclose()

    async def post(self, **kwargs):
        body = {"messages": [{"role": "user", "content": "你好"}], **kwargs}
        return await self.client.post("/v1/chat/completions", json=body)

    async def test_nonstream_and_legacy_route(self):
        for path in ("/v1/chat/completions", "/chat/completions"):
            response = await self.client.post(path, json={"messages": [{"role": "user", "content": "你好"}]})
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertEqual(result["choices"][0]["message"]["content"], "你好")
            self.assertNotIn("usage", result)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_stream_output_and_unknown_usage(self):
        result = await self.post(stream=True, stream_options={"include_usage": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["x-accel-buffering"], "no")
        frames = sse_frames(result.text)
        self.assertEqual(frames[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(all(item["usage"] is None for item in frames))
        self.assertEqual(len({item["created"] for item in frames}), 1)
        self.assertTrue(result.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_full_history_and_independent_sessions(self):
        messages = [
            {"role": "system", "content": "简短回答"},
            {"role": "developer", "content": "使用中文"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，我可以帮助识别诈骗。"},
            {"role": "user", "content": "刚才提到什么？"},
        ]
        await self.post(messages=messages)
        await self.post(messages=messages)
        self.assertNotEqual(self.service.calls[0][0][0], self.service.calls[1][0][0])
        prompt = self.service.calls[0][0][1]
        for message in messages:
            self.assertIn(message["content"], prompt)
            self.assertIn('"role": "' + message["role"] + '"', prompt)
        self.assertIsNone(self.service.calls[0][0][3])

    async def test_single_user_unchanged_and_options_forwarded(self):
        result = await self.post(temperature=1.25, max_completion_tokens=512)
        self.assertEqual(result.status_code, 200)
        args, kwargs = self.service.calls[0]
        self.assertEqual(args[1], "你好")
        self.assertEqual(kwargs["temperature"], 1.25)
        self.assertEqual(kwargs["max_tokens"], 512)

    async def test_exact_modes_and_alias(self):
        for model, expected in CHAT_MODELS.items():
            result = await self.post(model=model)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["model"], model)
            kwargs = self.service.calls[-1][1]
            self.assertEqual((kwargs["is_deep_mode"], kwargs["is_english_mode"]), expected)

    async def test_image_order_and_message_association(self):
        await self.post(messages=[
            {"role": "user", "content": [{"type": "text", "text": "旧图片"}, {"type": "image_url", "image_url": {"url": "https://example.com/1.png"}}]},
            {"role": "assistant", "content": "待比较"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/2.png"}}, {"type": "text", "text": "新图片"}]},
        ])
        args, _ = self.service.calls[0]
        self.assertEqual(args[2], ["https://example.com/1.png", "https://example.com/2.png"])
        self.assertLess(args[1].index("旧图片"), args[1].index("[Image attachment 1]"))
        self.assertLess(args[1].index("[Image attachment 2]"), args[1].index("新图片"))

    async def test_multiline_sse_and_metadata(self):
        self.service.lines = [": heartbeat", "", "event: metadata", 'data: {"data":{"id":"not text"}}', "", "id: x", "retry: 1000", "event: message", 'data: {"data":', 'data: {"answer":"甲"}}', "", 'data:{"content":"乙"}', "", "event: done", ""]
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "甲乙")

    async def test_metadata_strings_never_become_assistant_content(self):
        self.service.lines = [
            "event: heartbeat", "data: ping", "",
            'data: {"type":"metadata","data":"private-session-id"}', "",
            "event: status", 'data: {"text":"internal-status"}', "",
            'data: {"answer":"answer"}', "", 'data: [DONE]', "",
        ]
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "answer")

    async def test_repeated_deltas_are_not_guessed_or_deduplicated(self):
        self.service.lines = ['data: {"answer":"a"}', '', 'data: {"answer":"aa"}', '', 'data: {"answer":"aa"}', '', 'data: [DONE]', '']
        response = await self.post()
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "aaaaa")

    async def test_neutral_numeric_sdk_defaults(self):
        response = await self.post(top_p=1.0, presence_penalty=0.0, frequency_penalty=0.0, top_logprobs=0.0)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.post(top_p=True)).status_code, 400)

    async def test_transport_boundaries_bom_unknown_fields_and_crlf(self):
        for separator in ("\r", "\n", "\r\n"):
            raw = ("\ufeffextension: ignored" + separator + 'data: {"answer":"汉字"}' + separator * 2 + 'data: [DONE]' + separator * 2).encode()
            self.service.raw_chunks = [bytes([byte]) for byte in raw]
            result = await self.post()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["choices"][0]["message"]["content"], "汉字")

    async def test_large_unterminated_line_and_invalid_utf8(self):
        self.service.cfg.max_upstream_response_bytes = 100
        self.service.raw_chunks = [b'data: {"answer":"', b"x" * 90]
        self.assertEqual((await self.post()).status_code, 502)
        self.service.raw_chunks = [b"data: \xff\n\n"]
        self.assertEqual((await self.post()).status_code, 502)

    async def test_invalid_unicode_is_a_request_or_upstream_error(self):
        raw = '{"messages":[{"role":"user","content":"' + "\\ud800" + '"}]}'
        result = await self.client.post("/v1/chat/completions", content=raw)
        self.assertEqual(result.status_code, 400)
        self.service.raw_chunks = [b'data: {"answer":"\\ud800"}\n\n']
        self.assertEqual((await self.post()).status_code, 502)

    async def test_official_answer_elapsed_marker_completes_in_both_modes(self):
        nodes = [
            {"type": "answer", "answer": ""},
            {"type": "node_status", "status": "running", "content": "internal node details"},
            {"type": "answer", "answer": "请"},
            {"type": "node_status", "status": "done", "text": "internal status text"},
            {"type": "answer", "answer": "核实"},
            {"type": "message_saved", "content": "internal persistence details"},
            {"type": "answer", "answer": "来电。"},
            {"type": "answer", "answer": "", "exec_elapsed_secs": "1.25"},
            {"type": "message_saved", "content": "internal final metadata"},
        ]
        for strict in (False, True):
            for stream in (False, True):
                with self.subTest(strict=strict, stream=stream):
                    self.service.cfg.require_stream_done = strict
                    self.service.lines = official_sse_lines(*nodes)
                    result = await self.post(stream=stream)
                    self.assertEqual(result.status_code, 200)
                    if stream:
                        frames = sse_frames(result.text)
                        self.assertFalse(any("error" in frame for frame in frames))
                        text = "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames)
                        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
                        self.assertTrue(result.text.endswith("data: [DONE]\n\n"))
                    else:
                        text = result.json()["choices"][0]["message"]["content"]
                    self.assertEqual(text, "请核实来电。")
                    self.assertTrue(self.service.last_response.exhausted)
                    self.assertEqual(self.service.opened, self.service.closed)

    async def test_elapsed_marker_preserves_final_text_and_closes_stream(self):
        self.service.cfg.require_stream_done = True
        for marker in (0, 0.0, "0", "0.0", "2.75", " 0.0 "):
            for stream in (False, True):
                with self.subTest(marker=marker, stream=stream):
                    self.service.lines = official_sse_lines({"type": "answer", "answer": "完整正文", "exec_elapsed_secs": marker})
                    # Native completion precedes the persistence trailer;
                    # the reader must keep consuming it through normal EOF.
                    self.service.lines.extend(official_sse_lines({"type": "message_saved", "content": "internal save metadata"}))
                    result = await self.post(stream=stream)
                    self.assertEqual(result.status_code, 200)
                    if stream:
                        frames = sse_frames(result.text)
                        self.assertFalse(any("error" in frame for frame in frames))
                        self.assertEqual("".join(frame["choices"][0]["delta"].get("content", "") for frame in frames), "完整正文")
                    else:
                        self.assertEqual(result.json()["choices"][0]["message"]["content"], "完整正文")
                    self.assertTrue(self.service.last_response.exhausted)
                    self.assertEqual(self.service.opened, self.service.closed)

    async def test_transport_failure_after_native_completion_is_not_hidden(self):
        self.service.cfg.require_stream_done = True
        self.service.lines = official_sse_lines({"type": "answer", "answer": "正文", "exec_elapsed_secs": "0.2"})
        self.service.lines.append(httpx.ReadError("interrupted persistence trailer"))
        self.assertEqual((await self.post()).status_code, 502)
        result = await self.post(stream=True)
        self.assertEqual(result.status_code, 200)
        self.assertIn("error", sse_frames(result.text)[-1])
        self.assertNotIn('"finish_reason": "stop"', result.text)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_cancellation_while_waiting_for_native_trailer_closes_connection(self):
        waiting_for_trailer = asyncio.Event()
        class TrailerStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'data: {"code":200,"data":{"type":"answer","answer":"text","exec_elapsed_secs":"0.2"}}\n\n'
                waiting_for_trailer.set()
                await asyncio.Event().wait()

            async def aclose(self):
                await asyncio.sleep(0)
                self.closed = True

        source = TrailerStream()
        cfg = AppConfig(access_token="synthetic-access", refresh_token="", token_state_file="", require_stream_done=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=source,
        ))) as http:
            options = OpenAIAdapter.validate_chat({"messages": [{"role": "user", "content": "question"}]}, "fanzha-ai")
            state = StreamState()
            upstream = _chat_text(AntiFraudClient(cfg, http), "synthetic-session", None, options, state)
            self.assertEqual(await upstream.__anext__(), "text")
            pending = asyncio.create_task(upstream.__anext__())
            await asyncio.wait_for(waiting_for_trailer.wait(), timeout=1)
            self.assertTrue(state.complete)
            self.assertFalse(source.closed)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await upstream.aclose()
            self.assertTrue(source.closed)

    async def test_explicit_terminal_marker_still_stops_without_waiting_for_eof(self):
        self.service.cfg.require_stream_done = True
        self.service.lines = official_sse_lines({"type": "answer", "answer": "正文"})
        self.service.lines.extend(["data: [DONE]", "", httpx.ReadError("must not read past DONE")])
        result = await self.post()
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.service.last_response.exhausted)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_empty_elapsed_marker_does_not_truncate_an_answer(self):
        self.service.cfg.require_stream_done = True
        for marker in (None, "", " "):
            self.service.lines = official_sse_lines(
                {"type": "answer", "answer": "前", "exec_elapsed_secs": marker},
                {"type": "answer", "answer": "后", "exec_elapsed_secs": "0.1"},
            )
            result = await self.post()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["choices"][0]["message"]["content"], "前后")
            self.service.lines = official_sse_lines({"type": "answer", "answer": "未完", "exec_elapsed_secs": marker})
            self.assertEqual((await self.post()).status_code, 502)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_metadata_elapsed_values_never_complete_or_leak_text(self):
        self.service.cfg.require_stream_done = True
        for node in (
            {"type": "node_status", "exec_elapsed_secs": "0.5", "text": "internal"},
            {"type": "message_saved", "exec_elapsed_secs": "0.5", "content": "internal"},
            {"type": "metadata", "exec_elapsed_secs": "0.5", "answer": "internal"},
            {"type": "answer", "answer": "", "metadata": {"exec_elapsed_secs": "0.5"}},
        ):
            with self.subTest(node=node):
                self.service.lines = official_sse_lines(
                    {"type": "answer", "answer": "前"}, node,
                    {"type": "answer", "answer": "后", "exec_elapsed_secs": "0.2"},
                )
                result = await self.post()
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["choices"][0]["message"]["content"], "前后")
                self.service.lines = official_sse_lines({"type": "answer", "answer": "正文"}, node)
                self.assertEqual((await self.post()).status_code, 502)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_malformed_elapsed_markers_fail_safely_before_and_during_stream(self):
        for marker in (True, False, -1, -0.5, "-0.1", "-1e-999", "NaN", "Infinity", "-Infinity", "1e999", "not-a-number", "1_0", [], {}):
            with self.subTest(marker=marker):
                for strict in (False, True):
                    self.service.cfg.require_stream_done = strict
                    self.service.lines = official_sse_lines({"type": "answer", "answer": "正文", "exec_elapsed_secs": marker})
                    self.assertEqual((await self.post(stream=True)).status_code, 502)
                    self.service.lines = official_sse_lines(
                        {"type": "answer", "answer": "部分"},
                        {"type": "answer", "answer": "", "exec_elapsed_secs": marker},
                    )
                    self.assertEqual((await self.post()).status_code, 502)
                    result = await self.post(stream=True)
                    self.assertEqual(result.status_code, 200)
                    frames = sse_frames(result.text)
                    self.assertIn("error", frames[-1])
                    self.assertNotIn('"finish_reason": "stop"', result.text)
                    self.assertTrue(result.text.endswith("data: [DONE]\n\n"))
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_elapsed_completion_without_any_text_is_still_an_error(self):
        self.service.lines = official_sse_lines({"type": "answer", "answer": "", "exec_elapsed_secs": 0})
        for stream in (False, True):
            self.assertEqual((await self.post(stream=stream)).status_code, 502)
        self.assertEqual(self.service.opened, self.service.closed)

    async def test_known_finish_reason(self):
        self.service.lines = ['data: {"data":{"answer":"截断","finish_reason":"length"}}', '']
        result = await self.post()
        self.assertEqual(result.json()["choices"][0]["finish_reason"], "length")

    async def test_initial_upstream_errors_have_http_error_status(self):
        for stream in (False, True):
            for error, expected in ((APIError("Unauthorized", status_code=401), 401), (APIError("Limited", status_code=429), 429)):
                self.service.enter_error = error
                result = await self.post(stream=stream)
                self.assertEqual(result.status_code, expected)
                self.assertIn("error", result.json())

    async def test_session_creation_failure(self):
        self.service.session_error = APIError("Unauthorized", status_code=401)
        result = await self.post(stream=True)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(len(self.service.calls), 0)

    async def test_sse_error_initial_and_after_text(self):
        error = 'data: {"code":401,"message":"expired"}'
        self.service.lines = [error, '']
        result = await self.post(stream=True)
        self.assertEqual(result.status_code, 401)
        self.service.lines = ['data: {"answer":"部分"}', '', error, '']
        for stream in (False, True):
            result = await self.post(stream=stream)
            self.assertEqual(result.status_code, 200 if stream else 401)
            if stream:
                frames = sse_frames(result.text)
                self.assertIn("error", frames[-1])
                self.assertFalse(any(frame.get("choices", [{}])[0].get("finish_reason") == "stop" for frame in frames))
        self.assertEqual(self.service.closed, self.service.opened)

    async def test_timeouts_and_network_breaks(self):
        for error, status in ((httpx.ReadTimeout("timeout"), 504), (httpx.ReadError("broken"), 502)):
            self.service.lines = [error]
            self.assertEqual((await self.post(stream=True)).status_code, status)
            self.service.lines = ['data: {"answer":"部分"}', '', error]
            result = await self.post(stream=True)
            self.assertIn("error", sse_frames(result.text)[-1])
            self.assertNotIn('"finish_reason": "stop"', result.text)
        self.assertEqual(self.service.closed, self.service.opened)

    async def test_invalid_and_empty_upstream_never_fabricate_answer(self):
        cases = [[], ['data: [DONE]', ''], ['data: []', ''], ['data: {invalid}', ''], ['data: {"answer":123}', ''], ['data: {"data":123}', ''], ['event: error', 'data: {"message":"error"}', ''], ['<html>error</html>']]
        for lines in cases:
            with self.subTest(lines=lines):
                self.service.lines = lines
                for stream in (False, True):
                    result = await self.post(stream=stream)
                    self.assertEqual(result.status_code, 502)
                    self.assertIn("error", result.json())
        self.assertEqual(self.service.closed, self.service.opened)

    async def test_normal_eof_compatible_and_strict_mode_detects_truncation(self):
        self.service.lines = ['data: {"answer":"正文"}', '']
        self.assertEqual((await self.post()).status_code, 200)
        self.service.cfg.require_stream_done = True
        self.assertEqual((await self.post()).status_code, 502)
        result = await self.post(stream=True)
        self.assertIn("error", sse_frames(result.text)[-1])
        self.assertNotIn('"finish_reason": "stop"', result.text)

    async def test_incomplete_json_after_text_is_not_success(self):
        self.service.lines = ['data: {"answer":"正文"}', '', 'data: {"answer":']
        self.assertEqual((await self.post()).status_code, 502)

    async def test_bounds(self):
        self.service.cfg.max_chat_chars = 2
        self.assertEqual((await self.post(messages=[{"role": "user", "content": "abc"}])).status_code, 400)
        self.service.cfg.max_upstream_response_bytes = 3
        self.assertEqual((await self.post()).status_code, 502)

    async def test_nonobject_and_invalid_json(self):
        for value in ([], 1, None, "text", False):
            response = await self.client.post("/v1/chat/completions", json=value)
            self.assertEqual(response.status_code, 400)
        response = await self.client.post("/v1/chat/completions", content=b'{"messages":')
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/v1/chat/completions", content=b'\xff')
        self.assertEqual(response.status_code, 400)

    async def test_invalid_parameters(self):
        cases = [
            {"model": None}, {"model": "fanzha-ai-en-deeper"}, {"model": []},
            {"stream": "false"}, {"stream": 1}, {"temperature": True},
            {"temperature": -0.1}, {"temperature": 2.1}, {"temperature": None},
            {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": 1.2},
            {"max_tokens": 4, "max_completion_tokens": 5}, {"n": 2}, {"n": True},
            {"messages": []}, {"messages": "hi"}, {"messages": [1]},
            {"messages": [{"role": "tool", "content": "result"}]},
            {"messages": [{"role": [], "content": "result"}]},
            {"messages": [{"role": "user", "content": 1}]},
            {"messages": [{"role": "user", "content": None}]},
            {"messages": [{"role": "user", "content": " "}]},
            {"messages": [{"role": "assistant", "content": "hi"}]},
            {"messages": [{"role": "user", "content": [{"type": "text", "text": 3}]}]},
            {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]},
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": None}]}]},
            {"tools": [{"type": "function"}]}, {"stop": ["end"]},
            {"response_format": {"type": "json_object"}}, {"top_p": 0.9},
            {"stream_options": {}}, {"stream": True, "stream_options": []},
            {"stream": True, "stream_options": {"include_usage": 1}},
            {"stream": True, "stream_options": {"unknown": True}},
            {"user": None}, {"unrecognized": 1},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = await self.post(**body)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.service.sessions, [])

    async def test_nonfinite_numbers(self):
        for numeric in ("NaN", "Infinity", "-Infinity", "1e9999"):
            response = await self.client.post("/v1/chat/completions", content='{"messages":[{"role":"user","content":"hi"}],"temperature":' + numeric + '}')
            self.assertEqual(response.status_code, 400)

    async def test_real_client_refresh_is_used_by_subsequent_chat(self):
        calls = []
        async def handler(request):
            calls.append((request.url.path, request.headers.get("authorization")))
            if request.url.path.endswith("/token/refresh"):
                return httpx.Response(200, json={"code": 200, "data": {"access_token": "new-access"}})
            if request.url.path.endswith("/create_session"):
                if request.headers["authorization"] == "Bearer old-access":
                    return httpx.Response(401, json={"code": 401})
                return httpx.Response(200, json={"code": 200, "data": "real-session"})
            self.assertEqual(request.headers["authorization"], "Bearer new-access")
            self.assertEqual(json.loads(request.content)["text"], "你好")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"answer":"已续期"}\n\ndata: [DONE]\n\n')
        cfg = AppConfig(access_token="old-access", refresh_token="refresh-token", base_url="https://upstream.test", token_state_file="")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream_client:
            self.app.state.service = AntiFraudClient(cfg, upstream_client)
            result = await self.post()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["choices"][0]["message"]["content"], "已续期")
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1][1], "Bearer new-access")

    async def test_first_sse_line_is_available_before_upstream_eof(self):
        gate = asyncio.Event()
        class LiveStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"answer":"first"}\n\n'
                await gate.wait()
                yield b'data: [DONE]\n\n'
        response = httpx.Response(200, stream=LiveStream())
        lines = _lines(response, 1024)
        try:
            first = await asyncio.wait_for(lines.__anext__(), timeout=0.5)
            self.assertEqual(first, 'data: {"answer":"first"}')
            self.assertFalse(gate.is_set())
        finally:
            await lines.aclose()
            await response.aclose()

    async def test_response_send_failure_closes_preopened_upstream(self):
        closed = []
        async def upstream():
            try:
                yield "first"
                yield "second"
            finally:
                closed.append(True)
        source = upstream()
        await source.__anext__()
        response = ClosingStreamingResponse(source, upstream=source)
        async def send(_):
            raise OSError("client disconnected")
        async def receive():
            await asyncio.Event().wait()
        with self.assertRaises(BaseException):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertEqual(closed, [True])

    async def test_disconnect_closes_preopened_upstream(self):
        closed = []
        async def upstream():
            try:
                yield "first"
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        source = upstream()
        await source.__anext__()
        response = ClosingStreamingResponse(source, upstream=source)
        async def send(_):
            pass
        async def receive():
            return {"type": "http.disconnect"}
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()


def test_sse_empty_text_fields_fall_back_to_populated_fields():
    from routers.chat import _frame
    for event in [
        {"data": {"answer": "", "content": "visible"}},
        {"answer": "", "text": "visible"},
        {"data": {"answer": ""}, "text": "visible"},
    ]:
        assert _frame(json.dumps(event), "") == ("visible", None)


def test_elapsed_completion_requires_the_exact_answer_node_location():
    for event in [
        {"type": "answer", "answer": "text", "exec_elapsed_secs": "0.2"},
        {"exec_elapsed_secs": "0.2", "data": {"type": "answer", "answer": "text"}},
        {"data": {"answer": "text", "exec_elapsed_secs": "0.2"}},
        {"data": {"type": "answer", "answer": "text", "metadata": {"exec_elapsed_secs": "0.2"}}},
    ]:
        assert _frame(json.dumps(event), "")[1] is None
    # Metadata event names take precedence even when their body resembles an answer.
    assert _frame(json.dumps({"data": {"type": "answer", "answer": "internal", "exec_elapsed_secs": "0.2"}}), "metadata") == (None, None)


def test_elapsed_completion_never_overrides_explicit_finish_reason_or_errors():
    event = {"code": 200, "data": {"type": "answer", "answer": "text", "exec_elapsed_secs": "0.2", "finish_reason": "length"}}
    assert _frame(json.dumps(event), "") == ("text", "length")
    for marker in (float("nan"), float("inf"), float("-inf")):
        event["data"]["exec_elapsed_secs"] = marker
        try:
            _frame(json.dumps(event), "")
        except APIError as error:
            assert error.status_code == 502
        else:
            raise AssertionError("Non-finite marker must be rejected")
    event["data"]["exec_elapsed_secs"] = "0.2"
    event["code"] = 403
    try:
        _frame(json.dumps(event), "")
    except APIError as error:
        assert error.status_code == 403
    else:
        raise AssertionError("Completion marker must not hide upstream errors")
