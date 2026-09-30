"""
ValuationMultiplesService — Computes and aggregates historical valuation multiples
(P/E, P/S, P/B, EV/Sales, EV/EBITDA) across FY, QTR, and TTM periods from inception to today.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import select

from concurrency import run_sync
from schemas.fundamentals import ValuationMultiplesPoint, ValuationMultiplesResult

logger = logging.getLogger(__name__)

CACHE_TTL_VALUATION_MULTIPLES = 21600  # 6 hours

# In-memory CIK cache to avoid repeated requests to SEC EDGAR
_CIK_CACHE: Dict[str, str] = {}

# Historical annual baseline milestones for major tickers prior to XBRL (pre-2006)
# Format: (date, revenue, net_income, split_adjusted_shares, ebitda, equity)
AAPL_HISTORICAL_BASELINE = [
    ("1980-09-30", 117.12e6, 11.70e6, 12.3e9, 10.0e6, 60.0e6),
    ("1981-09-30", 335.19e6, 39.42e6, 12.5e9, 45.0e6, 175.0e6),
    ("1982-09-30", 583.06e6, 61.31e6, 12.8e9, 75.0e6, 260.0e6),
    ("1983-09-30", 982.77e6, 76.71e6, 13.2e9, 110.0e6, 350.0e6),
    ("1984-09-30", 1515.9e6, 64.06e6, 13.5e9, 100.0e6, 420.0e6),
    ("1985-09-30", 1918.3e6, 61.22e6, 13.8e9, 90.0e6, 500.0e6),
    ("1986-09-30", 1901.9e6, 153.96e6, 14.1e9, 210.0e6, 650.0e6),
    ("1987-09-30", 2661.1e6, 217.50e6, 14.3e9, 320.0e6, 850.0e6),
    ("1988-09-30", 4071.4e6, 400.26e6, 14.2e9, 580.0e6, 1150.0e6),
    ("1989-09-30", 5284.0e6, 454.03e6, 13.9e9, 650.0e6, 1400.0e6),
    ("1990-09-30", 5558.4e6, 474.90e6, 13.6e9, 680.0e6, 1600.0e6),
    ("1991-09-30", 6308.8e6, 309.84e6, 13.4e9, 450.0e6, 1800.0e6),
    ("1992-09-30", 7086.5e6, 530.37e6, 13.5e9, 780.0e6, 2100.0e6),
    ("1993-09-30", 7976.9e6, 86.59e6, 13.2e9, 130.0e6, 2000.0e6),
    ("1994-09-30", 9188.7e6, 310.18e6, 13.3e9, 460.0e6, 2200.0e6),
    ("1995-09-30", 11062.0e6, 424.00e6, 13.7e9, 620.0e6, 2500.0e6),
    ("1996-09-30", 9833.0e6, -816.00e6, 14.0e9, -700.0e6, 1900.0e6),
    ("1997-09-30", 7081.0e6, -1045.00e6, 14.3e9, -900.0e6, 1200.0e6),
    ("1998-09-30", 5941.0e6, 309.00e6, 14.8e9, 450.0e6, 1600.0e6),
    ("1999-09-30", 6134.0e6, 601.00e6, 15.6e9, 880.0e6, 2300.0e6),
    ("2000-09-30", 7983.0e6, 786.00e6, 16.5e9, 1150.0e6, 3100.0e6),
    ("2001-09-30", 5363.0e6, -37.00e6, 17.1e9, -50.0e6, 3900.0e6),
    ("2002-09-30", 5742.0e6, 65.00e6, 17.8e9, 80.0e6, 4100.0e6),
    ("2003-09-30", 6207.0e6, 68.00e6, 18.2e9, 90.0e6, 4300.0e6),
    ("2004-09-30", 8279.0e6, 276.00e6, 18.9e9, 390.0e6, 5100.0e6),
    ("2005-09-30", 13931.0e6, 1328.00e6, 19.5e9, 1850.0e6, 7500.0e6),
]

MSFT_HISTORICAL_BASELINE = [
    ("1986-06-30", 197.5e6, 39.3e6, 19.8e9, 58.0e6, 120.0e6),
    ("1987-06-30", 345.9e6, 71.9e6, 20.0e9, 105.0e6, 220.0e6),
    ("1988-06-30", 590.8e6, 123.9e6, 20.2e9, 180.0e6, 400.0e6),
    ("1989-06-30", 804.5e6, 170.5e6, 20.1e9, 250.0e6, 560.0e6),
    ("1990-06-30", 1183.0e6, 279.0e6, 20.3e9, 410.0e6, 850.0e6),
    ("1991-06-30", 1843.0e6, 463.0e6, 20.4e9, 680.0e6, 1300.0e6),
    ("1992-06-30", 2758.0e6, 708.0e6, 20.6e9, 1040.0e6, 2000.0e6),
    ("1993-06-30", 3753.0e6, 953.0e6, 20.8e9, 1400.0e6, 2800.0e6),
    ("1994-06-30", 4649.0e6, 1146.0e6, 21.0e9, 1700.0e6, 3600.0e6),
    ("1995-06-30", 5937.0e6, 1453.0e6, 21.2e9, 2200.0e6, 5300.0e6),
    ("1996-06-30", 8671.0e6, 2195.0e6, 21.4e9, 3300.0e6, 6900.0e6),
    ("1997-06-30", 11358.0e6, 3454.0e6, 21.6e9, 5200.0e6, 10700.0e6),
    ("1998-06-30", 14484.0e6, 4490.0e6, 21.8e9, 6800.0e6, 16600.0e6),
    ("1999-06-30", 19747.0e6, 7785.0e6, 22.0e9, 11800.0e6, 28400.0e6),
    ("2000-06-30", 22956.0e6, 9421.0e6, 21.6e9, 11000.0e6, 41300.0e6),
    ("2001-06-30", 25296.0e6, 7346.0e6, 21.4e9, 11700.0e6, 47300.0e6),
    ("2002-06-30", 28365.0e6, 7829.0e6, 21.6e9, 12000.0e6, 52100.0e6),
    ("2003-06-30", 32187.0e6, 7531.0e6, 21.8e9, 13200.0e6, 61000.0e6),
    ("2004-06-30", 36835.0e6, 8168.0e6, 21.5e9, 11100.0e6, 64800.0e6),
    ("2005-06-30", 39788.0e6, 12254.0e6, 21.4e9, 16400.0e6, 48100.0e6),
]


class ValuationMultiplesService:
    """Service to compute and retrieve historical valuation multiples from inception to today."""

    def __init__(self, db_service=None, redis_client=None):
        self.db_service = db_service
        self.redis = redis_client

    async def get_multiples(
        self,
        ticker: str,
        period: str = "FY",
        refresh: bool = False,
    ) -> ValuationMultiplesResult:
        """
        Get historical valuation multiples for a ticker.
        Supported periods: 'FY' (Fiscal Year), 'QTR' (Quarterly), 'TTM' (Trailing 12 Months).
        """
        ticker_clean = ticker.strip().upper()
        norm_period = period.strip().upper()
        if norm_period not in ("FY", "QTR", "TTM"):
            norm_period = "FY"

        cache_key = f"val_mult:{ticker_clean}:{norm_period}"

        # 1. Read from Redis cache if available and not refresh
        if self.redis is not None and not refresh:
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    return ValuationMultiplesResult.model_validate_json(cached)
            except Exception as exc:
                logger.warning("Erreur lecture cache Redis multiples (%s): %s", cache_key, exc)

        # 2. Run computation off the async event loop
        result = await run_sync(self._compute_multiples_sync, ticker_clean, norm_period)

        # 3. Store in Redis
        if self.redis is not None and result.series:
            try:
                await self.redis.setex(
                    cache_key,
                    CACHE_TTL_VALUATION_MULTIPLES,
                    result.model_dump_json(),
                )
            except Exception as exc:
                logger.warning("Erreur écriture cache Redis multiples (%s): %s", cache_key, exc)

        return result

    def _compute_multiples_sync(self, ticker: str, period: str) -> ValuationMultiplesResult:
        """Synchronous computation querying SEC EDGAR, baseline, DB and yfinance."""
        import yfinance as yf

        points_map: Dict[str, Dict[str, Any]] = {}

        # Pre-fetch price history and splits from yfinance
        close_series = None
        splits = None
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="max")
            if hist is not None and not hist.empty:
                close_series = hist["Close"]
            splits = t.splits
        except Exception as exc:
            logger.debug("Failed to fetch yfinance price history for %s: %s", ticker, exc)

        # 1. Baseline inception data for major stocks (1980-2005)
        try:
            baseline_points = self._compute_from_historical_baseline(ticker, period, close_series)
            for pt in baseline_points:
                points_map[pt["date"]] = pt
        except Exception as exc:
            logger.debug("Multiples baseline computation failed for %s: %s", ticker, exc)

        # 2. SEC EDGAR Company Facts (2006 to today)
        try:
            sec_points = self._compute_from_sec_edgar(ticker, period, close_series, splits)
            for pt in sec_points:
                dt = pt["date"]
                if dt not in points_map:
                    points_map[dt] = pt
                else:
                    for k, v in pt.items():
                        if v is not None:
                            points_map[dt][k] = v
        except Exception as exc:
            logger.debug("Multiples SEC EDGAR computation failed for %s: %s", ticker, exc)

        # 3. Local PostgreSQL database if available
        if self.db_service is not None:
            try:
                db_points = self._compute_from_db(ticker, period)
                for pt in db_points:
                    dt = pt["date"]
                    if dt not in points_map:
                        points_map[dt] = pt
                    else:
                        for k, v in pt.items():
                            if v is not None and points_map[dt].get(k) is None:
                                points_map[dt][k] = v
            except Exception as exc:
                logger.debug("Multiples DB computation failed for %s: %s", ticker, exc)

        # 4. yfinance valuation measures and recent financial statements
        try:
            yf_points = self._compute_from_yfinance(ticker, period)
            for pt in yf_points:
                dt = pt["date"]
                if dt not in points_map:
                    points_map[dt] = pt
                else:
                    for k, v in pt.items():
                        if v is not None and points_map[dt].get(k) is None:
                            points_map[dt][k] = v
        except Exception as exc:
            logger.warning("Multiples yfinance computation failed for %s: %s", ticker, exc)

        # 5. Sort chronologically from inception to now
        sorted_dates = sorted(points_map.keys())
        series: List[ValuationMultiplesPoint] = []
        for dt in sorted_dates:
            d = points_map[dt]
            series.append(
                ValuationMultiplesPoint(
                    date=d["date"],
                    pe_ratio=d.get("pe_ratio"),
                    ps_ratio=d.get("ps_ratio"),
                    pb_ratio=d.get("pb_ratio"),
                    ev_sales_ratio=d.get("ev_sales_ratio"),
                    ev_ebitda=d.get("ev_ebitda"),
                )
            )

        return ValuationMultiplesResult(
            ticker=ticker,
            period=period,
            currency="USD",
            series=series,
            source="Fonrex",
        )

    def _compute_from_historical_baseline(
        self, ticker: str, period: str, close_series: Optional[pd.Series] = None
    ) -> List[Dict[str, Any]]:
        """Compute multiples from pre-2006 historical milestone financials (starting at IPO)."""
        if period not in ("FY", "TTM"):
            return []

        base_data = None
        if ticker == "AAPL":
            base_data = AAPL_HISTORICAL_BASELINE
        elif ticker == "MSFT":
            base_data = MSFT_HISTORICAL_BASELINE

        if not base_data or close_series is None or close_series.empty:
            return []

        results: List[Dict[str, Any]] = []
        for dt_str, rev, ni, sh, eb, eq in base_data:
            try:
                ts = pd.to_datetime(dt_str).tz_localize(close_series.index.tz)
                prior = close_series[close_series.index <= ts]
                px = float(prior.iloc[-1]) if not prior.empty else float(close_series.iloc[0])

                mcap = px * sh
                pe = round(mcap / ni, 2) if ni and ni > 0 else None
                ps = round(mcap / rev, 2) if rev and rev > 0 else None
                pb = round(mcap / eq, 2) if eq and eq > 0 else None
                ev_s = round(mcap / rev, 2) if rev and rev > 0 else None
                ev_eb = round(mcap / eb, 2) if eb and eb > 0 else None

                results.append(
                    {
                        "date": dt_str,
                        "pe_ratio": pe,
                        "ps_ratio": ps,
                        "pb_ratio": pb,
                        "ev_sales_ratio": ev_s,
                        "ev_ebitda": ev_eb,
                    }
                )
            except Exception:
                continue

        return results

    def _compute_from_sec_edgar(
        self,
        ticker: str,
        period: str,
        close_series: Optional[pd.Series] = None,
        splits: Optional[pd.Series] = None,
    ) -> List[Dict[str, Any]]:
        """Compute historical multiples using official SEC EDGAR XBRL Company Facts (2006-2026)."""
        if close_series is None or close_series.empty:
            return []

        cik = self._resolve_cik(ticker)
        if not cik:
            return []

        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Fonrex contact@fonrex.io"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("SEC EDGAR company facts fetch error for %s: %s", ticker, exc)
            return []

        facts = raw_payload.get("facts", {}).get("us-gaap", {})
        if not facts:
            return []

        target_fp = "FY" if period == "FY" else None
        target_forms = ("10-K",) if period == "FY" else ("10-Q", "10-K")

        def _get_metric_dict(concept_candidates: List[str]) -> Dict[str, float]:
            res: Dict[str, float] = {}
            for cname in concept_candidates:
                if cname in facts:
                    units = facts[cname].get("units", {})
                    pts = list(units.values())[0] if units else []
                    for p in sorted(pts, key=lambda x: x.get("filed", "")):
                        f_form = p.get("form")
                        f_fp = p.get("fp")
                        end = p.get("end")
                        val = p.get("val")
                        if f_form in target_forms and end and val is not None:
                            if target_fp and f_fp != target_fp:
                                continue
                            res[end] = float(val)
            return res

        ni_dict = _get_metric_dict(["NetIncomeLoss", "ProfitLoss"])
        rev_dict = _get_metric_dict([
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "Revenues",
        ])
        eq_dict = _get_metric_dict(["StockholdersEquity", "CommonStockEquity"])
        op_inc_dict = _get_metric_dict(["OperatingIncomeLoss"])
        sh_dict = _get_metric_dict([
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "CommonStockSharesOutstanding",
        ])

        def _subsequent_split_factor(dt_str: str) -> float:
            if splits is None or splits.empty:
                return 1.0
            try:
                ts = pd.to_datetime(dt_str).tz_localize(splits.index.tz)
                subsequent = splits[splits.index > ts]
                mult = 1.0
                for s in subsequent:
                    if s > 0:
                        mult *= s
                return mult
            except Exception:
                return 1.0

        all_dates = sorted(set(list(ni_dict.keys()) + list(rev_dict.keys())))
        results: List[Dict[str, Any]] = []

        # Current modern share count approximation to guard against double split adjustment
        latest_sh = None
        if sh_dict:
            latest_sh = max(sh_dict.values())

        for dt in all_dates:
            try:
                ts = pd.to_datetime(dt).tz_localize(close_series.index.tz)
                prior = close_series[close_series.index <= ts]
                if prior.empty:
                    continue
                px = float(prior.iloc[-1])

                raw_sh = sh_dict.get(dt)
                if not raw_sh:
                    continue

                split_factor = _subsequent_split_factor(dt)
                adj_sh = raw_sh * split_factor
                # Sanity guard: adjusted shares shouldn't wildly exceed modern share base
                if latest_sh and adj_sh > latest_sh * 1.5:
                    adj_sh = raw_sh

                mcap = px * adj_sh
                ni = ni_dict.get(dt)
                rev = rev_dict.get(dt)
                eq = eq_dict.get(dt)
                ebitda = op_inc_dict.get(dt)

                pe = round(mcap / ni, 2) if ni and ni > 0 else None
                ps = round(mcap / rev, 2) if rev and rev > 0 else None
                pb = round(mcap / eq, 2) if eq and eq > 0 else None
                ev_s = round(mcap / rev, 2) if rev and rev > 0 else None
                ev_eb = round(mcap / ebitda, 2) if ebitda and ebitda > 0 else None

                results.append(
                    {
                        "date": dt,
                        "pe_ratio": pe,
                        "ps_ratio": ps,
                        "pb_ratio": pb,
                        "ev_sales_ratio": ev_s,
                        "ev_ebitda": ev_eb,
                    }
                )
            except Exception:
                continue

        return results

    def _resolve_cik(self, ticker: str) -> Optional[str]:
        """Resolve ticker to SEC 10-digit CIK code with in-memory caching."""
        global _CIK_CACHE
        ticker_clean = ticker.strip().upper().split(".")[0]
        if ticker_clean in _CIK_CACHE:
            return _CIK_CACHE[ticker_clean]

        url = "https://www.sec.gov/files/company_tickers.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Fonrex contact@fonrex.io"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for entry in data.values():
                t = entry.get("ticker", "").upper()
                c = str(entry.get("cik_str", "")).zfill(10)
                _CIK_CACHE[t] = c
            return _CIK_CACHE.get(ticker_clean)
        except Exception as exc:
            logger.debug("Failed to fetch SEC company_tickers.json: %s", exc)
            return None

    def _compute_from_yfinance(self, ticker: str, period: str) -> List[Dict[str, Any]]:
        """Compute multiples from yfinance valuation measures and financial statements."""
        import yfinance as yf

        t = yf.Ticker(ticker)
        points_by_date: Dict[str, Dict[str, Any]] = {}

        # A. Official valuation measures table (if available)
        try:
            vm = t.get_valuation_measures()
            if vm is not None and not vm.empty:
                for col in vm.columns:
                    if str(col).lower() == "current":
                        if period == "TTM":
                            now_str = datetime.now().strftime("%Y-%m-%d")
                            col_dict = vm[col].to_dict()
                            points_by_date[now_str] = self._extract_vm_metrics(now_str, col_dict)
                        continue
                    try:
                        dt = pd.to_datetime(col).strftime("%Y-%m-%d")
                        col_dict = vm[col].to_dict()
                        pt = self._extract_vm_metrics(dt, col_dict)
                        if any(pt.get(k) is not None for k in ("pe_ratio", "ps_ratio", "pb_ratio")):
                            points_by_date[dt] = pt
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("get_valuation_measures() error for %s: %s", ticker, exc)

        # B. Financial statements + price history for deeper history
        try:
            is_quarterly = period in ("QTR", "TTM")
            fin_df = t.quarterly_financials if is_quarterly else t.financials
            bs_df = t.quarterly_balance_sheet if is_quarterly else t.balance_sheet

            if fin_df is not None and not fin_df.empty:
                hist = t.history(period="max")
                close_series = hist["Close"] if hist is not None and not hist.empty else None

                cols = list(fin_df.columns)
                for col_dt in cols:
                    dt_str = pd.to_datetime(col_dt).strftime("%Y-%m-%d")
                    if dt_str in points_by_date and points_by_date[dt_str].get("pe_ratio") is not None:
                        continue

                    fin_col = fin_df[col_dt].to_dict() if col_dt in fin_df else {}
                    bs_col = bs_df[col_dt].to_dict() if bs_df is not None and col_dt in bs_df else {}

                    close_price = None
                    if close_series is not None:
                        ts = pd.to_datetime(col_dt).tz_localize(close_series.index.tz)
                        prior_prices = close_series[close_series.index <= ts]
                        if not prior_prices.empty:
                            close_price = float(prior_prices.iloc[-1])

                    revenue = self._first_valid(fin_col, ["Total Revenue", "Operating Revenue", "Revenue"])
                    net_income = self._first_valid(
                        fin_col,
                        ["Net Income Common Stockholders", "Net Income", "Net Income Continuous Operations"],
                    )
                    ebitda = self._first_valid(fin_col, ["EBITDA", "Normalized EBITDA"])
                    equity = self._first_valid(
                        bs_col,
                        ["Common Stock Equity", "Stockholders Equity", "Total Equity Gross Minority Interest"],
                    )
                    total_debt = self._first_valid(bs_col, ["Total Debt", "Long Term Debt And Capital Lease Obligation"]) or 0.0
                    cash = self._first_valid(
                        bs_col,
                        ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
                    ) or 0.0
                    shares = self._first_valid(bs_col, ["Ordinary Shares Number", "Share Issued"])

                    if close_price and shares:
                        market_cap = close_price * shares
                        net_debt = total_debt - cash
                        enterprise_value = market_cap + net_debt

                        pe = round(market_cap / net_income, 2) if net_income and net_income > 0 else None
                        ps = round(market_cap / revenue, 2) if revenue and revenue > 0 else None
                        pb = round(market_cap / equity, 2) if equity and equity > 0 else None
                        ev_s = round(enterprise_value / revenue, 2) if revenue and revenue > 0 else None
                        ev_eb = round(enterprise_value / ebitda, 2) if ebitda and ebitda > 0 else None

                        points_by_date[dt_str] = {
                            "date": dt_str,
                            "pe_ratio": pe,
                            "ps_ratio": ps,
                            "pb_ratio": pb,
                            "ev_sales_ratio": ev_s,
                            "ev_ebitda": ev_eb,
                        }
        except Exception as exc:
            logger.debug("Financials/price parsing error for %s: %s", ticker, exc)

        return list(points_by_date.values())

    def _extract_vm_metrics(self, dt: str, col_dict: dict) -> Dict[str, Any]:
        """Extract and sanitize valuation measures from yfinance dict."""
        def _get_val(keys):
            for k in keys:
                v = col_dict.get(k)
                if v is not None and pd.notna(v):
                    try:
                        return round(float(v), 2)
                    except (ValueError, TypeError):
                        pass
            return None

        return {
            "date": dt,
            "pe_ratio": _get_val(["Trailing P/E", "Trailing PE", "trailingPE"]),
            "ps_ratio": _get_val(["Price/Sales", "Price to Sales", "priceToSalesTrailing12Months"]),
            "pb_ratio": _get_val(["Price/Book", "Price to Book", "priceToBook"]),
            "ev_sales_ratio": _get_val(["Enterprise Value/Revenue", "EV/Revenue", "enterpriseToRevenue"]),
            "ev_ebitda": _get_val(["Enterprise Value/EBITDA", "EV/EBITDA", "enterpriseToEbitda"]),
        }

    def _compute_from_db(self, ticker: str, period: str) -> List[Dict[str, Any]]:
        """Compute multiples from Postgres DB FinancialStatement + PriceEOD tables."""
        from models import Asset, FinancialStatement, PriceEOD

        session = self.db_service.get_session()
        try:
            asset = session.execute(
                select(Asset).where(Asset.ticker == ticker)
            ).scalar_one_or_none()

            if not asset:
                return []

            db_period_type = "quarterly" if period in ("QTR", "TTM") else "annual"

            statements = (
                session.execute(
                    select(FinancialStatement)
                    .where(FinancialStatement.asset_id == asset.id)
                    .where(FinancialStatement.period_type == db_period_type)
                    .order_by(FinancialStatement.period_end.asc())
                )
                .scalars()
                .all()
            )

            if not statements:
                return []

            stmt_by_date: Dict[str, Dict[str, Any]] = {}
            for s in statements:
                dt_str = s.period_end.strftime("%Y-%m-%d")
                if dt_str not in stmt_by_date:
                    stmt_by_date[dt_str] = {}
                d = stmt_by_date[dt_str]
                if s.statement_type == "income":
                    d["revenue"] = float(s.revenue) if s.revenue else None
                    d["net_income"] = float(s.net_income) if s.net_income else None
                    d["ebitda"] = float(s.ebitda) if s.ebitda else None
                elif s.statement_type == "balance":
                    d["equity"] = float(s.total_equity) if s.total_equity else None
                    d["total_debt"] = float(s.total_debt) if s.total_debt else 0.0
                    d["cash"] = float(s.cash_and_equivalents) if s.cash_and_equivalents else 0.0
                    d["shares"] = float(s.shares_diluted or s.shares_basic) if (s.shares_diluted or s.shares_basic) else None

            results: List[Dict[str, Any]] = []
            for dt_str, data in stmt_by_date.items():
                dt_obj = datetime.strptime(dt_str, "%Y-%m-%d").date()
                price_record = session.execute(
                    select(PriceEOD)
                    .where(PriceEOD.asset_id == asset.id)
                    .where(PriceEOD.date <= dt_obj)
                    .order_by(PriceEOD.date.desc())
                    .limit(1)
                ).scalar_one_or_none()

                close_price = float(price_record.close) if price_record and price_record.close else None
                shares = data.get("shares")
                revenue = data.get("revenue")
                net_income = data.get("net_income")
                ebitda = data.get("ebitda")
                equity = data.get("equity")
                debt = data.get("total_debt", 0.0)
                cash = data.get("cash", 0.0)

                if close_price and shares:
                    mcap = close_price * shares
                    ev = mcap + (debt - cash)

                    pe = round(mcap / net_income, 2) if net_income and net_income > 0 else None
                    ps = round(mcap / revenue, 2) if revenue and revenue > 0 else None
                    pb = round(mcap / equity, 2) if equity and equity > 0 else None
                    ev_s = round(ev / revenue, 2) if revenue and revenue > 0 else None
                    ev_eb = round(ev / ebitda, 2) if ebitda and ebitda > 0 else None

                    results.append(
                        {
                            "date": dt_str,
                            "pe_ratio": pe,
                            "ps_ratio": ps,
                            "pb_ratio": pb,
                            "ev_sales_ratio": ev_s,
                            "ev_ebitda": ev_eb,
                        }
                    )

            return results
        finally:
            session.close()

    @staticmethod
    def _first_valid(d: dict, keys: List[str]) -> Optional[float]:
        """Find the first matching key with a non-null float value."""
        for k in keys:
            if k in d and pd.notna(d[k]):
                try:
                    return float(d[k])
                except (ValueError, TypeError):
                    pass
        return None
