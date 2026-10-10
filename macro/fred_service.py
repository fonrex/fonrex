"""Macro-economic rates from FRED (St. Louis Fed): the US risk-free rate."""

import logging
import os
from datetime import datetime
from decimal import Decimal

import httpx

from macro.rate_cache import CachedRateService
from schemas.dcf import DEFAULT_RISK_FREE_RATE
from schemas.macro import RATE_UNIT, MacroRate, MacroRatesResponse, RiskFreeRate

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class FREDService(CachedRateService):
    """Service to fetch and cache macro-economic rates from FRED (St. Louis Fed)."""

    source = "fred"

    def __init__(self, db_service, redis_client=None):
        super().__init__(db_service, redis_client)
        self.api_key = os.environ.get("FRED_API_KEY")

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

    def _can_fetch(self) -> bool:
        return bool(self.api_key)

    def _cannot_fetch_reason(self, series_id: str) -> str:
        return f"FRED_API_KEY manquante, impossible de mettre à jour {series_id} depuis l'API."

    async def _fetch_series(self, series_id: str, label: str) -> MacroRate | None:
        return await self._fetch_fred_series(series_id, label)

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

                return rate
        except Exception as e:
            logger.error("Erreur API FRED pour %s: %s", series_id, e)
            return None
