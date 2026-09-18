import time
from fastapi import APIRouter

router = APIRouter(tags=["Models"])


@router.get("/v1/models")
@router.get("/models")
async def list_models():
    """返回所有支持的模型列表"""
    now = int(time.time())
    models = [
        "fanzha-ai",            # 中文标准问答
        "fanzha-ai-deep",       # 中文深度研判 (xxhd)
        "fanzha-ai-en",         # 国际版英文问答
        "fanzha-ai-en-deep",    # 国际版英文深度研判
        "tts-1",                # 语音合成
        "whisper-1",            # 语音识别
        "gpt-4o-mini",          # 别名兼容
    ]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "fanzha"} for m in models],
    }