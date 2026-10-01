"""Unit tests for EarningsProvider and format_earnings_history_table."""

from unittest.mock import AsyncMock, MagicMock, patch
import pandas as pd
import pytest

from financials.providers.earnings_provider import (
    EarningsProvider,
    _format_financial_amount,
)
from integrations.openbb.adapters import (
    _format_amount,
    format_earnings_history_table,
)


def test_format_financial_amount():
    assert _format_financial_amount(None) == "-"
    assert _format_financial_amount(0) == "-"
    assert _format_financial_amount(109417000000) == "109.417 B"
    assert _format_financial_amount(113210000000) == "113.21 B"
    assert _format_financial_amount(2500000000000) == "2.500 T"
    assert _format_financial_amount(50000000) == "50.00 M"
    assert _format_financial_amount(75000) == "75.00 K"
    assert _format_financial_amount(12.5) == "12.50"
    assert _format_financial_amount("invalid") == "invalid"


def test_format_earnings_history_table():
    records = [
        {
            "date": "2026-10-29",
            "eps": None,
            "eps_estimate": 1.98,
            "revenue": None,
            "revenue_estimate": 113210000000,
            "surprise_pct": None,
            "transcript": "",
            "currency": "USD",
        },
        {
            "date": "2026-07-30",
            "eps": 2.02,
            "eps_estimate": 1.89,
            "revenue": 109417000000,
            "revenue_estimate": 109040000000,
            "surprise_pct": 6.74,
            "transcript": "View transcript",
            "currency": "USD",
        },
    ]

    rows = format_earnings_history_table(records)
    assert len(rows) == 2

    # Row 1: Upcoming
    assert rows[0]["Date"] == "2026-10-29"
    assert rows[0]["EPS"] == "-"
    assert rows[0]["EPS Est."] == 1.98
    assert rows[0]["Revenue"] == "-"
    assert rows[0]["Revenue Est."] == "113.21 B"
    assert rows[0]["Transcript"] == ""
    assert rows[0]["currency"] == "USD"

    # Row 2: Reported
    assert rows[1]["Date"] == "2026-07-30"
    assert rows[1]["EPS"] == 2.02
    assert rows[1]["EPS Est."] == 1.89
    assert rows[1]["Revenue"] == "109.417 B"
    assert rows[1]["Revenue Est."] == "109.04 B"
    assert rows[1]["Transcript"] == "View transcript"
    assert rows[1]["currency"] == "USD"


@pytest.mark.asyncio
async def test_earnings_provider_yfinance_fetch():
    provider = EarningsProvider()

    # Create mock yfinance ticker
    mock_stock = MagicMock()

    # Dates
    dates = pd.to_datetime(["2026-10-29", "2026-07-30", "2026-04-30"])
    ed_df = pd.DataFrame(
        {
            "Reported EPS": [None, 2.02, 2.01],
            "EPS Estimate": [1.98, 1.89, 1.95],
            "Surprise(%)": [None, 6.74, 3.46],
        },
        index=dates,
    )
    mock_stock.earnings_dates = ed_df

    # Income statement
    inc_dates = pd.to_datetime(["2026-06-30", "2026-03-31"])
    inc_df = pd.DataFrame(
        {"Total Revenue": [109417000000.0, 111184000000.0]}, index=inc_dates
    ).T
    mock_stock.quarterly_income_stmt = inc_df

    # Revenue estimate
    mock_stock.revenue_estimate = pd.DataFrame(
        {"avg": [113210000000.0]}, index=["0q"]
    )
    mock_stock.calendar = {}
    mock_stock.info = {"currency": "USD"}

    with patch("yfinance.Ticker", return_value=mock_stock):
        records = await provider.get_earnings_history("AAPL", limit=10)

    assert len(records) == 3
    assert records[0]["date"] == "2026-10-29"
    assert records[0]["eps"] is None
    assert records[0]["eps_estimate"] == 1.98
    assert records[0]["revenue"] is None
    assert records[0]["revenue_estimate"] == 113210000000.0

    assert records[1]["date"] == "2026-07-30"
    assert records[1]["eps"] == 2.02
    assert records[1]["revenue"] == 109417000000.0


@pytest.mark.asyncio
async def test_earnings_provider_cache_flow():
    mock_cache = MagicMock()
    mock_cache.enabled = True
    cached_data = [{"date": "2026-07-30", "eps": 2.02}]
    mock_cache.get = MagicMock(return_value=cached_data)

    provider = EarningsProvider()
    res = await provider.get_earnings_history("AAPL", cache=mock_cache)
    assert res == cached_data
    mock_cache.get.assert_called_once_with("earnings_history:AAPL:50")
