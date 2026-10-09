"""A stored price series stays on one adjustment basis.

Yahoo adjusts a whole history again after each split and each dividend. The
ingestion used to store Yahoo's dividend-adjusted prices in ``close`` and
``adj_close``, then add new bars to them: after a dividend or a split the new
bars and the stored ones were adjusted on two bases, and a false return appeared
where they met (about minus the dividend yield, -75 % after a 4-for-1 split).

These tests guard the fix: the traded close and the adjusted close are stored
separately, and a series whose stored bars no longer match the source is fetched
again in one piece.
"""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.price_series import session_date, session_timestamp
from historical.adjustment import (
    ADJUSTMENT_SCHEME,
    OVERLAP_SESSIONS,
    adjustment_changed,
    record_series_scheme,
)
from historical.ingestion_service import HistoricalIngestionService
from historical.normalization import normalize_bars
from historical.providers import HistoricalMarketDataFetcher
from historical.yahoo_symbols import SymbolResolution
from models import Asset, AssetListing, Base, PriceEOD, PriceSeriesAdjustment

LISTING, ASSET = 20, 2
SESSIONS = list(pd.bdate_range("2024-01-02", "2024-03-29").date)  # 63 sessions


# ── the source ────────────────────────────────────────────────────────────────
class TestYahooBars:
    def _fetch(self, history):
        with patch("historical.providers.yf.Ticker") as ticker:
            ticker.return_value.history.return_value = history
            result = HistoricalMarketDataFetcher()._fetch_yfinance_sync(
                "AIR.PA", "1D", date(2024, 1, 1), date(2024, 1, 31)
            )
        return ticker.return_value.history, result["bars"]

    def test_traded_close_and_adjusted_close_are_kept_apart(self):
        history = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Adj Close": [98.25],
                "Volume": [10],
            },
            index=pd.DatetimeIndex(["2024-01-08"]).tz_localize("Europe/Paris"),
        )
        call, bars = self._fetch(history)

        assert call.call_args.kwargs["auto_adjust"] is False
        assert (bars[0]["close"], bars[0]["adj_close"]) == (100.5, 98.25)

    def test_close_stands_in_for_a_missing_adjusted_close(self):
        history = pd.DataFrame(
            {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.5], "Volume": [1]},
            index=pd.DatetimeIndex(["2024-01-08"]),
        )
        _, bars = self._fetch(history)
        assert bars[0]["adj_close"] == 1.5

    def test_tradingview_bar_has_no_adjusted_close(self):
        opening = 1704724200  # 2024-01-08 14:30 UTC
        message = (
            '~m~1~m~{"m":"timescale_update","p":["cs",{"sds_1":{"s":'
            f'[{{"i":0,"v":[{opening},1.0,2.0,0.5,1.5,10]}}]}}}}]}}'
        )
        bars: list[dict] = []
        HistoricalMarketDataFetcher._append_tradingview_bars(
            message, date(2024, 1, 1), date(2024, 1, 31), bars
        )
        assert bars[0]["close"] == 1.5
        assert bars[0]["adj_close"] is None

    def test_normalization_keeps_the_adjusted_close_or_none(self):
        bars = [
            {**_raw(SESSIONS[0], 10.0), "adj_close": 9.5},
            {**_raw(SESSIONS[1], 10.0), "adj_close": float("nan")},
            {key: value for key, value in _raw(SESSIONS[2], 10.0).items() if key != "adj_close"},
        ]
        normalized = normalize_bars(bars, ASSET, LISTING, "1D")
        assert [bar["adj_close"] for bar in normalized] == [9.5, None, None]


# ── the comparison ────────────────────────────────────────────────────────────
class TestAdjustmentChanged:
    STORED = [{"time": session_timestamp(SESSIONS[0]), "close": 100.0, "adj_close": 95.0}]

    def _fresh(self, close, adj_close):
        return [{"time": session_timestamp(SESSIONS[0]), "close": close, "adj_close": adj_close}]

    def test_same_prices_are_no_change(self):
        assert not adjustment_changed(self.STORED, self._fresh(100.0, 95.0))
        assert not adjustment_changed(self.STORED, self._fresh(100.000001, 95.000001))

    def test_a_split_changes_the_close(self):
        assert adjustment_changed(self.STORED, self._fresh(25.0, 23.75))

    def test_a_dividend_changes_the_adjusted_close_only(self):
        assert adjustment_changed(self.STORED, self._fresh(100.0, 94.5))

    def test_sessions_on_one_side_only_are_not_compared(self):
        other_day = [{"time": session_timestamp(SESSIONS[1]), "close": 1.0, "adj_close": 1.0}]
        assert not adjustment_changed(self.STORED, other_day)

    def test_a_source_without_adjusted_close_is_compared_on_the_close(self):
        assert not adjustment_changed(self.STORED, self._fresh(100.0, None))

    def test_a_stored_bar_without_adjusted_close_is_fetched_again_once_one_is_known(self):
        tradingview = [{"time": session_timestamp(SESSIONS[0]), "close": 100.0, "adj_close": None}]
        assert adjustment_changed(tradingview, self._fresh(100.0, 95.0))


# ── the ingestion ─────────────────────────────────────────────────────────────
def _raw(day: date, close: float, adj_close: float | None = None) -> dict:
    return {
        "timestamp": session_timestamp(day),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close if adj_close is None else adj_close,
        "volume": 100,
        "source": "yfinance",
    }


class Market:
    """What the source answers: one price per session, adjusted as of today."""

    def __init__(self):
        self.close = {day: 100.0 + index for index, day in enumerate(SESSIONS)}
        self.factor = dict.fromkeys(SESSIONS, 1.0)  # dividend adjustment of adj_close
        self.calls: list[tuple[date, date]] = []
        self.down = False

    def dividend(self, ex_date: date, ratio: float):
        for day in SESSIONS:
            if day < ex_date:
                self.factor[day] *= ratio

    def split(self, day_of_split: date, ratio: int):
        for day in SESSIONS:
            if day < day_of_split:
                self.close[day] /= ratio

    async def fetch(self, ticker, resolution, source, start, end, **kwargs):
        self.calls.append((start, end))
        if self.down:
            return {"bars": [], "source_used": "yfinance", "error": "Yahoo is down"}
        bars = [
            _raw(day, round(self.close[day], 6), round(self.close[day] * self.factor[day], 6))
            for day in SESSIONS
            if start <= day <= end
        ]
        return {"bars": bars, "source_used": "yfinance", "symbol": "AIR.PA"}


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(Asset(id=ASSET, ticker="AIR", name="Airbus", isin="NL0000235190"))
    session.add(
        AssetListing(id=LISTING, asset_id=ASSET, ticker="AIR.PA", exchange="XPAR", currency="EUR")
    )
    session.commit()
    session.close()
    yield session_factory
    engine.dispose()


@pytest.fixture
def market():
    return Market()


@pytest.fixture
def ingestion(factory, market):
    database = MagicMock()
    database.get_session.side_effect = factory

    async def history_range(ticker, resolution, currency=None, exchange=None):
        session = factory()
        days = [session_date(row[0]) for row in session.execute(select(PriceEOD.timestamp))]
        session.close()
        if not days:
            return {"min_date": None, "max_date": None, "count": 0}
        return {"min_date": min(days), "max_date": max(days), "count": len(days)}

    query = MagicMock()
    query.get_history_range = AsyncMock(side_effect=history_range)
    symbols = MagicMock()
    symbols.resolve = AsyncMock(return_value=SymbolResolution("AIR.PA", origin="manual"))
    service = HistoricalIngestionService(
        db_service=database, query_service=query, symbol_resolver=symbols
    )
    service._fetch_with_fallback = market.fetch
    service._log_ingest = AsyncMock()
    return service


def _ingest(service, last: date, **kwargs):
    return asyncio.run(
        service.ingest("AIR.PA", from_date=kwargs.pop("first", SESSIONS[0]), to_date=last, **kwargs)
    )


def _stored(factory) -> dict[date, tuple[float, float]]:
    session = factory()
    rows = session.execute(
        select(PriceEOD.timestamp, PriceEOD.close, PriceEOD.adj_close).order_by(PriceEOD.timestamp)
    ).all()
    session.close()
    return {session_date(time): (close, adj) for time, close, adj in rows}


def _scheme(factory):
    session = factory()
    row = session.get(PriceSeriesAdjustment, (LISTING, "1D"))
    session.close()
    return row.scheme if row else None


class TestIngestionKeepsOneBasis:
    def test_first_ingestion_records_the_scheme_of_the_series(self, ingestion, factory, market):
        result = _ingest(ingestion, SESSIONS[29])

        assert result.status == "success"
        assert len(_stored(factory)) == 30
        assert _scheme(factory) == ADJUSTMENT_SCHEME
        assert market.calls == [(SESSIONS[0], SESSIONS[29])]

    def test_completing_an_unchanged_series_writes_only_the_new_sessions(
        self, ingestion, factory, market
    ):
        _ingest(ingestion, SESSIONS[29])
        ingestion._upsert_prices_eod = AsyncMock(wraps=ingestion._upsert_prices_eod)

        result = _ingest(ingestion, SESSIONS[39])

        # The last stored bars were fetched again to be compared, not rewritten.
        assert market.calls[-1] == (SESSIONS[30 - OVERLAP_SESSIONS], SESSIONS[39])
        assert result.records_added == 10
        assert result.note is None
        assert ingestion._upsert_prices_eod.await_args.kwargs == {"replace": False}
        assert len(_stored(factory)) == 40

    def test_a_dividend_since_the_last_ingestion_fetches_the_whole_series_again(
        self, ingestion, factory, market
    ):
        _ingest(ingestion, SESSIONS[29])
        market.dividend(SESSIONS[35], 0.98)  # a 2 % dividend paid after the ingestion

        result = _ingest(ingestion, SESSIONS[39])

        assert result.status == "success"
        assert "Whole history fetched again" in result.note
        assert market.calls[-1] == (SESSIONS[0], SESSIONS[39])
        stored = _stored(factory)
        # Every stored bar is adjusted as the source adjusts it today...
        for day in SESSIONS[:40]:
            assert stored[day][1] == pytest.approx(market.close[day] * market.factor[day])
        # ... so no false return where the old and the new bars meet (30th to 31st).
        junction = stored[SESSIONS[30]][1] / stored[SESSIONS[29]][1] - 1
        assert junction == pytest.approx(130 / 129 - 1)  # the market move, nothing else

    def test_a_split_since_the_last_ingestion_leaves_no_false_drop(
        self, ingestion, factory, market
    ):
        _ingest(ingestion, SESSIONS[29])
        market.split(SESSIONS[35], 4)

        result = _ingest(ingestion, SESSIONS[39])

        assert "split or dividend" in result.note
        stored = _stored(factory)
        assert stored[SESSIONS[0]][0] == pytest.approx(market.close[SESSIONS[0]])
        # Before the fix the series fell by 75 % between the 30th and the 31st session.
        moves = [
            stored[after][0] / stored[before][0] - 1
            for before, after in zip(SESSIONS[:39], SESSIONS[1:40], strict=False)
            if after != SESSIONS[35]
        ]
        assert max(abs(move) for move in moves) < 0.05

    def test_a_series_stored_before_the_scheme_is_fetched_again_in_full(
        self, ingestion, factory, market
    ):
        # As stored before the fix: the dividend-adjusted price as close, no scheme.
        session = factory()
        legacy = normalize_bars(
            [_raw(day, 90.0 + i, 90.0 + i) for i, day in enumerate(SESSIONS[:30])],
            ASSET,
            LISTING,
            "1D",
        )
        ingestion._upsert_prices_eod_sync(legacy)
        session.close()

        result = _ingest(ingestion, SESSIONS[39])

        assert "stored before the adjustment scheme was recorded" in result.note
        assert market.calls == [(SESSIONS[0], SESSIONS[39])]
        assert _stored(factory)[SESSIONS[0]][0] == market.close[SESSIONS[0]]
        assert _scheme(factory) == ADJUSTMENT_SCHEME

    def test_nothing_is_written_when_the_series_cannot_be_fetched_again(
        self, ingestion, factory, market
    ):
        _ingest(ingestion, SESSIONS[29])
        before = _stored(factory)
        market.dividend(SESSIONS[35], 0.98)
        fetch = market.fetch

        async def down_after_first_call(*args, **kwargs):
            answer = await fetch(*args, **kwargs)
            market.down = True
            return answer

        ingestion._fetch_with_fallback = down_after_first_call

        result = _ingest(ingestion, SESSIONS[39])

        assert result.status == "failed"
        assert "could not be fetched again: nothing was written" in result.error
        assert "Yahoo is down" in result.error
        assert _stored(factory) == before

    def test_older_sessions_are_checked_against_the_first_stored_bars(
        self, ingestion, factory, market
    ):
        _ingest(ingestion, SESSIONS[59], first=SESSIONS[30])
        today = SESSIONS[59] + timedelta(days=1)

        with patch("historical.ingestion_service.date") as clock:
            clock.today.return_value = today
            result = _ingest(ingestion, SESSIONS[59], first=SESSIONS[10])

        assert market.calls[-1] == (SESSIONS[10], SESSIONS[30 + OVERLAP_SESSIONS - 1])
        assert result.records_added == 20
        assert len(_stored(factory)) == 50

    def test_forced_refresh_fetches_the_stored_range_too(self, ingestion, factory, market):
        _ingest(ingestion, SESSIONS[59])

        result = _ingest(ingestion, SESSIONS[59], first=SESSIONS[40], force_refresh=True)

        assert market.calls[-1] == (SESSIONS[0], SESSIONS[59])
        assert "forced refresh" in result.note
        assert len(_stored(factory)) == 60


def test_recording_the_scheme_twice_keeps_one_row(factory):
    session = factory()
    record_series_scheme(session, LISTING, "1D")
    record_series_scheme(session, LISTING, "1D")
    session.commit()
    assert session.query(PriceSeriesAdjustment).count() == 1
    session.close()
