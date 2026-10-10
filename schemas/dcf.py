#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Schémas Pydantic pour le module DCF Valuation.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from schemas.macro import SolvencyRatios
from settings import env_decimal, env_int

# Defaults of a DCF request, tunable through the environment (see .env.example).
DEFAULT_PROJECTION_YEARS = env_int("DCF_DEFAULT_PROJECTION_YEARS", 5, minimum=3, maximum=10)
# A ratio: 0.025 for 2.5 %.
DEFAULT_TERMINAL_GROWTH_RATE = env_decimal(
    "DCF_TERMINAL_GROWTH_RATE", "0.025", minimum="-1", maximum="1"
)
# Risk-free rate used when no source answers and none was ever stored. A ratio:
# 0.04 for 4 %; it may be negative (euro government rates were, in 2019-2021).
DEFAULT_RISK_FREE_RATE = env_decimal("DCF_RISK_FREE_RATE", "0.04", minimum="-0.1", maximum="0.5")


class WACCInput(BaseModel):
    """Paramètres d'entrée personnalisés pour le calcul du WACC."""

    risk_free_rate: Optional[Decimal] = Field(
        None, description="Taux sans risque (ex: 0.04 pour 4%)"
    )
    equity_risk_premium: Optional[Decimal] = Field(
        None, description="Prime de risque actions (ex: 0.055 pour 5.5%)"
    )
    beta_override: Optional[Decimal] = Field(None, description="Valeur de Beta forcée")
    cost_of_debt_override: Optional[Decimal] = Field(None, description="Coût de la dette forcé")
    tax_rate_override: Optional[Decimal] = Field(None, description="Taux d'imposition forcé")


class DCFRequest(BaseModel):
    """Requête de calcul DCF personnalisée."""

    models: List[Literal["fcf", "eps", "ddm"]] = Field(default_factory=lambda: ["fcf"])
    projection_years: int = Field(
        DEFAULT_PROJECTION_YEARS,
        ge=3,
        le=10,
        description="Nombre d'années de projection (3 à 10)",
    )
    terminal_growth_rate: Decimal = Field(
        DEFAULT_TERMINAL_GROWTH_RATE,
        description="Taux de croissance terminal (ex: 0.025 pour 2.5%)",
    )
    wacc_params: Optional[WACCInput] = Field(None, description="Paramètres de WACC personnalisés")
    fcf_growth_override: Optional[Decimal] = Field(
        None, description="Force le taux de croissance FCF initial"
    )
    eps_growth_override: Optional[Decimal] = Field(
        None, description="Force le taux de croissance EPS initial"
    )
    dividend_growth_override: Optional[Decimal] = Field(
        None, description="Force le taux de croissance Dividende initial"
    )
    model_weights: Optional[Dict[Literal["fcf", "eps", "ddm"], Decimal]] = Field(
        None,
        description="Pondérations personnalisées pour le calcul du consensus. Ex: {'fcf': 0.5, 'eps': 0.3, 'ddm': 0.2}",
    )


class WACCResult(BaseModel):
    """Détail du WACC calculé."""

    wacc: Decimal
    cost_of_equity: Decimal
    cost_of_debt: Decimal
    tax_rate: Decimal
    weight_equity: Decimal
    weight_debt: Decimal
    beta_used: Decimal
    cost_of_debt_source: Optional[str] = Field(
        None, description="Source du Kd (client_override, calculated, sector_estimate)"
    )
    risk_free_rate: Optional[Decimal] = Field(None, description="Rf used, as a ratio")
    risk_free_rate_source: Optional[str] = Field(
        None,
        description=(
            "Source of Rf: client_override; ecb_live, ecb_cached, ecb_stale (euro AAA "
            "10-year rate, for cash flows in EUR); fred_live, fred_cached, fred_stale (US "
            "10-year Treasury rate, for cash flows in USD) — live: read now, cached: read "
            "less than one cache lifetime ago, stale: older stored value, the source could "
            "not be read; env_fallback (DCF_RISK_FREE_RATE: no source for the currency, or "
            "nothing read)"
        ),
    )
    risk_free_rate_date: Optional[date] = Field(
        None, description="Observation date of Rf, when it comes from a source"
    )
    risk_free_rate_currency: Optional[str] = Field(
        None,
        description=(
            "Currency of the rate read from a source (EUR from the ECB, USD from FRED); "
            "null for DCF_RISK_FREE_RATE or a rate set in the request"
        ),
    )


class DCFModelResult(BaseModel):
    """Résultat détaillé pour un modèle spécifique (FCF, EPS, ou DDM)."""

    model_name: str
    intrinsic_value_per_share: Decimal
    upside_pct: Optional[Decimal] = Field(
        None,
        description="Upside over the share price, in percent; null when the price is in "
        "another currency than the statements",
    )
    projected_values: List[Decimal]
    terminal_value: Decimal
    present_values: List[Decimal]
    pv_terminal: Decimal
    warnings: List[str] = Field(default_factory=list)


class DCFResult(BaseModel):
    """Réponse finale de l'API pour une valorisation DCF."""

    ticker: str
    currency: str = Field(..., description="Currency of the statements and of the values")
    current_price: Optional[Decimal] = None
    price_currency: Optional[str] = Field(
        None, description="Currency of current_price (converted from a minor unit such as GBX)"
    )
    warnings: List[str] = Field(default_factory=list)
    shares_outstanding: Optional[int] = None
    wacc: WACCResult
    models: Dict[str, DCFModelResult]
    solvency: Optional[SolvencyRatios] = Field(
        None, description="Ratios de solvabilité et données d'endettement sous-jacentes"
    )
    consensus_value: Optional[Decimal] = None
    consensus_upside_pct: Optional[Decimal] = None
    analyst_target: Optional[Decimal] = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SensitivityCell(BaseModel):
    """Cellule de la matrice de sensibilité."""

    wacc: Decimal
    terminal_growth: Decimal
    intrinsic_value: Decimal
    upside_pct: Optional[Decimal] = None


class SensitivityResult(BaseModel):
    """Matrice de sensibilité complète."""

    ticker: str
    model: str
    wacc_range: List[Decimal]
    growth_range: List[Decimal]
    matrix: List[List[SensitivityCell]]
