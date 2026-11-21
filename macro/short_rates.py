"""The 3-month interest rates of the OECD, kept month by month.

The OECD publishes the 3-month interbank rate of its members and of a few other
countries (``financials/providers/oecd.py``): one request returns every country.
The rates are kept in ``macro_observations`` (source ``oecd``, indicator
``rate_3m``, ``geo`` the reference area of the OECD: ``EA20`` for the euro area,
``CHE``, ``GBR``, ``SWE``...), one row per month dated by its first day, as
ratios; ``macro_series_loads`` keeps, for each area, the last reading and the
months it holds.

A reading asks for every country at once:

* the first reading takes the whole history from ``FIRST_PERIOD``;
* the next ones ask again for the last ``REVISED_MONTHS`` months, so that a
  revision replaces the value of its month;
* a reading less than ``OECD_REFRESH_HOURS`` hours old (24 by default) is not
  done again: the OECD allows about 60 downloads an hour.

When the OECD does not answer (refused, over its limit), the stored rates stay
and the reading says ``failed`` with the reason. An answer that cannot be read is
not stored at all. After a failure the OECD is not asked again for
``OECD_RETRY_MINUTES`` minutes (15 by default), except with ``force``: every
request needing a rate would otherwise download again, one after the other
behind the lock, and spend the quota. A country the OECD no longer publishes
keeps its last months: the caller decides how long a rate may be carried over.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.providers.oecd import OECDError, ShortRate
from models import MacroObservation, MacroSeriesLoad
from schemas.macro import RATE_UNIT
from settings import env_int

logger = logging.getLogger(__name__)

SOURCE = "oecd"
INDICATOR = "rate_3m"
FREQUENCY = "monthly"
# The first month read: the Treasury curve, the US rate of the same use, starts in 1990.
FIRST_PERIOD = date(1990, 1, 1)
REVISED_MONTHS = 24
_PRECISION = Decimal("0.00000001")  # macro_observations.value is NUMERIC(18, 8)
# Rows of one INSERT: 9 columns a row, 27,000 parameters, under the 32,767 asyncpg
# accepts in one statement (the whole history is about 45 areas × 430 months).
_MAX_PARAMETERS = 32_767
_CHUNK = 3000

FETCHED = "fetched"
FRESH = "fresh"
FAILED = "failed"

RatesReader = Callable[[date], Awaitable[list[ShortRate] | None]]


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def months_before(day: date, months: int) -> date:
    """The first day of the month ``months`` months before the month of ``day``."""
    index = day.year * 12 + day.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class StoredArea:
    """The months stored for one area, and its last reading."""

    area: str
    first_period: date | None
    last_period: date | None
    fetched_at: datetime


@dataclass
class RatesLoad:
    """What a reading asked of the OECD, and what it received."""

    status: str = FRESH  # fetched: read now; fresh: read recently; failed
    fetched_at: datetime | None = None  # the last reading that succeeded
    start: date | None = None  # first month asked
    received: int = 0
    areas: list[str] = field(default_factory=list)
    reason: str | None = None


class ShortRates:
    """The 3-month rates of the OECD, stored month by month and read again on demand."""

    source = SOURCE

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        read_rates: RatesReader,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.session_factory = session_factory
        self.read_rates = read_rates
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_after = timedelta(
            hours=env_int("OECD_REFRESH_HOURS", 24, minimum=1, maximum=24 * 30)
        )
        self.retry_after = timedelta(
            minutes=env_int("OECD_RETRY_MINUTES", 15, minimum=1, maximum=24 * 60)
        )
        # The last failed reading of this process, and its reason.
        self._failure: tuple[datetime, str] | None = None
        # One reading at a time: two requests needing the rates ask the OECD once.
        self._lock = asyncio.Lock()

    async def last_reading(self) -> datetime | None:
        """When the rates were last read, for any area."""
        async with self.session_factory() as session:
            return await session.scalar(
                select(func.max(MacroSeriesLoad.fetched_at)).where(
                    MacroSeriesLoad.source == SOURCE, MacroSeriesLoad.indicator == INDICATOR
                )
            )

    def first_month(self, last_reading: datetime | None) -> date:
        """The first month a reading asks for: the whole history the first time."""
        if last_reading is None:
            return FIRST_PERIOD
        return max(FIRST_PERIOD, months_before(_utc(self.now()).date(), REVISED_MONTHS))

    async def refresh(self, *, force: bool = False, start: date | None = None) -> RatesLoad:
        """Read the rates of every country, unless they were read recently (always with
        ``force``). ``start`` chooses the first month asked."""
        async with self._lock:
            last = await self.last_reading()
            if (
                not force
                and last is not None
                and _utc(self.now()) - _utc(last) < self.refresh_after
            ):
                return RatesLoad(FRESH, last)
            if not force and self._failure is not None:
                failed_at, reason = self._failure
                if _utc(self.now()) - failed_at < self.retry_after:
                    return RatesLoad(
                        FAILED,
                        last,
                        reason=f"{reason} (at {failed_at:%Y-%m-%d %H:%M} UTC; asked again "
                        f"{int(self.retry_after.total_seconds() // 60)} minutes after)",
                    )
            first = start or self.first_month(last)
            result = RatesLoad(FAILED, last, first)
            try:
                rates = await self.read_rates(first)
            except OECDError as error:
                result.reason = f"OECDError: {error}"
            except Exception as error:  # network, unexpected failure
                result.reason = (
                    f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
                )
            else:
                if rates is None:
                    result.reason = "the OECD did not answer (refused, over its limit, or no rate)"
                elif not rates:
                    result.reason = f"the OECD gave no rate from {first:%Y-%m}"
                else:
                    result.status, result.fetched_at = FETCHED, await self._store(rates)
                    result.received = len(rates)
                    result.areas = sorted({rate.area for rate in rates})
            if result.reason:
                self._failure = (_utc(self.now()), result.reason)
                logger.warning("OECD 3-month rates not read: %s", result.reason)
            else:
                self._failure = None
            return result

    async def _store(self, rates: list[ShortRate]) -> datetime:
        fetched_at = _utc(self.now())
        rows = [
            {
                "source": SOURCE,
                "indicator": INDICATOR,
                "geo": rate.area,
                "period": rate.period,
                "frequency": FREQUENCY,
                "value": rate.value.quantize(_PRECISION),
                "unit": RATE_UNIT,
                "status": rate.status,
                "fetched_at": fetched_at,
            }
            for rate in rates
        ]
        areas = sorted({rate.area for rate in rates})
        async with self.session_factory() as session:
            async with session.begin():
                for chunk in range(0, len(rows), _CHUNK):
                    statement = insert(MacroObservation).values(rows[chunk : chunk + _CHUNK])
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["source", "indicator", "geo", "period"],
                            set_={
                                "value": statement.excluded.value,
                                "status": statement.excluded.status,
                                "fetched_at": statement.excluded.fetched_at,
                            },
                        )
                    )
                ranges = await session.execute(
                    select(
                        MacroObservation.geo,
                        func.min(MacroObservation.period),
                        func.max(MacroObservation.period),
                    )
                    .where(
                        MacroObservation.source == SOURCE,
                        MacroObservation.indicator == INDICATOR,
                        MacroObservation.geo.in_(areas),
                    )
                    .group_by(MacroObservation.geo)
                )
                for area, first_period, last_period in ranges:
                    await session.merge(
                        MacroSeriesLoad(
                            source=SOURCE,
                            indicator=INDICATOR,
                            geo=area,
                            fetched_at=fetched_at,
                            first_period=first_period,
                            last_period=last_period,
                        )
                    )
        return fetched_at

    async def stored_areas(self) -> list[StoredArea]:
        """The areas stored, with their months and their last reading."""
        async with self.session_factory() as session:
            loads = await session.scalars(
                select(MacroSeriesLoad)
                .where(MacroSeriesLoad.source == SOURCE, MacroSeriesLoad.indicator == INDICATOR)
                .order_by(MacroSeriesLoad.geo)
            )
            return [
                StoredArea(load.geo, load.first_period, load.last_period, load.fetched_at)
                for load in loads
            ]

    async def series(
        self, area: str, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, Decimal]]:
        """The stored rates of an area, as ratios, oldest month first."""
        statement = select(MacroObservation.period, MacroObservation.value).where(
            MacroObservation.source == SOURCE,
            MacroObservation.indicator == INDICATOR,
            MacroObservation.geo == area,
        )
        if start is not None:
            statement = statement.where(MacroObservation.period >= start)
        if end is not None:
            statement = statement.where(MacroObservation.period <= end)
        async with self.session_factory() as session:
            rows = await session.execute(statement.order_by(MacroObservation.period))
            return [(period, value) for period, value in rows]
