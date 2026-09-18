import time
import json
from pathlib import Path
from typing import Any, Dict
from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from config import config, logger
from client import AntiFraudClient
from adapter import OpenAIAdapter

router = APIRouter(tags=["Dashboard"])
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


def render_dashboard_html(raw_json: Dict[str, Any], token: str) -> str:
    """读取 templates/dashboard.html 并填充动态数据"""
    data = raw_json.get("data") or {}
    remaining = data.get("remaining", 0)
    used = data.get("used", 0)
    limit = data.get("limit", 20)
    role = data.get("role", "UNKNOWN")
    exceeded = data.get("exceeded", False)
    banned = data.get("banned", False)
    ban_ttl = data.get("ban_ttl", 0)

    percentage = int((used / limit) * 100) if limit > 0 else 0
    percentage = min(100, max(0, percentage))
    token_preview = f"{token[:8]}...{token[-8:]}" if len(token) > 16 else (token or "None")
    pretty_json = json.dumps(raw_json, indent=2, ensure_ascii=False)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{{remaining}}": str(remaining),
        "{{used}}": str(used),
        "{{limit}}": str(limit),
        "{{percentage}}": str(percentage),
        "{{role}}": str(role),
        "{{exceeded}}": str(exceeded).lower(),
        "{{banned}}": str(banned).lower(),
        "{{ban_ttl}}": str(ban_ttl),
        "{{token_preview}}": token_preview,
        "{{pretty_json}}": pretty_json,
        "{{port}}": str(config.port),
        "{{banned_bool}}": "true" if banned else "false",
        "{{exceeded_bool}}": "true" if exceeded else "false",
    }
    for k, v in replacements.items():
        template = template.replace(k, v)
    return template


@router.get("/")
@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "fanzha-ai-proxy", "timestamp": int(time.time())}


@router.get("/usage")
@router.get("/v1/dashboard/billing/usage")
async def check_usage(request: Request):
    """查询配额，根据请求头决定返回 UI 看板或 JSON"""
    service: AntiFraudClient = request.app.state.service
    token = OpenAIAdapter.get_auth_token(request, service.access_token)

    if not token:
        err_msg = "Missing access token. Please configure FANZHA_ACCESS_TOKEN in .env."
        if "text/html" in request.headers.get("accept", "").lower():
            return HTMLResponse(content=f"<h3>Authentication Error:</h3><p>{err_msg}</p>", status_code=401)
        return OpenAIAdapter.error_response(err_msg, status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        raw_res = await service.fetch_usage(token)
    except Exception as e:
        logger.error(f"配额查询失败: {e}")
        raw_res = {"code": 500, "message": str(e), "data": {}}

    accept_header = request.headers.get("accept", "").lower()
    if "text/html" in accept_header or "*/*" in accept_header:
        html_content = render_dashboard_html(raw_res, token)
        return HTMLResponse(content=html_content)

    return JSONResponse(content=raw_res)