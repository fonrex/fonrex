"""Tests for extracted historical market-data providers."""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from historical.providers import HistoricalMarketDataFetcher


@pytest.mark.asyncio
async def test_yfinance_failure_is_returned_as_provider_error():
    fetcher = HistoricalMarketDataFetcher()
    with patch.object(fetcher, "_fetch_yfinance_sync", side_effect=RuntimeError("offline")):
        result = await fetcher.fetch_yfinance(
            "AAPL",
            "1D",
            date(2026, 1, 1),
            date(2026, 1, 2),
        )
    assert result["bars"] == []
    assert result["source_used"] == "yfinance"
    assert result["error"] == "offline"


def test_yfinance_empty_history_returns_no_bars():
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    with patch("historical.providers.yf.Ticker", return_value=ticker):
        result = HistoricalMarketDataFetcher()._fetch_yfinance_sync(
            "AAPL",
            "1W",
            date(2026, 1, 1),
            date(2026, 1, 2),
        )
    assert result == {"bars": [], "source_used": "yfinance", "symbol": "AAPL"}


def test_yfinance_prices_lose_the_noise_of_their_32_bit_origin():
    """Yahoo sends 31.378 as 31.378000259399414: the stored price is 31.378."""
    frame = pd.DataFrame(
        {
            "Open": [31.461999893188477],
            "High": [31.56599998474121],
            "Low": [31.378000259399414],
            "Close": [31.378000259399414],
            "Volume": [3426],
        },
        index=pd.DatetimeIndex(["2026-10-02"], tz="Europe/Berlin"),
    )
    ticker = MagicMock()
    ticker.history.return_value = frame
    with patch("historical.providers.yf.Ticker", return_value=ticker):
        result = HistoricalMarketDataFetcher()._fetch_yfinance_sync(
            "SPFF.DE", "1D", date(2026, 10, 1), date(2026, 10, 2)
        )

    bar = result["bars"][0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (31.462, 31.566, 31.378, 31.378)
    assert bar["timestamp"] == datetime(2026, 10, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw", "stored"),
    [(0.00001234567891234, 0.000012345679), (123456.78125, 123456.78), (51.1879997253418, 51.188)],
)
def test_price_keeps_eight_significant_digits(raw, stored):
    from historical.providers import _price

    assert _price(raw) == stored


def test_tradingview_parser_keeps_requested_dates_and_ignores_invalid_frames():
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    payload = {
        "m": "timescale_update",
        "p": [None, {"sds_1": {"s": [{"v": [timestamp, 10, 12, 9, 11, 100]}]}}],
    }
    response = f"~m~4~m~not-json~m~100~m~{json.dumps(payload)}"
    bars = []

    HistoricalMarketDataFetcher._append_tradingview_bars(
        response,
        date(2026, 1, 1),
        date(2026, 1, 3),
        bars,
    )

    assert len(bars) == 1
    assert bars[0]["close"] == 11.0
    assert bars[0]["timestamp"].tzinfo is timezone.utc


def test_tradingview_fetch_handles_heartbeat_and_timescale_response():
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    payload = {
        "m": "timescale_update",
        "p": [None, {"sds_1": {"s": [{"v": [timestamp, 10, 12, 9, 11]}]}}],
    }
    socket = MagicMock()
    socket.recv.side_effect = ["~m~5~m~~h~1", f"~m~100~m~{json.dumps(payload)}"]
    fetcher = HistoricalMarketDataFetcher()

    with (
        patch.object(fetcher, "_resolve_tradingview_symbol", return_value="NASDAQ:AAPL"),
        patch("historical.providers.websocket.create_connection", return_value=socket),
    ):
        result = fetcher._fetch_tradingview_sync(
            "AAPL",
            "1M",
            date(2026, 1, 1),
            date(2026, 1, 3),
        )

    assert result["source_used"] == "tradingview"
    assert result["bars"][0]["volume"] == 0
    assert socket.send.call_count >= 5
    socket.close.assert_called_once()


@pytest.mark.asyncio
async def test_tradingview_connection_failure_is_returned_as_provider_error():
    fetcher = HistoricalMarketDataFetcher()
    with (
        patch.object(fetcher, "_resolve_tradingview_symbol", return_value="NASDAQ:AAPL"),
        patch(
            "historical.providers.websocket.create_connection",
            side_effect=OSError("offline"),
        ),
    ):
        result = await fetcher.fetch_tradingview(
            "AAPL",
            "1D",
            date(2026, 1, 1),
            date(2026, 1, 2),
        )
    assert result["bars"] == []
    assert result["source_used"] == "tradingview"
    assert result["error"] == "offline"


def test_tradingview_symbol_passthrough():
    assert HistoricalMarketDataFetcher._resolve_tradingview_symbol("NASDAQ:AAPL") == "NASDAQ:AAPL"


# ── Choice of the source ─────────────────────────────────────────────────────

PERIOD = (date(2026, 9, 21), date(2026, 10, 2))
ONE_BAR = [{"timestamp": datetime(2026, 10, 2, tzinfo=timezone.utc), "close": 51.5}]


def _fetcher(yahoo: dict, tradingview: dict) -> HistoricalMarketDataFetcher:
    fetcher = HistoricalMarketDataFetcher()
    fetcher.fetch_yfinance = AsyncMock(return_value=yahoo)
    fetcher.fetch_tradingview = AsyncMock(return_value=tradingview)
    return fetcher


@pytest.mark.asyncio
async def test_explicit_yahoo_source_uses_the_verified_symbol_and_nothing_else():
    yahoo = {"bars": ONE_BAR, "source_used": "yfinance", "symbol": "SYBC.DE"}
    fetcher = _fetcher(yahoo, {"bars": ONE_BAR, "source_used": "tradingview"})

    result = await fetcher.fetch("EUCO", "1D", "yfinance", *PERIOD, yahoo_symbol="SYBC.DE")

    assert result == yahoo
    assert fetcher.fetch_yfinance.await_args.args == ("SYBC.DE", "1D", *PERIOD)
    fetcher.fetch_tradingview.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_source_reports_the_failure_of_yahoo():
    fetcher = _fetcher(
        {"bars": [], "source_used": "yfinance", "error": "429 Too Many Requests"},
        {"bars": ONE_BAR, "source_used": "tradingview", "symbol": "XETR:SYBC"},
    )

    result = await fetcher.fetch(
        "EUCO", "1D", "auto", *PERIOD, yahoo_symbol="SYBC.DE", currency="EUR"
    )

    assert result["source_used"] == "tradingview"
    assert result["note"] == "Yahoo has no bar for SYBC.DE (429 Too Many Requests)"
    assert fetcher.fetch_tradingview.await_args.args == ("SYBC", "1D", *PERIOD, "EUR")


@pytest.mark.asyncio
async def test_both_sources_empty_with_a_verified_symbol_keeps_the_answer_of_the_second():
    tradingview = {"bars": [], "source_used": "tradingview", "error": "no line in EUR"}
    fetcher = _fetcher({"bars": [], "source_used": "yfinance"}, tradingview)

    result = await fetcher.fetch("EUCO", "1D", "auto", *PERIOD, yahoo_symbol="SYBC.DE")

    assert result == tradingview
