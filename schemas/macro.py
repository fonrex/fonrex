from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# A rate is always given as a ratio: 0.0412 for 4.12 %. Rows stored before this
# rule say "percent" while holding the same ratio.
RATE_UNIT = "ratio"

Freshness = Literal["live", "cached", "stale"]


class MacroRate(BaseModel):
    """An observation of a macroeconomic rate (e.g. the risk-free rate)."""

    series_id: str = Field(
        ...,
        description="Series name: FRED id (DGS10) or ECB flow.key (YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y).",
    )
    label: Optional[str] = Field(None, description="Label of the series.")
    value: Decimal = Field(
        ..., description="The rate as a ratio: 0.0412 for 4.12 %. It may be zero or negative."
    )
    unit: Optional[str] = Field(
        RATE_UNIT, description="'ratio' for a rate; 'index' for an index such as the CISS."
    )
    observation_date: date = Field(..., description="Date of the observation at the source.")
    freshness: Optional[Freshness] = Field(
        None,
        description=(
            "live: read from the source for this answer; cached: read from it less than "
            "one cache lifetime ago; stale: an older stored value, the source could not "
            "be read (no API key, or no answer)"
        ),
    )
    source: Optional[str] = Field(
        None, description="Where the series is read: 'fred', 'treasury' or 'ecb'."
    )
    currency: Optional[str] = Field(
        None,
        description="Currency (or currency area) of the series: 'USD' for FRED, 'EUR' for the ECB.",
    )

    model_config = ConfigDict(from_attributes=True)


@dataclass(frozen=True)
class RiskFreeRate:
    """The risk-free rate a valuation uses, and where it comes from."""

    value: Decimal
    source: str  # fred_live, fred_cached, fred_stale, treasury_live, ecb_live, ..., env_fallback
    observation_date: Optional[date] = None


class MacroRatesResponse(BaseModel):
    """Answer of GET /macro/rates."""

    currency: Optional[str] = Field(
        None, description="Currency asked (USD or EUR); empty when every source is given."
    )
    risk_free_rate: Optional[MacroRate] = Field(
        None,
        description=(
            "10-year risk-free rate of the currency asked: US Treasury (FRED DGS10, or the "
            "Treasury par yield curve when FRED gives none) for USD and when no currency is asked, AAA euro area government spot rate (ECB) for EUR."
        ),
    )
    rates: List[MacroRate] = Field(
        default_factory=list,
        description=(
            "Every series of the currency asked (all currencies when none is asked): "
            "DGS10 (or treasury:nominal:10Y) for USD; the AAA 10-year spot rate, the deposit facility rate and the "
            "CISS stress index for EUR."
        ),
    )


EUROSTAT_SOURCE = "Eurostat, https://ec.europa.eu/eurostat"

IndicatorUnit = Literal["ratio", "index", "balance"]


class IndicatorLoad(BaseModel):
    """What the database holds for one series, and what its last refresh did."""

    status: Literal["fetched", "fresh", "failed"] = Field(
        ...,
        description=(
            "fetched: read from Eurostat for this answer; fresh: read less than "
            "EUROSTAT_REFRESH_HOURS hours ago; failed: Eurostat could not be read "
            "(what was stored is kept)"
        ),
    )
    fetched_at: Optional[datetime] = Field(None, description="Last reading from Eurostat.")
    first_period: Optional[date] = None
    last_period: Optional[date] = None
    reason: Optional[str] = Field(None, description="Why the refresh failed.")


class IndicatorValue(BaseModel):
    """The latest value of one indicator for one country or area."""

    indicator: str
    label: str
    frequency: Literal["monthly", "quarterly"]
    unit: IndicatorUnit = Field(
        ...,
        description=(
            "ratio: 0.038 for 3.8 %; index: as published (100 = long-term average); "
            "balance: as published (positive answers minus negative answers)"
        ),
    )
    period: Optional[date] = Field(
        None, description="First day of the period: 2026-04-01 for the second quarter of 2026."
    )
    value: Optional[float] = None
    status: Optional[str] = Field(
        None, description="Flag of Eurostat: 'e' for an estimate (the flash inflation)."
    )
    previous_period: Optional[date] = None
    previous_value: Optional[float] = None
    change: Optional[float] = Field(None, description="value - previous_value")
    load: IndicatorLoad


class IndicatorsResponse(BaseModel):
    """Answer of GET /macro/indicators: the latest value of each indicator."""

    geo: str = Field(..., description="Country or area, as Eurostat names it ('EA21', 'FR').")
    source: str = EUROSTAT_SOURCE
    indicators: List[IndicatorValue]


class IndicatorObservation(BaseModel):
    period: date
    value: float
    status: Optional[str] = None


class IndicatorGeoSeries(BaseModel):
    geo: str
    load: IndicatorLoad
    observations: List[IndicatorObservation]


class IndicatorSeriesResponse(BaseModel):
    """Answer of GET /macro/indicators/{indicator}: one series per country, oldest first."""

    indicator: str
    label: str
    frequency: Literal["monthly", "quarterly"]
    unit: IndicatorUnit
    source: str = EUROSTAT_SOURCE
    series: List[IndicatorGeoSeries]


class IndicatorRefreshItem(IndicatorLoad):
    indicator: str
    geo: str
    received: int = Field(0, description="Observations read from Eurostat.")


class IndicatorRefreshResponse(BaseModel):
    source: str = EUROSTAT_SOURCE
    loads: List[IndicatorRefreshItem]


class SolvencyRatios(BaseModel):
    """Solvency ratios and cost of debt of the company."""

    debt_to_equity_ratio: Optional[Decimal] = Field(None, description="Total Debt / Total Equity")
    debt_to_assets_ratio: Optional[Decimal] = Field(None, description="Total Debt / Total Assets")
    net_debt_to_ebitda: Optional[Decimal] = Field(None, description="(Total Debt - Cash) / EBITDA")
    interest_coverage_ratio: Optional[Decimal] = Field(None, description="EBIT / Interest Expense")
    actual_cost_of_debt: Optional[Decimal] = Field(
        None,
        description="Actual cost of debt (Interest Expense / Total Debt), or a sector estimate.",
    )
    cost_of_debt_source: Optional[str] = Field(
        None, description="Source of the cost of debt: 'calculated' or 'sector_estimate'."
    )


# ── US Treasury yield curves ─────────────────────────────────────────────────

# A field named ``date`` would hide the type ``date`` from pydantic: the type is named ``Day``.
Day = date

TREASURY_SOURCE = "US Treasury, Daily Treasury Par Yield Curve Rates (https://home.treasury.gov)"


class TreasuryLoad(BaseModel):
    """What the database holds for a curve, and what this request read from the Treasury."""

    status: Literal["fetched", "fresh", "failed"] = Field(
        ...,
        description=(
            "fetched: a year read from the Treasury for this answer; fresh: every year "
            "needed was stored (a past year read after its end, or the current year read "
            "less than TREASURY_REFRESH_HOURS hours ago); failed: the Treasury could not "
            "be read (what was stored is given)"
        ),
    )
    fetched_at: Optional[datetime] = Field(None, description="Latest reading of the years asked.")
    first_day: Optional[Day] = Field(None, description="First day stored for the curve.")
    last_day: Optional[Day] = Field(None, description="Last day stored for the curve.")
    years_read: List[int] = Field(default_factory=list)
    years_failed: List[int] = Field(default_factory=list)
    reason: Optional[str] = Field(None, description="Why a year could not be read.")


class TreasuryPoint(BaseModel):
    tenor: str = Field(..., description="Maturity: 1M, 6W (six weeks), 2M, ... 10Y, 20Y, 30Y.")
    months: float = Field(..., description="Length of the maturity in months (6W is 1.5).")
    value: float = Field(..., description="The rate as a ratio: 0.0509 for 5.09 %.")


class TreasuryChange(TreasuryPoint):
    change: Optional[float] = Field(
        None, description="Rate of the curve asked minus this one, in ratio (0.0025 = 25 bp)."
    )


class TreasuryComparison(BaseModel):
    requested: str = Field(..., description="As asked: a date, or 1W, 1M, 3M, 1Y... before.")
    date: Optional[Day] = Field(None, description="Business day found, on or before.")
    points: List[TreasuryChange]


class TreasuryCurveResponse(BaseModel):
    """Answer of GET /macro/treasury/curve: the curve of one day, shortest maturity first."""

    curve: Literal["nominal", "real"]
    label: str
    source: str = TREASURY_SOURCE
    requested_date: Optional[Day] = Field(None, description="The day asked (latest when empty).")
    date: Day = Field(
        ..., description="Business day of the curve: the day asked or the one before."
    )
    points: List[TreasuryPoint]
    comparisons: List[TreasuryComparison] = Field(default_factory=list)
    breakeven: List[TreasuryPoint] = Field(
        default_factory=list,
        description=(
            "Breakeven inflation of the day: nominal minus real yield, for the maturities "
            "of both curves (5Y to 30Y); empty when one curve has no rate that day."
        ),
    )
    load: TreasuryLoad


class TreasuryObservation(BaseModel):
    date: Day
    value: float


class TreasurySeries(BaseModel):
    tenor: str = Field(..., description="A maturity (10Y), or a spread of two (10Y-2Y).")
    observations: List[TreasuryObservation]


class TreasurySeriesResponse(BaseModel):
    """Answer of GET /macro/treasury/series: maturities and spreads over time, oldest first."""

    curve: Literal["nominal", "real"]
    label: str
    source: str = TREASURY_SOURCE
    start: Day
    end: Day
    series: List[TreasurySeries]
    load: TreasuryLoad


class TreasuryRefreshItem(TreasuryLoad):
    curve: Literal["nominal", "real"]


class TreasuryRefreshResponse(BaseModel):
    source: str = TREASURY_SOURCE
    loads: List[TreasuryRefreshItem]
