"""Tests for extracted historical market-data providers."""

import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

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
    assert result == {"bars": [], "source_used": "yfinance"}


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
