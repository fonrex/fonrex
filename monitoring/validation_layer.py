"""
ValidationLayer — checks every value returned by a provider as it arrives.

Two checks:
1. Range check     : is the value within a plausible range?
2. Consensus check : does the value deviate too much from the other providers?

Run by FinancialProviderRunner AFTER the results are collected.
An invalid value is replaced by None (and the fallback takes over).

Usage:
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
from monitoring.units import to_ratio

logger = logging.getLogger(__name__)

# ── Plausible range of each financial field ──────────────────────────────────
# Format: (min, max) — a value outside is out_of_range → rejected
# Ranges are ratios (0.0345 = 3.45 %). A provider that publishes a field as a
# percentage is converted before the check (see monitoring/units.py).
FIELD_RANGES: Dict[str, Tuple[float, float]] = {
    # Valuation
    "pe_ratio": (0.5, 1000.0),
    "pe_forward": (0.5, 500.0),
    "pb_ratio": (0.0, 100.0),
    "ps_ratio": (0.0, 200.0),
    "peg_ratio": (-10.0, 50.0),
    "ev_ebitda": (0.0, 500.0),
    # Price
    "price": (0.001, 1_000_000.0),
    "target_price": (0.001, 1_000_000.0),
    # Yields (ratios, not %)
    "dividend_yield": (0.0, 0.50),
    "dividend_rate": (0.0, 1000.0),
    # Profitability (ratios)
    "roe": (-5.0, 10.0),
    "roa": (-2.0, 2.0),
    "net_margin": (-5.0, 1.0),
    "operating_margin": (-5.0, 1.0),
    "gross_margin": (-1.0, 1.0),
    # Growth (ratios)
    "quarterly_revenue_growth_yoy": (-0.99, 10.0),
    "quarterly_earnings_growth_yoy": (-0.99, 20.0),
    # EPS
    "eps_trailing": (-1000.0, 10000.0),
    "eps_forward": (-1000.0, 10000.0),
    "eps": (-1000.0, 10000.0),
    # Beta
    "beta": (-3.0, 5.0),
    # Short interest (ratio)
    "short_percent_float": (0.0, 1.0),
    # Payout ratio
    "payout_ratio": (0.0, 10.0),
    # 52W
    "week_52_high": (0.001, 1_000_000.0),
    "week_52_low": (0.001, 1_000_000.0),
    # Scores (GuruFocus)
    "piotroski_score": (0.0, 9.0),
    "beneish_m_score": (-20.0, 20.0),
    "altman_z_score": (-100.0, 1000.0),
    "gf_score": (0.0, 100.0),
    "roic": (-5.0, 10.0),
}

# Threshold set by environment variable
CONSENSUS_DEVIATION_THRESHOLD = float(os.environ.get("VALIDATION_OUTLIER_THRESHOLD", "0.50"))
MIN_PROVIDERS_FOR_CONSENSUS = int(os.environ.get("VALIDATION_MIN_PROVIDERS", "2"))

# Checked fields of StandardFinancials / FinancialMetrics
_VALIDATABLE_FIELDS = list(FIELD_RANGES.keys())


class ValidationLayer:
    """
    Checks the results returned by the financial providers.

    Knows only the storage contract of the monitoring; the storage technology
    is chosen where the application is assembled.
    """

    def __init__(self, repository: ValidationLogRepository | None = None) -> None:
        """
        Args:
            repository: logging port. With ``None``, the checks are not
                        stored.
        """
        self._repository = repository

    # ── Main entry point ─────────────────────────────────────────────────────

    async def validate_results(
        self,
        ticker: str,
        results: Dict[str, Any],  # {provider_name: payload (dict or pydantic obj)}
    ) -> Dict[str, Any]:
        """
        Check every provider result of a ticker.

        For each field of each provider:
        1. Range check → value within FIELD_RANGES?
        2. Consensus check → value too far from the others?

        Values are first turned into ratios (see monitoring/units.py): the
        checks and the logs use the normalised value, the payload of the
        provider keeps its own unit.

        An invalid value is replaced by None in the object and logged in
        provider_health_log.

        Returns the cleaned results (objects changed in place).
        """
        try:
            # Collect every value per field: {field: {provider: Decimal}}
            field_values: Dict[str, Dict[str, Decimal]] = {}
            for provider_name, obj in results.items():
                if obj is None or (isinstance(obj, dict) and obj.get("error")):
                    continue
                for field in _VALIDATABLE_FIELDS:
                    val = self._extract_normalized(provider_name, obj, field)
                    if val is not None:
                        field_values.setdefault(field, {})[provider_name] = val

            # Consensus of each field
            consensus_by_field: Dict[str, Optional[Decimal]] = {}
            for field, prov_vals in field_values.items():
                consensus_by_field[field] = self._compute_consensus(field, prov_vals)

            # Check each (provider, field) and collect the logs
            pending_logs: List[ValidationLogEntry] = []

            for provider_name, obj in results.items():
                if obj is None or (isinstance(obj, dict) and obj.get("error")):
                    continue

                for field in _VALIDATABLE_FIELDS:
                    val = self._extract_normalized(provider_name, obj, field)

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
        Whether a value is within the plausible range of its field.

        Returns (status, reason).
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
        Whether a value deviates too much from the consensus of the other providers.
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
                    f"{provider}.{field}={value} deviates {deviation:.1%} from the consensus {consensus}",
                )
            return ("ok", deviation_dec, None)
        except (ArithmeticError, TypeError, ValueError):
            return ("ok", None, None)

    # ── Consensus ─────────────────────────────────────────────────────────────

    def _compute_consensus(
        self,
        field: str,
        provider_values: Dict[str, Decimal],
    ) -> Optional[Decimal]:
        """
        Consensus value (median) of a field.

        Values out of range are left out first.
        Fewer than MIN_PROVIDERS_FOR_CONSENSUS values → None.
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
        Insert every entry into provider_health_log in one batch.
        Never raises.
        """
        if not entries or not self._repository:
            return
        try:
            await self._repository.save_validation_logs(entries)
        except MonitoringRepositoryError as exc:
            logger.warning("[ValidationLayer] Failed to log checks: %s", exc)

    # ── Field reading ─────────────────────────────────────────────────────────

    def _extract_field(self, obj: Any, field: str) -> Optional[Decimal]:
        """
        Read a field of an object (Pydantic model or dict) defensively.
        None when the attribute is missing or None.
        Converted to Decimal.
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

    def _extract_normalized(self, provider: str, obj: Any, field: str) -> Optional[Decimal]:
        """Read a field and convert it to the unit of the ranges (ratio)."""
        return to_ratio(provider, field, self._extract_field(obj, field))

    def _set_field_none(self, obj: Any, field: str) -> None:
        """Set a field of an object (Pydantic or dict) to None."""
        try:
            if isinstance(obj, dict):
                obj[field] = None
            else:
                setattr(obj, field, None)
        except (AttributeError, TypeError):
            pass
