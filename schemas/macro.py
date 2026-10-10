from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# A rate is always given as a ratio: 0.0412 for 4.12 %. Rows stored before this
# rule say "percent" while holding the same ratio.
RATE_UNIT = "ratio"

Freshness = Literal["live", "cached", "stale"]


class MacroRate(BaseModel):
    """Une observation d'un taux macro-économique (ex: taux sans risque)."""
    series_id: str = Field(
        ...,
        description="Series name: FRED id (DGS10) or ECB flow.key (YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y).",
    )
    label: Optional[str] = Field(None, description="Libellé de la série.")
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
    
    model_config = ConfigDict(from_attributes=True)


@dataclass(frozen=True)
class RiskFreeRate:
    """The risk-free rate a valuation uses, and where it comes from."""

    value: Decimal
    source: str  # fred_live, fred_cached, fred_stale, ecb_live, ..., or env_fallback
    observation_date: Optional[date] = None


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
