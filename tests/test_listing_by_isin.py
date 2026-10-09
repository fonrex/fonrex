"""The ISIN names the instrument when several instruments share a ticker.

A bare ticker is not unique: ``NEM`` is Newmont and Nemetschek. Here Newmont is
quoted in USD and in AUD, Nemetschek in EUR. The currency tells two listings of one instrument apart,
not two instruments. ``isin`` keeps the listings of one instrument only, and the
price answers say which listing they read, so that a client can check it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cache.service import CacheService
from database.price_series import PriceSeries, listing_identity, resolve_price_series
from database.query import QueryService
from historical.ingestion_service import HistoricalIngestionService
from models import Asset, AssetListing, Base
from routers import assets as assets_router
from routers import historical as historical_router
from routers.dependencies import (
    get_cache_service,
    get_ingestion_service,
    get_query_service,
    get_redis_client,
)

NEWMONT = "US6516391066"
NEMETSCHEK = "DE0006452907"
MERCK_KGAA = "DE0006599905"


@pytest.fixture
def catalogue():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add_all(
        [
            Asset(id=1, ticker="NEM", name="Newmont", isin=NEWMONT, is_active=True),
            Asset(id=2, ticker="NEM", name="Nemetschek", isin=NEMETSCHEK, is_active=True),
            Asset(id=3, ticker="MRK", name="Merck KGaA", isin=MERCK_KGAA, is_active=True),
        ]
    )
    session.add_all(
        [
            # As in data/stocks.csv: bare tickers, no exchange.
            AssetListing(id=10, asset_id=1, ticker="NEM", currency="USD", is_primary=True),
            AssetListing(id=11, asset_id=1, ticker="NEM", currency="AUD"),
            AssetListing(id=20, asset_id=2, ticker="NEM", currency="EUR", is_primary=True),
            AssetListing(id=30, asset_id=3, ticker="MRK", currency="EUR", is_primary=True),
        ]
    )
    session.commit()
    session.close()
    yield factory
    engine.dispose()


class TestResolution:
    def test_without_isin_a_shared_ticker_may_designate_another_instrument(self, catalogue):
        # Both primary: the currency in alphabetical order decides (EUR before USD).
        assert resolve_price_series(catalogue(), "NEM") == PriceSeries(2, 20)

    def test_isin_keeps_the_listings_of_one_instrument(self, catalogue):
        session = catalogue()
        assert resolve_price_series(session, "NEM", isin=NEWMONT) == PriceSeries(1, 10)
        assert resolve_price_series(session, "NEM", isin=NEMETSCHEK) == PriceSeries(2, 20)

    def test_isin_and_currency_name_one_listing(self, catalogue):
        session = catalogue()
        assert resolve_price_series(session, "NEM", isin=NEWMONT, currency="aud") == (
            PriceSeries(1, 11)
        )
        assert resolve_price_series(session, "NEM", isin=NEMETSCHEK, currency="USD") is None

    def test_isin_is_read_whatever_its_case(self, catalogue):
        assert resolve_price_series(catalogue(), "nem", isin=" us6516391066 ") == (
            PriceSeries(1, 10)
        )

    def test_isin_of_an_instrument_without_that_ticker_designates_nothing(self, catalogue):
        session = catalogue()
        # Never another instrument, nor another ticker of the named one.
        assert resolve_price_series(session, "MRK", isin=NEWMONT) is None
        assert resolve_price_series(session, "UNKNOWN", isin=NEWMONT) is None

    def test_suffix_fallback_stays_within_the_named_instrument(self, catalogue):
        session = catalogue()
        # MRK.DE is not in the catalogue: the bare MRK is Merck KGaA, not another instrument.
        assert resolve_price_series(session, "MRK.DE", isin=MERCK_KGAA) == PriceSeries(3, 30)
        assert resolve_price_series(session, "MRK.DE", isin=NEWMONT) is None

    def test_listing_identity_gives_ticker_isin_currency_and_exchange(self, catalogue):
        row = catalogue().execute(listing_identity(11)).one()
        # A listing without exchange stores an empty string; the reader answers null.
        assert (row.ticker, row.isin, row.currency, row.exchange) == ("NEM", NEWMONT, "AUD", "")


class _AsyncSession:
    """Run the statements of the async reader on a synchronous SQLite session."""

    def __init__(self, factory):
        self._session = factory()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._session.close()

    async def execute(self, statement, params=None):
        return self._session.execute(statement, params or {})


class TestListingIdentityOfTheReader:
    def test_reader_returns_the_listing_the_ticker_designates(self, catalogue):
        service = QueryService(session_factory=lambda: _AsyncSession(catalogue))

        listing = asyncio.run(service.get_listing("NEM", isin=NEWMONT))
        missing = asyncio.run(service.get_listing("NEM", isin=NEMETSCHEK, currency="USD"))

        assert listing == {"ticker": "NEM", "isin": NEWMONT, "currency": "USD", "exchange": None}
        assert missing is None


class TestIngestion:
    @pytest.fixture
    def ingestion(self, catalogue):
        database = MagicMock()
        database.get_session.side_effect = catalogue
        return HistoricalIngestionService(
            db_service=database, query_service=MagicMock(), symbol_resolver=MagicMock()
        )

    def test_isin_reaches_the_choice_of_the_listing_and_the_gap_detection(self, ingestion):
        ingestion._detect_gaps = AsyncMock(return_value=(None, None, True))
        ingestion._log_ingest = AsyncMock()

        result = asyncio.run(ingestion.ingest("NEM", isin=NEMETSCHEK))

        assert result.status == "up_to_date"
        assert ingestion._detect_gaps.await_args.args[-3:] == (None, None, NEMETSCHEK)
        logged = ingestion._log_ingest.await_args.kwargs
        assert logged["asset_id"] == 2

    def test_missing_listing_names_the_choice_in_the_error(self, ingestion):
        result = asyncio.run(ingestion.ingest("NEM", isin=NEMETSCHEK, currency="USD"))

        assert result.status == "failed"
        assert result.error == (
            f"No listing found for ticker NEM (ISIN {NEMETSCHEK}, currency USD): "
            "prices are stored per listing"
        )


LISTING = {"ticker": "NEM", "isin": NEWMONT, "currency": "USD", "exchange": None}
ROW = {
    "time": "2022-12-30T00:00:00+00:00",
    "open": 1.0,
    "high": 1.0,
    "low": 1.0,
    "close": 1.0,
    "adj_close": 1.0,
    "volume": 10,
}


class TestRoutes:
    @pytest.fixture
    def client(self):
        application = FastAPI()
        application.include_router(assets_router.router)
        application.include_router(historical_router.router)
        query = MagicMock()
        query.get_history = AsyncMock(return_value=[ROW])
        query.get_listing = AsyncMock(return_value=LISTING)
        ingestion = MagicMock()
        ingestion.ingest = AsyncMock(
            return_value=SimpleNamespace(status="failed", error="no data", source_used=None)
        )
        application.dependency_overrides[get_query_service] = lambda: query
        application.dependency_overrides[get_ingestion_service] = lambda: ingestion
        application.dependency_overrides[get_cache_service] = lambda: None
        application.dependency_overrides[get_redis_client] = lambda: None
        client = TestClient(application)
        client.query, client.ingestion = query, ingestion
        return client

    def test_eod_reads_the_listing_of_the_isin_and_says_which_one(self, client):
        response = client.get(
            f"/eod/NEM?from=2020-01-01&to=2022-12-31&currency=USD&isin={NEWMONT.lower()}"
        )

        assert response.status_code == 200
        assert response.json()["listing"] == LISTING
        assert client.query.get_history.await_args.kwargs["isin"] == NEWMONT
        assert client.query.get_listing.await_args.args == ("NEM", "USD", None, NEWMONT)

    def test_eod_ingests_the_listing_of_the_isin(self, client):
        client.query.get_history.return_value = []

        response = client.get(f"/eod/NEM?period=1y&isin={NEMETSCHEK}")

        assert response.status_code == 404
        assert client.ingestion.ingest.await_args.kwargs["isin"] == NEMETSCHEK

    @pytest.mark.parametrize("isin", ["US651639106", "1S6516391066", "", "US65163910661"])
    def test_eod_refuses_a_malformed_isin(self, client, isin):
        response = client.get(f"/eod/NEM?period=1y&isin={isin}")

        assert response.status_code == 400
        assert "ISIN" in response.json()["message"]
        client.query.get_history.assert_not_awaited()

    def test_history_reads_the_listing_of_the_isin_and_says_which_one(self, client):
        response = client.get(f"/ticker/NEM/history?isin={NEWMONT}")

        assert response.status_code == 200
        assert response.json()["listing"] == LISTING
        assert client.query.get_history.await_args.kwargs["isin"] == NEWMONT

    def test_history_refuses_a_malformed_isin(self, client):
        assert client.get("/ticker/NEM/history?isin=NOTANISIN").status_code == 422

    def test_manual_ingestion_passes_the_isin(self, client):
        client.ingestion.ingest.return_value = {
            "ticker": "NEM",
            "resolution": "1D",
            "status": "failed",
            "error": "no data",
        }

        response = client.post(f"/historical/ingest?ticker=NEM&isin={NEWMONT.lower()}")

        assert response.status_code == 200
        assert client.ingestion.ingest.await_args.kwargs["isin"] == NEWMONT


def test_two_instruments_sharing_a_ticker_never_share_a_cache_entry():
    cache = CacheService.__new__(CacheService)
    keys = {
        cache.generate_key("NEM", "1y", cache_type="eod", isin=isin)
        for isin in (None, NEWMONT, NEMETSCHEK)
    }
    assert len(keys) == 3
