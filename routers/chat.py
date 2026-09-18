import time
import uuid
import asyncio
from typing import List, AsyncIterator
from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse, JSONResponse

from config import config, logger
from client import AntiFraudClient
from adapter import OpenAIAdapter

router = APIRouter(tags=["Chat"])


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 标准聊天对话补全接口"""
    service: AntiFraudClient = request.app.state.service
    token = OpenAIAdapter.get_auth_token(request, service.access_token)

    if not token:
        return OpenAIAdapter.error_response(
            message="Missing access token. Please set FANZHA_ACCESS_TOKEN in .env.",
            status_code=status.HTTP_401_UNAUTHORIZED,
            err_type="authentication_error",
        )

    try:
        payload = await request.json()
    except Exception:
        return OpenAIAdapter.error_response("Invalid JSON payload.", status_code=status.HTTP_400_BAD_REQUEST)

    messages = payload.get("messages", [])
    if not messages:
        return OpenAIAdapter.error_response("Missing required messages array.", status_code=status.HTTP_400_BAD_REQUEST)

    stream = payload.get("stream", False)
    model = payload.get("model", config.default_model)
    user_prompt, files = OpenAIAdapter.extract_prompt_and_files(messages)

    # 模式判断：是否为深度研判或纯英文模式
    model_lower = model.lower()
    is_deep_mode = "deep" in model_lower
    is_english_mode = "en" in model_lower

    anchor_key = OpenAIAdapter.compute_conversation_anchor(messages)
    session_id = await service.get_session_id(anchor_key, token)

    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    mode_desc = f"{'deep(xxhd)' if is_deep_mode else 'standard'}{'-en' if is_english_mode else ''}"
    logger.info(f"模式: {mode_desc} | 锚点: {anchor_key[:8]} | 会话: {session_id[:8]} | 提问: {user_prompt[:30]}")

    if stream:
        async def event_generator() -> AsyncIterator[str]:
            yielded = False
            try:
                yield OpenAIAdapter.format_chunk(chat_id, model, {"role": "assistant", "content": ""})

                async with await service.request_chat_stream(
                    session_id, user_prompt, files, token,
                    is_deep_mode=is_deep_mode,
                    is_english_mode=is_english_mode,
                    stream=True
                ) as resp:
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

                if not yielded:
                    fallback_msg = "Notice: No valid response received from upstream. Please verify if your inquiry falls within the anti-fraud scope."
                    yield OpenAIAdapter.format_chunk(chat_id, model, {"content": fallback_msg})

                yield OpenAIAdapter.format_chunk(chat_id, model, {}, finish_reason="stop")
                yield "data: [DONE]\n\n"

            except asyncio.CancelledError:
                logger.info(f"客户端断开连接 [{chat_id}]")
            except Exception as e:
                logger.error(f"流式响应异常: {e}")

        sse_headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_generator(), headers=sse_headers)

    else:
        full_reply: List[str] = []
        try:
            async with await service.request_chat_stream(
                session_id, user_prompt, files, token,
                is_deep_mode=is_deep_mode,
                is_english_mode=is_english_mode,
                stream=True
            ) as resp:
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
            logger.error(f"非流式请求失败: {e}")
            return OpenAIAdapter.error_response(f"Upstream communication failed: {str(e)}", status_code=status.HTTP_502_BAD_GATEWAY)

        final_content = "".join(full_reply) or "Notice: No response from assistant."
        return JSONResponse(
            content={
                "id": chat_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": final_content}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": len(user_prompt),
                    "completion_tokens": len(final_content),
                    "total_tokens": len(user_prompt) + len(final_content),
                },
            }
        )