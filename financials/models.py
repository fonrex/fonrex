from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
    esg_score: Optional[str] = Field(None, description="Score ESG")
    risk_level: Optional[str] = Field(None, description="Risk level (ETF)")
    morningstar_rating: Optional[str] = Field(None, description="Morningstar rating")
    eligibility: Optional[list[str]] = Field(None, description="Eligibility (PEA, SRD, etc.)")
    # Gurufocus Scores
    piotroski_score: Optional[int] = Field(None, description="Score Piotroski")
    beneish_m_score: Optional[float] = Field(None, description="Score Beneish M")
    roic: Optional[float] = Field(None, description="ROIC (%)")
    gf_score: Optional[float] = Field(None, description="Score Gurufocus")

    # Metadata
    provider_url: Optional[str] = Field(None, description="URL de la source")
    ticker: Optional[str] = Field(None, description="Symbole boursier (Format Provider)")
