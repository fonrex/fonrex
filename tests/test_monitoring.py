"""
tests/test_monitoring.py — Tests unitaires du système de monitoring providers.

Couvre :
  - ValidationLayer : range check, consensus, batch logging
  - CanaryMonitor : check canary, daily stats, alertes
  - Router endpoints : via TestClient FastAPI
"""

import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from monitoring.canary_monitor import (
    CanaryMonitor,
    _get_provider_class,
    _is_compatible,
)
from monitoring.validation_layer import (
    CONSENSUS_DEVIATION_THRESHOLD,
    FIELD_RANGES,
    ValidationLayer,
)
from schemas.monitoring import (
    CanaryCheckResult,
    DailyStatSchema,
    HealthStatsResponse,
    HealthStatus,
    ProviderHealthSummary,
    ProviderStatus,
)

# ── ValidationLayer Tests ─────────────────────────────────────────────────────


class TestValidationLayerRangeCheck:
    """Tests pour la validation par range."""

    def setup_method(self):
        self.validator = ValidationLayer(repository=None)

    def test_range_check_ok(self):
        status, reason = self.validator._check_range("pe_ratio", Decimal("25.0"))
        assert status == "ok"

    def test_range_check_too_low(self):
        status, reason = self.validator._check_range("pe_ratio", Decimal("0.01"))
        assert status == "out_of_range"
        assert "min" in reason.lower() or "<" in reason

    def test_range_check_too_high(self):
        status, reason = self.validator._check_range("pe_ratio", Decimal("5000"))
        assert status == "out_of_range"

    def test_range_check_none(self):
        status, reason = self.validator._check_range("pe_ratio", None)
        assert status == "null"

    def test_range_check_unknown_field(self):
        """Un champ inconnu ne doit pas lever d'erreur."""
        status, reason = self.validator._check_range("unknown_field", Decimal("42.0"))
        assert status == "ok"

    def test_range_check_boundary_min(self):
        """Valeur exactement au minimum → OK."""
        min_val, _ = FIELD_RANGES["pe_ratio"]
        status, _ = self.validator._check_range("pe_ratio", Decimal(str(min_val)))
        assert status == "ok"

    def test_range_check_boundary_max(self):
        """Valeur exactement au maximum → OK."""
        _, max_val = FIELD_RANGES["pe_ratio"]
        status, _ = self.validator._check_range("pe_ratio", Decimal(str(max_val)))
        assert status == "ok"


class TestValidationLayerConsensus:
    """Tests pour la validation par consensus."""

    def setup_method(self):
        self.validator = ValidationLayer(repository=None)

    def test_consensus_ok(self):
        status, dev, reason = self.validator._check_consensus(
            "pe_ratio", Decimal("25.0"), Decimal("24.0"), "TestProv"
        )
        assert status == "ok"

    def test_consensus_outlier(self):
        """Valeur qui dépasse le seuil de déviation → outlier."""
        status, dev, reason = self.validator._check_consensus(
            "pe_ratio", Decimal("0.8"), Decimal("24.0"), "TestProv"
        )
        assert status == "outlier"
        assert dev is not None
        assert float(dev) > CONSENSUS_DEVIATION_THRESHOLD

    def test_consensus_near_zero(self):
        """Consensus proche de 0 → pas de rejet."""
        status, dev, reason = self.validator._check_consensus(
            "pe_ratio", Decimal("0.001"), Decimal("0.0"), "TestProv"
        )
        assert status == "ok"

    def test_compute_consensus_median(self):
        """Le consensus est la médiane des valeurs valides."""
        vals = {
            "A": Decimal("20.0"),
            "B": Decimal("22.0"),
            "C": Decimal("21.0"),
        }
        consensus = self.validator._compute_consensus("pe_ratio", vals)
        assert consensus is not None
        assert float(consensus) == 21.0  # median

    def test_compute_consensus_too_few_providers(self):
        """Moins de MIN_PROVIDERS valeurs → consensus None."""
        vals = {"A": Decimal("20.0")}
        consensus = self.validator._compute_consensus("pe_ratio", vals)
        assert consensus is None

    def test_compute_consensus_filters_out_of_range(self):
        """Les valeurs hors range sont exclues du consensus."""
        vals = {
            "A": Decimal("20.0"),
            "B": Decimal("22.0"),
            "C": Decimal("99999.0"),  # out of range pour pe_ratio
        }
        consensus = self.validator._compute_consensus("pe_ratio", vals)
        assert consensus is not None
        assert float(consensus) == 21.0  # median de [20, 22]


class TestValidationLayerValidateResults:
    """Tests d'intégration pour validate_results."""

    def setup_method(self):
        self.validator = ValidationLayer(repository=None)

    @pytest.mark.asyncio
    async def test_validate_results_ok(self):
        results = {
            "ProviderA": {"pe_ratio": 25.0, "price": 150.0},
            "ProviderB": {"pe_ratio": 24.0, "price": 149.0},
        }
        cleaned = await self.validator.validate_results("AAPL", results)
        assert cleaned["ProviderA"]["pe_ratio"] is not None
        assert cleaned["ProviderB"]["pe_ratio"] is not None

    @pytest.mark.asyncio
    async def test_validate_results_rejects_out_of_range(self):
        results = {
            "ProviderA": {"pe_ratio": 0.01, "price": 150.0},
            "ProviderB": {"pe_ratio": 24.0, "price": 149.0},
        }
        cleaned = await self.validator.validate_results("AAPL", results)
        assert cleaned["ProviderA"]["pe_ratio"] is None  # rejected
        assert cleaned["ProviderB"]["pe_ratio"] is not None

    @pytest.mark.asyncio
    async def test_validate_results_rejects_outlier(self):
        results = {
            "ProviderA": {"pe_ratio": 24.0, "price": 150.0},
            "ProviderB": {"pe_ratio": 25.0, "price": 149.0},
            "ProviderC": {"pe_ratio": 0.8, "price": 150.0},  # outlier
        }
        cleaned = await self.validator.validate_results("AAPL", results)
        # ProviderC.pe_ratio should be rejected as outlier (0.8 vs median ~24)
        assert cleaned["ProviderC"]["pe_ratio"] is None

    @pytest.mark.asyncio
    async def test_validate_results_handles_none_provider(self):
        results = {
            "ProviderA": {"pe_ratio": 24.0},
            "ProviderB": None,
        }
        cleaned = await self.validator.validate_results("AAPL", results)
        assert cleaned["ProviderB"] is None

    @pytest.mark.asyncio
    async def test_validate_results_never_raises(self):
        """validate_results ne doit jamais lever d'exception."""
        results = {
            "Bad": {"pe_ratio": "invalid_string"},
        }
        cleaned = await self.validator.validate_results("AAPL", results)
        assert isinstance(cleaned, dict)

    @pytest.mark.asyncio
    async def test_validation_logs_are_sent_through_repository_port(self):
        repository = MagicMock()
        repository.save_validation_logs = AsyncMock()
        validator = ValidationLayer(repository=repository)

        await validator.validate_results("AAPL", {"ProviderA": {"pe_ratio": 25.0}})

        repository.save_validation_logs.assert_awaited_once()
        entries = repository.save_validation_logs.await_args.args[0]
        assert any(entry["field"] == "pe_ratio" and entry["status"] == "ok" for entry in entries)


class TestValidationLayerFieldExtraction:
    """Tests pour l'extraction de champs."""

    def setup_method(self):
        self.validator = ValidationLayer(repository=None)

    def test_extract_from_dict(self):
        val = self.validator._extract_field({"pe_ratio": 25.0}, "pe_ratio")
        assert val == Decimal("25.0")

    def test_extract_from_dict_none(self):
        val = self.validator._extract_field({"pe_ratio": None}, "pe_ratio")
        assert val is None

    def test_extract_from_dict_missing(self):
        val = self.validator._extract_field({"price": 100}, "pe_ratio")
        assert val is None

    def test_extract_from_pydantic(self):
        class FakeModel:
            pe_ratio = 25.0

        val = self.validator._extract_field(FakeModel(), "pe_ratio")
        assert val == Decimal("25.0")

    def test_extract_invalid_string(self):
        val = self.validator._extract_field({"pe_ratio": "not_a_number"}, "pe_ratio")
        assert val is None


# ── CanaryMonitor Tests ───────────────────────────────────────────────────────


class TestCanaryCompatibility:
    def test_eu_only_with_us_ticker(self):
        assert _is_compatible("Boursorama", "AAPL") is False
        assert _is_compatible("Fortuneo", "MSFT") is False

    def test_eu_only_with_eu_ticker(self):
        assert _is_compatible("Boursorama", "AIR.PA") is True

    def test_global_provider_with_us_ticker(self):
        assert _is_compatible("YahooFinance", "AAPL") is True


class TestCanaryMonitorArchitecture:
    """Verifies architectural invariants of the CanaryMonitor imports."""

    def test_all_monitored_providers_have_valid_imports(self):
        from monitoring.canary_monitor import MONITORED_PROVIDERS

        for name in MONITORED_PROVIDERS:
            cls = _get_provider_class(name)
            assert cls is not None, (
                f"Provider {name} could not be dynamically imported! Check mapping in canary_monitor.py"
            )
            assert isinstance(cls, type), (
                f"Provider {name} resolved to {cls} which is not a class type"
            )


class TestCanaryCheckValue:
    def setup_method(self):
        self.monitor = CanaryMonitor()

    def test_check_ok(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            Decimal("30.0"),
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.ok
        assert result.value_received == Decimal("30.0")

    def test_check_out_of_range_high(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            Decimal("100.0"),
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.out_of_range

    def test_check_out_of_range_low(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            Decimal("5.0"),
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.out_of_range

    def test_check_null(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            None,
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.null

    def test_check_boundary_exact_min(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            Decimal("20.0"),
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.ok

    def test_check_boundary_exact_max(self):
        result = self.monitor._check_canary_value(
            "TestProv",
            "AAPL",
            "pe_ratio",
            Decimal("45.0"),
            20.0,
            45.0,
        )
        assert result.status == HealthStatus.ok


class TestCanaryMonitorDynamicPriceRange:
    """Verify dynamic price range calculations from historical EOD prices."""

    @pytest.mark.asyncio
    async def test_no_repository(self):
        monitor = CanaryMonitor(repository=None)
        res = await monitor._get_dynamic_price_range("AAPL")
        assert res is None

    @pytest.mark.asyncio
    async def test_caching(self):
        monitor = CanaryMonitor(repository=None, clock=lambda: 100.0)
        monitor._cache_dynamic_price_range("AAPL", (150.0, 250.0))
        res = await monitor._get_dynamic_price_range("AAPL")
        assert res == (150.0, 250.0)

    @pytest.mark.asyncio
    async def test_no_asset_found(self):
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(return_value=None)
        monitor = CanaryMonitor(repository=repository)
        res = await monitor._get_dynamic_price_range("AAPL")
        assert res is None

    @pytest.mark.asyncio
    async def test_too_few_prices(self):
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(
            return_value=[100.0, 102.0, 98.0, 99.0, 101.0]
        )
        monitor = CanaryMonitor(repository=repository)
        res = await monitor._get_dynamic_price_range("AAPL")
        assert res is None

    @pytest.mark.asyncio
    async def test_success_range_calculation(self):
        # 10 prices at 100.0 -> mean=100.0, std_dev=0.0 -> max(0, 100*0.02) = 2.0
        # bounds = 100 +/- 3 * 2 = [94.0, 106.0]
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(return_value=[100.0] * 10)
        monitor = CanaryMonitor(repository=repository)
        res = await monitor._get_dynamic_price_range("AAPL")
        assert res is not None
        assert res == (94.0, 106.0)
        assert monitor._dynamic_price_ranges["AAPL"].bounds == (94.0, 106.0)

    @pytest.mark.asyncio
    async def test_expired_range_is_recalculated(self):
        now = [0.0]
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(side_effect=[[100.0] * 10, [200.0] * 10])
        monitor = CanaryMonitor(
            repository=repository,
            price_range_ttl_seconds=10,
            clock=lambda: now[0],
        )

        assert await monitor._get_dynamic_price_range("AAPL") == (94.0, 106.0)
        now[0] = 11.0
        assert await monitor._get_dynamic_price_range("AAPL") == (188.0, 212.0)
        assert repository.get_recent_closing_prices.await_count == 2

    @pytest.mark.asyncio
    async def test_negative_cache_expires_quickly(self):
        now = [0.0]
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(side_effect=[None, [100.0] * 10])
        monitor = CanaryMonitor(
            repository=repository,
            negative_price_range_ttl_seconds=5,
            clock=lambda: now[0],
        )

        assert await monitor._get_dynamic_price_range("AAPL") is None
        assert await monitor._get_dynamic_price_range("AAPL") is None
        assert repository.get_recent_closing_prices.await_count == 1
        now[0] = 6.0
        assert await monitor._get_dynamic_price_range("AAPL") == (94.0, 106.0)

    @pytest.mark.asyncio
    async def test_concurrent_providers_share_one_refresh(self):
        repository = MagicMock()
        repository.get_recent_closing_prices = AsyncMock(return_value=[100.0] * 10)
        monitor = CanaryMonitor(repository=repository)

        first, second = await asyncio.gather(
            monitor._get_dynamic_price_range("AAPL"),
            monitor._get_dynamic_price_range("AAPL"),
        )
        assert first == second == (94.0, 106.0)
        repository.get_recent_closing_prices.assert_awaited_once()

    def test_explicit_invalidation(self):
        monitor = CanaryMonitor(clock=lambda: 0.0)
        monitor._cache_dynamic_price_range("AAPL", (94.0, 106.0))
        monitor._cache_dynamic_price_range("MSFT", (300.0, 500.0))

        monitor.invalidate_dynamic_price_range("AAPL")
        assert "AAPL" not in monitor._dynamic_price_ranges
        assert "MSFT" in monitor._dynamic_price_ranges
        monitor.invalidate_dynamic_price_range()
        assert monitor._dynamic_price_ranges == {}


class TestCanaryMonitorDailyStats:
    def setup_method(self):
        self.monitor = CanaryMonitor()

    @pytest.mark.asyncio
    async def test_update_daily_stats_no_db(self):
        """Sans session factory, retourne quand même les stats calculées."""
        from schemas.monitoring import CanaryCheckResult

        results = [
            CanaryCheckResult(
                provider="A",
                ticker="AAPL",
                field="pe_ratio",
                status=HealthStatus.ok,
                checked_at=datetime.now(timezone.utc),
            ),
            CanaryCheckResult(
                provider="A",
                ticker="AAPL",
                field="price",
                status=HealthStatus.out_of_range,
                checked_at=datetime.now(timezone.utc),
            ),
        ]
        stats = await self.monitor._update_daily_stats("A", results)
        assert stats["checks_total"] == 2
        assert stats["checks_ok"] == 1
        assert stats["success_rate"] == 0.5
        assert stats["canary_passed"] is False

    @pytest.mark.asyncio
    async def test_daily_stats_are_persisted_through_repository_port(self):
        repository = MagicMock()
        repository.upsert_daily_stats = AsyncMock()
        monitor = CanaryMonitor(repository=repository)
        results = [
            CanaryCheckResult(
                provider="A",
                ticker="AAPL",
                field="price",
                status=HealthStatus.ok,
                checked_at=datetime.now(timezone.utc),
            )
        ]

        stats = await monitor._update_daily_stats("A", results)

        repository.upsert_daily_stats.assert_awaited_once_with("A", date.today(), stats)


class TestCanaryMonitorPersistencePorts:
    @pytest.mark.asyncio
    async def test_alert_policy_sends_candidates_to_repository(self):
        repository = MagicMock()
        repository.create_alert_if_absent = AsyncMock(side_effect=[11, 12])
        repository.resolve_alerts = AsyncMock()
        monitor = CanaryMonitor(repository=repository)
        failure = CanaryCheckResult(
            provider="A",
            ticker="AAPL",
            field="price",
            value_received=Decimal("1"),
            expected_min=Decimal("100"),
            expected_max=Decimal("200"),
            status=HealthStatus.out_of_range,
            checked_at=datetime.now(timezone.utc),
        )
        stats = {
            "checks_total": 1,
            "checks_ok": 0,
            "checks_outlier": 0,
            "checks_null": 0,
            "checks_timeout": 0,
            "success_rate": 0.0,
            "canary_passed": False,
            "is_healthy": False,
        }

        created = await monitor._evaluate_and_create_alerts("A", [failure], stats)

        assert created == [11, 12]
        candidates = [call.args[1] for call in repository.create_alert_if_absent.await_args_list]
        assert [candidate.alert_type for candidate in candidates] == [
            "canary_failed",
            "high_outlier_rate",
        ]
        repository.resolve_alerts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_canary_resolves_failure_alerts(self):
        repository = MagicMock()
        repository.create_alert_if_absent = AsyncMock()
        repository.resolve_alerts = AsyncMock()
        monitor = CanaryMonitor(repository=repository)
        result = CanaryCheckResult(
            provider="A",
            ticker="AAPL",
            field="price",
            status=HealthStatus.ok,
            checked_at=datetime.now(timezone.utc),
        )
        stats = {
            "checks_total": 1,
            "checks_ok": 1,
            "checks_outlier": 0,
            "checks_null": 0,
            "checks_timeout": 0,
            "success_rate": 1.0,
            "canary_passed": True,
            "is_healthy": True,
        }

        assert await monitor._evaluate_and_create_alerts("A", [result], stats) == []
        repository.resolve_alerts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_canary_logs_are_sent_through_repository_port(self):
        repository = MagicMock()
        repository.save_canary_results = AsyncMock()
        monitor = CanaryMonitor(repository=repository)
        result = CanaryCheckResult(
            provider="A",
            ticker="AAPL",
            field="price",
            status=HealthStatus.ok,
            checked_at=datetime.now(timezone.utc),
        )

        await monitor._log_canary_results([result])

        repository.save_canary_results.assert_awaited_once_with([result])


class TestCanaryMonitorRedis:
    @pytest.mark.asyncio
    async def test_update_redis_health_status(self):
        """Vérifie que le résumé est écrit dans Redis."""
        import fakeredis.aioredis

        redis_client = fakeredis.aioredis.FakeRedis()
        monitor = CanaryMonitor(redis_client=redis_client)

        from schemas.monitoring import CanaryCheckResult

        all_results = {
            "ProviderA": [
                CanaryCheckResult(
                    provider="ProviderA",
                    ticker="AAPL",
                    field="pe_ratio",
                    status=HealthStatus.ok,
                    checked_at=datetime.now(timezone.utc),
                ),
            ],
        }
        await monitor._update_redis_health_status(all_results)

        cached = await redis_client.get("provider:health:summary")
        assert cached is not None
        data = json.loads(cached)
        assert "ProviderA" in data["providers"]
        assert data["providers"]["ProviderA"]["status"] == "ok"

        await redis_client.aclose()


# ── Schemas Tests ─────────────────────────────────────────────────────────────


class TestMonitoringSchemas:
    def test_provider_status_creation(self):
        ps = ProviderStatus(
            name="Test",
            is_healthy=True,
            status_label="OK",
        )
        assert ps.name == "Test"
        assert ps.active_alerts == 0

    def test_health_summary(self):
        summary = ProviderHealthSummary(
            checked_at=datetime.now(timezone.utc),
            total_providers=3,
            healthy=2,
            degraded=1,
            down=0,
            providers=[],
        )
        assert summary.total_providers == 3

    def test_daily_stat_schema(self):
        ds = DailyStatSchema(
            date=date.today(),
            checks_total=10,
            checks_ok=8,
            checks_outlier=1,
            checks_null=1,
            success_rate=0.8,
        )
        assert ds.checks_total == 10

    def test_health_stats_response(self):
        hsr = HealthStatsResponse(
            period="last_7_days",
            total_values_validated=1000,
            total_valid=900,
            total_outliers=50,
            total_out_of_range=30,
            total_nulls=20,
            overall_quality_score=0.9,
        )
        assert hsr.overall_quality_score == 0.9


# ── Router Tests (via TestClient) ────────────────────────────────────────────


class TestMonitoringRouter:
    """Tests basiques des endpoints (sans DB réelle)."""

    def setup_method(self):
        from fastapi import FastAPI

        from routers.monitoring import router

        self.app = FastAPI()
        self.app.include_router(router)

        # Configure with mocks
        self.mock_canary = MagicMock()
        self.mock_canary.run_all = AsyncMock(return_value={})
        self.mock_canary.run_provider = AsyncMock(return_value=[])

        self.app.state.canary_monitor = self.mock_canary
        self.app.state.async_session_factory = None
        self.app.state.redis_client = None
        self.client = TestClient(self.app)

    def test_health_providers_no_config(self):
        """Sans session factory ni Redis → 503."""
        resp = self.client.get("/health/providers")
        assert resp.status_code == 503

    def test_health_alerts_no_config(self):
        resp = self.client.get("/health/alerts")
        assert resp.status_code == 503

    def test_canary_run_endpoint(self):
        resp = self.client.post("/health/canary/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["provider"] == "all"

    def test_canary_run_specific_provider(self):
        resp = self.client.post("/health/canary/run?provider_name=ZoneBourse")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "ZoneBourse"

    def test_health_stats_no_config(self):
        resp = self.client.get("/health/stats")
        assert resp.status_code == 503


class TestMonitoringRouterWithRedis:
    """Tests des endpoints avec Redis factice."""

    def setup_method(self):
        from fastapi import FastAPI

        from routers.monitoring import router

        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.canary_monitor = None
        self.app.state.async_session_factory = None
        self.app.state.redis_client = None

    @pytest.mark.asyncio
    async def test_health_providers_from_redis(self):
        import fakeredis.aioredis

        redis_client = fakeredis.aioredis.FakeRedis()

        # Seed Redis
        payload = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "providers": {
                "ZoneBourse": {"status": "ok", "success_rate": 0.95},
                "Boursorama": {"status": "degraded", "success_rate": 0.78},
            },
        }
        await redis_client.set(
            "provider:health:summary",
            json.dumps(payload, default=str),
            ex=3600,
        )

        self.app.state.redis_client = redis_client

        # Use httpx for async requests
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/health/providers")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_providers"] == 2
            assert data["healthy"] == 1
            assert data["degraded"] == 1

        await redis_client.aclose()
