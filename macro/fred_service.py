"""Macro-economic rates from FRED (St. Louis Fed): the US risk-free rate.

FRED republishes the 10-year rate of the US Treasury (``DGS10``) and needs an API
key. The Treasury publishes the same rate in its daily par yield curve, without
key (``macro/treasury_curves.py``): when FRED gives no rate, or only an older
stored one, the 10-year rate of the Treasury curve is used, if it is as recent.
"""

import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from macro.rate_cache import CachedRateService
from schemas.dcf import DEFAULT_RISK_FREE_RATE
from schemas.macro import RATE_UNIT, MacroRate, MacroRatesResponse, RiskFreeRate

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
NOMINAL = "nominal"
TEN_YEARS = "10Y"
TREASURY_SERIES = "treasury:nominal:10Y"
# A curve read now is live, one stored and up to date is cached, one the Treasury
# could not refresh is stale: the same words as for FRED.
_TREASURY_FRESHNESS = {"fetched": "live", "fresh": "cached", "failed": "stale"}


class FREDService(CachedRateService):
    """Service to fetch and cache macro-economic rates from FRED (St. Louis Fed)."""

    source = "fred"
    currency = "USD"

    def __init__(self, db_service, redis_client=None, treasury=None):
        super().__init__(db_service, redis_client)
        self.api_key = os.environ.get("FRED_API_KEY")
        # The yield curves of the Treasury (``TreasuryCurves``): the 10-year rate without key.
        self.treasury = treasury

    async def get_current_rates(self) -> MacroRatesResponse:
        """The 10-year rate of FRED, or of the Treasury curve when FRED gives none or an older one."""
        risk_free = await self._get_series("DGS10", "10-Year Treasury Constant Maturity Rate")
        if risk_free is None or risk_free.freshness == "stale":
            treasury = await self._treasury_ten_years()
            if treasury is not None and (
                risk_free is None or treasury.observation_date >= risk_free.observation_date
            ):
                risk_free = treasury
        return MacroRatesResponse(
            currency=self.currency, risk_free_rate=risk_free, rates=[risk_free] if risk_free else []
        )

    async def _treasury_ten_years(self) -> MacroRate | None:
        """The 10-year rate of the latest Treasury curve stored, read first when needed."""
        if self.treasury is None:
            return None
        try:
            load = await self.treasury.ensure(NOMINAL, self.treasury.today() - timedelta(days=10))
            day = await self.treasury.curve_on(NOMINAL)
        except Exception as error:  # the Treasury is a fallback: it never breaks the answer
            logger.warning("Treasury 10-year rate unavailable: %s", error)
            return None
        if day is None or TEN_YEARS not in day.points:
            return None
        return MacroRate(
            series_id=TREASURY_SERIES,
            label="10-Year Treasury Par Yield (US Treasury)",
            value=day.points[TEN_YEARS],
            unit=RATE_UNIT,
            observation_date=day.day,
            freshness=_TREASURY_FRESHNESS[load.status],
            source="treasury",
            currency=self.currency,
        )

    async def get_rates(self) -> MacroRatesResponse:
        """Every FRED series Fonrex reads (the 10-year Treasury rate), for GET /macro/rates."""
        return await self.get_current_rates()

    async def get_risk_free_rate(self) -> RiskFreeRate:
        """The risk-free rate for DCFService, and where it comes from.

        A rate is used whatever its sign: a zero or negative rate is a rate. The
        source says who gave it (``fred_live``, ``treasury_cached``...). Without any
        (no FRED key and no Treasury curve, or no answer), the rate of
        ``DCF_RISK_FREE_RATE`` is used.
        """
        rates = await self.get_current_rates()
        rate = rates.risk_free_rate
        if rate is not None:
            return RiskFreeRate(
                value=rate.value,
                source=f"{rate.source or self.source}_{rate.freshness or 'cached'}",
                observation_date=rate.observation_date,
            )
        return RiskFreeRate(value=DEFAULT_RISK_FREE_RATE, source="env_fallback")

    def _can_fetch(self) -> bool:
        return bool(self.api_key)

    def _cannot_fetch_reason(self, series_id: str) -> str:
        return f"FRED_API_KEY is not set: {series_id} cannot be read from the FRED API."

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
            logger.error("FRED API error for %s: %s", series_id, e)
            return None
