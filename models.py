#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FonRex - Database models for financial data.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import declarative_base, relationship

logger = logging.getLogger(__name__)

# Standard SQLAlchemy Base
Base = declarative_base()


def utc_now():
    """Timezone-aware UTC timestamp for SQLAlchemy defaults."""
    return datetime.now(UTC)


def naive_utc_now():
    """UTC timestamp for the one legacy timezone-naive column."""
    return datetime.now(UTC).replace(tzinfo=None)


class Asset(Base):
    """
    Asset table (assets).
    Stores the financial instrument. For instruments with ISIN, a row
    represents the instrument and quotes live in AssetListing.
    """

    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(50), nullable=False)
    name = Column(String(255))
    exchange = Column(String(50))
    currency = Column(String(10))
    sector = Column(String(100))
    industry = Column(String(100))
    is_active = Column(Boolean, default=True)

    # Extended Metadata
    isin = Column(String(20))
    quote_type = Column(String(20))
    fund_family = Column(String(100))
    long_business_summary = Column(Text)
    display_name = Column(String(255))
    official_symbol = Column(String(50))
    ir_website = Column(String(255))
    logo_path = Column(String(255))
    country = Column(String(100))
    country_code = Column(String(10))
    profile = Column(JSONB().with_variant(JSON(), "sqlite"))

    gic_sector = Column(String(100))
    gic_group = Column(String(100))
    gic_industry = Column(String(100))
    gic_sub_industry = Column(String(100))

    created_at = Column(DateTime(timezone=True), default=utc_now)

    # Relations
    prices_eod = relationship("PriceEOD", back_populates="asset", lazy="dynamic")
    fundamentals = relationship("Fundamental", backref="asset", lazy="dynamic")
    mappings = relationship("AssetMapping", back_populates="asset", cascade="all, delete-orphan")
    listings = relationship("AssetListing", back_populates="asset", cascade="all, delete-orphan")
    # Deep fundamentals (Phase 1 + Phase 2)
    highlights = relationship(
        "FundamentalsHighlights",
        back_populates="asset",
        uselist=False,
        cascade="all, delete-orphan",
    )
    financial_statements = relationship(
        "FinancialStatement", back_populates="asset", lazy="dynamic", cascade="all, delete-orphan"
    )
    earnings_history = relationship(
        "EarningsHistory", back_populates="asset", lazy="dynamic", cascade="all, delete-orphan"
    )
    analyst_ratings = relationship(
        "AnalystRatings", back_populates="asset", uselist=False, cascade="all, delete-orphan"
    )
    # ETF-specific (Phase 2)
    etf_details = relationship(
        "ETFDetails", back_populates="asset", uselist=False, cascade="all, delete-orphan"
    )
    etf_holdings = relationship(
        "ETFHolding", back_populates="etf_asset", cascade="all, delete-orphan"
    )

    # Premium Fields (Phase 4)
    earnings_trend = relationship(
        "EarningsTrend", back_populates="asset", cascade="all, delete-orphan"
    )
    esg_scores = relationship(
        "ESGScores", back_populates="asset", uselist=False, cascade="all, delete-orphan"
    )
    outstanding_shares_history = relationship(
        "OutstandingSharesHistory", back_populates="asset", cascade="all, delete-orphan"
    )

    # Realtime streaming (Phase 6)
    prices_intraday = relationship(
        "PriceIntraday", back_populates="asset", lazy="dynamic", cascade="all, delete-orphan"
    )
    realtime_subscriptions = relationship(
        "RealtimeSubscription", back_populates="asset", uselist=False, cascade="all, delete-orphan"
    )

    # News aggregation (Phase 10)
    news_articles = relationship(
        "NewsArticle", back_populates="asset", lazy="dynamic", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_assets_ticker_exchange", "ticker", "exchange"),
        Index("idx_assets_isin", "isin"),
        Index(
            "uq_assets_isin_not_null",
            "isin",
            unique=True,
            postgresql_where=text("isin IS NOT NULL"),
        ),
    )


class AssetListing(Base):
    """
    Listing of a financial instrument on a given market/currency.
    An ETF can share the same ISIN across multiple tickers/currencies.
    """

    __tablename__ = "asset_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    ticker = Column(String(50), nullable=False)
    exchange = Column(String(50), nullable=False, default="")
    currency = Column(String(10), nullable=False, default="")
    source = Column(String(50), default="manual", nullable=False)
    is_primary = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    asset = relationship("Asset", back_populates="listings")
    mappings = relationship("AssetMapping", back_populates="listing", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint(
            "asset_id", "ticker", "exchange", "currency", name="uq_asset_listing_identity"
        ),
        Index("idx_asset_listings_ticker_identity", "ticker", "exchange", "currency"),
        Index("idx_asset_listings_asset", "asset_id"),
    )


class AssetMapping(Base):
    """
    Asset Mapping table (asset_mappings).
    Centralizes third-party identifiers for data providers.
    """

    __tablename__ = "asset_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    asset_listing_id = Column(
        Integer, ForeignKey("asset_listings.id", ondelete="CASCADE"), nullable=True
    )
    provider_name = Column(String(50), nullable=False)  # ex: "boursorama", "gurufocus"
    provider_ticker = Column(String(50))  # Specific identifier
    provider_url = Column(String(500))  # Direct URL
    source = Column(String(50), default="manual", nullable=False)
    confidence_score = Column(Float)
    is_active = Column(Boolean, default=True, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    last_verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    # Inverse relation
    asset = relationship("Asset", back_populates="mappings")
    listing = relationship("AssetListing", back_populates="mappings")

    # Uniqueness constraint to avoid duplicates per provider
    __table_args__ = (
        UniqueConstraint("asset_listing_id", "provider_name", name="uq_asset_listing_provider"),
        Index("idx_asset_mappings_asset_provider", "asset_id", "provider_name"),
        Index("idx_asset_mappings_provider_ticker", "provider_name", "provider_ticker"),
        Index("idx_asset_mappings_active", "provider_name", "is_active"),
    )


class PriceEOD(Base):
    """
    EOD Prices table (prices_eod) - TimescaleDB Hypertable.
    Stores price history.
    """

    __tablename__ = "prices_eod"

    timestamp = Column("time", DateTime(timezone=True), nullable=False, primary_key=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, primary_key=True)
    asset_listing_id = Column(Integer, ForeignKey("asset_listings.id"), nullable=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    adj_close = Column(Float)
    volume = Column(BigInteger)
    resolution = Column(String(3), default="1D", nullable=False)
    adjusted = Column(Boolean, default=True, nullable=False)
    source = Column(String(20))

    asset = relationship("Asset", back_populates="prices_eod")

    # Implicit composite primary key for SQLAlchemy (TimescaleDB handles partitioning)
    __table_args__ = (
        UniqueConstraint("time", "asset_id", name="prices_eod_timestamp_asset_id_key"),
        Index("idx_prices_asset_timestamp", "asset_id", text("time DESC")),
        Index("idx_prices_listing_timestamp", "asset_listing_id", text("time DESC")),
        Index("ix_prices_eod_asset_resolution_time", "asset_id", "resolution", "time"),
    )


class Fundamental(Base):
    """
    Fundamentals table (fundamentals) - TimescaleDB Hypertable.
    Stores fundamental data with JSONB flexibility.
    """

    __tablename__ = "fundamentals"

    timestamp = Column(DateTime(timezone=True), nullable=False, primary_key=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, primary_key=True)
    market_cap = Column(Float)
    pe_ratio = Column(Float)
    dividend_yield = Column(Float)
    extra_metrics = Column(JSONB().with_variant(JSON(), "sqlite"))


class FundamentalsHighlights(Base):
    """
    Daily snapshot of an asset's key metrics.
    One row per asset — daily upsert.
    Replaces the contents of extra_metrics for market data.
    """

    __tablename__ = "fundamentals_highlights"
    __table_args__ = (
        Index("ix_fund_highlights_asset_id", "asset_id"),
        Index("ix_fund_highlights_fetched_at", "fetched_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    source = Column(String(50), default="yfinance")

    # Valuation
    market_cap = Column(Numeric(20, 2))
    enterprise_value = Column(Numeric(20, 2))
    pe_ratio = Column(Numeric(10, 4))  # trailing
    pe_forward = Column(Numeric(10, 4))
    pb_ratio = Column(Numeric(10, 4))
    ps_ratio = Column(Numeric(10, 4))
    peg_ratio = Column(Numeric(10, 4))
    ev_ebitda = Column(Numeric(10, 4))
    ev_revenue = Column(Numeric(10, 4))

    # Profitability
    roe = Column(Numeric(10, 6))
    roa = Column(Numeric(10, 6))
    roic = Column(Numeric(10, 6))
    net_margin = Column(Numeric(10, 6))
    operating_margin = Column(Numeric(10, 6))
    gross_margin = Column(Numeric(10, 6))

    # Per share
    eps_trailing = Column(Numeric(10, 4))
    eps_forward = Column(Numeric(10, 4))
    book_value_per_share = Column(Numeric(10, 4))
    revenue_per_share = Column(Numeric(10, 4))

    # Dividend
    dividend_yield = Column(Numeric(10, 6))
    dividend_rate = Column(Numeric(10, 4))
    dividend_ex_date = Column(Date)
    dividend_pay_date = Column(Date)
    payout_ratio = Column(Numeric(10, 6))

    # Technical
    beta = Column(Numeric(8, 4))
    week_52_high = Column(Numeric(14, 4))
    week_52_low = Column(Numeric(14, 4))
    ma_50 = Column(Numeric(14, 4))
    ma_200 = Column(Numeric(14, 4))

    # Ownership
    shares_outstanding = Column(BigInteger)
    float_shares = Column(BigInteger)
    pct_insiders = Column(Numeric(8, 6))
    pct_institutions = Column(Numeric(8, 6))

    # Short selling
    shares_short = Column(BigInteger)
    shares_short_prior = Column(BigInteger)
    short_ratio = Column(Numeric(8, 4))
    short_percent_float = Column(Numeric(8, 6))
    short_percent_outstanding = Column(Numeric(8, 6))
    shares_short_date = Column(Date)

    # TTM
    gross_profit_ttm = Column(Numeric(20, 2))
    diluted_eps_ttm = Column(Numeric(10, 4))
    return_on_assets_ttm = Column(Numeric(10, 6))
    return_on_equity_ttm = Column(Numeric(10, 6))
    quarterly_revenue_growth_yoy = Column(Numeric(10, 6))
    quarterly_earnings_growth_yoy = Column(Numeric(10, 6))
    revenue_ttm = Column(Numeric(20, 2))
    ebitda_ttm = Column(Numeric(20, 2))

    asset = relationship("Asset", back_populates="highlights")


class FinancialStatement(Base):
    """
    Row of a financial statement (income / balance / cashflow).
    Each row = one asset × one type × one periodicity × one period end.
    """

    __tablename__ = "financial_statements"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "statement_type", "period_type", "period_end", name="uq_financial_statement"
        ),
        Index("ix_financial_stmt_asset_period", "asset_id", "period_end"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    statement_type = Column(String(20), nullable=False)  # "income" | "balance" | "cashflow"
    period_type = Column(String(10), nullable=False)  # "annual" | "quarterly"
    period_end = Column(Date, nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    currency = Column(String(3), default="USD")

    # Income statement
    revenue = Column(Numeric(20, 2))
    gross_profit = Column(Numeric(20, 2))
    ebitda = Column(Numeric(20, 2))
    ebit = Column(Numeric(20, 2))
    operating_income = Column(Numeric(20, 2))
    net_income = Column(Numeric(20, 2))
    eps_basic = Column(Numeric(10, 4))
    eps_diluted = Column(Numeric(10, 4))
    shares_basic = Column(BigInteger)
    shares_diluted = Column(BigInteger)
    rd_expense = Column(Numeric(20, 2))
    sga_expense = Column(Numeric(20, 2))
    interest_expense = Column(Numeric(20, 2))
    tax_provision = Column(Numeric(20, 2))

    # Balance sheet
    total_assets = Column(Numeric(20, 2))
    total_liabilities = Column(Numeric(20, 2))
    total_equity = Column(Numeric(20, 2))
    total_debt = Column(Numeric(20, 2))
    net_debt = Column(Numeric(20, 2))
    cash_and_equivalents = Column(Numeric(20, 2))
    short_term_investments = Column(Numeric(20, 2))
    accounts_receivable = Column(Numeric(20, 2))
    inventory = Column(Numeric(20, 2))
    goodwill = Column(Numeric(20, 2))
    intangible_assets = Column(Numeric(20, 2))
    long_term_debt = Column(Numeric(20, 2))
    retained_earnings = Column(Numeric(20, 2))

    # Cash Flow
    operating_cashflow = Column(Numeric(20, 2))
    investing_cashflow = Column(Numeric(20, 2))
    financing_cashflow = Column(Numeric(20, 2))
    free_cashflow = Column(Numeric(20, 2))
    capex = Column(Numeric(20, 2))
    dividends_paid = Column(Numeric(20, 2))
    stock_repurchases = Column(Numeric(20, 2))
    depreciation_amortization = Column(Numeric(20, 2))

    asset = relationship("Asset", back_populates="financial_statements")


class EarningsHistory(Base):
    """Actual vs estimated EPS history, per quarter."""

    __tablename__ = "earnings_history"
    __table_args__ = (
        UniqueConstraint("asset_id", "period", name="uq_earnings_period"),
        Index("ix_earnings_asset_id", "asset_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    period = Column(String(10), nullable=False)  # "2024Q4"
    period_end = Column(Date)
    eps_actual = Column(Numeric(10, 4))
    eps_estimate = Column(Numeric(10, 4))
    surprise = Column(Numeric(10, 4))
    surprise_pct = Column(Numeric(8, 4))
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    asset = relationship("Asset", back_populates="earnings_history")


class AnalystRatings(Base):
    """Analyst consensus and price targets (one row per asset)."""

    __tablename__ = "analyst_ratings"
    __table_args__ = (Index("ix_analyst_ratings_asset_id", "asset_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(
        Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    consensus = Column(String(20))
    target_mean = Column(Numeric(14, 4))
    target_low = Column(Numeric(14, 4))
    target_high = Column(Numeric(14, 4))
    target_median = Column(Numeric(14, 4))
    nb_analysts = Column(Integer)
    strong_buy = Column(Integer, default=0)
    buy = Column(Integer, default=0)
    hold = Column(Integer, default=0)
    sell = Column(Integer, default=0)
    strong_sell = Column(Integer, default=0)

    asset = relationship("Asset", back_populates="analyst_ratings")


class ETFDetails(Base):
    """ETF-specific metadata (one row per ETF)."""

    __tablename__ = "etf_details"
    __table_args__ = (Index("ix_etf_details_asset_id", "asset_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(
        Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    inception_date = Column(Date)
    net_expense_ratio = Column(Numeric(8, 6))
    annual_holdings_turnover = Column(Numeric(8, 4))
    total_net_assets = Column(Numeric(20, 2))
    average_market_cap = Column(Numeric(20, 2))
    is_ucits = Column(Boolean, default=False)
    domicile = Column(String(5))
    replication_method = Column(String(20))
    distribution_policy = Column(String(20))
    ytd_return = Column(Numeric(8, 6))
    return_1y = Column(Numeric(8, 6))
    return_3y = Column(Numeric(8, 6))
    return_5y = Column(Numeric(8, 6))
    volatility_1y = Column(Numeric(8, 6))
    sharpe_ratio = Column(Numeric(8, 4))
    tracking_error = Column(Numeric(8, 6))
    alloc_cash = Column(Numeric(6, 4))
    alloc_stock_us = Column(Numeric(6, 4))
    alloc_stock_non_us = Column(Numeric(6, 4))
    alloc_bond = Column(Numeric(6, 4))
    alloc_other = Column(Numeric(6, 4))

    asset = relationship("Asset", back_populates="etf_details")


class ETFHolding(Base):
    """Individual holdings of an ETF (top positions)."""

    __tablename__ = "etf_holdings"
    __table_args__ = (
        UniqueConstraint("etf_asset_id", "holding_ticker", name="uq_etf_holding"),
        Index("ix_etf_holdings_etf_id", "etf_asset_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    etf_asset_id = Column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    holding_ticker = Column(String(20))
    holding_isin = Column(String(12))
    holding_name = Column(String(200))
    weight = Column(Numeric(8, 6))
    sector = Column(String(100))
    country = Column(String(5))
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    etf_asset = relationship("Asset", back_populates="etf_holdings")


class EarningsTrend(Base):
    """Future analyst estimates per period."""

    __tablename__ = "earnings_trend"
    __table_args__ = (
        UniqueConstraint("asset_id", "period", name="uq_earnings_trend_period"),
        Index("ix_earnings_trend_asset_id", "asset_id"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    period = Column(String(10), nullable=False)
    # "0q" = current quarter, "+1q" = next quarter,
    # "0y" = current year, "+1y" = next year
    period_end = Column(Date)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    # Revenue
    revenue_avg = Column(Numeric(20, 2))
    revenue_low = Column(Numeric(20, 2))
    revenue_high = Column(Numeric(20, 2))
    revenue_nb_analysts = Column(Integer)
    revenue_growth = Column(Numeric(10, 6))
    # EPS
    eps_avg = Column(Numeric(10, 4))
    eps_low = Column(Numeric(10, 4))
    eps_high = Column(Numeric(10, 4))
    eps_nb_analysts = Column(Integer)
    eps_growth = Column(Numeric(10, 6))

    asset = relationship("Asset", back_populates="earnings_trend")


class ESGScores(Base):
    """ESG scores and controversial activities."""

    __tablename__ = "esg_scores"
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, unique=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    rating_date = Column(Date)
    # Global scores
    total_esg = Column(Numeric(6, 2))
    environment_score = Column(Numeric(6, 2))
    social_score = Column(Numeric(6, 2))
    governance_score = Column(Numeric(6, 2))
    controversy_level = Column(Integer)
    controversy_score = Column(Numeric(6, 2))
    # Percentiles
    percentile = Column(Numeric(6, 2))
    peer_group = Column(String(200))
    peer_count = Column(Integer)
    peer_esg_score_perf_min = Column(Numeric(6, 2))
    peer_esg_score_perf_avg = Column(Numeric(6, 2))
    peer_esg_score_perf_max = Column(Numeric(6, 2))
    # Categories
    esg_risk_rating = Column(String(50))
    highest_controversy = Column(Integer)
    adult = Column(Boolean)
    alcoholic = Column(Boolean)
    animal_testing = Column(Boolean)
    catholic = Column(Boolean)
    controversial_weapons = Column(Boolean)
    small_arms = Column(Boolean)
    fur_leather = Column(Boolean)
    gambling = Column(Boolean)
    gmo = Column(Boolean)
    military_contract = Column(Boolean)
    nuclear = Column(Boolean)
    pesticides = Column(Boolean)
    palm_oil = Column(Boolean)
    coal = Column(Boolean)
    tobacco = Column(Boolean)

    asset = relationship("Asset", back_populates="esg_scores")


class OutstandingSharesHistory(Base):
    """History of the evolution of the number of outstanding shares."""

    __tablename__ = "outstanding_shares_history"
    __table_args__ = (
        UniqueConstraint("asset_id", "date", name="uq_outstanding_shares_date"),
        Index("ix_outstanding_shares_asset_id", "asset_id"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    date = Column(Date, nullable=False)
    shares = Column(BigInteger, nullable=False)
    period_type = Column(String(10))  # "annual" | "quarterly"
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())

    asset = relationship("Asset", back_populates="outstanding_shares_history")


class UsageLog(Base):
    """
    API usage log for analytics, provider costs, and future SaaS billing.
    """

    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(String(100), nullable=True, index=True)
    endpoint = Column(String(255), nullable=False, index=True)
    method = Column(String(10), nullable=False)
    provider_used = Column(String(100), nullable=True)
    cache_hit = Column(Boolean, default=False, nullable=False)
    status_code = Column(Integer, nullable=False)
    latency_ms = Column(Integer, nullable=False)
    cost_bucket = Column(String(50), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=naive_utc_now, nullable=False)

    __table_args__ = (
        Index("idx_usage_logs_created", "created_at"),
        Index("idx_usage_logs_endpoint_created", "endpoint", "created_at"),
        Index("idx_usage_logs_api_key_created", "api_key_id", "created_at"),
        Index("idx_usage_logs_provider_created", "provider_used", "created_at"),
    )


class IngestLog(Base):
    __tablename__ = "ingest_log"
    __table_args__ = (
        Index("ix_ingest_log_asset_id", "asset_id"),
        Index("ix_ingest_log_created_at", "created_at"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    ticker = Column(String(20), nullable=False)
    resolution = Column(String(3), nullable=False)
    source = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)  # "success"|"failed"|"partial"
    records_added = Column(Integer)
    from_date = Column(Date)
    to_date = Column(Date)
    error_msg = Column(Text)
    duration_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    asset = relationship("Asset")


class PriceIntraday(Base):
    """
    Intraday 1min bars received via TradingView WebSocket.
    TimescaleDB Hypertable with automatic 30-day retention.
    Migration: 007_realtime_tables
    """

    __tablename__ = "prices_intraday"
    __table_args__ = (Index("ix_prices_intraday_asset_ts", "asset_id", "timestamp"),)

    timestamp = Column(DateTime(timezone=True), primary_key=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), primary_key=True)
    open = Column(Numeric(14, 4))
    high = Column(Numeric(14, 4))
    low = Column(Numeric(14, 4))
    close = Column(Numeric(14, 4))
    volume = Column(BigInteger)
    resolution = Column(String(5), default="1min")
    source = Column(String(20), default="tradingview")

    asset = relationship("Asset", back_populates="prices_intraday")


class RealtimeSubscription(Base):
    """
    Registry of tickers currently streamed by the TradingView worker.
    One row per asset (unique constraint on asset_id).
    Migration: 007_realtime_tables
    """

    __tablename__ = "realtime_subscriptions"
    __table_args__ = (
        Index("ix_realtime_subs_active", "is_active"),
        Index("ix_realtime_subs_ticker", "ticker"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False, unique=True)
    ticker = Column(String(20), nullable=False)
    tv_exchange = Column(String(20), nullable=False)  # ex: "EURONEXT"
    tv_symbol = Column(String(20), nullable=False)  # ex: "AIR"
    is_active = Column(Boolean, default=True)
    subscribed_at = Column(DateTime(timezone=True), server_default=func.now())
    last_tick_at = Column(DateTime(timezone=True))
    tick_count = Column(BigInteger, default=0)

    asset = relationship("Asset", back_populates="realtime_subscriptions")


class NewsArticle(Base):
    """Financial news article aggregated from providers."""

    __tablename__ = "news_articles"
    __table_args__ = (
        Index("ix_news_asset_published", "asset_id", "published_at"),
        Index("ix_news_published_at", "published_at"),
        Index("ix_news_provider", "provider"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=True)
    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    url = Column(String(1000), nullable=False, unique=True)
    image_url = Column(String(1000), nullable=True)
    source = Column(String(100), nullable=True)
    provider = Column(String(50), nullable=False)
    author = Column(String(200), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    sentiment = Column(String(10), nullable=True)
    sentiment_score = Column(Numeric(4, 3), nullable=True)
    # PostgreSQL arrays are useful for overlap queries in production.  The JSON
    # variants keep the metadata portable for the SQLite unit-test database.
    related_tickers = Column(ARRAY(String(20)).with_variant(JSON(), "sqlite"), nullable=True)
    related_isin = Column(ARRAY(String(12)).with_variant(JSON(), "sqlite"), nullable=True)
    language = Column(String(5), nullable=True, default="en")

    asset = relationship("Asset", back_populates="news_articles")


# ── Phase 12 : Provider Health Monitoring ─────────────────────────────────────


class ProviderHealthLog(Base):
    """Log of each health check of a provider value."""

    __tablename__ = "provider_health_log"
    __table_args__ = (
        Index("ix_phl_provider_checked", "provider_name", "checked_at"),
        Index("ix_phl_status", "status"),
    )

    # Composite PK required by TimescaleDB (partitioning column in the PK)
    id = Column(Integer, autoincrement=True, nullable=False)
    checked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    provider_name = Column(String(50), nullable=False)
    ticker = Column(String(20), nullable=True)
    field = Column(String(50), nullable=False)
    value_received = Column(Numeric(20, 6), nullable=True)
    value_expected_min = Column(Numeric(20, 6), nullable=True)
    value_expected_max = Column(Numeric(20, 6), nullable=True)
    consensus_value = Column(Numeric(20, 6), nullable=True)
    deviation_pct = Column(Numeric(8, 4), nullable=True)
    status = Column(String(20), nullable=False)
    check_type = Column(String(20), nullable=False)

    __mapper_args__ = {"primary_key": [id, checked_at]}


class ProviderHealthDaily(Base):
    """Daily health aggregate by provider."""

    __tablename__ = "provider_health_daily"
    __table_args__ = (
        UniqueConstraint("provider_name", "date", name="uq_provider_health_daily"),
        Index("ix_phd_provider_date", "provider_name", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_name = Column(String(50), nullable=False)
    date = Column(Date, nullable=False)
    checks_total = Column(Integer, default=0)
    checks_ok = Column(Integer, default=0)
    checks_outlier = Column(Integer, default=0)
    checks_null = Column(Integer, default=0)
    checks_timeout = Column(Integer, default=0)
    success_rate = Column(Numeric(5, 4), nullable=True)
    avg_latency_ms = Column(Integer, nullable=True)
    canary_passed = Column(Boolean, nullable=True)
    is_healthy = Column(Boolean, default=True)


class ProviderAlert(Base):
    """Active alert on a provider."""

    __tablename__ = "provider_alerts"
    __table_args__ = (
        Index("ix_pa_provider_active", "provider_name", "is_resolved"),
        Index("ix_pa_severity", "severity", "is_resolved"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_name = Column(String(50), nullable=False)
    alert_type = Column(String(50), nullable=False)
    severity = Column(String(10), nullable=False)
    description = Column(Text, nullable=False)
    ticker = Column(String(20), nullable=True)
    field = Column(String(50), nullable=True)
    value_received = Column(Numeric(20, 6), nullable=True)
    value_expected = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    is_resolved = Column(Boolean, default=False)
    resolution_note = Column(Text, nullable=True)


def cleanup_old_data(days_to_keep=730):  # 2 ans par défaut
    """
    DEPRECATED: Use db_service.cleanup_old_data() instead.

    Cleans up old data to prevent the DB from growing too large.

    Args:
        days_to_keep (int): Number of days of data to keep

    Returns:
        int: Total number of records deleted
    """
    from database.service import DatabaseService

    logger.warning(
        "⚠️ La fonction cleanup_old_data() est dépréciée. Utilisez db_service.cleanup_old_data() à la place."
    )

    # Create a temporary instance of the service
    db_service = DatabaseService()

    # Call the internal method to perform cleanup
    return db_service._cleanup_old_data(days_to_keep)
