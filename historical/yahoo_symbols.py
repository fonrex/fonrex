"""Find, verify and remember the Yahoo symbol of a listing.

A ticker of the catalogue is not a Yahoo symbol. ``SPFF`` is, in the catalogue,
a bond UCITS ETF quoted in EUR; asked for ``SPFF``, Yahoo answers with a
US-listed fund of the same ticker, quoted in USD. Its prices would be stored
under the EUR listing without any error.

The symbol used to fetch prices is therefore never guessed from the ticker:

* it comes from a Yahoo search on the **ISIN** of the instrument, which
  identifies the instrument whatever its ticker on each exchange
  (``EUCO`` in the catalogue is ``SYBC.DE`` on Yahoo);
* it is kept only if Yahoo quotes it in the **currency of the listing** and
  has a price for it (the first search result may be a dead line);
* once verified it is stored in the ``YahooFinance`` mapping of the listing and
  reused; a listing for which nothing was found is not looked up again before
  ``RETRY_AFTER``.

A listing without a verified symbol is not ingested: no price is better than
the price of another instrument.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import yfinance as yf
from sqlalchemy.exc import SQLAlchemyError

from concurrency import run_sync
from models import Asset, AssetListing, AssetMapping

logger = logging.getLogger(__name__)

YAHOO_PROVIDER = "YahooFinance"
QUOTE_URL = "https://finance.yahoo.com/quote/{symbol}"

# How the stored symbol was obtained (``asset_mappings.source``).
FOUND_BY_ISIN = "isin_search"
FOUND_BY_TICKER = "ticker_check"
SET_BY_HAND = "manual"
NOT_FOUND = "symbol_not_found"

# A listing for which no symbol was found is looked up again after this delay.
RETRY_AFTER = timedelta(hours=24)
MAX_CANDIDATES = 8
# Lines that trade on an exchange, as opposed to fund or indicative lines.
TRADED_TYPES = ("ETF", "EQUITY")
# A Yahoo symbol without suffix is a North-American listing.
NORTH_AMERICAN_ISIN_PREFIXES = ("US", "CA")


class YahooLookupError(Exception):
    """Yahoo could not be queried: nothing can be concluded about the listing."""


@dataclass(frozen=True)
class YahooCandidate:
    """A symbol Yahoo associates with an ISIN."""

    symbol: str
    quote_type: str | None = None
    score: float = 0.0


@dataclass(frozen=True)
class YahooQuote:
    """What is checked on a symbol before trusting it."""

    currency: str | None
    last_price: float | None


@dataclass(frozen=True)
class SymbolResolution:
    """Outcome of a resolution: a verified symbol, or the reason there is none."""

    symbol: str | None
    reason: str | None = None
    origin: str | None = None

    @property
    def found(self) -> bool:
        return self.symbol is not None


class YahooLookupPort(Protocol):
    def search(self, isin: str) -> list[YahooCandidate]: ...

    def quote(self, symbol: str) -> YahooQuote | None: ...


class YahooLookup:
    """Queries Yahoo through yfinance (blocking: call it off the event loop)."""

    def search(self, isin: str) -> list[YahooCandidate]:
        try:
            quotes = yf.Search(
                isin, max_results=20, news_count=0, lists_count=0, recommended=0
            ).quotes
        except Exception as exc:
            raise YahooLookupError(f"Yahoo search failed for {isin}: {exc}") from exc
        return [
            YahooCandidate(
                symbol=str(quote["symbol"]),
                quote_type=quote.get("quoteType"),
                score=float(quote.get("score") or 0.0),
            )
            for quote in quotes or []
            if quote.get("symbol")
        ]

    def quote(self, symbol: str) -> YahooQuote | None:
        try:
            # ``fast_info`` is read by item with the names it publishes
            # (``keys()``). Its ``get`` answers ``None`` for any other name
            # instead of failing: ``get("last_price")`` gave no price for every
            # symbol, and no symbol was ever verified.
            info = yf.Ticker(symbol).fast_info
            return YahooQuote(currency=info["currency"], last_price=info["lastPrice"])
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            # yfinance has no data for this symbol.
            return None
        except Exception as exc:
            raise YahooLookupError(f"Yahoo quote failed for {symbol}: {exc}") from exc


def normalize_currency(value: str | None) -> str | None:
    """Currency code as compared between a listing and a Yahoo quote.

    Yahoo quotes London lines in pence as ``GBp``: it is not ``GBP``, the
    prices differ by a factor of 100.
    """
    if not value:
        return None
    value = value.strip()
    if value == "GBp":
        return "GBX"
    return value.upper()


def _has_price(quote: YahooQuote) -> bool:
    try:
        return quote.last_price is not None and float(quote.last_price) > 0
    except (TypeError, ValueError):
        return False


def _base(symbol: str) -> str:
    return symbol.split(".")[0].upper()


@dataclass(frozen=True)
class _Listing:
    listing_id: int
    asset_id: int
    ticker: str
    currency: str | None
    isin: str | None
    mapped_symbol: str | None
    mapping_source: str | None
    mapping_active: bool
    mapping_verified: bool
    mapping_updated_at: datetime | None


class YahooSymbolResolver:
    """Return the verified Yahoo symbol of a listing, looking it up when unknown."""

    def __init__(
        self,
        db_service: Any,
        lookup: YahooLookupPort | None = None,
        *,
        retry_after: timedelta = RETRY_AFTER,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db = db_service
        self._lookup = lookup or YahooLookup()
        self._retry_after = retry_after
        self._now = now

    async def resolve(self, listing_id: int, *, refresh: bool = False) -> SymbolResolution:
        return await run_sync(self.resolve_sync, listing_id, refresh)

    def resolve_sync(self, listing_id: int, refresh: bool = False) -> SymbolResolution:
        """Resolve the symbol of a listing.

        ``refresh`` ignores what is stored (a verified symbol, or an earlier
        failure) and asks Yahoo again.
        """
        try:
            listing = self._load(listing_id)
        except SQLAlchemyError as exc:
            # Like an unreachable Yahoo: nothing is concluded, nothing is asked.
            logger.warning("Listing %s could not be read: %s", listing_id, exc)
            return SymbolResolution(None, f"Listing {listing_id} could not be read: {exc}")
        if listing is None:
            return SymbolResolution(None, f"Listing {listing_id} does not exist")

        if not refresh:
            stored = self._stored(listing)
            if stored is not None:
                return stored

        try:
            resolution = self._look_up(listing)
        except YahooLookupError as exc:
            logger.warning("%s", exc)
            return SymbolResolution(None, str(exc))

        try:
            self._remember(listing, resolution)
        except SQLAlchemyError as exc:
            # Two first requests for one listing store the same answer: the
            # second write hits the unique key. The answer itself is still good.
            logger.warning("Yahoo symbol of listing %s not stored: %s", listing_id, exc)
        return resolution

    # ── What is already known ────────────────────────────────────────────────

    def _load(self, listing_id: int) -> _Listing | None:
        session = self._db.get_session()
        try:
            row = (
                session.query(AssetListing, Asset.isin)
                .join(Asset, Asset.id == AssetListing.asset_id)
                .filter(AssetListing.id == listing_id)
                .first()
            )
            if row is None:
                return None
            listing, isin = row
            mapping = self._mapping(session, listing_id)
            return _Listing(
                listing_id=listing.id,
                asset_id=listing.asset_id,
                ticker=(listing.ticker or "").strip().upper(),
                currency=normalize_currency(listing.currency),
                isin=(isin or "").strip().upper() or None,
                mapped_symbol=(mapping.provider_ticker or None) if mapping else None,
                mapping_source=mapping.source if mapping else None,
                mapping_active=bool(mapping.is_active) if mapping else False,
                mapping_verified=bool(mapping and mapping.last_verified_at),
                mapping_updated_at=mapping.updated_at if mapping else None,
            )
        finally:
            session.close()

    @staticmethod
    def _mapping(session: Any, listing_id: int) -> AssetMapping | None:
        return (
            session.query(AssetMapping)
            .filter(
                AssetMapping.asset_listing_id == listing_id,
                AssetMapping.provider_name == YAHOO_PROVIDER,
            )
            .first()
        )

    def _stored(self, listing: _Listing) -> SymbolResolution | None:
        """A symbol verified earlier (or set by hand), or a recent failure."""
        trusted = listing.mapping_source == SET_BY_HAND or listing.mapping_verified
        if listing.mapping_active and listing.mapped_symbol and trusted:
            return SymbolResolution(listing.mapped_symbol, origin=listing.mapping_source)

        if listing.mapping_source == NOT_FOUND and listing.mapping_updated_at is not None:
            checked_at = listing.mapping_updated_at
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=UTC)
            if self._now() - checked_at < self._retry_after:
                return SymbolResolution(
                    None,
                    f"No verified Yahoo symbol for {listing.ticker} in {listing.currency} "
                    f"(checked on {checked_at:%Y-%m-%d %H:%M} UTC)",
                    origin=NOT_FOUND,
                )
        return None

    # ── Asking Yahoo ─────────────────────────────────────────────────────────

    def _look_up(self, listing: _Listing) -> SymbolResolution:
        if not listing.currency:
            return SymbolResolution(
                None, f"Listing {listing.ticker} has no currency: its symbol cannot be verified"
            )

        seen: list[str] = []  # "SYMBOL (currency)" of what Yahoo offered, for the message

        by_isin = self._candidates_by_isin(listing)
        symbol = self._first_valid(listing, by_isin, seen)
        if symbol:
            return SymbolResolution(symbol, origin=FOUND_BY_ISIN)

        symbol = self._first_valid(listing, self._candidates_by_ticker(listing, by_isin), seen)
        if symbol:
            return SymbolResolution(symbol, origin=FOUND_BY_TICKER)

        identity = f"ISIN {listing.isin}" if listing.isin else f"ticker {listing.ticker}"
        offered = f"; Yahoo offers {', '.join(seen)}" if seen else ""
        return SymbolResolution(
            None,
            f"No Yahoo symbol quoted in {listing.currency} for {identity}{offered}",
            origin=NOT_FOUND,
        )

    def _candidates_by_isin(self, listing: _Listing) -> list[str]:
        if not listing.isin:
            return []
        candidates = self._lookup.search(listing.isin)
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                candidate.symbol.upper() != listing.ticker,  # the very symbol of the catalogue
                _base(candidate.symbol) != _base(listing.ticker),  # then the same ticker
                (candidate.quote_type or "").upper() not in TRADED_TYPES,
                -candidate.score,
            ),
        )
        return [candidate.symbol for candidate in ranked]

    @staticmethod
    def _candidates_by_ticker(listing: _Listing, already_tried: list[str]) -> list[str]:
        """Symbols taken from the catalogue itself, when the ISIN gave nothing usable.

        A ticker that names its exchange (``GOVY.SW``) is tried as it is. A ticker
        without suffix is a North-American symbol on Yahoo: it is tried only for
        an instrument that has no ISIN or a North-American one.
        """
        candidates = []
        for symbol in (listing.mapped_symbol, listing.ticker):
            if symbol and "." in symbol:
                candidates.append(symbol.upper())
        north_american = not listing.isin or listing.isin.startswith(NORTH_AMERICAN_ISIN_PREFIXES)
        if north_american and listing.ticker and "." not in listing.ticker:
            candidates.append(listing.ticker)
        tried = {symbol.upper() for symbol in already_tried}
        return [symbol for symbol in dict.fromkeys(candidates) if symbol not in tried]

    def _first_valid(self, listing: _Listing, symbols: list[str], seen: list[str]) -> str | None:
        """First symbol quoted in the currency of the listing, with a price."""
        for symbol in symbols[:MAX_CANDIDATES]:
            quote = self._lookup.quote(symbol)
            currency = normalize_currency(quote.currency) if quote else None
            if quote is None or not _has_price(quote):
                seen.append(f"{symbol} (no price)")
                continue
            if currency != listing.currency:
                seen.append(f"{symbol} ({currency or 'no currency'})")
                continue
            return symbol
        return None

    # ── Remembering the answer ───────────────────────────────────────────────

    def _remember(self, listing: _Listing, resolution: SymbolResolution) -> None:
        session = self._db.get_session()
        try:
            mapping = self._mapping(session, listing.listing_id)
            if mapping is None:
                mapping = AssetMapping(
                    asset_id=listing.asset_id,
                    asset_listing_id=listing.listing_id,
                    provider_name=YAHOO_PROVIDER,
                    failure_count=0,
                )
                session.add(mapping)

            now = self._now()
            if resolution.found:
                mapping.provider_ticker = resolution.symbol
                mapping.provider_url = QUOTE_URL.format(symbol=resolution.symbol)
                mapping.source = resolution.origin
                mapping.confidence_score = 1.0 if resolution.origin == FOUND_BY_ISIN else 0.8
                mapping.is_active = True
                mapping.failure_count = 0
                mapping.last_verified_at = now
            else:
                # The unverified symbol is switched off: nothing must use it.
                mapping.source = NOT_FOUND
                mapping.is_active = False
                mapping.failure_count = (mapping.failure_count or 0) + 1
                mapping.last_verified_at = None
            mapping.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
