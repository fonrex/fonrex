"""Tests for the shared sync/async execution boundary."""

import contextvars
import threading
from pathlib import Path

import pytest

from concurrency import run_sync


@pytest.mark.asyncio
async def test_run_sync_uses_worker_thread_and_propagates_context():
    request_id = contextvars.ContextVar("request_id")
    request_id.set("request-42")
    event_loop_thread = threading.get_ident()

    worker_thread, propagated_value = await run_sync(
        lambda: (threading.get_ident(), request_id.get())
    )

    assert worker_thread != event_loop_thread
    assert propagated_value == "request-42"


@pytest.mark.asyncio
async def test_run_sync_propagates_operation_errors():
    def fail():
        raise ValueError("blocking failure")

    with pytest.raises(ValueError, match="blocking failure"):
        await run_sync(fail)


def test_application_code_uses_the_shared_async_boundary():
    root = Path(__file__).parents[1]
    scanned_roots = (
        "main.py",
        "routers",
        "use_cases",
        "financials",
        "historical",
        "realtime",
        "technical",
        "valuation",
        "news",
    )
    allowed = {
        root / "concurrency.py",
        root / "realtime" / "worker.py",
    }
    violations = []
    for relative in scanned_roots:
        path = root / relative
        candidates = [path] if path.is_file() else path.rglob("*.py")
        for candidate in candidates:
            if candidate in allowed:
                continue
            source = candidate.read_text()
            if "asyncio.to_thread" in source or "run_in_executor" in source:
                violations.append(candidate.relative_to(root).as_posix())
    assert violations == []
