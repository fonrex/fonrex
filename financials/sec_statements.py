"""The annual statements a company filed with the SEC, kept in ``xbrl_facts``.

For an instrument whose CIK is verified (``financials/sec_cik.py``), the facts of
the company are read from the SEC (``companyfacts``), reduced to the concepts
Fonrex maps (``financials/xbrl_mapping.py``) and stored: one value per concept
and period, the one of the latest filing, so that a restatement replaces the
first figure. A company read less than ``SEC_XBRL_REFRESH_HOURS`` hours ago is
not asked again; a failed reading keeps what was stored.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.sec_cik import CikResolution
from financials.xbrl_mapping import FactRow, FiscalYear, XbrlError, annual_statements, facts_to_rows
from models import SecCompanyLoad, XbrlFact
from settings import env_int

logger = logging.getLogger(__name__)

_CHUNK = 2000


class CikResolverPort(Protocol):
    async def resolve(self, asset_id: int, *, refresh: bool = False) -> CikResolution: ...


class CompanyFactsPort(Protocol):
    async def company_facts(self, cik: str) -> dict | None: ...


@dataclass(frozen=True)
class SecLoad:
    """What the database holds for a company, and what the last reading did."""

    status: str  # fetched, fresh, failed, no_cik
    cik: str | None = None
    entity_name: str | None = None
    fetched_at: datetime | None = None
    last_filed: Any = None
    facts: int = 0
    reason: str | None = None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


class SecStatements:
    """Annual statements of the SEC for the instruments of the catalogue."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: CompanyFactsPort,
        cik_resolver: CikResolverPort,
        *,
        now=lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.provider = provider
        self.cik_resolver = cik_resolver
        self._now = now
        self.refresh_after = timedelta(
            hours=env_int("SEC_XBRL_REFRESH_HOURS", 24, minimum=1, maximum=24 * 30)
        )

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        fetched_at = _aware(fetched_at)
        return fetched_at is not None and self._now() - fetched_at < self.refresh_after

    async def refresh(self, asset_id: int, force: bool = False) -> SecLoad:
        """Read the facts of the company of an instrument, unless they were read recently."""
        resolution = await self.cik_resolver.resolve(asset_id, refresh=False)
        if not resolution.found:
            return SecLoad("no_cik", reason=resolution.reason)
        cik = resolution.cik
        load = await self._load(cik)
        if load is not None and not force and self._is_recent(load.fetched_at):
            return SecLoad(
                "fresh", cik, load.entity_name, load.fetched_at, load.last_filed, load.facts
            )

        try:
            answer = await self.provider.company_facts(cik)
            if answer is None:
                raise XbrlError("no answer from the SEC")
            rows = facts_to_rows(cik, answer)
        except Exception as error:  # network, refusal, unreadable answer
            message = str(error).splitlines()[0] if str(error) else ""
            reason = f"{type(error).__name__}: {message}" if message else type(error).__name__
            logger.warning("SEC facts of %s not read: %s", cik, reason)
            return SecLoad(
                "failed",
                cik,
                load.entity_name if load else None,
                load.fetched_at if load else None,
                load.last_filed if load else None,
                load.facts if load else 0,
                reason=reason,
            )

        fetched_at = self._now()
        entity_name = answer.get("entityName") if isinstance(answer, dict) else None
        await self._store(cik, rows)
        async with self.session_factory() as session:
            async with session.begin():
                count, last_filed = (
                    await session.execute(
                        select(func.count(), func.max(XbrlFact.filed)).where(XbrlFact.cik == cik)
                    )
                ).one()
                await session.merge(
                    SecCompanyLoad(
                        cik=cik,
                        entity_name=entity_name,
                        fetched_at=fetched_at,
                        last_filed=last_filed,
                        facts=count,
                    )
                )
        return SecLoad("fetched", cik, entity_name, fetched_at, last_filed, count)

    async def annual(
        self, asset_id: int, *, refresh: bool = True
    ) -> tuple[SecLoad, list[FiscalYear]]:
        """The fiscal years of the company of an instrument, most recent first.

        The facts are read from the SEC first when they are missing or old
        (``refresh``); when that fails, the stored ones are used.
        """
        if refresh:
            load = await self.refresh(asset_id)
        else:
            resolution = await self.cik_resolver.resolve(asset_id, refresh=False)
            if not resolution.found:
                return SecLoad("no_cik", reason=resolution.reason), []
            stored = await self._load(resolution.cik)
            load = SecLoad(
                "fresh" if stored else "failed",
                resolution.cik,
                stored.entity_name if stored else None,
                stored.fetched_at if stored else None,
                stored.last_filed if stored else None,
                stored.facts if stored else 0,
                reason=None if stored else "never read from the SEC",
            )
        if load.cik is None:
            return load, []
        return load, annual_statements(await self.stored_rows(load.cik))

    async def _load(self, cik: str) -> SecCompanyLoad | None:
        async with self.session_factory() as session:
            return await session.get(SecCompanyLoad, cik)

    async def stored_rows(self, cik: str) -> list[FactRow]:
        async with self.session_factory() as session:
            facts = (
                (await session.execute(select(XbrlFact).where(XbrlFact.cik == cik))).scalars().all()
            )
        return [
            FactRow(
                cik=fact.cik,
                taxonomy=fact.taxonomy,
                concept=fact.concept,
                unit=fact.unit,
                period_end=fact.period_end,
                duration_days=fact.duration_days,
                period_start=fact.period_start,
                value=fact.value,
                accession=fact.accession,
                form=fact.form,
                fiscal_year=fact.fiscal_year,
                fiscal_period=fact.fiscal_period,
                filed=fact.filed,
                frame=fact.frame,
            )
            for fact in facts
        ]

    async def _store(self, cik: str, rows: list[FactRow]) -> None:
        """Insert the facts; a stored fact is replaced only by one filed as late or later."""
        values = [asdict(row) for row in rows]
        async with self.session_factory() as session:
            async with session.begin():
                for start in range(0, len(values), _CHUNK):
                    statement = insert(XbrlFact).values(values[start : start + _CHUNK])
                    excluded = statement.excluded
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[
                                "cik",
                                "taxonomy",
                                "concept",
                                "unit",
                                "period_end",
                                "duration_days",
                            ],
                            set_={
                                "period_start": excluded.period_start,
                                "value": excluded.value,
                                "accession": excluded.accession,
                                "form": excluded.form,
                                "fiscal_year": excluded.fiscal_year,
                                "fiscal_period": excluded.fiscal_period,
                                "filed": excluded.filed,
                                "frame": excluded.frame,
                            },
                            where=excluded.filed >= XbrlFact.filed,
                        )
                    )
