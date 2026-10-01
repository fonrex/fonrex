"""DividendProvider — Provider for historical dividend payments and corporate action calendars.

Sources:
1. Nasdaq Dividends API (US stocks): Full corporate calendar including declaration, record,
   ex-dividend, payment dates, and cash dividend amounts.
2. Yahoo Finance / yfinance (fallback & European stocks): Historical dividend distributions,
   ex-dates, split adjustments, and payment dates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import yfinance as yf

from concurrency import run_sync

logger = logging.getLogger(__name__)


class DividendProvider:
    """Provides historical dividend payments and calendar data."""

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }

    async def get_dividends(
        self,
        ticker: str,
        limit: int = 50,
        refresh: bool = False,
        cache: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch historical dividend payments for a ticker with caching."""
        clean_ticker = (ticker or "").strip().upper()
        if not clean_ticker or clean_ticker.startswith("{") or clean_ticker.lower() in ("undefined", "none"):
            clean_ticker = "AAPL"

        cache_key = f"dividends:{clean_ticker}:{limit}"
        if cache and getattr(cache, "enabled", False) and not refresh:
            try:
                cached = await run_sync(cache.get, cache_key)
                if cached and isinstance(cached, list):
                    return cached
            except Exception as exc:
                logger.warning("Cache get error for dividends %s: %s", clean_ticker, exc)

        # 1. For US stocks (tickers without European exchange suffixes like .PA, .DE, .MC), try Nasdaq first
        records: List[Dict[str, Any]] = []
        is_us_stock = "." not in clean_ticker or clean_ticker.endswith(".US")

        if is_us_stock:
            nasdaq_sym = clean_ticker.replace(".US", "")
            try:
                records = await run_sync(self._fetch_nasdaq_dividends, nasdaq_sym)
            except Exception as exc:
                logger.warning("Nasdaq dividend fetch failed for %s: %s", clean_ticker, exc)

        # 2. Fallback to yfinance if Nasdaq returned no records or for non-US stocks
        if not records:
            try:
                records = await run_sync(self._fetch_yfinance_dividends, clean_ticker)
            except Exception as exc:
                logger.warning("yfinance dividend fetch failed for %s: %s", clean_ticker, exc)

        # Truncate to requested limit
        results = records[:limit]

        if cache and getattr(cache, "enabled", False) and results:
            try:
                # Cache for 24h
                await run_sync(cache.set, cache_key, results, ttl=86400)
            except Exception as exc:
                logger.warning("Cache set error for dividends %s: %s", clean_ticker, exc)

        return results

    def _fetch_nasdaq_dividends(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch dividends from Nasdaq Quote API."""
        url = f"https://api.nasdaq.com/api/quote/{ticker}/dividends?assetclass=stocks"
        response = requests.get(url, headers=self._headers, timeout=self.timeout)
        if response.status_code != 200:
            return []

        data = response.json().get("data") or {}
        divs_obj = data.get("dividends") or {}
        rows = divs_obj.get("rows") or []
        if not rows:
            return []

        def _parse_date(date_str: Optional[str]) -> Optional[str]:
            if not date_str or date_str.strip() in ("N/A", "--", ""):
                return None
            try:
                return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
            except Exception:
                return date_str.strip()

        records: List[Dict[str, Any]] = []
        for r in rows:
            raw_amt = (r.get("amount") or "").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw_amt)
            except (ValueError, TypeError):
                amt = None

            records.append(
                {
                    "date": _parse_date(r.get("exOrEffDate")),
                    "adjusted_dividend": amt,
                    "dividend": amt,
                    "record_date": _parse_date(r.get("recordDate")),
                    "payment_date": _parse_date(r.get("paymentDate")),
                    "declaration_date": _parse_date(r.get("declarationDate")),
                    "currency": r.get("currency") or "USD",
                }
            )

        # Sort descending by date
        records.sort(key=lambda x: x.get("date") or "", reverse=True)
        return records

    def _fetch_yfinance_dividends(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch dividends from yfinance."""
        stock = yf.Ticker(ticker)
        divs = stock.dividends
        if divs is None or len(divs) == 0:
            return []

        info = stock.info or {}
        calendar = stock.calendar or {}
        currency = info.get("currency") or ("USD" if "." not in ticker else "EUR")

        cal_div_date = calendar.get("Dividend Date")
        if hasattr(cal_div_date, "strftime"):
            cal_div_date = cal_div_date.strftime("%Y-%m-%d")
        else:
            cal_div_date = str(cal_div_date) if cal_div_date else None

        cal_ex_date = calendar.get("Ex-Dividend Date")
        if hasattr(cal_ex_date, "strftime"):
            cal_ex_date = cal_ex_date.strftime("%Y-%m-%d")
        else:
            cal_ex_date = str(cal_ex_date) if cal_ex_date else None

        records: List[Dict[str, Any]] = []
        for dt, val in divs.sort_index(ascending=False).items():
            dt_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
            pay_dt = cal_div_date if dt_str == cal_ex_date else None
            try:
                amt = round(float(val), 4)
            except (ValueError, TypeError):
                amt = None

            records.append(
                {
                    "date": dt_str,
                    "adjusted_dividend": amt,
                    "dividend": amt,
                    "record_date": None,
                    "payment_date": pay_dt,
                    "declaration_date": None,
                    "currency": currency,
                }
            )

        return records
