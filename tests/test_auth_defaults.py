"""Authentication must be enforced unless it is explicitly disabled."""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from auth.dependencies import (
    get_configured_api_keys,
    is_auth_enforced,
    is_read_only_key,
    is_read_request,
    validate_api_key,
)
from main import app, log_authentication_mode
from schemas.realtime import QuoteSnapshot
from use_cases.realtime import GetQuote

WELL_FORMED_KEY = "frx_live_well_formed_but_unknown"
KEY_ENV_VARS = (
    "FONREX_API_KEY",
    "FONREX_RELAY_KEY",
    "FONREX_API_KEYS",
    "FONREX_READ_ONLY_API_KEYS",
)
FULL_KEY = "frx_live_full_access_secret_1"
READ_KEY = "frx_live_read_only_secret_2"


@pytest.fixture
def clean_auth_env(monkeypatch):
    """Start from an environment with no authentication setting at all."""
    for name in (*KEY_ENV_VARS, "FONREX_AUTH_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def client():
    """TestClient with minimal mocked services to allow startup."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.setex = AsyncMock()
    mock_fred = MagicMock()
    mock_fred.get_current_rates = AsyncMock(return_value={"risk_free_rate": None})

    names = ("redis_client", "db_service", "db_available", "fred_service")
    originals = {name: getattr(app.state, name, None) for name in names}

    with TestClient(app) as test_client:
        app.state.redis_client = mock_redis
        app.state.db_service = MagicMock()
        app.state.db_available = True
        app.state.fred_service = mock_fred
        yield test_client

    for name, value in originals.items():
        setattr(app.state, name, value)


def test_suite_does_not_inherit_credentials_from_the_shell():
    """A key exported in the developer's shell must not switch auth on in the tests.

    `tests/conftest.py` clears the credentials before anything is imported. Without
    it, `export FONREX_API_KEY=...` in the terminal makes every unauthenticated
    request of the suite answer 401.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_router_integration.py::test_get_news_stats",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            **os.environ,
            "FONREX_API_KEY": "frx_live_exported_in_the_shell",
            "FONREX_AUTH_REQUIRED": "true",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]


class TestSecureDefault:
    def test_auth_is_enforced_without_any_configuration(self, clean_auth_env):
        assert is_auth_enforced() is True

    def test_empty_key_variable_does_not_disable_auth(self, clean_auth_env):
        # `FONREX_API_KEY=` is what a freshly copied .env.example provides.
        clean_auth_env.setenv("FONREX_API_KEY", "")
        assert get_configured_api_keys() == []
        assert is_auth_enforced() is True

    def test_well_formed_key_is_not_a_credential(self, clean_auth_env):
        assert validate_api_key(WELL_FORMED_KEY) is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "", "maybe"])
    def test_only_explicit_false_values_disable_auth(self, clean_auth_env, value):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", value)
        assert is_auth_enforced() is True
        assert validate_api_key(WELL_FORMED_KEY) is False


class TestExplicitOptOut:
    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " false "])
    def test_false_values_disable_auth(self, clean_auth_env, value):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", value)
        assert is_auth_enforced() is False

    def test_configured_key_wins_over_opt_out(self, clean_auth_env):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", "false")
        clean_auth_env.setenv("FONREX_API_KEY", "frx_live_configured_secret_1")

        assert is_auth_enforced() is True
        assert validate_api_key("frx_live_configured_secret_1") is True
        assert validate_api_key(WELL_FORMED_KEY) is False


class TestConfiguredKeys:
    def test_keys_are_collected_from_every_variable(self, clean_auth_env):
        clean_auth_env.setenv("FONREX_API_KEY", "frx_live_primary_1")
        clean_auth_env.setenv("FONREX_RELAY_KEY", "frx_live_relay_2")
        clean_auth_env.setenv("FONREX_API_KEYS", "frx_live_extra_3, frx_live_extra_4 ,")

        assert get_configured_api_keys() == [
            "frx_live_primary_1",
            "frx_live_relay_2",
            "frx_live_extra_3",
            "frx_live_extra_4",
        ]
        assert validate_api_key("frx_live_extra_4") is True


class TestRoutesWithSecureDefault:
    def test_protected_route_rejects_anonymous_requests(self, client, clean_auth_env):
        response = client.get("/macro/rates")

        assert response.status_code == 401
        assert "Missing API key" in response.json()["detail"]

    def test_protected_route_rejects_well_formed_unknown_key(self, client, clean_auth_env):
        response = client.get("/macro/rates", headers={"X-API-KEY": WELL_FORMED_KEY})

        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/cache/clear"),
            ("post", "/database/cleanup"),
            ("post", "/health/canary/run"),
            ("get", "/database/stats"),
        ],
    )
    def test_administration_routes_are_protected(self, client, clean_auth_env, method, path):
        response = getattr(client, method)(path)

        assert response.status_code == 401

    @pytest.mark.parametrize("path", ["/health", "/widgets.json", "/apps.json", "/openapi.json"])
    def test_public_routes_stay_reachable(self, client, clean_auth_env, path):
        response = client.get(path)

        assert response.status_code not in (401, 403)

    def test_configured_key_grants_access(self, client, clean_auth_env):
        clean_auth_env.setenv("FONREX_API_KEY", "frx_live_configured_secret_1")

        response = client.get(
            "/macro/rates", headers={"Authorization": "Bearer frx_live_configured_secret_1"}
        )

        assert response.status_code == 200

    def test_opt_out_opens_the_routes(self, client, clean_auth_env):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", "false")

        response = client.get("/macro/rates")

        assert response.status_code == 200


class TestReadOnlyKeys:
    """A read-only key is safe to hand to a spreadsheet or a dashboard."""

    @pytest.fixture
    def keys(self, clean_auth_env):
        clean_auth_env.setenv("FONREX_API_KEY", FULL_KEY)
        clean_auth_env.setenv("FONREX_READ_ONLY_API_KEYS", f"{READ_KEY}, frx_live_other_reader_3")
        return clean_auth_env

    def test_read_only_keys_are_valid_credentials(self, keys):
        assert validate_api_key(READ_KEY) is True
        assert validate_api_key("frx_live_other_reader_3") is True
        assert is_read_only_key(READ_KEY) is True
        assert is_read_only_key(FULL_KEY) is False
        assert is_read_only_key("frx_live_unknown_key_9") is False

    def test_a_read_only_key_alone_still_enforces_authentication(self, clean_auth_env):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", "false")
        clean_auth_env.setenv("FONREX_READ_ONLY_API_KEYS", READ_KEY)

        assert is_auth_enforced() is True
        assert get_configured_api_keys() == [READ_KEY]

    def test_a_key_listed_twice_keeps_full_access(self, clean_auth_env):
        clean_auth_env.setenv("FONREX_API_KEY", FULL_KEY)
        clean_auth_env.setenv("FONREX_READ_ONLY_API_KEYS", FULL_KEY)

        assert is_read_only_key(FULL_KEY) is False

    @pytest.mark.parametrize(
        ("method", "path", "expected"),
        [
            ("GET", "/fundamental", True),
            ("get", "/database/stats", True),
            ("HEAD", "/health", True),
            ("OPTIONS", "/cache/clear", True),
            ("POST", "/technical/batch", True),
            ("POST", "/dcf/AIR.PA", True),
            ("POST", "/dcf/AIR.PA/compare", False),
            ("POST", "/cache/clear", False),
            ("POST", "/cache/clear/AIR.PA", False),
            ("POST", "/database/cleanup", False),
            ("POST", "/historical/ingest", False),
            ("POST", "/historical/ingest/bulk", False),
            ("POST", "/health/canary/run", False),
            ("POST", "/news/AIR.PA/refresh", False),
            ("POST", "/realtime/subscribe", False),
            ("DELETE", "/realtime/subscribe/AIR.PA", False),
            ("PUT", "/fundamental", False),
            ("PATCH", "/fundamental", False),
        ],
    )
    def test_read_requests_are_recognised(self, method, path, expected):
        assert is_read_request(method, path) is expected

    def test_read_only_key_can_read(self, client, keys):
        response = client.get("/macro/rates", headers={"Authorization": f"Bearer {READ_KEY}"})

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/cache/clear"),
            ("post", "/cache/clear/AIR.PA"),
            ("post", "/database/cleanup"),
            ("post", "/historical/ingest"),
            ("post", "/historical/ingest/bulk"),
            ("post", "/health/canary/run"),
            ("post", "/news/AIR.PA/refresh"),
            ("post", "/realtime/subscribe"),
            ("delete", "/realtime/subscribe/AIR.PA"),
        ],
    )
    def test_read_only_key_cannot_change_anything(self, client, keys, method, path):
        response = getattr(client, method)(path, headers={"X-API-KEY": READ_KEY})

        assert response.status_code == 403
        assert "read-only" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/cache/clear"),
            ("post", "/database/cleanup"),
            ("delete", "/realtime/subscribe/AIR.PA"),
        ],
    )
    def test_full_access_key_is_not_restricted(self, client, keys, method, path):
        response = getattr(client, method)(path, headers={"X-API-KEY": FULL_KEY})

        assert response.status_code not in (401, 403)

    def test_read_only_key_may_run_a_computation(self, client, keys):
        response = client.post(
            "/technical/batch",
            headers={"X-API-KEY": READ_KEY},
            json={"tickers": [], "indicators": []},
        )

        assert response.status_code not in (401, 403)

    @pytest.fixture
    def quote_execute(self, monkeypatch):
        """Spy on GetQuote.execute: its second argument says whether it subscribes."""
        execute = AsyncMock(
            return_value=QuoteSnapshot(
                ticker="AIR.PA", price=100.0, timestamp=datetime.now(timezone.utc)
            )
        )
        monkeypatch.setattr(GetQuote, "execute", execute)
        return execute

    def test_quote_does_not_subscribe_by_default(self, client, keys, quote_execute):
        """A GET changes nothing unless asked to (AGENTS.md, rule 7), whatever the key."""
        response = client.get("/quote/AIR.PA", headers={"X-API-KEY": FULL_KEY})

        assert response.status_code == 200
        quote_execute.assert_called_once_with("AIR.PA", False)

    def test_read_only_key_does_not_create_subscription_via_quote(
        self, client, keys, quote_execute
    ):
        reader = client.get(
            "/quote/AIR.PA?subscribe_if_missing=true", headers={"X-API-KEY": READ_KEY}
        )
        owner = client.get(
            "/quote/AIR.PA?subscribe_if_missing=true", headers={"X-API-KEY": FULL_KEY}
        )

        assert (reader.status_code, owner.status_code) == (200, 200)
        assert [call.args for call in quote_execute.call_args_list] == [
            ("AIR.PA", False),
            ("AIR.PA", True),
        ]

    def test_openbb_quote_never_creates_a_subscription(self, client, keys, quote_execute):
        """The OpenBB quote widget is a GET: it changes nothing, whatever the key."""
        reader = client.get("/openbb/quote/AIR.PA", headers={"X-API-KEY": READ_KEY})
        owner = client.get("/openbb/quote/AIR.PA", headers={"X-API-KEY": FULL_KEY})

        assert (reader.status_code, owner.status_code) == (200, 200)
        assert [call.args for call in quote_execute.call_args_list] == [
            ("AIR.PA", False),
            ("AIR.PA", False),
        ]

    def test_read_only_key_does_not_create_subscription_via_websocket(self, client, keys):
        """A read-only connection starts no stream, and is told so."""
        worker = MagicMock()
        worker.get_active_tickers = AsyncMock(return_value=[])
        worker.subscribe = AsyncMock()
        worker.get_quote_from_cache = AsyncMock(return_value=None)

        # The route listens to Redis pub/sub: a channel that delivers nothing.
        async def no_message():
            return
            yield

        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        pubsub.listen = no_message
        app.state.redis_client.pubsub = MagicMock(return_value=pubsub)

        original_worker = getattr(app.state, "realtime_worker", None)
        app.state.realtime_worker = worker
        try:
            with client.websocket_connect("/ws/realtime/AIR.PA?token=" + READ_KEY) as ws:
                message = ws.receive_json()
        finally:
            app.state.realtime_worker = original_worker

        worker.get_active_tickers.assert_awaited_once()  # the route did run
        worker.subscribe.assert_not_called()
        assert message["type"] == "not_streaming"
        assert message["ticker"] == "AIR.PA"
        assert "read-only" in message["error"]


class TestStartupLog:
    def test_missing_key_is_reported(self, clean_auth_env, caplog):
        with caplog.at_level("WARNING", logger="main"):
            log_authentication_mode()

        assert "no API key is configured" in caplog.text

    def test_disabled_auth_is_reported(self, clean_auth_env, caplog):
        clean_auth_env.setenv("FONREX_AUTH_REQUIRED", "false")

        with caplog.at_level("WARNING", logger="main"):
            log_authentication_mode()

        assert "Authentication is DISABLED" in caplog.text
