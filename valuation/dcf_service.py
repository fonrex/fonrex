#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DCFService — Computes DCF valuations (FCF, EPS, DDM) and sensitivity analysis.
"""

import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Literal, Optional

from sqlalchemy import select

from concurrency import run_sync
from models import (
    AnalystRatings,
    Asset,
    AssetListing,
    EarningsTrend,
    FinancialStatement,
    FundamentalsHighlights,
)
from schemas.dcf import (
    DCFModelResult,
    DCFRequest,
    DCFResult,
    SensitivityCell,
    SensitivityResult,
    WACCInput,
    WACCResult,
)

logger = logging.getLogger(__name__)


class DCFService:
    """Financial valuation service using Discounted Cash Flow (DCF) methodology."""

    def __init__(self, db_service, redis_client=None):
        self.db_service = db_service
        self.redis = redis_client

    def _dec(self, val) -> Decimal:
        """Safely converts a value to Decimal, returning 0 if None."""
        if val is None:
            return Decimal("0")
        if isinstance(val, Decimal):
            return val
        return Decimal(str(val))

    def _safe_div(self, num: Decimal, denom: Decimal, fallback: Decimal = Decimal("0")) -> Decimal:
        """Safe division to avoid ZeroDivisionError."""
        if denom == 0:
            return fallback
        return num / denom

    async def compute_dcf(self, ticker: str, request: DCFRequest) -> DCFResult:
        """Run the synchronous SQLAlchemy valuation workflow off the event loop."""
        return await run_sync(self._compute_dcf_sync, ticker, request)

    def _compute_dcf_sync(self, ticker: str, request: DCFRequest) -> DCFResult:
        """
        Computes the DCF valuation for a given ticker.
        """
        session = self.db_service.get_session()
        try:
            # 1. Resolve the asset
            stmt_asset = (
                select(Asset)
                .join(AssetListing, AssetListing.asset_id == Asset.id)
                .where(AssetListing.ticker == ticker)
                .where(AssetListing.is_active.is_(True))
                .limit(1)
            )
            result_asset = session.execute(stmt_asset)
            asset = result_asset.scalars().first()
            if not asset:
                raise ValueError(f"Ticker {ticker} not found or inactive.")

            # 2. Load financial data
            # Highlights
            stmt_hl = select(FundamentalsHighlights).where(
                FundamentalsHighlights.asset_id == asset.id
            )
            highlights = session.execute(stmt_hl).scalars().first()
            if not highlights:
                raise ValueError(f"FundamentalsHighlights data missing for {ticker}.")

            # Statements (last 5 years)
            stmt_stmt = (
                select(FinancialStatement)
                .where(FinancialStatement.asset_id == asset.id)
                .where(FinancialStatement.period_type == "annual")
                .order_by(FinancialStatement.period_end.desc())
                .limit(5)
            )
            statements = session.execute(stmt_stmt).scalars().all()
            if not statements:
                raise ValueError(f"Annual financial statements missing for {ticker}.")

            # EarningsTrend
            stmt_trend = select(EarningsTrend).where(EarningsTrend.asset_id == asset.id)
            trends = session.execute(stmt_trend).scalars().all()
            trends_dict = {t.period: t for t in trends}

            # AnalystRatings
            stmt_ratings = select(AnalystRatings).where(AnalystRatings.asset_id == asset.id)
            ratings = session.execute(stmt_ratings).scalars().first()

            # 3. Retrieve current share price and shares outstanding
            current_price = self._dec(highlights.week_52_high + highlights.week_52_low) / Decimal(
                "2"
            )
            if highlights.pe_ratio and highlights.eps_trailing:
                current_price = self._dec(highlights.pe_ratio) * self._dec(highlights.eps_trailing)

            # Use the most recent price_eod if available
            from models import PriceEOD

            stmt_price = (
                select(PriceEOD.close)
                .where(PriceEOD.asset_id == asset.id)
                .order_by(PriceEOD.timestamp.desc())
                .limit(1)
            )
            latest_price_db = session.execute(stmt_price).scalar()
            if latest_price_db:
                current_price = self._dec(latest_price_db)

            shares = highlights.shares_outstanding or 0
            if not shares and statements:
                shares = statements[0].shares_diluted or statements[0].shares_basic or 0
            shares = int(shares)
            if shares <= 0:
                raise ValueError(f"Invalid or missing shares outstanding for {ticker}.")

            # 4. Compute WACC
            wacc_res = self._compute_wacc(highlights, statements, request.wacc_params)

            # 5. Compute requested models
            model_results: Dict[str, DCFModelResult] = {}
            for model_name in request.models:
                if model_name == "fcf":
                    model_results["fcf"] = self._dcf_fcf(
                        statements=statements,
                        wacc=wacc_res.wacc,
                        shares_outstanding=shares,
                        projection_years=request.projection_years,
                        terminal_growth=request.terminal_growth_rate,
                        growth_override=request.fcf_growth_override,
                    )
                elif model_name == "eps":
                    model_results["eps"] = self._dcf_eps(
                        statements=statements,
                        trend_dict=trends_dict,
                        wacc=wacc_res.wacc,
                        cost_of_equity=wacc_res.cost_of_equity,
                        highlights=highlights,
                        shares_outstanding=shares,
                        projection_years=request.projection_years,
                        terminal_growth=request.terminal_growth_rate,
                        growth_override=request.eps_growth_override,
                    )
                elif model_name == "ddm":
                    model_results["ddm"] = self._dcf_ddm(
                        highlights=highlights,
                        statements=statements,
                        cost_of_equity=wacc_res.cost_of_equity,
                        projection_years=request.projection_years,
                        terminal_growth=request.terminal_growth_rate,
                        growth_override=request.dividend_growth_override,
                    )

            # 6. Compute consensus value (weighted average)
            consensus_val = None
            consensus_upside = None
            if model_results:
                # Default weights: FCF=50%, EPS=30%, DDM=20%
                weights = request.model_weights or {
                    "fcf": Decimal("0.5"),
                    "eps": Decimal("0.3"),
                    "ddm": Decimal("0.2"),
                }
                # Normalize weights for computed and available models
                active_weights = {k: self._dec(v) for k, v in weights.items() if k in model_results}
                sum_weights = sum(active_weights.values())

                if sum_weights > 0:
                    weighted_sum = Decimal("0")
                    for k, val_res in model_results.items():
                        weight = active_weights[k] / sum_weights
                        weighted_sum += val_res.intrinsic_value_per_share * weight
                    consensus_val = weighted_sum.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    if current_price > 0:
                        consensus_upside = (
                            self._safe_div(consensus_val - current_price, current_price)
                            * Decimal("100")
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Analyst target price
            analyst_target = (
                self._dec(ratings.target_mean) if ratings and ratings.target_mean else None
            )

            # Build the response
            return DCFResult(
                ticker=ticker,
                currency=asset.currency or "USD",
                current_price=current_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                shares_outstanding=shares,
                wacc=wacc_res,
                models=model_results,
                consensus_value=consensus_val,
                consensus_upside_pct=consensus_upside,
                analyst_target=analyst_target,
                computed_at=datetime.now(timezone.utc),
            )

        finally:
            session.close()

    def _compute_wacc(
        self,
        highlights: FundamentalsHighlights,
        statements: List[FinancialStatement],
        params: Optional[WACCInput],
    ) -> WACCResult:
        """Computes the Weighted Average Cost of Capital (WACC)."""
        # Default base parameters
        risk_free = Decimal("0.04")  # 4%
        erp = Decimal("0.055")  # 5.5%
        beta = self._dec(highlights.beta) if highlights.beta else Decimal("1.0")

        # User overrides
        if params:
            if params.risk_free_rate is not None:
                risk_free = params.risk_free_rate
            if params.equity_risk_premium is not None:
                erp = params.equity_risk_premium
            if params.beta_override is not None:
                beta = params.beta_override

        # Cost of equity: Ke = Rf + Beta * ERP
        cost_of_equity = risk_free + beta * erp

        # Cost of debt (Kd) and tax rate
        # Use data from the most recent available balance sheet
        latest_statement = statements[0] if statements else None

        interest_expense = Decimal("0")
        ebit = Decimal("0")
        tax_provision = Decimal("0")
        total_debt = Decimal("0")
        total_equity = Decimal("0")

        if latest_statement:
            interest_expense = self._dec(latest_statement.interest_expense)
            ebit = self._dec(latest_statement.ebit or latest_statement.operating_income)
            tax_provision = self._dec(latest_statement.tax_provision)
            total_debt = self._dec(latest_statement.total_debt)
            total_equity = self._dec(latest_statement.total_equity)

        # Cost of debt: Kd = interest_expense / total_debt
        cost_of_debt = self._safe_div(
            interest_expense, total_debt, fallback=risk_free + Decimal("0.02")
        )
        if params and params.cost_of_debt_override is not None:
            cost_of_debt = params.cost_of_debt_override

        # Effective tax rate: t = tax_provision / ebit
        tax_rate = self._safe_div(tax_provision, ebit, fallback=Decimal("0.25"))
        if tax_rate < 0 or tax_rate > 1:
            tax_rate = Decimal("0.25")
        if params and params.tax_rate_override is not None:
            tax_rate = params.tax_rate_override

        # Capital structure weights (Market Cap vs Debt)
        market_cap = self._dec(highlights.market_cap)
        if market_cap <= 0:
            market_cap = total_equity
        if market_cap <= 0:
            market_cap = Decimal("1000000")  # minimal dummy value to avoid division by zero

        total_capital = market_cap + total_debt
        weight_equity = self._safe_div(market_cap, total_capital, fallback=Decimal("1.0"))
        weight_debt = self._safe_div(total_debt, total_capital, fallback=Decimal("0.0"))

        # WACC = We * Ke + Wd * Kd * (1 - t)
        wacc = (weight_equity * cost_of_equity) + (
            weight_debt * cost_of_debt * (Decimal("1.0") - tax_rate)
        )

        # Clamp WACC between 5% and 20%
        wacc = max(Decimal("0.05"), min(Decimal("0.20"), wacc))

        return WACCResult(
            wacc=wacc.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            cost_of_equity=cost_of_equity.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            cost_of_debt=cost_of_debt.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            tax_rate=tax_rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            weight_equity=weight_equity.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            weight_debt=weight_debt.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            beta_used=beta.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        )

    def _dcf_fcf(
        self,
        statements: List[FinancialStatement],
        wacc: Decimal,
        shares_outstanding: int,
        projection_years: int,
        terminal_growth: Decimal,
        growth_override: Optional[Decimal],
    ) -> DCFModelResult:
        """Valuation using Free Cash Flow (FCF) discounted cash flow model."""
        warnings = []

        # 1. Base FCF — average of the last 3 FCF values
        fcfs = [self._dec(s.free_cashflow) for s in statements[:3] if s.free_cashflow is not None]
        if not fcfs:
            # Fallback: compute via Operating Cash Flow - CapEx, or Net Income
            fcfs = []
            for s in statements[:3]:
                ni = self._dec(s.net_income)
                ocf = self._dec(s.operating_cashflow)
                capex = self._dec(s.capex)
                if ocf != 0:
                    fcfs.append(ocf - abs(capex))
                else:
                    fcfs.append(ni)
            warnings.append(
                "Free cash flow not directly available; computed via Operating Cash Flow or Net Income."
            )

        if not fcfs:
            fcfs = [Decimal("0")]
            warnings.append("No cash flow data found. Base set to 0.")

        base_fcf = sum(fcfs) / len(fcfs)
        if base_fcf < 0:
            warnings.append(
                f"Base cash flow is negative ({base_fcf.quantize(Decimal('0.01'))}). Valuation may be skewed."
            )

        # 2. Growth rate
        growth_rate = Decimal("0.04")  # default 4%
        if growth_override is not None:
            growth_rate = growth_override
        else:
            # Compute historical FCF CAGR over available statements (up to 5 years)
            valid_fcfs = [
                self._dec(s.free_cashflow)
                for s in reversed(statements)
                if s.free_cashflow is not None
            ]
            if len(valid_fcfs) >= 2 and valid_fcfs[0] > 0 and valid_fcfs[-1] > 0:
                n = len(valid_fcfs) - 1
                try:
                    cagr = (valid_fcfs[-1] / valid_fcfs[0]) ** Decimal(str(1 / n)) - Decimal("1.0")
                    # Clamp growth between 0% and 20% for conservatism
                    growth_rate = max(Decimal("0.0"), min(Decimal("0.20"), cagr))
                except (ArithmeticError, ValueError):
                    pass
            else:
                # Fallback: use revenue CAGR
                revs = [self._dec(s.revenue) for s in reversed(statements) if s.revenue is not None]
                if len(revs) >= 2 and revs[0] > 0 and revs[-1] > 0:
                    n = len(revs) - 1
                    try:
                        cagr_rev = (revs[-1] / revs[0]) ** Decimal(str(1 / n)) - Decimal("1.0")
                        growth_rate = max(Decimal("0.0"), min(Decimal("0.20"), cagr_rev))
                        warnings.append(
                            f"FCF CAGR unavailable; using Revenue CAGR ({growth_rate.quantize(Decimal('0.0001'))})"
                        )
                    except (ArithmeticError, ValueError):
                        pass
                else:
                    warnings.append("Historical growth rate not computable; using default 4%.")

        # Guard: terminal growth must stay below WACC
        if terminal_growth >= wacc - Decimal("0.005"):
            terminal_growth = wacc - Decimal("0.005")
            warnings.append(
                f"Terminal growth rate too high. Clamped to {terminal_growth.quantize(Decimal('0.0001'))} to prevent divergence."
            )

        # 3. Project FCF
        projected_fcfs = []
        current_fcf = base_fcf
        for _ in range(projection_years):
            current_fcf = current_fcf * (Decimal("1.0") + growth_rate)
            projected_fcfs.append(current_fcf)

        # 4. Discount to present value
        present_values = []
        for t, fcf in enumerate(projected_fcfs, start=1):
            pv = fcf / ((Decimal("1.0") + wacc) ** Decimal(str(t)))
            present_values.append(pv)

        # 5. Terminal Value (Gordon Growth Model)
        fcf_N = projected_fcfs[-1]
        terminal_value = self._safe_div(
            fcf_N * (Decimal("1.0") + terminal_growth), wacc - terminal_growth
        )
        pv_terminal = terminal_value / ((Decimal("1.0") + wacc) ** Decimal(str(projection_years)))

        # 6. Enterprise Value
        enterprise_value = sum(present_values) + pv_terminal

        # 7. Equity Value = EV - Net Debt (Total Debt - Cash)
        latest_stmt = statements[0] if statements else None
        total_debt = self._dec(latest_stmt.total_debt) if latest_stmt else Decimal("0")
        cash = self._dec(latest_stmt.cash_and_equivalents) if latest_stmt else Decimal("0")

        net_debt = total_debt - cash
        equity_value = enterprise_value - net_debt

        intrinsic_value_per_share = self._safe_div(equity_value, Decimal(str(shares_outstanding)))
        if intrinsic_value_per_share < 0:
            intrinsic_value_per_share = Decimal("0")
            warnings.append("Computed intrinsic value is negative. Set to 0.")

        # Upside (computed at endpoint level with current price)
        upside = Decimal("0")

        return DCFModelResult(
            model_name="FCF DCF",
            intrinsic_value_per_share=intrinsic_value_per_share.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            upside_pct=upside,
            projected_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in projected_fcfs
            ],
            terminal_value=terminal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            present_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in present_values
            ],
            pv_terminal=pv_terminal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            warnings=warnings,
        )

    def _dcf_eps(
        self,
        statements: List[FinancialStatement],
        trend_dict: Dict,
        wacc: Decimal,
        cost_of_equity: Decimal,
        highlights: FundamentalsHighlights,
        shares_outstanding: int,
        projection_years: int,
        terminal_growth: Decimal,
        growth_override: Optional[Decimal],
    ) -> DCFModelResult:
        """Valuation using Earnings Per Share (EPS) discounted cash flow model."""
        warnings = []

        # 1. Base EPS
        base_eps = self._dec(highlights.eps_trailing)
        if base_eps <= 0 and statements:
            base_eps = self._dec(statements[0].eps_diluted or statements[0].eps_basic)

        if base_eps <= 0:
            base_eps = Decimal("0.01")
            warnings.append("Base EPS is negative or zero. Set to 0.01 for projection.")

        # 2. Growth rate
        growth_rate = Decimal("0.05")  # default 5%
        if growth_override is not None:
            growth_rate = growth_override
        else:
            # Try reading growth from analyst trends ("0y" or "+1y")
            trend_0y = trend_dict.get("0y")
            trend_1y = trend_dict.get("+1y")
            if trend_0y and trend_0y.eps_growth is not None:
                growth_rate = self._dec(trend_0y.eps_growth)
            elif trend_1y and trend_1y.eps_growth is not None:
                growth_rate = self._dec(trend_1y.eps_growth)
            else:
                # Historical EPS CAGR from available statements
                eps_history = [
                    self._dec(s.eps_diluted)
                    for s in reversed(statements)
                    if s.eps_diluted is not None
                ]
                if len(eps_history) >= 2 and eps_history[0] > 0 and eps_history[-1] > 0:
                    n = len(eps_history) - 1
                    try:
                        cagr_eps = (eps_history[-1] / eps_history[0]) ** Decimal(
                            str(1 / n)
                        ) - Decimal("1.0")
                        growth_rate = max(Decimal("0.0"), min(Decimal("0.20"), cagr_eps))
                    except (ArithmeticError, ValueError):
                        pass
                else:
                    warnings.append("No analyst estimate nor valid EPS CAGR. Growth fixed at 5%.")

        # Guard: terminal growth vs Cost of Equity (EPS is an equity measure, discounted by Ke)
        if terminal_growth >= cost_of_equity - Decimal("0.005"):
            terminal_growth = cost_of_equity - Decimal("0.005")
            warnings.append(
                f"Terminal growth rate too high. Clamped to {terminal_growth.quantize(Decimal('0.0001'))} to prevent divergence."
            )

        # 3. Project EPS with linear fade toward terminal growth
        projected_eps = []
        current_eps = base_eps
        for t in range(1, projection_years + 1):
            # Linearly transition growth rate from initial to terminal
            factor = Decimal(str(t / projection_years))
            yearly_growth = growth_rate * (Decimal("1.0") - factor) + terminal_growth * factor
            current_eps = current_eps * (Decimal("1.0") + yearly_growth)
            projected_eps.append(current_eps)

        # 4. Discount by cost of equity (Ke), since EPS is an equity metric
        present_values = []
        for t, eps in enumerate(projected_eps, start=1):
            pv = eps / ((Decimal("1.0") + cost_of_equity) ** Decimal(str(t)))
            present_values.append(pv)

        # 5. Terminal Value using target P/E multiple applied to final projected EPS
        pe_ratio = self._dec(highlights.pe_ratio or highlights.pe_forward)
        if pe_ratio <= 0:
            pe_ratio = Decimal("15.0")
        else:
            # Clamp target P/E between 10 and 30 for conservatism
            pe_ratio = max(Decimal("10.0"), min(Decimal("30.0"), pe_ratio))

        eps_N = projected_eps[-1]
        terminal_value = eps_N * pe_ratio
        pv_terminal = terminal_value / (
            (Decimal("1.0") + cost_of_equity) ** Decimal(str(projection_years))
        )

        # 6. Intrinsic value per share (direct equity measure)
        intrinsic_value_per_share = sum(present_values) + pv_terminal
        if intrinsic_value_per_share < 0:
            intrinsic_value_per_share = Decimal("0")

        return DCFModelResult(
            model_name="EPS DCF (Target Multiple)",
            intrinsic_value_per_share=intrinsic_value_per_share.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            upside_pct=Decimal("0"),
            projected_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in projected_eps
            ],
            terminal_value=terminal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            present_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in present_values
            ],
            pv_terminal=pv_terminal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            warnings=warnings,
        )

    def _dcf_ddm(
        self,
        highlights: FundamentalsHighlights,
        statements: List[FinancialStatement],
        cost_of_equity: Decimal,
        projection_years: int,
        terminal_growth: Decimal,
        growth_override: Optional[Decimal],
    ) -> DCFModelResult:
        """Valuation using Dividend Discount Model (DDM)."""
        warnings = []

        # 1. Base dividend per share
        base_div = self._dec(highlights.dividend_rate)
        if (
            base_div <= 0
            and highlights.dividend_yield
            and highlights.pe_ratio
            and highlights.eps_trailing
        ):
            # Attempt to reconstruct dividend from yield, P/E and EPS
            base_div = (
                self._dec(highlights.dividend_yield)
                * self._dec(highlights.pe_ratio)
                * self._dec(highlights.eps_trailing)
            )

        if base_div <= 0 and statements:
            # Dividends paid / shares outstanding
            latest_stmt = statements[0]
            div_paid = abs(self._dec(latest_stmt.dividends_paid))
            shares = (
                latest_stmt.shares_diluted
                or latest_stmt.shares_basic
                or highlights.shares_outstanding
                or 0
            )
            base_div = self._safe_div(div_paid, self._dec(shares))

        if base_div <= 0:
            raise ValueError(
                "Base dividend per share is zero or missing. DDM model cannot be applied."
            )

        # 2. Growth rate
        growth_rate = Decimal("0.03")  # default 3%
        if growth_override is not None:
            growth_rate = growth_override
        else:
            # Historical dividend CAGR from paid dividends
            div_history = [
                abs(self._dec(s.dividends_paid))
                for s in reversed(statements)
                if s.dividends_paid is not None
            ]
            if len(div_history) >= 2 and div_history[0] > 0 and div_history[-1] > 0:
                n = len(div_history) - 1
                try:
                    cagr_div = (div_history[-1] / div_history[0]) ** Decimal(str(1 / n)) - Decimal(
                        "1.0"
                    )
                    growth_rate = max(Decimal("0.0"), min(Decimal("0.15"), cagr_div))
                except (ArithmeticError, ValueError):
                    pass
            else:
                warnings.append("Insufficient dividend history. Default growth rate set to 3%.")

        # Guard: terminal growth vs Cost of Equity
        if terminal_growth >= cost_of_equity - Decimal("0.005"):
            terminal_growth = cost_of_equity - Decimal("0.005")
            warnings.append(
                f"Terminal growth rate too high. Clamped to {terminal_growth.quantize(Decimal('0.0001'))} to prevent divergence."
            )

        # 3. Project dividends
        projected_divs = []
        current_div = base_div
        for _ in range(projection_years):
            current_div = current_div * (Decimal("1.0") + growth_rate)
            projected_divs.append(current_div)

        # 4. Discount to present value
        present_values = []
        for t, div in enumerate(projected_divs, start=1):
            pv = div / ((Decimal("1.0") + cost_of_equity) ** Decimal(str(t)))
            present_values.append(pv)

        # 5. Terminal Value (Gordon Growth Model on final projected DPS)
        div_N = projected_divs[-1]
        terminal_value = self._safe_div(
            div_N * (Decimal("1.0") + terminal_growth), cost_of_equity - terminal_growth
        )
        pv_terminal = terminal_value / (
            (Decimal("1.0") + cost_of_equity) ** Decimal(str(projection_years))
        )

        # 6. Intrinsic value per share
        intrinsic_value_per_share = sum(present_values) + pv_terminal
        if intrinsic_value_per_share < 0:
            intrinsic_value_per_share = Decimal("0")

        return DCFModelResult(
            model_name="Dividend Discount Model (Gordon)",
            intrinsic_value_per_share=intrinsic_value_per_share.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            upside_pct=Decimal("0"),
            projected_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in projected_divs
            ],
            terminal_value=terminal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            present_values=[
                v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for v in present_values
            ],
            pv_terminal=pv_terminal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            warnings=warnings,
        )

    def compute_sensitivity(
        self,
        ticker: str,
        model_name: Literal["fcf", "eps", "ddm"],
        wacc_range: List[Decimal],
        growth_range: List[Decimal],
    ) -> SensitivityResult:
        """
        Computes a WACC × Terminal Growth sensitivity matrix using numpy.
        """
        # Load base data to quickly re-run the calculation
        session = self.db_service.get_session()
        try:
            # Resolve the asset
            stmt_asset = (
                select(Asset)
                .join(AssetListing, AssetListing.asset_id == Asset.id)
                .where(AssetListing.ticker == ticker)
                .where(AssetListing.is_active.is_(True))
                .limit(1)
            )
            asset = session.execute(stmt_asset).scalars().first()
            if not asset:
                raise ValueError(f"Ticker {ticker} not found or inactive.")

            # Highlights
            stmt_hl = select(FundamentalsHighlights).where(
                FundamentalsHighlights.asset_id == asset.id
            )
            highlights = session.execute(stmt_hl).scalars().first()
            if not highlights:
                raise ValueError(f"Highlights missing for {ticker}.")

            # Statements
            stmt_stmt = (
                select(FinancialStatement)
                .where(FinancialStatement.asset_id == asset.id)
                .where(FinancialStatement.period_type == "annual")
                .order_by(FinancialStatement.period_end.desc())
                .limit(5)
            )
            statements = session.execute(stmt_stmt).scalars().all()
            if not statements:
                raise ValueError(f"Statements missing for {ticker}.")

            # 1. Retrieve common variables
            # Current price
            current_price = self._dec(highlights.week_52_high + highlights.week_52_low) / Decimal(
                "2"
            )
            if highlights.pe_ratio and highlights.eps_trailing:
                current_price = self._dec(highlights.pe_ratio) * self._dec(highlights.eps_trailing)

            from models import PriceEOD

            stmt_price = (
                select(PriceEOD.close)
                .where(PriceEOD.asset_id == asset.id)
                .order_by(PriceEOD.timestamp.desc())
                .limit(1)
            )
            latest_price_db = session.execute(stmt_price).scalar()
            if latest_price_db:
                current_price = self._dec(latest_price_db)

            shares = highlights.shares_outstanding or 0
            if not shares and statements:
                shares = statements[0].shares_diluted or statements[0].shares_basic or 0
            shares = int(shares)
            if shares <= 0:
                raise ValueError("Invalid shares outstanding.")

            # Extract base values and growth rates for each model
            # Re-run once with nominal values to derive intermediate parameters
            base_val = Decimal("0")
            growth_rate = Decimal("0.04")

            if model_name == "fcf":
                # FCF base value
                fcfs = [
                    self._dec(s.free_cashflow)
                    for s in statements[:3]
                    if s.free_cashflow is not None
                ]
                if not fcfs:
                    fcfs = [
                        self._dec(s.operating_cashflow) - abs(self._dec(s.capex))
                        for s in statements[:3]
                    ]
                base_val = sum(fcfs) / len(fcfs) if fcfs else Decimal("0")

                # Historical FCF CAGR
                growth_rate = Decimal("0.04")
                valid_fcfs = [
                    self._dec(s.free_cashflow)
                    for s in reversed(statements)
                    if s.free_cashflow is not None
                ]
                if len(valid_fcfs) >= 2 and valid_fcfs[0] > 0 and valid_fcfs[-1] > 0:
                    n = len(valid_fcfs) - 1
                    try:
                        growth_rate = (valid_fcfs[-1] / valid_fcfs[0]) ** Decimal(
                            str(1 / n)
                        ) - Decimal("1.0")
                    except (ArithmeticError, ValueError):
                        pass
                else:
                    revs = [
                        self._dec(s.revenue) for s in reversed(statements) if s.revenue is not None
                    ]
                    if len(revs) >= 2 and revs[0] > 0 and revs[-1] > 0:
                        n = len(revs) - 1
                        try:
                            growth_rate = (revs[-1] / revs[0]) ** Decimal(str(1 / n)) - Decimal(
                                "1.0"
                            )
                        except (ArithmeticError, ValueError):
                            pass
                growth_rate = max(Decimal("0.0"), min(Decimal("0.20"), growth_rate))

            elif model_name == "eps":
                # EPS base value
                base_val = self._dec(highlights.eps_trailing)
                if base_val <= 0 and statements:
                    base_val = self._dec(statements[0].eps_diluted or statements[0].eps_basic)
                if base_val <= 0:
                    base_val = Decimal("0.01")

                # Historical EPS CAGR
                eps_history = [
                    self._dec(s.eps_diluted)
                    for s in reversed(statements)
                    if s.eps_diluted is not None
                ]
                if len(eps_history) >= 2 and eps_history[0] > 0 and eps_history[-1] > 0:
                    n = len(eps_history) - 1
                    try:
                        growth_rate = (eps_history[-1] / eps_history[0]) ** Decimal(
                            str(1 / n)
                        ) - Decimal("1.0")
                    except (ArithmeticError, ValueError):
                        pass
                growth_rate = max(Decimal("0.0"), min(Decimal("0.20"), growth_rate))

            elif model_name == "ddm":
                # DDM base value
                base_val = self._dec(highlights.dividend_rate)
                if base_val <= 0:
                    latest_stmt = statements[0]
                    div_paid = abs(self._dec(latest_stmt.dividends_paid))
                    s_shares = (
                        latest_stmt.shares_diluted
                        or latest_stmt.shares_basic
                        or highlights.shares_outstanding
                        or 0
                    )
                    base_val = self._safe_div(div_paid, self._dec(s_shares))
                if base_val <= 0:
                    raise ValueError("Zero dividends.")

                # Historical dividend CAGR
                div_history = [
                    abs(self._dec(s.dividends_paid))
                    for s in reversed(statements)
                    if s.dividends_paid is not None
                ]
                if len(div_history) >= 2 and div_history[0] > 0 and div_history[-1] > 0:
                    n = len(div_history) - 1
                    try:
                        growth_rate = (div_history[-1] / div_history[0]) ** Decimal(
                            str(1 / n)
                        ) - Decimal("1.0")
                    except (ArithmeticError, ValueError):
                        pass
                growth_rate = max(Decimal("0.0"), min(Decimal("0.15"), growth_rate))

            # 2. Vectorized computation with numpy
            # Convert ranges to float arrays for numpy
            # Output matrix: len(wacc_range) x len(growth_range)
            results_matrix = []

            # Convert common variables to float for performance
            float_base = float(base_val)
            float_growth_rate = float(growth_rate)
            float_shares = float(shares)
            float_price = float(current_price)

            # Balance sheet values for FCF model
            float_debt = float(self._dec(statements[0].total_debt)) if statements else 0.0
            float_cash = float(self._dec(statements[0].cash_and_equivalents)) if statements else 0.0
            float_net_debt = float_debt - float_cash

            # Target P/E for EPS model
            float_pe = float(self._dec(highlights.pe_ratio or highlights.pe_forward))
            if float_pe <= 0:
                float_pe = 15.0
            float_pe = max(10.0, min(30.0, float_pe))

            # Iterate over the WACC × growth grid
            for w_val in wacc_range:
                row_cells = []
                w = float(w_val)
                for g_val in growth_range:
                    g = float(g_val)

                    # Guard: prevent divergence if g >= w
                    if g >= w - 0.005:
                        g_used = w - 0.005
                    else:
                        g_used = g

                    intrinsic_value = 0.0

                    if model_name == "fcf":
                        # 5-year FCF projection
                        proj = [float_base * ((1.0 + float_growth_rate) ** t) for t in range(1, 6)]
                        pv_list = [val / ((1.0 + w) ** t) for t, val in enumerate(proj, start=1)]
                        # Terminal Value
                        tv = (proj[-1] * (1.0 + g_used)) / (w - g_used) if (w - g_used) > 0 else 0.0
                        pv_tv = tv / ((1.0 + w) ** 5)
                        ev = sum(pv_list) + pv_tv
                        equity_val = ev - float_net_debt
                        intrinsic_value = max(0.0, equity_val / float_shares)

                    elif model_name == "eps":
                        # 5-year EPS projection with linear growth fade
                        proj = []
                        curr = float_base
                        for t in range(1, 6):
                            factor = t / 5.0
                            yr_growth = float_growth_rate * (1.0 - factor) + g_used * factor
                            curr = curr * (1.0 + yr_growth)
                            proj.append(curr)
                        pv_list = [val / ((1.0 + w) ** t) for t, val in enumerate(proj, start=1)]
                        # Terminal Value via target P/E
                        tv = proj[-1] * float_pe
                        pv_tv = tv / ((1.0 + w) ** 5)
                        intrinsic_value = max(0.0, sum(pv_list) + pv_tv)

                    elif model_name == "ddm":
                        # 5-year dividend projection
                        proj = [float_base * ((1.0 + float_growth_rate) ** t) for t in range(1, 6)]
                        pv_list = [val / ((1.0 + w) ** t) for t, val in enumerate(proj, start=1)]
                        # Terminal Value (Gordon Growth)
                        tv = (proj[-1] * (1.0 + g_used)) / (w - g_used) if (w - g_used) > 0 else 0.0
                        pv_tv = tv / ((1.0 + w) ** 5)
                        intrinsic_value = max(0.0, sum(pv_list) + pv_tv)

                    # Convert to Decimal for the cell
                    dec_val = Decimal(str(intrinsic_value)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    upside = Decimal("0")
                    if float_price > 0:
                        upside = (
                            self._safe_div(dec_val - current_price, current_price) * Decimal("100")
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    row_cells.append(
                        SensitivityCell(
                            wacc=w_val,
                            terminal_growth=g_val,
                            intrinsic_value=dec_val,
                            upside_pct=upside,
                        )
                    )
                results_matrix.append(row_cells)

            return SensitivityResult(
                ticker=ticker,
                model=model_name,
                wacc_range=wacc_range,
                growth_range=growth_range,
                matrix=results_matrix,
            )

        finally:
            session.close()
