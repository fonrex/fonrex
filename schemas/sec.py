"""Schemas of the routes of the SEC filings (``/sec``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

SOURCE = "SEC EDGAR, XBRL company facts (https://www.sec.gov/edgar)"

LoadStatus = Literal["fetched", "fresh", "failed"]


class SecLoadInfo(BaseModel):
    """What the database holds for the company, and what the last reading did."""

    status: LoadStatus = Field(
        ...,
        description=(
            "fetched: read from the SEC for this answer; fresh: read less than "
            "SEC_XBRL_REFRESH_HOURS hours ago; failed: the SEC could not be read "
            "(what was stored is kept)"
        ),
    )
    fetched_at: Optional[datetime] = None
    last_filed: Optional[date] = Field(None, description="Latest filing among the facts kept.")
    facts: int = Field(0, description="Facts kept for the company.")
    reason: Optional[str] = None


class SecFigure(BaseModel):
    """A figure as filed, and the filing it comes from."""

    value: float
    concept: str = Field(..., description="XBRL concept: 'Revenues', 'Revenue', ...")
    taxonomy: str = Field(..., description="'us-gaap' or 'ifrs-full'")
    unit: str = Field(..., description="'USD', 'EUR', 'USD/shares', 'shares'")
    form: str = Field(..., description="'10-K', '20-F', ...")
    filed: date
    accession: str
    filing_url: str


class SecFiscalYear(BaseModel):
    period_end: date
    currency: str
    figures: Dict[str, SecFigure] = Field(
        ...,
        description=(
            "By column of financial_statements. Payments (capex, dividends_paid, "
            "stock_repurchases) are positive, as filed. A figure not filed is absent."
        ),
    )


class SecStatementsResponse(BaseModel):
    """Answer of GET /sec/{ticker}/statements: the fiscal years filed, most recent first."""

    ticker: str
    cik: str
    entity_name: Optional[str] = None
    source: str = SOURCE
    load: SecLoadInfo
    years: List[SecFiscalYear]


class SecFigureCheck(BaseModel):
    column: str
    statement: Literal["income", "balance", "cashflow"]
    status: Literal["match", "mismatch", "missing_yahoo", "missing_sec"]
    sec: Optional[float] = None
    yahoo: Optional[float] = None
    difference: Optional[float] = Field(
        None,
        description="(yahoo - sec) / |sec|, as a ratio; on amounts for capex, dividends, interest",
    )
    concept: Optional[str] = None
    filing_url: Optional[str] = None


class SecYearCheck(BaseModel):
    period_end: date = Field(..., description="End of the fiscal year, as filed with the SEC.")
    yahoo_period_end: Optional[date] = Field(
        None, description="End of the Yahoo year matched with it (two weeks at most apart)."
    )
    currency: str
    yahoo_currency: Optional[str] = None
    note: Optional[str] = None
    match: int = 0
    mismatch: int = 0
    missing_yahoo: int = 0
    missing_sec: int = 0
    figures: List[SecFigureCheck]


class SecCheckResponse(BaseModel):
    """Answer of GET /sec/{ticker}/check: the Yahoo statements against the SEC filings."""

    ticker: str
    cik: str
    entity_name: Optional[str] = None
    source: str = SOURCE
    tolerance: float = Field(..., description="SEC_CHECK_TOLERANCE, as a ratio (0.01 = 1 %).")
    not_compared: Dict[str, str] = Field(
        ..., description="Figures the two sources define differently, and why."
    )
    load: SecLoadInfo
    years: List[SecYearCheck]


class SecRefreshResponse(BaseModel):
    ticker: str
    cik: str
    entity_name: Optional[str] = None
    load: SecLoadInfo
