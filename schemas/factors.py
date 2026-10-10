"""Schemas of the factor routes (``/factors``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

SOURCE = (
    "Kenneth R. French Data Library, "
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html"
)


class FactorLoadInfo(BaseModel):
    """What the database holds for one file of the library."""

    dataset: str
    frequency: Literal["monthly", "daily"]
    status: Literal["fetched", "fresh", "stale", "failed", "missing"] = Field(
        ...,
        description=(
            "fetched: downloaded for this answer; fresh: downloaded less than "
            "FACTORS_REFRESH_DAYS days ago; stale: downloaded before; failed: the "
            "download failed (what was stored is kept); missing: never downloaded"
        ),
    )
    fetched_at: Optional[datetime] = None
    first_period: Optional[date] = None
    last_period: Optional[date] = None
    periods: int = 0
    source_note: Optional[str] = Field(
        None, description="Database the library built the file from, e.g. 'CRSP 202608'"
    )
    reason: Optional[str] = Field(None, description="Why the download failed")


class FactorDatasetInfo(BaseModel):
    """A dataset of the library, and what is stored of it."""

    dataset: str
    region: Literal["us", "europe", "developed"]
    label: str
    factors: List[str]
    currency: Literal["USD"] = "USD"
    loads: List[FactorLoadInfo]


class FactorDatasetsResponse(BaseModel):
    source: str = SOURCE
    datasets: List[FactorDatasetInfo]


class FactorSeriesResponse(BaseModel):
    """Factor returns of one dataset, oldest period first."""

    dataset: str
    frequency: Literal["monthly", "daily"]
    region: Literal["us", "europe", "developed"]
    label: str
    factors: List[str]
    currency: Literal["USD"] = Field(
        "USD", description="Every return of the library is in US dollars, whatever the region"
    )
    unit: Literal["ratio"] = Field("ratio", description="0.0289 for 2.89 %")
    source: str = SOURCE
    load: FactorLoadInfo
    data: List[Dict[str, Union[date, float]]] = Field(
        ...,
        description=(
            "One row per period: 'date' (the last day of the month for monthly data) "
            "and one ratio per factor; RF is the US one-month T-bill rate"
        ),
    )


class FactorRefreshResponse(BaseModel):
    source: str = SOURCE
    loads: List[FactorLoadInfo]


class Coefficient(BaseModel):
    value: float
    std_error: float
    t_stat: Optional[float] = Field(None, description="value / std_error")


class ExposureAlpha(Coefficient):
    annualized: float = Field(
        ..., description="Alpha per period times the periods of a year (12 or 252)"
    )


class FactorExposureResponse(BaseModel):
    """Regression of the excess returns of a listing on the Fama/French factors."""

    ticker: str
    listing: Dict[str, Optional[str]]
    model: Literal["ff3", "ff5", "carhart"]
    region: Literal["us", "europe", "developed"]
    frequency: Literal["monthly", "daily"]
    datasets: List[str]
    return_currency: Literal["USD"] = Field(
        "USD", description="Returns are in US dollars, as the factors"
    )
    converted_from: Optional[str] = Field(
        None, description="Currency of the prices converted with the ECB reference rates"
    )
    start: date = Field(..., description="First period of the regression")
    end: date = Field(..., description="Last period of the regression")
    periods: int
    alpha: ExposureAlpha
    betas: Dict[str, Coefficient]
    r_squared: float
    adj_r_squared: float
    residual_volatility: float = Field(
        ..., description="Annualised standard deviation of the residuals (idiosyncratic risk)"
    )
    warnings: List[str]
    source: str = SOURCE
