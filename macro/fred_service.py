import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from concurrency import run_sync
from models import MacroRateCache
from schemas.dcf import DEFAULT_RISK_FREE_RATE
from schemas.macro import RATE_UNIT, MacroRate, MacroRatesResponse, RiskFreeRate

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class FREDService:
    """Service to fetch and cache macro-economic rates from FRED (St. Louis Fed)."""

    def __init__(self, db_service, redis_client=None):
        self.db_service = db_service
        self.redis_client = redis_client
        self.api_key = os.environ.get("FRED_API_KEY")
        
        # TTL for Redis cache (default to 6 hours)
        ttl_str = os.environ.get("MACRO_RATES_CACHE_TTL", "21600")
        try:
            self.redis_ttl = int(ttl_str)
        except ValueError:
            self.redis_ttl = 21600

    async def get_current_rates(self) -> MacroRatesResponse:
        """Returns the current macro rates (mainly risk-free rate) from cache or FRED."""
        risk_free = await self._get_series("DGS10", "10-Year Treasury Constant Maturity Rate")
        return MacroRatesResponse(risk_free_rate=risk_free)

    async def get_risk_free_rate(self) -> RiskFreeRate:
        """The risk-free rate for DCFService, and where it comes from.

        A rate read from FRED is used whatever its sign: a zero or negative rate is
        a rate. Without any (no key and nothing stored, or no answer), the rate of
        ``DCF_RISK_FREE_RATE`` is used.
        """
        rates = await self.get_current_rates()
        rate = rates.risk_free_rate
        if rate is not None:
            return RiskFreeRate(
                value=rate.value,
                source=f"fred_{rate.freshness or 'cached'}",
                observation_date=rate.observation_date,
            )
        return RiskFreeRate(value=DEFAULT_RISK_FREE_RATE, source="env_fallback")

    async def _get_series(self, series_id: str, label: str) -> MacroRate | None:
        """Fetch series from Redis, then PostgreSQL, then FRED."""
        # 1. Try Redis
        redis_key = f"macro:fred:{series_id}"
        if self.redis_client:
            try:
                cached_data = await self.redis_client.get(redis_key)
                if cached_data:
                    data = json.loads(cached_data)
                    return MacroRate(**{**data, "unit": RATE_UNIT, "freshness": "cached"})
            except Exception as e:
                logger.warning("Erreur lecture cache Redis %s: %s", redis_key, e)

        # 2. The value stored in PostgreSQL, when it was read from FRED recently.
        stored, fetched_at = await run_sync(self._get_latest_from_db, series_id)
        rate = stored.model_copy(update={"freshness": "cached"}) if (
            stored and self._is_recent(fetched_at)
        ) else None

        # 3. Otherwise FRED. The stored value is what is left when FRED cannot be
        # asked or does not answer — it used to be returned first, whatever its
        # age: once a rate was stored, FRED was never asked again.
        if rate is None:
            if self.api_key:
                rate = await self._fetch_fred_series(series_id, label)
            else:
                logger.warning(
                    "FRED_API_KEY manquante, impossible de mettre à jour %s depuis l'API.",
                    series_id,
                )
            if rate is None and stored is not None:
                rate = stored.model_copy(update={"freshness": "stale"})

        # Cache in Redis for the next requests. A stale value is not cached: FRED
        # is asked again at the next request.
        if rate and rate.freshness != "stale" and self.redis_client:
            try:
                await self.redis_client.setex(
                    redis_key, 
                    self.redis_ttl, 
                    rate.model_dump_json()
                )
            except Exception as e:
                logger.warning("Erreur écriture cache Redis %s: %s", redis_key, e)

        return rate

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        """Whether a stored value was read from FRED less than one cache lifetime ago."""
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < timedelta(seconds=self.redis_ttl)

    async def _fetch_fred_series(self, series_id: str, label: str) -> MacroRate | None:
        """Call FRED API for the latest observation."""
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(FRED_BASE_URL, params=params)
                response.raise_for_status()
                data = response.json()
                
                observations = data.get("observations", [])
                if not observations:
                    return None
                    
                obs = observations[0]
                val_str = obs.get("value")
                
                # FRED returns "." for missing data on holidays
                if val_str == "." or val_str is None:
                    # Request more points to find the last valid one
                    params["limit"] = 5
                    response = await client.get(FRED_BASE_URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                    for o in data.get("observations", []):
                        if o.get("value") != ".":
                            obs = o
                            val_str = o.get("value")
                            break
                            
                if val_str == "." or val_str is None:
                    return None
                    
                val_dec = Decimal(val_str)
                # FRED rates are often in percentage (e.g. "4.15" means 4.15%)
                # Convert to decimal 0.0415
                val_dec = val_dec / Decimal("100")
                
                obs_date = datetime.strptime(obs.get("date"), "%Y-%m-%d").date()
                
                rate = MacroRate(
                    series_id=series_id,
                    label=label,
                    value=val_dec,
                    unit=RATE_UNIT,
                    observation_date=obs_date,
                    freshness="live",
                )
                
                # Save to DB
                await run_sync(self._upsert_rate, rate)
                
                return rate
        except Exception as e:
            logger.error("Erreur API FRED pour %s: %s", series_id, e)
            return None

    def _upsert_rate(self, rate: MacroRate) -> None:
        """Save the fetched rate to PostgreSQL."""
        session = self.db_service.get_session()
        try:
            # Check if exists
            existing = session.query(MacroRateCache).filter_by(
                series_id=rate.series_id,
                observation_date=rate.observation_date
            ).first()
            
            if existing:
                # Same observation (FRED publishes on business days): the value
                # may have been revised, and the row was confirmed just now.
                existing.value = rate.value
                existing.fetched_at = datetime.now(timezone.utc)
                session.commit()
            else:
                obj = MacroRateCache(
                    series_id=rate.series_id,
                    label=rate.label,
                    value=rate.value,
                    unit=rate.unit,
                    observation_date=rate.observation_date,
                    fetched_at=datetime.now(timezone.utc),
                )
                session.add(obj)
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error("Erreur upsert MacroRateCache: %s", e)
        finally:
            session.close()

    def _get_latest_from_db(self, series_id: str) -> tuple[MacroRate | None, datetime | None]:
        """The most recent value stored for a series, and when it was read from FRED."""
        session = self.db_service.get_session()
        try:
            latest = session.query(MacroRateCache).filter_by(
                series_id=series_id
            ).order_by(MacroRateCache.observation_date.desc()).first()
            
            if latest:
                return (
                    MacroRate(
                        series_id=latest.series_id,
                        label=latest.label,
                        value=latest.value,
                        # Rows stored before say "percent": the value is a ratio all the same.
                        unit=RATE_UNIT,
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
