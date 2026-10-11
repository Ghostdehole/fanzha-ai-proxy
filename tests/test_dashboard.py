import copy
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errors import APIError
from routers import dashboard


@pytest.fixture
def usage():
    return {"code": 200, "data": {"remaining": 15, "used": 5, "limit": 20, "role": "USER", "exceeded": False, "banned": False, "ban_ttl": 0}}


@pytest.fixture
def client(monkeypatch, usage):
    service = SimpleNamespace(access_token="private-credential-should-not-display", fetch_usage=AsyncMock(return_value=usage))
    app = FastAPI()
    app.state.service = service
    app.include_router(dashboard.router)
    monkeypatch.setattr(dashboard, "get_upstream_token", lambda request, service: None)
    with TestClient(app) as client:
        yield client, service


@pytest.mark.parametrize("path", ["/usage", "/v1/dashboard/billing/usage"])
@pytest.mark.parametrize("accept", ["application/json", "application/json, */*", "application/json;q=1, text/html;q=0.5", "application/*", ""])
def test_usage_json_negotiation_preserves_success(client, usage, path, accept):
    http, service = client
    response = http.get(path, headers={"Accept": accept})
    assert response.status_code == 200
    assert response.json() == usage
    assert response.headers["Cache-Control"] == "no-store"
    assert "Accept" in response.headers["Vary"]
    service.fetch_usage.assert_awaited_once_with(None)


@pytest.mark.parametrize("accept", ["text/html", "text/html,application/xhtml+xml,*/*;q=0.8", "*/*", "text/html;q=1, application/json;q=0.5"])
def test_html_success_security_headers_and_controls(client, accept):
    http, service = client
    response = http.get("/usage", headers={"Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-state="normal"' in response.text
    assert 'value="25"' in response.text
    assert service.access_token not in response.text
    assert service.access_token[:8] not in response.text
    assert 'data-i18n="credentialConfigured"' in response.text
    assert 'data-i18n="errorTitle"' in response.text
    assert 'role="alert" hidden' in response.text
    assert "toggleTheme" in response.text and "toggleLanguage" in response.text
    assert "document.documentElement.lang = lang" in response.text
    assert "账户配额与运行监控面板" in response.text
    assert 'data-theme="dark"' in response.text
    assert "onclick=" not in response.text
    assert "style=" not in response.text
    for button in ("refresh-btn", "theme-btn", "lang-btn"):
        assert f"document.getElementById('{button}').addEventListener('click'" in response.text
    nonces = re.findall(r'<(?:style|script) nonce="([^"]+)"', response.text)
    assert len(nonces) == 2 and nonces[0] == nonces[1]
    csp = response.headers["Content-Security-Policy"]
    assert f"script-src 'nonce-{nonces[0]}'" in csp
    assert f"style-src 'nonce-{nonces[0]}'" in csp
    assert "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("status_code", [401, 429, 502, 504])
@pytest.mark.parametrize("accept", ["text/html", "application/json, */*"])
def test_upstream_failures_keep_http_status_and_unknown_state(client, status_code, accept):
    http, service = client
    error = APIError("Upstream quota unavailable <img src=x onerror=alert(1)>.", status_code=status_code, headers={"Retry-After": "60"})
    service.fetch_usage.side_effect = error
    response = http.get("/usage", headers={"Accept": accept})
    assert response.status_code == status_code
    assert response.headers["Retry-After"] == "60"
    assert response.headers["Cache-Control"] == "no-store"
    if accept.startswith("text/html"):
        assert 'data-state="unknown"' in response.text
        assert 'data-i18n="statusUnknown"' in response.text
        assert 'data-state="normal"' not in response.text
        assert "<img" not in response.text
        assert "&lt;img" in response.text
        assert 'role="alert" hidden' not in response.text
        assert '<div id="metric-remaining-val" class="metric-value">—</div>' in response.text
    else:
        assert response.json() == error.as_dict()


def test_unexpected_error_does_not_expose_exception_or_credentials(client, caplog):
    http, service = client
    secret = "https://upstream.invalid/?access_token=very-private-credential"
    service.fetch_usage.side_effect = RuntimeError(secret)
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 502
    assert 'data-state="unknown"' in response.text
    assert secret not in response.text
    assert secret not in caplog.text


def test_auth_error_does_not_query_upstream(client, monkeypatch):
    http, service = client
    def no_auth(request, service):
        raise APIError("Missing access token.", status_code=401)
    monkeypatch.setattr(dashboard, "get_upstream_token", no_auth)
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 401
    assert 'data-state="unknown"' in response.text
    service.fetch_usage.assert_not_awaited()


@pytest.mark.parametrize("key,value", [
    ("remaining", -1), ("remaining", True), ("remaining", "15"),
    ("remaining", 21), ("used", float("nan")), ("limit", float("inf")),
    ("limit", None), ("used", 1 << 54), ("used", {}), ("limit", []),
    ("banned", "false"), ("exceeded", 0), ("ban_ttl", -1),
])
def test_malformed_telemetry_is_unavailable_not_green(client, usage, key, value):
    http, service = client
    malformed = copy.deepcopy(usage)
    malformed["data"][key] = value
    service.fetch_usage.return_value = malformed
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 502
    assert 'data-state="unknown"' in response.text
    assert 'data-state="normal"' not in response.text


@pytest.mark.parametrize("key", ["remaining", "used", "limit", "banned", "exceeded"])
def test_missing_required_telemetry_is_unavailable(client, usage, key):
    http, service = client
    malformed = copy.deepcopy(usage)
    del malformed["data"][key]
    service.fetch_usage.return_value = malformed
    response = http.get("/usage", headers={"Accept": "application/json"})
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


@pytest.mark.parametrize("malformed", [None, [], {}, {"data": None}, {"data": []}, {"data": {}}])
def test_malformed_top_level_payload_is_unavailable(client, malformed):
    http, service = client
    service.fetch_usage.return_value = malformed
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 502
    assert 'data-state="unknown"' in response.text


def test_optional_role_and_ban_ttl_can_be_absent(client, usage):
    http, service = client
    minimal = copy.deepcopy(usage)
    del minimal["data"]["role"]
    del minimal["data"]["ban_ttl"]
    service.fetch_usage.return_value = minimal
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert 'data-state="normal"' in response.text
    assert "UNKNOWN" in response.text


@pytest.mark.parametrize("changes,state", [
    ({"banned": True}, "banned"),
    ({"exceeded": True}, "exceeded"),
    ({"remaining": 0}, "exceeded"),
    ({"remaining": 0, "limit": 0}, "exceeded"),
    ({"remaining": 0, "used": 20, "limit": 5e-324}, "exceeded"),
])
def test_valid_restriction_states_and_zero_limits(client, usage, changes, state):
    http, service = client
    payload = copy.deepcopy(usage)
    payload["data"].update(changes)
    service.fetch_usage.return_value = payload
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert f'data-state="{state}"' in response.text


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.data = []
        self.script_depth = 0
        self.script_data = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.script_depth += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.script_depth -= 1

    def handle_data(self, data):
        self.data.append(data)
        if self.script_depth:
            self.script_data.append(data)


def test_html_escapes_all_upstream_text_and_does_not_interpolate_it_in_js(usage):
    attack = '</code><img src=x onerror="alert(1)"><script>alert("xss-marker")</script>& {{token_status}}'
    usage["data"]["role"] = attack
    usage["message"] = attack
    page = dashboard.render_dashboard_html(usage, "dont-ever-show-this-token", nonce='safe"nonce')
    parsed = PageParser()
    parsed.feed(page)
    assert all(tag != "img" for tag, attrs in parsed.tags)
    assert sum(tag == "script" for tag, attrs in parsed.tags) == 1
    assert "xss-marker" not in "".join(parsed.script_data)
    assert attack in "".join(parsed.data)
    assert "{{token_status}}" in page
    assert "dont-ever-show-this-token" not in page
    assert any(tag == "script" and attrs.get("nonce") == 'safe"nonce' for tag, attrs in parsed.tags)


def test_direct_render_of_bad_business_response_is_unknown(usage):
    usage["code"] = 401
    assert 'data-state="unknown"' in dashboard.render_dashboard_html(usage, "configured")


def test_public_health_is_only_process_liveness(client):
    http, service = client
    for path in ("/", "/health"):
        response = http.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["check"] == "liveness"
        assert response.json()["upstream_checked"] is False
        assert response.headers["Cache-Control"] == "no-store"
    service.fetch_usage.assert_not_awaited()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "\ud800"])
@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_malformed_extra_fields_cannot_cause_response_500(client, usage, value, accept):
    http, service = client
    payload = copy.deepcopy(usage)
    payload["extra"] = value
    service.fetch_usage.return_value = payload
    response = http.get("/usage", headers={"Accept": accept})
    assert response.status_code == 502
    if accept == "text/html":
        assert 'data-state="unknown"' in response.text
    else:
        assert response.json()["error"]["type"] == "upstream_error"


@pytest.mark.parametrize("code", [0, "0", 200, "200", 10000, "10000", None])
@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_usage_accepts_every_client_supported_success_code(client, usage, code, accept):
    http, service = client
    payload = copy.deepcopy(usage)
    payload["code"] = code
    service.fetch_usage.return_value = payload
    response = http.get("/usage", headers={"Accept": accept})
    assert response.status_code == 200
    if accept == "application/json":
        assert response.json() == payload
    else:
        assert 'data-state="normal"' in response.text


@pytest.mark.parametrize("code", [False, True, 0.0, 200.0, "false", 401, "401"])
def test_usage_rejects_non_success_codes_even_if_quota_fields_are_present(client, usage, code):
    http, service = client
    payload = copy.deepcopy(usage)
    payload["code"] = code
    service.fetch_usage.return_value = payload
    response = http.get("/usage", headers={"Accept": "text/html"})
    assert response.status_code == 502
    assert 'data-state="unknown"' in response.text


def test_template_has_no_style_attributes_or_inline_event_handlers(usage):
    page = dashboard.render_dashboard_html(usage, "configured", nonce="test-nonce")
    parsed = PageParser()
    parsed.feed(page)
    for tag, attrs in parsed.tags:
        assert "style" not in attrs
        assert not any(key.startswith("on") for key in attrs)
        if tag in ("style", "script"):
            assert attrs.get("nonce") == "test-nonce"
    # A native progress element replaces the former inline width style.
    assert any(tag == "progress" and attrs.get("value") == "25" for tag, attrs in parsed.tags)
