from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class MacroRate(BaseModel):
    """Une observation d'un taux macro-économique (ex: taux sans risque)."""
    series_id: str = Field(..., description="L'identifiant de la série FRED (ex: DGS10).")
    label: Optional[str] = Field(None, description="Libellé de la série.")
    value: Decimal = Field(..., description="La valeur brute observée.")
    unit: Optional[str] = Field(None, description="Unité, ex: 'percent'.")
    observation_date: date = Field(..., description="Date d'observation par FRED.")
    
    model_config = ConfigDict(from_attributes=True)


class MacroRatesResponse(BaseModel):
    """Réponse de l'endpoint GET /macro/rates."""
    risk_free_rate: Optional[MacroRate] = Field(None, description="Taux sans risque à 10 ans (DGS10).")
    # On pourrait en rajouter d'autres plus tard, ex: inflation, corporate spread...


class SolvencyRatios(BaseModel):
    """Ratios de solvabilité et coût de la dette de l'entreprise."""
    debt_to_equity_ratio: Optional[Decimal] = Field(
        None, description="Total Debt / Total Equity"
    )
    debt_to_assets_ratio: Optional[Decimal] = Field(
        None, description="Total Debt / Total Assets"
    )
    net_debt_to_ebitda: Optional[Decimal] = Field(
        None, description="(Total Debt - Cash) / EBITDA"
    )
    interest_coverage_ratio: Optional[Decimal] = Field(
        None, description="EBIT / Interest Expense"
    )
    actual_cost_of_debt: Optional[Decimal] = Field(
        None, description="Coût de la dette réel calculé (Interest Expense / Total Debt) ou estimé sectoriel."
    )
    cost_of_debt_source: Optional[str] = Field(
        None, description="Source du coût de la dette : 'calculated' ou 'sector_estimate'."
    )
