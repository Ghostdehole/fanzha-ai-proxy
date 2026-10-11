import time
from fastapi import APIRouter
from adapter import CHAT_MODELS

router = APIRouter(tags=["Models"])


@router.get("/v1/models")
@router.get("/models")
async def list_models():
    """返回所有支持的模型列表"""
    now = int(time.time())
    models = [*CHAT_MODELS, "tts-1", "tts-1-hd", "whisper-1"]
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "fanzha"} for m in models],
    }
