"""Settings documented in ``.env.example`` must really drive the code."""

import importlib.util
import logging
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routers.technical as technical_router
import valuation.dcf_service as dcf_service_module
from models import FinancialStatement, FundamentalsHighlights
from schemas.dcf import DCFRequest, WACCInput
from schemas.technical import TechnicalRequest
from settings import env_decimal, env_int

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# Where a setting can legitimately be read.
SOURCE_SUFFIXES = {".py", ".yml", ".yaml", ".sh", ".gs"}
SOURCE_NAMES = {"Dockerfile", "Makefile"}
SKIPPED_DIRECTORIES = {
    "tests",
    "venv",
    "node_modules",
    "scratch",
    "data",
    "static",
    "logs",
    "docs",
    "img",
    "__pycache__",
}


def _documented_variables() -> list[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)


def _source_text() -> str:
    chunks = []
    for directory, subdirectories, files in os.walk(PROJECT_ROOT):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in SKIPPED_DIRECTORIES and not name.startswith(".")
        ]
        for name in files:
            path = Path(directory) / name
            if path.suffix in SOURCE_SUFFIXES or name in SOURCE_NAMES:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def _load_copy(monkeypatch, relative_path: str, env: dict[str, str]):
    """Import a fresh copy of a module, as a new process started with ``env`` would."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    module_name = "settings_probe_" + relative_path.replace("/", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


class TestEnvExampleIsNotDecorative:
    def test_every_documented_variable_is_read_somewhere(self):
        source = _source_text()
        unread = [
            name
            for name in _documented_variables()
            if not re.search(rf"""["']{name}["']|\$\{{?{name}\b""", source)
        ]
        assert not unread, (
            f"{unread} are documented in .env.example but no code reads them: "
            "wire them to the code or remove them from the file"
        )

    def test_documented_variables_are_unique(self):
        names = _documented_variables()
        assert len(names) == len(set(names))


class TestEnvInt:
    def test_unset_or_empty_gives_the_default(self, monkeypatch):
        monkeypatch.delenv("SETTING_UNDER_TEST", raising=False)
        assert env_int("SETTING_UNDER_TEST", 7) == 7
        monkeypatch.setenv("SETTING_UNDER_TEST", "   ")
        assert env_int("SETTING_UNDER_TEST", 7) == 7

    def test_value_is_parsed(self, monkeypatch):
        monkeypatch.setenv("SETTING_UNDER_TEST", " 42 ")
        assert env_int("SETTING_UNDER_TEST", 7, minimum=1, maximum=100) == 42

    def test_bounds_are_inclusive(self, monkeypatch):
        monkeypatch.setenv("SETTING_UNDER_TEST", "10")
        assert env_int("SETTING_UNDER_TEST", 7, minimum=10, maximum=10) == 10

    @pytest.mark.parametrize("raw", ["abc", "12.5", "1e3"])
    def test_unreadable_value_falls_back_and_is_logged(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("SETTING_UNDER_TEST", raw)
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert env_int("SETTING_UNDER_TEST", 7) == 7
        assert "SETTING_UNDER_TEST" in caplog.text

    @pytest.mark.parametrize("raw", ["0", "101"])
    def test_out_of_range_value_falls_back_and_is_logged(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("SETTING_UNDER_TEST", raw)
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert env_int("SETTING_UNDER_TEST", 7, minimum=1, maximum=100) == 7
        assert "outside the accepted range" in caplog.text


class TestEnvDecimal:
    def test_unset_gives_the_default(self, monkeypatch):
        monkeypatch.delenv("SETTING_UNDER_TEST", raising=False)
        assert env_decimal("SETTING_UNDER_TEST", "0.055") == Decimal("0.055")

    def test_value_is_parsed_exactly(self, monkeypatch):
        monkeypatch.setenv("SETTING_UNDER_TEST", "0.0475")
        assert env_decimal("SETTING_UNDER_TEST", "0.055", minimum="0", maximum="1") == Decimal(
            "0.0475"
        )

    @pytest.mark.parametrize("raw", ["five", "5,5", "NaN", "Infinity"])
    def test_unreadable_value_falls_back_and_is_logged(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("SETTING_UNDER_TEST", raw)
        with caplog.at_level(logging.WARNING, logger="settings"):
            assert env_decimal("SETTING_UNDER_TEST", "0.055") == Decimal("0.055")
        assert "SETTING_UNDER_TEST" in caplog.text

    def test_a_percentage_typed_instead_of_a_ratio_is_refused(self, monkeypatch, caplog):
        # 5.5 instead of 0.055 would silently multiply the premium by 100.
        monkeypatch.setenv("SETTING_UNDER_TEST", "5.5")
        with caplog.at_level(logging.WARNING, logger="settings"):
            value = env_decimal("SETTING_UNDER_TEST", "0.055", minimum="0", maximum="1")
        assert value == Decimal("0.055")
        assert "outside the accepted range" in caplog.text


class TestDcfSettings:
    def test_defaults_are_unchanged_without_configuration(self):
        request = DCFRequest()
        assert request.projection_years == 5
        assert request.terminal_growth_rate == Decimal("0.025")
        assert dcf_service_module.DEFAULT_EQUITY_RISK_PREMIUM == Decimal("0.055")

    def test_request_defaults_follow_the_environment(self, monkeypatch):
        schema = _load_copy(
            monkeypatch,
            "schemas/dcf.py",
            {"DCF_DEFAULT_PROJECTION_YEARS": "8", "DCF_TERMINAL_GROWTH_RATE": "0.02"},
        )
        request = schema.DCFRequest()
        assert request.projection_years == 8
        assert request.terminal_growth_rate == Decimal("0.02")
        # An explicit value in the request still wins.
        assert schema.DCFRequest(projection_years=4).projection_years == 4

    def test_projection_years_outside_the_accepted_range_keep_the_default(self, monkeypatch):
        schema = _load_copy(monkeypatch, "schemas/dcf.py", {"DCF_DEFAULT_PROJECTION_YEARS": "25"})
        assert schema.DCFRequest().projection_years == 5

    def test_equity_risk_premium_follows_the_environment(self, monkeypatch):
        service_module = _load_copy(
            monkeypatch, "valuation/dcf_service.py", {"DCF_EQUITY_RISK_PREMIUM": "0.07"}
        )
        assert service_module.DEFAULT_EQUITY_RISK_PREMIUM == Decimal("0.07")

    def _wacc(self, params=None):
        service = dcf_service_module.DCFService(MagicMock())
        highlights = FundamentalsHighlights(beta=Decimal("1.0"), market_cap=Decimal("1000000"))
        statements = [
            FinancialStatement(
                interest_expense=Decimal("-1000"),
                total_debt=Decimal("50000"),
                total_equity=Decimal("100000"),
                ebit=Decimal("10000"),
                tax_provision=Decimal("2500"),
            )
        ]
        return service._compute_wacc(
            highlights, statements, params, rf_fred=Decimal("0.04"), rf_source="fred_live"
        )

    def test_cost_of_equity_uses_the_configured_premium(self, monkeypatch):
        assert self._wacc().cost_of_equity == Decimal("0.095")

        monkeypatch.setattr(dcf_service_module, "DEFAULT_EQUITY_RISK_PREMIUM", Decimal("0.07"))

        assert self._wacc().cost_of_equity == Decimal("0.11")

    def test_request_premium_still_overrides_the_configured_one(self, monkeypatch):
        monkeypatch.setattr(dcf_service_module, "DEFAULT_EQUITY_RISK_PREMIUM", Decimal("0.07"))

        result = self._wacc(WACCInput(equity_risk_premium=Decimal("0.03")))

        assert result.cost_of_equity == Decimal("0.07")

    def test_cache_lifetime_follows_the_environment(self, monkeypatch):
        router = _load_copy(monkeypatch, "routers/valuation.py", {"DCF_CACHE_TTL": "900"})
        assert router.DCF_CACHE_TTL == 900


class TestNewsSettings:
    def test_limits_follow_the_environment(self, monkeypatch):
        router = _load_copy(
            monkeypatch, "routers/news.py", {"NEWS_DEFAULT_LIMIT": "5", "NEWS_MAX_LIMIT": "30"}
        )
        assert router.NEWS_DEFAULT_LIMIT == 5
        assert router.NEWS_MAX_LIMIT == 30
        # The feed default never exceeds the configured maximum.
        assert router.NEWS_FEED_DEFAULT_LIMIT == 30

    def test_default_limit_never_exceeds_the_maximum(self, monkeypatch):
        router = _load_copy(
            monkeypatch, "routers/news.py", {"NEWS_DEFAULT_LIMIT": "80", "NEWS_MAX_LIMIT": "10"}
        )
        assert router.NEWS_DEFAULT_LIMIT == 10

    def test_defaults_are_unchanged_without_configuration(self, monkeypatch):
        for name in ("NEWS_DEFAULT_LIMIT", "NEWS_MAX_LIMIT"):
            monkeypatch.delenv(name, raising=False)
        router = _load_copy(monkeypatch, "routers/news.py", {})
        assert (router.NEWS_DEFAULT_LIMIT, router.NEWS_MAX_LIMIT) == (20, 100)
        assert router.NEWS_FEED_DEFAULT_LIMIT == 50


class TestTechnicalSettings:
    def test_default_limit_follows_the_environment(self, monkeypatch):
        schema = _load_copy(monkeypatch, "schemas/technical.py", {"TECHNICAL_DEFAULT_LIMIT": "250"})
        assert schema.TECHNICAL_DEFAULT_LIMIT == 250
        assert schema.TechnicalRequest(tickers=["AIR.PA"], indicators=["rsi_14"]).limit == 250

    def test_default_limit_outside_the_accepted_range_keeps_the_default(self, monkeypatch):
        schema = _load_copy(monkeypatch, "schemas/technical.py", {"TECHNICAL_DEFAULT_LIMIT": "5"})
        assert schema.TECHNICAL_DEFAULT_LIMIT == 500

    def test_batch_limits_follow_the_environment(self, monkeypatch):
        router = _load_copy(
            monkeypatch,
            "routers/technical.py",
            {"TECHNICAL_MAX_BATCH_TICKERS": "3", "TECHNICAL_MAX_BATCH_INDICATORS": "2"},
        )
        assert router.TECHNICAL_MAX_BATCH_TICKERS == 3
        assert router.TECHNICAL_MAX_BATCH_INDICATORS == 2

    async def test_batch_refuses_more_tickers_than_configured(self, monkeypatch):
        monkeypatch.setattr(technical_router, "TECHNICAL_MAX_BATCH_TICKERS", 2)
        payload = TechnicalRequest(tickers=["A", "B", "C"], indicators=["rsi_14"])

        with pytest.raises(HTTPException) as error:
            await technical_router.get_technical_batch(payload, service=MagicMock())

        assert error.value.status_code == 400
        assert "Maximum 2 tickers" in error.value.detail

    async def test_batch_refuses_more_indicators_than_configured(self, monkeypatch):
        monkeypatch.setattr(technical_router, "TECHNICAL_MAX_BATCH_INDICATORS", 1)
        payload = TechnicalRequest(tickers=["A"], indicators=["rsi_14", "macd"])

        with pytest.raises(HTTPException) as error:
            await technical_router.get_technical_batch(payload, service=MagicMock())

        assert error.value.status_code == 400
        assert "Maximum 1 indicateurs" in error.value.detail

    async def test_default_batch_limits_are_unchanged(self):
        assert technical_router.TECHNICAL_MAX_BATCH_TICKERS == 20
        assert technical_router.TECHNICAL_MAX_BATCH_INDICATORS == 10
        payload = TechnicalRequest(tickers=[f"T{i}" for i in range(21)], indicators=["rsi_14"])

        with pytest.raises(HTTPException) as error:
            await technical_router.get_technical_batch(payload, service=MagicMock())

        assert "Maximum 20 tickers" in error.value.detail
