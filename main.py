import os
import time
import json
import uuid
import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional
from dataclasses import dataclass
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fanzha-proxy")

def _clean_token(token: Optional[str]) -> str:
    if not token:
        return ""
    token = str(token).strip()
    return token[7:].strip() if token.startswith("Bearer ") else token


@dataclass(frozen=True)
class AppConfig:
    base_url: str = os.getenv("FANZHA_BASE_URL", "https://xzfzznt.gaj.sh.gov.cn")
    access_token: str = _clean_token(os.getenv("FANZHA_ACCESS_TOKEN", ""))
    refresh_token: str = _clean_token(os.getenv("FANZHA_REFRESH_TOKEN", ""))
    default_model: str = os.getenv("DEFAULT_MODEL", "国家反诈AI")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8088"))
    # 是否信任本地操作系统代理（默认 False 强制直连，防止国内政务网被 VPN 劫持）
    trust_env_proxy: bool = os.getenv("TRUST_ENV_PROXY", "false").lower() == "true"


config = AppConfig()

class AntiFraudClient:
    def __init__(self, cfg: AppConfig, http_client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = http_client
        self.access_token = cfg.access_token
        self.refresh_token = cfg.refresh_token
        self._refresh_lock = asyncio.Lock()

        # 预设 API 端点
        self.session_url = f"{cfg.base_url}/api/ai/create_session"
        self.chat_url = f"{cfg.base_url}/api/ai/chat?type=0"
        self.refresh_url = f"{cfg.base_url}/api/v1/user/token/refresh"

    def build_headers(self, token: str) -> Dict[str, str]:
        """构造防拦截的真实浏览器标头"""
        pure_token = _clean_token(token)
        return {
            "Authorization": f"Bearer {pure_token}",
            "Content-Type": "application/json",
            "Origin": "https://xzfzznt.gaj.sh.gov.cn",
            "Referer": "https://xzfzznt.gaj.sh.gov.cn/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/event-stream, application/json, */*",
        }

    async def refresh_token_if_needed(self) -> bool:
        """并发安全的 Token 长效自动续期"""
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
                        self.access_token = _clean_token(new_at)
                    if new_rt:
                        self.refresh_token = _clean_token(new_rt)
                    logger.info("Access Token 自动续期成功！")
                    return True
            except Exception as e:
                logger.error(f"令牌续期发生异常: {e}")
        return False

    async def create_session(self, current_token: str) -> str:
        """向上游申请官方会话 Session ID，支持 401 自动续期与自适应解析"""
        headers = self.build_headers(current_token)
        try:
            resp = await self.client.post(self.session_url, headers=headers, json={}, timeout=15.0)
            # 遇到 401 触发刷新机制
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
            logger.warning(f"会话创建异常，使用降级 UUID 兜底: {e}")
        return uuid.uuid4().hex

    async def request_chat_stream(self, session_id: str, prompt: str, current_token: str):
        """向上游发送聊天流请求（严格匹配 text 字段要求）"""
        headers = self.build_headers(current_token)
        payload = {
            "conversation_id": session_id,
            "session_id": session_id,
            "text": prompt,
            "query": prompt,  # 双字段兼容
        }
        return self.client.stream("POST", self.chat_url, headers=headers, json=payload)

class OpenAIAdapter:
    @staticmethod
    def extract_prompt(messages: List[Dict[str, Any]]) -> str:
        """从 OpenAI messages 列表中精准提取最新的用户提问，兼容多模态字典"""
        target = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                target = msg.get("content")
                break
        if target is None and messages:
            target = messages[-1].get("content")

        if isinstance(target, str):
            return target
        if isinstance(target, list):
            parts = []
            for item in target:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text") or item.get("content") or "")
            return "\n".join(filter(None, parts))
        return str(target or "")

    @staticmethod
    def parse_sse_line(raw_json: str) -> Optional[str]:
        """解析上游推流中多层嵌套的答案字段"""
        try:
            event = json.loads(raw_json)
            if not isinstance(event, dict):
                return None
            data_node = event.get("data")
            if isinstance(data_node, dict):
                return data_node.get("answer") or data_node.get("content") or data_node.get("text")
            elif isinstance(data_node, str):
                return data_node
            return event.get("answer") or event.get("content") or event.get("text")
        except Exception:
            return None

    @staticmethod
    def format_chunk(chat_id: str, model: str, delta_dict: dict, finish_reason: Optional[str] = None) -> str:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta_dict, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    @staticmethod
    def error_response(message: str, status_code: int = 400, err_type: str = "invalid_request_error"):
        """严格返回 OpenAI 官方标准的 Error Payload"""
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": message,
                    "type": err_type,
                    "param": None,
                    "code": status_code,
                }
            },
        )

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建立具有限制保护的高性能全局连接池
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)

    http_client = httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=config.trust_env_proxy)
    app.state.service = AntiFraudClient(config, http_client)

    token_display = f"{config.access_token[:10]}...{config.access_token[-6:]}" if config.access_token else "未配置"
    logger.info(f"服务已就绪 -> 监听: http://{config.host}:{config.port} | 默认模型: {config.default_model}")
    logger.info(f"环境变量 Token 装载状态: {token_display}")

    yield

    await http_client.aclose()
    logger.info("HTTP 连接池已安全释放")


app = FastAPI(
    title="National Anti-Fraud AI - OpenAI Compatible Proxy",
    version="2.1.0",
    lifespan=lifespan,
)

# 注册 CORS 跨域支持 Web 端直接请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "fanzha-ai-proxy", "timestamp": int(time.time())}


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    now = int(time.time())
    models = ["国家反诈AI", "fanzha-ai", "gpt-4o-mini"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "fanzha"} for m in models],
    }


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    service: AntiFraudClient = request.app.state.service

    # 1. 鉴权令牌协商（动态传参 > 环境变量默认值）
    auth_header = request.headers.get("Authorization", "")
    req_token = _clean_token(auth_header)
    # 忽略常用占位符 Key
    if req_token in ("sk-no-key-needed", "") or req_token.startswith("sk-"):
        token = service.access_token
    else:
        token = req_token

    if not token:
        return OpenAIAdapter.error_response(
            message="未提供有效的访问令牌。请在请求头设置 Authorization: Bearer <token> 或在 .env 中配置 FANZHA_ACCESS_TOKEN。",
            status_code=status.HTTP_401_UNAUTHORIZED,
            err_type="authentication_error",
        )

    # 2. 请求体解析与校验
    try:
        payload = await request.json()
    except Exception:
        return OpenAIAdapter.error_response("无效的 JSON 请求体。", status_code=status.HTTP_400_BAD_REQUEST)

    messages = payload.get("messages", [])
    if not messages:
        return OpenAIAdapter.error_response("缺少必要的 messages 列表。", status_code=status.HTTP_400_BAD_REQUEST)

    stream = payload.get("stream", False)
    model = payload.get("model", config.default_model)
    user_prompt = OpenAIAdapter.extract_prompt(messages)

    # 3. 创建上游会话
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    session_id = await service.create_session(token)
    logger.info(f"会话创建完毕 [{session_id[:8]}...] | 接收问题: {user_prompt[:30]}...")

    # 4. 流式传输处理 (SSE)
    if stream:
        async def event_generator() -> AsyncIterator[str]:
            yielded = False
            try:
                # 首包下发角色标记
                yield OpenAIAdapter.format_chunk(chat_id, model, {"role": "assistant", "content": ""})

                async with await service.request_chat_stream(session_id, user_prompt, token) as resp:
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if raw_data == "[DONE]":
                            continue

                        text_chunk = OpenAIAdapter.parse_sse_line(raw_data)
                        if text_chunk:
                            yielded = True
                            yield OpenAIAdapter.format_chunk(chat_id, model, {"content": text_chunk})

                # 无响应时的兜底提示
                if not yielded:
                    fallback_msg = "⚠️【系统提示】未获取到有效回答，请检查配置是否正确。"
                    yield OpenAIAdapter.format_chunk(chat_id, model, {"content": fallback_msg})

                # 规范结束符
                yield OpenAIAdapter.format_chunk(chat_id, model, {}, finish_reason="stop")
                yield "data: [DONE]\n\n"

            except asyncio.CancelledError:
                logger.info(f"客户端主动中止了流传输 [{chat_id}]")
            except Exception as e:
                logger.error(f"流式传输过程中发生未捕获异常: {e}")

        # 携带标准无缓冲响应头，确保各级反代下保持流式打字机效果
        sse_headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_generator(), headers=sse_headers)

    # 5. 非流式响应处理 (Non-Streaming JSON)
    else:
        full_reply: List[str] = []
        try:
            async with await service.request_chat_stream(session_id, user_prompt, token) as resp:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw_data = line[5:].strip()
                    if raw_data == "[DONE]":
                        continue
                    text_chunk = OpenAIAdapter.parse_sse_line(raw_data)
                    if text_chunk:
                        full_reply.append(text_chunk)
        except Exception as e:
            logger.error(f"非流式请求异常: {e}")
            return OpenAIAdapter.error_response(f"上游通信失败: {str(e)}", status_code=status.HTTP_502_BAD_GATEWAY)

        final_content = "".join(full_reply) or "⚠️【系统提示】未获取到有效回答，请检查配置是否正确。"
        return JSONResponse(
            content={
                "id": chat_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": final_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(user_prompt),
                    "completion_tokens": len(final_content),
                    "total_tokens": len(user_prompt) + len(final_content),
                },
            }
        )

if __name__ == "__main__":
    uvicorn.run(app, host=config.host, port=config.port, access_log=False)