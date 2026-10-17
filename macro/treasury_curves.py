"""The daily yield curves of the US Treasury, kept day by day.

The Treasury publishes its par yield curve (nominal, since 1990) and its par real
yield curve (TIPS, since 2003) as one CSV file per year
(``financials/providers/treasury.py``). The rates are kept in
``yield_curve_points``, one row per curve, day and maturity, and each reading of a
year in ``yield_curve_loads``.

A year is read only when a request needs it:

* a year never read is read;
* a past year read after its end is complete: it is never asked again;
* the current year, or a past year read before it ended, is read again once its
  last reading is older than ``TREASURY_REFRESH_HOURS`` hours (12 by default);
* a year before the first of the curve, or after the current one, is not asked.

A file read again replaces the rates of its year: the year stored is the file. A
file without any day while days are stored for its year is taken for a failure
of the Treasury, and the stored days are kept. When the Treasury does not answer,
the stored rates are given and the reading says ``failed`` with the reason; the
next years of the request are not asked, so that a request over the whole
history does not wait for every year. A file that cannot be read fails alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.providers.treasury import CURVES, TENORS_BY_CODE, CurveFile, TreasuryError
from models import YieldCurveLoad, YieldCurvePoint
from settings import env_int

logger = logging.getLogger(__name__)

SOURCE = "treasury"
_CHUNK = 5000

FETCHED = "fetched"
FRESH = "fresh"
FAILED = "failed"

YearReader = Callable[[str, int], Awaitable[CurveFile | None]]


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def curve_of(name: str) -> str:
    """The name of a curve, or ``ValueError``."""
    if name not in CURVES:
        raise ValueError(f"Unknown curve {name!r}: {', '.join(CURVES)}")
    return name


def is_complete(year: int, fetched_at: datetime | None) -> bool:
    """A year read after its end holds all its days: the Treasury dates a day in New York,
    so a reading from 2 January (UTC) on holds 31 December."""
    return fetched_at is not None and _utc(fetched_at).date() >= date(year + 1, 1, 2)


@dataclass
class CurveLoad:
    """What a request asked of the Treasury for a curve, and what the database holds."""

    curve: str
    status: str = FRESH  # fetched: a year read now; fresh: nothing to read; failed
    fetched_at: datetime | None = None  # the latest reading among the years asked
    first_day: date | None = None
    last_day: date | None = None
    years_read: list[int] = field(default_factory=list)
    years_failed: list[int] = field(default_factory=list)
    reason: str | None = None  # of the first failure


@dataclass(frozen=True)
class StoredDay:
    """The rates of a curve on one day, as ratios, by tenor code, shortest first."""

    day: date
    points: dict[str, Decimal]


class TreasuryCurves:
    """The yield curves of the US Treasury, stored day by day and read again on demand."""

    source = SOURCE

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        read_year: YearReader,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.session_factory = session_factory
        self.read_year = read_year
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_after = timedelta(
            hours=env_int("TREASURY_REFRESH_HOURS", 12, minimum=1, maximum=24 * 30)
        )
        # One reading of a curve at a time: two requests needing the same year read it once.
        self._locks = {name: asyncio.Lock() for name in CURVES}

    def today(self) -> date:
        return _utc(self.now()).date()

    def years(self, curve: str, start_year: int, end_year: int | None = None) -> list[int]:
        """The years of a request that the Treasury publishes for the curve."""
        current = self.today().year
        last = min(current if end_year is None else end_year, current)
        return list(range(max(start_year, CURVES[curve_of(curve)].first_year), last + 1))

    def _needs_reading(self, year: int, load: YieldCurveLoad | None, force: bool) -> bool:
        if load is None or force:
            return True
        if is_complete(year, load.fetched_at):
            return False
        return _utc(self.now()) - _utc(load.fetched_at) >= self.refresh_after

    async def refresh(
        self, curve: str, start_year: int, end_year: int | None = None, *, force: bool = False
    ) -> CurveLoad:
        """Read from the Treasury the years of a curve that need it (all of them with ``force``)."""
        result = CurveLoad(curve_of(curve))
        years = self.years(curve, start_year, end_year)
        async with self._locks[curve]:
            loads = await self._loads(curve, years)
            unanswered = None
            for year in years:
                if not self._needs_reading(year, loads.get(year), force):
                    continue
                if unanswered is not None:
                    result.years_failed.append(year)
                    continue
                reason, answered = await self._read(curve, year)
                if reason is None:
                    result.years_read.append(year)
                    continue
                result.years_failed.append(year)
                result.reason = result.reason or reason
                if not answered:
                    unanswered = reason
            loads = await self._loads(curve, years)
        if result.years_failed:
            result.status = FAILED
        elif result.years_read:
            result.status = FETCHED
        readings = [load.fetched_at for load in loads.values() if load.fetched_at]
        result.fetched_at = max(readings, default=None)
        result.first_day, result.last_day = await self.stored_range(curve)
        return result

    async def ensure(self, curve: str, start: date, end: date | None = None) -> CurveLoad:
        """Read the years of a period that need it, before the stored rates are read."""
        end = end or self.today()
        return await self.refresh(curve, start.year, end.year)

    async def _loads(self, curve: str, years: Iterable[int]) -> dict[int, YieldCurveLoad]:
        years = list(years)
        if not years:
            return {}
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(YieldCurveLoad).where(
                    YieldCurveLoad.source == SOURCE,
                    YieldCurveLoad.curve == curve,
                    YieldCurveLoad.year.in_(years),
                )
            )
            return {load.year: load for load in rows}

    async def _read(self, curve: str, year: int) -> tuple[str | None, bool]:
        """Read one year and store it: the reason when it could not be done, and whether
        the Treasury answered (a file it sent that cannot be read is an answer)."""
        answered = False
        try:
            curve_file = await self.read_year(curve, year)
        except TreasuryError as error:
            reason, answered = f"TreasuryError: {error}", True
        except Exception as error:  # network, unexpected failure
            reason = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        else:
            if curve_file is None:
                reason = "the Treasury did not answer"
            else:
                return await self._store(curve, year, curve_file), True
        logger.warning("Treasury %s curve of %s not read: %s", curve, year, reason)
        return reason, answered

    async def _store(self, curve: str, year: int, curve_file: CurveFile) -> str | None:
        fetched_at = _utc(self.now())
        first, last = date(year, 1, 1), date(year, 12, 31)
        in_year = (
            YieldCurvePoint.source == SOURCE,
            YieldCurvePoint.curve == curve,
            YieldCurvePoint.rate_date.between(first, last),
        )
        rows = [
            {
                "source": SOURCE,
                "curve": curve,
                "rate_date": day.day,
                "tenor": tenor,
                "tenor_months": TENORS_BY_CODE[tenor].months,
                "value": value,
                "fetched_at": fetched_at,
            }
            for day in curve_file.days
            for tenor, value in day.points.items()
        ]
        async with self.session_factory() as session:
            async with session.begin():
                if not rows:
                    stored = await session.scalar(select(func.count()).where(*in_year))
                    if stored:
                        reason = f"the Treasury gave no day for {year}; the {stored} rates stored are kept"
                        logger.warning("Treasury %s curve: %s", curve, reason)
                        return reason
                await session.execute(delete(YieldCurvePoint).where(*in_year))
                for chunk in range(0, len(rows), _CHUNK):
                    statement = insert(YieldCurvePoint).values(rows[chunk : chunk + _CHUNK])
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["source", "curve", "rate_date", "tenor"],
                            set_={
                                "tenor_months": statement.excluded.tenor_months,
                                "value": statement.excluded.value,
                                "fetched_at": statement.excluded.fetched_at,
                            },
                        )
                    )
                days = [day.day for day in curve_file.days]
                await session.merge(
                    YieldCurveLoad(
                        source=SOURCE,
                        curve=curve,
                        year=year,
                        fetched_at=fetched_at,
                        days=len(days),
                        first_day=min(days, default=None),
                        last_day=max(days, default=None),
                    )
                )
        return None

    async def stored_range(self, curve: str) -> tuple[date | None, date | None]:
        """The first and the last day stored for a curve."""
        async with self.session_factory() as session:
            first, last = (
                await session.execute(
                    select(
                        func.min(YieldCurvePoint.rate_date), func.max(YieldCurvePoint.rate_date)
                    ).where(YieldCurvePoint.source == SOURCE, YieldCurvePoint.curve == curve)
                )
            ).one()
        return first, last

    async def curve_on(self, curve: str, day: date | None = None) -> StoredDay | None:
        """The stored curve of a day, or of the last day before it with rates (the latest
        day when ``day`` is not given): a week-end or a holiday gives the business day before."""
        on_curve = (YieldCurvePoint.source == SOURCE, YieldCurvePoint.curve == curve_of(curve))
        latest = select(func.max(YieldCurvePoint.rate_date)).where(*on_curve)
        if day is not None:
            latest = latest.where(YieldCurvePoint.rate_date <= day)
        async with self.session_factory() as session:
            found = await session.scalar(latest)
            if found is None:
                return None
            rows = (
                await session.execute(
                    select(YieldCurvePoint.tenor, YieldCurvePoint.value)
                    .where(*on_curve, YieldCurvePoint.rate_date == found)
                    .order_by(YieldCurvePoint.tenor_months)
                )
            ).all()
        return StoredDay(found, {tenor: value for tenor, value in rows})

    async def history(
        self,
        curve: str,
        tenors: Iterable[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, list[tuple[date, Decimal]]]:
        """The stored rates of some maturities, oldest day first, by tenor code."""
        tenors = list(dict.fromkeys(tenors))
        unknown = [tenor for tenor in tenors if tenor not in TENORS_BY_CODE]
        if unknown:
            raise ValueError(f"Unknown maturity {unknown[0]!r}: {', '.join(TENORS_BY_CODE)}")
        statement = select(
            YieldCurvePoint.tenor, YieldCurvePoint.rate_date, YieldCurvePoint.value
        ).where(
            YieldCurvePoint.source == SOURCE,
            YieldCurvePoint.curve == curve_of(curve),
            YieldCurvePoint.tenor.in_(tenors),
        )
        if start is not None:
            statement = statement.where(YieldCurvePoint.rate_date >= start)
        if end is not None:
            statement = statement.where(YieldCurvePoint.rate_date <= end)
        series: dict[str, list[tuple[date, Decimal]]] = {tenor: [] for tenor in tenors}
        async with self.session_factory() as session:
            rows = await session.execute(statement.order_by(YieldCurvePoint.rate_date))
            for tenor, day, value in rows:
                series[tenor].append((day, value))
        return series
