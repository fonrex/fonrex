"""Tests for the shared sync/async execution boundary."""

import contextvars
import threading

import pytest
from project_sources import relative, scope

from concurrency import run_sync

# Modules allowed to call asyncio.to_thread or run_in_executor themselves.
# A ratchet (tests/test_guard_ratchets.py): this set may shrink, never grow.
ALLOWED_THREAD_MODULES = {"concurrency.py", "realtime/worker.py"}


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
    violations = []
    for candidate in scope("service_code"):
        if relative(candidate) in ALLOWED_THREAD_MODULES:
            continue
        source = candidate.read_text(encoding="utf-8")
        if "asyncio.to_thread" in source or "run_in_executor" in source:
            violations.append(relative(candidate))
    assert violations == []
