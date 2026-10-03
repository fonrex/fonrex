"""Authentication must be enforced unless it is explicitly disabled."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from auth.dependencies import (
    get_configured_api_keys,
    is_auth_enforced,
    validate_api_key,
)
from main import app, log_authentication_mode

WELL_FORMED_KEY = "frx_live_well_formed_but_unknown"
KEY_ENV_VARS = ("FONREX_API_KEY", "FONREX_RELAY_KEY", "FONREX_API_KEYS")


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
