"""SQLAlchemy persistence adapter for provider monitoring."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import Asset, PriceEOD, ProviderAlert, ProviderHealthDaily, ProviderHealthLog
from monitoring.models import CanaryCheckResult
from monitoring.ports import (
    AlertCandidate,
    DailyHealthStats,
    MonitoringRepositoryError,
    ValidationLogEntry,
)


class SqlAlchemyMonitoringRepository:
    """Translate monitoring persistence operations into SQLAlchemy queries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_recent_closing_prices(self, ticker: str, since: datetime) -> list[float] | None:
        try:
            async with self._session_factory() as session:
                asset_id = (
                    await session.execute(select(Asset.id).where(Asset.ticker == ticker))
                ).scalar()
                if not asset_id:
                    return None
                statement = (
                    select(PriceEOD.close)
                    .where(PriceEOD.asset_id == asset_id)
                    .where(PriceEOD.timestamp >= since)
                    .where(PriceEOD.resolution == "1D")
                )
                result = await session.execute(statement)
                return [float(price) for price in result.scalars().all() if price is not None]
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to read historical prices") from exc

    async def save_validation_logs(self, entries: Sequence[ValidationLogEntry]) -> None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add_all([ProviderHealthLog(**entry) for entry in entries])
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to save validation logs") from exc

    async def upsert_daily_stats(
        self, provider_name: str, day: date, stats: DailyHealthStats
    ) -> None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    statement = select(ProviderHealthDaily).where(
                        ProviderHealthDaily.provider_name == provider_name,
                        ProviderHealthDaily.date == day,
                    )
                    row = (await session.execute(statement)).scalars().first()
                    values = {
                        "checks_total": stats["checks_total"],
                        "checks_ok": stats["checks_ok"],
                        "checks_outlier": stats["checks_outlier"],
                        "checks_null": stats["checks_null"],
                        "checks_timeout": stats["checks_timeout"],
                        "success_rate": Decimal(str(stats["success_rate"])),
                        "canary_passed": stats["canary_passed"],
                        "is_healthy": stats["is_healthy"],
                    }
                    if row:
                        for field, value in values.items():
                            setattr(row, field, value)
                    else:
                        session.add(
                            ProviderHealthDaily(provider_name=provider_name, date=day, **values)
                        )
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to update daily monitoring stats") from exc

    async def create_alert_if_absent(
        self, provider_name: str, candidate: AlertCandidate
    ) -> int | None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    statement = select(ProviderAlert).where(
                        ProviderAlert.provider_name == provider_name,
                        ProviderAlert.alert_type == candidate.alert_type,
                        ProviderAlert.is_resolved.is_(False),
                    )
                    if (await session.execute(statement)).scalars().first():
                        return None
                    alert = ProviderAlert(
                        provider_name=provider_name,
                        alert_type=candidate.alert_type,
                        severity=candidate.severity,
                        description=candidate.description,
                        ticker=candidate.ticker,
                        field=candidate.field,
                        value_received=candidate.value_received,
                        value_expected=candidate.value_expected,
                    )
                    session.add(alert)
                    await session.flush()
                    return int(alert.id)
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to create monitoring alert") from exc

    async def resolve_alerts(self, provider_name: str, alert_type: str, note: str) -> None:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    statement = select(ProviderAlert).where(
                        ProviderAlert.provider_name == provider_name,
                        ProviderAlert.alert_type == alert_type,
                        ProviderAlert.is_resolved.is_(False),
                    )
                    alerts = (await session.execute(statement)).scalars().all()
                    resolved_at = datetime.now(timezone.utc)
                    for alert in alerts:
                        alert.is_resolved = True
                        alert.resolved_at = resolved_at
                        alert.resolution_note = note
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to resolve monitoring alerts") from exc

    async def save_canary_results(self, results: Sequence[CanaryCheckResult]) -> None:
        entries = [
            ProviderHealthLog(
                provider_name=result.provider,
                ticker=result.ticker,
                field=result.field,
                value_received=result.value_received,
                value_expected_min=result.expected_min,
                value_expected_max=result.expected_max,
                deviation_pct=result.deviation_pct,
                status=result.status.value,
                check_type="canary",
            )
            for result in results
        ]
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add_all(entries)
        except SQLAlchemyError as exc:
            raise MonitoringRepositoryError("Unable to save canary results") from exc
