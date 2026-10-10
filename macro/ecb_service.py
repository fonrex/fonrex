"""Euro area rates from the European Central Bank (ECB Data Portal).

The ECB publishes its statistics through a free API without key:
``https://data-api.ecb.europa.eu/service/data/{flow}/{key}``. A series is read
in CSV (``format=csvdata``): one row per observation, ``TIME_PERIOD`` and
``OBS_VALUE`` among the columns. Rates are published in percent and stored as
ratios (``3.51918971`` becomes ``0.035192``); an index such as the CISS is
stored as it is.

The series go through the same cache as FRED (:mod:`macro.rate_cache`): Redis,
then ``macro_rates_cache``, then the API.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from macro.rate_cache import CachedRateService
from schemas.macro import RATE_UNIT, MacroRate, RiskFreeRate

logger = logging.getLogger(__name__)

ECB_API_URL = "https://data-api.ecb.europa.eu/service/data"

# Observations asked for, to find the latest one with a value.
_LAST_OBSERVATIONS = 5

# Stored like FRED rates: six decimals (``macro_rates_cache.value`` is NUMERIC(10, 6)).
_PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class EcbSeries:
    """A series of the ECB Data Portal: its flow, its key and how its values read."""

    flow: str
    key: str
    label: str
    unit: str = RATE_UNIT  # "ratio": published in percent; "index": stored as published

    @property
    def series_id(self) -> str:
        """Name of the series in ``macro_rates_cache``: the ``KEY`` column of the ECB."""
        return f"{self.flow}.{self.key}"


# Euro risk-free rate: 10-year spot rate of the yield curve of the euro area
# governments rated AAA (Svensson model), business days.
EURO_RISK_FREE_10Y = EcbSeries(
    "YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "Euro area AAA government 10-year spot rate"
)
# Deposit facility rate: the policy rate the ECB steers money markets with.
DEPOSIT_FACILITY_RATE = EcbSeries("FM", "D.U2.EUR.4F.KR.DFR.LEV", "ECB deposit facility rate")
# Composite Indicator of Systemic Stress, between 0 and 1.
SYSTEMIC_STRESS = EcbSeries(
    "CISS",
    "D.U2.Z0Z.4F.EC.SS_CIN.IDX",
    "Composite Indicator of Systemic Stress (euro area)",
    unit="index",
)

ECB_SERIES = {
    series.series_id: series
    for series in (EURO_RISK_FREE_10Y, DEPOSIT_FACILITY_RATE, SYSTEMIC_STRESS)
}


def parse_latest_observation(text: str, series: EcbSeries) -> MacroRate | None:
    """The latest observation with a value in an ECB CSV answer, or ``None``."""
    latest: tuple[date, Decimal] | None = None
    for row in csv.DictReader(io.StringIO(text)):
        raw_value = (row.get("OBS_VALUE") or "").strip()
        raw_period = (row.get("TIME_PERIOD") or "").strip()
        if not raw_value or not raw_period:
            continue
        try:
            value = Decimal(raw_value)
            observed = date.fromisoformat(raw_period)
        except (InvalidOperation, ValueError):
            continue
        if not value.is_finite():
            continue
        if latest is None or observed > latest[0]:
            latest = (observed, value)
    if latest is None:
        return None

    observed, value = latest
    if series.unit == RATE_UNIT:
        value = value / Decimal("100")
    return MacroRate(
        series_id=series.series_id,
        label=series.label,
        value=value.quantize(_PRECISION),
        unit=series.unit,
        observation_date=observed,
    )


class ECBService(CachedRateService):
    """Euro area rates from the ECB Data Portal, through the shared macro cache."""

    source = "ecb"

    def __init__(self, db_service, redis_client=None):
        super().__init__(db_service, redis_client)
        self.base_url = (os.environ.get("ECB_API_URL") or ECB_API_URL).rstrip("/")

    async def get_series(self, series: EcbSeries) -> MacroRate | None:
        """The latest observation of a series: cached, read now, or stored before."""
        return await self._get_series(series.series_id, series.label)

    async def get_euro_risk_free_rate(self) -> RiskFreeRate | None:
        """The euro risk-free rate (AAA 10-year), or ``None`` when none is known.

        The caller chooses the fallback: the ECB is not asked for other currencies.
        """
        rate = await self.get_series(EURO_RISK_FREE_10Y)
        if rate is None:
            return None
        return RiskFreeRate(
            value=rate.value,
            source=f"ecb_{rate.freshness or 'cached'}",
            observation_date=rate.observation_date,
        )

    async def _fetch_series(self, series_id: str, label: str) -> MacroRate | None:
        series = ECB_SERIES.get(series_id)
        if series is None:
            logger.error("ECB series %s is not known", series_id)
            return None
        url = f"{self.base_url}/{series.flow}/{series.key}"
        params = {"lastNObservations": _LAST_OBSERVATIONS, "format": "csvdata"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params, headers={"Accept": "text/csv"})
                response.raise_for_status()
        except Exception as e:
            logger.error("Erreur API BCE pour %s: %s", series_id, e)
            return None
        rate = parse_latest_observation(response.text, series)
        if rate is None:
            logger.warning("ECB answered no observation with a value for %s", series_id)
        return rate
