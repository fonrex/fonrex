"""Tests for the OpenBB Workspace integration layer.

Validates:
- /widgets.json and /apps.json serve valid JSON
- All documented Fonrex endpoints have a corresponding widget
- apps.json only references widgets that exist in widgets.json
- CORS allows the OpenBB origin
- X-API-KEY authentication works identically to Authorization: Bearer
- Missing auth headers return 401 on protected endpoints
- /widgets.json and /apps.json are excluded from OpenAPI schema
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """TestClient with minimal mocked services to allow startup."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()

    orig_redis = getattr(app.state, "redis_client", None)
    orig_db = getattr(app.state, "db_service", None)

    with TestClient(app) as test_client:
        app.state.redis_client = mock_redis
        app.state.db_service = MagicMock()
        yield test_client

    app.state.redis_client = orig_redis
    app.state.db_service = orig_db


@pytest.fixture
def widgets_data():
    """Load widgets.json from disk for structural validation."""
    widgets_path = Path(__file__).parent.parent / "integrations" / "openbb" / "widgets.json"
    return json.loads(widgets_path.read_text(encoding="utf-8"))


@pytest.fixture
def apps_data():
    """Load apps.json from disk for structural validation."""
    apps_path = Path(__file__).parent.parent / "integrations" / "openbb" / "apps.json"
    return json.loads(apps_path.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────
# Test: /widgets.json returns valid JSON
# ──────────────────────────────────────────────────────────────────────

def test_widgets_json_is_valid_json(client):
    """GET /widgets.json returns valid JSON with status 200."""
    response = client.get("/widgets.json")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict), "widgets.json should be a JSON object"
    assert len(data) > 0, "widgets.json should not be empty"


# ──────────────────────────────────────────────────────────────────────
# Test: all documented endpoints have widgets
# ──────────────────────────────────────────────────────────────────────

# The 19 Fonrex endpoints that must be covered by widgets
DOCUMENTED_ENDPOINTS = [
    "fundamental",
    "fundamental/deep",
    "eod/{ticker}",
    "ticker/{symbol}/history",
    "quote/{ticker}",
    "quotes",
    "technical/{ticker}",
    "technical/{ticker}/multi",
    "technical/{ticker}/chart",
    "technical/screen",
    "news/{ticker}",
    "news/feed",
    "dcf/{ticker}",
    "dcf/{ticker}/compare",
    "dcf/{ticker}/sensitivity",
    "insider-transactions/{ticker}",
    "etf/{isin}/details",
    "index/{name}/constituents",
    "macro/rates",
]


def test_widgets_json_covers_all_documented_endpoints(widgets_data):
    """Each documented Fonrex endpoint has a corresponding widget."""
    widget_endpoints = {w["endpoint"] for w in widgets_data.values()}
    missing = [ep for ep in DOCUMENTED_ENDPOINTS if ep not in widget_endpoints]
    assert not missing, f"Missing widgets for endpoints: {missing}"


# ──────────────────────────────────────────────────────────────────────
# Test: apps.json only references existing widgets
# ──────────────────────────────────────────────────────────────────────

def test_apps_json_references_only_existing_widgets(widgets_data, apps_data):
    """Every widget_id referenced in apps.json layout exists in widgets.json."""
    widget_ids = set(widgets_data.keys())
    referenced_ids = set()

    for app_def in apps_data:
        for tab in app_def.get("tabs", {}).values():
            for layout_item in tab.get("layout", []):
                referenced_ids.add(layout_item["i"])

    orphans = referenced_ids - widget_ids
    assert not orphans, f"apps.json references non-existent widgets: {orphans}"


# ──────────────────────────────────────────────────────────────────────
# Test: CORS allows OpenBB origin
# ──────────────────────────────────────────────────────────────────────

def test_cors_allows_openbb_origin(client):
    """A preflight request from https://pro.openbb.co receives proper CORS headers."""
    response = client.options(
        "/widgets.json",
        headers={
            "Origin": "https://pro.openbb.co",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "https://pro.openbb.co"


# ──────────────────────────────────────────────────────────────────────
# Test: X-API-KEY authenticates same as Bearer
# ──────────────────────────────────────────────────────────────────────

def test_x_api_key_header_authenticates_same_as_bearer():
    """X-API-KEY and Authorization: Bearer resolve to the same key."""
    from auth.dependencies import get_api_key_from_request
    from routers.dependencies import get_api_key_from_request as router_get_key

    # Single point of truth: both imports resolve to the same function
    assert get_api_key_from_request is router_get_key

    # Mock request with X-API-KEY
    mock_request_xapi = MagicMock()
    mock_request_xapi.headers = {"X-API-KEY": "frx_live_test123"}
    key_xapi = get_api_key_from_request(mock_request_xapi)

    # Mock request with Authorization: Bearer
    mock_request_bearer = MagicMock()
    mock_request_bearer.headers = {"Authorization": "Bearer frx_live_test123"}
    key_bearer = get_api_key_from_request(mock_request_bearer)

    assert key_xapi == key_bearer == "frx_live_test123"


# ──────────────────────────────────────────────────────────────────────
# Test: missing both auth headers returns 401
# ──────────────────────────────────────────────────────────────────────

def test_missing_both_auth_headers_returns_401():
    """When neither X-API-KEY nor Authorization Bearer is present:
    1. get_api_key_from_request returns None
    2. require_api_key raises HTTPException 401
    3. An endpoint protected with require_api_key returns 401 over HTTP.
    """
    from fastapi import Depends, FastAPI, HTTPException

    from auth.dependencies import get_api_key_from_request, require_api_key

    mock_request = MagicMock()
    mock_request.headers = {}
    key = get_api_key_from_request(mock_request)
    assert key is None

    # Calling require_api_key directly raises HTTPException(status_code=401)
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(mock_request)
    assert exc_info.value.status_code == 401

    # End-to-end FastAPI test: protected route returns 401 when unauthenticated
    test_app = FastAPI()

    @test_app.get("/protected")
    def protected_route(api_key: str = Depends(require_api_key)):
        return {"status": "ok", "key": api_key}

    test_client = TestClient(test_app)

    # No headers -> 401 Unauthorized
    resp_unauth = test_client.get("/protected")
    assert resp_unauth.status_code == 401

    # Bearer header -> 200 OK
    resp_bearer = test_client.get(
        "/protected", headers={"Authorization": "Bearer frx_live_test123"}
    )
    assert resp_bearer.status_code == 200
    assert resp_bearer.json()["key"] == "frx_live_test123"

    # X-API-KEY header -> 200 OK
    resp_xapi = test_client.get(
        "/protected", headers={"X-API-KEY": "frx_live_test123"}
    )
    assert resp_xapi.status_code == 200
    assert resp_xapi.json()["key"] == "frx_live_test123"


# ──────────────────────────────────────────────────────────────────────
# Test: /widgets.json and /apps.json not in OpenAPI schema
# ──────────────────────────────────────────────────────────────────────

def test_widgets_json_not_in_openapi_schema(client):
    """GET /widgets.json and /apps.json do not appear in /openapi.json."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema.get("paths", {})
    assert "/widgets.json" not in paths, "/widgets.json should not appear in OpenAPI schema"
    assert "/apps.json" not in paths, "/apps.json should not appear in OpenAPI schema"


# ──────────────────────────────────────────────────────────────────────
# Test: apps.json returns valid JSON
# ──────────────────────────────────────────────────────────────────────

def test_apps_json_is_valid_json(client):
    """GET /apps.json returns valid JSON with status 200."""
    response = client.get("/apps.json")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list), "apps.json should be a JSON array"
    assert len(data) == 2, "apps.json should contain exactly 2 apps"


# ──────────────────────────────────────────────────────────────────────
# Test: widget schema structure
# ──────────────────────────────────────────────────────────────────────

def test_widget_schema_structure(widgets_data):
    """Each widget has the required fields for OpenBB Workspace."""
    required_fields = {"name", "description", "category", "type", "endpoint", "gridData", "source", "params"}
    for widget_id, widget in widgets_data.items():
        missing = required_fields - set(widget.keys())
        assert not missing, f"Widget '{widget_id}' is missing fields: {missing}"
        assert widget["source"] == "Fonrex", f"Widget '{widget_id}' source should be 'Fonrex'"
        assert widget["type"] in {"table", "chart", "markdown", "metric"}, (
            f"Widget '{widget_id}' has invalid type: {widget['type']}"
        )


# ──────────────────────────────────────────────────────────────────────
# Test: Bearer header priority over X-API-KEY
# ──────────────────────────────────────────────────────────────────────

def test_bearer_takes_priority_over_x_api_key():
    """When both headers are present, Authorization: Bearer takes priority."""
    from routers.dependencies import get_api_key_from_request

    mock_request = MagicMock()
    mock_request.headers = {
        "Authorization": "Bearer frx_live_bearer_key",
        "X-API-KEY": "frx_live_xapi_key",
    }
    key = get_api_key_from_request(mock_request)
    assert key == "frx_live_bearer_key"


# ──────────────────────────────────────────────────────────────────────
# Test: invalid API key format or value returns 403
# ──────────────────────────────────────────────────────────────────────

def test_invalid_api_key_returns_403():
    """Arbitrary values such as 'frx_fake' or malformed keys are rejected with 403."""
    from fastapi import HTTPException

    from auth.dependencies import require_api_key

    for bad_key in ["frx_fake", "invalid", "Bearer 123", "frx_live_"]:
        mock_request = MagicMock()
        mock_request.headers = {"X-API-KEY": bad_key}
        with pytest.raises(HTTPException) as exc_info:
            require_api_key(mock_request)
        assert exc_info.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Test: configured FONREX_API_KEY validation
# ──────────────────────────────────────────────────────────────────────

def test_configured_api_key_validation(monkeypatch):
    """When FONREX_API_KEY is configured, only exact matching keys are accepted."""
    from fastapi import HTTPException

    from auth.dependencies import require_api_key

    monkeypatch.setenv("FONREX_API_KEY", "frx_live_production_secret_999")

    # Matching key passes
    req_valid = MagicMock()
    req_valid.headers = {"X-API-KEY": "frx_live_production_secret_999"}
    assert require_api_key(req_valid) == "frx_live_production_secret_999"

    # Non-matching key (even well-formatted) is rejected
    req_other = MagicMock()
    req_other.headers = {"X-API-KEY": "frx_live_other_valid_looking_key"}
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(req_other)
    assert exc_info.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Test: production routes enforce auth when FONREX_API_KEY is set
# ──────────────────────────────────────────────────────────────────────

def test_production_routes_auth_enforced_when_configured(client, monkeypatch):
    """When FONREX_API_KEY is configured, unauthenticated calls to API routes return 401."""
    monkeypatch.setenv("FONREX_API_KEY", "frx_live_secret123")

    # Public discovery route remains accessible
    resp_widgets = client.get("/widgets.json")
    assert resp_widgets.status_code == 200

    # Protected route without auth returns 401
    resp_unauth = client.get("/quotes?tickers=AAPL")
    assert resp_unauth.status_code == 401
    assert "Missing API key" in resp_unauth.json()["detail"]

    # Protected route with wrong key returns 403
    resp_wrong = client.get("/quotes?tickers=AAPL", headers={"X-API-KEY": "frx_live_wrong_key"})
    assert resp_wrong.status_code == 403

    # Protected route with valid key passes auth middleware
    resp_valid = client.get(
        "/quotes?tickers=AAPL",
        headers={"X-API-KEY": "frx_live_secret123"},
    )
    # The response is not 401 or 403 (it reaches the handler)
    assert resp_valid.status_code not in (401, 403)


# ──────────────────────────────────────────────────────────────────────
# Test: fonrex_eod and fonrex_history parameters match backend endpoints
# ──────────────────────────────────────────────────────────────────────

def test_eod_and_history_widget_parameters(widgets_data):
    """Verify parameters for fonrex_eod and fonrex_history match API route signatures."""
    # fonrex_eod should only use ticker and period (no resolution)
    eod_params = {p["paramName"] for p in widgets_data["fonrex_eod"]["params"]}
    assert "resolution" not in eod_params
    assert "ticker" in eod_params
    assert "period" in eod_params

    # fonrex_history should use symbol, start_date, end_date, interval
    history_params = {p["paramName"] for p in widgets_data["fonrex_history"]["params"]}
    assert history_params == {"symbol", "start_date", "end_date", "interval"}

