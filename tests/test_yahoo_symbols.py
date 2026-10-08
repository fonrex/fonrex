"""The prices of a listing are fetched with a verified symbol, never a guessed one.

The cases below are real: they are what Yahoo answered for three listings of
the ETF catalogue (``data/etf.csv``), whose tickers are not Yahoo symbols.

- ``SPFF`` (EUR) is a bond UCITS ETF, ISIN ``IE000AQ7A2X6``. Asked for ``SPFF``,
  Yahoo returns a US fund of the same ticker, in USD: its prices were stored
  under the EUR listing. By ISIN, Yahoo offers a dead Stuttgart line first,
  then ``SPFF.DE``.
- ``EUCO`` (EUR), ISIN ``IE00B3T9LM79``, is ``SYBC.DE`` on Yahoo: another ticker.
- ``GOVY`` exists in EUR and in CHF for ISIN ``IE00B3S5XW04``; Yahoo only has
  ``SYBB.DE``, in EUR. The CHF listing has no symbol: it must not be ingested.
"""

import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.price_series import PriceSeries, resolve_price_series, session_timestamp
from historical.ingestion_service import HistoricalIngestionService
from historical.providers import HistoricalMarketDataFetcher
from historical.yahoo_symbols import (
    FOUND_BY_ISIN,
    FOUND_BY_TICKER,
    NOT_FOUND,
    SET_BY_HAND,
    YAHOO_PROVIDER,
    SymbolResolution,
    YahooCandidate,
    YahooLookupError,
    YahooQuote,
    YahooSymbolResolver,
    normalize_currency,
)
from models import Asset, AssetListing, AssetMapping, Base
from routers import assets as assets_router
from routers import historical as historical_router
from routers.dependencies import (
    get_cache_service,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
)

# What Yahoo answers to a search by ISIN (observed on 2026-10-04).
SEARCH = {
    "IE00B3T9LM79": [YahooCandidate("SYBC.DE", "ETF", 20006.0)],
    "IE00B3S5XW04": [YahooCandidate("SYBB.DE", "ETF", 20001.0)],
    "IE000AQ7A2X6": [
        YahooCandidate("IE000AQ7A2X6.SG", "MUTUALFUND", 20003.0),  # dead line, ranked first
        YahooCandidate("SPFF.DE", "ETF", 20002.0),
    ],
}
# Currency and last price of each symbol.
QUOTES = {
    "SYBC.DE": YahooQuote("EUR", 61.2),
    "SYBB.DE": YahooQuote("EUR", 55.4),
    "IE000AQ7A2X6.SG": YahooQuote("EUR", None),
    "SPFF.DE": YahooQuote("EUR", 28.3),
    "SPFF": YahooQuote("USD", 9.1),  # the US homonym
    "AAPL": YahooQuote("USD", 255.0),
    "GOVY.SW": YahooQuote("CHF", 52.0),
    "VOD.L": YahooQuote("GBp", 70.5),
}


class FakeYahoo:
    def __init__(self):
        self.searched: list[str] = []
        self.quoted: list[str] = []
        self.down = False

    def search(self, isin):
        if self.down:
            raise YahooLookupError("Yahoo search failed: offline")
        self.searched.append(isin)
        return list(SEARCH.get(isin, []))

    def quote(self, symbol):
        self.quoted.append(symbol)
        return QUOTES.get(symbol)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add_all(
        [
            Asset(id=1, ticker="EUCO", name="Euro corporate bonds", isin="IE00B3T9LM79"),
            Asset(id=2, ticker="GOVY", name="Euro government bonds", isin="IE00B3S5XW04"),
            Asset(id=3, ticker="SPFF", name="Global aggregate bonds", isin="IE000AQ7A2X6"),
            Asset(id=4, ticker="AAPL", name="Apple"),
            Asset(id=5, ticker="VOD.L", name="Vodafone", isin="GB00BH4HKS39"),
        ]
    )
    session.add_all(
        [
            AssetListing(id=10, asset_id=1, ticker="EUCO", currency="EUR", is_primary=True),
            AssetListing(id=20, asset_id=2, ticker="GOVY", currency="EUR", is_primary=True),
            AssetListing(id=21, asset_id=2, ticker="GOVY", currency="CHF"),
            AssetListing(id=30, asset_id=3, ticker="SPFF", currency="EUR", is_primary=True),
            AssetListing(id=40, asset_id=4, ticker="AAPL", currency="USD", is_primary=True),
            AssetListing(id=50, asset_id=5, ticker="VOD.L", currency="GBP", is_primary=True),
            AssetListing(id=51, asset_id=5, ticker="VOD.L", exchange="LSE", currency="GBX"),
        ]
    )
    # The importer records the ticker of the CSV as "Yahoo symbol", unverified.
    session.add(
        AssetMapping(
            asset_id=3,
            asset_listing_id=30,
            provider_name=YAHOO_PROVIDER,
            provider_ticker="SPFF",
            source="csv_import",
            confidence_score=1.0,
            is_active=True,
        )
    )
    session.commit()
    session.close()
    service = MagicMock()
    service.get_session.side_effect = factory
    service.factory = factory
    yield service
    engine.dispose()


@pytest.fixture
def yahoo():
    return FakeYahoo()


def _mapping(database, listing_id) -> AssetMapping | None:
    session = database.factory()
    try:
        return (
            session.query(AssetMapping)
            .filter_by(asset_listing_id=listing_id, provider_name=YAHOO_PROVIDER)
            .first()
        )
    finally:
        session.close()


class TestSymbolFoundByIsin:
    def test_ticker_of_the_catalogue_is_not_the_yahoo_symbol(self, database, yahoo):
        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(10)

        assert resolution == SymbolResolution("SYBC.DE", origin=FOUND_BY_ISIN)

    def test_homonym_and_dead_line_are_both_avoided(self, database, yahoo):
        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(30)

        assert resolution.symbol == "SPFF.DE"
        assert "SPFF" not in yahoo.quoted  # the bare ticker (the US fund) is never tried
        assert "IE000AQ7A2X6.SG" not in yahoo.quoted  # the line bearing the ticker comes first

    def test_dead_line_is_skipped_when_it_is_tried(self, database, yahoo):
        session = database.factory()
        session.query(AssetListing).filter_by(id=30).update({"ticker": "OTHERNAME"})
        session.commit()
        session.close()

        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(30)

        # No line bears the ticker: tradable lines first, the dead fund line last.
        assert resolution.symbol == "SPFF.DE"

    def test_verified_symbol_replaces_the_unverified_one_of_the_import(self, database, yahoo):
        YahooSymbolResolver(database, yahoo).resolve_sync(30)

        mapping = _mapping(database, 30)
        assert mapping.provider_ticker == "SPFF.DE"
        assert mapping.provider_url == "https://finance.yahoo.com/quote/SPFF.DE"
        assert mapping.source == FOUND_BY_ISIN
        assert mapping.is_active is True
        assert mapping.last_verified_at is not None

    def test_verified_symbol_is_reused_without_asking_yahoo_again(self, database, yahoo):
        resolver = YahooSymbolResolver(database, yahoo)
        resolver.resolve_sync(10)
        yahoo.searched.clear()

        assert resolver.resolve_sync(10).symbol == "SYBC.DE"
        assert yahoo.searched == []

    def test_answer_survives_a_failed_write(self, database, yahoo):
        """Two first requests for one listing: the second write hits the unique key."""
        from sqlalchemy.exc import IntegrityError

        resolver = YahooSymbolResolver(database, yahoo)
        with patch.object(
            resolver, "_remember", side_effect=IntegrityError("insert", {}, Exception("dup"))
        ):
            resolution = resolver.resolve_sync(10)

        assert resolution.found

    def test_unreadable_listing_is_a_lookup_without_answer(self, database, yahoo):
        from sqlalchemy.exc import OperationalError

        resolver = YahooSymbolResolver(database, yahoo)
        with patch.object(
            resolver, "_load", side_effect=OperationalError("select", {}, Exception("down"))
        ):
            resolution = resolver.resolve_sync(10)

        assert not resolution.found
        assert "could not be read" in resolution.reason
        assert yahoo.searched == []

    def test_async_entry_point(self, database, yahoo):
        resolution = asyncio.run(YahooSymbolResolver(database, yahoo).resolve(10))
        assert resolution.found and resolution.symbol == "SYBC.DE"


class TestListingWithoutSymbolInItsCurrency:
    def test_the_same_instrument_in_another_currency_is_not_a_substitute(self, database, yahoo):
        resolver = YahooSymbolResolver(database, yahoo)

        assert resolver.resolve_sync(20).symbol == "SYBB.DE"  # GOVY in EUR
        refused = resolver.resolve_sync(21)  # GOVY in CHF

        assert not refused.found
        assert refused.origin == NOT_FOUND
        assert "CHF" in refused.reason and "SYBB.DE (EUR)" in refused.reason

    def test_failure_is_remembered_then_retried_after_a_day(self, database, yahoo):
        clock = SimpleNamespace(now=datetime(2026, 10, 4, 18, 0, tzinfo=UTC))
        resolver = YahooSymbolResolver(database, yahoo, now=lambda: clock.now)
        resolver.resolve_sync(21)
        mapping = _mapping(database, 21)
        assert (mapping.source, mapping.is_active, mapping.failure_count) == (NOT_FOUND, False, 1)

        yahoo.searched.clear()
        again = resolver.resolve_sync(21)
        assert not again.found and "checked on 2026-10-04" in again.reason
        assert yahoo.searched == []  # not asked again within the day

        clock.now += timedelta(hours=25)
        resolver.resolve_sync(21)
        assert yahoo.searched == ["IE00B3S5XW04"]
        assert _mapping(database, 21).failure_count == 2

    def test_refresh_asks_again_at_once(self, database, yahoo):
        resolver = YahooSymbolResolver(database, yahoo)
        resolver.resolve_sync(21)
        yahoo.searched.clear()

        resolver.resolve_sync(21, refresh=True)

        assert yahoo.searched == ["IE00B3S5XW04"]

    def test_unreachable_yahoo_concludes_nothing(self, database, yahoo):
        yahoo.down = True

        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(10)

        assert not resolution.found and "offline" in resolution.reason
        assert _mapping(database, 10) is None  # no failure recorded: it will be tried again

    def test_pence_are_not_pounds(self, database, yahoo):
        resolver = YahooSymbolResolver(database, yahoo)

        assert not resolver.resolve_sync(50).found  # listing in GBP, Yahoo quotes pence
        assert resolver.resolve_sync(51).symbol == "VOD.L"  # listing in GBX
        assert normalize_currency("GBp") == "GBX"
        assert normalize_currency(" eur ") == "EUR"
        assert normalize_currency("") is None

    def test_listing_without_currency_or_unknown_listing(self, database, yahoo):
        session = database.factory()
        session.query(AssetListing).filter_by(id=10).update({"currency": ""})
        session.commit()
        session.close()
        resolver = YahooSymbolResolver(database, yahoo)

        assert "no currency" in resolver.resolve_sync(10).reason
        assert "does not exist" in resolver.resolve_sync(999).reason


class TestSymbolTakenFromTheCatalogue:
    def test_ticker_without_suffix_is_accepted_for_an_instrument_without_isin(
        self, database, yahoo
    ):
        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(40)

        assert resolution == SymbolResolution("AAPL", origin=FOUND_BY_TICKER)
        assert _mapping(database, 40).confidence_score == 0.8

    def test_ticker_that_names_its_exchange_is_tried_when_the_isin_gives_nothing(
        self, database, yahoo
    ):
        session = database.factory()
        session.query(AssetListing).filter_by(id=21).update({"ticker": "GOVY.SW"})
        session.commit()
        session.close()

        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(21)

        assert resolution == SymbolResolution("GOVY.SW", origin=FOUND_BY_TICKER)

    def test_symbol_set_by_hand_is_trusted(self, database, yahoo):
        session = database.factory()
        session.add(
            AssetMapping(
                asset_id=2,
                asset_listing_id=21,
                provider_name=YAHOO_PROVIDER,
                provider_ticker="GOVY.SW",
                source=SET_BY_HAND,
                is_active=True,
            )
        )
        session.commit()
        session.close()

        resolution = YahooSymbolResolver(database, yahoo).resolve_sync(21)

        assert resolution.symbol == "GOVY.SW"
        assert yahoo.searched == [] and yahoo.quoted == []


def _fast_info(closes, currency=None, market_price=None, metadata=None):
    """The real ``FastInfo`` of yfinance, fed with a canned history instead of Yahoo."""
    from yfinance.scrapers.quote import FastInfo

    if metadata is None:
        metadata = {
            "currency": currency,
            "exchangeTimezoneName": "Europe/Berlin",
            "currentTradingPeriod": {"regular": {"start": 1790924400, "end": 1790955000}},
        }
        if market_price is not None:
            metadata["regularMarketPrice"] = market_price

    class CannedTicker:
        def history(self, *args, **kwargs):
            index = pd.date_range(
                end=pd.Timestamp.now("UTC").tz_convert("Europe/Berlin").normalize(),
                periods=len(closes),
                freq="D",
            )
            return pd.DataFrame({"Close": closes}, index=index)

        def get_history_metadata(self):
            return metadata

    return FastInfo(CannedTicker())


class TestYahooLookupAdapter:
    def test_search_maps_the_quotes_of_yahoo(self, yahoo_adapter):
        quotes = [
            {"symbol": "SPFF.DE", "quoteType": "ETF", "score": 20002.0},
            {"symbol": "IE000AQ7A2X6.SG", "quoteType": "MUTUALFUND"},
            {"shortname": "no symbol"},
        ]
        with patch("historical.yahoo_symbols.yf.Search") as search:
            search.return_value.quotes = quotes
            candidates = yahoo_adapter.search("IE000AQ7A2X6")

        assert candidates == [
            YahooCandidate("SPFF.DE", "ETF", 20002.0),
            YahooCandidate("IE000AQ7A2X6.SG", "MUTUALFUND", 0.0),
        ]

    def test_search_failure_is_a_lookup_error(self, yahoo_adapter):
        with patch("historical.yahoo_symbols.yf.Search", side_effect=RuntimeError("429")):
            with pytest.raises(YahooLookupError, match="429"):
                yahoo_adapter.search("IE000AQ7A2X6")

    def test_quote_reads_currency_and_last_price(self, yahoo_adapter):
        """Read through the ``fast_info`` object of the installed yfinance.

        A plain dict in its place hid a real failure: ``fast_info.get`` knows only
        the names it publishes and answered ``None`` to ``get("last_price")``, so
        every symbol was reported without a price and none was ever verified.
        """
        with patch("historical.yahoo_symbols.yf.Ticker") as ticker:
            ticker.return_value.fast_info = _fast_info(closes=[28.1, 28.3], currency="EUR")
            assert yahoo_adapter.quote("SPFF.DE") == YahooQuote("EUR", 28.3)

    def test_quote_of_a_line_without_recent_bar_uses_the_price_of_the_exchange(
        self, yahoo_adapter
    ):
        with patch("historical.yahoo_symbols.yf.Ticker") as ticker:
            ticker.return_value.fast_info = _fast_info(closes=[], currency="CHF", market_price=50.5)
            assert yahoo_adapter.quote("GOVY.SW") == YahooQuote("CHF", 50.5)

    def test_symbol_without_data_has_no_quote(self, yahoo_adapter):
        with patch("historical.yahoo_symbols.yf.Ticker") as ticker:
            ticker.return_value.fast_info = _fast_info(closes=[], metadata={})
            assert yahoo_adapter.quote("DEAD.SG") is None

    def test_line_without_any_price_is_not_valid(self, yahoo_adapter):
        with patch("historical.yahoo_symbols.yf.Ticker") as ticker:
            ticker.return_value.fast_info = _fast_info(closes=[], currency="EUR")
            assert yahoo_adapter.quote("IE000AQ7A2X6.SG") == YahooQuote("EUR", None)

    def test_quote_failure_is_a_lookup_error(self, yahoo_adapter):
        with patch("historical.yahoo_symbols.yf.Ticker", side_effect=ConnectionError("offline")):
            with pytest.raises(YahooLookupError, match="offline"):
                yahoo_adapter.quote("SPFF.DE")


def _bars(symbol_source: str):
    return [
        {
            "timestamp": session_timestamp(date(2026, 10, 2)),
            "open": 28.0,
            "high": 28.5,
            "low": 27.9,
            "close": 28.3,
            "volume": 1000,
            "source": symbol_source,
        }
    ]


class TestIngestionUsesTheVerifiedSymbol:
    @pytest.fixture
    def service(self, database, yahoo):
        fetcher = HistoricalMarketDataFetcher()
        fetcher.fetch_yfinance = AsyncMock(
            return_value={"bars": _bars("yfinance"), "source_used": "yfinance", "symbol": "SPFF.DE"}
        )
        fetcher.fetch_tradingview = AsyncMock(
            return_value={"bars": [], "source_used": "tradingview", "error": "no line in CHF"}
        )
        service = HistoricalIngestionService(
            db_service=database,
            query_service=MagicMock(),
            market_data_fetcher=fetcher,
            symbol_resolver=YahooSymbolResolver(database, yahoo),
        )
        service._detect_gaps = AsyncMock(
            return_value=(date(2026, 9, 21), date(2026, 10, 4), False)
        )
        service._log_ingest = AsyncMock()
        service.yf_delay = 0
        service.fetcher = fetcher
        return service

    def test_yahoo_is_queried_with_the_symbol_of_the_listing_not_its_ticker(self, service):
        result = asyncio.run(service.ingest("SPFF"))

        assert result.status == "success"
        assert result.provider_symbol == "SPFF.DE"
        assert service.fetcher.fetch_yfinance.await_args.args[0] == "SPFF.DE"

    def test_listing_is_chosen_by_currency(self, service):
        asyncio.run(service.ingest("GOVY", currency="eur"))
        assert service.fetcher.fetch_yfinance.await_args.args[0] == "SYBB.DE"

        gap_arguments = service._detect_gaps.await_args.args
        assert gap_arguments[-2:] == ("eur", None)

    def test_listing_without_symbol_is_not_fetched_from_yahoo(self, service):
        result = asyncio.run(service.ingest("GOVY", currency="CHF", source="yfinance"))

        assert result.status == "failed"
        assert "No Yahoo symbol quoted in CHF" in result.error
        service.fetcher.fetch_yfinance.assert_not_awaited()

    def test_fallback_accepts_only_a_line_in_the_currency_of_the_listing(self, service):
        result = asyncio.run(service.ingest("GOVY", currency="CHF"))

        assert result.status == "failed"
        # The reason Yahoo was not used, then what TradingView answered.
        assert "No Yahoo symbol quoted in CHF" in result.error
        assert "no line in CHF" in result.error
        service.fetcher.fetch_yfinance.assert_not_awaited()
        assert service.fetcher.fetch_tradingview.await_args.args == (
            "GOVY",
            "1D",
            date(2026, 9, 21),
            date(2026, 10, 4),
            "CHF",
        )

    def test_fallback_after_an_empty_answer_of_yahoo(self, service):
        service.fetcher.fetch_yfinance.return_value = {"bars": [], "source_used": "yfinance"}
        service.fetcher.fetch_tradingview.return_value = {
            "bars": _bars("tradingview"),
            "source_used": "tradingview",
            "symbol": "XETR:SYBC",
        }

        result = asyncio.run(service.ingest("EUCO"))

        assert (result.status, result.source_used, result.provider_symbol) == (
            "success",
            "tradingview",
            "XETR:SYBC",
        )
        # TradingView is searched with the ticker of the verified line, without suffix.
        assert service.fetcher.fetch_tradingview.await_args.args[0] == "SYBC"
        assert result.note == "Yahoo has no bar for SYBC.DE"

    def test_success_of_the_second_source_says_why_yahoo_was_not_used(self, service):
        """A listing served by TradingView alone must not look like a plain success."""
        service.fetcher.fetch_tradingview.return_value = {
            "bars": _bars("tradingview"),
            "source_used": "tradingview",
            "symbol": "SIX:GOVY",
        }

        result = asyncio.run(service.ingest("GOVY", currency="CHF"))

        assert (result.status, result.provider_symbol) == ("success", "SIX:GOVY")
        assert "No Yahoo symbol quoted in CHF" in result.note
        assert result.error is None

    def test_success_of_yahoo_has_no_note(self, service):
        assert asyncio.run(service.ingest("SPFF")).note is None

    def test_explicit_tradingview_source(self, service):
        asyncio.run(service.ingest("XETR:SPFF", source="tradingview"))
        asyncio.run(service.ingest("SPFF", source="tradingview"))

        service.fetcher.fetch_yfinance.assert_not_awaited()
        assert service.fetcher.fetch_tradingview.await_args.args[0] == "SPFF"
        assert service.fetcher.fetch_tradingview.await_args.args[4] == "EUR"


class TestTradingViewLineIsCheckedByCurrency:
    def _screener(self, rows):
        module = ModuleType("tradingview_scraper.symbols.screener")
        screener = MagicMock()
        screener.return_value.screen.return_value = {"status": "success", "data": rows}
        module.Screener = screener
        return patch.dict(
            sys.modules,
            {
                "tradingview_scraper": ModuleType("tradingview_scraper"),
                "tradingview_scraper.symbols": ModuleType("tradingview_scraper.symbols"),
                "tradingview_scraper.symbols.screener": module,
            },
        )

    ROWS = [
        {"symbol": "AMEX:SPFF", "currency": "USD"},
        {"symbol": "XETR:SPFF", "currency": "EUR"},
        {"symbol": "OTC:SPFF"},
    ]

    def test_line_in_the_currency_of_the_listing_is_chosen(self):
        with self._screener(self.ROWS):
            resolve = HistoricalMarketDataFetcher._resolve_tradingview_symbol
            assert resolve("SPFF", "EUR") == "XETR:SPFF"
            assert resolve("SPFF", "usd") == "AMEX:SPFF"
            assert resolve("SPFF", "CHF") is None

    def test_without_currency_the_first_line_is_taken_as_before(self):
        with self._screener(self.ROWS):
            assert HistoricalMarketDataFetcher._resolve_tradingview_symbol("SPFF") == "AMEX:SPFF"

    def test_explicit_symbol_is_kept(self):
        assert HistoricalMarketDataFetcher._resolve_tradingview_symbol("XETR:SPFF", "EUR") == (
            "XETR:SPFF"
        )

    def test_no_line_means_no_bar_and_a_reason(self):
        fetcher = HistoricalMarketDataFetcher()
        with self._screener([{"symbol": "AMEX:SPFF", "currency": "USD"}]):
            result = fetcher._fetch_tradingview_sync(
                "SPFF", "1D", date(2026, 9, 1), date(2026, 10, 1), "EUR"
            )
        assert result["bars"] == []
        assert result["error"] == "No TradingView symbol found for SPFF quoted in EUR"


class TestListingChosenInTheRequest:
    def test_currency_or_exchange_names_one_listing_among_those_sharing_a_ticker(self, database):
        session = database.factory()
        assert resolve_price_series(session, "GOVY") == PriceSeries(2, 20)  # primary listing
        assert resolve_price_series(session, "GOVY", currency="chf") == PriceSeries(2, 21)
        assert resolve_price_series(session, "GOVY", currency="USD") is None
        assert resolve_price_series(session, "VOD.L", exchange="lse") == PriceSeries(5, 51)
        session.close()

    @pytest.fixture
    def client(self):
        application = FastAPI()
        application.include_router(assets_router.router)
        application.include_router(historical_router.router)
        query = MagicMock()
        query.get_history = AsyncMock(return_value=[])
        ingestion = MagicMock()
        ingestion.ingest = AsyncMock(
            return_value=SimpleNamespace(
                status="failed", error="No Yahoo symbol quoted in CHF for ISIN IE00B3S5XW04"
            )
        )
        application.dependency_overrides[get_query_service] = lambda: query
        application.dependency_overrides[get_ingestion_service] = lambda: ingestion
        application.dependency_overrides[get_cache_service] = lambda: None
        application.dependency_overrides[get_redis_client] = lambda: None
        client = TestClient(application)
        client.query, client.ingestion = query, ingestion
        return client

    def test_eod_passes_the_choice_and_explains_a_missing_series(self, client):
        response = client.get("/eod/GOVY?period=1mo&currency=CHF")

        assert response.status_code == 404
        assert response.json()["reason"] == "No Yahoo symbol quoted in CHF for ISIN IE00B3S5XW04"
        assert client.query.get_history.await_args.kwargs["currency"] == "CHF"
        assert client.ingestion.ingest.await_args.kwargs["currency"] == "CHF"

    def test_history_passes_the_choice(self, client):
        response = client.get("/ticker/GOVY/history?currency=CHF&exchange=SIX")

        assert response.status_code == 200
        assert client.query.get_history.await_args.kwargs == {"currency": "CHF", "exchange": "SIX"}

    def test_manual_ingestion_passes_the_choice(self, client):
        client.ingestion.ingest.return_value = {
            "ticker": "GOVY",
            "resolution": "1D",
            "status": "failed",
            "error": "No Yahoo symbol quoted in CHF",
        }

        response = client.post("/historical/ingest?ticker=GOVY&currency=CHF&force_refresh=true")

        assert response.status_code == 200
        arguments = client.ingestion.ingest.await_args.kwargs
        assert (arguments["currency"], arguments["force_refresh"]) == ("CHF", True)


class TestNoTestReachesYahoo:
    def test_default_lookup_is_refused_in_the_test_suite(self, database):
        resolver = YahooSymbolResolver(database)  # no lookup injected: the real one

        with pytest.raises(AssertionError, match="reached Yahoo"):
            resolver.resolve_sync(10)
