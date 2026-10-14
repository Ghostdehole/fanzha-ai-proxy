"""Chat routes: request-scoped sessions and shared streaming/non-stream parsing."""
import asyncio
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import AsyncIterator, Optional, Tuple

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from adapter import CHAT_MODELS, OpenAIAdapter
from auth import get_upstream_token
from config import config, logger
from errors import APIError

router = APIRouter(tags=["Chat"])


@dataclass
class StreamState:
    finish_reason: str = "stop"
    complete: bool = False
    has_text: bool = False


def upstream_error(message: str) -> APIError:
    return APIError(message, status_code=502, err_type="upstream_error")


def _answer_completed(node: dict) -> bool:
    """The official answer event reports elapsed seconds on its final frame."""
    if node.get("type") != "answer" or "exec_elapsed_secs" not in node:
        return False
    elapsed = node["exec_elapsed_secs"]
    if elapsed is None or (isinstance(elapsed, str) and not elapsed.strip()):
        return False
    try:
        if type(elapsed) not in (int, float, str):
            raise ValueError("Invalid elapsed time type")
        if isinstance(elapsed, str) and not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", elapsed.strip()):
            raise ValueError("Invalid elapsed time syntax")
        # Decimal checks the sign exactly, including negative values that would
        # underflow to -0.0; float finiteness rejects NaN/infinity/overflow.
        seconds = Decimal(str(elapsed))
        if not seconds.is_finite() or seconds < 0 or not math.isfinite(float(seconds)):
            raise ValueError("Invalid elapsed time value")
    except (InvalidOperation, ValueError, OverflowError):
        raise upstream_error("Upstream returned an invalid answer completion marker.") from None
    return True


@dataclass(frozen=True)
class ParsedFrame:
    text: Optional[str] = None
    finish_reason: Optional[str] = None
    wait_for_eof: bool = False


def _parse_frame(data: str, event_name: str) -> ParsedFrame:
    """Parse content and completion without mistaking metadata for an answer."""
    if event_name.lower() in {"error", "failed", "failure"}:
        raise upstream_error("Upstream reported a stream error.")
    if data.strip() == "[DONE]":
        return ParsedFrame(finish_reason="stop")
    if event_name.lower() in {"ping", "pong", "heartbeat", "keepalive", "keep-alive"}:
        return ParsedFrame()
    terminal = event_name.lower() in {"done", "end", "complete", "completed", "finish", "finished"}
    if not data.strip():
        return ParsedFrame(finish_reason="stop" if terminal else None)
    try:
        event = json.loads(data)
    except (ValueError, TypeError):
        raise upstream_error("Upstream returned malformed SSE JSON.") from None
    if not isinstance(event, dict):
        raise upstream_error("Upstream returned an invalid SSE event object.")
    node = event.get("data")
    nodes = [event] + ([node] if isinstance(node, dict) else [])
    finish_reason = "stop" if terminal else None
    for obj in nodes:
        if obj.get("error") not in (None, False, "", {}, []) or obj.get("success") is False or str(obj.get("type", "")).lower() in {"error", "failed", "failure"}:
            raise upstream_error("Upstream reported a stream error.")
        code = obj.get("code")
        if code is not None and str(code) not in ("0", "200", "10000"):
            status_code = int(code) if str(code) in {"401", "403", "429"} else 502
            err_type = "authentication_error" if status_code == 401 else "upstream_error"
            raise APIError("Upstream rejected the chat request.", status_code=status_code, err_type=err_type)
        if any(obj.get(key) is True for key in ("done", "finished", "is_end", "is_final")) or str(obj.get("type", "")).lower() in {"done", "end", "complete", "completed", "finish", "finished"}:
            finish_reason = "stop"
        reason = obj.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or reason not in {"stop", "length", "content_filter"}:
                raise upstream_error("Upstream returned an unsupported completion reason.")
            finish_reason = reason
    metadata_types = {"metadata", "usage", "status", "session", "session_created", "start", "message_start", "node_status", "message_saved", "ping", "pong", "heartbeat", "keepalive", "keep-alive"}
    if event_name.lower() in metadata_types or any(str(obj.get("type", "")).lower() in metadata_types for obj in nodes):
        return ParsedFrame(finish_reason=finish_reason)
    wait_for_eof = False
    if isinstance(node, dict) and _answer_completed(node):
        # Native elapsed-time completion precedes message_saved. Like the
        # official reader, consume this persistence trailer through normal EOF.
        # Existing explicit terminal markers retain their immediate stop policy.
        wait_for_eof = finish_reason is None
        finish_reason = finish_reason or "stop"
    text = None
    for obj in reversed(nodes):
        for key in ("answer", "content", "text"):
            if key in obj and obj[key] is not None:
                if not isinstance(obj[key], str):
                    raise upstream_error("Upstream returned non-text chat output.")
                if obj[key]:
                    text = obj[key]
                    break
        if text is not None:
            break
    if text is None and isinstance(node, str):
        text = node
    elif node is not None and not isinstance(node, (dict, str)):
        raise upstream_error("Upstream returned an invalid SSE data object.")
    if text is not None:
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise upstream_error("Upstream returned invalid Unicode text.") from None
    return ParsedFrame(text, finish_reason, wait_for_eof)


def _frame(data: str, event_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Compatibility view for callers that only need content and finish reason."""
    frame = _parse_frame(data, event_name)
    return frame.text, frame.finish_reason


async def _lines(response, max_bytes: int) -> AsyncIterator[str]:
    """Bound the decoded response before buffering even one unterminated line."""
    pending = b""
    skip_lf = False
    first = True
    size = 0
    # No chunk_size: HTTPX would buffer until that many bytes before yielding,
    # delaying SSE tokens and even deadlocking a live stream.
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise upstream_error("Upstream chat response exceeds the configured size limit.")
        if not chunk:
            continue
        if skip_lf and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        skip_lf = False
        data = pending + chunk
        start = 0
        for match in re.finditer(b"\r\n|\r|\n", data):
            raw_line = data[start:match.start()]
            start = match.end()
            try:
                line = raw_line.decode("utf-8")
            except UnicodeError:
                raise upstream_error("Upstream SSE must be valid UTF-8.") from None
            if first:
                line = line.removeprefix("\ufeff")
                first = False
            yield line
        pending = data[start:]
        skip_lf = data.endswith(b"\r")
    if pending:
        try:
            line = pending.decode("utf-8")
        except UnicodeError:
            raise upstream_error("Upstream SSE ended with invalid UTF-8.") from None
        yield line.removeprefix("\ufeff") if first else line


async def _events(response, max_bytes: int) -> AsyncIterator[Tuple[str, str]]:
    data_lines = []
    event_name = ""
    async for line in _lines(response, max_bytes):
        if line == "":
            if data_lines or event_name:
                yield "\n".join(data_lines), event_name
            data_lines = []
            event_name = ""
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_name = value
        # Other fields (including id/retry/extensions) are ignored by SSE.
    # Preserve tolerance for an otherwise complete last event lacking a blank
    # separator. A malformed partial JSON event still fails explicitly.
    if data_lines or event_name:
        yield "\n".join(data_lines), event_name


async def _chat_text(service, session_id, token, options, state: StreamState) -> AsyncIterator[str]:
    deep, english = CHAT_MODELS[options.model]
    cfg = getattr(service, "cfg", config)
    try:
        async with await service.request_chat_stream(
            session_id, options.prompt, options.files, token,
            is_deep_mode=deep, is_english_mode=english, stream=True,
            temperature=options.temperature, max_tokens=options.max_tokens,
        ) as response:
            async for raw, event_name in _events(response, getattr(cfg, "max_upstream_response_bytes", 8 * 1024 * 1024)):
                frame = _parse_frame(raw, event_name)
                if frame.text:
                    state.has_text = True
                    yield frame.text
                if frame.finish_reason is not None:
                    state.finish_reason = frame.finish_reason
                    state.complete = True
                    if not frame.wait_for_eof:
                        break
            if not state.has_text:
                raise upstream_error("Upstream completed without any assistant text.")
            if not state.complete and getattr(cfg, "require_stream_done", False):
                raise upstream_error("Upstream stream ended without a completion marker.")
    except APIError:
        raise
    except httpx.TimeoutException:
        raise APIError("Upstream chat timed out.", status_code=504, err_type="upstream_error") from None
    except httpx.HTTPError:
        raise upstream_error("Upstream connection failed during chat.") from None


class ClosingStreamingResponse(StreamingResponse):
    """Close the pre-opened stream even if response start/send is cancelled."""
    def __init__(self, content, upstream, **kwargs):
        super().__init__(content, **kwargs)
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette uses cancellation scopes on disconnect. Cleanup needs
            # its own shield to release the HTTP connection in that case.
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    service = request.app.state.service
    try:
        token = get_upstream_token(request, service)
        try:
            payload = await request.json()
        except (ValueError, UnicodeError, RecursionError):
            raise APIError("Invalid JSON payload.", status_code=400, err_type="invalid_request_error") from None
        cfg = getattr(service, "cfg", config)
        options = OpenAIAdapter.validate_chat(payload, cfg.default_model, getattr(cfg, "max_chat_chars", 128000))
        # The supplied history is the sole source of context; no message hash
        # or shared upstream session can mix accounts or conversation branches.
        session_id = await service.create_session(token)
        state = StreamState()
        upstream = _chat_text(service, session_id, token, options, state)
        first = await upstream.__anext__()
    except APIError as error:
        return error.response()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("Chat setup failed (%s).", type(error).__name__)
        return upstream_error("Unable to start the upstream chat.").response()

    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    if not options.stream:
        try:
            parts = [first]
            async for text in upstream:
                parts.append(text)
            return JSONResponse({
                "id": chat_id, "object": "chat.completion", "created": created,
                "model": options.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": state.finish_reason}],
            })
        except APIError as error:
            return error.response()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Chat response failed (%s).", type(error).__name__)
            return upstream_error("Upstream chat failed.").response()
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

    def chunk(delta, finish_reason=None):
        return OpenAIAdapter.format_chunk(chat_id, options.model, delta, finish_reason, created, options.include_usage)

    async def event_generator() -> AsyncIterator[str]:
        try:
            yield chunk({"role": "assistant", "content": ""})
            yield chunk({"content": first})
            async for text in upstream:
                yield chunk({"content": text})
            yield chunk({}, state.finish_reason)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, APIError):
                logger.warning("Chat stream failed (%s).", type(error).__name__)
                error = upstream_error("Upstream chat failed during streaming.")
            yield "data: " + json.dumps(error.as_dict(), ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

    return ClosingStreamingResponse(event_generator(), upstream=upstream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no",
    })
