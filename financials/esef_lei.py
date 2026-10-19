"""Find, verify and remember the Legal Entity Identifier (LEI) of an instrument.

The ESEF filings are kept by LEI (``financials/providers/esef.py``). The LEI of an
instrument is asked to GLEIF with its **ISIN**, never with a ticker: ``AIR.PA`` is
Airbus in Paris and ``AIR`` is AAR Corp in New York, while ``NL0000235190`` is
one security, issued by one company.

The LEI is still verified, never taken as given:

* an instrument that files no statements (ETF, fund, index, currency…) and an
  instrument without ISIN are not asked about;
* GLEIF must give an entity for the ISIN, and its **name** (its legal name, or
  one of its other names) must be the name of the instrument in the catalogue,
  the legal forms left out (``Airbus SE`` is ``AIRBUS SE``, ``Novo Nordisk A/S`` is
  ``NOVO NORDISK A/S``); an ISIN given to several entities named alike is refused;
* the LEI is kept in the ``LEI`` mapping of the **instrument** (no listing): every
  listing shares it, as the filings are those of the company. A LEI set by hand
  is trusted. An instrument for which nothing was found is not asked again before
  ``RETRY_AFTER``; when GLEIF does not answer, nothing is remembered.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financials.providers.esef import EsefError, LeiRecord
from financials.sec_cik import NO_STATEMENTS_QUOTE_TYPES, same_company
from models import Asset, AssetMapping

logger = logging.getLogger(__name__)

LEI_PROVIDER = "LEI"
GLEIF_RECORD_URL = "https://search.gleif.org/#/record/{lei}"

# How the stored LEI was obtained (``asset_mappings.source``), as for the CIK.
FOUND_BY_ISIN = "isin_check"
SET_BY_HAND = "manual"
NOT_FOUND = "symbol_not_found"

# The LEI of an ISIN seldom changes: an instrument without LEI is asked again a month later.
RETRY_AFTER = timedelta(days=30)

_ISIN = re.compile(r"[A-Z]{2}[0-9A-Z]{9}[0-9]")
# European legal forms, written without their dots ("S.p.A." is "spa"), left out of
# the names before they are compared; the other forms are left out by ``same_company``.
_EUROPEAN_FORMS = frozenset(
    """sa se nv bv ag spa sca sas sarl srl asa as oyj abp ab publ kgaa gmbh plc""".split()
)


def _without_legal_forms(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = text.lower().replace("a/s", " ").replace(".", "")
    return " ".join(word for word in re.findall(r"[a-z0-9&]+", text) if word not in _EUROPEAN_FORMS)


def _contains(longer: list[str], shorter: list[str]) -> bool:
    size = len(shorter)
    return any(longer[at : at + size] == shorter for at in range(len(longer) - size + 1))


def _same_name(ours: str, theirs: str) -> bool:
    if same_company(ours, theirs):
        return True
    # A long legal name holding the short one: "Air Liquide" in "L'AIR LIQUIDE SOCIETE
    # ANONYME POUR L'ETUDE ET L'EXPLOITATION DES PROCEDES GEORGES CLAUDE". Two words at
    # least: one word alone ("Air") is in too many names.
    short, long = sorted((ours.split(), theirs.split()), key=len)
    return len(short) >= 2 and _contains(long, short)


def same_entity(catalogue_name: str | None, record: LeiRecord) -> bool:
    """Whether the instrument of the catalogue is the entity of GLEIF, by one of its names."""
    ours = _without_legal_forms(catalogue_name or "")
    return bool(ours) and any(
        _same_name(ours, _without_legal_forms(name))
        for name in (record.legal_name, *record.other_names)
    )


@dataclass(frozen=True)
class LeiResolution:
    """Outcome of a resolution: a verified LEI, or the reason there is none."""

    lei: str | None
    reason: str | None = None
    origin: str | None = None
    legal_name: str | None = None
    country: str | None = None

    @property
    def found(self) -> bool:
        return self.lei is not None


@dataclass(frozen=True)
class LeiInstrument:
    """What is checked on an instrument, and what is already stored for it."""

    asset_id: int
    name: str | None
    quote_type: str | None
    isin: str | None
    stored_lei: str | None = None
    stored_source: str | None = None
    stored_active: bool = False
    stored_verified: bool = False
    stored_at: datetime | None = None


class LeiStore(Protocol):
    async def load(self, asset_id: int) -> LeiInstrument | None: ...

    async def remember(self, asset_id: int, resolution: LeiResolution, now: datetime) -> None: ...


RecordsSource = Callable[[str], Awaitable[list[LeiRecord] | None]]


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


class LeiResolver:
    """Return the verified LEI of an instrument, asking GLEIF when it is unknown."""

    def __init__(
        self,
        store: LeiStore,
        records_source: RecordsSource,
        *,
        retry_after: timedelta = RETRY_AFTER,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._records_source = records_source
        self._retry_after = retry_after
        self._now = now

    async def resolve(self, asset_id: int, *, refresh: bool = False) -> LeiResolution:
        """Resolve the LEI of an instrument.

        ``refresh`` ignores what is stored (a verified LEI, or an earlier failure)
        and asks GLEIF again; a LEI set by hand is kept even then.
        """
        instrument = await self._store.load(asset_id)
        if instrument is None:
            return LeiResolution(None, f"Instrument {asset_id} does not exist")
        if (instrument.quote_type or "").strip().upper() in NO_STATEMENTS_QUOTE_TYPES:
            return LeiResolution(None, f"{instrument.quote_type} files no annual report")

        stored = self._stored(instrument, refresh)
        if stored is not None:
            return stored

        isin = (instrument.isin or "").strip().upper()
        if not _ISIN.fullmatch(isin):
            # Nothing was asked: nothing to remember either.
            return LeiResolution(None, "The instrument has no ISIN: GLEIF is not asked about it")
        try:
            records = await self._records_source(isin)
        except EsefError as error:
            logger.warning("GLEIF answer for %s not read: %s", isin, error)
            return LeiResolution(None, f"The answer of GLEIF could not be read: {error}")
        if records is None:
            return LeiResolution(None, "GLEIF did not answer")

        resolution = self._look_up(instrument, isin, records)
        await self._store.remember(asset_id, resolution, self._now())
        return resolution

    def _stored(self, instrument: LeiInstrument, refresh: bool) -> LeiResolution | None:
        if instrument.stored_active and instrument.stored_lei:
            if instrument.stored_source == SET_BY_HAND:
                return LeiResolution(instrument.stored_lei, origin=SET_BY_HAND)
            if instrument.stored_verified and not refresh:
                return LeiResolution(instrument.stored_lei, origin=instrument.stored_source)
        checked_at = _aware(instrument.stored_at)
        if not refresh and instrument.stored_source == NOT_FOUND and checked_at is not None:
            if self._now() - checked_at < self._retry_after:
                return LeiResolution(
                    None,
                    f"No LEI found for instrument {instrument.asset_id} "
                    f"(checked on {checked_at:%Y-%m-%d %H:%M} UTC)",
                    origin=NOT_FOUND,
                )
        return None

    @staticmethod
    def _look_up(instrument: LeiInstrument, isin: str, records: list[LeiRecord]) -> LeiResolution:
        if not records:
            return LeiResolution(None, f"GLEIF knows no entity for {isin}", origin=NOT_FOUND)
        if not instrument.name:
            return LeiResolution(
                None, "The instrument has no name: its LEI cannot be confirmed", origin=NOT_FOUND
            )
        matching = {
            record.lei: record for record in records if same_entity(instrument.name, record)
        }
        if len(matching) == 1:
            record = next(iter(matching.values()))
            return LeiResolution(
                record.lei,
                origin=FOUND_BY_ISIN,
                legal_name=record.legal_name,
                country=record.country,
            )
        if matching:
            names = ", ".join(f"{lei} ({r.legal_name})" for lei, r in sorted(matching.items()))
            return LeiResolution(
                None, f"Several entities at GLEIF for {isin}: {names}", origin=NOT_FOUND
            )
        offered = ", ".join(f"{r.lei} ({r.legal_name})" for r in records)
        return LeiResolution(
            None,
            f"No GLEIF entity named like {instrument.name!r} for {isin}; GLEIF gives {offered}",
            origin=NOT_FOUND,
        )


class SqlLeiStore:
    """The ``LEI`` mapping of an instrument in ``asset_mappings`` (asynchronous session)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _mapping_query(asset_id: int):
        return (
            select(AssetMapping)
            .where(
                AssetMapping.asset_id == asset_id,
                AssetMapping.provider_name == LEI_PROVIDER,
                AssetMapping.asset_listing_id.is_(None),
            )
            .order_by(AssetMapping.id)
        )

    async def load(self, asset_id: int) -> LeiInstrument | None:
        async with self._session_factory() as session:
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return None
            mapping = (await session.execute(self._mapping_query(asset_id))).scalars().first()
            return LeiInstrument(
                asset_id=asset.id,
                name=asset.name or asset.display_name,
                quote_type=asset.quote_type,
                isin=asset.isin,
                stored_lei=(mapping.provider_ticker or None) if mapping else None,
                stored_source=mapping.source if mapping else None,
                stored_active=bool(mapping.is_active) if mapping else False,
                stored_verified=bool(mapping and mapping.last_verified_at),
                stored_at=mapping.updated_at if mapping else None,
            )

    async def remember(self, asset_id: int, resolution: LeiResolution, now: datetime) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                mapping = (await session.execute(self._mapping_query(asset_id))).scalars().first()
                if mapping is None:
                    mapping = AssetMapping(
                        asset_id=asset_id,
                        asset_listing_id=None,
                        provider_name=LEI_PROVIDER,
                        failure_count=0,
                    )
                    session.add(mapping)
                if resolution.found:
                    mapping.provider_ticker = resolution.lei
                    mapping.provider_url = GLEIF_RECORD_URL.format(lei=resolution.lei)
                    mapping.source = resolution.origin
                    mapping.confidence_score = 1.0
                    mapping.is_active = True
                    mapping.failure_count = 0
                    mapping.last_verified_at = now
                else:
                    # A LEI that is no longer confirmed is switched off: nothing must use it.
                    mapping.source = NOT_FOUND
                    mapping.is_active = False
                    mapping.failure_count = (mapping.failure_count or 0) + 1
                    mapping.last_verified_at = None
                mapping.updated_at = now
