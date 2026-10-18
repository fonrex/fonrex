"""The Commitments of Traders reports of the CFTC, kept week by week.

The CFTC publishes every week the positions held on the Tuesday in the US futures
markets, by type of trader, in three reports read here (futures only): Legacy
since 1986, Traders in Financial Futures (TFF) and Disaggregated since June 2006
(``financials/providers/cftc.py``). The lines are kept in ``cot_weeks``, their
positions in ``cot_positions``, and each reading of a year in ``cot_loads``.

A year is read only when a request needs it:

* a year never read is read;
* a year up to 2016 comes from the history file of its report, which holds every
  year from the first one to 2016: reading one of them reads them all, once, and
  they are never asked again;
* a later past year read from 15 January of the next year on is complete (the
  last report of a year is published in the first days of January): it is never
  asked again;
* the current year, or a past year read before that, is read again once its last
  reading is older than ``COT_REFRESH_HOURS`` hours (24 by default);
* a year before the first of the report, or after the current one, is not asked.

A file read again replaces the lines of its years: the years stored are the file.
A file without any line while lines are stored for its years is taken for a
failure of the CFTC, and the stored lines are kept. When the CFTC does not answer,
the stored lines are given and the reading says ``failed`` with the reason; the
next years of the request are not asked. A file that cannot be read fails alone.

The signals of a market (``signals``) are computed from the stored lines: the net
position of a type of trader, its share of the open interest, its change since the
previous report, and the COT index, which places the net position between its
lowest (0) and its highest (100) over a window of weeks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.providers.cftc import (
    DISAGGREGATED,
    HISTORY_LAST_YEAR,
    LEGACY,
    REPORTS,
    TFF,
    CftcError,
    CotFile,
)
from models import CotLoad, CotPosition, CotWeek
from settings import env_int

logger = logging.getLogger(__name__)

# asyncpg takes at most 32767 parameters a statement: 7 columns x 4000 rows.
_CHUNK = 4000

FETCHED = "fetched"
FRESH = "fresh"
FAILED = "failed"

# The type of trader followed by default: the speculators of each report.
SPECULATORS = {LEGACY: "noncommercial", TFF: "leveraged_funds", DISAGGREGATED: "managed_money"}

# The markets shown when none is asked, by CFTC contract market code.
KEY_MARKETS: dict[str, tuple[str, ...]] = {
    LEGACY: (
        "13874A",
        "209742",
        "043602",
        "099741",
        "097741",
        "088691",
        "067651",
        "023651",
        "002602",
    ),
    TFF: (
        "13874A",  # E-mini S&P 500
        "209742",  # Nasdaq mini
        "239742",  # Russell 2000 E-mini
        "042601",  # UST 2-year note
        "043602",  # UST 10-year note
        "099741",  # euro
        "097741",  # Japanese yen
        "096742",  # British pound
        "1170E1",  # VIX futures
        "133741",  # Bitcoin
    ),
    DISAGGREGATED: (
        "067651",  # WTI crude oil
        "023651",  # natural gas (Henry Hub)
        "088691",  # gold
        "084691",  # silver
        "085692",  # copper
        "002602",  # corn
        "005602",  # soybeans
        "001602",  # SRW wheat
    ),
}

DEFAULT_WINDOW_WEEKS = 156
_RATIO = Decimal("0.000001")
_INDEX = Decimal("0.1")

FileReader = Callable[[str, int], Awaitable[CotFile | None]]


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def report_of(name: str) -> str:
    """The name of a report, or ``ValueError``."""
    if name not in REPORTS:
        raise ValueError(f"Unknown report {name!r}: {', '.join(REPORTS)}")
    return name


def category_of(report: str, category: str | None) -> str:
    """A type of trader of the report (its speculators when none is given), or ``ValueError``."""
    codes = [known.code for known in REPORTS[report_of(report)].categories]
    if category is None:
        return SPECULATORS[report]
    if category not in codes:
        raise ValueError(f"Unknown type of trader {category!r} for {report}: {', '.join(codes)}")
    return category


def is_complete(year: int, fetched_at: datetime | None) -> bool:
    """A year holds all its reports: a year of the history files once read, a later year
    once read from 15 January of the next year on."""
    if fetched_at is None:
        return False
    if year <= HISTORY_LAST_YEAR:
        return True
    return _utc(fetched_at).date() >= date(year + 1, 1, 15)


def published_on(report_date: date) -> date:
    """The day a report was published, as an estimate: the Friday after its date.

    The CFTC publishes on Friday at 3:30 p.m. in New York the positions of the Tuesday.
    A holiday moves it by a day or two, and the reports of before October 1992 (twice a
    month) were published later; the CFTC keeps the true dates of 13 months only.
    """
    return report_date + timedelta(days=(4 - report_date.weekday()) % 7 or 7)


@dataclass
class ReportLoad:
    """What a request asked of the CFTC for a report, and what the database holds."""

    report: str
    status: str = FRESH  # fetched: a year read now; fresh: nothing to read; failed
    fetched_at: datetime | None = None  # the latest reading among the years asked
    first_day: date | None = None
    last_day: date | None = None
    years_read: list[int] = field(default_factory=list)
    years_failed: list[int] = field(default_factory=list)
    reason: str | None = None  # of the first failure


@dataclass(frozen=True)
class StoredPosition:
    long: int
    short: int
    spreading: int | None

    @property
    def net(self) -> int:
        return self.long - self.short


@dataclass(frozen=True)
class StoredWeek:
    """A stored line: a market on a report date, with the positions of each type of trader."""

    report_date: date
    name: str
    open_interest: int
    positions: dict[str, StoredPosition]


@dataclass(frozen=True)
class StoredMarket:
    """A market of a report: its latest name and the report dates stored for it."""

    report: str
    code: str
    name: str
    contract_units: str | None
    first_date: date
    last_date: date
    weeks: int


@dataclass(frozen=True)
class Signal:
    """The position of one type of trader on one report date, and what is computed from it.

    ``net_share`` is the net position as a ratio of the open interest. ``change`` is the
    change of the net position since the previous report stored. ``cot_index`` (0 to
    100) places the net position between its lowest and its highest over the window;
    it is ``None`` until the market has reports over the whole window, and when the
    net position did not move in it.
    """

    report_date: date
    published_on: date
    open_interest: int
    long: int
    short: int
    spreading: int | None
    net: int
    net_share: Decimal | None
    change: int | None
    cot_index: Decimal | None


def signals(
    weeks: Sequence[StoredWeek], category: str, window_weeks: int = DEFAULT_WINDOW_WEEKS
) -> list[Signal]:
    """The signals of one type of trader over stored lines (oldest first).

    The window of the COT index is a length of time (``window_weeks`` x 7 days ending on
    the report date), not a number of lines: the Legacy reports came twice a month
    until September 1992, and a market leaves the report some weeks.
    """
    if window_weeks < 1:
        raise ValueError("the window holds at least one week")
    ordered = [
        week for week in sorted(weeks, key=lambda w: w.report_date) if category in week.positions
    ]
    if not ordered:
        return []
    first_date = ordered[0].report_date
    span = timedelta(days=7 * window_weeks)
    nets = [week.positions[category].net for week in ordered]
    result = []
    start = 0  # the first line inside the window of the current one
    for index, week in enumerate(ordered):
        position = week.positions[category]
        window_start = week.report_date - span
        while ordered[start].report_date <= window_start:
            start += 1
        cot_index = None
        # The window is covered when a report is stored on its first week or before.
        if first_date <= window_start + timedelta(days=7):
            low, high = min(nets[start : index + 1]), max(nets[start : index + 1])
            if high > low:
                cot_index = (Decimal(100) * (position.net - low) / (high - low)).quantize(
                    _INDEX, rounding=ROUND_HALF_EVEN
                )
        share = None
        if week.open_interest > 0:
            share = (Decimal(position.net) / week.open_interest).quantize(
                _RATIO, rounding=ROUND_HALF_EVEN
            )
        result.append(
            Signal(
                report_date=week.report_date,
                published_on=published_on(week.report_date),
                open_interest=week.open_interest,
                long=position.long,
                short=position.short,
                spreading=position.spreading,
                net=position.net,
                net_share=share,
                change=position.net - nets[index - 1] if index else None,
                cot_index=cot_index,
            )
        )
    return result


class CotReports:
    """The Commitments of Traders reports, stored week by week and read again on demand."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        read_file: FileReader,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.session_factory = session_factory
        self.read_file = read_file
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_after = timedelta(
            hours=env_int("COT_REFRESH_HOURS", 24, minimum=1, maximum=24 * 30)
        )
        # One reading of a report at a time: two requests needing the same year read it once.
        self._locks = {name: asyncio.Lock() for name in REPORTS}

    def today(self) -> date:
        return _utc(self.now()).date()

    def years(self, report: str, start_year: int, end_year: int | None = None) -> list[int]:
        """The years of a request that the CFTC publishes for the report."""
        current = self.today().year
        last = min(current if end_year is None else end_year, current)
        return list(range(max(start_year, REPORTS[report_of(report)].first_year), last + 1))

    def _needs_reading(self, year: int, load: CotLoad | None, force: bool) -> bool:
        if load is None or force:
            return True
        if is_complete(year, load.fetched_at):
            return False
        return _utc(self.now()) - _utc(load.fetched_at) >= self.refresh_after

    async def refresh(
        self, report: str, start_year: int, end_year: int | None = None, *, force: bool = False
    ) -> ReportLoad:
        """Read from the CFTC the years of a report that need it (all of them with ``force``)."""
        result = ReportLoad(report_of(report))
        years = self.years(report, start_year, end_year)
        async with self._locks[report]:
            loads = await self._loads(report, years)
            to_read = [year for year in years if self._needs_reading(year, loads.get(year), force)]
            unanswered = None
            done: set[int] = set()
            for year in to_read:
                if year in done:
                    continue
                held = set(range(*_inclusive(REPORTS[report].years_of_file(year))))
                file_years = [asked for asked in to_read if asked in held]
                done.update(file_years)
                if unanswered is not None:
                    result.years_failed += file_years
                    continue
                reason, answered = await self._read(report, year)
                if reason is None:
                    result.years_read += file_years
                    continue
                result.years_failed += file_years
                result.reason = result.reason or reason
                if not answered:
                    unanswered = reason
            loads = await self._loads(report, years)
        if result.years_failed:
            result.status = FAILED
        elif result.years_read:
            result.status = FETCHED
        readings = [load.fetched_at for load in loads.values() if load.fetched_at]
        result.fetched_at = max(readings, default=None)
        result.first_day, result.last_day = await self.stored_range(report)
        return result

    async def ensure(self, report: str, start: date, end: date | None = None) -> ReportLoad:
        """Read the years of a period that need it, before the stored lines are read."""
        end = end or self.today()
        return await self.refresh(report, start.year, end.year)

    async def _loads(self, report: str, years: Iterable[int]) -> dict[int, CotLoad]:
        years = list(years)
        if not years:
            return {}
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(CotLoad).where(CotLoad.report == report, CotLoad.year.in_(years))
            )
            return {load.year: load for load in rows}

    async def _read(self, report: str, year: int) -> tuple[str | None, bool]:
        """Read the file of a year and store it: the reason when it could not be done, and
        whether the CFTC answered (a file it sent that cannot be read is an answer)."""
        answered = False
        try:
            cot_file = await self.read_file(report, year)
        except CftcError as error:
            reason, answered = f"CftcError: {error}", True
        except Exception as error:  # network, unexpected failure
            reason = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        else:
            if cot_file is None:
                reason = "the CFTC did not answer"
            else:
                return await self._store(cot_file), True
        logger.warning("CFTC %s report of %s not read: %s", report, year, reason)
        return reason, answered

    async def _store(self, cot_file: CotFile) -> str | None:
        report = cot_file.report
        fetched_at = _utc(self.now())
        first, last = date(cot_file.first_year, 1, 1), date(cot_file.last_year, 12, 31)
        in_file = (CotWeek.report == report, CotWeek.report_date.between(first, last))
        weeks = [
            {
                "report": report,
                "market_code": week.code,
                "report_date": week.report_date,
                "market_name": week.name,
                "contract_units": week.contract_units,
                "open_interest": week.open_interest,
                "fetched_at": fetched_at,
            }
            for week in cot_file.weeks
        ]
        positions = [
            {
                "report": report,
                "market_code": week.code,
                "report_date": week.report_date,
                "category": position.category,
                "long": position.long,
                "short": position.short,
                "spreading": position.spreading,
            }
            for week in cot_file.weeks
            for position in week.positions
        ]
        async with self.session_factory() as session:
            async with session.begin():
                if not weeks:
                    stored = await session.scalar(select(func.count()).where(*in_file))
                    if stored:
                        reason = (
                            f"the CFTC gave no line in {cot_file.file}; "
                            f"the {stored} lines stored are kept"
                        )
                        logger.warning("CFTC %s report: %s", report, reason)
                        return reason
                # The positions of the lines go with them (ON DELETE CASCADE).
                await session.execute(delete(CotWeek).where(*in_file))
                for model, rows in ((CotWeek, weeks), (CotPosition, positions)):
                    for chunk in range(0, len(rows), _CHUNK):
                        await session.execute(insert(model).values(rows[chunk : chunk + _CHUNK]))
                for year in range(cot_file.first_year, cot_file.last_year + 1):
                    days = [w.report_date for w in cot_file.weeks if w.report_date.year == year]
                    await session.merge(
                        CotLoad(
                            report=report,
                            year=year,
                            file=cot_file.file,
                            fetched_at=fetched_at,
                            weeks=len(days),
                            first_day=min(days, default=None),
                            last_day=max(days, default=None),
                        )
                    )
        return None

    async def stored_range(self, report: str) -> tuple[date | None, date | None]:
        """The first and the last report date stored for a report."""
        async with self.session_factory() as session:
            first, last = (
                await session.execute(
                    select(func.min(CotWeek.report_date), func.max(CotWeek.report_date)).where(
                        CotWeek.report == report_of(report)
                    )
                )
            ).one()
        return first, last

    async def markets(self, report: str | None = None) -> list[StoredMarket]:
        """The markets stored, by report then code, each with its latest name."""
        dates = select(
            CotWeek.report,
            CotWeek.market_code,
            func.min(CotWeek.report_date),
            func.max(CotWeek.report_date),
            func.count(),
        ).group_by(CotWeek.report, CotWeek.market_code)
        latest = (
            select(CotWeek.report, CotWeek.market_code, CotWeek.market_name, CotWeek.contract_units)
            .distinct(CotWeek.report, CotWeek.market_code)
            .order_by(CotWeek.report, CotWeek.market_code, CotWeek.report_date.desc())
        )
        if report is not None:
            dates = dates.where(CotWeek.report == report_of(report))
            latest = latest.where(CotWeek.report == report)
        async with self.session_factory() as session:
            spans = {
                (r, c): (first, last, count)
                for r, c, first, last, count in (await session.execute(dates)).all()
            }
            names = (await session.execute(latest)).all()
        return [
            StoredMarket(r, c, name, units, *spans[(r, c)])
            for r, c, name, units in sorted(names, key=lambda row: (row[0], row[1]))
        ]

    async def history(
        self, report: str, code: str, start: date | None = None, end: date | None = None
    ) -> list[StoredWeek]:
        """The stored lines of a market in a report, oldest first, with all their positions."""
        on_market = (CotWeek.report == report_of(report), CotWeek.market_code == code)
        lines = select(CotWeek.report_date, CotWeek.market_name, CotWeek.open_interest).where(
            *on_market
        )
        held = select(
            CotPosition.report_date,
            CotPosition.category,
            CotPosition.long,
            CotPosition.short,
            CotPosition.spreading,
        ).where(CotPosition.report == report, CotPosition.market_code == code)
        if start is not None:
            lines = lines.where(CotWeek.report_date >= start)
            held = held.where(CotPosition.report_date >= start)
        if end is not None:
            lines = lines.where(CotWeek.report_date <= end)
            held = held.where(CotPosition.report_date <= end)
        async with self.session_factory() as session:
            weeks = (await session.execute(lines.order_by(CotWeek.report_date))).all()
            rows = (await session.execute(held)).all()
        by_date: dict[date, dict[str, StoredPosition]] = {}
        for day, category, long, short, spreading in rows:
            by_date.setdefault(day, {})[category] = StoredPosition(long, short, spreading)
        order = [known.code for known in REPORTS[report].categories]
        return [
            StoredWeek(
                day,
                name,
                open_interest,
                {
                    category: by_date[day][category]
                    for category in order
                    if category in by_date.get(day, {})
                },
            )
            for day, name, open_interest in weeks
        ]


def _inclusive(bounds: tuple[int, int]) -> tuple[int, int]:
    return bounds[0], bounds[1] + 1
