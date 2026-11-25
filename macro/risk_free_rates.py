"""The risk-free rate of each day, in the currency of a listing.

Sharpe and Sortino compare each daily return to the risk-free rate of its day
(``risk/metrics.py``). The rate is a short-term rate, in the currency of the
listing, as an annual ratio:

* **USD**: the 3-month maturity of the par yield curve of the US Treasury, daily
  (``macro/treasury_curves.py``); a day without rate (week-end, holiday) takes the
  rate of the last day before it, at most ``MAX_DAILY_GAP`` days earlier;
* **other currencies**: the 3-month interbank rate of the OECD, monthly
  (``macro/short_rates.py``), of the area of the currency (``EUR`` is the euro area
  ``EA20``, ``CHF`` is ``CHE``...); a day takes the rate of its month. The OECD
  publishes a month about six weeks after its end, so a month not published yet
  takes the last month stored, at most ``MAX_MONTHS_CARRIED`` months earlier.
  The OECD chains the series itself across the end of the LIBOR (2021): checked in
  November 2026, ``EA20`` is the monthly mean of the 3-month Euribor (2.51 % in
  August 2026, as Eurostat gives it), so the ECB is not read for this rate, and
  ``CHE`` follows the SARON since the CHF LIBOR stopped (-0.0415 % in October 2026,
  SARON fixings near -0.04 %), with no break in the series;
* **GBP**: the monthly mean of SONIA, the overnight rate the Bank of England
  administers (``SONIA_SERIES`` of ``macro/short_rates.py``), on the same monthly
  rules. The OECD stopped its 3-month rate of the United Kingdom in early 2026; SONIA
  runs from 1997 without a break and is published a few days after each month;
* a currency of minor unit is read as its major one (``GBX``, the pence of London,
  as ``GBP``): a constant unit does not change a return;
* a currency without source, or a day without rate within those bounds, gives no
  rate at all, with the reason: Sharpe and Sortino are then ``None``. A fixed rate
  such as ``DCF_RISK_FREE_RATE`` is never used, since a wrong rate over five years
  would give a wrong ratio without saying so.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from financials.providers.treasury import NOMINAL
from macro.short_rates import ShortRates, months_before
from macro.treasury_curves import TreasuryCurves
from valuation.currency import MINOR_UNITS, currency_code

logger = logging.getLogger(__name__)

USD = "USD"
TREASURY_TENOR = "3M"
MAX_DAILY_GAP = 7  # days: a long week-end, a holiday week
MAX_MONTHS_CARRIED = 3  # the OECD publishes a month about six weeks after its end

GBP = "GBP"
SONIA_AREA = "GBR"  # the area under which the monthly SONIA is stored

# The area of the OECD of each currency (ISO 4217 → reference area of the OECD). The
# pound has its own source: the OECD stopped its rate of the United Kingdom in 2026.
OECD_AREA_OF_CURRENCY: dict[str, str] = {
    "EUR": "EA20",
    "CHF": "CHE",
    "SEK": "SWE",
    "NOK": "NOR",
    "DKK": "DNK",
    "ISK": "ISL",
    "PLN": "POL",
    "CZK": "CZE",
    "HUF": "HUN",
    "RON": "ROU",
    "JPY": "JPN",
    "KRW": "KOR",
    "CNY": "CHN",
    "INR": "IND",
    "IDR": "IDN",
    "CAD": "CAN",
    "AUD": "AUS",
    "NZD": "NZL",
    "ILS": "ISR",
    "ZAR": "ZAF",
    "MXN": "MEX",
    "CLP": "CHL",
    "COP": "COL",
    "CRC": "CRI",
}

TREASURY_SOURCE = "US Treasury, par yield curve, 3-month maturity"
SONIA_SOURCE = "Bank of England, SONIA, monthly mean"


def oecd_source(area: str) -> str:
    return f"OECD, 3-month interbank rate ({area})"


@dataclass(frozen=True)
class RiskFreeRates:
    """The rate of each day asked, as an annual ratio, or the reason why there is none."""

    currency: str | None
    source: str | None
    rates: dict[date, float] | None
    reason: str | None = None
    # The first and last observations of the source used for the days asked.
    first_observation: date | None = None
    last_observation: date | None = None

    @property
    def mean(self) -> float | None:
        if not self.rates:
            return None
        return sum(self.rates.values()) / len(self.rates)


def major_currency(currency: str | None) -> str | None:
    """The currency in upper case, a minor unit as its major one (``GBp`` → ``GBP``)."""
    code = currency_code(currency)
    return MINOR_UNITS[code][0] if code in MINOR_UNITS else code


def month_of(day: date) -> date:
    return day.replace(day=1)


def align_daily(
    days: Iterable[date], series: Sequence[tuple[date, Decimal]], max_gap: int = MAX_DAILY_GAP
) -> tuple[dict[date, tuple[date, float]], date | None]:
    """The rate of each day, else of the last day before it within ``max_gap`` days.

    Returns the rate of each day with the day it comes from, and the first day
    without rate (``None`` when every day has one). ``series`` is oldest first.
    """
    found: dict[date, tuple[date, float]] = {}
    ordered = sorted(series)
    index, last = 0, None
    for day in sorted(set(days)):
        while index < len(ordered) and ordered[index][0] <= day:
            last = ordered[index]
            index += 1
        if last is None or (day - last[0]).days > max_gap:
            return found, day
        found[day] = (last[0], float(last[1]))
    return found, None


def align_monthly(
    days: Iterable[date],
    series: Sequence[tuple[date, Decimal]],
    max_months: int = MAX_MONTHS_CARRIED,
) -> tuple[dict[date, tuple[date, float]], date | None]:
    """The rate of the month of each day, else of the last month before it within
    ``max_months`` months. Same return as ``align_daily``; months are dated by their first day."""
    found: dict[date, tuple[date, float]] = {}
    ordered = sorted(series)
    index, last = 0, None
    for day in sorted(set(days)):
        month = month_of(day)
        while index < len(ordered) and ordered[index][0] <= month:
            last = ordered[index]
            index += 1
        if last is None or last[0] < months_before(month, max_months):
            return found, day
        found[day] = (last[0], float(last[1]))
    return found, None


def _rates(
    currency: str,
    source: str,
    aligned: dict[date, tuple[date, float]],
    missing: date | None,
    series: Sequence[tuple[date, Decimal]],
    refresh_reason: str | None,
) -> RiskFreeRates:
    if missing is not None:
        held = (
            f"the rates read run from {series[0][0]} to {series[-1][0]}"
            if series
            else "nothing stored"
        )
        reason = f"no {source} for {missing.isoformat()} ({held})"
        if refresh_reason:
            reason += f"; the last reading failed: {refresh_reason}"
        return RiskFreeRates(currency, source, None, reason)
    used = [origin for origin, _rate in aligned.values()]
    return RiskFreeRates(
        currency,
        source,
        {day: rate for day, (_origin, rate) in aligned.items()},
        first_observation=min(used, default=None),
        last_observation=max(used, default=None),
    )


class RiskFreeRateRepository:
    """The daily risk-free rates of a currency, read from the stored sources."""

    def __init__(
        self, treasury: TreasuryCurves, short_rates: ShortRates, sonia: ShortRates | None = None
    ):
        self.treasury = treasury
        self.short_rates = short_rates
        # The monthly SONIA (``SONIA_SERIES``); without it, the pound has no rate.
        self.sonia = sonia

    def source_of(self, currency: str | None) -> str | None:
        """The source of the rates of a currency, ``None`` when there is none."""
        major = major_currency(currency)
        if major == USD:
            return TREASURY_SOURCE
        if major == GBP:
            return SONIA_SOURCE if self.sonia is not None else None
        if major in OECD_AREA_OF_CURRENCY:
            return oecd_source(OECD_AREA_OF_CURRENCY[major])
        return None

    async def rates(
        self, currency: str | None, days: Iterable[date], *, refresh: bool = True
    ) -> RiskFreeRates:
        """The rate of each of ``days``. With ``refresh``, the source is read first when
        it is not up to date (a failure keeps the stored rates)."""
        major = major_currency(currency)
        wanted = sorted(set(days))
        if not wanted:
            return RiskFreeRates(major, self.source_of(major), {})
        if major == USD:
            return await self._treasury(wanted, refresh)
        if major == GBP and self.sonia is not None:
            return await self._monthly(GBP, self.sonia, SONIA_AREA, SONIA_SOURCE, wanted, refresh)
        area = OECD_AREA_OF_CURRENCY.get(major or "")
        if area is None:
            return RiskFreeRates(
                major, None, None, f"no short-term rate source for {major or 'an unknown currency'}"
            )
        return await self._monthly(
            major or "", self.short_rates, area, oecd_source(area), wanted, refresh
        )

    async def _treasury(self, days: list[date], refresh: bool) -> RiskFreeRates:
        start, end = days[0] - timedelta(days=MAX_DAILY_GAP), days[-1]
        refresh_reason = None
        if refresh:
            load = await self.treasury.ensure(NOMINAL, start, end)
            refresh_reason = load.reason
        series = (await self.treasury.history(NOMINAL, [TREASURY_TENOR], start, end))[
            TREASURY_TENOR
        ]
        aligned, missing = align_daily(days, series)
        return _rates(USD, TREASURY_SOURCE, aligned, missing, series, refresh_reason)

    async def _monthly(
        self,
        currency: str,
        short_rates: ShortRates,
        area: str,
        source: str,
        days: list[date],
        refresh: bool,
    ) -> RiskFreeRates:
        refresh_reason = None
        if refresh:
            load = await short_rates.refresh()
            refresh_reason = load.reason
        start = months_before(days[0], MAX_MONTHS_CARRIED)
        series = await short_rates.series(area, start, month_of(days[-1]))
        aligned, missing = align_monthly(days, series)
        return _rates(currency, source, aligned, missing, series, refresh_reason)
