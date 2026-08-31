"""Unit tests for the Zipline bundle data source and ingest orchestration.

The tests deliberately avoid importing ``zipline`` itself: the bundle can
be exercised end-to-end with fake writers as long as we call
``FonRexBundle.ingest`` directly. This keeps the CI matrix lean and lets us
validate the SQL extraction, session alignment and metadata generation
without pulling in bcolz/exchange-calendars.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Asset, AssetListing, Base, PriceEOD
from zipline_bundle import FonRexBundle, FonRexBundleDataSource, fonrex_equities
from zipline_bundle.bundle import _build_metadata_frame, _empty_adjustment_frames
from zipline_bundle.data_source import OHLCV_COLUMNS


def _make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _seed_asset(session, *, asset_id, ticker, listings=(), rows=()):
    session.add(
        Asset(
            id=asset_id,
            ticker=ticker,
            name=f"Company {ticker}",
            exchange="NASDAQ",
            currency="USD",
            is_active=True,
        )
    )
    session.commit()
    listing_id_map = {}
    for spec in listings:
        listing = AssetListing(
            asset_id=asset_id,
            ticker=spec["ticker"],
            exchange=spec.get("exchange", "NASDAQ"),
            currency=spec.get("currency", "USD"),
            source=spec.get("source", "manual"),
            is_primary=spec.get("is_primary", False),
            is_active=spec.get("is_active", True),
        )
        session.add(listing)
        session.commit()
        listing_id_map[spec["ticker"]] = listing.id
    for row in rows:
        session.add(
            PriceEOD(
                timestamp=row["timestamp"],
                asset_id=asset_id,
                asset_listing_id=row.get("asset_listing_id"),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
                close=row.get("close"),
                adj_close=row.get("adj_close"),
                volume=row.get("volume", 0),
                resolution=row.get("resolution", "1D"),
                adjusted=row.get("adjusted", True),
                source=row.get("source", "yfinance"),
            )
        )
    session.commit()
    return listing_id_map


class FakeDailyBarWriter:
    """Minimal capture writer that records ``(sid, DataFrame)`` pairs."""

    def __init__(self):
        self.written = []

    def write(self, data, show_progress=False, **_kwargs):
        for sid, frame in data:
            self.written.append((sid, frame.copy()))


class FakeAssetDBWriter:
    def __init__(self):
        self.equities = None

    def write(self, equities=None, **_kwargs):
        self.equities = None if equities is None else equities.copy()


class FakeAdjustmentWriter:
    def __init__(self):
        self.splits = None
        self.dividends = None

    def write(self, splits=None, dividends=None, **_kwargs):
        self.splits = None if splits is None else splits.copy()
        self.dividends = None if dividends is None else dividends.copy()


class FakeCalendar:
    """Zipline-compatible calendar exposing ``sessions_in_range``."""

    def __init__(self, sessions):
        self._sessions = pd.DatetimeIndex(sessions)

    def sessions_in_range(self, start, end):
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        mask = (self._sessions >= start_ts) & (self._sessions <= end_ts)
        return self._sessions[mask]


class DataSourceTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        Base.metadata.create_all(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        session = self.Session()
        # Two assets: AAPL (with primary listing + secondary), MSFT (no listing).
        _seed_asset(
            session,
            asset_id=1,
            ticker="AAPL",
            listings=[
                {"ticker": "AAPL", "exchange": "NASDAQ", "currency": "USD", "is_primary": True},
                {"ticker": "AAPL", "exchange": "XETRA", "currency": "EUR", "is_primary": False},
            ],
            rows=[
                {
                    "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "adj_close": 101.0,
                    "volume": 1_000_000,
                },
                {
                    "timestamp": datetime(2024, 1, 3, tzinfo=timezone.utc),
                    "open": 101.0,
                    "high": 104.0,
                    "low": 100.5,
                    "close": 103.0,
                    "adj_close": 102.5,
                    "volume": 900_000,
                },
                {
                    "timestamp": datetime(2024, 1, 5, tzinfo=timezone.utc),  # a Friday
                    "open": 103.0,
                    "high": 105.0,
                    "low": 102.0,
                    "close": 104.0,
                    "adj_close": 104.0,
                    "volume": None,  # exercise NaN volume normalisation
                },
            ],
        )
        _seed_asset(
            session,
            asset_id=2,
            ticker="MSFT",
            listings=(),
            rows=[
                {
                    "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                    "open": 300.0,
                    "high": 305.0,
                    "low": 299.0,
                    "close": 304.0,
                    "adj_close": 304.0,
                    "volume": 500_000,
                },
            ],
        )
        session.close()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_iter_tickers_returns_ohlcv_frames_and_metadata(self):
        with FonRexBundleDataSource(engine=self.engine) as source:
            bars = list(
                source.iter_tickers(
                    start_session=pd.Timestamp("2024-01-01"),
                    end_session=pd.Timestamp("2024-01-31"),
                )
            )
        symbols = [entry.metadata.symbol for entry in bars]
        self.assertEqual(symbols, ["AAPL", "MSFT"])

        aapl = bars[0]
        self.assertEqual(list(aapl.frame.columns), list(OHLCV_COLUMNS))
        self.assertEqual(aapl.metadata.sid, 0)
        self.assertEqual(aapl.metadata.exchange, "NASDAQ")
        self.assertEqual(aapl.metadata.start_date, pd.Timestamp("2024-01-02"))
        self.assertEqual(aapl.metadata.end_date, pd.Timestamp("2024-01-05"))
        self.assertEqual(aapl.metadata.auto_close_date, pd.Timestamp("2024-01-06"))
        # adj_close wins over close when available.
        self.assertEqual(aapl.frame.loc[pd.Timestamp("2024-01-03"), "close"], 102.5)
        # NaN volume is coerced to 0 float.
        self.assertEqual(aapl.frame.loc[pd.Timestamp("2024-01-05"), "volume"], 0.0)

    def test_iter_tickers_filters_by_ticker_whitelist(self):
        with FonRexBundleDataSource(engine=self.engine, tickers=["msft"]) as source:
            bars = list(
                source.iter_tickers(
                    start_session=pd.Timestamp("2024-01-01"),
                    end_session=pd.Timestamp("2024-01-31"),
                )
            )
        self.assertEqual([entry.metadata.symbol for entry in bars], ["MSFT"])
        self.assertEqual(bars[0].metadata.sid, 0)

    def test_iter_tickers_reindexes_on_allowed_sessions(self):
        allowed = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])  # drops 2024-01-05
        with FonRexBundleDataSource(engine=self.engine, tickers=["AAPL"]) as source:
            bars = list(
                source.iter_tickers(
                    start_session=pd.Timestamp("2024-01-01"),
                    end_session=pd.Timestamp("2024-01-31"),
                    allowed_sessions=allowed,
                )
            )
        self.assertEqual(len(bars[0].frame), 2)
        self.assertNotIn(pd.Timestamp("2024-01-05"), bars[0].frame.index)

    def test_iter_tickers_skips_asset_without_prices_in_window(self):
        with FonRexBundleDataSource(engine=self.engine) as source:
            bars = list(
                source.iter_tickers(
                    start_session=pd.Timestamp("2020-01-01"),
                    end_session=pd.Timestamp("2020-12-31"),
                )
            )
        self.assertEqual(bars, [])

    def test_close_disposes_the_engine_only_when_owned(self):
        external_engine = _make_engine()
        Base.metadata.create_all(external_engine)
        try:
            with FonRexBundleDataSource(engine=external_engine) as source:
                self.assertFalse(source._owns_engine)
            # Engine should still work after the source is closed.
            with external_engine.connect() as conn:
                conn.execute(Asset.__table__.select())
        finally:
            external_engine.dispose()


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()
        Base.metadata.create_all(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        session = self.Session()
        _seed_asset(
            session,
            asset_id=1,
            ticker="AAPL",
            listings=[{"ticker": "AAPL", "exchange": "NASDAQ", "currency": "USD", "is_primary": True}],
            rows=[
                {
                    "timestamp": datetime(2024, 1, 2, tzinfo=timezone.utc),
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "adj_close": 101.0,
                    "volume": 1_000_000,
                },
                {
                    "timestamp": datetime(2024, 1, 3, tzinfo=timezone.utc),
                    "open": 101.0,
                    "high": 104.0,
                    "low": 100.5,
                    "close": 103.0,
                    "adj_close": 102.5,
                    "volume": 900_000,
                },
            ],
        )
        session.close()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _run_bundle(self, *, calendar=None, tickers=None):
        asset_writer = FakeAssetDBWriter()
        daily_writer = FakeDailyBarWriter()
        adjustment_writer = FakeAdjustmentWriter()

        bundle = FonRexBundle(tickers=tickers)
        # Substitute the SQL data source with one backed by the in-memory engine.
        original_source = bundle.__class__

        def _fake_source(*_a, **_kw):
            return FonRexBundleDataSource(engine=self.engine, tickers=tickers)

        bundle_module = sys.modules["zipline_bundle.bundle"]
        original = bundle_module.FonRexBundleDataSource
        bundle_module.FonRexBundleDataSource = _fake_source
        try:
            bundle.ingest(
                environ={},
                asset_db_writer=asset_writer,
                minute_bar_writer=MagicMock(),
                daily_bar_writer=daily_writer,
                adjustment_writer=adjustment_writer,
                calendar=calendar,
                start_session=pd.Timestamp("2024-01-01"),
                end_session=pd.Timestamp("2024-01-31"),
                cache=MagicMock(),
                show_progress=False,
                output_dir="/tmp/fonrex-bundle",
            )
        finally:
            bundle_module.FonRexBundleDataSource = original
            del original_source
        return asset_writer, daily_writer, adjustment_writer

    def test_ingest_writes_equities_bars_and_empty_adjustments(self):
        asset_writer, daily_writer, adjustment_writer = self._run_bundle()

        self.assertIsNotNone(asset_writer.equities)
        self.assertEqual(list(asset_writer.equities["symbol"]), ["AAPL"])
        self.assertEqual(list(asset_writer.equities["exchange"]), ["NASDAQ"])
        self.assertEqual(asset_writer.equities.loc[0, "start_date"], pd.Timestamp("2024-01-02"))
        self.assertEqual(asset_writer.equities.loc[0, "auto_close_date"], pd.Timestamp("2024-01-04"))

        self.assertEqual(len(daily_writer.written), 1)
        sid, frame = daily_writer.written[0]
        self.assertEqual(sid, 0)
        self.assertEqual(list(frame.columns), list(OHLCV_COLUMNS))
        self.assertEqual(len(frame), 2)

        self.assertTrue(adjustment_writer.splits.empty)
        self.assertTrue(adjustment_writer.dividends.empty)
        self.assertEqual(list(adjustment_writer.splits.columns), ["sid", "ratio", "effective_date"])

    def test_ingest_uses_calendar_sessions_when_available(self):
        calendar = FakeCalendar(["2024-01-02"])  # drops the 2024-01-03 row
        _, daily_writer, _ = self._run_bundle(calendar=calendar)
        _, frame = daily_writer.written[0]
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.index[0], pd.Timestamp("2024-01-02"))

    def test_ingest_produces_empty_writes_when_no_data(self):
        asset_writer, daily_writer, adjustment_writer = self._run_bundle(tickers=["ZZZZ"])
        self.assertEqual(daily_writer.written, [])
        self.assertTrue(asset_writer.equities.empty)
        self.assertTrue(adjustment_writer.splits.empty)


class HelpersTests(unittest.TestCase):
    def test_empty_adjustment_frames_have_expected_columns(self):
        frames = _empty_adjustment_frames()
        self.assertEqual(list(frames["splits"].columns), ["sid", "ratio", "effective_date"])
        self.assertEqual(
            list(frames["dividends"].columns),
            ["sid", "amount", "ex_date", "record_date", "declared_date", "pay_date"],
        )
        self.assertTrue(frames["splits"].empty)
        self.assertTrue(frames["dividends"].empty)

    def test_build_metadata_frame_supports_empty_input(self):
        frame = _build_metadata_frame([])
        self.assertEqual(
            list(frame.columns),
            ["start_date", "end_date", "auto_close_date", "symbol", "exchange"],
        )
        self.assertTrue(frame.empty)

    def test_fonrex_equities_factory_returns_bundle_ingest_callable(self):
        callable_ingest = fonrex_equities(tickers=["AAPL"])
        self.assertTrue(callable(callable_ingest))
        # Retrieve the owning bundle by inspecting the bound method.
        self.assertIsInstance(callable_ingest.__self__, FonRexBundle)


if __name__ == "__main__":
    unittest.main()
