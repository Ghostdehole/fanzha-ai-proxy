import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Dict, List, Optional

import anyio
import httpx

from config import AppConfig, clean_token, logger, validate_chat_key
from errors import APIError
from utils import split_text_for_tts, concatenate_wav_streams


class _ShieldedStream(httpx.AsyncByteStream):
    """Also protect HTTPX's automatic close when iteration reaches EOF."""

    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            await self.stream.aclose()


class AntiFraudClient:
    """Upstream client. None selects server credentials; strings are external tokens."""

    def __init__(self, cfg: AppConfig, http_client: httpx.AsyncClient):
        self.cfg = cfg
        self._chat_key = validate_chat_key(getattr(cfg, "chat_key", ""))
        self.client = http_client
        self.access_token = clean_token(cfg.access_token)
        self.refresh_token = clean_token(cfg.refresh_token)
        self._refresh_lock = asyncio.Lock()
        self._refresh_failure = None
        self.max_response_bytes = getattr(cfg, "max_upstream_response_bytes", 8 * 1024 * 1024)
        self._state_file = getattr(cfg, "token_state_file", "")
        binding = "\0".join((cfg.base_url, self.access_token, self.refresh_token))
        self._state_binding = hashlib.sha256(binding.encode("utf-8")).hexdigest()
        self._load_token_state()

        base_url = cfg.base_url.rstrip("/")
        self.session_url = f"{base_url}/api/ai/create_session"
        self.chat_url = f"{base_url}/api/ai/chat?type=0"
        self.refresh_url = f"{base_url}/api/v1/user/token/refresh"
        self.usage_url = f"{base_url}/api/v1/conversations/usage"
        self.tts_url = f"{base_url}/zwuat/api/v1/transcription/tts/volcengine"
        self.asr_url = f"{base_url}/zwuat/api/v1/transcription/asr/audio"

    def _load_token_state(self) -> None:
        if not self._state_file:
            return
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._state_file, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                    raise ValueError("Invalid token state file")
                if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                    raise ValueError("Token state permissions are too broad")
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("binding") != self._state_binding:
                logger.warning("忽略与当前服务器凭证不匹配的令牌状态文件。")
                return
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            if not isinstance(access_token, str) or not clean_token(access_token):
                raise ValueError("Missing access token")
            if not isinstance(refresh_token, str):
                raise ValueError("Invalid refresh token")
            self.access_token = clean_token(access_token)
            self.refresh_token = clean_token(refresh_token)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning("无法加载令牌状态文件，使用配置中的凭证。")

    def _save_token_state(self) -> None:
        if not self._state_file:
            return
        temp_path = None
        try:
            path = Path(self._state_file)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.chmod(temp_path, 0o600)
                json.dump({"binding": self._state_binding,
                           "access_token": self.access_token,
                           "refresh_token": self.refresh_token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            temp_path = None
        except OSError:
            logger.warning("令牌刷新成功，但状态文件保存失败；重启前需更新配置凭证。")
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def build_headers(self, token: str, is_multipart: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {clean_token(token)}",
            "Origin": "https://xzfzznt.gaj.sh.gov.cn",
            "Referer": "https://xzfzznt.gaj.sh.gov.cn/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/event-stream, application/json, */*",
            "channel": "web",
        }
        if not is_multipart:
            headers["Content-Type"] = "application/json"
        return headers

    def _resolve_token(self, current_token: Optional[str]) -> str:
        token = self.access_token if current_token is None else clean_token(current_token)
        if not token:
            raise APIError("Upstream access token is not configured.", 401, "authentication_error")
        return token

    @staticmethod
    def _status_error(status_code: int, response: Optional[httpx.Response] = None) -> APIError:
        headers = None
        if status_code == 429 and response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and len(retry_after) <= 128:
                headers = {"Retry-After": retry_after}
        if status_code == 401:
            return APIError("Upstream authentication failed.", 401, "authentication_error")
        if status_code == 403:
            return APIError("Upstream access was denied.", 403, "permission_error")
        if status_code == 429:
            return APIError("Upstream rate limit exceeded.", 429, "rate_limit_error", headers=headers)
        return APIError("Upstream request failed.", 502, "upstream_error")

    def _check_http_status(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise self._status_error(response.status_code, response)

    def _check_business_code(self, code: Any, response: httpx.Response) -> None:
        if code is None or (type(code) in (int, str) and str(code) in ("0", "200", "10000")):
            return
        # Only exact status values identify authentication/rate-limit errors;
        # coercing a float or boolean can cause an unrelated failure to refresh.
        status_code = int(code) if type(code) in (int, str) and str(code) in ("401", "403", "429") else 502
        raise self._status_error(status_code, response)

    def _check_business_status(self, data: Dict[str, Any], response: httpx.Response) -> None:
        self._check_business_code(data.get("code"), response)
        self._check_business_code(data.get("resultCode"), response)
        # The supplied official response has this explicit two-layer envelope.
        # Do not recursively interpret arbitrary result payloads as statuses.
        official_wrapper = (
            type(data.get("result")) is bool
            and type(data.get("succeed")) is bool
            and "resultCode" in data
        )
        if official_wrapper and isinstance(data.get("data"), dict):
            self._check_business_code(data["data"].get("code"), response)
        if any(data.get(flag) is False for flag in ("success", "result", "succeed")) or data.get("error"):
            raise APIError("Upstream returned an error response.", 502, "upstream_error")

    async def _read_json(self, response: httpx.Response) -> Dict[str, Any]:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self.max_response_bytes:
                raise APIError("Upstream response exceeded the size limit.", 502, "upstream_error")
            body.extend(chunk)
        try:
            data = json.loads(body, parse_constant=self._reject_json_constant)
            self._validate_json_numbers(data)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError("Upstream returned invalid JSON.", 502, "upstream_error") from None
        if not isinstance(data, dict):
            raise APIError("Upstream returned an invalid response object.", 502, "upstream_error")
        self._check_business_status(data, response)
        return data

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError("Non-finite JSON constant")

    @classmethod
    def _validate_json_numbers(cls, value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        if isinstance(value, dict):
            for item in value.values():
                cls._validate_json_numbers(item)
        elif isinstance(value, list):
            for item in value:
                cls._validate_json_numbers(item)

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        # AnyIO cancellation scopes repeatedly cancel awaits until scope exit.
        with anyio.CancelScope(shield=True):
            await response.aclose()

    async def _send_stream(self, request: httpx.Request) -> httpx.Response:
        response = await self.client.send(request, stream=True)
        response.stream = _ShieldedStream(response.stream)
        return response

    async def _json_once(self, method: str, url: str, token: str, **kwargs) -> Dict[str, Any]:
        multipart = "files" in kwargs
        response = None
        try:
            request = self.client.build_request(method, url, headers=self.build_headers(token, multipart), **kwargs)
            response = await self._send_stream(request)
            self._check_http_status(response)
            return await self._read_json(response)
        except httpx.TimeoutException:
            raise APIError("Upstream request timed out.", 504, "upstream_timeout") from None
        except httpx.RequestError:
            raise APIError("Could not communicate with upstream.", 502, "upstream_error") from None
        finally:
            if response is not None:
                await self._close_response(response)

    async def refresh_token_if_needed(self, failed_token: Optional[str] = None) -> bool:
        """Refresh once per stale access token, including concurrent requests."""
        async with self._refresh_lock:
            if failed_token is not None and failed_token != self.access_token:
                return True
            if not self.refresh_token:
                return False
            credential_version = (self.access_token, self.refresh_token)
            if self._refresh_failure is not None:
                version, expires_at, error_fields = self._refresh_failure
                if version == credential_version and time.monotonic() < expires_at:
                    raise APIError(**error_fields)
                self._refresh_failure = None
            try:
                data = await self._json_once("POST", self.refresh_url, self.refresh_token, json={}, timeout=15.0)
                tokens = data.get("data")
                if not isinstance(tokens, dict):
                    raise APIError("Upstream token refresh returned invalid credentials.", 502, "upstream_error")
                new_at = tokens.get("access_token") or tokens.get("accessToken")
                new_rt = tokens.get("refresh_token") or tokens.get("refreshToken")
                if not isinstance(new_at, str) or not clean_token(new_at):
                    raise APIError("Upstream token refresh did not provide an access token.", 502, "upstream_error")
                if new_rt is not None and (not isinstance(new_rt, str) or not clean_token(new_rt)):
                    raise APIError("Upstream token refresh returned an invalid refresh token.", 502, "upstream_error")
            except APIError as error:
                self._refresh_failure = (credential_version, time.monotonic() + 5.0, {
                    "message": error.message, "status_code": error.status_code,
                    "err_type": error.err_type, "param": error.param,
                    "headers": dict(error.headers or {}),
                })
                raise
            self._refresh_failure = None
            self.access_token = clean_token(new_at)
            if new_rt is not None:
                self.refresh_token = clean_token(new_rt)
            self._save_token_state()
            logger.info("服务器访问令牌已刷新。")
            return True

    async def _request_json(self, method: str, url: str, current_token: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        for attempt in range(2):
            token = self._resolve_token(current_token)
            try:
                return await self._json_once(method, url, token, **kwargs)
            except APIError as error:
                if error.status_code != 401 or current_token is not None or attempt:
                    raise
                if not await self.refresh_token_if_needed(token):
                    raise
        raise APIError("Upstream authentication failed.", 401, "authentication_error")

    async def create_session(self, current_token: Optional[str] = None) -> str:
        data = await self._request_json("POST", self.session_url, current_token, json={}, timeout=15.0)
        session = data.get("data")
        if isinstance(session, dict):
            session = session.get("data") or session.get("session_id") or session.get("conversation_id")
        if not isinstance(session, str) or not session.strip():
            raise APIError("Upstream did not return a valid session ID.", 502, "upstream_error")
        return session.strip()

    async def _create_raw_session(self, current_token: Optional[str] = None) -> str:
        return await self.create_session(current_token)

    async def get_session_id(self, anchor_key: str, current_token: Optional[str] = None) -> str:
        """Compatibility method: each request gets an isolated upstream session."""
        return await self.create_session(current_token)

    @asynccontextmanager
    async def _chat_response(self, payload: Dict[str, Any], current_token: Optional[str]):
        response = None
        for attempt in range(2):
            token = self._resolve_token(current_token)
            try:
                headers = self.build_headers(token)
                if self._chat_key:
                    headers["X-Chat-Key"] = self._chat_key
                request = self.client.build_request("POST", self.chat_url, headers=headers, json=payload)
                response = await self._send_stream(request)
                self._check_http_status(response)
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    await self._read_json(response)
                    raise APIError("Upstream did not return an event stream.", 502, "upstream_error")
                break
            except APIError as error:
                if response is not None:
                    await self._close_response(response)
                    response = None
                if error.status_code != 401 or current_token is not None or attempt:
                    raise
                if not await self.refresh_token_if_needed(token):
                    raise
            except httpx.TimeoutException:
                if response is not None:
                    await self._close_response(response)
                raise APIError("Upstream request timed out.", 504, "upstream_timeout") from None
            except httpx.RequestError:
                if response is not None:
                    await self._close_response(response)
                raise APIError("Could not communicate with upstream.", 502, "upstream_error") from None
            except BaseException:
                if response is not None:
                    await self._close_response(response)
                raise
        try:
            yield response
        except httpx.TimeoutException:
            raise APIError("Upstream response timed out.", 504, "upstream_timeout") from None
        except httpx.RequestError:
            raise APIError("Upstream stream was interrupted.", 502, "upstream_error") from None
        finally:
            if response is not None:
                await self._close_response(response)

    async def request_chat_stream(
        self, session_id: str, prompt: str, files: List[Any],
        current_token: Optional[str] = None, is_deep_mode: bool = False,
        is_english_mode: bool = False, stream: bool = True, *,
        temperature: float = 0.1, max_tokens: int = 2048,
    ):
        answer_mode = "normal"
        if is_english_mode and is_deep_mode:
            answer_mode = "english_detailed"
        elif is_english_mode:
            answer_mode = "english"
        elif is_deep_mode:
            answer_mode = "detailed"
        payload = {
            "conversation_id": session_id, "session_id": session_id,
            "text": prompt, "query": prompt, "stream": stream, "version": 2,
            "max_tokens": max_tokens, "using_context": True,
            "temperature": str(temperature), "files": files, "model_name": "",
            "answer_mode": answer_mode,
        }
        if is_deep_mode:
            payload["params"] = {"special_type": "xxhd"}
        return self._chat_response(payload, current_token)

    async def synthesize_speech(self, text: str, current_token: Optional[str] = None, voice_type: str = "mandarin") -> bytes:
        if not text.strip():
            raise APIError("Speech input must not be empty.", 400, "invalid_request_error", param="input")
        if len(text) > getattr(self.cfg, "max_tts_chars", 20000):
            raise APIError("Speech input exceeds the length limit.", 400, "invalid_request_error", param="input")
        wav_parts: List[bytes] = []
        total_bytes = 0
        for chunk in split_text_for_tts(text, max_chars=150):
            if not chunk.strip():
                continue
            result = await self._request_json(
                "POST", self.tts_url, current_token,
                json={"text": chunk, "voice_type": voice_type or "mandarin"}, timeout=30.0,
            )
            data = result.get("data")
            audio = data.get("audio") if isinstance(data, dict) else None
            if not isinstance(audio, str) or not audio:
                raise APIError("Upstream did not return an audio chunk.", 502, "upstream_error")
            try:
                decoded = base64.b64decode(audio, validate=True)
            except (binascii.Error, ValueError):
                raise APIError("Upstream returned invalid audio encoding.", 502, "upstream_error") from None
            total_bytes += len(decoded)
            if not decoded or total_bytes > self.max_response_bytes:
                raise APIError("Upstream audio is empty or exceeds the size limit.", 502, "upstream_error")
            wav_parts.append(decoded)
        return concatenate_wav_streams(wav_parts)

    async def transcribe_audio(self, file_content: bytes, filename: str, content_type: str, current_token: Optional[str] = None) -> str:
        result = await self._request_json(
            "POST", self.asr_url, current_token,
            data={"fileType": "audio"}, files={"file": (filename, file_content, content_type)}, timeout=60.0,
        )
        text = result.get("data")
        if not isinstance(text, str):
            raise APIError("Upstream returned an invalid transcription.", 502, "upstream_error")
        return text

    async def fetch_usage(self, current_token: Optional[str] = None) -> Dict[str, Any]:
        return await self._request_json("GET", self.usage_url, current_token, timeout=10.0)
