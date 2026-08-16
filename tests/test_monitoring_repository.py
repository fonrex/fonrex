"""Focused tests for the SQLAlchemy monitoring adapter."""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from database.monitoring import SqlAlchemyMonitoringRepository
from monitoring.models import CanaryCheckResult, HealthStatus
from monitoring.ports import AlertCandidate, MonitoringRepositoryError


def _session_adapter() -> tuple[SqlAlchemyMonitoringRepository, MagicMock]:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    return SqlAlchemyMonitoringRepository(factory), session


@pytest.mark.asyncio
async def test_get_recent_closing_prices_maps_query_result():
    repository, session = _session_adapter()
    asset_result = MagicMock()
    asset_result.scalar.return_value = 7
    prices_result = MagicMock()
    prices_result.scalars.return_value.all.return_value = [Decimal("10.5"), None]
    session.execute.side_effect = [asset_result, prices_result]

    prices = await repository.get_recent_closing_prices("AAPL", datetime.now(timezone.utc))

    assert prices == [10.5]
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_save_validation_logs_builds_orm_rows():
    repository, session = _session_adapter()
    entry = {
        "provider_name": "ProviderA",
        "ticker": "AAPL",
        "field": "price",
        "value_received": Decimal("100"),
        "value_expected_min": None,
        "value_expected_max": None,
        "consensus_value": None,
        "deviation_pct": None,
        "status": "ok",
        "check_type": "realtime",
    }

    await repository.save_validation_logs([entry])

    rows = session.add_all.call_args.args[0]
    assert len(rows) == 1
    assert rows[0].provider_name == "ProviderA"


@pytest.mark.asyncio
async def test_upsert_daily_stats_updates_existing_row():
    repository, session = _session_adapter()
    row = MagicMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = row
    session.execute.return_value = result
    stats = {
        "checks_total": 2,
        "checks_ok": 1,
        "checks_outlier": 1,
        "checks_null": 0,
        "checks_timeout": 0,
        "success_rate": 0.5,
        "canary_passed": False,
        "is_healthy": False,
    }

    await repository.upsert_daily_stats("ProviderA", date.today(), stats)

    assert row.checks_total == 2
    assert row.success_rate == Decimal("0.5")


@pytest.mark.asyncio
async def test_create_alert_if_absent_returns_created_id():
    repository, session = _session_adapter()
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    session.execute.return_value = result
    session.add.side_effect = lambda alert: setattr(alert, "id", 42)

    alert_id = await repository.create_alert_if_absent(
        "ProviderA",
        AlertCandidate("canary_failed", "warning", "A canary failed"),
    )

    assert alert_id == 42
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_alerts_updates_active_rows():
    repository, session = _session_adapter()
    alert = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [alert]
    session.execute.return_value = result

    await repository.resolve_alerts("ProviderA", "canary_failed", "Recovered")

    assert alert.is_resolved is True
    assert alert.resolution_note == "Recovered"
    assert alert.resolved_at is not None


@pytest.mark.asyncio
async def test_save_canary_results_builds_orm_rows():
    repository, session = _session_adapter()
    result = CanaryCheckResult(
        provider="ProviderA",
        ticker="AAPL",
        field="price",
        status=HealthStatus.ok,
        checked_at=datetime.now(timezone.utc),
    )

    await repository.save_canary_results([result])

    rows = session.add_all.call_args.args[0]
    assert rows[0].check_type == "canary"
    assert rows[0].status == "ok"


@pytest.mark.asyncio
async def test_sqlalchemy_errors_are_translated_to_port_error():
    repository, session = _session_adapter()
    session.execute.side_effect = SQLAlchemyError("database unavailable")

    with pytest.raises(MonitoringRepositoryError):
        await repository.get_recent_closing_prices("AAPL", datetime.now(timezone.utc))
