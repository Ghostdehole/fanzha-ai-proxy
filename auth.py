"""Keep the proxy API key separate from optional upstream credentials."""
import hmac
import re
from typing import Optional

from fastapi import Request

from config import AppConfig, clean_token, is_loopback
from errors import APIError

LOCAL_ORIGIN_PATTERN = r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?"


def origin_allowed(origin: str, cfg: AppConfig) -> bool:
    return bool(re.fullmatch(LOCAL_ORIGIN_PATTERN, origin)) or origin in cfg.cors_origins


def authorization_token(request: Request) -> str:
    if len(request.headers.getlist("authorization")) > 1:
        raise APIError("Multiple Authorization headers are not supported.", 400, "invalid_request_error")
    return clean_token(request.headers.get("authorization", ""))


def check_proxy_access(request: Request, cfg: AppConfig) -> None:
    origin = request.headers.get("origin")
    if origin and not origin_allowed(origin, cfg):
        raise APIError("This browser origin is not allowed.", 403, "permission_error")
    if cfg.proxy_api_key:
        provided = authorization_token(request)
        if not hmac.compare_digest(provided.encode(), cfg.proxy_api_key.encode()):
            raise APIError("A valid proxy API key is required.", 401, "authentication_error", headers={"WWW-Authenticate": "Bearer"})
    else:
        # Also protect alternative uvicorn launches and DNS rebinding, not just HOST.
        if not request.client or not is_loopback(request.client.host) or not is_loopback(request.url.hostname or ""):
            raise APIError("Remote access requires PROXY_API_KEY.", 403, "permission_error")


def get_upstream_token(request: Request, service) -> Optional[str]:
    check_proxy_access(request, service.cfg)
    if len(request.headers.getlist("x-upstream-token")) > 1:
        raise APIError("Multiple X-Upstream-Token headers are not supported.", 400, "invalid_request_error")
    override = clean_token(request.headers.get("x-upstream-token", ""))
    if override:
        if any(ord(c) < 32 or ord(c) > 126 for c in override):
            raise APIError("Invalid upstream credential format.", 400, "invalid_request_error")
        return override
    token = authorization_token(request)
    # With proxy authentication enabled, Authorization must never reach upstream.
    if service.cfg.proxy_api_key:
        return None
    if not token or token.startswith("sk-"):
        return None
    if any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise APIError("Invalid upstream credential format.", 400, "invalid_request_error")
    return token
