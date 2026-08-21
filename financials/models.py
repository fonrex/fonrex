from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class NewsArticle(BaseModel):
    """
    Model for news articles.
    """
    title: str = Field(..., description="Article title")
    url: str = Field(..., description="Article URL")
    date: Optional[str] = Field(None, description="Publication date")
    source: Optional[str] = Field(None, description="Article source (e.g., Dow Jones, MarketWatch)")
    image: Optional[str] = Field(None, description="Image URL")


class Competitor(BaseModel):
    """
    Model for a competitor extracted from MarketWatch.
    """
    name: str = Field(..., description="Competitor Name")
    ticker: str = Field(..., description="Competitor Ticker Symbol")
    change_percent: Optional[str] = Field(None, description="Change Percentage")
    market_cap: Optional[str] = Field(None, description="Market Capitalization")


class StandardFinancials(BaseModel):
    """
    Standard schema of the financial data.
    """

    revenue: Optional[float] = Field(None, description="Revenue")
    ebitda: Optional[float] = Field(None, description="EBITDA")
    net_income: Optional[float] = Field(None, description="Net income")
    eps: Optional[float] = Field(None, description="Earnings per share (EPS)")
    payout_ratio: Optional[float] = Field(None, description="Payout ratio")
    dividend_yield: Optional[float] = Field(None, description="Dividend yield (%)")
    debt_to_equity: Optional[float] = Field(None, description="Debt to equity ratio")
    isin: Optional[str] = Field(None, description="ISIN")

    model_config = ConfigDict(extra="allow")


class StockSummary(BaseModel):
    """
    Summary of a share, for list views.
    """

    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None
    price: Optional[float] = None
    change_percent: Optional[float] = None


class FinancialMetrics(StandardFinancials):
    """
    Enriched schema of the financial data (Boursorama, etc.)
    """

    pe_ratio: Optional[float] = Field(None, description="Price Earning Ratio")
    profit_margin: Optional[float] = Field(None, description="Net margin (%)")
    operating_margin: Optional[float] = Field(None, description="Operating margin (%)")
    esg_score: Optional[str] = Field(None, description="ESG Score")
    risk_level: Optional[str] = Field(None, description="Risk level (ETF)")
    morningstar_rating: Optional[str] = Field(None, description="Morningstar rating")
    eligibility: Optional[list[str]] = Field(None, description="Eligibility (PEA, SRD, etc.)")
    # Gurufocus Scores
    piotroski_score: Optional[int] = Field(None, description="Piotroski Score")
    beneish_m_score: Optional[float] = Field(None, description="Beneish M Score")
    altman_z_score: Optional[float] = Field(None, description="Altman Z-Score")
    roic: Optional[float] = Field(None, description="ROIC (%)")
    gf_score: Optional[float] = Field(None, description="Gurufocus Score")
    insider_transactions: Optional[dict] = Field(
        None, description="Insider transactions summary and recent filings"
    )

    # Metadata
    provider_url: Optional[str] = Field(None, description="Source URL")
    ticker: Optional[str] = Field(None, description="Stock ticker (Provider format)")

    news: Optional[list[NewsArticle]] = Field(None, description="Recent news (Dow Jones, Other Sources)")
    competitors: Optional[list[Competitor]] = Field(None, description="List of competitors")
    rating: Optional[str] = Field(None, description="Analyst Rating")
    sentiment: Optional[str] = Field(None, description="Sentiment Score")

