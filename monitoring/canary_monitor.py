"""
CanaryMonitor — Vérifie quotidiennement des actifs "canary" contre chaque provider.

Un actif canary est un actif très liquide avec des valeurs connues et stables.
Ex: Apple P/E est toujours entre 20 et 40. Si un provider retourne 0.4 → cassé.

Lancé quotidiennement via APScheduler (06:00 UTC par défaut).

Usage :
    monitor = CanaryMonitor(monitoring_repository, redis_client)
    await monitor.run_all()
    await monitor.run_provider("ZoneBourse")
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from monitoring.canary_catalog import (
    CANARY_ASSETS,
    MONITORED_PROVIDERS,
    get_provider_class,
    is_compatible,
)
from monitoring.models import CanaryCheckResult, HealthStatus
from monitoring.ports import (
    AlertCandidate,
    CanaryMonitoringRepository,
    DailyHealthStats,
    MonitoringHealthCache,
    MonitoringRepositoryError,
)
from monitoring.price_ranges import DynamicPriceRangeResolver

logger = logging.getLogger(__name__)

# Seuils d'alerte
ALERT_THRESHOLDS = {
    "canary_failures_critical": int(os.environ.get("ALERT_CANARY_CRITICAL", "3")),
    "canary_failures_warning": 1,
    "success_rate_critical": float(os.environ.get("ALERT_SUCCESS_RATE_CRITICAL", "0.70")),
    "success_rate_warning": float(os.environ.get("ALERT_SUCCESS_RATE_WARNING", "0.85")),
}

# Semaphore limit
_SEMAPHORE_LIMIT = int(os.environ.get("CANARY_PROVIDER_SEMAPHORE", "3"))

# Dynamic ranges must be fresher than the daily canary run. Failed lookups use a
# shorter TTL so a temporarily empty database does not disable dynamic ranges.
_PRICE_RANGE_TTL_SECONDS = int(os.environ.get("CANARY_PRICE_RANGE_TTL_SECONDS", "21600"))
_PRICE_RANGE_NEGATIVE_TTL_SECONDS = int(
    os.environ.get("CANARY_PRICE_RANGE_NEGATIVE_TTL_SECONDS", "300")
)


# Backward-compatible aliases for callers that imported the old module helpers.
_get_provider_class = get_provider_class
_is_compatible = is_compatible


class CanaryMonitor:
    """
    Moniteur canary quotidien.

    Applique les règles métier de monitoring via un port de persistance et
    utilise Redis pour publier le cache de statut de santé.
    """

    def __init__(
        self,
        repository: CanaryMonitoringRepository | None = None,
        redis_client: MonitoringHealthCache | None = None,
        *,
        price_range_ttl_seconds: int = _PRICE_RANGE_TTL_SECONDS,
        negative_price_range_ttl_seconds: int = _PRICE_RANGE_NEGATIVE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._repository = repository
        self.redis = redis_client
        self._price_ranges = DynamicPriceRangeResolver(
            repository,
            ttl_seconds=price_range_ttl_seconds,
            negative_ttl_seconds=negative_price_range_ttl_seconds,
            clock=clock,
        )
        self._dynamic_price_ranges = self._price_ranges.cache

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def run_all(self) -> Dict[str, List[CanaryCheckResult]]:
        """
        Exécute les checks canary pour tous les providers en parallèle.
        Semaphore(3) pour éviter de flood. Timeout global 120s.
        """
        sem = asyncio.Semaphore(_SEMAPHORE_LIMIT)
        all_results: Dict[str, List[CanaryCheckResult]] = {}

        async def _run_with_sem(name: str):
            async with sem:
                return name, await self.run_provider(name)

        try:
            tasks = [_run_with_sem(name) for name in MONITORED_PROVIDERS]
            done = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=120,
            )

            for item in done:
                if isinstance(item, Exception):
                    logger.error("[CanaryMonitor] Provider task error: %s", item)
                    continue
                prov_name, prov_results = item
                all_results[prov_name] = prov_results

                # Update daily stats and alerts
                daily_stats = await self._update_daily_stats(prov_name, prov_results)
                await self._evaluate_and_create_alerts(prov_name, prov_results, daily_stats)

        except TimeoutError:
            logger.error("[CanaryMonitor] Global timeout (120s) exceeded")
        except Exception as exc:
            logger.error("[CanaryMonitor] run_all failed: %s", exc, exc_info=True)

        # Update Redis health summary
        await self._update_redis_health_status(all_results)

        logger.info(
            "[CanaryMonitor] Run complete — %d providers checked",
            len(all_results),
        )
        return all_results

    async def run_provider(
        self,
        provider_name: str,
    ) -> List[CanaryCheckResult]:
        """
        Exécute les checks canary pour un provider spécifique.
        """
        results: List[CanaryCheckResult] = []
        provider_cls = _get_provider_class(provider_name)
        if provider_cls is None:
            logger.warning("[CanaryMonitor] Provider %s not loadable, skip", provider_name)
            return results

        for ticker, expected_fields in CANARY_ASSETS.items():
            if not _is_compatible(provider_name, ticker):
                continue

            try:
                provider_instance = provider_cls()
                data = await asyncio.wait_for(
                    provider_instance.get_financials(ticker),
                    timeout=15,
                )
            except TimeoutError:
                logger.warning("[CanaryMonitor] %s timeout for %s", provider_name, ticker)
                for field_name in expected_fields:
                    results.append(
                        CanaryCheckResult(
                            provider=provider_name,
                            ticker=ticker,
                            field=field_name,
                            status=HealthStatus.timeout,
                            checked_at=datetime.now(timezone.utc),
                        )
                    )
                continue
            except Exception as exc:
                logger.warning("[CanaryMonitor] %s error for %s: %s", provider_name, ticker, exc)
                for field_name in expected_fields:
                    results.append(
                        CanaryCheckResult(
                            provider=provider_name,
                            ticker=ticker,
                            field=field_name,
                            status=HealthStatus.null,
                            checked_at=datetime.now(timezone.utc),
                        )
                    )
                continue

            # Déduire dynamiquement le range pour "price" si possible
            price_range_override = None
            if "price" in expected_fields:
                price_range_override = await self._get_dynamic_price_range(ticker)

            for field_name, (exp_min, exp_max) in expected_fields.items():
                if field_name == "price" and price_range_override:
                    exp_min, exp_max = price_range_override

                val = self._extract_field(data, field_name)
                check_result = self._check_canary_value(
                    provider_name, ticker, field_name, val, exp_min, exp_max
                )
                results.append(check_result)

        # Log to DB
        await self._log_canary_results(results)

        return results

    async def _get_dynamic_price_range(self, ticker: str) -> Optional[tuple[float, float]]:
        return await self._price_ranges.get(ticker)

    def _cached_dynamic_price_range(self, ticker: str) -> tuple[bool, tuple[float, float] | None]:
        return self._price_ranges.cached(ticker)

    def _cache_dynamic_price_range(self, ticker: str, bounds: tuple[float, float] | None) -> None:
        self._price_ranges.store(ticker, bounds)

    def invalidate_dynamic_price_range(self, ticker: str | None = None) -> None:
        """Invalidate one ticker or all dynamic canary price ranges."""
        self._price_ranges.invalidate(ticker)

    async def _calculate_dynamic_price_range(self, ticker: str) -> tuple[float, float] | None:
        return await self._price_ranges._calculate(ticker)

    # ── Check canary value ────────────────────────────────────────────────────

    def _check_canary_value(
        self,
        provider: str,
        ticker: str,
        field: str,
        value: Optional[Decimal],
        exp_min: float,
        exp_max: float,
    ) -> CanaryCheckResult:
        now = datetime.now(timezone.utc)
        if value is None:
            return CanaryCheckResult(
                provider=provider,
                ticker=ticker,
                field=field,
                status=HealthStatus.null,
                expected_min=Decimal(str(exp_min)),
                expected_max=Decimal(str(exp_max)),
                checked_at=now,
            )

        fval = float(value)
        if fval < exp_min or fval > exp_max:
            # Compute deviation from nearest bound
            mid = (exp_min + exp_max) / 2
            deviation = abs(fval - mid) / mid if mid != 0 else None
            return CanaryCheckResult(
                provider=provider,
                ticker=ticker,
                field=field,
                value_received=value,
                expected_min=Decimal(str(exp_min)),
                expected_max=Decimal(str(exp_max)),
                deviation_pct=Decimal(str(round(deviation, 4))) if deviation else None,
                status=HealthStatus.out_of_range,
                checked_at=now,
            )

        return CanaryCheckResult(
            provider=provider,
            ticker=ticker,
            field=field,
            value_received=value,
            expected_min=Decimal(str(exp_min)),
            expected_max=Decimal(str(exp_max)),
            status=HealthStatus.ok,
            checked_at=now,
        )

    # ── Mise à jour des agrégats quotidiens ──────────────────────────────────

    async def _update_daily_stats(
        self,
        provider_name: str,
        results: List[CanaryCheckResult],
    ) -> DailyHealthStats:
        """Upsert dans provider_health_daily pour aujourd'hui."""
        checks_total = len(results)
        checks_ok = sum(1 for result in results if result.status == HealthStatus.ok)
        success_rate = round(checks_ok / checks_total, 4) if checks_total > 0 else 0.0
        stats: DailyHealthStats = {
            "checks_total": checks_total,
            "checks_ok": checks_ok,
            "checks_outlier": sum(1 for r in results if r.status == HealthStatus.outlier),
            "checks_null": sum(
                1 for r in results if r.status in (HealthStatus.null, HealthStatus.timeout)
            ),
            "checks_timeout": sum(1 for r in results if r.status == HealthStatus.timeout),
            "success_rate": success_rate,
            "canary_passed": (
                all(result.status == HealthStatus.ok for result in results) if results else None
            ),
            "is_healthy": success_rate >= ALERT_THRESHOLDS["success_rate_warning"],
        }

        if not self._repository:
            return stats

        try:
            await self._repository.upsert_daily_stats(provider_name, date.today(), stats)
        except MonitoringRepositoryError as exc:
            logger.warning("[CanaryMonitor] Failed to update daily stats: %s", exc)

        return stats

    # ── Gestion des alertes ───────────────────────────────────────────────────

    async def _evaluate_and_create_alerts(
        self,
        provider_name: str,
        results: List[CanaryCheckResult],
        daily_stats: DailyHealthStats,
    ) -> List[int]:
        """
        Évalue si des alertes doivent être créées ou résolues.
        Déduplique les alertes actives de même type pour le provider.
        """
        if not self._repository:
            return []

        created_ids: List[int] = []

        try:
            failures = [
                r for r in results if r.status in (HealthStatus.out_of_range, HealthStatus.outlier)
            ]
            all_ok = all(r.status == HealthStatus.ok for r in results) if results else False

            if failures:
                severity = (
                    "critical"
                    if len(failures) >= ALERT_THRESHOLDS["canary_failures_critical"]
                    else "warning"
                )
                failed_fields = ", ".join(
                    f"{failure.ticker}.{failure.field}={failure.value_received}"
                    for failure in failures[:5]
                )
                first_failure = failures[0]
                alert_id = await self._repository.create_alert_if_absent(
                    provider_name,
                    AlertCandidate(
                        alert_type="canary_failed",
                        severity=severity,
                        description=f"Canary check failed: {failed_fields}",
                        ticker=first_failure.ticker,
                        field=first_failure.field,
                        value_received=first_failure.value_received,
                        value_expected=(
                            f"[{first_failure.expected_min}, {first_failure.expected_max}]"
                        ),
                    ),
                )
                if alert_id is not None:
                    created_ids.append(alert_id)

            success_rate = daily_stats["success_rate"]
            if success_rate < ALERT_THRESHOLDS["success_rate_warning"]:
                severity = (
                    "critical"
                    if success_rate < ALERT_THRESHOLDS["success_rate_critical"]
                    else "warning"
                )
                alert_id = await self._repository.create_alert_if_absent(
                    provider_name,
                    AlertCandidate(
                        alert_type="high_outlier_rate",
                        severity=severity,
                        description=f"Success rate {success_rate:.1%} below threshold",
                    ),
                )
                if alert_id is not None:
                    created_ids.append(alert_id)

            if all_ok:
                await self._repository.resolve_alerts(
                    provider_name,
                    "canary_failed",
                    "Auto-resolved: all canary checks passed",
                )

        except MonitoringRepositoryError as exc:
            logger.warning("[CanaryMonitor] Failed to evaluate alerts: %s", exc)

        return created_ids

    # ── Cache Redis du statut de santé ────────────────────────────────────────

    async def _update_redis_health_status(
        self,
        all_results: Dict[str, List[CanaryCheckResult]],
    ) -> None:
        """Met à jour le cache Redis avec le statut de santé global."""
        if not self.redis:
            return

        try:
            providers_summary = {}
            for prov_name, checks in all_results.items():
                total = len(checks)
                ok = sum(1 for c in checks if c.status == HealthStatus.ok)
                rate = ok / total if total > 0 else 0.0

                if rate >= ALERT_THRESHOLDS["success_rate_warning"]:
                    status = "ok"
                elif rate >= ALERT_THRESHOLDS["success_rate_critical"]:
                    status = "degraded"
                else:
                    status = "down"

                providers_summary[prov_name] = {
                    "status": status,
                    "success_rate": round(rate, 4),
                }

            payload = {
                "last_run": datetime.now(timezone.utc).isoformat(),
                "providers": providers_summary,
            }

            await self.redis.set(
                "provider:health:summary",
                json.dumps(payload, default=str),
                ex=3600,
            )
        except Exception as exc:
            logger.warning("[CanaryMonitor] Failed to update Redis health: %s", exc)

    # ── Log canary results to DB ──────────────────────────────────────────────

    async def _log_canary_results(self, results: List[CanaryCheckResult]) -> None:
        """Batch insert des résultats canary dans provider_health_log."""
        if not results or not self._repository:
            return

        try:
            await self._repository.save_canary_results(results)
        except MonitoringRepositoryError as exc:
            logger.warning("[CanaryMonitor] Failed to log canary results: %s", exc)

    # ── Field extraction ──────────────────────────────────────────────────────

    def _extract_field(self, obj: Any, field: str) -> Optional[Decimal]:
        """Extrait un champ d'un objet de façon défensive."""
        try:
            if obj is None:
                return None
            if isinstance(obj, dict):
                val = obj.get(field)
            else:
                val = getattr(obj, field, None)
            if val is None:
                return None
            return Decimal(str(float(val)))
        except (ValueError, TypeError, ArithmeticError):
            return None
