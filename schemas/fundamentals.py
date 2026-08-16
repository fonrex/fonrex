"""
Pydantic v2 schemas for Fonrex fundamental data.

Usage:
    from schemas.fundamentals import DeepFundamentalsResponse, HighlightsSchema
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ── Enums ─────────────────────────────────────────────────────────────────────


class StatementType(str, Enum):
    income = "income"
    balance = "balance"
    cashflow = "cashflow"


class PeriodType(str, Enum):
    annual = "annual"
    quarterly = "quarterly"


# ── Highlights ────────────────────────────────────────────────────────────────


class HighlightsSchema(BaseModel):
    """Key aggregated metrics of an asset."""

    model_config = ConfigDict(from_attributes=True)

    # Valuation
    market_cap: Optional[Decimal] = None
    enterprise_value: Optional[Decimal] = None
    pe_ratio: Optional[Decimal] = None
    pe_forward: Optional[Decimal] = None
    pb_ratio: Optional[Decimal] = None
    ps_ratio: Optional[Decimal] = None
    peg_ratio: Optional[Decimal] = None
    ev_ebitda: Optional[Decimal] = None
    ev_revenue: Optional[Decimal] = None

    # Profitability
    roe: Optional[Decimal] = None
    roa: Optional[Decimal] = None
    roic: Optional[Decimal] = None
    net_margin: Optional[Decimal] = None
    operating_margin: Optional[Decimal] = None
    gross_margin: Optional[Decimal] = None

    # Per share
    eps_trailing: Optional[Decimal] = None
    eps_forward: Optional[Decimal] = None
    book_value_per_share: Optional[Decimal] = None
    revenue_per_share: Optional[Decimal] = None

    # Dividend
    dividend_yield: Optional[Decimal] = None
    dividend_rate: Optional[Decimal] = None
    dividend_ex_date: Optional[date] = None
    dividend_pay_date: Optional[date] = None
    payout_ratio: Optional[Decimal] = None

    # Technical
    beta: Optional[Decimal] = None
    week_52_high: Optional[Decimal] = None
    week_52_low: Optional[Decimal] = None
    ma_50: Optional[Decimal] = None
    ma_200: Optional[Decimal] = None

    # Ownership
    shares_outstanding: Optional[int] = None
    float_shares: Optional[int] = None
    pct_insiders: Optional[Decimal] = None
    pct_institutions: Optional[Decimal] = None

    # Metadata
    fetched_at: Optional[datetime] = None
    source: Optional[str] = None


# ── Financial Statements ──────────────────────────────────────────────────────


class FinancialStatementSchema(BaseModel):
    """Row of a financial statement (income / balance / cashflow)."""

    model_config = ConfigDict(from_attributes=True)

    period_end: date
    period_type: PeriodType
    statement_type: Optional[StatementType] = None
    currency: Optional[str] = None

    # Income statement
    revenue: Optional[Decimal] = None
    gross_profit: Optional[Decimal] = None
    ebitda: Optional[Decimal] = None
    ebit: Optional[Decimal] = None
    operating_income: Optional[Decimal] = None
    net_income: Optional[Decimal] = None
    eps_basic: Optional[Decimal] = None
    eps_diluted: Optional[Decimal] = None
    shares_basic: Optional[int] = None
    shares_diluted: Optional[int] = None
    rd_expense: Optional[Decimal] = None
    sga_expense: Optional[Decimal] = None
    interest_expense: Optional[Decimal] = None
    tax_provision: Optional[Decimal] = None

    # Balance sheet
    total_assets: Optional[Decimal] = None
    total_liabilities: Optional[Decimal] = None
    total_equity: Optional[Decimal] = None
    total_debt: Optional[Decimal] = None
    net_debt: Optional[Decimal] = None
    cash_and_equivalents: Optional[Decimal] = None
    short_term_investments: Optional[Decimal] = None
    accounts_receivable: Optional[Decimal] = None
    inventory: Optional[Decimal] = None
    goodwill: Optional[Decimal] = None
    intangible_assets: Optional[Decimal] = None
    long_term_debt: Optional[Decimal] = None
    retained_earnings: Optional[Decimal] = None

    # Cash Flow
    operating_cashflow: Optional[Decimal] = None
    investing_cashflow: Optional[Decimal] = None
    financing_cashflow: Optional[Decimal] = None
    free_cashflow: Optional[Decimal] = None
    capex: Optional[Decimal] = None
    dividends_paid: Optional[Decimal] = None
    stock_repurchases: Optional[Decimal] = None
    depreciation_amortization: Optional[Decimal] = None


# ── Earnings History ──────────────────────────────────────────────────────────


class EarningsHistorySchema(BaseModel):
    """Actual vs estimated EPS per quarter."""

    model_config = ConfigDict(from_attributes=True)

    period: str
    period_end: Optional[date] = None
    eps_actual: Optional[Decimal] = None
    eps_estimate: Optional[Decimal] = None
    surprise: Optional[Decimal] = None
    surprise_pct: Optional[Decimal] = None


# ── Analyst Ratings ───────────────────────────────────────────────────────────


class AnalystRatingsSchema(BaseModel):
    """Analyst consensus and price targets."""

    model_config = ConfigDict(from_attributes=True)

    consensus: Optional[str] = None
    target_mean: Optional[Decimal] = None
    target_low: Optional[Decimal] = None
    target_high: Optional[Decimal] = None
    target_median: Optional[Decimal] = None
    nb_analysts: Optional[int] = None
    strong_buy: Optional[int] = None
    buy: Optional[int] = None
    hold: Optional[int] = None
    sell: Optional[int] = None
    strong_sell: Optional[int] = None
    fetched_at: Optional[datetime] = None


# ── ETF Details ───────────────────────────────────────────────────────────────


class ETFDetailsSchema(BaseModel):
    """ETF-specific metadata."""

    model_config = ConfigDict(from_attributes=True)

    inception_date: Optional[date] = None
    net_expense_ratio: Optional[Decimal] = None
    total_net_assets: Optional[Decimal] = None
    average_market_cap: Optional[Decimal] = None
    is_ucits: Optional[bool] = None
    domicile: Optional[str] = None
    replication_method: Optional[str] = None
    distribution_policy: Optional[str] = None
    ytd_return: Optional[Decimal] = None
    return_1y: Optional[Decimal] = None
    return_3y: Optional[Decimal] = None
    return_5y: Optional[Decimal] = None
    volatility_1y: Optional[Decimal] = None
    sharpe_ratio: Optional[Decimal] = None
    tracking_error: Optional[Decimal] = None
    annual_holdings_turnover: Optional[Decimal] = None
    alloc_cash: Optional[Decimal] = None
    alloc_stock_us: Optional[Decimal] = None
    alloc_stock_non_us: Optional[Decimal] = None
    alloc_bond: Optional[Decimal] = None
    alloc_other: Optional[Decimal] = None
    fetched_at: Optional[datetime] = None


class ETFHoldingSchema(BaseModel):
    """Individual ETF holding."""

    model_config = ConfigDict(from_attributes=True)

    holding_ticker: Optional[str] = None
    holding_isin: Optional[str] = None
    holding_name: Optional[str] = None
    weight: Optional[Decimal] = None
    sector: Optional[str] = None
    country: Optional[str] = None


# ── Deep Fundamentals Response ─────────────────────────────────────────────────


class DeepFundamentalsResponse(BaseModel):
    """
    Complete response of the /fundamental/deep endpoint.
    Aggregates all structured fundamental data of an asset.
    """

    asset_profile: dict

    highlights: Optional[HighlightsSchema] = None
    statements: Dict[str, List[FinancialStatementSchema]] = Field(
        default_factory=lambda: {
            "income": [],
            "balance": [],
            "cashflow": [],
        }
    )
    earnings_history: List[EarningsHistorySchema] = []
    analyst_ratings: Optional[AnalystRatingsSchema] = None

    # ETF-specific (None for stocks)
    etf_details: Optional[ETFDetailsSchema] = None
    etf_holdings: List[ETFHoldingSchema] = []

    meta: dict = Field(
        default_factory=lambda: {
            "source": "fonrex",
            "cache_hit": False,
            "fetched_at": None,
        }
    )


# ── EODHD Premium Response ────────────────────────────────────────────────────


class SharesStatsSchema(BaseModel):
    shares_outstanding: Optional[int] = Field(None, alias="SharesOutstanding")
    shares_float: Optional[int] = Field(None, alias="SharesFloat")
    percent_insiders: Optional[Decimal] = Field(None, alias="PercentInsiders")
    percent_institutions: Optional[Decimal] = Field(None, alias="PercentInstitutions")
    shares_short: Optional[int] = Field(None, alias="SharesShort")
    shares_short_prior_month: Optional[int] = Field(None, alias="SharesShortPriorMonth")
    short_ratio: Optional[Decimal] = Field(None, alias="ShortRatio")
    short_percent_float: Optional[Decimal] = Field(None, alias="ShortPercentFloat")
    short_percent_outstanding: Optional[Decimal] = Field(None, alias="ShortPercentOutstanding")
    model_config = ConfigDict(populate_by_name=True)


class TechnicalsSchema(BaseModel):
    beta: Optional[Decimal] = Field(None, alias="Beta")
    week_52_high: Optional[Decimal] = Field(None, alias="52WeekHigh")
    week_52_low: Optional[Decimal] = Field(None, alias="52WeekLow")
    ma_50: Optional[Decimal] = Field(None, alias="50DayMA")
    ma_200: Optional[Decimal] = Field(None, alias="200DayMA")
    model_config = ConfigDict(populate_by_name=True)


class ESGScoresSchema(BaseModel):
    rating_date: Optional[date] = Field(None, alias="RatingDate")
    total_esg: Optional[Decimal] = Field(None, alias="TotalEsg")
    environment_score: Optional[Decimal] = Field(None, alias="EnvironmentScore")
    social_score: Optional[Decimal] = Field(None, alias="SocialScore")
    governance_score: Optional[Decimal] = Field(None, alias="GovernanceScore")
    controversy_level: Optional[int] = Field(None, alias="ControversyLevel")
    activities_involved: Dict[str, Optional[bool]] = Field(
        default_factory=dict, alias="ActivitiesInvolved"
    )
    model_config = ConfigDict(populate_by_name=True)


class EODHDFundamentalResponse(BaseModel):
    """
    Standardized Premium response aligned with the EODHD API.
    """

    general: dict = Field(..., alias="General")
    highlights: dict = Field(..., alias="Highlights")
    valuation: dict = Field(..., alias="Valuation")
    shares_stats: Optional[SharesStatsSchema] = Field(None, alias="SharesStats")
    technicals: Optional[TechnicalsSchema] = Field(None, alias="Technicals")
    splits_dividends: dict = Field(..., alias="SplitsDividends")
    analyst_ratings: dict = Field(..., alias="AnalystRatings")
    holders: dict = Field(..., alias="Holders")
    insider_transactions: dict = Field(..., alias="InsiderTransactions")
    esg_scores: Optional[ESGScoresSchema] = Field(None, alias="ESGScores")
    earnings: dict = Field(..., alias="Earnings")
    financials: dict = Field(..., alias="Financials")
    etf_data: Optional[dict] = Field(None, alias="ETF_Data")

    model_config = ConfigDict(populate_by_name=True)
