"""One rule to find the price series a ticker designates.

A price series belongs to a *listing* — a ticker on an exchange, in a currency —
not to the instrument: the same ETF quoted in EUR and in USD has two series. A
row of ``prices_eod`` is identified by its listing, its resolution and the date
of its trading session.

Every reader and writer of ``prices_eod`` goes through this module, so that a
ticker cannot resolve to one listing when the prices are written and to another
when they are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models import Asset, AssetListing, PriceEOD


@dataclass(frozen=True)
class PriceSeries:
    """The listing whose prices a ticker designates, with its instrument."""

    asset_id: int
    listing_id: int


def session_timestamp(day: date) -> datetime:
    """Return the value stored in ``prices_eod.time`` for a trading session.

    A daily, weekly or monthly bar is dated by its session on its exchange,
    stored as midnight UTC of that date: 8 January in Paris and 8 January in New
    York are both ``2024-01-08 00:00+00``, whatever the time zone of the exchange.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def session_date(value: datetime | date) -> date:
    """Return the session date of a stored ``prices_eod.time`` value.

    A driver may hand the timestamp back in the time zone of the database
    session: it is brought back to UTC before its date is read.
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    return value


def ticker_candidates(ticker: str) -> list[str]:
    """Symbols to look up for a requested ticker: itself, then without its suffix."""
    normalized = ticker.strip().upper()
    candidates = [normalized]
    if "." in normalized:
        base_symbol = normalized.split(".")[0]
        if base_symbol and base_symbol not in candidates:
            candidates.append(base_symbol)
    return candidates


@dataclass(frozen=True)
class ListingChoice:
    """What a request may add to a ticker to name one listing among several.

    ``GOVY`` quoted in EUR and in CHF is two listings with the same ticker: the
    currency (or the exchange) tells them apart. Without a choice, the primary
    listing is taken.
    """

    currency: str | None = None
    exchange: str | None = None
    active_only: bool = False

    def narrow(self, statement: Select) -> Select:
        if self.currency:
            statement = statement.where(
                func.upper(AssetListing.currency) == self.currency.strip().upper()
            )
        if self.exchange:
            statement = statement.where(
                func.upper(AssetListing.exchange) == self.exchange.strip().upper()
            )
        if self.active_only:
            statement = statement.where(AssetListing.is_active.is_(True))
        return statement


def _listing_by_ticker(symbol: str, choice: ListingChoice) -> Select:
    statement = choice.narrow(
        select(AssetListing.asset_id, AssetListing.id).where(AssetListing.ticker == symbol)
    )
    return statement.order_by(
        AssetListing.is_primary.desc(),
        AssetListing.currency.asc(),
        AssetListing.exchange.asc(),
        AssetListing.id.asc(),
    ).limit(1)


def _listing_of_asset_ticker(symbol: str, choice: ListingChoice) -> Select:
    """Preferred listing of an instrument known by its own (legacy) ticker only."""
    statement = choice.narrow(
        select(AssetListing.asset_id, AssetListing.id)
        .join(Asset, Asset.id == AssetListing.asset_id)
        .where(Asset.ticker == symbol)
    )
    if choice.active_only:
        statement = statement.where(Asset.is_active.is_(True))
    return statement.order_by(
        AssetListing.is_primary.desc(),
        AssetListing.is_active.desc(),
        AssetListing.id.asc(),
    ).limit(1)


def preferred_listing_of_asset(asset_id: int) -> Select:
    """Statement returning the listing that stands for an instrument as a whole.

    Used by readers that start from the instrument (valuation): the primary
    listing first, then the oldest active one.
    """
    return (
        select(AssetListing.id)
        .where(AssetListing.asset_id == asset_id)
        .order_by(
            AssetListing.is_primary.desc(),
            AssetListing.is_active.desc(),
            AssetListing.id.asc(),
        )
        .limit(1)
    )


def latest_daily_close_of_asset(asset_id: int) -> Select:
    """Statement returning the last daily close of an instrument's preferred listing.

    Reading "the last price of the instrument" without choosing a listing would
    mix currencies and resolutions.
    """
    return (
        select(PriceEOD.close)
        .where(
            PriceEOD.asset_listing_id == preferred_listing_of_asset(asset_id).scalar_subquery(),
            PriceEOD.resolution == "1D",
        )
        .order_by(PriceEOD.timestamp.desc())
        .limit(1)
    )


def _statements(ticker: str, choice: ListingChoice) -> list[Select]:
    statements = []
    for symbol in ticker_candidates(ticker):
        statements.append(_listing_by_ticker(symbol, choice))
        statements.append(_listing_of_asset_ticker(symbol, choice))
    return statements


def resolve_price_series(
    session: Session,
    ticker: str,
    *,
    currency: str | None = None,
    exchange: str | None = None,
    active_only: bool = False,
) -> PriceSeries | None:
    """Return the price series designated by ``ticker``, or ``None``.

    ``currency`` and ``exchange`` name one listing among those sharing the
    ticker. An instrument without any listing has no price series: its prices
    would have neither a currency nor an exchange.
    """
    choice = ListingChoice(currency=currency, exchange=exchange, active_only=active_only)
    for statement in _statements(ticker, choice):
        row = session.execute(statement).first()
        if row:
            return PriceSeries(asset_id=row[0], listing_id=row[1])
    return None


async def resolve_price_series_async(
    session: AsyncSession,
    ticker: str,
    *,
    currency: str | None = None,
    exchange: str | None = None,
    active_only: bool = False,
) -> PriceSeries | None:
    """Asynchronous twin of :func:`resolve_price_series` (same statements)."""
    choice = ListingChoice(currency=currency, exchange=exchange, active_only=active_only)
    for statement in _statements(ticker, choice):
        row = (await session.execute(statement)).first()
        if row:
            return PriceSeries(asset_id=row[0], listing_id=row[1])
    return None
