"""Usage log: written after the response, without the caller's IP, purged with age."""

import asyncio
import logging
import tempfile
import threading
import time
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import usage_recorder
from database.service import DatabaseService
from database.usage import MAX_ENDPOINT_LENGTH
from main import app
from models import Base, UsageLog, naive_utc_now
from settings import env_choice
from usage_recorder import UsageRecorder, is_logged_path, stored_ip, stored_user_agent

USAGE_ENV_VARS = ("USAGE_LOG_IP", "USAGE_LOG_RETENTION_DAYS")


@pytest.fixture
def clean_usage_env(monkeypatch):
    for name in USAGE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def entry(endpoint: str = "/fundamental", **fields) -> dict:
    return {"endpoint": endpoint, "method": "GET", "status_code": 200, "latency_ms": 12, **fields}


class TestStoredIp:
    def test_nothing_is_kept_by_default(self, clean_usage_env):
        assert stored_ip("203.0.113.57") is None

    def test_truncated_keeps_the_network_only(self, clean_usage_env):
        clean_usage_env.setenv("USAGE_LOG_IP", "truncated")

        assert stored_ip("203.0.113.57") == "203.0.113.0"
        assert stored_ip("2001:db8:abcd:12:34:56:78:9a") == "2001:db8:abcd::"

    def test_full_keeps_the_address(self, clean_usage_env):
        clean_usage_env.setenv("USAGE_LOG_IP", "FULL")

        assert stored_ip("203.0.113.57") == "203.0.113.57"

    def test_unknown_mode_falls_back_to_nothing(self, clean_usage_env, caplog):
        clean_usage_env.setenv("USAGE_LOG_IP", "everything")

        with caplog.at_level(logging.WARNING, logger="settings"):
            assert stored_ip("203.0.113.57") is None
        assert "USAGE_LOG_IP" in caplog.text

    @pytest.mark.parametrize("address", [None, "", "testclient", "not-an-ip"])
    def test_missing_or_unparsable_address_is_not_stored_when_truncating(
        self, clean_usage_env, address
    ):
        assert stored_ip(address, "truncated") is None

    def test_user_agent_is_capped(self):
        assert stored_user_agent(None) is None
        assert len(stored_user_agent("x" * 1000)) == usage_recorder.MAX_USER_AGENT_LENGTH


class TestEnvChoice:
    def test_default_when_unset_or_empty(self, monkeypatch):
        monkeypatch.delenv("CHOICE_UNDER_TEST", raising=False)
        assert env_choice("CHOICE_UNDER_TEST", "none", ("none", "full")) == "none"
        monkeypatch.setenv("CHOICE_UNDER_TEST", " ")
        assert env_choice("CHOICE_UNDER_TEST", "none", ("none", "full")) == "none"

    def test_value_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CHOICE_UNDER_TEST", " Full ")
        assert env_choice("CHOICE_UNDER_TEST", "none", ("none", "full")) == "full"


class TestLoggedPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/health/",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/favicon.ico",
            "/static/logos/default.svg",
        ],
    )
    def test_probes_static_files_and_documentation_are_not_logged(self, path):
        assert is_logged_path(path) is False

    @pytest.mark.parametrize(
        "path", ["/fundamental", "/health/providers", "/widgets.json", "/dcf/AIR.PA", "/"]
    )
    def test_api_calls_are_logged(self, path):
        assert is_logged_path(path) is True


class TestRecorder:
    async def test_recording_does_not_touch_the_database(self):
        database = MagicMock()
        recorder = UsageRecorder(lambda: database)

        recorder.record(entry())

        assert recorder.pending == [entry()]
        database.log_usage_batch.assert_not_called()

    async def test_flush_writes_the_pending_entries_in_one_batch(self):
        database = MagicMock()
        recorder = UsageRecorder(lambda: database)
        recorder.record(entry("/a"))
        recorder.record(entry("/b"))

        assert await recorder.flush() == 2

        database.log_usage_batch.assert_called_once_with([entry("/a"), entry("/b")])
        assert recorder.pending == []

    async def test_flush_without_entries_does_nothing(self):
        database = MagicMock()

        assert await UsageRecorder(lambda: database).flush() == 0
        database.log_usage_batch.assert_not_called()

    async def test_entries_are_dropped_when_the_database_is_unavailable(self):
        recorder = UsageRecorder(lambda: None)
        recorder.record(entry())

        assert await recorder.flush() == 0
        assert recorder.pending == []

    async def test_a_failing_database_never_raises(self, caplog):
        database = MagicMock()
        database.log_usage_batch.side_effect = RuntimeError("connection lost")
        recorder = UsageRecorder(lambda: database)
        recorder.record(entry())

        with caplog.at_level(logging.WARNING, logger="usage_recorder"):
            assert await recorder.flush() == 0

        assert "1 entries not written" in caplog.text
        assert recorder.pending == []

    async def test_queue_is_bounded(self):
        recorder = UsageRecorder(lambda: None, max_pending=3)
        for index in range(5):
            recorder.record(entry(f"/{index}"))

        assert [item["endpoint"] for item in recorder.pending] == ["/2", "/3", "/4"]
        assert recorder.dropped == 2

    async def test_background_task_flushes_periodically(self):
        database = MagicMock()
        recorder = UsageRecorder(lambda: database, flush_interval=0.01)
        await recorder.start()
        recorder.record(entry())

        await asyncio.sleep(0.1)
        await recorder.stop()

        database.log_usage_batch.assert_called_with([entry()])

    async def test_stop_writes_what_is_still_pending(self):
        database = MagicMock()
        recorder = UsageRecorder(lambda: database, flush_interval=3600)
        await recorder.start()
        recorder.record(entry())

        await recorder.stop()

        database.log_usage_batch.assert_called_once_with([entry()])

    async def test_stop_without_start_is_safe(self):
        await UsageRecorder(lambda: None).stop()

    async def test_stop_waits_for_the_write_in_progress(self):
        """Cancelling the loop must not leave a database write running after stop()."""
        started = threading.Event()
        release = threading.Event()
        written = []

        def slow_write(batch):
            started.set()
            release.wait(timeout=5)
            written.extend(batch)

        database = MagicMock()
        database.log_usage_batch.side_effect = slow_write
        recorder = UsageRecorder(lambda: database, flush_interval=0.01)
        recorder.record(entry())
        await recorder.start()
        await asyncio.to_thread(started.wait, 5)

        stopping = asyncio.create_task(recorder.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()  # stop() waits for the batch being written

        release.set()
        await stopping
        assert written == [entry()]

    async def test_stop_ends_when_the_write_in_progress_fails(self, caplog):
        """A write failing during shutdown must not swallow the stop request.

        The error of the write replaced the cancellation: the background loop
        carried on and stop() waited for it forever.
        """
        started = threading.Event()
        release = threading.Event()

        def failing_write(batch):
            started.set()
            release.wait(timeout=5)
            raise RuntimeError("connection lost")

        database = MagicMock()
        database.log_usage_batch.side_effect = failing_write
        recorder = UsageRecorder(lambda: database, flush_interval=0.01)
        recorder.record(entry())
        await recorder.start()
        await asyncio.to_thread(started.wait, 5)

        stopping = asyncio.create_task(recorder.stop())
        await asyncio.sleep(0.05)
        release.set()

        with caplog.at_level(logging.WARNING, logger="usage_recorder"):
            # asyncio.wait does not cancel stop(): a stop() that hangs stays visible.
            await asyncio.wait({stopping}, timeout=2)

        assert stopping.done(), "stop() is still waiting for the background loop"
        assert "connection lost" in caplog.text


class TestPurge:
    async def test_purge_uses_the_default_retention(self, clean_usage_env):
        database = MagicMock()
        database.purge_usage_logs.return_value = 7
        recorder = UsageRecorder(lambda: database)

        assert await recorder.purge_if_due(now=1000.0) == 7
        database.purge_usage_logs.assert_called_once_with(90)

    async def test_purge_runs_at_most_once_a_day(self, clean_usage_env):
        database = MagicMock()
        database.purge_usage_logs.return_value = 0
        recorder = UsageRecorder(lambda: database)

        await recorder.purge_if_due(now=1000.0)
        assert await recorder.purge_if_due(now=1000.0 + 3600) is None
        await recorder.purge_if_due(now=1000.0 + usage_recorder.PURGE_INTERVAL_SECONDS)

        assert database.purge_usage_logs.call_count == 2

    async def test_retention_follows_the_environment(self, clean_usage_env):
        clean_usage_env.setenv("USAGE_LOG_RETENTION_DAYS", "30")
        database = MagicMock()
        database.purge_usage_logs.return_value = 0

        await UsageRecorder(lambda: database).purge_if_due(now=time.monotonic())

        database.purge_usage_logs.assert_called_once_with(30)

    async def test_zero_retention_keeps_everything(self, clean_usage_env):
        clean_usage_env.setenv("USAGE_LOG_RETENTION_DAYS", "0")
        database = MagicMock()

        assert await UsageRecorder(lambda: database).purge_if_due(now=1000.0) is None
        database.purge_usage_logs.assert_not_called()

    async def test_purge_without_database_or_on_error_never_raises(self, clean_usage_env, caplog):
        assert await UsageRecorder(lambda: None).purge_if_due(now=1000.0) == 0

        database = MagicMock()
        database.purge_usage_logs.side_effect = RuntimeError("locked")
        with caplog.at_level(logging.WARNING, logger="usage_recorder"):
            assert await UsageRecorder(lambda: database).purge_if_due(now=1000.0) is None
        assert "purge failed" in caplog.text


class TestRepository:
    @pytest.fixture
    def database(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            service = DatabaseService(f"sqlite:///{tmp.name}")
            Base.metadata.create_all(service.engine)
            yield service
            service.Session.remove()
            service.engine.dispose()

    def _rows(self, database):
        session = database.get_session()
        try:
            return session.query(UsageLog).order_by(UsageLog.id).all()
        finally:
            session.close()

    def test_batch_is_written_in_one_call(self, database):
        written = database.log_usage_batch(
            [
                entry("/fundamental", api_key_id="frx_live_sha256_abc", cache_hit=True),
                entry("/dcf/AIR.PA", status_code=404, ip_address=None, user_agent="curl/8"),
            ]
        )

        rows = self._rows(database)
        assert written == 2
        assert [row.endpoint for row in rows] == ["/fundamental", "/dcf/AIR.PA"]
        assert rows[0].cache_hit is True
        assert rows[1].status_code == 404
        assert rows[1].ip_address is None

    def test_unknown_fields_are_ignored_and_empty_batch_is_a_no_op(self, database):
        assert database.log_usage_batch([]) == 0
        assert database.log_usage_batch([entry(not_a_column="x")]) == 1
        assert len(self._rows(database)) == 1

    def test_a_long_path_is_capped_to_its_column(self, database):
        """PostgreSQL would reject the whole batch for one path longer than the column."""
        long_entry = entry("/fundamental/" + "x" * 500)
        original = dict(long_entry)

        assert database.log_usage_batch([long_entry, entry("/dcf/AIR.PA")]) == 2

        rows = self._rows(database)
        assert len(rows[0].endpoint) == MAX_ENDPOINT_LENGTH == UsageLog.endpoint.type.length
        assert rows[0].endpoint == original["endpoint"][:MAX_ENDPOINT_LENGTH]
        assert rows[1].endpoint == "/dcf/AIR.PA"
        assert long_entry == original  # the caller's entry is left untouched

    def test_purge_deletes_only_the_expired_rows(self, database):
        database.log_usage_batch([entry("/old"), entry("/recent")])
        session = database.get_session()
        try:
            old = session.query(UsageLog).filter(UsageLog.endpoint == "/old").one()
            old.created_at = naive_utc_now() - timedelta(days=120)
            session.commit()
        finally:
            session.close()

        assert database.purge_usage_logs(90) == 1
        assert [row.endpoint for row in self._rows(database)] == ["/recent"]

    def test_purge_with_zero_days_deletes_nothing(self, database):
        database.log_usage_batch([entry()])

        assert database.purge_usage_logs(0) == 0
        assert len(self._rows(database)) == 1


class TestMiddleware:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("FONREX_AUTH_REQUIRED", "false")
        for name in (
            "FONREX_API_KEY",
            "FONREX_RELAY_KEY",
            "FONREX_API_KEYS",
            "FONREX_READ_ONLY_API_KEYS",
        ):
            monkeypatch.delenv(name, raising=False)
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.setex = AsyncMock()
        mock_fred = MagicMock()
        mock_fred.get_current_rates = AsyncMock(return_value={"risk_free_rate": None})
        names = ("redis_client", "db_service", "db_available", "fred_service")
        originals = {name: getattr(app.state, name, None) for name in names}

        with TestClient(app) as test_client:
            app.state.redis_client = mock_redis
            app.state.db_service = MagicMock()
            app.state.db_available = True
            app.state.fred_service = mock_fred
            yield test_client

        for name, value in originals.items():
            setattr(app.state, name, value)

    def test_response_does_not_wait_for_the_database(self, client, clean_usage_env):
        response = client.get("/macro/rates", headers={"User-Agent": "unit-test"})

        assert response.status_code == 200
        # The entry is queued, the database has not been called yet.
        recorded = app.state.usage_recorder.pending[-1]
        assert recorded["endpoint"] == "/macro/rates"
        assert recorded["method"] == "GET"
        assert recorded["status_code"] == 200
        assert recorded["user_agent"] == "unit-test"
        app.state.db_service.log_usage_batch.assert_not_called()
        app.state.db_service.log_usage.assert_not_called()

    def test_ip_address_is_not_recorded_by_default(self, client, clean_usage_env):
        client.get("/macro/rates")

        assert app.state.usage_recorder.pending[-1]["ip_address"] is None

    def test_ip_address_is_recorded_on_request(self, client, clean_usage_env):
        clean_usage_env.setenv("USAGE_LOG_IP", "full")

        client.get("/macro/rates")

        assert app.state.usage_recorder.pending[-1]["ip_address"] == "testclient"

    @pytest.mark.parametrize("path", ["/health", "/openapi.json", "/static/logos/default.svg"])
    def test_probes_and_static_files_are_not_recorded(self, client, clean_usage_env, path):
        before = len(app.state.usage_recorder.pending)

        client.get(path)

        assert len(app.state.usage_recorder.pending) == before

    def test_pending_entries_are_written_at_shutdown(self, monkeypatch, clean_usage_env):
        monkeypatch.setenv("FONREX_AUTH_REQUIRED", "false")
        database = MagicMock()
        originals = {
            name: getattr(app.state, name, None) for name in ("db_service", "db_available")
        }

        with TestClient(app) as test_client:
            app.state.db_service = database
            app.state.db_available = True
            test_client.get("/widgets.json")
            database.log_usage_batch.assert_not_called()

        for name, value in originals.items():
            setattr(app.state, name, value)
        database.log_usage_batch.assert_called_once()
        written = database.log_usage_batch.call_args.args[0]
        assert [item["endpoint"] for item in written] == ["/widgets.json"]
