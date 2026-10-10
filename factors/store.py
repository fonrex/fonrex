"""Factor returns kept in the database, and their refresh from the library.

A file of the library is stored whole (``factor_returns``), replacing what was
there: the library revises past values and its files are small (the longest,
US daily 3 factors since 1926, is about 100 000 values). ``factor_dataset_loads``
records each download; a file read less than ``FACTORS_REFRESH_DAYS`` days ago is
not asked again, as the library is updated about once a month.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Literal

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from factors.french_library import DATASETS, FactorTable, Frequency, download_factor_table
from models import FactorDatasetLoad, FactorReturn
from settings import env_int

logger = logging.getLogger(__name__)

# Rows inserted per statement: 5 parameters each, under the PostgreSQL limit.
_CHUNK = 5000

# fetched: read now; fresh: read recently; stale: read long ago; failed: the download failed.
LoadStatus = Literal["fetched", "fresh", "stale", "failed"]
Downloader = Callable[[str, Frequency], Awaitable[FactorTable]]


@dataclass(frozen=True)
class FactorLoad:
    """What the database holds for one file, and what the last refresh did."""

    dataset: str
    frequency: str
    status: LoadStatus
    fetched_at: datetime | None = None
    first_period: date | None = None
    last_period: date | None = None
    periods: int = 0
    source_note: str | None = None
    reason: str | None = None


def _known(dataset: str, frequency: str) -> None:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown factor dataset {dataset!r}: {', '.join(DATASETS)}")
    if frequency not in ("monthly", "daily"):
        raise ValueError(f"Unknown frequency {frequency!r}: monthly or daily")


async def replace_table(
    session: AsyncSession, dataset: str, frequency: str, table: FactorTable, fetched_at: datetime
) -> None:
    """Store a whole file, in place of what the database held for it."""
    await session.execute(
        delete(FactorReturn).where(
            FactorReturn.dataset == dataset, FactorReturn.frequency == frequency
        )
    )
    rows = [
        {
            "dataset": dataset,
            "frequency": frequency,
            "period_end": period,
            "factor": factor,
            "value": value,
        }
        for period, values in table.rows.items()
        for factor, value in values.items()
    ]
    for start in range(0, len(rows), _CHUNK):
        await session.execute(insert(FactorReturn), rows[start : start + _CHUNK])
    await session.execute(
        delete(FactorDatasetLoad).where(
            FactorDatasetLoad.dataset == dataset, FactorDatasetLoad.frequency == frequency
        )
    )
    session.add(
        FactorDatasetLoad(
            dataset=dataset,
            frequency=frequency,
            fetched_at=fetched_at,
            first_period=table.first_period,
            last_period=table.last_period,
            periods=len(table.rows),
            source_note=table.note,
        )
    )


async def read_load(
    session: AsyncSession, dataset: str, frequency: str
) -> FactorDatasetLoad | None:
    return await session.get(FactorDatasetLoad, (dataset, frequency))


async def read_series(
    session: AsyncSession,
    dataset: str,
    frequency: str,
    start: date | None = None,
    end: date | None = None,
) -> dict[date, dict[str, Decimal]]:
    """Stored values of one file, by period end then factor, oldest period first."""
    statement = select(FactorReturn.period_end, FactorReturn.factor, FactorReturn.value).where(
        FactorReturn.dataset == dataset, FactorReturn.frequency == frequency
    )
    if start is not None:
        statement = statement.where(FactorReturn.period_end >= start)
    if end is not None:
        statement = statement.where(FactorReturn.period_end <= end)
    statement = statement.order_by(FactorReturn.period_end, FactorReturn.factor)
    series: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for period, factor, value in (await session.execute(statement)).all():
        series[period][factor] = value
    return dict(series)


def _as_load(row: FactorDatasetLoad, status: LoadStatus, reason: str | None = None) -> FactorLoad:
    return FactorLoad(
        dataset=row.dataset,
        frequency=row.frequency,
        status=status,
        fetched_at=row.fetched_at,
        first_period=row.first_period,
        last_period=row.last_period,
        periods=row.periods,
        source_note=row.source_note,
        reason=reason,
    )


class FactorLibrary:
    """The factor returns of the library, stored and refreshed in the database."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        downloader: Downloader = download_factor_table,
    ):
        self.session_factory = session_factory
        self.downloader = downloader
        self.refresh_after = timedelta(
            days=env_int("FACTORS_REFRESH_DAYS", 7, minimum=1, maximum=90)
        )

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < self.refresh_after

    async def status(self, dataset: str, frequency: Frequency) -> FactorLoad | None:
        """What the database holds for one file: ``fresh`` when read recently, else ``stale``."""
        _known(dataset, frequency)
        async with self.session_factory() as session:
            row = await read_load(session, dataset, frequency)
        if row is None:
            return None
        return _as_load(row, "fresh" if self._is_recent(row.fetched_at) else "stale")

    async def refresh(self, dataset: str, frequency: Frequency, force: bool = False) -> FactorLoad:
        """Download one file and store it, unless it was read recently.

        A failed download leaves the stored values as they are: the answer then
        says ``failed`` with the reason, and what the database still holds.
        """
        _known(dataset, frequency)
        async with self.session_factory() as session:
            stored = await read_load(session, dataset, frequency)
        if stored is not None and not force and self._is_recent(stored.fetched_at):
            return _as_load(stored, "fresh")

        try:
            table = await self.downloader(dataset, frequency)
        except Exception as error:  # network, HTTP status, unreadable file
            message = str(error).splitlines()[0] if str(error) else ""
            reason = f"{type(error).__name__}: {message}" if message else type(error).__name__
            logger.warning("Factor file %s/%s not refreshed: %s", dataset, frequency, reason)
            if stored is not None:
                return _as_load(stored, "failed", reason)
            return FactorLoad(dataset, frequency, "failed", reason=reason)

        fetched_at = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            async with session.begin():
                await replace_table(session, dataset, frequency, table, fetched_at)
        logger.info(
            "Factor file %s/%s stored: %s periods up to %s",
            dataset,
            frequency,
            len(table.rows),
            table.last_period,
        )
        return FactorLoad(
            dataset=dataset,
            frequency=frequency,
            status="fetched",
            fetched_at=fetched_at,
            first_period=table.first_period,
            last_period=table.last_period,
            periods=len(table.rows),
            source_note=table.note,
        )

    async def series(
        self,
        dataset: str,
        frequency: Frequency,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[date, dict[str, Decimal]]:
        """Stored values of one file, between ``start`` and ``end`` included."""
        _known(dataset, frequency)
        async with self.session_factory() as session:
            return await read_series(session, dataset, frequency, start, end)
