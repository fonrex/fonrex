"""Dynamic canary price ranges backed by historical closing prices."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from monitoring.ports import CanaryMonitoringRepository, MonitoringRepositoryError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DynamicPriceRangeCacheEntry:
    bounds: tuple[float, float] | None
    expires_at: float


class DynamicPriceRangeResolver:
    """Calculate and cache statistically plausible canary price ranges."""

    def __init__(
        self,
        repository: CanaryMonitoringRepository | None,
        *,
        ttl_seconds: int,
        negative_ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._ttl_seconds = max(0, ttl_seconds)
        self._negative_ttl_seconds = max(0, negative_ttl_seconds)
        self._clock = clock
        self.cache: dict[str, DynamicPriceRangeCacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, ticker: str) -> tuple[float, float] | None:
        cache_hit, bounds = self.cached(ticker)
        if cache_hit:
            return bounds
        if self._repository is None:
            return None

        lock = self._locks.setdefault(ticker, asyncio.Lock())
        async with lock:
            cache_hit, bounds = self.cached(ticker)
            if cache_hit:
                return bounds
            return await self._calculate(ticker)

    def cached(self, ticker: str) -> tuple[bool, tuple[float, float] | None]:
        entry = self.cache.get(ticker)
        if entry is None:
            return False, None
        if entry.expires_at <= self._clock():
            self.cache.pop(ticker, None)
            return False, None
        return True, entry.bounds

    def store(self, ticker: str, bounds: tuple[float, float] | None) -> None:
        ttl = self._ttl_seconds if bounds is not None else self._negative_ttl_seconds
        self.cache[ticker] = DynamicPriceRangeCacheEntry(
            bounds=bounds,
            expires_at=self._clock() + ttl,
        )

    def invalidate(self, ticker: str | None = None) -> None:
        if ticker is None:
            self.cache.clear()
            return
        self.cache.pop(ticker, None)

    async def _calculate(self, ticker: str) -> tuple[float, float] | None:
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=90)
            prices = await self._repository.get_recent_closing_prices(ticker, cutoff_date)
            if prices is None or len(prices) < 10:
                if prices is not None:
                    logger.debug(
                        "[CanaryMonitor] Not enough historical prices for %s (%d/10)",
                        ticker,
                        len(prices),
                    )
                self.store(ticker, None)
                return None

            mean = sum(prices) / len(prices)
            variance = sum((price - mean) ** 2 for price in prices) / (len(prices) - 1)
            std_dev = max(math.sqrt(variance), mean * 0.02)
            bounds = (max(mean - 3 * std_dev, 0.01), mean + 3 * std_dev)
            logger.info(
                "[CanaryMonitor] Dynamic range for %s: [%.2f, %.2f] over %d days",
                ticker,
                bounds[0],
                bounds[1],
                len(prices),
            )
            self.store(ticker, bounds)
            return bounds
        except MonitoringRepositoryError as exc:
            logger.warning("[CanaryMonitor] Dynamic price range failed for %s: %s", ticker, exc)
            self.store(ticker, None)
            return None
