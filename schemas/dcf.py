#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Schémas Pydantic pour le module DCF Valuation.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


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
        5, ge=3, le=10, description="Nombre d'années de projection (3 à 10)"
    )
    terminal_growth_rate: Decimal = Field(
        Decimal("0.025"), description="Taux de croissance terminal (ex: 0.025 pour 2.5%)"
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


class DCFModelResult(BaseModel):
    """Résultat détaillé pour un modèle spécifique (FCF, EPS, ou DDM)."""

    model_name: str
    intrinsic_value_per_share: Decimal
    upside_pct: Decimal
    projected_values: List[Decimal]
    terminal_value: Decimal
    present_values: List[Decimal]
    pv_terminal: Decimal
    warnings: List[str] = Field(default_factory=list)


class DCFResult(BaseModel):
    """Réponse finale de l'API pour une valorisation DCF."""

    ticker: str
    currency: str
    current_price: Optional[Decimal] = None
    shares_outstanding: Optional[int] = None
    wacc: WACCResult
    models: Dict[str, DCFModelResult]
    consensus_value: Optional[Decimal] = None
    consensus_upside_pct: Optional[Decimal] = None
    analyst_target: Optional[Decimal] = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SensitivityCell(BaseModel):
    """Cellule de la matrice de sensibilité."""

    wacc: Decimal
    terminal_growth: Decimal
    intrinsic_value: Decimal
    upside_pct: Decimal


class SensitivityResult(BaseModel):
    """Matrice de sensibilité complète."""

    ticker: str
    model: str
    wacc_range: List[Decimal]
    growth_range: List[Decimal]
    matrix: List[List[SensitivityCell]]
