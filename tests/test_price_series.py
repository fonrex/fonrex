"""A price series belongs to one listing, one resolution, and is dated by session.

These tests guard the three defects fixed together:

- the prices of two listings of the same instrument (two currencies) used to
  overwrite or interleave each other, because a bar was identified by
  ``(time, asset_id)`` only;
- a weekly bar used to replace the daily bar of the same day, because the
  resolution was not part of that identity;
- a daily bar used to be stored at the UTC instant of midnight on its exchange,
  so every exchange east of Greenwich was dated one day early.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.price_series import (
    PriceSeries,
    latest_daily_close_of_asset,
    resolve_price_series,
    resolve_price_series_async,
    session_date,
    session_timestamp,
    ticker_candidates,
)
from database.query import QueryService
from historical.ingestion_service import HistoricalIngestionService
from historical.normalization import normalize_bars
from historical.providers import HistoricalMarketDataFetcher
from historical.yahoo_symbols import SymbolResolution
from models import Asset, AssetListing, Base, PriceEOD

JAN_8 = session_timestamp(date(2024, 1, 8))


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def catalogue(session_factory):
    """One ETF quoted in EUR and in USD, one stock, one instrument without listing."""
    session = session_factory()
    session.add_all(
        [
            Asset(id=1, ticker="VWCE", name="World ETF", isin="IE00BK5BQT80", is_active=True),
            Asset(id=2, ticker="AIR", name="Airbus", isin="NL0000235190", is_active=True),
            Asset(id=3, ticker="ORPHAN", name="No listing", is_active=True),
            Asset(id=4, ticker="OLDNAME", name="Known by its legacy ticker", is_active=True),
        ]
    )
    session.add_all(
        [
            AssetListing(id=10, asset_id=1, ticker="VWCE", exchange="XETRA", currency="EUR"),
            AssetListing(
                id=11, asset_id=1, ticker="VWRA", exchange="LSE", currency="USD", is_primary=True
            ),
            AssetListing(
                id=20, asset_id=2, ticker="AIR", exchange="PA", currency="EUR", is_primary=True
            ),
            AssetListing(
                id=21, asset_id=2, ticker="AIR", exchange="DE", currency="EUR", is_active=False
            ),
            AssetListing(id=40, asset_id=4, ticker="NEWNAME", exchange="PA", currency="EUR"),
            AssetListing(
                id=41, asset_id=4, ticker="NEWNAME2", exchange="DE", currency="EUR", is_primary=True
            ),
        ]
    )
    session.commit()
    session.close()
    return session_factory


def _bar(day: date, close: float, **overrides) -> dict:
    return {
        "timestamp": session_timestamp(day),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        **overrides,
    }


class TestSessionDate:
    def test_a_session_is_stored_at_midnight_utc_of_its_date(self):
        assert session_timestamp(date(2024, 1, 8)) == datetime(2024, 1, 8, tzinfo=UTC)

    def test_reading_back_does_not_depend_on_the_time_zone_of_the_driver(self):
        stored = session_timestamp(date(2024, 1, 8))
        new_york = timezone(timedelta(hours=-5))
        assert session_date(stored.astimezone(new_york)) == date(2024, 1, 8)
        assert session_date(stored) == date(2024, 1, 8)
        assert session_date(datetime(2024, 1, 8)) == date(2024, 1, 8)
        assert session_date(date(2024, 1, 8)) == date(2024, 1, 8)


class TestResolution:
    def test_candidates_are_the_ticker_then_the_ticker_without_its_suffix(self):
        assert ticker_candidates(" air.pa ") == ["AIR.PA", "AIR"]
        assert ticker_candidates("AAPL") == ["AAPL"]

    def test_a_ticker_designates_its_own_listing_not_another_one_of_the_instrument(
        self, catalogue
    ):
        session = catalogue()
        assert resolve_price_series(session, "VWCE") == PriceSeries(asset_id=1, listing_id=10)
        assert resolve_price_series(session, "vwra") == PriceSeries(asset_id=1, listing_id=11)

    def test_primary_listing_wins_between_listings_sharing_a_ticker(self, catalogue):
        assert resolve_price_series(catalogue(), "AIR.PA") == PriceSeries(asset_id=2, listing_id=20)

    def test_inactive_listings_can_be_left_out(self, catalogue):
        session = catalogue()
        session.query(AssetListing).filter_by(id=20).update({"is_active": False})
        session.commit()
        assert resolve_price_series(session, "AIR") == PriceSeries(2, 20)
        assert resolve_price_series(session, "AIR", active_only=True) is None

    def test_legacy_instrument_ticker_designates_the_preferred_listing(self, catalogue):
        assert resolve_price_series(catalogue(), "OLDNAME") == PriceSeries(4, 41)

    def test_an_instrument_without_listing_has_no_price_series(self, catalogue):
        session = catalogue()
        assert resolve_price_series(session, "ORPHAN") is None
        assert resolve_price_series(session, "UNKNOWN") is None

    def test_async_resolution_uses_the_same_statements(self):
        session = MagicMock()
        found = MagicMock()
        found.first.return_value = (1, 10)
        missing = MagicMock()
        missing.first.return_value = None
        session.execute = AsyncMock(side_effect=[missing, found])

        series = asyncio.run(resolve_price_series_async(session, "VWCE"))

        assert series == PriceSeries(asset_id=1, listing_id=10)
        assert session.execute.await_count == 2


class TestFetchedBarsAreDatedBySession:
    @pytest.mark.parametrize(
        "exchange_time_zone",
        ["Europe/Paris", "Asia/Tokyo", "America/New_York", "Australia/Sydney", None],
    )
    def test_yfinance_bar_keeps_the_date_of_its_exchange(self, exchange_time_zone):
        index = pd.DatetimeIndex(["2024-01-08"])
        if exchange_time_zone:
            index = index.tz_localize(exchange_time_zone)
        history = pd.DataFrame(
            {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]},
            index=index,
        )
        with patch("historical.providers.yf.Ticker") as ticker:
            ticker.return_value.history.return_value = history
            result = HistoricalMarketDataFetcher()._fetch_yfinance_sync(
                "X", "1D", date(2024, 1, 1), date(2024, 1, 31)
            )

        assert [bar["timestamp"] for bar in result["bars"]] == [JAN_8]
        assert result["bars"][0]["source"] == "yfinance"

    def test_tradingview_bar_is_dated_by_the_utc_date_of_its_session_opening(self):
        opening = int(datetime(2024, 1, 8, 14, 30, tzinfo=UTC).timestamp())
        message = (
            '~m~1~m~{"m":"timescale_update","p":["cs",{"sds_1":{"s":'
            f'[{{"i":0,"v":[{opening},1.0,2.0,0.5,1.5,10]}}]}}}}]}}'
        )
        bars: list[dict] = []
        HistoricalMarketDataFetcher._append_tradingview_bars(
            message, date(2024, 1, 1), date(2024, 1, 31), bars
        )

        assert [bar["timestamp"] for bar in bars] == [JAN_8]
        assert bars[0]["source"] == "tradingview"

    def test_normalized_bars_carry_listing_resolution_and_source(self):
        bars = normalize_bars(
            [_bar(date(2024, 1, 8), 10.0, source="yfinance")],
            asset_id=1,
            listing_id=10,
            resolution="1D",
        )
        assert bars[0]["asset_listing_id"] == 10
        assert bars[0]["resolution"] == "1D"
        assert bars[0]["time"] == JAN_8
        assert bars[0]["source"] == "yfinance"


class TestStoredSeries:
    @pytest.fixture
    def ingestion(self, catalogue):
        database = MagicMock()
        database.get_session.side_effect = catalogue
        symbols = MagicMock()
        symbols.resolve = AsyncMock(return_value=SymbolResolution("AIR.PA", origin="manual"))
        return HistoricalIngestionService(
            db_service=database, query_service=MagicMock(), symbol_resolver=symbols
        )

    def _write(self, ingestion, listing_id, asset_id, resolution, bars, replace=False):
        normalized = normalize_bars(bars, asset_id, listing_id, resolution)
        return ingestion._upsert_prices_eod_sync(normalized, replace)

    def _closes(self, session_factory, listing_id, resolution="1D") -> dict[date, float]:
        session = session_factory()
        rows = session.execute(
            select(PriceEOD.timestamp, PriceEOD.close)
            .where(PriceEOD.asset_listing_id == listing_id, PriceEOD.resolution == resolution)
            .order_by(PriceEOD.timestamp)
        ).all()
        session.close()
        return {session_date(time): close for time, close in rows}

    def test_two_listings_of_one_instrument_keep_their_own_prices(self, ingestion, catalogue):
        day = date(2024, 1, 8)
        self._write(ingestion, 10, 1, "1D", [_bar(day, 90.0)])  # EUR listing
        self._write(ingestion, 11, 1, "1D", [_bar(day, 100.0)])  # USD listing, same day

        assert self._closes(catalogue, 10) == {day: 90.0}
        assert self._closes(catalogue, 11) == {day: 100.0}

    def test_a_weekly_bar_does_not_replace_the_daily_bar_of_the_same_day(
        self, ingestion, catalogue
    ):
        monday = date(2024, 1, 8)
        self._write(ingestion, 20, 2, "1D", [_bar(monday, 140.0)])
        self._write(ingestion, 20, 2, "1W", [_bar(monday, 145.0)])

        assert self._closes(catalogue, 20, "1D") == {monday: 140.0}
        assert self._closes(catalogue, 20, "1W") == {monday: 145.0}

    def test_ingesting_the_same_session_again_updates_the_bar_in_place(self, ingestion, catalogue):
        day = date(2024, 1, 8)
        self._write(ingestion, 20, 2, "1D", [_bar(day, 140.0)])
        self._write(ingestion, 20, 2, "1D", [_bar(day, 141.0)])

        assert self._closes(catalogue, 20) == {day: 141.0}

    def test_forced_refresh_replaces_the_fetched_range_only(self, ingestion, catalogue):
        first, stray, last, later = (date(2024, 1, day) for day in (8, 9, 10, 15))
        self._write(
            ingestion, 20, 2, "1D", [_bar(first, 1.0), _bar(stray, 2.0), _bar(later, 9.0)]
        )
        self._write(ingestion, 10, 1, "1D", [_bar(stray, 50.0)])  # another listing

        # The refreshed source no longer has a bar on `stray` (it was a wrong date).
        self._write(ingestion, 20, 2, "1D", [_bar(first, 1.5), _bar(last, 3.5)], replace=True)

        assert self._closes(catalogue, 20) == {first: 1.5, last: 3.5, later: 9.0}
        assert self._closes(catalogue, 10) == {stray: 50.0}

    def test_ingestion_passes_the_forced_refresh_to_the_writer(self, ingestion):
        ingestion._detect_gaps = AsyncMock(return_value=(date(2024, 1, 1), date(2024, 1, 31), False))
        ingestion._fetch_with_fallback = AsyncMock(
            return_value={"bars": [_bar(date(2024, 1, 8), 140.0)], "source_used": "yfinance"}
        )
        ingestion._upsert_prices_eod = AsyncMock(return_value=1)
        ingestion._log_ingest = AsyncMock()

        result = asyncio.run(ingestion.ingest("AIR.PA", force_refresh=True))

        assert result.status == "success"
        bars = ingestion._upsert_prices_eod.await_args.args[0]
        assert {bar["asset_listing_id"] for bar in bars} == {20}
        assert ingestion._upsert_prices_eod.await_args.kwargs == {"replace": True}

    def test_an_instrument_without_listing_cannot_be_ingested(self, ingestion):
        result = asyncio.run(ingestion.ingest("ORPHAN"))

        assert result.status == "failed"
        assert "No listing found" in result.error

    def test_last_price_of_an_instrument_is_the_last_daily_close_of_its_main_listing(
        self, ingestion, catalogue
    ):
        self._write(ingestion, 11, 1, "1D", [_bar(date(2024, 1, 8), 100.0)])  # primary, USD
        self._write(ingestion, 10, 1, "1D", [_bar(date(2024, 1, 9), 90.0)])  # more recent, EUR
        self._write(ingestion, 11, 1, "1W", [_bar(date(2024, 1, 15), 111.0)])  # weekly bar

        session = catalogue()
        assert session.execute(latest_daily_close_of_asset(1)).scalar() == 100.0
        assert session.execute(latest_daily_close_of_asset(3)).scalar() is None


class TestQueryServiceConnection:
    """Where the reader connects when it is not handed a session factory."""

    def test_address_given_or_read_from_the_environment_uses_the_async_driver(self, monkeypatch):
        given = QueryService("postgresql://reader:secret@db.internal:5432/fonrex")
        assert given.engine.url.drivername == "postgresql+asyncpg"
        assert given.engine.url.host == "db.internal"
        assert given._owns_engine is True

        monkeypatch.setenv("DATABASE_URL", "postgresql://fonrex:secret@other-host:5433/prices")
        from_environment = QueryService()
        assert (from_environment.engine.url.host, from_environment.engine.url.port) == (
            "other-host",
            5433,
        )

    def test_injected_session_factory_is_used_as_is(self):
        factory, engine = MagicMock(), MagicMock()
        service = QueryService(session_factory=factory, engine=engine)
        assert (service.async_session, service.engine, service._owns_engine) == (
            factory,
            engine,
            False,
        )


class TestHistoryIsReadPerListing:
    def _service(self, rows):
        result = MagicMock()
        result.mappings.return_value.all.return_value = rows
        result.mappings.return_value.first.return_value = rows[0] if rows else None
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = session
        service = QueryService(session_factory=factory)
        service.get_series = AsyncMock(return_value=PriceSeries(asset_id=1, listing_id=10))
        return service, session

    def test_history_filters_on_the_listing_with_utc_session_bounds(self):
        service, session = self._service([{"time": JAN_8, "close": 90.0}])

        rows = asyncio.run(
            service.get_history("VWCE", date(2024, 1, 1), date(2024, 1, 31), "daily")
        )

        statement, parameters = session.execute.await_args.args
        assert "asset_listing_id = :listing_id" in str(statement)
        assert parameters == {
            "listing_id": 10,
            "resolution": "1D",
            "start_date": session_timestamp(date(2024, 1, 1)),
            "end_date": session_timestamp(date(2024, 1, 31)),
        }
        assert rows == [{"time": JAN_8, "close": 90.0}]

    def test_range_is_reported_in_session_dates(self):
        new_york = timezone(timedelta(hours=-5))
        service, session = self._service(
            [{"min_date": JAN_8.astimezone(new_york), "max_date": JAN_8, "count": 1}]
        )

        assert asyncio.run(service.get_history_range("VWCE")) == {
            "min_date": date(2024, 1, 8),
            "max_date": date(2024, 1, 8),
            "count": 1,
        }
        assert session.execute.await_args.args[1] == {"listing_id": 10, "resolution": "1D"}

    def test_unknown_ticker_has_no_history(self):
        service, _session = self._service([])
        service.get_series = AsyncMock(return_value=None)

        assert asyncio.run(service.get_history("NOPE")) == []
        assert asyncio.run(service.get_history_range("NOPE"))["count"] == 0
        assert asyncio.run(service.get_asset_id("NOPE")) is None
