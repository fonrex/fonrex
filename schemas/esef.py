"""Schemas of the routes of the ESEF filings of European listed companies (``/esef``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

SOURCE = (
    "ESEF annual reports, filings.xbrl.org (XBRL International, https://filings.xbrl.org); "
    "LEI from GLEIF (https://www.gleif.org)"
)

LoadStatus = Literal["fetched", "fresh", "failed"]


class EsefLoadInfo(BaseModel):
    """What the database holds for the company, and what the last reading did."""

    status: LoadStatus = Field(
        ...,
        description=(
            "fetched: read from filings.xbrl.org for this answer; fresh: read less than "
            "ESEF_REFRESH_HOURS hours ago; failed: the index, or a filing of it, could not "
            "be read (what was stored is kept)"
        ),
    )
    fetched_at: Optional[datetime] = None
    filings: int = Field(0, description="Filings of the company in the index.")
    last_report_end: Optional[date] = Field(
        None, description="End of the latest period of the filings read."
    )
    facts: int = Field(0, description="Values kept for the company.")
    read: List[str] = Field(
        default_factory=list, description="Filings read for this answer (fxo_id)."
    )
    refused: Dict[str, str] = Field(
        default_factory=dict, description="Filings refused for this answer, and why."
    )
    reason: Optional[str] = None


class EsefFigure(BaseModel):
    """A figure as filed, and the filing it comes from."""

    value: float
    concept: str = Field(..., description="IFRS concept: 'Revenue', 'ProfitLoss', ...")
    taxonomy: str = Field(..., description="'ifrs-full'")
    unit: str = Field(..., description="'EUR', 'USD', 'EUR/shares', 'shares'")
    fxo_id: str = Field(..., description="The filing of filings.xbrl.org it was read from.")
    report_end: date = Field(..., description="End of the latest period of that filing.")
    viewer_url: Optional[str] = Field(
        None, description="The filing in the viewer of filings.xbrl.org, the figure tagged."
    )


class EsefFiscalYear(BaseModel):
    period_end: date
    currency: str
    warnings: List[str] = Field(
        default_factory=list,
        description="Figures of the filing left out as not plausible (an error of scale), and why.",
    )
    figures: Dict[str, EsefFigure] = Field(
        ...,
        description=(
            "By column of financial_statements. Payments (capex, dividends_paid, "
            "stock_repurchases) are positive, as filed. A figure not filed is absent."
        ),
    )


class EsefStatementsResponse(BaseModel):
    """Answer of GET /esef/{ticker}/statements: the fiscal years filed, most recent first."""

    ticker: str
    lei: str
    entity_name: Optional[str] = None
    source: str = SOURCE
    load: EsefLoadInfo
    years: List[EsefFiscalYear]


class EsefFigureCheck(BaseModel):
    column: str
    statement: Literal["income", "balance", "cashflow"]
    status: Literal["match", "mismatch", "missing_yahoo", "missing_esef"]
    esef: Optional[float] = None
    yahoo: Optional[float] = None
    difference: Optional[float] = Field(
        None,
        description="(yahoo - esef) / |esef|, as a ratio; on amounts for capex, dividends, interest",
    )
    concept: Optional[str] = None
    viewer_url: Optional[str] = None


class EsefYearCheck(BaseModel):
    period_end: date = Field(..., description="End of the fiscal year, as filed.")
    yahoo_period_end: Optional[date] = Field(
        None, description="End of the Yahoo year matched with it (two weeks at most apart)."
    )
    currency: str
    yahoo_currency: Optional[str] = None
    note: Optional[str] = None
    match: int = 0
    mismatch: int = 0
    missing_yahoo: int = 0
    missing_esef: int = 0
    warnings: List[str] = Field(
        default_factory=list,
        description="Figures of the filing left out as not plausible, and why.",
    )
    figures: List[EsefFigureCheck]


class EsefCheckResponse(BaseModel):
    """Answer of GET /esef/{ticker}/check: the Yahoo statements against the ESEF filings."""

    ticker: str
    lei: str
    entity_name: Optional[str] = None
    source: str = SOURCE
    tolerance: float = Field(..., description="SEC_CHECK_TOLERANCE, as a ratio (0.01 = 1 %).")
    not_compared: Dict[str, str] = Field(
        ..., description="Figures the two sources define differently, and why."
    )
    load: EsefLoadInfo
    years: List[EsefYearCheck]


class EsefFilingInfo(BaseModel):
    """A filing of the index, and what its last reading found."""

    fxo_id: str
    listed_period_end: Optional[date] = Field(
        None, description="The period the index gives the filing: it can be wrong."
    )
    report_end: Optional[date] = Field(
        None, description="End of the latest period of its facts, once read: the true period."
    )
    country: Optional[str] = None
    read_status: Optional[Literal["read", "refused", "missing"]] = Field(
        None,
        description=(
            "read; refused (read_reason says why); missing (no xBRL-JSON); null when not "
            "read: another filing of the same period was read, or it was not given yet"
        ),
    )
    read_reason: Optional[str] = None
    read_at: Optional[datetime] = None
    facts: int = 0
    breakdowns: int = Field(0, description="Facts with a dimension, left out.")
    conflicts: int = Field(0, description="Facts given twice with two values, dropped.")
    error_count: Optional[int] = None
    warning_count: Optional[int] = None
    inconsistency_count: Optional[int] = None
    processed: Optional[datetime] = None
    viewer_url: Optional[str] = None
    report_url: Optional[str] = None
    json_url: Optional[str] = None
    package_url: Optional[str] = None


class EsefFilingsResponse(BaseModel):
    """Answer of GET /esef/{ticker}/filings: the filings of the company, latest period first."""

    ticker: str
    lei: str
    entity_name: Optional[str] = None
    source: str = SOURCE
    load: EsefLoadInfo
    filings: List[EsefFilingInfo]


class EsefRefreshResponse(BaseModel):
    ticker: str
    lei: str
    entity_name: Optional[str] = None
    load: EsefLoadInfo
