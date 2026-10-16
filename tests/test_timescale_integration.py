"""Migrations and price storage checked on a real TimescaleDB.

The rest of the suite builds its schema on SQLite from the models and never runs
Alembic: hypertables, compression, continuous aggregates and ``ON CONFLICT`` on
PostgreSQL are out of its reach. These tests run the real migrations on a real
database.

They are skipped unless ``FONREX_TEST_DATABASE_URL`` points to a TimescaleDB
server with a role allowed to create databases, for instance the one of
``docker compose``::

    FONREX_TEST_DATABASE_URL=postgresql://fonrex:fonrex_password@localhost:5432/fonrex \\
        pytest tests/test_timescale_integration.py

A temporary database is created next to the one named in the URL and dropped at
the end; the database of the URL itself is never touched.
"""

import os
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from database.price_series import session_date, session_timestamp
from database.query import QueryService
from database.technical import SqlAlchemyTechnicalRepository
from historical.ingestion_service import HistoricalIngestionService
from historical.normalization import normalize_bars
from historical.yahoo_symbols import YahooCandidate, YahooQuote, YahooSymbolResolver
from technical.contracts import MarketSeries
from zipline_bundle.data_source import FonRexBundleDataSource

SERVER_URL = os.environ.get("FONREX_TEST_DATABASE_URL")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not SERVER_URL,
    reason="set FONREX_TEST_DATABASE_URL to a TimescaleDB server to run the database tests",
)

# Prices as they were stored before revision 014: identified by (time, asset_id),
# at the UTC instant of midnight on the exchange.
OLD_SHAPE_DATA = """
INSERT INTO assets (id, ticker, name, exchange, currency, is_active, isin) VALUES
 (1, 'VWCE', 'World ETF', 'XETRA', 'EUR', true, 'IE00BK5BQT80'),
 (2, 'AAPL', 'Apple', 'NASDAQ', 'USD', true, 'US0378331005'),
 (3, 'ORPHAN', 'No listing', 'NYSE', 'USD', true, NULL);
INSERT INTO asset_listings
 (id, asset_id, ticker, exchange, currency, source, is_primary, is_active, created_at, updated_at)
VALUES
 (10, 1, 'VWCE', 'XETRA', 'EUR', 'csv', false, true, now(), now()),
 (11, 1, 'VWRA', 'LSE', 'USD', 'csv', true, true, now(), now()),
 (20, 2, 'AAPL', 'NASDAQ', 'USD', 'csv', true, true, now(), now());
SELECT setval('asset_listings_id_seq', 100);
INSERT INTO prices_eod
 (time, asset_id, asset_listing_id, open, high, low, close, adj_close, volume, resolution)
VALUES
 -- Xetra listing: midnight in Frankfurt is 23:00Z the day before
 ('2024-01-07 23:00+00', 1, 10, 90, 91, 89, 90.5, 90.5, 1000, '1D'),
 ('2024-01-08 23:00+00', 1, 10, 91, 92, 90, 91.5, 91.5, 1100, '1D'),
 -- London listing of the same instrument, in another currency
 ('2024-01-08 00:00+00', 1, 11, 100, 101, 99, 100.5, 100.5, 2000, '1D'),
 ('2024-01-09 00:00+00', 1, 11, 101, 102, 100, 101.5, 101.5, 2100, '1D'),
 -- New York: midnight is 05:00Z the same day
 ('2024-01-08 05:00+00', 2, 20, 180, 181, 179, 180.5, 180.5, 5000, '1D'),
 -- row that names no listing
 ('2024-01-09 05:00+00', 2, NULL, 181, 182, 180, 181.5, 181.5, 5100, '1D'),
 -- TradingView fallback: opening of the session, 14:30Z
 ('2024-01-10 14:30+00', 2, 20, 182, 183, 181, 182.5, 182.5, 5200, '1D'),
 -- one session fetched from both sources
 ('2024-01-11 05:00+00', 2, 20, 183, 184, 182, 183.5, 183.5, 5300, '1D'),
 ('2024-01-11 14:30+00', 2, 20, 183, 184, 182, 183.4, 183.4, 10, '1D'),
 -- weekly bar
 ('2024-01-15 05:00+00', 2, 20, 180, 190, 179, 188, 188, 30000, '1W'),
 -- Tokyo (15:00Z the day before) and Sydney in summer time (13:00Z the day before)
 ('2024-01-16 15:00+00', 2, 20, 1, 1, 1, 1.0, 1.0, 1, '1D'),
 ('2024-01-17 13:00+00', 2, 20, 2, 2, 2, 2.0, 2.0, 2, '1D'),
 -- old rows, in a chunk compressed before the migration
 ('2020-03-02 05:00+00', 2, 20, 70, 71, 69, 70.5, 70.5, 900, '1D'),
 ('2020-03-01 23:00+00', 1, 10, 60, 61, 59, 60.5, 60.5, 800, '1D'),
 -- instrument that has prices but no listing
 ('2024-01-08 05:00+00', 3, NULL, 10, 11, 9, 10.5, 10.5, 50, '1D');
SELECT compress_chunk(c) FROM show_chunks('prices_eod', older_than => DATE '2021-01-01') c;
INSERT INTO fundamentals_highlights
 (asset_id, dividend_yield, dividend_rate, pe_ratio, eps_trailing)
VALUES
 -- stored when Yahoo published a ratio: 1.80 of dividend for a price of 100
 (1, 0.018, 1.80, 20, 5),
 -- stored as Yahoo publishes it now, in percent: 1.08 for a price of 333.6
 (2, 0.32, 1.08, 38.3, 8.71),
 -- a percentage on a row without the figures that give the price
 (3, 1.45, NULL, NULL, NULL);
"""


def _alembic(url: str, *arguments: str) -> None:
    """Run Alembic in its own process (its logging setup must not leak into pytest)."""
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]


@pytest.fixture(scope="module")
def database_url():
    """A temporary database on the test server, dropped at the end."""
    server = make_url(SERVER_URL)
    name = f"fonrex_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def engine(database_url):
    """Database migrated to 013, filled with old-shape prices, then migrated to head.

    Sessions run in the New York time zone, to show that nothing read from the
    table depends on the time zone of the session.
    """
    _alembic(database_url, "upgrade", "013")
    setup = create_engine(database_url, poolclass=NullPool)
    with setup.begin() as connection:
        connection.execute(text(OLD_SHAPE_DATA))
        name = make_url(database_url).database
        connection.execute(text(f"ALTER DATABASE \"{name}\" SET timezone = 'America/New_York'"))
    setup.dispose()
    _alembic(database_url, "upgrade", "head")

    engine = create_engine(database_url, poolclass=NullPool)
    yield engine
    engine.dispose()


def _series(engine, listing_id: int, resolution: str = "1D") -> dict[date, float]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
                SELECT (time AT TIME ZONE 'UTC')::date, close FROM prices_eod
                WHERE asset_listing_id = :listing AND resolution = :resolution
                ORDER BY time
            """),
            {"listing": listing_id, "resolution": resolution},
        ).all()
    return dict(rows)


def _bar(day: date, close: float) -> dict:
    return {
        "timestamp": session_timestamp(day),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "source": "yfinance",
    }


@pytest.fixture
def ingestion(engine):
    database = MagicMock()
    database.get_session.side_effect = sessionmaker(bind=engine)
    return HistoricalIngestionService(db_service=database, query_service=MagicMock())


class TestMigrationOfExistingPrices:
    def test_every_bar_is_at_midnight_utc(self, engine):
        with engine.connect() as connection:
            off_midnight = connection.execute(
                text("SELECT count(*) FROM prices_eod WHERE time <> date_trunc('day', time, 'UTC')")
            ).scalar()
        assert off_midnight == 0

    def test_european_sessions_are_no_longer_dated_the_day_before(self, engine):
        assert _series(engine, 10) == {
            date(2020, 3, 2): 60.5,
            date(2024, 1, 8): 90.5,
            date(2024, 1, 9): 91.5,
        }

    def test_listings_of_one_instrument_have_separate_series(self, engine):
        assert _series(engine, 11) == {date(2024, 1, 8): 100.5, date(2024, 1, 9): 101.5}

    def test_american_asian_and_fallback_rows_get_their_session_date(self, engine):
        assert _series(engine, 20) == {
            date(2020, 3, 2): 70.5,  # was in a compressed chunk
            date(2024, 1, 8): 180.5,
            date(2024, 1, 9): 181.5,  # the row that named no listing
            date(2024, 1, 10): 182.5,  # TradingView opening, same day
            date(2024, 1, 11): 183.5,  # two fetches merged, the larger volume kept
            date(2024, 1, 17): 1.0,  # Tokyo midnight
            date(2024, 1, 18): 2.0,  # Sydney summer midnight
        }

    def test_weekly_bars_live_next_to_daily_bars(self, engine):
        assert _series(engine, 20, "1W") == {date(2024, 1, 15): 188.0}

    def test_instrument_without_listing_gets_one_and_keeps_its_prices(self, engine):
        with engine.connect() as connection:
            listing = connection.execute(
                text("SELECT id, ticker, source, is_primary FROM asset_listings WHERE asset_id = 3")
            ).one()
        assert (listing.ticker, listing.source, listing.is_primary) == (
            "ORPHAN",
            "migration_014",
            True,
        )
        assert _series(engine, listing.id) == {date(2024, 1, 8): 10.5}

    def test_table_is_a_compressed_hypertable_keyed_by_listing(self, engine):
        with engine.connect() as connection:
            key = connection.execute(
                text("""
                    SELECT array_agg(a.attname::text ORDER BY k.position)
                    FROM pg_index i
                    CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                    WHERE i.indrelid = 'prices_eod'::regclass AND i.indisprimary
                """)
            ).scalar()
            compression = connection.execute(
                text("""
                    SELECT compression_enabled FROM timescaledb_information.hypertables
                    WHERE hypertable_name = 'prices_eod'
                """)
            ).scalar()
            # The jobs view names the aggregate of a refresh policy, or its
            # materialisation hypertable in older TimescaleDB versions.
            jobs = connection.execute(
                text("""
                    SELECT j.proc_name::text, COALESCE(a.view_name, j.hypertable_name)::text
                    FROM timescaledb_information.jobs j
                    LEFT JOIN timescaledb_information.continuous_aggregates a
                      ON a.materialization_hypertable_schema = j.hypertable_schema
                     AND a.materialization_hypertable_name = j.hypertable_name
                    WHERE j.proc_name IN (
                        'policy_compression', 'policy_refresh_continuous_aggregate'
                    )
                    ORDER BY 1, 2
                """)
            ).all()
        assert key == ["asset_listing_id", "resolution", "time"]
        assert compression is True
        assert [tuple(job) for job in jobs] == [
            ("policy_compression", "prices_eod"),
            ("policy_refresh_continuous_aggregate", "prices_monthly"),
            ("policy_refresh_continuous_aggregate", "prices_weekly"),
        ]


class TestIngestionOnPostgres:
    def test_listings_and_resolutions_do_not_overwrite_each_other(self, ingestion, engine):
        day = date(2024, 2, 5)
        for listing_id, asset_id, resolution, close in (
            (10, 1, "1D", 90.0),
            (11, 1, "1D", 100.0),
            (11, 1, "1W", 105.0),
        ):
            bars = normalize_bars([_bar(day, close)], asset_id, listing_id, resolution)
            assert ingestion._upsert_prices_eod_sync(bars) == 1

        assert _series(engine, 10)[day] == 90.0
        assert _series(engine, 11)[day] == 100.0
        assert _series(engine, 11, "1W")[day] == 105.0

    def test_a_bar_of_a_compressed_chunk_is_updated_in_place(self, ingestion, engine):
        with engine.begin() as connection:
            compressed = connection.execute(
                text("""
                    SELECT count(compress_chunk(c, if_not_compressed => true))
                    FROM show_chunks('prices_eod', older_than => DATE '2021-01-01') c
                """)
            ).scalar()
        assert compressed >= 1

        bars = normalize_bars([_bar(date(2020, 3, 2), 77.0)], 2, 20, "1D")
        ingestion._upsert_prices_eod_sync(bars)

        assert _series(engine, 20)[date(2020, 3, 2)] == 77.0
        assert _series(engine, 10)[date(2020, 3, 2)] == 60.5  # the other listing is untouched

    def test_forced_refresh_replaces_the_range(self, ingestion, engine):
        first, stray, last = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)
        seed = normalize_bars([_bar(first, 1.0), _bar(stray, 2.0)], 2, 20, "1D")
        ingestion._upsert_prices_eod_sync(seed)

        fresh = normalize_bars([_bar(first, 1.5), _bar(last, 3.5)], 2, 20, "1D")
        ingestion._upsert_prices_eod_sync(fresh, replace=True)

        march = {day: close for day, close in _series(engine, 20).items() if day.month == 3}
        assert march == {date(2020, 3, 2): 77.0, first: 1.5, last: 3.5}


class TestReadsOnPostgres:
    @pytest.fixture
    async def query_service(self, database_url):
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        yield QueryService(session_factory=async_sessionmaker(async_engine))
        await async_engine.dispose()

    async def test_history_of_a_ticker_is_the_series_of_its_listing(self, query_service):
        day = date(2024, 1, 8)

        eur = await query_service.get_history("VWCE", start_date=day, end_date=day)
        usd = await query_service.get_history("VWRA", start_date=day, end_date=day)

        assert [(row["time"], row["close"]) for row in eur] == [(session_timestamp(day), 90.5)]
        assert [(row["time"], row["close"]) for row in usd] == [(session_timestamp(day), 100.5)]

    async def test_range_is_given_in_session_dates(self, query_service):
        assert await query_service.get_history_range("VWRA") == {
            "min_date": date(2024, 1, 8),
            "max_date": date(2024, 2, 5),
            "count": 3,
        }

    async def test_weekly_history_falls_back_on_the_aggregate_of_the_listing(self, query_service):
        weeks = await query_service.get_history(
            "VWCE", start_date=date(2024, 1, 8), end_date=date(2024, 1, 14), interval="weekly"
        )

        assert len(weeks) == 1
        assert weeks[0]["time"] == datetime(2024, 1, 8, tzinfo=timezone.utc)  # a Monday
        assert (weeks[0]["open"], weeks[0]["close"], weeks[0]["volume"]) == (90.0, 91.5, 2100)

    async def test_technical_data_is_read_per_listing(self, engine):
        repository = SqlAlchemyTechnicalRepository(sessionmaker(bind=engine))

        series = await repository.resolve_series("VWCE")
        frame = await repository.load_ohlcv(
            series, "1D", from_date=date(2024, 1, 8), to_date=date(2024, 1, 9)
        )

        assert series == MarketSeries(asset_id=1, listing_id=10)
        assert list(frame.index) == [
            pd.Timestamp("2024-01-08", tz="UTC"),
            pd.Timestamp("2024-01-09", tz="UTC"),
        ]
        assert list(frame["close"]) == [90.5, 91.5]

    def test_zipline_bundle_reads_the_primary_listing_on_its_session_dates(self, engine):
        source = FonRexBundleDataSource(engine=engine, tickers=["VWRA", "VWCE"])

        bars = list(source.iter_tickers(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-31")))

        assert [entry.metadata.symbol for entry in bars] == ["VWRA"]
        assert list(bars[0].frame.index) == [pd.Timestamp("2024-01-08"), pd.Timestamp("2024-01-09")]
        assert list(bars[0].frame["close"]) == [100.5, 101.5]


class TestSourceSymbolOnPostgres:
    """The verified symbol of a listing is stored in its ``YahooFinance`` mapping."""

    class Yahoo:
        def __init__(self):
            self.searched = []

        def search(self, isin):
            self.searched.append(isin)
            return [YahooCandidate("VWCE.DE", "ETF", 20001.0)]

        def quote(self, symbol):
            return YahooQuote("EUR", 120.0)

    @pytest.fixture
    def resolver(self, engine):
        database = MagicMock()
        database.get_session.side_effect = sessionmaker(bind=engine)
        yahoo = self.Yahoo()
        resolver = YahooSymbolResolver(database, yahoo)
        resolver.yahoo = yahoo
        return resolver

    def test_symbol_is_stored_then_reused(self, resolver, engine):
        assert resolver.resolve_sync(10).symbol == "VWCE.DE"  # EUR listing
        assert resolver.resolve_sync(10).symbol == "VWCE.DE"

        assert resolver.yahoo.searched == ["IE00BK5BQT80"]  # asked once
        with engine.connect() as connection:
            stored = connection.execute(
                text("""
                    SELECT provider_ticker, source, is_active, last_verified_at IS NOT NULL
                    FROM asset_mappings
                    WHERE asset_listing_id = 10 AND provider_name = 'YahooFinance'
                """)
            ).one()
        assert tuple(stored) == ("VWCE.DE", "isin_search", True, True)

    def test_listing_in_another_currency_is_refused_and_not_asked_again(self, resolver):
        first = resolver.resolve_sync(11)  # USD listing of the same instrument
        second = resolver.resolve_sync(11)

        assert not first.found and "VWCE.DE (EUR)" in first.reason
        assert not second.found and "checked on" in second.reason
        assert resolver.yahoo.searched == ["IE00BK5BQT80"]

    def test_symbol_set_by_hand_as_documented_in_the_readme(self, resolver, engine):
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO asset_mappings (asset_id, asset_listing_id, provider_name,
                        provider_ticker, source, is_active, failure_count, created_at, updated_at)
                    SELECT l.asset_id, l.id, 'YahooFinance', 'VWRA.L', 'manual', true, 0, now(), now()
                    FROM asset_listings l WHERE l.ticker = 'VWRA' AND l.currency = 'USD'
                    ON CONFLICT (asset_listing_id, provider_name)
                    DO UPDATE SET provider_ticker = EXCLUDED.provider_ticker,
                                  source = 'manual', is_active = true
                """)
            )

        assert resolver.resolve_sync(11).symbol == "VWRA.L"


class TestStoredDividendYieldsAreRatios:
    def _yields(self, engine) -> dict[int, float]:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT asset_id, dividend_yield FROM fundamentals_highlights")
            ).all()
        return {asset_id: float(value) for asset_id, value in rows}

    def test_percentages_are_converted_and_ratios_left_alone(self, engine):
        assert self._yields(engine) == {1: 0.018, 2: 0.0032, 3: 0.0145}

    def test_running_the_correction_again_changes_nothing(self, database_url, engine):
        _alembic(database_url, "downgrade", "014")
        _alembic(database_url, "upgrade", "head")

        assert self._yields(engine) == {1: 0.018, 2: 0.0032, 3: 0.0145}


class TestAdjustmentSchemeOnPostgres:
    """Revision 016: the adjustment scheme of each stored series (``historical/adjustment.py``)."""

    def _schemes(self, engine) -> list[tuple]:
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT asset_listing_id, resolution, scheme FROM price_series_adjustments")
            ).all()

    def test_series_stored_before_have_no_scheme_and_recording_it_is_an_upsert(self, engine):
        from historical.adjustment import ADJUSTMENT_SCHEME, record_series_scheme

        assert self._schemes(engine) == []  # fetched again in full at their next ingestion

        session = sessionmaker(bind=engine)()
        try:
            record_series_scheme(session, 20, "1D")
            record_series_scheme(session, 20, "1D")
            session.commit()
        finally:
            session.close()
        assert self._schemes(engine) == [(20, "1D", ADJUSTMENT_SCHEME)]

    def test_stored_range_and_edge_bars_are_read_in_session_dates(self, engine):
        from historical.adjustment import edge_bars, stored_series

        session = sessionmaker(bind=engine)()
        try:
            stored = stored_series(session, 11, "1D")
            newest = edge_bars(session, 11, "1D", True, 1)
            oldest = edge_bars(session, 11, "1D", False, 2)
        finally:
            session.close()
        # Listing 11 holds the two migrated sessions, and a bar written by an earlier test.
        assert stored.first == date(2024, 1, 8)
        assert [session_date(bar["time"]) for bar in newest] == [stored.last]
        assert [(session_date(bar["time"]), bar["close"]) for bar in oldest] == [
            (date(2024, 1, 8), 100.5),
            (date(2024, 1, 9), 101.5),
        ]

    def test_downgrade_drops_the_table_and_upgrade_creates_it_again(self, database_url, engine):
        _alembic(database_url, "downgrade", "015")
        with engine.connect() as connection:
            table = connection.execute(text("SELECT to_regclass('price_series_adjustments')"))
            assert table.scalar() is None
        _alembic(database_url, "upgrade", "head")
        assert self._schemes(engine) == []


class TestMacroRatesFromSeveralSources:
    """Revision 017: ECB series names fit, and each row says its source."""

    def test_an_ecb_series_is_stored_next_to_fred(self, engine):
        series_id = "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO macro_rates_cache
                        (series_id, source, label, value, unit, observation_date)
                    VALUES (:series_id, 'ecb', 'AAA 10Y', -0.005215, 'ratio', DATE '2020-08-03')
                """),
                {"series_id": series_id},
            )
            stored = connection.execute(
                text("SELECT source, value FROM macro_rates_cache WHERE series_id = :series_id"),
                {"series_id": series_id},
            ).one()
        assert (stored.source, float(stored.value)) == ("ecb", -0.005215)

    def test_downgrade_drops_the_long_names_then_upgrade_marks_fred_rows(
        self, database_url, engine
    ):
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO macro_rates_cache (series_id, label, value, unit, observation_date)
                    VALUES ('DGS10', '10Y', 0.0412, 'percent', DATE '2026-10-08')
                """)
            )

        _alembic(database_url, "downgrade", "016")
        with engine.connect() as connection:
            names = connection.execute(text("SELECT series_id FROM macro_rates_cache")).scalars()
            assert list(names) == ["DGS10"]

        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            sources = connection.execute(
                text("SELECT DISTINCT source FROM macro_rates_cache")
            ).scalars()
            assert list(sources) == ["fred"]


class TestStatementsCurrencyIsRecordedOrUnknown:
    """Revision 018: the USD written by default on every statement becomes unknown."""

    @staticmethod
    def _currencies(engine) -> dict[str, str | None]:
        with engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT statement_type, currency FROM financial_statements
                    WHERE period_end = DATE '2025-12-31'
                """)
            ).all()
        return dict(rows)

    @staticmethod
    def _default(engine) -> str | None:
        with engine.connect() as connection:
            return connection.execute(
                text("""
                    SELECT column_default FROM information_schema.columns
                    WHERE table_name = 'financial_statements' AND column_name = 'currency'
                """)
            ).scalar()

    def test_default_usd_becomes_unknown_and_comes_back_on_downgrade(self, database_url, engine):
        _alembic(database_url, "downgrade", "017")
        with engine.begin() as connection:
            asset_id = connection.execute(text("SELECT min(id) FROM assets")).scalar()
            connection.execute(
                text("""
                    INSERT INTO financial_statements
                        (asset_id, statement_type, period_type, period_end)
                    VALUES (:asset, 'income', 'annual', DATE '2025-12-31')
                """),
                {"asset": asset_id},
            )
        assert self._currencies(engine) == {"income": "USD"}

        _alembic(database_url, "upgrade", "head")
        assert self._currencies(engine) == {"income": None}
        assert self._default(engine) is None

        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO financial_statements
                        (asset_id, statement_type, period_type, period_end, currency)
                    VALUES (:asset, 'balance', 'annual', DATE '2025-12-31', 'EUR')
                """),
                {"asset": asset_id},
            )
        _alembic(database_url, "downgrade", "017")
        assert self._currencies(engine) == {"income": "USD", "balance": "EUR"}
        assert "USD" in self._default(engine)

        _alembic(database_url, "upgrade", "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM financial_statements WHERE period_end = DATE '2025-12-31'")
            )


class TestYahooEpochDatesBecomeUnknown:
    """Revision 019: the 1970-01-01 dates written from Yahoo epoch seconds are dropped."""

    def test_only_the_1970_dates_become_null(self, database_url, engine):
        _alembic(database_url, "downgrade", "018")
        with engine.begin() as connection:
            asset_ids = connection.execute(
                text("SELECT asset_id FROM fundamentals_highlights ORDER BY asset_id LIMIT 2")
            ).scalars().all()
            wrong, right = asset_ids
            connection.execute(
                text("""
                    UPDATE fundamentals_highlights
                    SET dividend_ex_date = DATE '1970-01-01', shares_short_date = DATE '1970-01-01'
                    WHERE asset_id = :asset
                """),
                {"asset": wrong},
            )
            connection.execute(
                text("""
                    UPDATE fundamentals_highlights
                    SET dividend_ex_date = DATE '2026-08-11', shares_short_date = DATE '2026-09-15'
                    WHERE asset_id = :asset
                """),
                {"asset": right},
            )

        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT asset_id, dividend_ex_date, shares_short_date
                    FROM fundamentals_highlights WHERE asset_id IN (:wrong, :right)
                """),
                {"wrong": wrong, "right": right},
            ).all()
        dates = {asset_id: (ex_date, short_date) for asset_id, ex_date, short_date in rows}
        assert dates == {
            wrong: (None, None),
            right: (date(2026, 8, 11), date(2026, 9, 15)),
        }


# Price relations a migration must not hold while it waits: the tables, the
# aggregates, their chunks and materialisation hypertables (named "_hyper_…",
# "_materialized_hypertable_…", "_compressed_hypertable_…"). The catalog of the
# jobs is not one of them.
PRICE_RELATIONS_HELD = r"""
    SELECT DISTINCT c.relname::text
    FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE l.pid = :pid AND l.granted
      AND (c.relname IN ('prices_eod', 'prices_weekly', 'prices_monthly')
           OR (n.nspname = '_timescaledb_internal' AND c.relname LIKE '\_%'))
"""


def _migrate_while_prices_are_read(database_url: str, engine, *arguments: str) -> set[str]:
    """Run Alembic while another session reads ``prices_eod``, as a TimescaleDB job does.

    Return the price relations the migration held while it waited for that
    session: holding any of them is what let a background job and the migration
    wait for each other (deadlock).
    """
    name = make_url(database_url).database
    reader = engine.connect()
    transaction = reader.begin()
    reader.execute(text("SELECT count(*) FROM prices_eod")).scalar()
    process = subprocess.Popen(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    held = None
    try:
        deadline = time.monotonic() + 30
        while held is None and time.monotonic() < deadline and process.poll() is None:
            with engine.connect() as observer:
                pid = observer.execute(
                    text("""
                        SELECT pid FROM pg_stat_activity
                        WHERE datname = :name AND wait_event_type = 'Lock'
                          AND pid <> :reader
                          -- not a TimescaleDB job waiting too
                          AND backend_type = 'client backend'
                    """),
                    {"name": name, "reader": reader.connection.dbapi_connection.get_backend_pid()},
                ).scalar()
                if pid is not None:
                    held = set(observer.execute(text(PRICE_RELATIONS_HELD), {"pid": pid}).scalars())
            time.sleep(0.05)
    finally:
        transaction.rollback()
        reader.close()
        _stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr[-3000:]
    assert held is not None, "the migration did not wait for the session reading prices_eod"
    return held


class TestMigrationIsReversible:
    def test_migration_waits_for_a_job_on_the_prices_without_holding_them(
        self, database_url, engine
    ):
        """A compression or refresh job running during the migration is waited for.

        The migration takes the price tables before anything else. Holding a chunk
        while waiting, it deadlocked with a compression job (CI, downgrade then
        upgrade).
        """
        assert _migrate_while_prices_are_read(database_url, engine, "downgrade", "013") == set()
        assert _migrate_while_prices_are_read(database_url, engine, "upgrade", "head") == set()

    def test_downgrade_then_upgrade(self, database_url, engine):
        """The former key holds one bar per instrument and instant: going back
        keeps the series of the primary listing, which survives the round trip."""
        primary_listing_before = _series(engine, 11)
        single_listing_before = _series(engine, 20)

        _alembic(database_url, "downgrade", "013")
        with engine.connect() as connection:
            old_key = connection.execute(
                text("""
                    SELECT count(*) FROM pg_constraint
                    WHERE conrelid = 'prices_eod'::regclass
                      AND conname = 'prices_eod_timestamp_asset_id_key'
                """)
            ).scalar()
        assert old_key == 1

        _alembic(database_url, "upgrade", "head")
        assert _series(engine, 11) == primary_listing_before
        assert _series(engine, 20) == single_listing_before


class TestCleanupOnPostgres:
    """Runs last: it deletes rows the other tests read."""

    def test_old_bars_are_deleted_from_compressed_chunks_too(self, engine):
        from database.maintenance import DatabaseMaintenance

        maintenance = DatabaseMaintenance(engine, sessionmaker(bind=engine))
        # Keep everything since 1 January 2021: only the bars of 2020 are older.
        days_to_keep = (date.today() - date(2021, 1, 1)).days
        with engine.connect() as connection:
            before = connection.execute(text("SELECT count(*) FROM prices_eod")).scalar()
            old = connection.execute(
                text("SELECT count(*) FROM prices_eod WHERE time < '2021-01-01'")
            ).scalar()
        assert old > 0

        success, simulated, _error = maintenance.cleanup_old_data(days_to_keep, dry_run=True)
        assert success and simulated["deleted_records"] == old
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM prices_eod")).scalar() == before

        success, result, error = maintenance.cleanup_old_data(days_to_keep)
        assert success, error
        assert result["deleted_records"] == old
        assert result["cutoff_date"] == "2021-01-01"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM prices_eod")).scalar() == (
                before - old
            )

    def test_zero_days_is_refused_and_deletes_nothing(self, engine):
        from database.maintenance import DatabaseMaintenance

        maintenance = DatabaseMaintenance(engine, sessionmaker(bind=engine))
        with engine.connect() as connection:
            before = connection.execute(text("SELECT count(*) FROM prices_eod")).scalar()

        success, _result, error = maintenance.cleanup_old_data(0)

        assert success is False and "days_to_keep" in error
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM prices_eod")).scalar() == before



class TestFactorReturnsOnPostgres:
    """Revision 020 and ``factors.store`` on the database of an installation."""

    @pytest.fixture
    async def library(self, database_url):
        from factors.french_library import parse_factor_csv, read_zip
        from factors.store import FactorLibrary

        fixtures = PROJECT_ROOT / "tests" / "fixtures" / "factors"

        def table(name):
            return parse_factor_csv(read_zip((fixtures / f"{name}_CSV.zip").read_bytes()), "monthly")

        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        downloader = MagicMock()
        library = FactorLibrary(async_sessionmaker(async_engine), downloader=downloader)
        yield library, downloader, table
        await async_engine.dispose()

    @staticmethod
    def _stored(engine, dataset):
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT count(*) FROM factor_returns WHERE dataset = :dataset"),
                {"dataset": dataset},
            ).scalar()

    async def test_a_file_is_stored_whole_and_replaced_by_the_next_one(self, library, engine):
        library, downloader, table = library
        full = table("F-F_Research_Data_Factors")

        async def download(dataset, frequency):
            return full

        downloader.side_effect = download
        load = await library.refresh("us_3", "monthly")
        assert (load.status, load.periods) == ("fetched", 6)
        assert self._stored(engine, "us_3") == 6 * 4

        # The library revised its file: the next download replaces every row.
        revised = type(full)(
            factors=full.factors,
            rows={p: v for p, v in full.rows.items() if p.year == 2026},
            note=full.note,
        )

        async def download_revised(dataset, frequency):
            return revised

        downloader.side_effect = download_revised
        await library.refresh("us_3", "monthly", force=True)
        assert self._stored(engine, "us_3") == 3 * 4

        series = await library.series("us_3", "monthly", start=date(2026, 7, 1))
        assert list(series) == [date(2026, 7, 31), date(2026, 8, 31)]
        assert float(series[date(2026, 8, 31)]["MKT_RF"]) == 0.0256
        status = await library.status("us_3", "monthly")
        assert (status.status, status.periods, status.source_note) == ("fresh", 3, "CRSP 202608")

    def test_downgrade_drops_the_tables_and_upgrade_creates_them(self, database_url, engine):
        _alembic(database_url, "downgrade", "019")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT to_regclass('factor_returns')")).scalar() is None
        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT to_regclass('factor_returns')")).scalar()
            assert connection.execute(text("SELECT to_regclass('factor_dataset_loads')")).scalar()


class TestExchangeRatesOnPostgres:
    """Revision 021 and ``macro.fx_rates`` on the database of an installation."""

    @pytest.fixture
    async def rates(self, database_url):
        from macro.fx_rates import EcbExchangeRates

        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        downloader = MagicMock()
        yield EcbExchangeRates(async_sessionmaker(async_engine), downloader=downloader), downloader
        await async_engine.dispose()

    async def test_history_then_only_the_days_after_the_last_one(self, rates):
        from decimal import Decimal

        rates, downloader = rates
        asked = []
        answers = [
            {date(2026, 10, 7): Decimal("1.0861"), date(2026, 10, 8): Decimal("1.0850")},
            # A correction of the 8th and a new day.
            {date(2026, 10, 8): Decimal("1.0851"), date(2026, 10, 9): Decimal("1.0790")},
        ]

        async def download(currency, start):
            asked.append((currency, start))
            return answers[len(asked) - 1]

        downloader.side_effect = download

        first = await rates.refresh("USD")
        assert (first.status, first.first_day, first.last_day) == (
            "fetched",
            date(2026, 10, 7),
            date(2026, 10, 8),
        )
        assert asked == [("USD", date(1999, 1, 4))]

        assert (await rates.refresh("USD")).status == "fresh"  # read a moment ago

        second = await rates.refresh("USD", force=True)
        assert asked[1] == ("USD", date(2026, 10, 1))  # a week before the last day stored
        assert (second.added, second.last_day) == (2, date(2026, 10, 9))
        assert await rates.per_euro("USD") == {
            date(2026, 10, 7): Decimal("1.08610000"),
            date(2026, 10, 8): Decimal("1.08510000"),
            date(2026, 10, 9): Decimal("1.07900000"),
        }
        euros = await rates.rates("USD", "EUR", start=date(2026, 10, 9))
        assert euros == {date(2026, 10, 9): Decimal("0.92678406")}

    async def test_a_failed_refresh_keeps_the_stored_days(self, rates):
        from decimal import Decimal

        rates, downloader = rates
        async def download(currency, start):
            return {date(2026, 10, 8): Decimal("0.8412")}

        downloader.side_effect = download
        await rates.refresh("GBP")
        downloader.side_effect = RuntimeError("ECB down\nmore")

        failed = await rates.refresh("GBP", force=True)

        assert (failed.status, failed.last_day, failed.reason) == (
            "failed",
            date(2026, 10, 8),
            "RuntimeError: ECB down",
        )
        assert await rates.per_euro("GBP") == {date(2026, 10, 8): Decimal("0.84120000")}

    def test_downgrade_drops_the_tables_and_upgrade_creates_them(self, database_url, engine):
        _alembic(database_url, "downgrade", "020")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT to_regclass('fx_rates')")).scalar() is None
        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT to_regclass('fx_rates')")).scalar()


class TestMacroObservationsOnPostgres:
    """Revision 022: macro-economic series by period."""

    def test_a_revision_replaces_the_value_of_its_period(self, engine):
        upsert = text("""
            INSERT INTO macro_observations
                (source, indicator, geo, period, frequency, value, unit, status, fetched_at)
            VALUES ('eurostat', 'hicp', 'EA21', DATE '2026-09-01', 'monthly',
                    :value, 'ratio', :status, now())
            ON CONFLICT (source, indicator, geo, period)
            DO UPDATE SET value = EXCLUDED.value, status = EXCLUDED.status,
                          fetched_at = EXCLUDED.fetched_at
        """)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM macro_observations"))
            # The flash estimate, then the final figure a month later.
            connection.execute(upsert, {"value": "0.038", "status": "e"})
            connection.execute(upsert, {"value": "0.037", "status": None})
            rows = connection.execute(
                text("SELECT value, status FROM macro_observations")
            ).all()
            connection.execute(text("DELETE FROM macro_observations"))
        assert [(str(value), status) for value, status in rows] == [("0.03700000", None)]

    @pytest.fixture
    async def eurostat(self, database_url, engine):
        from macro.eurostat_service import EurostatService

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM macro_observations"))
            connection.execute(text("DELETE FROM macro_series_loads"))
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        downloader = MagicMock()
        yield EurostatService(async_sessionmaker(async_engine), downloader=downloader), downloader
        await async_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM macro_observations"))
            connection.execute(text("DELETE FROM macro_series_loads"))

    async def test_whole_series_then_the_last_periods_again(self, eurostat):
        from macro.eurostat_service import Observation, SeriesAnswer

        service, downloader = eurostat
        asked = []
        answers = [
            SeriesAnswer(
                [
                    Observation(date(2026, 8, 1), Decimal("0.032")),
                    Observation(date(2026, 9, 1), Decimal("0.038"), "e"),
                ],
                datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc),
            ),
            # A month later: the flash estimate of September revised, October new.
            SeriesAnswer(
                [
                    Observation(date(2026, 9, 1), Decimal("0.037")),
                    Observation(date(2026, 10, 1), Decimal("0.036"), "e"),
                ]
            ),
        ]

        async def download(item, geo, start):
            asked.append((item.name, geo, start))
            return answers[len(asked) - 1]

        downloader.side_effect = download

        first = await service.refresh("hicp", "EA")
        assert (first.status, first.geo, first.first_period, first.last_period) == (
            "fetched",
            "EA21",
            date(2026, 8, 1),
            date(2026, 9, 1),
        )
        assert asked == [("hicp", "EA21", None)]  # the whole series

        assert (await service.refresh("hicp", "EA21")).status == "fresh"  # read a moment ago

        second = await service.refresh("hicp", "EA21", force=True)
        assert asked[1] == ("hicp", "EA21", date(2024, 9, 1))  # 24 months before September
        assert (second.received, second.last_period) == (2, date(2026, 10, 1))
        assert [
            (o.period, o.value, o.status) for o in await service.observations("hicp", "EA")
        ] == [
            (date(2026, 8, 1), Decimal("0.03200000"), None),
            (date(2026, 9, 1), Decimal("0.03700000"), None),
            (date(2026, 10, 1), Decimal("0.03600000"), "e"),
        ]
        later = await service.observations("hicp", "EA", start=date(2026, 9, 1), end=date(2026, 9, 1))
        assert [o.period for o in later] == [date(2026, 9, 1)]
        latest = await service.latest("hicp", "EA21")
        assert [(o.period, o.value) for o in latest] == [
            (date(2026, 10, 1), Decimal("0.03600000")),
            (date(2026, 9, 1), Decimal("0.03700000")),
        ]

    async def test_a_quarterly_series_asks_again_its_last_eight_quarters(self, eurostat):
        from macro.eurostat_service import Observation, SeriesAnswer

        service, downloader = eurostat
        downloader.side_effect = AsyncMock(
            return_value=SeriesAnswer([Observation(date(2026, 4, 1), Decimal("0.006"))])
        )
        await service.refresh("gdp_qoq", "FR")
        await service.refresh("gdp_qoq", "FR", force=True)
        assert downloader.side_effect.await_args.args[2] == date(2024, 4, 1)

    async def test_a_failed_refresh_keeps_the_stored_periods(self, eurostat):
        from macro.eurostat_service import EurostatError, Observation, SeriesAnswer

        service, downloader = eurostat
        downloader.side_effect = AsyncMock(
            return_value=SeriesAnswer([Observation(date(2026, 8, 1), Decimal("0.082"))])
        )
        await service.refresh("unemployment", "FR")
        downloader.side_effect = EurostatError("GEO=FR is not allowed\nmore")

        failed = await service.refresh("unemployment", "FR", force=True)

        assert (failed.status, failed.last_period, failed.reason) == (
            "failed",
            date(2026, 8, 1),
            "EurostatError: GEO=FR is not allowed",
        )
        assert [o.value for o in await service.observations("unemployment", "FR")] == [
            Decimal("0.08200000")
        ]

    async def test_a_first_refresh_that_fails_leaves_nothing(self, eurostat):
        service, downloader = eurostat
        downloader.side_effect = RuntimeError()
        failed = await service.refresh("economic_sentiment", "DE")
        assert (failed.status, failed.fetched_at, failed.reason) == ("failed", None, "RuntimeError")
        assert await service.observations("economic_sentiment", "DE") == []

    def test_downgrade_drops_the_tables_and_upgrade_creates_them(self, database_url, engine):
        _alembic(database_url, "downgrade", "021")
        with engine.connect() as connection:
            for table in ("macro_observations", "macro_series_loads"):
                assert connection.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                ).scalar() is None
        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            for table in ("macro_observations", "macro_series_loads"):
                assert connection.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                ).scalar()


class TestFactorExposureReadsTheListing:
    """``FactorExposure`` reads the daily closes of one listing, by its session date."""

    async def test_closes_of_the_listing_asked_in_its_currency(self, database_url, engine):
        from factors.exposure import FactorExposure

        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO assets (id, ticker, name, currency, is_active, isin)
                    VALUES (77, 'FFX', 'Exposure test', 'EUR', true, 'FR0000000077');
                    INSERT INTO asset_listings
                        (id, asset_id, ticker, exchange, currency, source, is_primary, is_active,
                         created_at, updated_at)
                    VALUES (770, 77, 'FFX', 'XPAR', 'EUR', 'csv', true, true, now(), now());
                    INSERT INTO prices_eod
                        (time, asset_id, asset_listing_id, close, adj_close, resolution)
                    VALUES
                        ('2026-09-29 00:00+00', 77, 770, 10.0, 9.5, '1D'),
                        ('2026-09-30 00:00+00', 77, 770, 11.0, NULL, '1D'),
                        ('2026-09-30 00:00+00', 77, 770, 99.0, 99.0, '1W');
                """)
            )
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        exposure = FactorExposure(async_sessionmaker(async_engine), library=None, fx=None)
        try:
            _, listing, closes = await exposure._listing("FFX", None, None, None)
            missing = await exposure._listing("NOPE", None, None, None)
        finally:
            await async_engine.dispose()

        assert (listing["ticker"], listing["currency"], listing["isin"]) == (
            "FFX",
            "EUR",
            "FR0000000077",
        )
        # Daily bars only; the dividend-adjusted close, else the close.
        assert closes == {date(2026, 9, 29): 9.5, date(2026, 9, 30): 11.0}
        assert missing == (None, None, {})


class TestSecCikOnPostgres:
    """``financials.sec_cik``: the CIK kept in the ``SEC`` mapping of the instrument."""

    async def test_found_then_reused_and_shared_by_every_listing(self, database_url, engine):
        import json

        from financials.sec_cik import NOT_FOUND, SecCikResolver, SqlCikStore

        table = json.loads(
            (PROJECT_ROOT / "tests" / "fixtures" / "sec" / "company_tickers_exchange.json").read_text()
        )
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO assets (id, ticker, name, currency, is_active, isin, quote_type)
                    VALUES (88, 'SAP', 'SAP SE', 'EUR', true, 'DE0007164600', 'EQUITY'),
                           (89, 'AIR', 'Airbus SE', 'EUR', true, 'NL0000235190', 'EQUITY');
                    INSERT INTO asset_listings
                        (id, asset_id, ticker, exchange, currency, source, is_primary, is_active,
                         created_at, updated_at)
                    VALUES (880, 88, 'SAP', 'XETR', 'EUR', 'csv', true, true, now(), now()),
                           (881, 88, 'SAP', 'NYSE', 'USD', 'csv', false, true, now(), now()),
                           (890, 89, 'AIR', 'XPAR', 'EUR', 'csv', true, true, now(), now());
                """)
            )
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        source = AsyncMock(return_value=table)
        resolver = SecCikResolver(SqlCikStore(async_sessionmaker(async_engine)), source)
        try:
            sap = await resolver.resolve(88)
            again = await resolver.resolve(88)
            airbus = await resolver.resolve(89)
            # A CIK no longer confirmed is switched off.
            source.return_value = {"fields": table["fields"], "data": []}
            lost = await resolver.resolve(88, refresh=True)
        finally:
            await async_engine.dispose()

        assert sap.cik == again.cik == "0001000184"
        assert airbus.cik is None and source.await_count == 2  # Airbus: the SEC is not asked
        assert lost.cik is None and lost.origin == NOT_FOUND
        with engine.begin() as connection:
            rows = connection.execute(
                text("""
                    SELECT asset_id, asset_listing_id, provider_ticker, source, is_active,
                           last_verified_at IS NULL, failure_count
                    FROM asset_mappings WHERE provider_name = 'SEC' ORDER BY asset_id
                """)
            ).all()
            connection.execute(text("DELETE FROM asset_mappings WHERE asset_id IN (88, 89)"))
            connection.execute(text("DELETE FROM asset_listings WHERE asset_id IN (88, 89)"))
            connection.execute(text("DELETE FROM assets WHERE id IN (88, 89)"))
        # One row for the instrument (no listing), kept with the last CIK, switched off.
        assert [tuple(row) for row in rows] == [
            (88, None, "0001000184", NOT_FOUND, False, True, 1)
        ]


class TestXbrlFactsOnPostgres:
    """Revision 023: the facts of the XBRL filings of the SEC."""

    UPSERT = text("""
        INSERT INTO xbrl_facts
            (cik, taxonomy, concept, unit, period_end, duration_days, period_start, value,
             accession, form, fiscal_year, fiscal_period, filed, frame)
        VALUES ('0001000184', 'ifrs-full', 'Assets', 'EUR', DATE '2024-12-31', 0, NULL,
                :value, :accession, '20-F', :fy, 'FY', :filed, NULL)
        ON CONFLICT (cik, taxonomy, concept, unit, period_end, duration_days)
        DO UPDATE SET value = EXCLUDED.value, accession = EXCLUDED.accession,
                      fiscal_year = EXCLUDED.fiscal_year, filed = EXCLUDED.filed
        WHERE EXCLUDED.filed >= xbrl_facts.filed
    """)

    def test_a_restatement_replaces_the_first_figure_and_an_older_filing_does_not(self, engine):
        first = {"value": 74122000000, "accession": "0001104659-25-017815", "fy": 2024,
                 "filed": date(2025, 2, 27)}
        restated = {"value": 74237000000, "accession": "0001104659-26-020058", "fy": 2025,
                    "filed": date(2026, 2, 26)}
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM xbrl_facts"))
            connection.execute(self.UPSERT, first)
            connection.execute(self.UPSERT, restated)
            connection.execute(self.UPSERT, first)  # read again from the older filing
            rows = connection.execute(
                text("SELECT value, accession, filed FROM xbrl_facts")
            ).all()
            connection.execute(text("DELETE FROM xbrl_facts"))
        assert [(int(value), accession, filed) for value, accession, filed in rows] == [
            (74237000000, "0001104659-26-020058", date(2026, 2, 26))
        ]

    def test_a_year_and_a_quarter_ending_the_same_day_are_two_facts(self, engine):
        insert = text("""
            INSERT INTO xbrl_facts (cik, taxonomy, concept, unit, period_end, duration_days,
                                    period_start, value, accession, form, filed)
            VALUES ('0000320193', 'us-gaap', 'Revenues', 'USD', DATE '2025-09-27', :days,
                    :start, :value, '0000320193-25-000079', '10-K', DATE '2025-10-31')
        """)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM xbrl_facts"))
            connection.execute(insert, {"days": 363, "start": date(2024, 9, 29), "value": 416161000000})
            connection.execute(insert, {"days": 90, "start": date(2025, 6, 29), "value": 100000000000})
            count = connection.execute(text("SELECT count(*) FROM xbrl_facts")).scalar()
            connection.execute(text("DELETE FROM xbrl_facts"))
        assert count == 2

    def test_downgrade_drops_the_tables_and_upgrade_creates_them(self, database_url, engine):
        _alembic(database_url, "downgrade", "022")
        with engine.connect() as connection:
            for table in ("xbrl_facts", "sec_company_loads"):
                assert connection.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                ).scalar() is None
        _alembic(database_url, "upgrade", "head")
        with engine.connect() as connection:
            for table in ("xbrl_facts", "sec_company_loads"):
                assert connection.execute(
                    text("SELECT to_regclass(:table)"), {"table": table}
                ).scalar()


class TestSecStatementsOnPostgres:
    """``financials.sec_statements``: facts of the SEC stored, then read as fiscal years."""

    @pytest.fixture
    async def statements(self, database_url, engine):
        from financials.sec_cik import CikResolution
        from financials.sec_statements import SecStatements

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM xbrl_facts"))
            connection.execute(text("DELETE FROM sec_company_loads"))
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        provider, resolver = MagicMock(), MagicMock()
        resolver.resolve = AsyncMock(
            return_value=CikResolution("0001000184", origin="ticker_check", company_name="SAP SE")
        )
        provider.company_facts = AsyncMock()
        yield SecStatements(async_sessionmaker(async_engine), provider, resolver), provider, resolver
        await async_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM xbrl_facts"))
            connection.execute(text("DELETE FROM sec_company_loads"))

    async def test_read_stored_then_reused_and_read_as_fiscal_years(self, statements):
        import json

        service, provider, _ = statements
        answer = json.loads(
            (PROJECT_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_sap.json").read_text()
        )
        provider.company_facts.return_value = answer

        first = await service.refresh(1)
        assert (first.status, first.cik, first.entity_name, first.last_filed) == (
            "fetched",
            "0001000184",
            "SAP SE",
            date(2026, 2, 26),
        )
        assert first.facts == 26  # 13 concepts, two fiscal years, latest filing each
        assert (await service.refresh(1)).status == "fresh"
        provider.company_facts.assert_awaited_once_with("0001000184")

        load, years = await service.annual(1, refresh=False)
        assert load.status == "fresh"
        assert [year.period_end for year in years] == [date(2025, 12, 31), date(2024, 12, 31)]
        assert years[0].currency == "EUR"
        assert years[0].value("revenue") == Decimal("36800000000")
        assert years[1].value("total_assets") == Decimal("74237000000")  # restated

    async def test_an_older_filing_read_later_does_not_replace_a_restatement(self, statements):
        service, provider, _ = statements
        restated = {"start": None, "end": "2024-12-31", "accn": "0001104659-26-020058",
                    "form": "20-F", "filed": "2026-02-26", "fy": 2025, "fp": "FY"}
        first = {**restated, "accn": "0001104659-25-017815", "filed": "2025-02-27", "fy": 2024}

        def facts(*entries):
            return {"entityName": "SAP SE", "facts": {"ifrs-full": {"Assets": {"units": {"EUR": [
                {key: value for key, value in entry.items() if value is not None} for entry in entries
            ]}}}}}

        provider.company_facts.return_value = facts({**restated, "val": 74237000000})
        await service.refresh(1, force=True)
        provider.company_facts.return_value = facts({**first, "val": 74122000000})
        await service.refresh(1, force=True)

        (row,) = await service.stored_rows("0001000184")
        assert (row.value, row.accession) == (Decimal("74237000000.000000"), "0001104659-26-020058")

    async def test_a_failed_reading_keeps_what_was_stored(self, statements):
        service, provider, _ = statements
        provider.company_facts.return_value = {"entityName": "SAP SE", "facts": {}}
        await service.refresh(1, force=True)
        provider.company_facts.return_value = None

        failed = await service.refresh(1, force=True)

        assert (failed.status, failed.entity_name, failed.reason) == (
            "failed",
            "SAP SE",
            "XbrlError: no answer from the SEC",
        )

    async def test_without_cik_the_sec_is_not_asked(self, statements):
        from financials.sec_cik import CikResolution

        service, provider, resolver = statements
        resolver.resolve.return_value = CikResolution(None, "No US listing in the catalogue")

        load, years = await service.annual(1)

        assert (load.status, load.reason, years) == ("no_cik", "No US listing in the catalogue", [])
        provider.company_facts.assert_not_awaited()


class TestSecCheckOnPostgres:
    """``financials.sec_check``: the Yahoo statements read by fiscal year from the database."""

    async def test_the_three_rows_of_a_year_are_read_together(self, database_url, engine):
        import json
        from types import SimpleNamespace

        from financials.sec_check import MATCH, SecCheck
        from financials.xbrl_mapping import annual_statements, facts_to_rows

        answer = json.loads(
            (PROJECT_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_aapl.json").read_text()
        )
        sec_years = annual_statements(facts_to_rows("0000320193", answer))
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO assets (id, ticker, name, currency, is_active, isin, quote_type)
                    VALUES (91, 'AAPL', 'Apple Inc.', 'USD', true, 'US0000000091', 'EQUITY');
                    INSERT INTO financial_statements
                        (asset_id, statement_type, period_type, period_end, currency,
                         revenue, net_income, total_assets, operating_cashflow, capex)
                    VALUES
                        (91, 'income', 'annual', DATE '2025-09-30', 'USD',
                         416161000000, 112010000000, NULL, NULL, NULL),
                        (91, 'balance', 'annual', DATE '2025-09-30', 'USD',
                         NULL, NULL, 359241000000, NULL, NULL),
                        (91, 'cashflow', 'annual', DATE '2025-09-30', 'USD',
                         NULL, NULL, NULL, 111482000000, -12715000000),
                        (91, 'income', 'quarterly', DATE '2025-09-30', 'USD',
                         1, NULL, NULL, NULL, NULL);
                """)
            )
        async_engine = create_async_engine(
            database_url.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool
        )
        statements = MagicMock()
        statements.annual = AsyncMock(return_value=(SimpleNamespace(status="fresh"), sec_years))
        try:
            result = await SecCheck(async_sessionmaker(async_engine), statements).check(91)
        finally:
            await async_engine.dispose()
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM financial_statements WHERE asset_id = 91"))
                connection.execute(text("DELETE FROM assets WHERE id = 91"))

        latest = result.years[0]
        figures = {figure.column: figure.status for figure in latest.figures}
        assert latest.yahoo_period_end == date(2025, 9, 30)
        # One row per statement, read together; the quarterly row is not an annual figure.
        for column in ("revenue", "net_income", "total_assets", "operating_cashflow", "capex"):
            assert figures[column] == MATCH, column
