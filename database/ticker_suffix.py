"""A Yahoo exchange suffix names a place: ``AIR.PA`` is Airbus in Paris, not any ``AIR``.

A ticker such as ``AIR.PA`` is looked up as it is, then without its suffix when
the catalogue stores the bare symbol (``AIR``) on its listing. The bare symbol
alone is not an identity: ``AIR`` is also AAR Corp in New York. A listing found
by the bare symbol is therefore taken only when it is on the place the suffix
names:

* its exchange is one of the codes of that place (MIC ``XPAR``, Google ``EPA``,
  Yahoo ``PAR``, the suffix ``PA``...);
* or its exchange is empty or unknown and it is quoted in a currency of that
  place (EUR for Paris).

A listing on another known exchange, or in another currency, is never taken. A
suffix that names no known place (``BRK.B`` is a share class) gives no lookup
without suffix.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.elements import ColumnElement

from models import AssetListing


@dataclass(frozen=True)
class Place:
    """An exchange as a Yahoo suffix names it: its codes and its currencies."""

    exchanges: frozenset[str]
    currencies: frozenset[str]


def _place(exchanges: str, currencies: str) -> Place:
    return Place(frozenset(exchanges.split()), frozenset(currencies.split()))


# Yahoo suffix -> codes under which the catalogue may store that exchange.
PLACES: dict[str, Place] = {
    "PA": _place("PA XPAR EPA PAR ENXTPA", "EUR"),
    "AS": _place("AS XAMS AMS ENXTAM", "EUR"),
    "BR": _place("BR XBRU EBR BRU ENXTBR", "EUR"),
    "LS": _place("LS XLIS ELI LIS ENXTLS", "EUR"),
    "IR": _place("IR XDUB ISE", "EUR"),
    "DE": _place("DE XETR XETRA ETR GER", "EUR"),
    "F": _place("F XFRA FRA", "EUR"),
    "MI": _place("MI XMIL BIT MIL", "EUR"),
    "MC": _place("MC XMAD BME MCE", "EUR"),
    "HE": _place("HE XHEL HEL", "EUR"),
    "VI": _place("VI XWBO VIE", "EUR"),
    "L": _place("L XLON LON LSE", "GBP GBX"),
    "SW": _place("SW XSWX SWX EBS", "CHF"),
    "ST": _place("ST XSTO STO", "SEK"),
    "CO": _place("CO XCSE CPH", "DKK"),
    "OL": _place("OL XOSL OSL", "NOK"),
    "TO": _place("TO XTSE TSE TOR", "CAD"),
    "T": _place("T XTKS TYO JPX", "JPY"),
    "HK": _place("HK XHKG HKG", "HKD"),
    "AX": _place("AX XASX ASX", "AUD"),
}

# Exchanges without a Yahoo suffix: a listing there is never on a suffixed place.
_OTHER_EXCHANGES = frozenset(
    "XNYS XNAS XASE ARCX NYSE NASDAQ NYQ NMS NGM NCM AMEX ARCA BATS PCX OTC".split()
)
KNOWN_EXCHANGES = frozenset().union(*(place.exchanges for place in PLACES.values())) | (
    _OTHER_EXCHANGES
)


@dataclass(frozen=True)
class TickerLookup:
    """A symbol to look up, and the place its listing must be on (``None``: any)."""

    symbol: str
    place: Place | None = None


def ticker_lookups(ticker: str) -> list[TickerLookup]:
    """The ticker as it is, then its bare symbol on the place its suffix names."""
    normalized = ticker.strip().upper()
    lookups = [TickerLookup(normalized)]
    symbol, dot, suffix = normalized.rpartition(".")
    place = PLACES.get(suffix) if dot else None
    if symbol and place is not None:
        lookups.append(TickerLookup(symbol, place))
    return lookups


def listing_on(place: Place) -> ColumnElement[bool]:
    """SQL condition: the listing (``asset_listings``) is on that place."""
    exchange = func.upper(func.coalesce(AssetListing.exchange, ""))
    return or_(
        exchange.in_(sorted(place.exchanges)),
        and_(
            exchange.not_in(sorted(KNOWN_EXCHANGES)),
            func.upper(AssetListing.currency).in_(sorted(place.currencies)),
        ),
    )
