"""The currency a valuation is made in, and whether the share price can be compared to it.

A DCF discounts the cash flows of the financial statements: the valuation is in
the currency of the statements (Yahoo's ``financialCurrency``, stored on
``financial_statements.currency``). The share price comes from the main listing
of the instrument, which may be quoted in another currency: London lines in pence
(``GBX``), a US listing of a European company. A price is compared to the
intrinsic value only when it is in the same currency, after turning a minor unit
into its major one; otherwise no upside is given rather than a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.price_series import preferred_listing_of_asset
from models import AssetListing, FinancialStatement

# Minor units in which some exchanges quote prices: (major currency, units per major).
MINOR_UNITS = {
    "GBX": ("GBP", Decimal("100")),
    "ZAC": ("ZAR", Decimal("100")),
    "ILA": ("ILS", Decimal("100")),
}


def _code(value: Optional[str]) -> Optional[str]:
    """A currency code in upper case, ``GBp`` (Yahoo's pence) as ``GBX``; ``None`` if empty."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value == "GBp":
        return "GBX"
    if value == "ZAc":
        return "ZAC"
    return value.upper() or None


@dataclass(frozen=True)
class ValuationCurrency:
    """The currency of a valuation and how the share price relates to it."""

    currency: str
    price_currency: Optional[str]
    # What the price is multiplied by to be in ``currency``; ``None`` when it cannot be.
    price_factor: Optional[Decimal]
    warning: Optional[str] = None

    @property
    def price_comparable(self) -> bool:
        return self.price_factor is not None


def resolve_currency(
    statements_currency: Optional[str],
    price_currency: Optional[str],
    instrument_currency: Optional[str] = None,
) -> ValuationCurrency:
    """The valuation currency, from the statements, else the price, else the instrument."""
    statements_currency = _code(statements_currency)
    price_currency = _code(price_currency)
    major_price, units = MINOR_UNITS.get(price_currency or "", (price_currency, Decimal("1")))
    currency = statements_currency or major_price or _code(instrument_currency) or "USD"

    if price_currency is None or major_price == currency:
        return ValuationCurrency(currency, price_currency, Decimal("1") / units)
    return ValuationCurrency(
        currency,
        price_currency,
        None,
        warning=(
            f"The share price is quoted in {price_currency} and the statements are in "
            f"{currency}: no upside is computed."
        ),
    )


def statements_currency(session: Session, asset_id: int) -> Optional[str]:
    """Currency of the most recent annual statement that records one."""
    return session.execute(
        select(FinancialStatement.currency)
        .where(
            FinancialStatement.asset_id == asset_id,
            FinancialStatement.period_type == "annual",
            FinancialStatement.currency.is_not(None),
        )
        .order_by(FinancialStatement.period_end.desc())
        .limit(1)
    ).scalar()


def price_listing_currency(session: Session, asset_id: int) -> Optional[str]:
    """Currency of the listing whose last close is the share price of the valuation."""
    return session.execute(
        select(AssetListing.currency).where(
            AssetListing.id == preferred_listing_of_asset(asset_id).scalar_subquery()
        )
    ).scalar()


def valuation_currency(session: Session, asset) -> ValuationCurrency:
    """The currency of the valuation of an instrument (a row of ``assets``)."""
    return resolve_currency(
        statements_currency(session, asset.id),
        price_listing_currency(session, asset.id),
        asset.currency,
    )
