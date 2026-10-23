"""The annual statements of the ESEF filings, kept in ``esef_filings`` and ``esef_facts``.

For an instrument whose LEI is verified (``financials/esef_lei.py``), the filings of
the company are listed from filings.xbrl.org, read, and their numbers stored: every
total of a filing, one value per concept, unit and period, the one of the latest
filing (``report_end``), so that a restatement replaces the first figure. The
annual statements are then read by ``financials/xbrl_mapping.py``, as for the SEC.

* **One filing per period is read.** The index can hold one filing per language
  (TotalEnergies files in French and in English) or two versions (BNP Paribas,
  ``-0`` and ``-1``): of the filings listed at the same period, only the one the
  index processed last is read; the others are kept in the index, unread.
* **A filing is read once.** A filing read, refused or without xBRL-JSON is not
  read again, unless its file changed (``sha256``) or ``force`` asks for it; a
  filing not given (network, server) is asked again at the next reading.
* **The true period of a filing** is the latest period of its facts (``report_end``),
  never the period of the index, which can be wrong (Pernod Ricard).
* A company read less than ``ESEF_REFRESH_HOURS`` hours ago is not asked again;
  a failed reading keeps what was stored.

Only the periods of a fiscal year (350 to 380 days) and the balances make the
annual statements: the half-years and quarters that some companies file (Novo
Nordisk) are stored but left out.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.esef_lei import LeiResolution
from financials.providers.esef import EsefError, EsefReport, Filing
from financials.xbrl_mapping import (
    FISCAL_YEAR_DAYS,
    MAPPED_CONCEPTS,
    FactRow,
    FiscalYear,
    annual_statements,
)
from models import EsefEntityLoad, EsefFiling
from models import EsefFact as StoredFact
from settings import env_int

logger = logging.getLogger(__name__)

ESEF_FORM = "ESEF"
# Earnings per share whose ratio to the profit is this many times the one of the other
# years: an error of scale in the filing (AB InBev tagged 265 for 2.65 in 2023).
PER_SHARE_COLUMNS = ("eps_basic", "eps_diluted")
SCALE_GAP = Decimal(20)
# Earnings per share below it are not judged: rounded to the cent, their ratio to the
# profit of a year near break-even can be wrong by more than SCALE_GAP.
MIN_PER_SHARE = Decimal("0.10")
# The facts at a date needed for it to be the period of a filing: one fact dated after
# the closing (shares at the date of the meeting) does not make it.
REPORT_END_FACTS = 10
_CHUNK = 2000

READ = "read"
REFUSED = "refused"
MISSING = "missing"
# The reason kept on a filing the site did not give: its status stays empty, so that it
# is asked again at the next listing of the index, not at every answer in between.
NOT_GIVEN = "not given by filings.xbrl.org"


class LeiResolverPort(Protocol):
    async def resolve(self, asset_id: int, *, refresh: bool = False) -> LeiResolution: ...


class EsefFilingsPort(Protocol):
    async def filings(self, lei: str) -> list[Filing] | None: ...

    async def report(self, filing: Filing) -> EsefReport | None: ...


@dataclass(frozen=True)
class StoredFiling:
    """What the database holds about a filing: enough to decide whether to read it."""

    fxo_id: str
    sha256: str | None
    read_status: str | None


@dataclass(frozen=True)
class EsefLoad:
    """What the database holds for a company, and what the last reading did."""

    status: str  # fetched, fresh, failed, no_lei
    lei: str | None = None
    entity_name: str | None = None
    fetched_at: datetime | None = None
    filings: int = 0
    last_report_end: date | None = None
    facts: int = 0
    read: tuple[str, ...] = ()
    refused: dict[str, str] = field(default_factory=dict)
    reason: str | None = None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def filings_to_read(
    filings: Iterable[Filing], stored: dict[str, StoredFiling], *, force: bool = False
) -> list[Filing]:
    """The filings to read: per period of the index, the one it processed last.

    A filing already read (or refused, or without xBRL-JSON) is read again only when
    its file changed or ``force`` asks for it. Oldest period first, so that a later
    filing replaces the values of an earlier one.
    """
    chosen: dict[date | str, Filing] = {}
    for filing in filings:
        period = filing.listed_period_end or filing.fxo_id
        known = chosen.get(period)
        if known is None or _processing_order(filing) > _processing_order(known):
            chosen[period] = filing
    wanted = []
    for filing in chosen.values():
        before = stored.get(filing.fxo_id)
        unchanged = before is not None and before.sha256 == filing.sha256
        if force or before is None or before.read_status is None or not unchanged:
            wanted.append(filing)
    return sorted(wanted, key=lambda f: (f.listed_period_end or date.min, f.fxo_id))


def _processing_order(filing: Filing) -> tuple:
    processed = filing.processed or datetime.min.replace(tzinfo=UTC)
    return (processed, filing.fxo_id)


def report_end_of(report: EsefReport) -> date:
    """The period of a filing: the latest date of at least ``REPORT_END_FACTS`` facts.

    The latest date of all would let a single fact dated after the closing pass for the
    period, and the filings of the next years could no longer replace its figures.
    """
    counts = Counter(fact.period_end for fact in report.facts)
    for end in report.period_ends:
        if counts[end] >= REPORT_END_FACTS:
            return end
    return report.period_ends[0]


def is_annual(duration_days: int) -> bool:
    """A balance at a date (0 days) or a fiscal year: what the annual statements read."""
    return duration_days == 0 or FISCAL_YEAR_DAYS[0] <= duration_days <= FISCAL_YEAR_DAYS[1]


def fact_row(fact: StoredFact) -> FactRow:
    """A stored ESEF value as the mapping of ``financials/xbrl_mapping.py`` reads it.

    The filing (``fxo_id``) stands for the accession of the SEC, and the true period of
    the filing (``report_end``) for its date of filing: the latest filing wins.
    """
    return FactRow(
        cik=fact.lei,
        taxonomy=fact.taxonomy,
        concept=fact.concept,
        unit=fact.unit,
        period_end=fact.period_end,
        duration_days=fact.duration_days,
        period_start=fact.period_start,
        value=fact.value,
        accession=fact.fxo_id,
        form=ESEF_FORM,
        fiscal_year=None,
        fiscal_period=None,
        filed=fact.report_end,
        frame=None,
    )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def check_per_share(years: list[FiscalYear]) -> list[FiscalYear]:
    """Leave out the earnings per share of a year whose scale the other years refute.

    Earnings per share divided by the profit is about one over the number of shares,
    which changes slowly. A year whose ratio is ``SCALE_GAP`` times larger or smaller
    than the median of the other years (three at least) holds an error of scale: the
    figure is left out and the year says why in ``warnings``. Earnings per share below
    ``MIN_PER_SHARE`` are neither judged nor used to judge.
    """
    for column in PER_SHARE_COLUMNS:
        ratios = {
            year.period_end: abs(year.value(column) / year.value("net_income"))
            for year in years
            if year.value(column)
            and abs(year.value(column)) >= MIN_PER_SHARE
            and year.value("net_income")
        }
        if len(ratios) < 4:
            continue
        for year in years:
            ratio = ratios.get(year.period_end)
            if ratio is None:
                continue
            others = _median([value for end, value in ratios.items() if end != year.period_end])
            gap = max(ratio / others, others / ratio)
            if gap >= SCALE_GAP:
                figure = year.figures.pop(column)
                year.warnings.append(
                    f"{column} {figure.value.normalize()} left out: its ratio to the net income "
                    f"is {gap:.0f} times the one of the other years (an error of scale in the "
                    "filing)"
                )
    return years


def _reason(error: Exception) -> str:
    message = str(error).splitlines()[0] if str(error) else ""
    text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return text[:500]


class EsefStatements:
    """Annual statements of the ESEF filings for the instruments of the catalogue."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: EsefFilingsPort,
        lei_resolver: LeiResolverPort,
        *,
        now=lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.lei_resolver = lei_resolver
        self._now = now
        self.refresh_after = timedelta(
            hours=env_int("ESEF_REFRESH_HOURS", 24, minimum=1, maximum=24 * 30)
        )

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        fetched_at = _aware(fetched_at)
        return fetched_at is not None and self._now() - fetched_at < self.refresh_after

    async def refresh(
        self, asset_id: int, force: bool = False, *, recheck_lei: bool = False
    ) -> EsefLoad:
        """Read the filings of the company of an instrument, unless read recently.

        A company read less than ``ESEF_REFRESH_HOURS`` ago is not listed again, but
        its filings still to read (never given, or set to be read again) are read.
        ``force`` lists the filings again and reads every one of them again;
        ``recheck_lei`` asks GLEIF for the LEI again.
        """
        resolution = await self.lei_resolver.resolve(asset_id, refresh=recheck_lei)
        if not resolution.found:
            return EsefLoad("no_lei", reason=resolution.reason)
        lei = resolution.lei
        load = await self._load(lei)
        if load is not None and not force and self._is_recent(load.fetched_at):
            known = await self._known_filings(lei)
            stored = {
                filing.fxo_id: StoredFiling(filing.fxo_id, filing.sha256, status)
                for filing, status, _ in known
            }
            reasons = {filing.fxo_id: reason for filing, _, reason in known}
            # The filings the site did not give wait for the next listing of the index.
            pending = [
                filing
                for filing in filings_to_read([filing for filing, _, _ in known], stored)
                if reasons.get(filing.fxo_id) != NOT_GIVEN
            ]
            if not pending:
                return self._as_load("fresh", load)
            return await self._read_all(
                lei, resolution, load, [filing for filing, _, _ in known], pending, listed=False
            )

        try:
            filings = await self.provider.filings(lei)
            if filings is None:
                raise EsefError("filings.xbrl.org did not answer")
        except EsefError as error:
            reason = _reason(error)
            logger.warning("ESEF filings of %s not listed: %s", lei, reason)
            return self._as_load("failed", load, lei=lei, reason=reason)

        stored = await self._list(lei, filings)
        pending = filings_to_read(filings, stored, force=force)
        return await self._read_all(lei, resolution, load, filings, pending, listed=True)

    async def _read_all(
        self,
        lei: str,
        resolution: LeiResolution,
        load: EsefEntityLoad | None,
        filings: list[Filing],
        pending: list[Filing],
        *,
        listed: bool,
    ) -> EsefLoad:
        """Read the pending filings and record the reading of the company.

        ``listed`` says whether the index was read now: if not, the time of the last
        listing is kept, so that the index is still listed again once it is old.
        """
        read, refused, not_given = [], {}, []
        for filing in pending:
            outcome = await self._read(filing)
            if outcome == READ:
                read.append(filing.fxo_id)
            elif outcome is None:
                not_given.append(filing.fxo_id)
            elif outcome != MISSING:
                refused[filing.fxo_id] = outcome

        fetched_at = self._now() if listed or load is None else load.fetched_at
        async with self.session_factory() as session:
            async with session.begin():
                count = (
                    await session.execute(
                        select(func.count()).select_from(StoredFact).where(StoredFact.lei == lei)
                    )
                ).scalar_one()
                last_report_end = (
                    await session.execute(
                        select(func.max(EsefFiling.report_end)).where(
                            EsefFiling.lei == lei, EsefFiling.read_status == READ
                        )
                    )
                ).scalar_one()
                entity_name = resolution.legal_name or (load.entity_name if load else None)
                await session.merge(
                    EsefEntityLoad(
                        lei=lei,
                        entity_name=entity_name,
                        fetched_at=fetched_at,
                        filings=len(filings),
                        last_report_end=last_report_end,
                        facts=count,
                    )
                )
        status, reason = "fetched", None
        if not_given:
            status, reason = "failed", f"not given by filings.xbrl.org: {', '.join(not_given)}"
        return EsefLoad(
            status,
            lei,
            entity_name,
            fetched_at,
            len(filings),
            last_report_end,
            count,
            read=tuple(read),
            refused=refused,
            reason=reason,
        )

    async def annual(
        self, asset_id: int, *, refresh: bool = True
    ) -> tuple[EsefLoad, list[FiscalYear]]:
        """The fiscal years of the company of an instrument, most recent first.

        The filings are read first when they are missing or old (``refresh``); when
        that fails, the stored values are used.
        """
        if refresh:
            load = await self.refresh(asset_id)
        else:
            resolution = await self.lei_resolver.resolve(asset_id, refresh=False)
            if not resolution.found:
                return EsefLoad("no_lei", reason=resolution.reason), []
            stored = await self._load(resolution.lei)
            if stored is None:
                load = EsefLoad("failed", resolution.lei, reason="never read from filings.xbrl.org")
            else:
                load = self._as_load("fresh", stored)
        if load.lei is None:
            return load, []
        return load, check_per_share(annual_statements(await self.stored_rows(load.lei)))

    async def stored_rows(self, lei: str) -> list[FactRow]:
        """The stored values of a company that make annual statements.

        Only the concepts of the columns, for a fiscal year or at a date: the other
        values stay stored, for a later mapping.
        """
        async with self.session_factory() as session:
            facts = (
                (
                    await session.execute(
                        select(StoredFact).where(
                            StoredFact.lei == lei,
                            tuple_(StoredFact.taxonomy, StoredFact.concept).in_(
                                sorted(MAPPED_CONCEPTS)
                            ),
                            or_(
                                StoredFact.duration_days == 0,
                                StoredFact.duration_days.between(*FISCAL_YEAR_DAYS),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [fact_row(fact) for fact in facts]

    async def filings(self, lei: str) -> list[EsefFiling]:
        """The filings of a company in the index, the latest period first."""
        async with self.session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(EsefFiling)
                        .where(EsefFiling.lei == lei)
                        .order_by(
                            EsefFiling.listed_period_end.desc().nulls_last(),
                            EsefFiling.fxo_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

    # ── Storage ──────────────────────────────────────────────────────────────

    async def _known_filings(self, lei: str) -> list[tuple[Filing, str | None, str | None]]:
        """The filings of a company as last listed, with their reading status and reason."""
        rows = await self.filings(lei)
        return [
            (
                Filing(
                    fxo_id=row.fxo_id,
                    lei=row.lei,
                    listed_period_end=row.listed_period_end,
                    country=row.country,
                    json_url=row.json_url,
                    viewer_url=row.viewer_url,
                    report_url=row.report_url,
                    package_url=row.package_url,
                    error_count=row.error_count,
                    warning_count=row.warning_count,
                    inconsistency_count=row.inconsistency_count,
                    added=_aware(row.added),
                    processed=_aware(row.processed),
                    sha256=row.sha256,
                ),
                row.read_status,
                row.read_reason,
            )
            for row in rows
        ]

    async def _load(self, lei: str) -> EsefEntityLoad | None:
        async with self.session_factory() as session:
            return await session.get(EsefEntityLoad, lei)

    @staticmethod
    def _as_load(
        status: str, load: EsefEntityLoad | None, *, lei: str | None = None, reason=None
    ) -> EsefLoad:
        if load is None:
            return EsefLoad(status, lei, reason=reason)
        return EsefLoad(
            status,
            load.lei,
            load.entity_name,
            load.fetched_at,
            load.filings,
            load.last_report_end,
            load.facts,
            reason=reason,
        )

    async def _list(self, lei: str, filings: list[Filing]) -> dict[str, StoredFiling]:
        """Store the index (what it says of each filing); return what was known before."""
        now = self._now()
        async with self.session_factory() as session:
            async with session.begin():
                before = {
                    row.fxo_id: StoredFiling(row.fxo_id, row.sha256, row.read_status)
                    for row in (
                        await session.execute(select(EsefFiling).where(EsefFiling.lei == lei))
                    )
                    .scalars()
                    .all()
                }
                for filing in filings:
                    listed = {
                        "fxo_id": filing.fxo_id,
                        "lei": filing.lei,
                        "listed_period_end": filing.listed_period_end,
                        "country": filing.country,
                        "json_url": filing.json_url,
                        "viewer_url": filing.viewer_url,
                        "report_url": filing.report_url,
                        "package_url": filing.package_url,
                        "error_count": filing.error_count,
                        "warning_count": filing.warning_count,
                        "inconsistency_count": filing.inconsistency_count,
                        "added": filing.added,
                        "processed": filing.processed,
                        "sha256": filing.sha256,
                        "listed_at": now,
                    }
                    statement = insert(EsefFiling).values(**listed)
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["fxo_id"],
                            set_={
                                key: statement.excluded[key] for key in listed if key != "fxo_id"
                            },
                        )
                    )
        return before

    async def _read(self, filing: Filing) -> str | None:
        """Read a filing and store its values: ``read``, ``missing``, the reason it was
        refused, or ``None`` when it was not given (asked again next time)."""
        if filing.json_url is None:
            await self._mark(filing.fxo_id, MISSING, "no xBRL-JSON in the index")
            return MISSING
        try:
            report = await self.provider.report(filing)
        except EsefError as error:
            reason = _reason(error)
            logger.warning("ESEF filing %s refused: %s", filing.fxo_id, reason)
            await self._mark(filing.fxo_id, REFUSED, reason)
            return reason
        if report is None:
            await self._mark(filing.fxo_id, None, NOT_GIVEN)
            return None
        if not report.facts:
            reason = "no number in the filing"
            await self._mark(filing.fxo_id, REFUSED, reason)
            return reason
        await self._store(filing, report)
        return READ

    async def _mark(self, fxo_id: str, status: str | None, reason: str | None) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(EsefFiling, fxo_id)
                row.read_at = self._now()
                row.read_status = status
                row.read_reason = reason

    async def _store(self, filing: Filing, report: EsefReport) -> None:
        """Insert the values; a stored value is replaced only by one of a filing as late."""
        report_end = report_end_of(report)
        values = [
            {
                "lei": filing.lei,
                "taxonomy": fact.prefix,
                "concept": fact.name,
                "unit": fact.unit,
                "period_end": fact.period_end,
                "duration_days": fact.duration_days,
                "period_start": fact.period_start,
                "value": fact.value,
                "decimals": fact.decimals,
                "fxo_id": filing.fxo_id,
                "report_end": report_end,
            }
            for fact in report.facts
        ]
        async with self.session_factory() as session:
            async with session.begin():
                for start in range(0, len(values), _CHUNK):
                    statement = insert(StoredFact).values(values[start : start + _CHUNK])
                    excluded = statement.excluded
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[
                                "lei",
                                "taxonomy",
                                "concept",
                                "unit",
                                "period_end",
                                "duration_days",
                            ],
                            set_={
                                "period_start": excluded.period_start,
                                "value": excluded.value,
                                "decimals": excluded.decimals,
                                "fxo_id": excluded.fxo_id,
                                "report_end": excluded.report_end,
                            },
                            where=excluded.report_end >= StoredFact.report_end,
                        )
                    )
                row = await session.get(EsefFiling, filing.fxo_id)
                row.read_at = self._now()
                row.read_status = READ
                row.read_reason = None
                row.report_end = report_end
                row.facts = len(report.facts)
                row.breakdowns = report.breakdowns
                row.conflicts = len(report.conflicts)
