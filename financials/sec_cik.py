"""Find, verify and remember the SEC identifier (CIK) of an instrument.

The SEC files its data by company, under a Central Index Key (CIK). A ticker of
the catalogue is not enough to find it: ``AIR.PA`` is Airbus in Paris, while
``AIR`` at the SEC is AAR Corp, a US company of the NYSE. A CIK found from the
wrong ticker would give the statements of another company without any error.

The CIK is therefore never guessed:

* only a **US listing** of the instrument is looked up: quoted in USD, on a US
  exchange (or on none named), with a ticker that names no foreign exchange.
  ``AIR.PA`` and ``SAP.DE`` are never sent to the SEC; SAP is found through its
  NYSE listing when the catalogue has one;
* the ticker must be in the table of the SEC (``company_tickers_exchange.json``)
  on a US exchange, and the **name** of the company there must be the name of
  the instrument in the catalogue;
* the CIK is kept in the ``SEC`` mapping of the **instrument** (no listing):
  every listing of the instrument shares it, as the filings are those of the
  company. An instrument for which nothing was found is not looked up again
  before ``RETRY_AFTER``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.ticker_suffix import PLACES
from models import Asset, AssetListing, AssetMapping

logger = logging.getLogger(__name__)

SEC_PROVIDER = "SEC"
EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"

# How the stored CIK was obtained (``asset_mappings.source``), as for Yahoo.
FOUND_BY_TICKER = "ticker_check"
SET_BY_HAND = "manual"
NOT_FOUND = "symbol_not_found"

# The table of the SEC changes slowly: an instrument without CIK is asked again a week later.
RETRY_AFTER = timedelta(days=7)
# Codes under which the catalogue may store a US exchange; empty when it names none.
US_EXCHANGES = frozenset(
    "XNYS XNAS XASE ARCX NYSE NASDAQ NYQ NMS NGM NCM AMEX ARCA BATS PCX OTC US".split()
) | {""}
# Exchanges of the SEC table that are US markets (``null`` is not one).
SEC_EXCHANGES = frozenset({"NASDAQ", "NYSE", "CBOE", "TXSE", "OTC"})
# Instruments that file no financial statements of their own.
NO_STATEMENTS_QUOTE_TYPES = frozenset(
    {"ETF", "MUTUALFUND", "FUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE"}
)
# Words that say what a company is, not which one: left out of the comparison of names.
_LEGAL_WORDS = frozenset(
    """inc incorporated corp corporation co company plc sa se ag nv bv ltd limited llc lp
    the class a b c adr ads sponsored ordinary shares share common stock new""".split()
)


class SecTableError(Exception):
    """The table of the SEC could not be read: nothing can be concluded."""


@dataclass(frozen=True)
class SecTicker:
    """A row of the table of the SEC: a ticker of a company on an exchange."""

    cik: int
    name: str
    ticker: str
    exchange: str | None


def parse_ticker_table(data: Any) -> dict[str, list[SecTicker]]:
    """The rows of ``company_tickers_exchange.json``, by ticker (upper case)."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise SecTableError("the table of the SEC has no 'data' rows")
    fields = data.get("fields") or ["cik", "name", "ticker", "exchange"]
    try:
        cik_at, name_at = fields.index("cik"), fields.index("name")
        ticker_at, exchange_at = fields.index("ticker"), fields.index("exchange")
    except ValueError as error:
        raise SecTableError(f"the table of the SEC has the fields {fields}") from error
    table: dict[str, list[SecTicker]] = {}
    for row in data["data"]:
        try:
            ticker = str(row[ticker_at] or "").strip().upper()
            entry = SecTicker(int(row[cik_at]), str(row[name_at] or ""), ticker, row[exchange_at])
        except (IndexError, TypeError, ValueError):
            continue
        if ticker:
            table.setdefault(ticker, []).append(entry)
    return table


def sec_symbol(ticker: str) -> str | None:
    """The ticker as the SEC writes it, or ``None`` when it names a foreign exchange.

    ``BRK.B`` is ``BRK-B`` at the SEC (the letter is a class of shares); ``MSFT.US``
    is ``MSFT``; ``AIR.PA`` names Paris and ``SAP.DE`` Xetra: neither is a US
    ticker, whatever the SEC lists under ``AIR`` or ``SAP``.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    base, dot, suffix = symbol.rpartition(".")
    if not dot:
        return symbol
    if suffix == "US" and base:
        return base
    if suffix in PLACES or not base:
        return None
    if len(suffix) == 1 and suffix.isalpha():
        return f"{base}-{suffix}"
    return None


def is_us_listing(ticker: str, exchange: str | None, currency: str | None) -> bool:
    """A listing quoted in USD on a US exchange (or on none named), with a US ticker."""
    return (
        (currency or "").strip().upper() == "USD"
        and (exchange or "").strip().upper() in US_EXCHANGES
        and sec_symbol(ticker) is not None
    )


def _name_words(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    return [word for word in words if word not in _LEGAL_WORDS]


def same_company(catalogue_name: str | None, sec_name: str | None) -> bool:
    """Whether the two names designate the same company.

    The legal forms and the classes of shares are left out (``Alphabet Inc Class A``
    is ``Alphabet Inc.``); the first word must be the same, and the names must
    share at least half of the words of the longer one: ``JPMorgan Chase & Co``
    is ``JPMORGAN CHASE & CO``; ``AAR Corp`` is not ``Airbus SE``, and
    ``Apple Hospitality REIT`` is not ``Apple Inc.``.
    """
    ours, theirs = _name_words(catalogue_name or ""), _name_words(sec_name or "")
    if not ours or not theirs or ours[0] != theirs[0]:
        return False
    shared = len(set(ours) & set(theirs))
    return shared / max(len(set(ours)), len(set(theirs))) >= 0.5


@dataclass(frozen=True)
class CikResolution:
    """Outcome of a resolution: a verified CIK (ten digits), or the reason there is none."""

    cik: str | None
    reason: str | None = None
    origin: str | None = None
    company_name: str | None = None

    @property
    def found(self) -> bool:
        return self.cik is not None


@dataclass(frozen=True)
class InstrumentListing:
    ticker: str
    exchange: str | None
    currency: str | None


@dataclass(frozen=True)
class Instrument:
    """What is checked on an instrument, and what is already stored for it."""

    asset_id: int
    name: str | None
    quote_type: str | None
    listings: tuple[InstrumentListing, ...]
    stored_cik: str | None = None
    stored_source: str | None = None
    stored_active: bool = False
    stored_verified: bool = False
    stored_at: datetime | None = None


class CikStore(Protocol):
    async def load(self, asset_id: int) -> Instrument | None: ...

    async def remember(self, asset_id: int, resolution: CikResolution, now: datetime) -> None: ...


def _cik(value: int | str) -> str:
    return str(int(value)).zfill(10)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


TableSource = Callable[[], Awaitable[Any]]


class SecCikResolver:
    """Return the verified CIK of an instrument, looking it up when unknown."""

    def __init__(
        self,
        store: CikStore,
        table_source: TableSource,
        *,
        retry_after: timedelta = RETRY_AFTER,
        table_lifetime: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._table_source = table_source
        self._retry_after = retry_after
        self._table_lifetime = table_lifetime
        self._now = now
        self._table: dict[str, list[SecTicker]] | None = None
        self._table_read_at: datetime | None = None

    async def resolve(self, asset_id: int, *, refresh: bool = False) -> CikResolution:
        """Resolve the CIK of an instrument.

        ``refresh`` ignores what is stored (a verified CIK, or an earlier failure)
        and reads the table of the SEC again.
        """
        instrument = await self._store.load(asset_id)
        if instrument is None:
            return CikResolution(None, f"Instrument {asset_id} does not exist")
        if (instrument.quote_type or "").strip().upper() in NO_STATEMENTS_QUOTE_TYPES:
            return CikResolution(
                None, f"{instrument.quote_type} files no financial statements with the SEC"
            )

        if not refresh:
            stored = self._stored(instrument)
            if stored is not None:
                return stored

        us_listings = [
            listing
            for listing in instrument.listings
            if is_us_listing(listing.ticker, listing.exchange, listing.currency)
        ]
        if not us_listings:
            # Nothing was asked: nothing to remember either.
            return CikResolution(
                None, "No US listing in the catalogue: the SEC is not asked about this instrument"
            )

        try:
            table = await self._ticker_table(refresh)
        except SecTableError as error:
            logger.warning("SEC table not read: %s", error)
            return CikResolution(None, f"The table of the SEC could not be read: {error}")

        resolution = self._look_up(instrument, us_listings, table)
        await self._store.remember(asset_id, resolution, self._now())
        return resolution

    def _stored(self, instrument: Instrument) -> CikResolution | None:
        trusted = instrument.stored_source == SET_BY_HAND or instrument.stored_verified
        if instrument.stored_active and instrument.stored_cik and trusted:
            return CikResolution(instrument.stored_cik, origin=instrument.stored_source)
        checked_at = _aware(instrument.stored_at)
        if instrument.stored_source == NOT_FOUND and checked_at is not None:
            if self._now() - checked_at < self._retry_after:
                return CikResolution(
                    None,
                    f"No CIK found for instrument {instrument.asset_id} "
                    f"(checked on {checked_at:%Y-%m-%d %H:%M} UTC)",
                    origin=NOT_FOUND,
                )
        return None

    async def _ticker_table(self, refresh: bool) -> dict[str, list[SecTicker]]:
        read_at = self._table_read_at
        if (
            not refresh
            and self._table is not None
            and read_at is not None
            and self._now() - read_at < self._table_lifetime
        ):
            return self._table
        data = await self._table_source()
        if data is None:
            raise SecTableError("no answer")
        self._table = parse_ticker_table(data)
        self._table_read_at = self._now()
        return self._table

    @staticmethod
    def _look_up(
        instrument: Instrument,
        us_listings: list[InstrumentListing],
        table: dict[str, list[SecTicker]],
    ) -> CikResolution:
        if not instrument.name:
            return CikResolution(
                None, "The instrument has no name: its CIK cannot be confirmed", origin=NOT_FOUND
            )
        seen: list[str] = []
        found: dict[str, SecTicker] = {}
        for listing in us_listings:
            symbol = sec_symbol(listing.ticker)
            for entry in table.get(symbol or "", []):
                exchange = (entry.exchange or "").upper()
                if exchange not in SEC_EXCHANGES:
                    seen.append(f"{entry.ticker}: {entry.name} (not on a US exchange)")
                elif not same_company(instrument.name, entry.name):
                    seen.append(f"{entry.ticker}: {entry.name}")
                else:
                    found.setdefault(_cik(entry.cik), entry)
        if len(found) == 1:
            cik, entry = next(iter(found.items()))
            return CikResolution(cik, origin=FOUND_BY_TICKER, company_name=entry.name)
        tickers = ", ".join(sorted({listing.ticker for listing in us_listings}))
        if len(found) > 1:
            names = ", ".join(f"{cik} ({entry.name})" for cik, entry in sorted(found.items()))
            return CikResolution(
                None, f"Several companies at the SEC for {tickers}: {names}", origin=NOT_FOUND
            )
        offered = f"; the SEC lists {', '.join(seen)}" if seen else ""
        return CikResolution(
            None,
            f"No SEC company named like {instrument.name!r} for {tickers}{offered}",
            origin=NOT_FOUND,
        )


class SqlCikStore:
    """The ``SEC`` mapping of an instrument in ``asset_mappings`` (asynchronous session)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _mapping_query(asset_id: int):
        return (
            select(AssetMapping)
            .where(
                AssetMapping.asset_id == asset_id,
                AssetMapping.provider_name == SEC_PROVIDER,
                AssetMapping.asset_listing_id.is_(None),
            )
            .order_by(AssetMapping.id)
        )

    async def load(self, asset_id: int) -> Instrument | None:
        async with self._session_factory() as session:
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return None
            listings = (
                await session.execute(
                    select(AssetListing.ticker, AssetListing.exchange, AssetListing.currency).where(
                        AssetListing.asset_id == asset_id, AssetListing.is_active.is_(True)
                    )
                )
            ).all()
            mapping = (await session.execute(self._mapping_query(asset_id))).scalars().first()
            return Instrument(
                asset_id=asset.id,
                name=asset.name or asset.display_name,
                quote_type=asset.quote_type,
                listings=tuple(InstrumentListing(*row) for row in listings),
                stored_cik=(mapping.provider_ticker or None) if mapping else None,
                stored_source=mapping.source if mapping else None,
                stored_active=bool(mapping.is_active) if mapping else False,
                stored_verified=bool(mapping and mapping.last_verified_at),
                stored_at=mapping.updated_at if mapping else None,
            )

    async def remember(self, asset_id: int, resolution: CikResolution, now: datetime) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                mapping = (await session.execute(self._mapping_query(asset_id))).scalars().first()
                if mapping is None:
                    mapping = AssetMapping(
                        asset_id=asset_id,
                        asset_listing_id=None,
                        provider_name=SEC_PROVIDER,
                        failure_count=0,
                    )
                    session.add(mapping)
                if resolution.found:
                    mapping.provider_ticker = resolution.cik
                    mapping.provider_url = EDGAR_URL.format(cik=resolution.cik)
                    mapping.source = resolution.origin
                    mapping.confidence_score = 1.0
                    mapping.is_active = True
                    mapping.failure_count = 0
                    mapping.last_verified_at = now
                else:
                    # A CIK that is no longer confirmed is switched off: nothing must use it.
                    mapping.source = NOT_FOUND
                    mapping.is_active = False
                    mapping.failure_count = (mapping.failure_count or 0) + 1
                    mapping.last_verified_at = None
                mapping.updated_at = now
