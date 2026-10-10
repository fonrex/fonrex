"""Read macro-economic series through one cache: Redis, then the database, then the source.

FRED and the ECB publish rates the same way for Fonrex: one observation per day,
read rarely, kept in ``macro_rates_cache`` and in Redis. :class:`CachedRateService`
holds that logic once; a source only says how to read its latest observation
(:meth:`CachedRateService._fetch_series`) and whether it can be asked.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from concurrency import run_sync
from models import MacroRateCache
from schemas.macro import RATE_UNIT, MacroRate

logger = logging.getLogger(__name__)

# Rows stored before rates were labelled "ratio" say "percent" while holding a ratio.
_LEGACY_UNITS = {"percent": RATE_UNIT}


def _unit(unit: str | None) -> str:
    return _LEGACY_UNITS.get(unit or RATE_UNIT, unit or RATE_UNIT)


class CachedRateService:
    """Base of the macro sources: Redis, then the stored value when recent, then the source.

    A stored value that is older is only what is left when the source cannot be
    asked or does not answer; it is then reported ``stale`` and not put in Redis,
    so that the source is asked again at the next request.
    """

    #: Short name of the source: first segment of the Redis keys, ``source`` column.
    source = "macro"

    def __init__(self, db_service, redis_client=None):
        self.db_service = db_service
        self.redis_client = redis_client
        # TTL for Redis cache (default to 6 hours)
        ttl_str = os.environ.get("MACRO_RATES_CACHE_TTL", "21600")
        try:
            self.redis_ttl = int(ttl_str)
        except ValueError:
            self.redis_ttl = 21600

    # ── What a source provides ────────────────────────────────────────────────
    def _can_fetch(self) -> bool:
        """Whether the source can be asked at all (an API key, for instance)."""
        return True

    def _cannot_fetch_reason(self, series_id: str) -> str:
        return f"{self.source}: {series_id} cannot be read from the source"

    async def _fetch_series(self, series_id: str, label: str) -> MacroRate | None:
        """Read the latest observation from the source; ``None`` when it does not answer."""
        raise NotImplementedError

    # ── The cache ─────────────────────────────────────────────────────────────
    async def _get_series(self, series_id: str, label: str) -> MacroRate | None:
        """Fetch series from Redis, then PostgreSQL, then the source."""
        redis_key = f"macro:{self.source}:{series_id}"
        if self.redis_client:
            try:
                cached_data = await self.redis_client.get(redis_key)
                if cached_data:
                    data = json.loads(cached_data)
                    return MacroRate(
                        **{**data, "unit": _unit(data.get("unit")), "freshness": "cached"}
                    )
            except Exception as e:
                logger.warning("Erreur lecture cache Redis %s: %s", redis_key, e)

        # The value stored in PostgreSQL, when it was read from the source recently.
        stored, fetched_at = await run_sync(self._get_latest_from_db, series_id)
        rate = (
            stored.model_copy(update={"freshness": "cached"})
            if stored and self._is_recent(fetched_at)
            else None
        )

        # Otherwise the source. The stored value is what is left when the source
        # cannot be asked or does not answer.
        if rate is None:
            if self._can_fetch():
                rate = await self._fetch_series(series_id, label)
                if rate is not None:
                    rate = rate.model_copy(update={"freshness": "live"})
                    await run_sync(self._upsert_rate, rate)
            else:
                logger.warning(self._cannot_fetch_reason(series_id))
            if rate is None and stored is not None:
                rate = stored.model_copy(update={"freshness": "stale"})

        if rate and rate.freshness != "stale" and self.redis_client:
            try:
                await self.redis_client.setex(redis_key, self.redis_ttl, rate.model_dump_json())
            except Exception as e:
                logger.warning("Erreur écriture cache Redis %s: %s", redis_key, e)

        return rate

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        """Whether a stored value was read from the source less than one cache lifetime ago."""
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < timedelta(seconds=self.redis_ttl)

    def _upsert_rate(self, rate: MacroRate) -> None:
        """Save the fetched rate to PostgreSQL."""
        session = self.db_service.get_session()
        try:
            existing = (
                session.query(MacroRateCache)
                .filter_by(series_id=rate.series_id, observation_date=rate.observation_date)
                .first()
            )
            if existing:
                # Same observation (published on business days): the value may
                # have been revised, and the row was confirmed just now.
                existing.value = rate.value
                existing.source = self.source
                existing.fetched_at = datetime.now(timezone.utc)
            else:
                session.add(
                    MacroRateCache(
                        series_id=rate.series_id,
                        source=self.source,
                        label=rate.label,
                        value=rate.value,
                        unit=rate.unit,
                        observation_date=rate.observation_date,
                        fetched_at=datetime.now(timezone.utc),
                    )
                )
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error("Erreur upsert MacroRateCache: %s", e)
        finally:
            session.close()

    def _get_latest_from_db(self, series_id: str) -> tuple[MacroRate | None, datetime | None]:
        """The most recent value stored for a series, and when it was read from the source."""
        session = self.db_service.get_session()
        try:
            latest = (
                session.query(MacroRateCache)
                .filter_by(series_id=series_id)
                .order_by(MacroRateCache.observation_date.desc())
                .first()
            )
            if latest:
                return (
                    MacroRate(
                        series_id=latest.series_id,
                        label=latest.label,
                        value=latest.value,
                        unit=_unit(latest.unit),
                        observation_date=latest.observation_date,
                    ),
                    latest.fetched_at,
                )
            return None, None
        except Exception as e:
            logger.error("Erreur lecture MacroRateCache DB: %s", e)
            return None, None
        finally:
            session.close()
