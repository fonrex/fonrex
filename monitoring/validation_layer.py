"""
ValidationLayer — Valide chaque valeur retournée par un provider en temps réel.

Deux types de validation :
1. Range check   : la valeur est-elle dans un range plausible ?
2. Consensus check : la valeur dévie-t-elle trop du consensus des autres providers ?

Intégré dans FinancialProviderRunner APRÈS la collecte des résultats.
Une valeur invalide est remplacée par None (et le fallback prend le relais).

Usage :
    validator = ValidationLayer(monitoring_repository)
    validated = await validator.validate_results(
        ticker="AIR.PA",
        results={"ZoneBourse": financials_obj, "Yahoo": financials_obj2}
    )
"""

import logging
import os
import statistics
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from monitoring.ports import (
    MonitoringRepositoryError,
    ValidationLogEntry,
    ValidationLogRepository,
)

logger = logging.getLogger(__name__)

# ── Ranges plausibles par champ financier ─────────────────────────────────────
# Format : (min, max) — valeurs en dehors = out_of_range → rejeté
FIELD_RANGES: Dict[str, Tuple[float, float]] = {
    # Valorisation
    "pe_ratio": (0.5, 1000.0),
    "pe_forward": (0.5, 500.0),
    "pb_ratio": (0.0, 100.0),
    "ps_ratio": (0.0, 200.0),
    "peg_ratio": (-10.0, 50.0),
    "ev_ebitda": (0.0, 500.0),
    # Prix
    "price": (0.001, 1_000_000.0),
    "target_price": (0.001, 1_000_000.0),
    # Rendements (en ratio, pas en %)
    "dividend_yield": (0.0, 0.50),
    "dividend_rate": (0.0, 1000.0),
    # Rentabilité (en ratio)
    "roe": (-5.0, 10.0),
    "roa": (-2.0, 2.0),
    "net_margin": (-5.0, 1.0),
    "operating_margin": (-5.0, 1.0),
    "gross_margin": (-1.0, 1.0),
    # Ratios de croissance (en ratio)
    "quarterly_revenue_growth_yoy": (-0.99, 10.0),
    "quarterly_earnings_growth_yoy": (-0.99, 20.0),
    # EPS
    "eps_trailing": (-1000.0, 10000.0),
    "eps_forward": (-1000.0, 10000.0),
    "eps": (-1000.0, 10000.0),
    # Beta
    "beta": (-3.0, 5.0),
    # Short interest (en ratio)
    "short_percent_float": (0.0, 1.0),
    # Payout ratio
    "payout_ratio": (0.0, 10.0),
    # 52W
    "week_52_high": (0.001, 1_000_000.0),
    "week_52_low": (0.001, 1_000_000.0),
}

# Seuil configurable via env
CONSENSUS_DEVIATION_THRESHOLD = float(os.environ.get("VALIDATION_OUTLIER_THRESHOLD", "0.50"))
MIN_PROVIDERS_FOR_CONSENSUS = int(os.environ.get("VALIDATION_MIN_PROVIDERS", "2"))

# Champs validables présents sur StandardFinancials / FinancialMetrics
_VALIDATABLE_FIELDS = list(FIELD_RANGES.keys())


class ValidationLayer:
    """
    Valide les résultats retournés par les providers financiers.

    Ne connaît que le contrat de persistance du monitoring. La technologie de
    stockage est choisie au point de composition de l'application.
    """

    def __init__(self, repository: ValidationLogRepository | None = None) -> None:
        """
        Args:
            repository: port de journalisation. Si ``None``, la persistance
                        des contrôles est désactivée.
        """
        self._repository = repository

    # ── Point d'entrée principal ─────────────────────────────────────────────

    async def validate_results(
        self,
        ticker: str,
        results: Dict[str, Any],  # {provider_name: payload (dict or pydantic obj)}
    ) -> Dict[str, Any]:
        """
        Valide tous les résultats des providers pour un ticker.

        Pour chaque champ de chaque provider :
        1. Range check → valeur dans FIELD_RANGES ?
        2. Consensus check → valeur trop différente des autres ?

        Si une valeur est invalide → la remplacer par None dans l'objet
        Et logger dans provider_health_log.

        Retourne les résultats nettoyés (objets modifiés in-place).
        """
        try:
            # Collecter toutes les valeurs par champ : {field: {provider: Decimal}}
            field_values: Dict[str, Dict[str, Decimal]] = {}
            for provider_name, obj in results.items():
                if obj is None or (isinstance(obj, dict) and obj.get("error")):
                    continue
                for field in _VALIDATABLE_FIELDS:
                    val = self._extract_field(obj, field)
                    if val is not None:
                        field_values.setdefault(field, {})[provider_name] = val

            # Calculer le consensus par champ
            consensus_by_field: Dict[str, Optional[Decimal]] = {}
            for field, prov_vals in field_values.items():
                consensus_by_field[field] = self._compute_consensus(field, prov_vals)

            # Valider chaque (provider, field) et collecter les logs
            pending_logs: List[ValidationLogEntry] = []

            for provider_name, obj in results.items():
                if obj is None or (isinstance(obj, dict) and obj.get("error")):
                    continue

                for field in _VALIDATABLE_FIELDS:
                    val = self._extract_field(obj, field)

                    # Range check
                    range_status, range_reason = self._check_range(field, val)

                    if range_status == "null":
                        # Value is None — log but don't reject (already None)
                        pending_logs.append(
                            self._make_log_entry(
                                provider_name,
                                ticker,
                                field,
                                val,
                                status="null",
                                check_type="realtime",
                            )
                        )
                        continue

                    if range_status == "out_of_range":
                        # Reject value
                        self._set_field_none(obj, field)
                        rng = FIELD_RANGES.get(field)
                        pending_logs.append(
                            self._make_log_entry(
                                provider_name,
                                ticker,
                                field,
                                val,
                                status="out_of_range",
                                check_type="realtime",
                                min_val=Decimal(str(rng[0])) if rng else None,
                                max_val=Decimal(str(rng[1])) if rng else None,
                            )
                        )
                        logger.warning(
                            "[Validation] %s.%s=%s OUT_OF_RANGE for %s — %s",
                            provider_name,
                            field,
                            val,
                            ticker,
                            range_reason,
                        )
                        continue

                    # Consensus check (only if value passed range check)
                    consensus = consensus_by_field.get(field)
                    if consensus is not None and val is not None:
                        cons_status, deviation, cons_reason = self._check_consensus(
                            field, val, consensus, provider_name
                        )
                        if cons_status == "outlier":
                            self._set_field_none(obj, field)
                            pending_logs.append(
                                self._make_log_entry(
                                    provider_name,
                                    ticker,
                                    field,
                                    val,
                                    status="outlier",
                                    check_type="consensus",
                                    consensus=consensus,
                                    deviation=deviation,
                                )
                            )
                            logger.warning(
                                "[Validation] %s.%s=%s OUTLIER for %s — %s",
                                provider_name,
                                field,
                                val,
                                ticker,
                                cons_reason,
                            )
                            continue

                    # Value is OK
                    pending_logs.append(
                        self._make_log_entry(
                            provider_name,
                            ticker,
                            field,
                            val,
                            status="ok",
                            check_type="realtime",
                            consensus=consensus,
                        )
                    )

            # Batch insert logs
            await self._batch_log(pending_logs)

        except Exception as exc:
            # Never fail the main runner — silently log
            logger.error("[ValidationLayer] Unexpected error: %s", exc, exc_info=True)

        return results

    # ── Range check ──────────────────────────────────────────────────────────

    def _check_range(
        self,
        field: str,
        value: Optional[Decimal],
    ) -> Tuple[str, Optional[str]]:
        """
        Vérifie si une valeur est dans le range plausible pour ce champ.

        Retourne (status, reason).
        """
        if value is None:
            return ("null", "valeur None")

        rng = FIELD_RANGES.get(field)
        if rng is None:
            return ("ok", None)

        min_val, max_val = rng
        fval = float(value)
        if fval < min_val:
            return ("out_of_range", f"{field} = {value} < min {min_val}")
        if fval > max_val:
            return ("out_of_range", f"{field} = {value} > max {max_val}")
        return ("ok", None)

    # ── Consensus check ──────────────────────────────────────────────────────

    def _check_consensus(
        self,
        field: str,
        value: Decimal,
        consensus: Decimal,
        provider: str,
    ) -> Tuple[str, Optional[Decimal], Optional[str]]:
        """
        Vérifie si une valeur dévie trop du consensus des autres providers.
        """
        try:
            fval = float(value)
            fcons = float(consensus)

            # Avoid division by zero / near-zero
            if abs(fcons) < 1e-9:
                return ("ok", None, None)
            if abs(fval) < 1e-9 and abs(fcons) < 1e-9:
                return ("ok", None, None)

            deviation = abs(fval - fcons) / abs(fcons)
            deviation_dec = Decimal(str(round(deviation, 4)))

            if deviation > CONSENSUS_DEVIATION_THRESHOLD:
                return (
                    "outlier",
                    deviation_dec,
                    f"{provider}.{field}={value} dévie {deviation:.1%} du consensus {consensus}",
                )
            return ("ok", deviation_dec, None)
        except (ArithmeticError, TypeError, ValueError):
            return ("ok", None, None)

    # ── Calcul du consensus ───────────────────────────────────────────────────

    def _compute_consensus(
        self,
        field: str,
        provider_values: Dict[str, Decimal],
    ) -> Optional[Decimal]:
        """
        Calcule la valeur de consensus (médiane) pour un champ donné.

        Filtre les valeurs hors range avant de calculer.
        Si < MIN_PROVIDERS_FOR_CONSENSUS valeurs → retourner None.
        """
        # Filter out values that fail range check
        valid_values: List[float] = []
        for prov, val in provider_values.items():
            status, _ = self._check_range(field, val)
            if status == "ok":
                valid_values.append(float(val))

        if len(valid_values) < MIN_PROVIDERS_FOR_CONSENSUS:
            return None

        median_val = statistics.median(valid_values)
        return Decimal(str(round(median_val, 6)))

    # ── Logging helpers ──────────────────────────────────────────────────────

    def _make_log_entry(
        self,
        provider: str,
        ticker: str,
        field: str,
        value: Optional[Decimal],
        status: str,
        check_type: str,
        min_val: Optional[Decimal] = None,
        max_val: Optional[Decimal] = None,
        consensus: Optional[Decimal] = None,
        deviation: Optional[Decimal] = None,
    ) -> ValidationLogEntry:
        return {
            "provider_name": provider,
            "ticker": ticker,
            "field": field,
            "value_received": value,
            "value_expected_min": min_val,
            "value_expected_max": max_val,
            "consensus_value": consensus,
            "deviation_pct": deviation,
            "status": status,
            "check_type": check_type,
        }

    async def _batch_log(self, entries: List[ValidationLogEntry]) -> None:
        """
        Insère toutes les entrées en un seul batch dans provider_health_log.
        Ne lève jamais d'exception.
        """
        if not entries or not self._repository:
            return
        try:
            await self._repository.save_validation_logs(entries)
        except MonitoringRepositoryError as exc:
            logger.warning("[ValidationLayer] Failed to log checks: %s", exc)

    # ── Extraction des champs ─────────────────────────────────────────────────

    def _extract_field(self, obj: Any, field: str) -> Optional[Decimal]:
        """
        Extrait un champ d'un objet (Pydantic model ou dict) de façon défensive.
        Retourne None si l'attribut n'existe pas ou est None.
        Convertit en Decimal.
        """
        try:
            if isinstance(obj, dict):
                val = obj.get(field)
            else:
                val = getattr(obj, field, None)
            if val is None:
                return None
            return Decimal(str(float(val)))
        except (ValueError, TypeError, InvalidOperation):
            return None

    def _set_field_none(self, obj: Any, field: str) -> None:
        """Remet un champ à None sur un objet (Pydantic ou dict)."""
        try:
            if isinstance(obj, dict):
                obj[field] = None
            else:
                setattr(obj, field, None)
        except (AttributeError, TypeError):
            pass
