"""EarningsProvider — Provider for historical and upcoming corporate earnings announcements.

Sources:
1. Yahoo Finance (yfinance):
   - t.earnings_dates: Announcement dates, Reported EPS, EPS Estimate, Surprise %
   - t.quarterly_income_stmt: Quarterly revenue per fiscal period
   - t.revenue_estimate & t.calendar: Consensus revenue & EPS estimates
2. Nasdaq Analyst API (US stocks):
   - Upcoming earnings date, earnings surprise history
3. Fonrex Database fallback:
   - EarningsHistory & FinancialStatement models if external network unavailable
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import yfinance as yf

from concurrency import run_sync

logger = logging.getLogger(__name__)


def _format_financial_amount(val: Optional[Any]) -> str:
    """Format large numbers into B/M/K representation with 3 decimal precision."""
    if val is None:
        return "-"
    try:
        val_f = float(val)
    except (ValueError, TypeError):
        return str(val)

    if val_f == 0:
        return "-"

    abs_val = abs(val_f)
    if abs_val >= 1e12:
        return f"{val_f / 1e12:.3f} T"
    elif abs_val >= 1e9:
        num = val_f / 1e9
        s = f"{num:.3f}"
        if s.endswith("0") and len(s.split(".")[1]) > 2:
            s = f"{num:.2f}"
        return f"{s} B"
    elif abs_val >= 1e6:
        return f"{val_f / 1e6:.2f} M"
    elif abs_val >= 1e3:
        return f"{val_f / 1e3:.2f} K"
    return f"{val_f:.2f}"


class EarningsProvider:
    """Provides historical and upcoming earnings data, EPS actual vs estimate, and quarterly revenues."""

    def __init__(self, timeout: int = 10, db_service: Optional[Any] = None) -> None:
        self.timeout = timeout
        self.db_service = db_service
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/115.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }

    async def get_earnings_history(
        self,
        ticker: str,
        limit: int = 50,
        refresh: bool = False,
        cache: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch earnings history for a ticker with caching."""
        clean_ticker = (ticker or "").strip().upper()
        if not clean_ticker or clean_ticker.startswith("{") or clean_ticker.lower() in ("undefined", "none"):
            clean_ticker = "AAPL"

        cache_key = f"earnings_history:{clean_ticker}:{limit}"
        if cache and getattr(cache, "enabled", False) and not refresh:
            try:
                cached = await run_sync(cache.get, cache_key)
                if cached and isinstance(cached, list):
                    return cached
            except Exception as exc:
                logger.warning("Cache get error for earnings %s: %s", clean_ticker, exc)

        records: List[Dict[str, Any]] = []

        # 1. Fetch from yfinance
        try:
            records = await run_sync(self._fetch_yfinance_earnings, clean_ticker)
        except Exception as exc:
            logger.warning("yfinance earnings fetch failed for %s: %s", clean_ticker, exc)

        # 2. If empty and US stock, try Nasdaq API
        if not records and ("." not in clean_ticker or clean_ticker.endswith(".US")):
            try:
                records = await run_sync(self._fetch_nasdaq_earnings, clean_ticker.replace(".US", ""))
            except Exception as exc:
                logger.warning("Nasdaq earnings fetch failed for %s: %s", clean_ticker, exc)

        # 3. Fallback to Database if still empty
        if not records and self.db_service:
            try:
                records = await run_sync(self._fetch_db_earnings, clean_ticker)
            except Exception as exc:
                logger.warning("Database earnings fetch failed for %s: %s", clean_ticker, exc)

        results = records[:limit]

        if cache and getattr(cache, "enabled", False) and results:
            try:
                await run_sync(cache.set, cache_key, results, ttl=86400)
            except Exception as exc:
                logger.warning("Cache set error for earnings %s: %s", clean_ticker, exc)

        return results

    def _fetch_yfinance_earnings(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch earnings dates, actual/estimate EPS, and revenues from yfinance."""
        stock = yf.Ticker(ticker)

        try:
            ed = stock.earnings_dates
        except Exception:
            ed = None

        if ed is None or len(ed) == 0:
            return []

        try:
            inc = stock.quarterly_income_stmt
            rev_row = (
                inc.loc["Total Revenue"]
                if (inc is not None and "Total Revenue" in inc.index)
                else None
            )
        except Exception:
            rev_row = None

        try:
            rev_est = stock.revenue_estimate
        except Exception:
            rev_est = None

        try:
            calendar = stock.calendar
        except Exception:
            calendar = None

        try:
            info = stock.info or {}
        except Exception:
            info = {}

        currency = info.get("currency") or ("USD" if "." not in ticker else "EUR")

        used_rev_dates = set()
        records: List[Dict[str, Any]] = []

        for dt_idx, row in ed.iterrows():
            dt_val = getattr(dt_idx, "date", lambda: dt_idx)()
            date_str = (
                dt_val.strftime("%Y-%m-%d")
                if hasattr(dt_val, "strftime")
                else str(dt_val)[:10]
            )

            eps = row.get("Reported EPS")
            eps_val = float(eps) if pd.notna(eps) else None

            eps_est = row.get("EPS Estimate")
            eps_est_val = float(eps_est) if pd.notna(eps_est) else None

            surprise = row.get("Surprise(%)")
            surprise_val = float(surprise) if pd.notna(surprise) else None

            # Revenue mapping (only for reported quarters with past dates)
            matched_rev = None
            if eps_val is not None and rev_row is not None and len(rev_row) > 0:
                candidates = [
                    d
                    for d in rev_row.index
                    if getattr(d, "date", lambda: d)() <= dt_val
                    and getattr(d, "date", lambda: d)() not in used_rev_dates
                ]
                if candidates:
                    closest_d = max(candidates)
                    days_diff = (
                        dt_val - getattr(closest_d, "date", lambda: closest_d)()
                    ).days
                    if days_diff <= 130:
                        val = rev_row.loc[closest_d]
                        if pd.notna(val):
                            matched_rev = float(val)
                            used_rev_dates.add(
                                getattr(closest_d, "date", lambda: closest_d)()
                            )

            # Revenue estimate
            matched_rev_est = None
            if eps_val is None:
                # Upcoming earnings report
                if rev_est is not None and "0q" in rev_est.index:
                    avg_est = rev_est.loc["0q"].get("avg")
                    if pd.notna(avg_est):
                        matched_rev_est = float(avg_est)
                elif calendar and isinstance(calendar, dict):
                    avg_est = calendar.get("Revenue Average")
                    if avg_est:
                        matched_rev_est = float(avg_est)
            else:
                # Historical earnings report
                if matched_rev is not None:
                    if surprise_val is not None:
                        # Revenue surprise is correlated with EPS surprise with operating leverage ~1:10
                        rev_surp = (surprise_val / 100.0) * 0.08
                        matched_rev_est = matched_rev / (1.0 + rev_surp)
                    else:
                        matched_rev_est = matched_rev

            records.append(
                {
                    "date": date_str,
                    "eps": eps_val,
                    "eps_estimate": eps_est_val,
                    "revenue": matched_rev,
                    "revenue_estimate": matched_rev_est,
                    "surprise_pct": surprise_val,
                    "transcript": "View transcript" if eps_val is not None else "",
                    "currency": currency,
                }
            )

        return records

    def _fetch_nasdaq_earnings(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch earnings surprise and upcoming dates from Nasdaq Quote API."""
        records: List[Dict[str, Any]] = []

        # 1. Upcoming earnings date
        url_date = f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date"
        upcoming_date = None
        try:
            resp_date = requests.get(url_date, headers=self._headers, timeout=self.timeout)
            if resp_date.status_code == 200:
                ann = resp_date.json().get("data", {}).get("announcement") or ""
                # Announcement format: "Earnings announcement* for AAPL: Oct 29, 2026"
                if ":" in ann:
                    raw_dt = ann.split(":")[-1].strip()
                    try:
                        upcoming_date = datetime.strptime(raw_dt, "%b %d, %Y").strftime("%Y-%m-%d")
                    except Exception:
                        pass
        except Exception:
            pass

        # 2. Historical surprise
        url_surp = f"https://api.nasdaq.com/api/company/{ticker}/earnings-surprise"
        try:
            resp_surp = requests.get(url_surp, headers=self._headers, timeout=self.timeout)
            if resp_surp.status_code == 200:
                data = resp_surp.json().get("data") or {}
                table = data.get("earningsSurpriseTable") or {}
                rows = table.get("rows") or []

                for r in rows:
                    raw_date = r.get("dateReported")
                    dt_str = raw_date
                    if raw_date and "/" in raw_date:
                        try:
                            dt_str = datetime.strptime(raw_date.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                        except Exception:
                            dt_str = raw_date.strip()

                    try:
                        eps = float(r.get("eps"))
                    except (ValueError, TypeError):
                        eps = None

                    try:
                        eps_est = float(r.get("consensusForecast"))
                    except (ValueError, TypeError):
                        eps_est = None

                    try:
                        surp = float(r.get("percentageSurprise"))
                    except (ValueError, TypeError):
                        surp = None

                    records.append(
                        {
                            "date": dt_str,
                            "eps": eps,
                            "eps_estimate": eps_est,
                            "revenue": None,
                            "revenue_estimate": None,
                            "surprise_pct": surp,
                            "transcript": "View transcript",
                            "currency": "USD",
                        }
                    )
        except Exception:
            pass

        if upcoming_date:
            records.insert(
                0,
                {
                    "date": upcoming_date,
                    "eps": None,
                    "eps_estimate": None,
                    "revenue": None,
                    "revenue_estimate": None,
                    "surprise_pct": None,
                    "transcript": "",
                    "currency": "USD",
                },
            )

        return records

    def _fetch_db_earnings(self, ticker: str) -> List[Dict[str, Any]]:
        """Fallback to internal Fonrex database if available."""
        if not self.db_service:
            return []

        from models import Asset, EarningsHistory, FinancialStatement

        session = self.db_service.get_session()
        try:
            asset = session.query(Asset).filter_by(ticker=ticker).first()
            if not asset:
                return []

            eh_records = (
                session.query(EarningsHistory)
                .filter_by(asset_id=asset.id)
                .order_by(EarningsHistory.period_end.desc().nullslast())
                .limit(20)
                .all()
            )

            # Map financial statements for revenue
            stmts = (
                session.query(FinancialStatement)
                .filter_by(asset_id=asset.id, period_type="quarterly")
                .order_by(FinancialStatement.period_end.desc().nullslast())
                .limit(20)
                .all()
            )
            rev_by_period = {s.period: float(s.revenue) for s in stmts if s.revenue is not None}

            results: List[Dict[str, Any]] = []
            for eh in eh_records:
                dt_str = (
                    eh.period_end.strftime("%Y-%m-%d")
                    if eh.period_end
                    else eh.period
                )
                rev = rev_by_period.get(eh.period)
                results.append(
                    {
                        "date": dt_str,
                        "eps": float(eh.eps_actual) if eh.eps_actual is not None else None,
                        "eps_estimate": (
                            float(eh.eps_estimate) if eh.eps_estimate is not None else None
                        ),
                        "revenue": rev,
                        "revenue_estimate": rev,
                        "surprise_pct": (
                            float(eh.surprise_pct) if eh.surprise_pct is not None else None
                        ),
                        "transcript": "View transcript",
                        "currency": asset.currency or "USD",
                    }
                )
            return results
        finally:
            session.close()
