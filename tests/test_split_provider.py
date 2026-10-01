"""Tests for SplitProvider and split calculation logic."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from financials.providers.split_provider import SplitProvider, calculate_split_ratio
from integrations.openbb.adapters import format_stock_splits_table


def test_calculate_split_ratio():
    """Verify conversion of float ratios to Split From / Split To."""
    # 4-for-1 forward split (e.g. AAPL 2020)
    sf, st = calculate_split_ratio(4.0)
    assert sf == 1 and st == 4

    # 7-for-1 forward split (e.g. AAPL 2014)
    sf, st = calculate_split_ratio(7.0)
    assert sf == 1 and st == 7

    # 2-for-1 forward split
    sf, st = calculate_split_ratio(2.0)
    assert sf == 1 and st == 2

    # 1-for-8 reverse split (e.g. GE 2021)
    sf, st = calculate_split_ratio(0.125)
    assert sf == 8 and st == 1

    # 1-for-10 reverse split (e.g. Citigroup 2011)
    sf, st = calculate_split_ratio(0.1)
    assert sf == 10 and st == 1

    # 3-for-2 split (e.g. NVDA 2007)
    sf, st = calculate_split_ratio(1.5)
    assert sf == 2 and st == 3

    # Edge cases
    assert calculate_split_ratio(0) == (1, 1)
    assert calculate_split_ratio(-2) == (1, 1)


@pytest.mark.asyncio
async def test_split_provider_fetch():
    """Test SplitProvider with mocked yfinance series."""
    provider = SplitProvider()

    # Create dummy series
    dates = pd.to_datetime(["2020-08-31", "2014-06-09"])
    mock_splits = pd.Series([4.0, 7.0], index=dates)

    mock_ticker = MagicMock()
    mock_ticker.splits = mock_splits

    with patch("yfinance.Ticker", return_value=mock_ticker):
        res = await provider.get_stock_splits("AAPL")
        assert len(res) == 2
        assert res[0]["execution_date"] == "2020-08-31"
        assert res[0]["split_from"] == 1
        assert res[0]["split_to"] == 4
        assert res[1]["execution_date"] == "2014-06-09"
        assert res[1]["split_from"] == 1
        assert res[1]["split_to"] == 7


@pytest.mark.asyncio
async def test_split_provider_empty():
    """Test SplitProvider when no splits exist."""
    provider = SplitProvider()

    mock_ticker = MagicMock()
    mock_ticker.splits = pd.Series([], dtype=float)

    with patch("yfinance.Ticker", return_value=mock_ticker):
        res = await provider.get_stock_splits("AIR.PA")
        assert res == []


def test_format_stock_splits_table():
    """Test format_stock_splits_table produces AgGrid-compatible dicts."""
    raw = [
        {"execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
        {"execution_date": "2014-06-09", "split_from": 1, "split_to": 7},
    ]
    rows = format_stock_splits_table(raw)
    assert len(rows) == 2
    assert rows[0] == {
        "Execution Date": "2020-08-31",
        "Split From": 1,
        "Split To": 4,
    }
    assert rows[1] == {
        "Execution Date": "2014-06-09",
        "Split From": 1,
        "Split To": 7,
    }
