"""
ValuationMultiplesService — Computes and aggregates historical valuation multiples
(P/E, P/S, P/B, EV/Sales, EV/EBITDA) across FY, QTR, and TTM periods.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import select

from concurrency import run_sync
from schemas.fundamentals import ValuationMultiplesPoint, ValuationMultiplesResult

logger = logging.getLogger(__name__)

CACHE_TTL_VALUATION_MULTIPLES = 21600  # 6 hours


class ValuationMultiplesService:
    """Service to compute and retrieve historical valuation multiples."""

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
        """Synchronous computation querying DB and/or external providers."""
        points_map: Dict[str, Dict[str, Any]] = {}

        # 1. Try DB first if db_service is configured
        if self.db_service is not None:
            try:
                db_points = self._compute_from_db(ticker, period)
                for pt in db_points:
                    points_map[pt["date"]] = pt
            except Exception as exc:
                logger.debug("Multiples DB computation failed for %s: %s", ticker, exc)

        # 2. Try yfinance to enrich or generate historical valuation multiples
        try:
            yf_points = self._compute_from_yfinance(ticker, period)
            for pt in yf_points:
                dt = pt["date"]
                if dt not in points_map:
                    points_map[dt] = pt
                else:
                    # Update missing fields
                    for k, v in pt.items():
                        if v is not None and points_map[dt].get(k) is None:
                            points_map[dt][k] = v
        except Exception as exc:
            logger.warning("Multiples yfinance computation failed for %s: %s", ticker, exc)

        # 3. Sort chronologically
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
                            # Use current as the latest TTM point
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

                # Extract statement dates
                cols = list(fin_df.columns)
                for col_dt in cols:
                    dt_str = pd.to_datetime(col_dt).strftime("%Y-%m-%d")
                    if dt_str in points_by_date and points_by_date[dt_str].get("pe_ratio") is not None:
                        continue

                    fin_col = fin_df[col_dt].to_dict() if col_dt in fin_df else {}
                    bs_col = bs_df[col_dt].to_dict() if bs_df is not None and col_dt in bs_df else {}

                    # Close price on or just before statement date
                    close_price = None
                    if close_series is not None:
                        ts = pd.to_datetime(col_dt).tz_localize(close_series.index.tz)
                        prior_prices = close_series[close_series.index <= ts]
                        if not prior_prices.empty:
                            close_price = float(prior_prices.iloc[-1])

                    # Financial metrics
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

            # Group statements by period_end
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
