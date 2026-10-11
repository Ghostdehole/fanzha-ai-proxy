from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from auth import LOCAL_ORIGIN_PATTERN, check_proxy_access
from client import AntiFraudClient
from config import AppConfig, config, logger
from errors import APIError
from routers import audio_router, chat_router, dashboard_router, models_router


class RequestPolicyMiddleware:
    """Authenticate before consuming bodies; cap even chunked uploads."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        cfg = getattr(getattr(scope["app"].state, "service", None), "cfg", scope["app"].state.config)
        try:
            if scope["path"] not in {"/", "/health"}:
                check_proxy_access(request, cfg)
            size = request.headers.get("content-length")
            if size is not None:
                if not size.isdecimal():
                    raise APIError("Invalid Content-Length.", 400, "invalid_request_error")
                if len(size) > 20 or int(size) > cfg.max_request_body_bytes:
                    raise APIError("Request body is too large.", 413, "invalid_request_error")
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > cfg.max_request_body_bytes:
                    raise APIError("Request body is too large.", 413, "invalid_request_error")
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
        except APIError as exc:
            return await exc.response()(scope, receive, send)
        cursor = iter([{"type": "http.request", "body": bytes(body), "more_body": False}])
        del body

        async def replay_receive():
            try:
                return next(cursor)
            except StopIteration:
                return await receive()

        await self.app(scope, replay_receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.config
    cfg.validate_server()
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30)
    timeout = httpx.Timeout(connect=10, read=60, write=10, pool=5)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=cfg.trust_env_proxy, follow_redirects=False) as http_client:
        app.state.service = AntiFraudClient(cfg, http_client)
        logger.info("Proxy listening at http://%s:%s; upstream credentials %s", cfg.host, cfg.port, "configured" if cfg.access_token else "not configured")
        try:
            yield
        finally:
            logger.info("Closing upstream connection pool.")


def create_app(cfg: AppConfig = config) -> FastAPI:
    app = FastAPI(title="Anti-Fraud AI Proxy", version="4.3.0", lifespan=lifespan)
    app.state.config = cfg

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return APIError("Invalid request fields.", 400, "invalid_request_error").response()

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return APIError("Request could not be processed.", exc.status_code, "invalid_request_error", headers=exc.headers).response()

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.error("Unhandled request error (%s)", type(exc).__name__)
        return APIError("Internal proxy error.", 500, "server_error").response()

    app.add_middleware(RequestPolicyMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=list(cfg.cors_origins), allow_origin_regex=LOCAL_ORIGIN_PATTERN,
                       allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-Upstream-Token", "Accept"])
    app.include_router(dashboard_router)
    app.include_router(models_router)
    app.include_router(chat_router)
    app.include_router(audio_router)
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=config.host, port=config.port, access_log=False)
