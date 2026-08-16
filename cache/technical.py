"""Redis adapter for technical-indicator result caching."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from redis.exceptions import RedisError

from technical.contracts import AsyncRedisPort, CachePayload

logger = logging.getLogger(__name__)


class RedisTechnicalCache:
    """Serialize technical results and absorb expected Redis failures."""

    def __init__(self, redis_client: AsyncRedisPort | None = None) -> None:
        self._redis = redis_client

    async def get(self, key: str) -> CachePayload | None:
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(key)
            decoded = json.loads(raw) if raw else None
            return decoded if isinstance(decoded, dict) else None
        except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as exc:
            logger.warning("[RedisTechnicalCache] Redis read failed: %s", exc)
            return None

    async def set(self, key: str, data: CachePayload, ttl: int) -> None:
        if not self._redis:
            return
        try:
            await self._redis.setex(
                key,
                ttl,
                json.dumps(data, default=self._serialize),
            )
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning("[RedisTechnicalCache] Redis write failed: %s", exc)

    @staticmethod
    def _serialize(value: object) -> str | NoReturn:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        raise TypeError(f"Type {type(value)} not serializable")
