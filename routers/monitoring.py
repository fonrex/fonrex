"""
routers/monitoring.py — Endpoints de monitoring des providers.

7 endpoints pour le suivi de la santé des providers financiers :
  1. GET  /health/providers              — résumé global (depuis Redis)
  2. GET  /health/providers/{name}       — détail par provider
  3. GET  /health/alerts                 — alertes actives
  4. POST /health/alerts/{id}/resolve    — résolution manuelle
  5. POST /health/canary/run             — check canary manuel
  6. GET  /health/canary/history         — historique des checks
  7. GET  /health/stats                  — statistiques globales
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from redis.exceptions import RedisError
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from schemas.monitoring import (
    AlertSchema,
    DailyStatSchema,
    HealthStatsResponse,
    ProviderDetailResponse,
    ProviderHealthSummary,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Provider Health"])


def get_canary_monitor(request: Request):
    return getattr(request.app.state, "canary_monitor", None)


def get_async_session_factory(request: Request):
    return getattr(request.app.state, "async_session_factory", None)


def get_redis_client(request: Request):
    return getattr(request.app.state, "redis_client", None)


def _decimal_to_float(val):
    if isinstance(val, Decimal):
        return float(val)
    return val


# ── Endpoint 1 : Statut global (depuis Redis cache) ──────────────────────────


@router.get("/health/providers", response_model=ProviderHealthSummary)
async def get_providers_health(
    redis_client=Depends(get_redis_client),
    async_session_factory=Depends(get_async_session_factory),
):
    """
    Statut de santé de tous les providers en temps réel.
    Lit depuis Redis (mis à jour par le canary monitor quotidien).
    Fallback sur la base de données si Redis indisponible.
    """
    # Try Redis first
    if redis_client:
        try:
            cached = await redis_client.get("provider:health:summary")
            if cached:
                data = json.loads(cached)
                providers_list = []
                for name, info in data.get("providers", {}).items():
                    status = info.get("status", "ok")
                    rate = info.get("success_rate", 1.0)
                    providers_list.append(
                        ProviderStatus(
                            name=name,
                            is_healthy=status == "ok",
                            success_rate_7d=rate,
                            status_label=status.upper(),
                        )
                    )

                healthy = sum(1 for p in providers_list if p.status_label == "OK")
                degraded = sum(1 for p in providers_list if p.status_label == "DEGRADED")
                down = sum(1 for p in providers_list if p.status_label == "DOWN")

                return ProviderHealthSummary(
                    checked_at=datetime.fromisoformat(data["last_run"]),
                    total_providers=len(providers_list),
                    healthy=healthy,
                    degraded=degraded,
                    down=down,
                    providers=providers_list,
                )
        except (RedisError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("[monitoring] Redis fallback: %s", exc)

    # Fallback to DB
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderAlert, ProviderHealthDaily

    providers_list = []
    try:
        async with async_session_factory() as session:
            seven_days_ago = date.today() - timedelta(days=7)
            rows = (
                await session.execute(
                    select(
                        ProviderHealthDaily.provider_name,
                        func.avg(ProviderHealthDaily.success_rate).label("avg_rate"),
                        func.avg(ProviderHealthDaily.avg_latency_ms).label("avg_lat"),
                        func.max(ProviderHealthDaily.date).label("last_date"),
                        func.bool_and(ProviderHealthDaily.canary_passed).label("canary_ok"),
                    )
                    .where(
                        ProviderHealthDaily.date >= seven_days_ago,
                    )
                    .group_by(ProviderHealthDaily.provider_name)
                )
            ).all()

            for row in rows:
                name = row.provider_name
                rate = _decimal_to_float(row.avg_rate)

                # Count active alerts
                alert_count = (
                    await session.execute(
                        select(func.count()).where(
                            ProviderAlert.provider_name == name,
                            ProviderAlert.is_resolved.is_(False),
                        )
                    )
                ).scalar() or 0

                if rate is not None and rate >= 0.85:
                    label = "OK"
                elif rate is not None and rate >= 0.70:
                    label = "DEGRADED"
                else:
                    label = "DOWN"

                providers_list.append(
                    ProviderStatus(
                        name=name,
                        is_healthy=label == "OK",
                        success_rate_7d=round(rate, 4) if rate else None,
                        avg_latency_ms=int(row.avg_lat) if row.avg_lat else None,
                        last_check=datetime.combine(row.last_date, datetime.min.time())
                        if row.last_date
                        else None,
                        canary_passed=row.canary_ok,
                        active_alerts=alert_count,
                        status_label=label,
                    )
                )
    except SQLAlchemyError as exc:
        logger.error("[monitoring] DB fallback failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load provider health") from exc

    healthy = sum(1 for p in providers_list if p.status_label == "OK")
    degraded = sum(1 for p in providers_list if p.status_label == "DEGRADED")
    down = sum(1 for p in providers_list if p.status_label == "DOWN")

    return ProviderHealthSummary(
        checked_at=datetime.now(timezone.utc),
        total_providers=len(providers_list),
        healthy=healthy,
        degraded=degraded,
        down=down,
        providers=providers_list,
    )


# ── Endpoint 2 : Détail par provider ─────────────────────────────────────────


@router.get("/health/providers/{provider_name}", response_model=ProviderDetailResponse)
async def get_provider_health_detail(
    provider_name: str,
    days: int = Query(7, ge=1, le=90),
    async_session_factory=Depends(get_async_session_factory),
):
    """Détail de santé d'un provider sur les N derniers jours."""
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderAlert, ProviderHealthDaily, ProviderHealthLog

    async with async_session_factory() as session:
        since = date.today() - timedelta(days=days)
        since_30 = date.today() - timedelta(days=30)

        # Daily stats
        daily_rows = (
            (
                await session.execute(
                    select(ProviderHealthDaily)
                    .where(
                        ProviderHealthDaily.provider_name == provider_name,
                        ProviderHealthDaily.date >= since,
                    )
                    .order_by(ProviderHealthDaily.date.desc())
                )
            )
            .scalars()
            .all()
        )

        # 7d and 30d success rates
        rate_7d = None
        rate_30d = None
        if daily_rows:
            rates_7 = [
                _decimal_to_float(r.success_rate) for r in daily_rows if r.success_rate is not None
            ]
            rate_7d = round(sum(rates_7) / len(rates_7), 4) if rates_7 else None

        all_30 = (
            await session.execute(
                select(func.avg(ProviderHealthDaily.success_rate)).where(
                    ProviderHealthDaily.provider_name == provider_name,
                    ProviderHealthDaily.date >= since_30,
                )
            )
        ).scalar()
        rate_30d = round(_decimal_to_float(all_30), 4) if all_30 else None

        avg_lat = (
            await session.execute(
                select(func.avg(ProviderHealthDaily.avg_latency_ms)).where(
                    ProviderHealthDaily.provider_name == provider_name,
                    ProviderHealthDaily.date >= since,
                )
            )
        ).scalar()

        # Recent failures
        failures = (
            (
                await session.execute(
                    select(ProviderHealthLog)
                    .where(
                        ProviderHealthLog.provider_name == provider_name,
                        ProviderHealthLog.status.in_(["out_of_range", "outlier"]),
                    )
                    .order_by(ProviderHealthLog.checked_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

        recent_failures = [
            {
                "ticker": f.ticker,
                "field": f.field,
                "value": _decimal_to_float(f.value_received),
                "expected": f"[{_decimal_to_float(f.value_expected_min)}, {_decimal_to_float(f.value_expected_max)}]",
                "at": f.checked_at.isoformat() if f.checked_at else None,
            }
            for f in failures
        ]

        # Active alerts
        alerts = (
            (
                await session.execute(
                    select(ProviderAlert)
                    .where(
                        ProviderAlert.provider_name == provider_name,
                        ProviderAlert.is_resolved.is_(False),
                    )
                    .order_by(ProviderAlert.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        # Determine status label
        if rate_7d is not None and rate_7d >= 0.85:
            status = "ok"
        elif rate_7d is not None and rate_7d >= 0.70:
            status = "degraded"
        else:
            status = "down" if rate_7d is not None else "unknown"

        return ProviderDetailResponse(
            provider=provider_name,
            status=status,
            success_rate_7d=rate_7d,
            success_rate_30d=rate_30d,
            avg_latency_ms=int(avg_lat) if avg_lat else None,
            daily_stats=[DailyStatSchema.model_validate(r) for r in daily_rows],
            recent_failures=recent_failures,
            active_alerts=[AlertSchema.model_validate(a) for a in alerts],
        )


# ── Endpoint 3 : Alertes actives ─────────────────────────────────────────────


@router.get("/health/alerts", response_model=List[AlertSchema])
async def get_active_alerts(
    severity: Optional[str] = None,
    provider_name: Optional[str] = None,
    include_resolved: bool = False,
    limit: int = Query(50, ge=1, le=500),
    async_session_factory=Depends(get_async_session_factory),
):
    """Liste des alertes actives (ou résolues si include_resolved=True)."""
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderAlert

    async with async_session_factory() as session:
        stmt = select(ProviderAlert)
        conditions = []

        if not include_resolved:
            conditions.append(ProviderAlert.is_resolved.is_(False))
        if severity:
            conditions.append(ProviderAlert.severity == severity)
        if provider_name:
            conditions.append(ProviderAlert.provider_name == provider_name)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(ProviderAlert.created_at.desc()).limit(limit)

        rows = (await session.execute(stmt)).scalars().all()
        return [AlertSchema.model_validate(r) for r in rows]


# ── Endpoint 4 : Résolution manuelle d'une alerte ────────────────────────────


@router.post("/health/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    resolution_note: Optional[str] = None,
    async_session_factory=Depends(get_async_session_factory),
):
    """Marque une alerte comme résolue manuellement."""
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderAlert

    async with async_session_factory() as session:
        async with session.begin():
            alert = (
                (await session.execute(select(ProviderAlert).where(ProviderAlert.id == alert_id)))
                .scalars()
                .first()
            )

            if not alert:
                raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
            if alert.is_resolved:
                return {"status": "already_resolved", "alert_id": alert_id}

            alert.is_resolved = True
            alert.resolved_at = datetime.now(timezone.utc)
            alert.resolution_note = resolution_note or "Resolved manually"

    return {
        "status": "resolved",
        "alert_id": alert_id,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Endpoint 5 : Déclencher un check canary manuel ───────────────────────────


@router.post("/health/canary/run")
async def run_canary_check(
    provider_name: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
    canary_monitor=Depends(get_canary_monitor),
):
    """Déclenche un check canary immédiat en background."""
    if not canary_monitor:
        raise HTTPException(status_code=503, detail="Canary monitor not configured")

    if provider_name:
        background_tasks.add_task(canary_monitor.run_provider, provider_name)
    else:
        background_tasks.add_task(canary_monitor.run_all)

    return {
        "status": "queued",
        "provider": provider_name or "all",
        "message": "Check canary déclenché en arrière-plan",
    }


# ── Endpoint 6 : Historique des checks canary ────────────────────────────────


@router.get("/health/canary/history")
async def get_canary_history(
    provider_name: Optional[str] = None,
    ticker: Optional[str] = None,
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(100, ge=1, le=1000),
    async_session_factory=Depends(get_async_session_factory),
):
    """Historique des résultats canary."""
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderHealthLog

    async with async_session_factory() as session:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(ProviderHealthLog).where(
            ProviderHealthLog.check_type == "canary",
            ProviderHealthLog.checked_at >= since,
        )

        if provider_name:
            stmt = stmt.where(ProviderHealthLog.provider_name == provider_name)
        if ticker:
            stmt = stmt.where(ProviderHealthLog.ticker == ticker)

        stmt = stmt.order_by(ProviderHealthLog.checked_at.desc()).limit(limit)

        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "provider": r.provider_name,
                "ticker": r.ticker,
                "field": r.field,
                "value_received": _decimal_to_float(r.value_received),
                "expected_min": _decimal_to_float(r.value_expected_min),
                "expected_max": _decimal_to_float(r.value_expected_max),
                "status": r.status,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in rows
        ]


# ── Endpoint 7 : Statistiques globales de validation ─────────────────────────


@router.get("/health/stats", response_model=HealthStatsResponse)
async def get_health_stats(
    async_session_factory=Depends(get_async_session_factory),
):
    """Statistiques globales sur la validation des données (7 derniers jours)."""
    if not async_session_factory:
        raise HTTPException(status_code=503, detail="Monitoring not configured")

    from models import ProviderHealthLog

    async with async_session_factory() as session:
        since = datetime.now(timezone.utc) - timedelta(days=7)

        # Count by status
        status_counts = (
            await session.execute(
                select(
                    ProviderHealthLog.status,
                    func.count().label("cnt"),
                )
                .where(
                    ProviderHealthLog.checked_at >= since,
                )
                .group_by(ProviderHealthLog.status)
            )
        ).all()

        counts = {row.status: row.cnt for row in status_counts}
        total = sum(counts.values())
        ok = counts.get("ok", 0)
        outliers = counts.get("outlier", 0)
        oor = counts.get("out_of_range", 0)
        nulls = counts.get("null", 0) + counts.get("timeout", 0)

        quality = round(ok / total, 4) if total > 0 else None

        # Most reliable providers (by success rate)
        prov_rates = (
            await session.execute(
                select(
                    ProviderHealthLog.provider_name,
                    func.count().label("total"),
                    func.sum(
                        case(
                            (ProviderHealthLog.status == "ok", 1),
                            else_=0,
                        )
                    ).label("ok_count"),
                )
                .where(
                    ProviderHealthLog.checked_at >= since,
                )
                .group_by(ProviderHealthLog.provider_name)
            )
        ).all()

        prov_reliability = []
        for row in prov_rates:
            rate = row.ok_count / row.total if row.total > 0 else 0
            prov_reliability.append((row.provider_name, rate))

        prov_reliability.sort(key=lambda x: x[1], reverse=True)
        most_reliable = [p[0] for p in prov_reliability[:3]]
        least_reliable = [p[0] for p in prov_reliability[-2:]] if len(prov_reliability) >= 2 else []

        # Fields most often invalid
        field_failures = (
            await session.execute(
                select(
                    ProviderHealthLog.field,
                    func.count().label("fail_count"),
                )
                .where(
                    ProviderHealthLog.checked_at >= since,
                    ProviderHealthLog.status.in_(["out_of_range", "outlier"]),
                )
                .group_by(ProviderHealthLog.field)
                .order_by(desc("fail_count"))
                .limit(5)
            )
        ).all()

        return HealthStatsResponse(
            period="last_7_days",
            total_values_validated=total,
            total_valid=ok,
            total_outliers=outliers,
            total_out_of_range=oor,
            total_nulls=nulls,
            overall_quality_score=quality,
            most_reliable_providers=most_reliable,
            least_reliable_providers=least_reliable,
            fields_most_often_invalid=[r.field for r in field_failures],
        )
