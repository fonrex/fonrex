"""SplitProvider — Provider for historical stock splits and corporate actions.

Sources:
1. Yahoo Finance / yfinance: Historical stock splits (t.splits) with execution dates and ratios.
   Automatically converts decimal ratios to integer fractions (Split From / Split To),
   properly handling forward splits (e.g. 4-for-1 -> From 1, To 4),
   reverse splits (e.g. 1-for-8 -> From 8, To 1), and fractional splits (e.g. 3-for-2 -> From 2, To 3).
"""

from __future__ import annotations

import logging
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import yfinance as yf

from concurrency import run_sync

logger = logging.getLogger(__name__)


def calculate_split_ratio(ratio: float) -> Tuple[int, int]:
    """Calculate Split From and Split To from a float split ratio.

    Example:
        ratio = 4.0   -> (1, 4)   (4-for-1 forward split)
        ratio = 7.0   -> (1, 7)   (7-for-1 forward split)
        ratio = 2.0   -> (1, 2)   (2-for-1 forward split)
        ratio = 0.125 -> (8, 1)   (1-for-8 reverse split)
        ratio = 0.1   -> (10, 1)  (1-for-10 reverse split)
        ratio = 1.5   -> (2, 3)   (3-for-2 split)
    """
    try:
        f = float(ratio)
        if f <= 0:
            return 1, 1
        frac = Fraction(f).limit_denominator(1000)
        split_from = frac.denominator
        split_to = frac.numerator
        return split_from, split_to
    except Exception:
        return 1, 1


class SplitProvider:
    """Provides historical stock splits and corporate actions."""

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = timeout

    async def get_stock_splits(
        self,
        ticker: str,
        limit: int = 50,
        refresh: bool = False,
        cache: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch historical stock splits for a ticker with caching."""
        clean_ticker = (ticker or "").strip().upper()
        if not clean_ticker or clean_ticker.startswith("{") or clean_ticker.lower() in ("undefined", "none"):
            clean_ticker = "AAPL"

        cache_key = f"splits:{clean_ticker}:{limit}"
        if cache and getattr(cache, "enabled", False) and not refresh:
            try:
                cached = await run_sync(cache.get, cache_key)
                if cached and isinstance(cached, list):
                    return cached
            except Exception as exc:
                logger.warning("Cache get error for splits %s: %s", clean_ticker, exc)

        records: List[Dict[str, Any]] = []
        try:
            records = await run_sync(self._fetch_yfinance_splits, clean_ticker)
        except Exception as exc:
            logger.warning("yfinance split fetch failed for %s: %s", clean_ticker, exc)

        results = records[:limit]

        if cache and getattr(cache, "enabled", False) and results:
            try:
                # Cache for 24h
                await run_sync(cache.set, cache_key, results, ttl=86400)
            except Exception as exc:
                logger.warning("Cache set error for splits %s: %s", clean_ticker, exc)

        return results

    def _fetch_yfinance_splits(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch stock splits from yfinance."""
        stock = yf.Ticker(ticker)
        splits = stock.splits
        if splits is None or len(splits) == 0:
            return []

        records: List[Dict[str, Any]] = []
        # Sort descending (most recent first)
        for dt, val in splits.sort_index(ascending=False).items():
            dt_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
            try:
                ratio_val = float(val)
            except (ValueError, TypeError):
                continue

            if ratio_val <= 0:
                continue

            split_from, split_to = calculate_split_ratio(ratio_val)

            records.append(
                {
                    "execution_date": dt_str,
                    "split_from": split_from,
                    "split_to": split_to,
                    "ratio": ratio_val,
                }
            )

        return records
