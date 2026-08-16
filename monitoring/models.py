"""Framework-neutral models used by provider-monitoring policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class HealthStatus(str, Enum):
    ok = "ok"
    outlier = "outlier"
    out_of_range = "out_of_range"
    null = "null"
    timeout = "timeout"


@dataclass(frozen=True)
class CanaryCheckResult:
    """Result of one provider, ticker and field canary check."""

    provider: str
    ticker: str
    field: str
    status: HealthStatus
    checked_at: datetime
    value_received: Decimal | None = None
    expected_min: Decimal | None = None
    expected_max: Decimal | None = None
    deviation_pct: Decimal | None = None
