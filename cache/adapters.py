"""Failure-neutral cache adapters composed at transport boundaries."""

from __future__ import annotations

import logging

from redis.exceptions import RedisError

from use_cases.ports import AsyncJsonCachePort

logger = logging.getLogger(__name__)


class ResilientAsyncJsonCache(AsyncJsonCachePort):
    """Expose Redis as a best-effort application cache.

    Only Redis operational failures are absorbed. Programming and serialization
    errors still propagate to make defects visible during development.
    """

    def __init__(self, redis_client: object) -> None:
        self._redis = redis_client

    async def get(self, key: str) -> str | bytes | None:
        try:
            return await self._redis.get(key)
        except RedisError as exc:
            logger.warning("Fundamentals cache read failed for %s: %s", key, exc)
            return None

    async def setex(self, key: str, ttl: int, value: str) -> object:
        try:
            return await self._redis.setex(key, ttl, value)
        except RedisError as exc:
            logger.warning("Fundamentals cache write failed for %s: %s", key, exc)
            return None
