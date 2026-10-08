"""Focused tests for previously under-covered application boundaries."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from cache.adapters import ResilientAsyncJsonCache
from database.technical import SqlAlchemyTechnicalRepository
from financials.enrichment.adapters import (
    YFinanceAssetProfileEnricher,
    YFinanceDeepFundamentalsEnricher,
)
from technical.contracts import MarketSeries
from use_cases.errors import DependencyUnavailable, UpstreamFailure
from use_cases.specialized import (
    GetEtfDetails,
    GetIndexConstituents,
    GetInsiderTransactions,
)


class DumpModel:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return self.payload


@pytest.mark.asyncio
async def test_resilient_cache_delegates_successful_operations():
    redis = SimpleNamespace(
        get=AsyncMock(return_value=b'{"ok": true}'),
        setex=AsyncMock(return_value=True),
    )
    cache = ResilientAsyncJsonCache(redis)

    assert await cache.get("key") == b'{"ok": true}'
    assert await cache.setex("key", 60, "value") is True
    redis.setex.assert_awaited_once_with("key", 60, "value")


@pytest.mark.asyncio
async def test_resilient_cache_absorbs_only_redis_failures():
    redis = SimpleNamespace(
        get=AsyncMock(side_effect=RedisConnectionError("offline")),
        setex=AsyncMock(side_effect=RedisConnectionError("offline")),
    )
    cache = ResilientAsyncJsonCache(redis)

    assert await cache.get("key") is None
    assert await cache.setex("key", 60, "value") is None


@pytest.mark.asyncio
async def test_resilient_cache_exposes_programming_errors():
    cache = ResilientAsyncJsonCache(SimpleNamespace(get=AsyncMock(side_effect=ValueError("bug"))))
    with pytest.raises(ValueError, match="bug"):
        await cache.get("key")


def _query_session(rows: list[dict[str, object]]) -> MagicMock:
    session = MagicMock()
    session.execute.return_value.mappings.return_value.all.return_value = rows
    return session


SERIES = MarketSeries(asset_id=42, listing_id=7)


@pytest.mark.parametrize(
    ("resolution", "expected_table", "expected_column", "series_filter", "expected_bounds"),
    [
        # End-of-day prices: one series per listing, bounds at midnight UTC of the session.
        (
            "1D",
            "prices_eod",
            "time",
            "asset_listing_id = :series_id",
            {
                "series_id": 7,
                "from_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "to_date": datetime(2026, 1, 3, tzinfo=timezone.utc),
            },
        ),
        # Intraday prices: still one series per instrument.
        (
            "5min",
            "prices_intraday",
            "timestamp",
            "asset_id = :series_id",
            {"series_id": 42, "from_date": date(2026, 1, 1), "to_date": date(2026, 1, 3)},
        ),
    ],
)
def test_technical_data_source_builds_resolution_specific_query(
    resolution: str,
    expected_table: str,
    expected_column: str,
    series_filter: str,
    expected_bounds: dict,
):
    rows = [
        {
            "timestamp": "2026-01-02T10:00:00Z",
            "open": "10",
            "high": "12",
            "low": "9",
            "close": "11",
            "volume": "100",
        }
    ]
    session = _query_session(rows)
    database = SimpleNamespace(get_session=MagicMock(return_value=session))
    source = SqlAlchemyTechnicalRepository(database)

    frame = source._load_ohlcv_sync(
        SERIES,
        resolution,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 3),
        limit=25,
    )

    statement, parameters = session.execute.call_args.args
    assert expected_table in str(statement)
    assert series_filter in str(statement)
    assert f"{expected_column} >= :from_date" in str(statement)
    assert parameters == {"resolution": resolution, "limit": 25, **expected_bounds}
    assert frame.iloc[0]["close"] == 11.0
    assert frame.index.tz is not None
    session.close.assert_called_once()


def test_technical_data_source_normalizes_and_filters_rows():
    rows = [
        {"timestamp": "2026-01-02", "close": "bad", "volume": "1"},
        {"timestamp": "2026-01-01", "close": "12.5", "volume": "bad"},
    ]
    frame = SqlAlchemyTechnicalRepository._rows_to_dataframe(rows)

    assert list(frame["close"]) == [12.5]
    assert pd.isna(frame.iloc[0]["volume"])
    assert SqlAlchemyTechnicalRepository._rows_to_dataframe([]).empty


@pytest.mark.asyncio
async def test_technical_data_source_loads_in_worker_thread():
    source = SqlAlchemyTechnicalRepository(MagicMock())
    expected = pd.DataFrame({"close": [1.0]})
    with patch.object(source, "_load_ohlcv_sync", return_value=expected) as load:
        result = await source.load_ohlcv(SERIES, "1D", limit=10)
    assert result is expected
    load.assert_called_once_with(SERIES, "1D", None, None, 10)


@pytest.mark.asyncio
async def test_technical_data_source_resolves_the_series_of_a_ticker():
    session = MagicMock()
    session.execute.return_value.first.return_value = (11, 3)
    source = SqlAlchemyTechnicalRepository(
        SimpleNamespace(get_session=MagicMock(return_value=session))
    )
    assert await source.resolve_series(" air.pa ") == MarketSeries(asset_id=11, listing_id=3)
    session.close.assert_called_once()

    unknown_session = MagicMock()
    unknown_session.execute.return_value.first.return_value = None
    unknown_source = SqlAlchemyTechnicalRepository(lambda: unknown_session)
    assert await unknown_source.resolve_series("nope") is None
    unknown_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_profile_enricher_skips_complete_profiles():
    database = SimpleNamespace(asset_profile_needs_enrichment=MagicMock(return_value=False))
    await YFinanceAssetProfileEnricher(database).enrich({"asset_id": 1}, "AAPL")
    database.asset_profile_needs_enrichment.assert_called_once()


@pytest.mark.asyncio
async def test_profile_enricher_tries_candidates_until_metadata_is_useful():
    database = SimpleNamespace(
        asset_profile_needs_enrichment=MagicMock(return_value=True),
        asset_profile_enrichment_tickers=MagicMock(return_value=["BAD", "AAPL"]),
        metadata_has_profile_enrichment=MagicMock(side_effect=[False, True]),
        update_asset_profile_from_metadata=MagicMock(return_value=True),
    )
    with patch(
        "import_assets.fetch_yfinance_data",
        side_effect=[{}, {"longName": "Apple"}],
    ):
        await YFinanceAssetProfileEnricher(database).enrich(
            {"asset_id": 1, "listing_id": 2}, "AAPL"
        )
    database.update_asset_profile_from_metadata.assert_called_once_with(1, {"longName": "Apple"}, 2)


@pytest.mark.asyncio
async def test_profile_enricher_asks_only_the_verified_symbol_when_given_one():
    """The tickers of the profile (``SPFF``) are not tried: they may name another instrument."""
    database = SimpleNamespace(
        asset_profile_needs_enrichment=MagicMock(return_value=True),
        asset_profile_enrichment_tickers=MagicMock(return_value=["SPFF"]),
        metadata_has_profile_enrichment=MagicMock(return_value=True),
        update_asset_profile_from_metadata=MagicMock(return_value=True),
    )
    with patch("import_assets.fetch_yfinance_data", return_value={"longName": "SPDR"}) as fetch:
        await YFinanceAssetProfileEnricher(database).enrich(
            {"asset_id": 3, "listing_id": 5}, "SPFF", symbol="SPFF.DE"
        )

    fetch.assert_called_once_with("SPFF.DE")
    database.asset_profile_enrichment_tickers.assert_not_called()


@pytest.mark.asyncio
async def test_deep_enricher_delegates_to_yfinance_service():
    with patch("financials.enrichment.adapters.YFinanceEnricher") as enricher_type:
        enricher_type.return_value.enrich = AsyncMock(return_value={"ok": True})
        adapter = YFinanceDeepFundamentalsEnricher(MagicMock())
        assert await adapter.enrich(1, "AAPL") == {"ok": True}
        enricher_type.return_value.enrich.assert_awaited_once_with(1, "AAPL")


def _cache(*, cached: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True,
        generate_key=MagicMock(return_value="cache-key"),
        get=MagicMock(return_value=cached),
        set=MagicMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_case",
    [
        GetInsiderTransactions(None),
        GetEtfDetails(None),
        GetIndexConstituents(None, lambda value: value),
    ],
)
async def test_specialized_use_cases_require_provider(use_case):
    with pytest.raises(DependencyUnavailable):
        await use_case.execute("AAPL")


@pytest.mark.asyncio
async def test_insider_transactions_uses_cache_and_persists_result():
    cache = _cache(cached={"cached": True})
    provider = SimpleNamespace(fetch=AsyncMock())
    assert await GetInsiderTransactions(provider, cache).execute("AAPL") == {"cached": True}
    provider.fetch.assert_not_awaited()

    cache.get.return_value = None
    provider.fetch.return_value = DumpModel({"transactions": [1]})
    result = await GetInsiderTransactions(provider, cache).execute("AAPL", refresh=True)
    assert result == {"transactions": [1]}
    cache.set.assert_called_once_with("cache-key", result, cache_type="insider_transactions")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [None, RuntimeError("upstream")])
async def test_insider_transactions_reports_provider_failures(provider_result):
    provider = SimpleNamespace(fetch=AsyncMock())
    if isinstance(provider_result, Exception):
        provider.fetch.side_effect = provider_result
        expected = UpstreamFailure
    else:
        provider.fetch.return_value = provider_result
        expected = DependencyUnavailable
    with pytest.raises(expected):
        await GetInsiderTransactions(provider).execute("AAPL")


@pytest.mark.asyncio
async def test_etf_details_fetches_and_caches_payload():
    cache = _cache()
    provider = SimpleNamespace(fetch=AsyncMock(return_value=DumpModel({"isin": "FR001"})))
    database = SimpleNamespace(
        get_asset_context=MagicMock(return_value={"details": {"quote_type": "ETF"}})
    )
    result = await GetEtfDetails(provider, database, cache).execute("FR001")
    assert result == {"isin": "FR001"}
    provider.fetch.assert_awaited_once_with(isin="FR001")
    cache.set.assert_called_once_with("cache-key", result, cache_type="etf_details")


@pytest.mark.asyncio
async def test_etf_details_reports_missing_and_failed_upstream():
    provider = SimpleNamespace(fetch=AsyncMock(return_value=None))
    with pytest.raises(DependencyUnavailable):
        await GetEtfDetails(provider).execute("FR001")
    provider.fetch.side_effect = RuntimeError("upstream")
    with pytest.raises(UpstreamFailure):
        await GetEtfDetails(provider).execute("FR001")


@pytest.mark.asyncio
async def test_index_constituents_fetches_enum_and_caches_payload():
    cache = _cache()
    provider = SimpleNamespace(fetch=AsyncMock(return_value=DumpModel({"count": 40})))
    enum = MagicMock(side_effect=lambda value: f"enum:{value}")
    result = await GetIndexConstituents(provider, enum, cache).execute("cac40")
    assert result == {"count": 40}
    provider.fetch.assert_awaited_once_with(index_name="enum:CAC40")
    cache.set.assert_called_once_with("cache-key", result, cache_type="index_constituents")


@pytest.mark.asyncio
async def test_index_constituents_reports_missing_enum_and_upstream_failures():
    provider = SimpleNamespace(fetch=AsyncMock(return_value=None))
    with pytest.raises(DependencyUnavailable):
        await GetIndexConstituents(provider, None).execute("CAC40")
    with pytest.raises(DependencyUnavailable):
        await GetIndexConstituents(provider, lambda value: value).execute("CAC40")
    provider.fetch.side_effect = RuntimeError("upstream")
    with pytest.raises(UpstreamFailure):
        await GetIndexConstituents(provider, lambda value: value).execute("CAC40")
