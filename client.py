import time
import uuid
import base64
import asyncio
from typing import Any, Dict, List
import httpx

from config import AppConfig, clean_token, logger
from utils import split_text_for_tts, concatenate_wav_streams


class SessionCache:
    """会话池缓存，在生命周期内维持会话连续性。"""
    def __init__(self, ttl: int = 86400):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self.ttl = ttl

    async def get_or_create(self, anchor_key: str, client_factory) -> str:
        async with self._lock:
            now = time.time()
            if anchor_key in self._cache:
                entry = self._cache[anchor_key]
                if now - entry["created"] < self.ttl:
                    return entry["session_id"]

            new_session_id = await client_factory()
            self._cache[anchor_key] = {"session_id": new_session_id, "created": now}
            return new_session_id


class AntiFraudClient:
    """反诈智能体官方通信客户端"""
    def __init__(self, cfg: AppConfig, http_client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = http_client
        self.access_token = cfg.access_token
        self.refresh_token = cfg.refresh_token
        self._refresh_lock = asyncio.Lock()
        self.session_cache = SessionCache()

        # 上游 RESTful & SSE 端点
        self.session_url = f"{cfg.base_url}/api/ai/create_session"
        self.chat_url = f"{cfg.base_url}/api/ai/chat?type=0"
        self.refresh_url = f"{cfg.base_url}/api/v1/user/token/refresh"
        self.usage_url = f"{cfg.base_url}/api/v1/conversations/usage"
        self.tts_url = f"{cfg.base_url}/zwuat/api/v1/transcription/tts/volcengine"
        self.asr_url = f"{cfg.base_url}/zwuat/api/v1/transcription/asr/audio"

    def build_headers(self, token: str, is_multipart: bool = False) -> Dict[str, str]:
        """伪造浏览器指纹与 Channel 请求头"""
        pure_token = clean_token(token)
        headers = {
            "Authorization": f"Bearer {pure_token}",
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

    async def refresh_token_if_needed(self) -> bool:
        """并发安全的 Access Token 自动刷新"""
        if not self.refresh_token:
            return False

        async with self._refresh_lock:
            try:
                headers = self.build_headers(self.refresh_token)
                resp = await self.client.post(self.refresh_url, headers=headers, json={}, timeout=15.0)
                data = resp.json()
                if data.get("code") in (200, 10000) and "data" in data:
                    new_at = data["data"].get("access_token")
                    new_rt = data["data"].get("refresh_token")
                    if new_at:
                        self.access_token = clean_token(new_at)
                    if new_rt:
                        self.refresh_token = clean_token(new_rt)
                    logger.info("Access Token 已自动刷新成功。")
                    return True
            except Exception as e:
                logger.error(f"Token 刷新失败: {e}")
        return False

    async def _create_raw_session(self, current_token: str) -> str:
        headers = self.build_headers(current_token)
        try:
            resp = await self.client.post(self.session_url, headers=headers, json={}, timeout=15.0)
            if (resp.status_code == 401 or resp.json().get("code") in (401, "401")) and current_token == self.access_token:
                if await self.refresh_token_if_needed():
                    headers = self.build_headers(self.access_token)
                    resp = await self.client.post(self.session_url, headers=headers, json={}, timeout=15.0)

            data = resp.json()
            res_data = data.get("data")
            if isinstance(res_data, dict):
                return res_data.get("data") or res_data.get("session_id") or res_data.get("conversation_id") or ""
            elif isinstance(res_data, str):
                return res_data
        except Exception as e:
            logger.warning(f"创建原始会话失败，降级使用独立 UUID: {e}")
        return uuid.uuid4().hex

    async def get_session_id(self, anchor_key: str, current_token: str) -> str:
        return await self.session_cache.get_or_create(
            anchor_key, lambda: self._create_raw_session(current_token)
        )

    async def request_chat_stream(
        self,
        session_id: str,
        prompt: str,
        files: List[Any],
        current_token: str,
        is_deep_mode: bool = False,
        is_english_mode: bool = False,
        stream: bool = True
    ):
        """向上游发送标准/深度/英文对话流"""
        headers = self.build_headers(current_token)

        # 映射官方 4 种问答引擎模式
        answer_mode = "normal"
        params = {}

        if is_english_mode and is_deep_mode:
            answer_mode = "english_detailed"
            params["special_type"] = "xxhd"
        elif is_english_mode:
            answer_mode = "english"
        elif is_deep_mode:
            answer_mode = "detailed"
            params["special_type"] = "xxhd"

        payload = {
            "conversation_id": session_id,
            "session_id": session_id,
            "text": prompt,
            "query": prompt,
            "stream": stream,
            "version": 2,
            "max_tokens": 2048,
            "using_context": True,
            "temperature": "0.1",
            "files": files,
            "model_name": "",
            "answer_mode": answer_mode,
        }

        if params:
            payload["params"] = params

        return self.client.stream("POST", self.chat_url, headers=headers, json=payload)

    async def synthesize_speech(self, text: str, current_token: str, voice_type: str = "mandarin") -> bytes:
        """TTS 火山引擎语音合成"""
        headers = self.build_headers(current_token)
        chunks = split_text_for_tts(text, max_chars=150)
        wav_parts: List[bytes] = []

        for chunk in chunks:
            if not chunk.strip():
                continue
            payload = {
                "text": chunk,
                "voice_type": voice_type or "mandarin",
            }
            resp = await self.client.post(self.tts_url, headers=headers, json=payload, timeout=30.0)
            res_json = resp.json()
            b64_audio = res_json.get("data", {}).get("audio", "")
            if b64_audio:
                wav_parts.append(base64.b64decode(b64_audio))

        return concatenate_wav_streams(wav_parts)

    async def transcribe_audio(self, file_content: bytes, filename: str, content_type: str, current_token: str) -> str:
        """ASR 语音转文字识别"""
        headers = self.build_headers(current_token, is_multipart=True)
        files = {"file": (filename, file_content, content_type)}
        data = {"fileType": "audio"}
        resp = await self.client.post(self.asr_url, headers=headers, data=data, files=files, timeout=60.0)
        res_json = resp.json()

        if res_json.get("code") in (200, 10000) and "data" in res_json:
            transcribed_text = res_json.get("data", "")
            return transcribed_text if isinstance(transcribed_text, str) else str(transcribed_text)
        raise Exception(f"ASR 识别失败: {res_json.get('message', '未知错误')}")

    async def fetch_usage(self, current_token: str) -> Dict[str, Any]:
        """获取账户用量与封禁状态"""
        headers = self.build_headers(current_token)
        resp = await self.client.get(self.usage_url, headers=headers, timeout=10.0)
        return resp.json()