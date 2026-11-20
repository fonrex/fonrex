"""Schemas of the risk route (``/risk``)."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DrawdownInfo(BaseModel):
    value: float = Field(..., description="Deepest fall from a peak close, a negative ratio")
    peak: date
    trough: date
    recovery: Optional[date] = Field(None, description="First close back at the peak, if any")


class RiskFreeRateInfo(BaseModel):
    """The risk-free rate used for Sharpe and Sortino, or why there is none."""

    currency: Optional[str] = Field(None, description="Major currency of the listing (GBX → GBP)")
    source: Optional[str] = None
    mean: Optional[float] = Field(None, description="Mean annual rate over the days measured")
    first_observation: Optional[date] = Field(
        None, description="First day (USD) or month (OECD) of the source used"
    )
    last_observation: Optional[date] = Field(
        None, description="Last day (USD) or month (OECD) of the source used"
    )
    reason: Optional[str] = Field(None, description="Why no rate was used")


class RiskResponse(BaseModel):
    """Risk and risk-adjusted performance of a listing over its last years of daily closes."""

    ticker: str
    listing: Dict[str, Optional[str]]
    years: int = Field(..., description="Years asked, before the last close stored")
    start: date = Field(..., description="First close measured")
    end: date = Field(..., description="Last close measured")
    returns: int = Field(..., description="Daily returns measured")
    confidence: float = Field(..., description="Confidence of the value at risk")
    annualized_return: float = Field(..., description="Geometric, 252 days a year")
    annualized_volatility: Optional[float] = Field(
        None, description="Sample standard deviation of the daily returns × √252"
    )
    max_drawdown: Optional[DrawdownInfo] = None
    calmar_ratio: Optional[float] = None
    historical_var: float = Field(..., description="Daily return quantile, a negative ratio")
    historical_cvar: float = Field(..., description="Mean daily return beyond the VaR")
    gaussian_var: Optional[float] = None
    gaussian_cvar: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    risk_free_rate: RiskFreeRateInfo
    unit: str = Field("ratio", description="Every figure is a ratio: 0.12 for 12 %")
    warnings: List[str]
