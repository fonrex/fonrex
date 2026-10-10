"""A cached answer is served only to the request it answers.

Each cache key must hold every parameter that changes the answer. A parameter
left out of the key made a request receive the answer of another one until the
entry expired: the fundamentals of one provider for a request for all of them,
French news for a request in English, 500 points of an indicator for a request
for 50.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from cache.service import CacheService
from cache.technical import RedisTechnicalCache
from news.news_service import NewsService
from technical.contracts import MarketSeries
from technical.indicator_service import TechnicalIndicatorService
from use_cases.fundamentals import GetDeepFundamentals, GetFundamentals
from use_cases.specialized import GetInsiderTransactions


class AsyncStore:
    """The part of the asynchronous Redis client the caches use."""

    def __init__(self):
        self.entries: dict[str, str] = {}

    async def get(self, key):
        return self.entries.get(key)

    async def setex(self, key, ttl, value):
        self.entries[key] = value


class SyncCache(CacheService):
    """``CacheService`` on a dictionary: real keys, real JSON, no server."""

    def _connect(self):
        store: dict[str, bytes] = {}
        self.client = SimpleNamespace(
            get=store.get, set=lambda key, value, ex=None: store.__setitem__(key, value)
        )
        self.enabled = True


# ── /fundamental ─────────────────────────────────────────────────────────────


def _fundamentals(store, runner):
    return GetFundamentals(redis=store, provider_runner=runner)


@pytest.mark.asyncio
async def test_fundamentals_of_one_provider_are_not_served_to_a_request_for_all():
    store = AsyncStore()
    runner = SimpleNamespace(
        run=AsyncMock(
            side_effect=lambda **kwargs: (
                {name: {"ticker": "AAPL"} for name in (kwargs["provider_params"] or ["A", "B"])},
                {},
            )
        )
    )

    one = await _fundamentals(store, runner).execute(ticker="AAPL", provider="A", fmt="raw")
    everything = await _fundamentals(store, runner).execute(ticker="AAPL", fmt="raw")

    assert set(one.data) >= {"A"} and "B" not in one.data
    assert {"A", "B"} <= set(everything.data)
    assert everything.cache_hit is False
    assert runner.run.await_count == 2


@pytest.mark.asyncio
async def test_same_providers_in_another_order_or_case_share_one_entry():
    store = AsyncStore()
    runner = SimpleNamespace(run=AsyncMock(return_value=({"A": {}, "B": {}}, {})))

    await _fundamentals(store, runner).execute(ticker="AAPL", provider="A,B", fmt="raw")
    again = await _fundamentals(store, runner).execute(ticker="AAPL", provider="b, a", fmt="raw")

    assert again.cache_hit is True
    assert runner.run.await_count == 1


# ── /fundamental/deep ────────────────────────────────────────────────────────

AAPL = {"asset_id": 42, "ticker": "AAPL", "isin": "US0378331005", "name": "Apple"}
EVERY_SECTION = {
    "highlights": {"market_cap": 1},
    "statements": {"income": {"annual": []}},
    "earnings_history": [{"period": "2026Q2"}],
    "analyst_ratings": {"consensus": "buy"},
}


def _deep(cache, enricher):
    database = SimpleNamespace(
        get_asset_context=MagicMock(return_value={"details": dict(AAPL)}),
        get_deep_sections=MagicMock(return_value=dict(EVERY_SECTION)),
    )
    return GetDeepFundamentals(database, cache, enricher=enricher)


@pytest.mark.asyncio
async def test_deep_answer_cached_for_some_sections_serves_the_others_too():
    cache = SyncCache()
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={}))

    first = await _deep(cache, enricher).execute(ticker="AAPL", sections="highlights")
    second = await _deep(cache, enricher).execute(ticker="AAPL", sections="statements,ratings")
    everything = await _deep(cache, enricher).execute(ticker="AAPL")

    assert "highlights" in first and "statements" not in first
    assert {"statements", "analyst_ratings"} <= set(second)
    assert "highlights" not in second and "earnings_history" not in second
    assert set(EVERY_SECTION) <= set(everything)
    # One fetch from Yahoo for the three requests: the entry holds every section.
    assert enricher.enrich.await_count == 1
    assert second["meta"]["cache_hit"] is True
    assert second["asset_profile"]["ticker"] == "AAPL"



@pytest.mark.asyncio
async def test_deep_answers_of_two_instruments_sharing_a_ticker_are_kept_apart():
    """``AIR.PA`` (Airbus) and ``AIR`` (AAR Corp) both resolve to the catalogue ticker AIR."""
    cache = SyncCache()
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={}))
    instruments = {
        "AIR": {"asset_id": 1, "ticker": "AIR", "isin": "US0003611052", "name": "AAR Corp"},
        "AIR.PA": {"asset_id": 2, "ticker": "AIR", "isin": "NL0000235190", "name": "Airbus SE"},
    }

    def deep(ticker):
        database = SimpleNamespace(
            get_asset_context=MagicMock(return_value={"details": dict(instruments[ticker])}),
            get_deep_sections=MagicMock(return_value=dict(EVERY_SECTION)),
        )
        return GetDeepFundamentals(database, cache, enricher=enricher).execute(ticker=ticker)

    await deep("AIR.PA")
    aar = await deep("AIR")

    assert aar["asset_profile"]["name"] == "AAR Corp"
    assert aar["meta"]["cache_hit"] is False
    assert enricher.enrich.await_count == 2

# ── /insider-transactions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insider_transactions_are_cached_per_number_of_filings_asked():
    cache = SyncCache()
    provider = SimpleNamespace(
        fetch=AsyncMock(
            side_effect=lambda ticker, limit: SimpleNamespace(
                model_dump=lambda mode: {"ticker": ticker, "transactions": list(range(limit))}
            )
        )
    )

    few = await GetInsiderTransactions(provider, cache).execute("AAPL", limit=5)
    many = await GetInsiderTransactions(provider, cache).execute("AAPL", limit=50)
    few_again = await GetInsiderTransactions(provider, cache).execute("AAPL", limit=5)

    assert (len(few["transactions"]), len(many["transactions"])) == (5, 50)
    assert few_again == few
    assert provider.fetch.await_count == 2


# ── /news/{ticker} ───────────────────────────────────────────────────────────


def _article(language):
    return SimpleNamespace(
        title=f"Title in {language}",
        url=f"https://example.test/{language}",
        summary=None,
        image_url=None,
        source=None,
        provider="fake",
        author=None,
        published_at=None,
        related_tickers=[],
        language=language,
    )


@pytest.mark.asyncio
async def test_news_in_one_language_are_not_served_to_a_request_for_another():
    store = AsyncStore()
    service = NewsService(db_session=MagicMock(), redis_client=store)
    service._resolve_asset = AsyncMock(return_value=None)
    service._fetch_from_all_providers = AsyncMock(
        side_effect=lambda *args: [_article("fr"), _article("en")]
    )

    french = await service.get_news("AIR.PA", language="fr")
    english = await service.get_news("AIR.PA", language="en")
    both = await service.get_news("AIR.PA")
    french_again = await service.get_news("AIR.PA", language="fr")

    assert [article.language for article in french.articles] == ["fr"]
    assert [article.language for article in english.articles] == ["en"]
    assert {article.language for article in both.articles} == {"fr", "en"}
    assert french_again.cached is True
    assert [article.language for article in french_again.articles] == ["fr"]
    assert service._fetch_from_all_providers.await_count == 3


@pytest.mark.asyncio
async def test_news_all_language_request_is_cached_as_unfiltered():
    store = AsyncStore()
    service = NewsService(db_session=MagicMock(), redis_client=store)
    service._resolve_asset = AsyncMock(return_value=None)
    service._fetch_from_all_providers = AsyncMock(
        side_effect=lambda *args: [_article("fr"), _article("en")]
    )

    all_str = await service.get_news("AIR.PA", language="all")
    none_str = await service.get_news("AIR.PA", language=None)

    assert {article.language for article in all_str.articles} == {"fr", "en"}
    assert none_str.cached is True
    assert {article.language for article in none_str.articles} == {"fr", "en"}
    assert service._fetch_from_all_providers.await_count == 1


# ── technical indicators ─────────────────────────────────────────────────────

SERIES = MarketSeries(asset_id=1, listing_id=7)


def _bars(count):
    index = pd.date_range("2024-01-01", periods=count, freq="D", tz="UTC")
    close = 100 + np.sin(np.arange(count) / 5) * 10
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000},
        index=index,
    )


@pytest.mark.asyncio
async def test_indicator_computed_on_a_window_is_not_served_for_another_window(monkeypatch):
    monkeypatch.setenv("TECHNICAL_CACHE_ENABLED", "true")
    service = TechnicalIndicatorService(MagicMock(), cache=RedisTechnicalCache(AsyncStore()))
    load = AsyncMock(side_effect=lambda series, resolution, from_date, to_date, limit: _bars(limit))

    with (
        patch.object(service, "_resolve_series", AsyncMock(return_value=SERIES)),
        patch.object(service, "_load_ohlcv_dataframe", load),
    ):
        long = await service.calculate("AIR.PA", "sma", limit=200)
        await asyncio.sleep(0)  # the cache write is a background task
        short = await service.calculate("AIR.PA", "sma", limit=50)
        await asyncio.sleep(0)
        short_again = await service.calculate("AIR.PA", "sma", limit=50)

    assert len(long.series[0].values) == 200
    assert len(short.series[0].values) == 50
    assert short.cached is False
    assert short_again.cached is True
    assert len(short_again.series[0].values) == 50


def test_keys_hold_every_parameter_that_changes_the_answer():
    service = TechnicalIndicatorService(None)

    assert service._cache_key("air.pa", "rsi", {"length": 14}, "1D", limit=50) != service._cache_key(
        "air.pa", "rsi", {"length": 14}, "1D", limit=500
    )
    # The prefix the ingestion drops after new prices is unchanged.
    assert service._cache_key("air.pa", "rsi", {"length": 14}, "1D", limit=50, series=MarketSeries(1, 7)).startswith(
        "technical:AIR.PA:7"
    )

@pytest.mark.asyncio
async def test_indicators_for_different_series_of_same_ticker_do_not_collide(monkeypatch):
    monkeypatch.setenv("TECHNICAL_CACHE_ENABLED", "true")
    service = TechnicalIndicatorService(MagicMock(), cache=RedisTechnicalCache(AsyncStore()))
    load = AsyncMock(side_effect=lambda series, resolution, from_date, to_date, limit: _bars(limit))

    # Test that when passing two different series with the same ticker, the cache key distinguishes them.
    with patch.object(service, "_load_ohlcv_dataframe", load):
        first = await service.calculate("AIR.PA", "sma", limit=50, series=MarketSeries(asset_id=1, listing_id=7))
        await asyncio.sleep(0)
        second = await service.calculate("AIR.PA", "sma", limit=50, series=MarketSeries(asset_id=1, listing_id=8))
        await asyncio.sleep(0)

    assert first.cached is False
    assert second.cached is False
    assert load.await_count == 2



@pytest.mark.asyncio
async def test_indicators_cached_by_a_multi_request_belong_to_its_series(monkeypatch):
    """calculate_multi() writes the same keys as calculate(): they hold the series too."""
    monkeypatch.setenv("TECHNICAL_CACHE_ENABLED", "true")
    service = TechnicalIndicatorService(MagicMock(), cache=RedisTechnicalCache(AsyncStore()))
    load = AsyncMock(side_effect=lambda series, resolution, from_date, to_date, limit: _bars(limit))
    paris, other_listing = MarketSeries(asset_id=1, listing_id=7), MarketSeries(asset_id=1, listing_id=8)

    with patch.object(service, "_load_ohlcv_dataframe", load):
        await service.calculate_multi("AIR.PA", ["sma_20"], limit=50, series=paris)
        await asyncio.sleep(0)  # the cache writes are background tasks
        same_series = await service.calculate("AIR.PA", "sma", period=20, limit=50, series=paris)
        other_series = await service.calculate(
            "AIR.PA", "sma", period=20, limit=50, series=other_listing
        )

    assert same_series.cached is True
    assert other_series.cached is False
