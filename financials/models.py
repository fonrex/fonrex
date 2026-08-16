from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class StandardFinancials(BaseModel):
    """
    Schéma standardisé pour les données financières.
    """

    revenue: Optional[float] = Field(None, description="Chiffre d'affaires")
    ebitda: Optional[float] = Field(None, description="EBITDA")
    net_income: Optional[float] = Field(None, description="Résultat Net")
    eps: Optional[float] = Field(None, description="Bénéfice par action (EPS)")
    payout_ratio: Optional[float] = Field(None, description="Ratio de distribution")
    dividend_yield: Optional[float] = Field(None, description="Rendement (%)")
    debt_to_equity: Optional[float] = Field(None, description="Ratio Dette/Capitaux Propres")
    isin: Optional[str] = Field(None, description="Code ISIN")

    model_config = ConfigDict(extra="allow")


class StockSummary(BaseModel):
    """
    Résumé d'une action pour l'affichage liste.
    """

    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None
    price: Optional[float] = None
    change_percent: Optional[float] = None


class FinancialMetrics(StandardFinancials):
    """
    Schéma enrichi pour les données financières (Boursorama, etc.)
    """

    pe_ratio: Optional[float] = Field(None, description="Price Earning Ratio")
    profit_margin: Optional[float] = Field(None, description="Marge nette (%)")
    operating_margin: Optional[float] = Field(None, description="Marge opérationnelle (%)")
    esg_score: Optional[str] = Field(None, description="Score ESG")
    risk_level: Optional[str] = Field(None, description="Niveau de risque (ETF)")
    morningstar_rating: Optional[str] = Field(None, description="Notation Morningstar")
    eligibility: Optional[list[str]] = Field(None, description="Éligibilité (PEA, SRD, etc.)")
    # Gurufocus Scores
    piotroski_score: Optional[int] = Field(None, description="Score Piotroski")
    beneish_m_score: Optional[float] = Field(None, description="Score Beneish M")
    roic: Optional[float] = Field(None, description="ROIC (%)")
    gf_score: Optional[float] = Field(None, description="Score Gurufocus")

    # Metadata
    provider_url: Optional[str] = Field(None, description="URL de la source")
    ticker: Optional[str] = Field(None, description="Symbole boursier (Format Provider)")
