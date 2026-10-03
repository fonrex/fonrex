"""Usage log: recorded after the response, written by batches, purged with age.

The HTTP middleware hands each request to :class:`UsageRecorder` and returns the
response at once. A background task writes the pending entries to the database
every few seconds and deletes, once a day, the rows older than the retention
period. A response therefore never waits for the database.

The log is a local usage journal of a self-hosted instance. By default it does
not keep the caller's IP address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Optional

from concurrency import run_sync
from settings import env_choice, env_int

logger = logging.getLogger(__name__)

# What is kept of the caller's IP address (USAGE_LOG_IP).
IP_MODES = ("none", "truncated", "full")
DEFAULT_IP_MODE = "none"
# Days of usage log kept (USAGE_LOG_RETENTION_DAYS); 0 keeps everything.
DEFAULT_RETENTION_DAYS = 90

FLUSH_INTERVAL_SECONDS = 5.0
PURGE_INTERVAL_SECONDS = 24 * 3600.0
MAX_PENDING_ENTRIES = 10_000
MAX_USER_AGENT_LENGTH = 256

# Requests that are not worth a row: probes, static files and the API documentation.
EXCLUDED_PATHS = frozenset(
    {"/health", "/health/", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
)
EXCLUDED_PREFIXES = ("/static",)


def ip_mode() -> str:
    return env_choice("USAGE_LOG_IP", DEFAULT_IP_MODE, IP_MODES)


def retention_days() -> int:
    return env_int("USAGE_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, minimum=0)


def is_logged_path(path: str) -> bool:
    """Tell whether a request path gets a row in the usage log."""
    return path not in EXCLUDED_PATHS and not path.startswith(EXCLUDED_PREFIXES)


def stored_ip(address: Optional[str], mode: Optional[str] = None) -> Optional[str]:
    """Return what the usage log keeps of an IP address.

    - ``none``: nothing;
    - ``truncated``: the network only (IPv4 /24, IPv6 /48), not the host;
    - ``full``: the address as received.
    """
    mode = mode or ip_mode()
    if not address or mode == "none":
        return None
    if mode == "full":
        return address
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    prefix = 24 if parsed.version == 4 else 48
    return str(ipaddress.ip_network(f"{parsed}/{prefix}", strict=False).network_address)


def stored_user_agent(user_agent: Optional[str]) -> Optional[str]:
    return user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else user_agent


class UsageRecorder:
    """Queue of usage entries, written to the database by a background task.

    ``get_database`` returns the database service to write to, or ``None`` when
    the database is unavailable: the pending entries are then dropped, as a
    usage log must never get in the way of the API.
    """

    def __init__(
        self,
        get_database: Callable[[], Any],
        *,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        max_pending: int = MAX_PENDING_ENTRIES,
    ) -> None:
        self._get_database = get_database
        self._flush_interval = flush_interval
        self._pending: deque[dict] = deque(maxlen=max_pending)
        self._task: Optional[asyncio.Task] = None
        self._next_purge = 0.0
        self.dropped = 0

    @property
    def pending(self) -> list[dict]:
        """Entries recorded and not yet written (most recent last)."""
        return list(self._pending)

    def record(self, entry: dict) -> None:
        """Queue an entry; never blocks and never raises."""
        if len(self._pending) == self._pending.maxlen:
            # The oldest entry is pushed out by the deque.
            self.dropped += 1
        self._pending.append(entry)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-recorder")

    async def stop(self) -> None:
        """Stop the background task and write what is still pending."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()
            await self.purge_if_due()

    async def _shielded(self, coro) -> Any:
        """Run a database operation that a cancellation does not interrupt.

        When the loop is cancelled (shutdown), the operation is awaited to its end,
        then the cancellation goes on. An error of the operation at that moment is
        logged, never raised: it would replace the cancellation, and the loop would
        carry on while ``stop()`` waits for it.
        """
        task = asyncio.create_task(coro)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception as exc:
                logger.warning("Usage log: operation interrupted by shutdown failed: %s", exc)
            raise

    async def flush(self) -> int:
        """Write the pending entries; return how many were written."""
        if not self._pending:
            return 0
        batch = list(self._pending)
        self._pending.clear()
        try:
            return await self._shielded(run_sync(self._write, batch))
        except Exception as exc:  # the usage log must never break the API
            logger.warning("Usage log: %d entries not written: %s", len(batch), exc)
            return 0

    def _write(self, batch: list[dict]) -> int:
        database = self._get_database()
        if database is None:
            return 0
        database.log_usage_batch(batch)
        return len(batch)

    async def purge_if_due(self, now: Optional[float] = None) -> Optional[int]:
        """Delete expired rows, at most once a day. Return the number deleted."""
        now = time.monotonic() if now is None else now
        if now < self._next_purge:
            return None
        self._next_purge = now + PURGE_INTERVAL_SECONDS
        days = retention_days()
        if days <= 0:
            return None
        try:
            deleted = await self._shielded(run_sync(self._purge, days))
        except Exception as exc:
            logger.warning("Usage log: purge failed: %s", exc)
            return None
        if deleted:
            logger.info("Usage log: %d rows older than %d days deleted", deleted, days)
        return deleted

    def _purge(self, days: int) -> int:
        database = self._get_database()
        if database is None:
            return 0
        return int(database.purge_usage_logs(days) or 0)
