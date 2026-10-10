"""
Pydantic schemas of the provider monitoring.

Defines the enumerations, the validation schemas and the answer models of
the /health/* routes.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from monitoring.models import CanaryCheckResult, HealthStatus

__all__ = ["CanaryCheckResult", "HealthStatus"]


class AlertSeverity(str, Enum):
    critical = "critical"
    warning = "warning"
    info = "info"


class AlertType(str, Enum):
    canary_failed = "canary_failed"
    high_outlier_rate = "high_outlier_rate"
    consecutive_nulls = "consecutive_nulls"
    latency_spike = "latency_spike"


class ProviderStatus(BaseModel):
    """Current status of a provider."""

    name: str
    is_healthy: bool
    success_rate_7d: Optional[float] = None
    avg_latency_ms: Optional[int] = None
    last_check: Optional[datetime] = None
    canary_passed: Optional[bool] = None
    active_alerts: int = 0
    status_label: str  # "OK" | "DEGRADED" | "DOWN"


class ProviderHealthSummary(BaseModel):
    """Overall health summary of every provider."""

    checked_at: datetime
    total_providers: int
    healthy: int
    degraded: int
    down: int
    providers: List[ProviderStatus]


class ValidationResult(BaseModel):
    """Validation result of a value."""

    provider: str
    field: str
    value: Optional[Decimal] = None
    is_valid: bool
    reason: Optional[str] = None
    consensus: Optional[Decimal] = None
    deviation_pct: Optional[Decimal] = None


class AlertSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_name: str
    alert_type: str
    severity: str
    description: str
    ticker: Optional[str] = None
    field: Optional[str] = None
    value_received: Optional[Decimal] = None
    value_expected: Optional[str] = None
    created_at: datetime
    is_resolved: bool
    resolved_at: Optional[datetime] = None


class DailyStatSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date
    checks_total: int = 0
    checks_ok: int = 0
    checks_outlier: int = 0
    checks_null: int = 0
    checks_timeout: int = 0
    success_rate: Optional[float] = None
    avg_latency_ms: Optional[int] = None
    canary_passed: Optional[bool] = None
    is_healthy: bool = True


class ProviderDetailResponse(BaseModel):
    """Health detail of a provider over the last N days."""

    provider: str
    status: str
    success_rate_7d: Optional[float] = None
    success_rate_30d: Optional[float] = None
    avg_latency_ms: Optional[int] = None
    daily_stats: List[DailyStatSchema] = []
    recent_failures: List[dict] = []
    active_alerts: List[AlertSchema] = []


class HealthStatsResponse(BaseModel):
    """Overall data validation statistics."""

    period: str
    total_values_validated: int
    total_valid: int
    total_outliers: int
    total_out_of_range: int
    total_nulls: int
    overall_quality_score: Optional[float] = None
    most_reliable_providers: List[str] = []
    least_reliable_providers: List[str] = []
    fields_most_often_invalid: List[str] = []
