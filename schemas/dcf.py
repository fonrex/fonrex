#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pydantic schemas of the DCF valuation.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from schemas.macro import SolvencyRatios
from settings import env_decimal, env_int

# Defaults of a DCF request, tunable through the environment (see .env.example).
DEFAULT_PROJECTION_YEARS = env_int("DCF_DEFAULT_PROJECTION_YEARS", 5, minimum=3, maximum=10)
# A ratio: 0.025 for 2.5 %.
DEFAULT_TERMINAL_GROWTH_RATE = env_decimal(
    "DCF_TERMINAL_GROWTH_RATE", "0.025", minimum="-1", maximum="1"
)
# Risk-free rate used when no source answers and none was ever stored. A ratio:
# 0.04 for 4 %; it may be negative (euro government rates were, in 2019-2021).
DEFAULT_RISK_FREE_RATE = env_decimal("DCF_RISK_FREE_RATE", "0.04", minimum="-0.1", maximum="0.5")


class WACCInput(BaseModel):
    """Custom inputs of the WACC."""

    risk_free_rate: Optional[Decimal] = Field(None, description="Risk-free rate (e.g. 0.04 for 4%)")
    equity_risk_premium: Optional[Decimal] = Field(
        None, description="Equity risk premium (e.g. 0.055 for 5.5%)"
    )
    beta_override: Optional[Decimal] = Field(None, description="Forced beta")
    cost_of_debt_override: Optional[Decimal] = Field(None, description="Forced cost of debt")
    tax_rate_override: Optional[Decimal] = Field(None, description="Forced tax rate")
    cost_of_equity_model: Literal["capm", "ff3", "ff5", "carhart"] = Field(
        "capm",
        description=(
            "capm: Rf + beta × equity risk premium. ff3, ff5, carhart: Rf + Σ beta × premium "
            "of the Fama/French factors, betas measured on 60 months of returns of the "
            "listing, premia the long-run means of the factors of its region"
        ),
    )


class DCFRequest(BaseModel):
    """Custom DCF request."""

    models: List[Literal["fcf", "eps", "ddm"]] = Field(default_factory=lambda: ["fcf"])
    projection_years: int = Field(
        DEFAULT_PROJECTION_YEARS,
        ge=3,
        le=10,
        description="Projection years (3 to 10)",
    )
    terminal_growth_rate: Decimal = Field(
        DEFAULT_TERMINAL_GROWTH_RATE,
        description="Terminal growth rate (e.g. 0.025 for 2.5%)",
    )
    wacc_params: Optional[WACCInput] = Field(None, description="Custom WACC parameters")
    fcf_growth_override: Optional[Decimal] = Field(
        None, description="Forces the initial FCF growth rate"
    )
    eps_growth_override: Optional[Decimal] = Field(
        None, description="Forces the initial EPS growth rate"
    )
    dividend_growth_override: Optional[Decimal] = Field(
        None, description="Forces the initial dividend growth rate"
    )
    model_weights: Optional[Dict[Literal["fcf", "eps", "ddm"], Decimal]] = Field(
        None,
        description="Custom weights of the consensus. E.g. {'fcf': 0.5, 'eps': 0.3, 'ddm': 0.2}",
    )


class FactorCostOfEquityResult(BaseModel):
    """How a Fama/French cost of equity was measured."""

    model: Literal["ff3", "ff5", "carhart"]
    region: Literal["us", "europe", "developed"]
    betas: Dict[str, Decimal] = Field(..., description="Exposure of the listing to each factor")
    premia: Dict[str, Decimal] = Field(
        ..., description="Annual long-run premium of each factor (mean monthly return × 12)"
    )
    premium: Decimal = Field(..., description="Σ beta × premium, added to Rf")
    start: date = Field(..., description="First month of the regression")
    end: date = Field(..., description="Last month of the regression")
    periods: int
    r_squared: float


class WACCResult(BaseModel):
    """Detail of the computed WACC."""

    wacc: Decimal
    cost_of_equity: Decimal
    cost_of_debt: Decimal
    tax_rate: Decimal
    weight_equity: Decimal
    weight_debt: Decimal
    beta_used: Decimal
    cost_of_debt_source: Optional[str] = Field(
        None, description="Source of Kd (client_override, calculated, sector_estimate)"
    )
    risk_free_rate: Optional[Decimal] = Field(None, description="Rf used, as a ratio")
    risk_free_rate_source: Optional[str] = Field(
        None,
        description=(
            "Source of Rf: client_override; ecb_live, ecb_cached, ecb_stale (euro AAA "
            "10-year rate, for cash flows in EUR); fred_live, fred_cached, fred_stale (US "
            "10-year Treasury rate read from FRED, for cash flows in USD); treasury_live, "
            "treasury_cached, treasury_stale (the same rate read from the Treasury yield "
            "curve, when FRED gives none) — live: read now, cached: read "
            "less than one cache lifetime ago, stale: older stored value, the source could "
            "not be read; env_fallback (DCF_RISK_FREE_RATE: no source for the currency, or "
            "nothing read)"
        ),
    )
    risk_free_rate_date: Optional[date] = Field(
        None, description="Observation date of Rf, when it comes from a source"
    )
    risk_free_rate_currency: Optional[str] = Field(
        None,
        description=(
            "Currency of the rate read from a source (EUR from the ECB, USD from FRED); "
            "null for DCF_RISK_FREE_RATE or a rate set in the request"
        ),
    )
    cost_of_equity_model: Literal["capm", "ff3", "ff5", "carhart"] = "capm"
    factor_cost_of_equity: Optional[FactorCostOfEquityResult] = Field(
        None, description="Betas and premia of a Fama/French cost of equity"
    )


class DCFModelResult(BaseModel):
    """Detailed result of one model (FCF, EPS or DDM)."""

    model_name: str
    intrinsic_value_per_share: Decimal
    upside_pct: Optional[Decimal] = Field(
        None,
        description="Upside over the share price, in percent; null when the price is in "
        "another currency than the statements",
    )
    projected_values: List[Decimal]
    terminal_value: Decimal
    present_values: List[Decimal]
    pv_terminal: Decimal
    warnings: List[str] = Field(default_factory=list)


class DCFResult(BaseModel):
    """API answer of a DCF valuation."""

    ticker: str
    currency: str = Field(..., description="Currency of the statements and of the values")
    current_price: Optional[Decimal] = None
    price_currency: Optional[str] = Field(
        None, description="Currency of current_price (converted from a minor unit such as GBX)"
    )
    warnings: List[str] = Field(default_factory=list)
    shares_outstanding: Optional[int] = None
    wacc: WACCResult
    models: Dict[str, DCFModelResult]
    solvency: Optional[SolvencyRatios] = Field(
        None, description="Solvency ratios and the underlying debt data"
    )
    consensus_value: Optional[Decimal] = None
    consensus_upside_pct: Optional[Decimal] = None
    analyst_target: Optional[Decimal] = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SensitivityCell(BaseModel):
    """A cell of the sensitivity matrix."""

    wacc: Decimal
    terminal_growth: Decimal
    intrinsic_value: Decimal
    upside_pct: Optional[Decimal] = None


class SensitivityResult(BaseModel):
    """The whole sensitivity matrix."""

    ticker: str
    model: str
    wacc_range: List[Decimal]
    growth_range: List[Decimal]
    matrix: List[List[SensitivityCell]]
