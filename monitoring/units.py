"""Unit normalisation applied before provider monitoring checks.

Monitoring ranges (``FIELD_RANGES`` and ``CANARY_ASSETS``) are expressed as
ratios: a 3.45 % dividend yield is ``0.0345``. Scraped providers publish some
fields as percentages, exactly as displayed on the source page (``3.45``).
Comparing the two directly rejects every valid value, so each provider
declares here which fields it returns as percentages, and the monitoring layer
converts them before any range, consensus or canary check.

The provider payload itself is never rewritten: the public API keeps returning
each provider's native unit.
"""

from __future__ import annotations

from decimal import Decimal

_HUNDRED = Decimal("100")

# Every monitored provider must be listed, even when it returns no percentage
# field: an explicit empty set documents that its values are already ratios.
# ``tests/test_provider_units.py`` enforces this for all monitored providers.
PROVIDER_PERCENT_FIELDS: dict[str, frozenset[str]] = {
    "ZoneBourse": frozenset({"dividend_yield", "operating_margin"}),
    "GoogleFinance": frozenset({"dividend_yield", "operating_margin"}),
    "Boursorama": frozenset({"dividend_yield"}),
    "Barrons": frozenset({"dividend_yield"}),
    "wallStreetJournal": frozenset({"dividend_yield"}),
    "Marketwatch": frozenset({"dividend_yield"}),
    "MorningStar": frozenset({"dividend_yield"}),
    "Investing": frozenset({"dividend_yield"}),
    "Gurufocus": frozenset(),
    "Fortuneo": frozenset(),
    "BourseDirect": frozenset(),
    "Msn": frozenset(),
    "InvestirLesEchos": frozenset(),
    "YahooFinance": frozenset(),
}

_PERCENT_FIELDS_BY_LOWER_NAME = {
    name.lower(): fields for name, fields in PROVIDER_PERCENT_FIELDS.items()
}


def percent_fields(provider: str) -> frozenset[str]:
    """Return the fields a provider publishes as percentages.

    Provider names are matched case-insensitively. Unknown providers are
    assumed to return ratios already.
    """
    return _PERCENT_FIELDS_BY_LOWER_NAME.get(provider.lower(), frozenset())


def to_ratio(provider: str, field: str, value: Decimal | None) -> Decimal | None:
    """Convert a provider value to the ratio unit used by monitoring ranges."""
    if value is None:
        return None
    if field in percent_fields(provider):
        return value / _HUNDRED
    return value
