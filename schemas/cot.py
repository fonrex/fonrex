"""Answers of the ``/cot`` routes: the Commitments of Traders reports of the CFTC."""

from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# A field named ``date`` would hide the type inside the class.
Day = date

COT_SOURCE = "CFTC, Commitments of Traders (https://www.cftc.gov)"

Report = Literal["legacy", "tff", "disaggregated"]


class CotLoad(BaseModel):
    """What the database holds for a report, and what this request read from the CFTC."""

    report: Report
    status: Literal["fetched", "fresh", "failed"] = Field(
        ...,
        description=(
            "fetched: a year read from the CFTC for this answer; fresh: every year needed "
            "was stored (the years up to 2016 once read, a later past year read from 15 "
            "January of the next year on, or the current year read less than "
            "COT_REFRESH_HOURS hours ago); failed: the CFTC could not be read (what was "
            "stored is given)"
        ),
    )
    fetched_at: Optional[datetime] = Field(None, description="Latest reading of the years asked.")
    first_day: Optional[Day] = Field(None, description="First report date stored for the report.")
    last_day: Optional[Day] = Field(None, description="Last report date stored for the report.")
    years_read: List[int] = Field(default_factory=list)
    years_failed: List[int] = Field(default_factory=list)
    reason: Optional[str] = Field(None, description="Why the CFTC could not be read.")


class CotCategory(BaseModel):
    code: str = Field(..., description="Type of trader: noncommercial, leveraged_funds...")
    label: str


class CotMarket(BaseModel):
    report: Report
    market: str = Field(..., description="CFTC contract market code (13874A, 20974+, 001602).")
    name: str = Field(..., description="Name of the market in its latest report stored.")
    contract_units: Optional[str] = None
    first_date: Day
    last_date: Day
    weeks: int = Field(..., description="Report dates stored for the market.")
    key: bool = Field(..., description="One of the markets shown when none is asked.")


class CotMarketsResponse(BaseModel):
    markets: List[CotMarket]
    loads: List[CotLoad]
    source: str = COT_SOURCE


class CotPosition(BaseModel):
    category: str
    long: int = Field(..., description="Contracts held long.")
    short: int = Field(..., description="Contracts held short.")
    spreading: Optional[int] = Field(
        None, description="Contracts held long and short at once; absent for some types."
    )
    net: int = Field(..., description="long minus short.")


class CotWeek(BaseModel):
    date: Day = Field(..., description="Report date: the Tuesday of the positions.")
    published_on: Day = Field(
        ..., description="Estimated day of publication: the Friday after the report date."
    )
    open_interest: int
    positions: List[CotPosition]


class CotPositionsResponse(BaseModel):
    report: Report
    market: str
    name: str = Field(..., description="Name of the market on the last date given.")
    categories: List[CotCategory]
    start: Day
    end: Day
    weeks: List[CotWeek]
    load: CotLoad
    source: str = COT_SOURCE


class CotSignal(BaseModel):
    date: Day = Field(..., description="Report date: the Tuesday of the positions.")
    published_on: Day = Field(
        ...,
        description=(
            "Estimated day of publication (the Friday after the report date): use it, not "
            "the report date, to know when the figures could be known"
        ),
    )
    open_interest: int
    long: int
    short: int
    spreading: Optional[int] = None
    net: int = Field(..., description="long minus short, in contracts.")
    net_share: Optional[float] = Field(None, description="net as a ratio of the open interest.")
    change: Optional[int] = Field(None, description="Change of net since the previous report.")
    cot_index: Optional[float] = Field(
        None,
        description=(
            "net between its lowest (0) and its highest (100) over the window; empty until "
            "the market has reports over the whole window"
        ),
    )


class CotSignalsResponse(BaseModel):
    report: Report
    market: str
    name: str
    category: CotCategory
    window_weeks: int
    start: Day
    end: Day
    signals: List[CotSignal]
    load: CotLoad
    source: str = COT_SOURCE


class CotExtreme(BaseModel):
    market: str
    name: str
    signal: CotSignal


class CotExtremesResponse(BaseModel):
    report: Report
    category: CotCategory
    window_weeks: int
    markets: List[CotExtreme] = Field(
        ..., description="Latest report of each market, the farthest from 50 first."
    )
    missing: List[str] = Field(
        default_factory=list, description="Markets asked without any report stored."
    )
    load: CotLoad
    source: str = COT_SOURCE


class CotRefreshResponse(BaseModel):
    loads: List[CotLoad]
