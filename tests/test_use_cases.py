"""Tests for the transport-independent application layer."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from use_cases.errors import InvalidInput, ResourceNotFound
from use_cases.fundamentals import GetDeepFundamentals, GetFundamentals
from use_cases.realtime import GetQuote, UnsubscribeTicker
from use_cases.specialized import GetEtfDetails, GetIndexConstituents


@pytest.mark.asyncio
async def test_fundamentals_rejects_missing_identity_without_http_dependency():
    with pytest.raises(InvalidInput):
        await GetFundamentals().execute()


@pytest.mark.asyncio
async def test_fundamentals_returns_cache_metadata():
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"General": {"Code": "AAPL"}})))
    result = await GetFundamentals(redis=redis).execute(ticker="AAPL")
    assert result.data == {"General": {"Code": "AAPL"}}
    assert result.cache_hit is True


@pytest.mark.asyncio
async def test_fundamentals_accepts_isin_without_database():
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"General": {"ISIN": "FR0001"}})))
    result = await GetFundamentals(redis=redis).execute(isin="FR0001")
    assert result.data["General"]["ISIN"] == "FR0001"


@pytest.mark.asyncio
async def test_fundamentals_does_not_hide_unexpected_repository_errors():
    database = SimpleNamespace(
        get_asset_details=MagicMock(side_effect=RuntimeError("repository bug"))
    )
    with pytest.raises(RuntimeError, match="repository bug"):
        await GetFundamentals(database=database).execute(ticker="AAPL")


@pytest.mark.asyncio
async def test_fundamentals_orchestrates_injected_ports():
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=(
                {"YahooFinance": {"ticker": "AAPL"}},
                {"YahooFinance": "https://example.test/AAPL"},
            )
        )
    )
    formatter = SimpleNamespace(to_eodhd=MagicMock(return_value={"General": {"Code": "AAPL"}}))

    result = await GetFundamentals(
        provider_runner=runner,
        formatter=formatter,
    ).execute(ticker="AAPL", nocache=True)

    assert result.data == {"General": {"Code": "AAPL"}}
    assert result.provider_used == "YahooFinance"
    runner.run.assert_awaited_once()
    formatter.to_eodhd.assert_called_once()


@pytest.mark.asyncio
async def test_fundamentals_coordinates_identity_enrichment_and_cache_ports():
    database = SimpleNamespace(
        get_asset_details=MagicMock(return_value={"isin": "US0378331005"}),
        get_asset_context=MagicMock(
            return_value={
                "details": {
                    "asset_id": 42,
                    "listing_id": 7,
                    "ticker": "AAPL",
                    "isin": "US0378331005",
                },
                "mappings": {},
            }
        ),
        get_deep_fundamentals=MagicMock(
            return_value={
                "statements": {"income": []},
                "analyst_ratings": {"consensus": "buy"},
                "earnings_history": [],
            }
        ),
    )
    redis = SimpleNamespace(
        get=AsyncMock(return_value=None),
        setex=AsyncMock(),
    )
    runner = SimpleNamespace(run=AsyncMock(return_value=({"YahooFinance": {"ticker": "AAPL"}}, {})))
    profile_enricher = SimpleNamespace(enrich=AsyncMock())
    sec_provider = SimpleNamespace(fetch=AsyncMock(return_value={"transactions": []}))

    result = await GetFundamentals(
        database=database,
        redis=redis,
        provider_runner=runner,
        profile_enricher=profile_enricher,
        ticker_normalizer=lambda _ticker: "AAPL",
        sec_edgar_provider=sec_provider,
    ).execute(ticker="AAPL:NASDAQ", fmt="raw")

    assert result.data["asset_profile"]["asset_id"] == 42
    assert result.data["Financials"] == {"income": []}
    assert result.data["SECEdgar"] == {"transactions": []}
    profile_enricher.enrich.assert_awaited_once()
    sec_provider.fetch.assert_awaited_once_with(ticker="AAPL", limit=10)
    redis.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_deep_fundamentals_uses_application_errors():
    with pytest.raises(InvalidInput):
        await GetDeepFundamentals(database=MagicMock()).execute()


@pytest.mark.asyncio
async def test_deep_fundamentals_uses_repository_and_enricher_ports():
    database = SimpleNamespace(
        get_asset_context=MagicMock(
            return_value={
                "details": {
                    "asset_id": 42,
                    "ticker": "AAPL",
                    "isin": "US0378331005",
                    "name": "Apple",
                    "exchange": "NASDAQ",
                    "currency": "USD",
                }
            }
        ),
        get_deep_sections=MagicMock(return_value={"highlights": {"market_cap": 123}}),
    )
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={"highlights": True}))

    result = await GetDeepFundamentals(
        database=database,
        enricher=enricher,
    ).execute(ticker="AAPL", sections="highlights")

    assert result["highlights"] == {"market_cap": 123}
    assert result["asset_profile"]["ticker"] == "AAPL"
    enricher.enrich.assert_awaited_once_with(42, "AAPL")
    database.get_deep_sections.assert_called_once_with(42, {"highlights"}, False)


@pytest.mark.asyncio
async def test_etf_use_case_rejects_non_etf_before_provider_call():
    provider = SimpleNamespace(fetch=AsyncMock())
    database = SimpleNamespace(
        get_asset_context=MagicMock(return_value={"details": {"quote_type": "EQUITY"}})
    )
    with pytest.raises(ResourceNotFound):
        await GetEtfDetails(provider, database).execute("FR0000000001")
    provider.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_use_case_validates_supported_names():
    with pytest.raises(InvalidInput):
        await GetIndexConstituents(provider=object(), index_name_enum=object()).execute("UNKNOWN")


@pytest.mark.asyncio
async def test_quote_use_case_builds_snapshot_from_worker_cache():
    worker = SimpleNamespace(
        get_quote_from_cache=AsyncMock(return_value={"close": 123.4, "previous_close": 120.0})
    )
    quote = await GetQuote(worker).execute("aapl")
    assert quote.ticker == "AAPL"
    assert float(quote.price) == 123.4
    assert quote.is_realtime is True


@pytest.mark.asyncio
async def test_unsubscribe_use_case_reports_missing_subscription():
    worker = SimpleNamespace(unsubscribe=AsyncMock(return_value=False))
    with pytest.raises(ResourceNotFound):
        await UnsubscribeTicker(worker).execute("aapl")


def test_use_case_modules_do_not_import_fastapi():
    root = Path(__file__).parents[1] / "use_cases"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported_roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "fastapi" not in imported_roots, path


def test_fundamentals_use_case_depends_only_on_application_ports():
    path = Path(__file__).parents[1] / "use_cases" / "fundamentals.py"
    tree = ast.parse(path.read_text())
    imported_roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_roots.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "database",
        "fastapi",
        "financials",
        "fundamental",
        "import_assets",
        "models",
        "redis",
        "sqlalchemy",
        "yfinance",
    }
    assert imported_roots.isdisjoint(forbidden)
