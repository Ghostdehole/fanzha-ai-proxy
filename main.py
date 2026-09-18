import httpx
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import config, logger
from client import AntiFraudClient
from routers import models_router, chat_router, audio_router, dashboard_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """全局异步生命周期：连接池管理"""
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)

    http_client = httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=config.trust_env_proxy)
    app.state.service = AntiFraudClient(config, http_client)

    token_display = f"{config.access_token[:10]}...{config.access_token[-6:]}" if config.access_token else "none"
    logger.info(f"Service running at http://{config.host}:{config.port} | Token: {token_display}")
    logger.info("Loaded models: fanzha-ai, fanzha-ai-deep, fanzha-ai-en, fanzha-ai-en-deep, tts-1, whisper-1")

    yield

    await http_client.aclose()
    logger.info("HTTP connection pool closed.")


app = FastAPI(title="Anti-Fraud AI Proxy", version="4.2.0", lifespan=lifespan)

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载业务路由
app.include_router(dashboard_router)
app.include_router(models_router)
app.include_router(chat_router)
app.include_router(audio_router)


if __name__ == "__main__":
    uvicorn.run(app, host=config.host, port=config.port, access_log=False)