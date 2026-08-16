"""Persistence contracts required by the provider monitoring use cases."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, Sequence, TypedDict

from monitoring.models import CanaryCheckResult


class MonitoringRepositoryError(RuntimeError):
    """Raised when the monitoring persistence adapter cannot complete an operation."""


class ValidationLogEntry(TypedDict):
    provider_name: str
    ticker: str
    field: str
    value_received: Decimal | None
    value_expected_min: Decimal | None
    value_expected_max: Decimal | None
    consensus_value: Decimal | None
    deviation_pct: Decimal | None
    status: str
    check_type: str


class DailyHealthStats(TypedDict):
    checks_total: int
    checks_ok: int
    checks_outlier: int
    checks_null: int
    checks_timeout: int
    success_rate: float
    canary_passed: bool | None
    is_healthy: bool


@dataclass(frozen=True)
class AlertCandidate:
    """Persistence-neutral description of an alert selected by monitoring policy."""

    alert_type: str
    severity: str
    description: str
    ticker: str | None = None
    field: str | None = None
    value_received: Decimal | None = None
    value_expected: str | None = None


class ValidationLogRepository(Protocol):
    async def save_validation_logs(self, entries: Sequence[ValidationLogEntry]) -> None: ...


class MonitoringHealthCache(Protocol):
    """Cache operation required to publish the provider health summary."""

    def setex(self, key: str, ttl: int, value: str) -> Awaitable[object]: ...


class CanaryMonitoringRepository(Protocol):
    async def get_recent_closing_prices(
        self, ticker: str, since: datetime
    ) -> list[float] | None: ...

    async def upsert_daily_stats(
        self, provider_name: str, day: date, stats: DailyHealthStats
    ) -> None: ...

    async def create_alert_if_absent(
        self, provider_name: str, candidate: AlertCandidate
    ) -> int | None: ...

    async def resolve_alerts(self, provider_name: str, alert_type: str, note: str) -> None: ...

    async def save_canary_results(self, results: Sequence[CanaryCheckResult]) -> None: ...
