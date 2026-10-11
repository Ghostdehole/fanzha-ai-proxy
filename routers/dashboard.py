"""Quota dashboard with explicit unavailable states and safe HTML rendering."""

import html
import json
import math
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from auth import get_upstream_token
from config import config, logger
from errors import APIError

router = APIRouter(tags=["Dashboard"])
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
_MAX_QUOTA_VALUE = (1 << 53) - 1


def _quota_number(value: Any) -> Optional[float]:
    """Reject booleans, coercions, infinities and unrealistic/unsafe numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= _MAX_QUOTA_VALUE or not math.isfinite(value):
        return None
    return value


def _usage_data(raw_json: Any) -> Tuple[Dict[str, Any], bool]:
    if not isinstance(raw_json, dict) or not isinstance(raw_json.get("data"), dict):
        return {}, False
    data = raw_json["data"]
    valid = all(_quota_number(data.get(key)) is not None for key in ("remaining", "used", "limit"))
    valid = valid and all(isinstance(data.get(key), bool) for key in ("banned", "exceeded"))
    # Remaining may not exceed the cycle limit. Used can exceed it upstream.
    if valid and data["remaining"] > data["limit"]:
        valid = False
    if "ban_ttl" in data and _quota_number(data["ban_ttl"]) is None:
        valid = False
    # The client checks business codes too, but keep standalone rendering safe.
    code = raw_json.get("code")
    if code is not None and (isinstance(code, bool) or str(code) not in ("0", "200", "10000")):
        valid = False
    if raw_json.get("success") is False or raw_json.get("error"):
        valid = False
    return data, valid


def _display_number(value: Any) -> str:
    number = _quota_number(value)
    if number is None:
        return "—"
    return format(number, ".15g") if isinstance(number, float) else str(number)


def _json_for_display(value: Any) -> Any:
    """Keep diagnostics JSON readable even if an upstream sends NaN/Infinity."""
    if isinstance(value, dict):
        return {str(key): _json_for_display(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_for_display(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def render_dashboard_html(
    raw_json: Dict[str, Any], token: Optional[str], *, error: Optional[str] = None, nonce: str = "", port: Optional[int] = None
) -> str:
    """Render telemetry without putting untrusted text into executable contexts.

    The original two positional arguments remain supported. Credentials are
    represented only by whether one is configured; no fragments are displayed.
    """
    data, valid = _usage_data(raw_json)
    known = valid and error is None
    if not known:
        state = "unknown"
        error = error or "Upstream returned incomplete or invalid quota data."
    elif data["banned"]:
        state = "banned"
    elif data["exceeded"] or data["remaining"] == 0:
        state = "exceeded"
    else:
        state = "normal"

    percentage = 0
    if known and data["limit"] > 0:
        percentage = 100 if data["used"] >= data["limit"] else int(data["used"] / data["limit"] * 100)
    role = data.get("role")
    role = role if isinstance(role, str) and role else "UNKNOWN"
    values = {
        "remaining": _display_number(data.get("remaining")) if known else "—",
        "used": _display_number(data.get("used")) if known else "—",
        "limit": _display_number(data.get("limit")) if known else "—",
        "percentage": str(percentage),
        "percentage_label": f"{percentage}%" if known else "—",
        "role": role,
        "exceeded": str(data["exceeded"]).lower() if known else "unknown",
        "banned": str(data["banned"]).lower() if known else "unknown",
        "ban_ttl": _display_number(data.get("ban_ttl")) if known else "—",
        "token_status_key": "credentialConfigured" if token else "credentialMissing",
        "token_status": "Configured" if token else "Not configured",
        "pretty_json": json.dumps(_json_for_display(raw_json), indent=2, ensure_ascii=False, allow_nan=False),
        "port": str(config.port if port is None else port),
        "state": state,
        "status_key": {"normal": "statusNormal", "banned": "statusBanned", "exceeded": "statusExceeded", "unknown": "statusUnknown"}[state],
        "status_text": {"normal": "Quota available", "banned": "Banned", "exceeded": "Exceeded", "unknown": "Unavailable"}[state],
        "error": error or "",
        "error_hidden": "" if error else "hidden",
        "nonce": nonce,
    }
    escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    # One pass prevents user-supplied {{placeholders}} from being expanded later.
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: escaped.get(match.group(1), match.group(0)), template)


def _accepts_html(accept: str) -> bool:
    """Honor explicit media types and q-values; generic browser requests get UI."""
    if not accept.strip():
        return False
    entries = {}
    for item in accept.lower().split(","):
        media_type, *parameters = item.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if separator and name == "q":
                try:
                    quality = float(value)
                    if not math.isfinite(quality) or not 0 <= quality <= 1:
                        quality = 0.0
                except ValueError:
                    quality = 0.0
        entries[media_type] = max(quality, entries.get(media_type, 0.0))

    def quality(media_type: str) -> float:
        if media_type in entries:
            return entries[media_type]
        major = media_type.split("/", 1)[0] + "/*"
        return entries.get(major, entries.get("*/*", 0.0))

    html_quality, json_quality = quality("text/html"), quality("application/json")
    if "application/json" in entries and json_quality > 0 and json_quality >= html_quality:
        return False
    return html_quality > 0 and html_quality >= json_quality


def _response_headers(nonce: Optional[str] = None) -> Dict[str, str]:
    headers = {"Cache-Control": "no-store", "Vary": "Accept, Authorization", "X-Content-Type-Options": "nosniff"}
    if nonce:
        headers.update({
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
        })
    return headers


@router.get("/")
@router.get("/health")
async def health_check():
    """Public process liveness only; does not test upstream connectivity/auth."""
    return JSONResponse(
        content={"status": "ok", "service": "fanzha-ai-proxy", "check": "liveness", "upstream_checked": False, "timestamp": int(time.time())},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/usage")
@router.get("/v1/dashboard/billing/usage")
async def check_usage(request: Request):
    """Return successful upstream telemetry or an error with its real HTTP status."""
    service = request.app.state.service
    show_html = _accepts_html(request.headers.get("accept", ""))
    token = None
    error = None
    try:
        token = get_upstream_token(request, service)
        raw_res = await service.fetch_usage(token)
        if not _usage_data(raw_res)[1]:
            raise APIError("Upstream returned incomplete or invalid quota data.", status_code=502)
        # Validate the full JSON value too: extra fields must not cause an
        # uncaught JSONResponse error (NaN, surrogates or excessive nesting).
        json.dumps(raw_res, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except APIError as exc:
        error = exc
        raw_res = exc.as_dict()
    except Exception:
        logger.error("Quota query failed unexpectedly.")
        error = APIError("Unable to retrieve upstream quota data.", status_code=502)
        raw_res = error.as_dict()

    status_code = error.status_code if error else 200
    if show_html:
        nonce = secrets.token_urlsafe(24)
        # None means server-managed credentials, not a missing credential.
        active_token = token if token is not None else getattr(service, "access_token", "")
        error_message = raw_res["error"]["message"] if error else None
        port = getattr(getattr(service, "cfg", None), "port", config.port)
        content = render_dashboard_html(raw_res, active_token, error=error_message, nonce=nonce, port=port)
        headers = dict(getattr(error, "headers", None) or {})
        headers.update(_response_headers(nonce))
        return HTMLResponse(content=content, status_code=status_code, headers=headers)
    headers = dict(getattr(error, "headers", None) or {})
    headers.update(_response_headers())
    return JSONResponse(content=raw_res, status_code=status_code, headers=headers)
