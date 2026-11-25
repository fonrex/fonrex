"""The latest 10-year rate of the OECD in the currency of a valuation.

A DCF discounts its cash flows at a rate built on the risk-free rate of their
currency: a long rate, the yield of a 10-year government bond. The OECD publishes
it every month for 44 areas (``OECD_LONG_SERIES`` of ``macro/short_rates.py``,
measure ``IRLT``); this module gives the latest one of the area of a currency,
with no request of its own:

* the area of each currency is the one of its 3-month rate
  (``OECD_AREA_OF_CURRENCY`` of ``macro/risk_free_rates.py``), except:
  ``EUR`` is Germany (``DEU``), the yield closest to an AAA one, since ``EA20``
  is a mean of the whole euro area and carries the credit risk of its members;
  ``USD`` is ``USA`` and ``GBP`` is ``GBR`` (the 10-year rate of the United
  Kingdom goes on, unlike its 3-month rate); ``BRL`` is ``BRA``; ``ISK`` and
  ``IDR`` have none, since the OECD gives Iceland and Indonesia no 10-year rate;
* a currency of minor unit is read as its major one (``GBX`` as ``GBP``);
* the rates are read again first when they are not up to date (``refresh()``
  keeps its refresh delay and its delay after a failure);
* the rate is the last month stored, at most ``MAX_MONTHS_OLD`` months before
  the month of the day: the OECD publishes a month about six weeks after its end;
* the source says how fresh it is: ``oecd_live`` (read now), ``oecd_cached``
  (read recently) or ``oecd_stale`` (the last reading failed, the stored rate is
  used);
* no rate within those bounds, or no area, gives no rate, with the reason: the
  caller chooses its fallback and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from macro.risk_free_rates import OECD_AREA_OF_CURRENCY, major_currency, month_of
from macro.short_rates import FAILED, FETCHED, RatesLoad, StoredArea, months_before

MAX_MONTHS_OLD = 3  # the OECD publishes a month about six weeks after its end

LIVE = "oecd_live"
CACHED = "oecd_cached"
STALE = "oecd_stale"

# Neither Iceland nor Indonesia has a 10-year rate at the OECD.
_WITHOUT_LONG_RATE = {"ISK", "IDR"}
# The area of the OECD of the 10-year rate of each currency (ISO 4217 → reference area).
OECD_LONG_AREA_OF_CURRENCY: dict[str, str] = {
    **{
        currency: area
        for currency, area in OECD_AREA_OF_CURRENCY.items()
        if currency not in _WITHOUT_LONG_RATE
    },
    "EUR": "DEU",
    "USD": "USA",
    "GBP": "GBR",
    "BRL": "BRA",
}


class LongRateSeries(Protocol):
    """The stored 10-year rates of every area (``ShortRates`` with ``OECD_LONG_SERIES``)."""

    async def refresh(self) -> RatesLoad: ...

    async def series(
        self, area: str, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, Decimal]]: ...

    async def stored_areas(self) -> list[StoredArea]: ...


@dataclass(frozen=True)
class LongRate:
    """The latest 10-year rate of a currency, as an annual ratio, or the reason why there is none."""

    currency: str | None
    value: Decimal | None
    source: str | None  # oecd_live, oecd_cached or oecd_stale
    area: str | None
    observation_date: date | None  # the month of the rate, dated by its first day
    reason: str | None = None


def long_rate_area(currency: str | None) -> str | None:
    """The area of the OECD of the 10-year rate of a currency, ``None`` when there is none."""
    return OECD_LONG_AREA_OF_CURRENCY.get(major_currency(currency) or "")


def _freshness(load: RatesLoad) -> str:
    if load.status == FETCHED:
        return LIVE
    if load.status == FAILED:
        return STALE
    return CACHED


class LongRateRepository:
    """The latest 10-year rate of the OECD in the currency of a valuation."""

    def __init__(self, long_rates: LongRateSeries) -> None:
        self.long_rates = long_rates

    def has_source(self, currency: str | None) -> bool:
        return long_rate_area(currency) is not None

    async def latest(self, currency: str | None, *, today: date) -> LongRate:
        """The rate of the last month stored for the area of ``currency``, at most
        ``MAX_MONTHS_OLD`` months before the month of ``today``."""
        major = major_currency(currency)
        area = long_rate_area(major)
        if area is None:
            return LongRate(
                major, None, None, None, None, f"no OECD 10-year rate for {major or 'no currency'}"
            )
        load = await self.long_rates.refresh()
        month = month_of(today)
        oldest = months_before(month, MAX_MONTHS_OLD)
        series = await self.long_rates.series(area, oldest, month)
        if series:
            period, value = series[-1]
            return LongRate(major, value, _freshness(load), area, period)
        reason = f"no OECD 10-year rate of {area} since {oldest:%Y-%m}"
        stored = {stored.area: stored for stored in await self.long_rates.stored_areas()}
        last = stored[area].last_period if area in stored else None
        reason += f" (the last month stored is {last:%Y-%m})" if last else " (nothing stored)"
        if load.reason:
            reason += f"; the last reading failed: {load.reason}"
        return LongRate(major, None, None, area, None, reason)
