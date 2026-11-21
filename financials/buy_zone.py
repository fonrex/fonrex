"""Dividend buy zone: is the dividend yield of a share at least the yield asked for?

A share is in the buy zone when its net dividend yield reaches a target yield. The
net yield is the gross yield less the tax on dividends; the highest price that still
gives the target yield is the price at which the share enters the zone.

Every rate here is a ratio (0.065 for 6.5 %), as in the rest of the fundamentals
document. The highest buy price is computed from the price and the yield
(``price * net_yield / target``) rather than from the dividend: Yahoo may give the
price of a London listing in pence and its dividend in pounds, and the yield it
publishes is consistent with neither unit only by accident. The dividend serves
only when there is no price.
"""

import math
from decimal import Decimal
from typing import Any, Dict, Optional

SECTION = "DividendBuyZone"

# Keys of the section that do not depend on the target yield or the tax: the
# formatter renders them, and a request computes the others from them.
INPUTS = ("Price", "DividendShare", "GrossYield", "PayoutRatio")


def _number(value: Any) -> Optional[float]:
    """A finite number, or ``None``: zero is a value, a text or NaN is not."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ratio(value: float) -> float:
    return round(value, 6)


def _price(value: float) -> float:
    return round(value, 4)


def dividend_buy_zone(
    price: Any = None,
    dividend: Any = None,
    gross_yield: Any = None,
    payout_ratio: Any = None,
    target_yield: Optional[float] = None,
    dividend_tax: float = 0.0,
) -> Dict[str, Any]:
    """Render the buy zone of a share from its price, dividend and yield.

    ``gross_yield`` is the published dividend yield; when it is missing it is the
    dividend divided by the price. Without a target yield only the yields are
    rendered: the zone, the gap and the highest buy price stay empty.
    """
    price = _number(price)
    dividend = _number(dividend)
    gross = _number(gross_yield)
    if gross is None and dividend is not None and price:
        gross = dividend / price
    keep = 1.0 - dividend_tax

    section: Dict[str, Any] = {
        "Price": price,
        "DividendShare": dividend,
        "GrossYield": _ratio(gross) if gross is not None else None,
        "PayoutRatio": _number(payout_ratio),
        "DividendTax": dividend_tax,
        "NetDividendShare": _price(dividend * keep) if dividend is not None else None,
        "NetYield": _ratio(gross * keep) if gross is not None else None,
        "TargetYield": target_yield,
        "MaxBuyPrice": None,
        "YieldGap": None,
        "InBuyZone": None,
    }
    if not target_yield:
        return section

    net = gross * keep if gross is not None else None
    if net is not None:
        section["YieldGap"] = _ratio((net - target_yield) / target_yield)
        section["InBuyZone"] = net >= target_yield
    if net is not None and price:
        section["MaxBuyPrice"] = _price(price * net / target_yield)
    elif dividend is not None:
        section["MaxBuyPrice"] = _price(dividend * keep / target_yield)
    return section


def apply_buy_zone(
    document: Dict[str, Any], target_yield: Optional[float], dividend_tax: float = 0.0
) -> Dict[str, Any]:
    """Recompute the buy zone of a rendered document for a target yield and a tax.

    The cached document holds the section without a target: the parameters of a
    request are applied to it here, so one cache entry serves every target. A
    document without the section (raw format, or cached before the section
    existed) is returned unchanged.
    """
    section = document.get(SECTION)
    if not isinstance(section, dict):
        return document
    inputs = {key: section.get(key) for key in INPUTS}
    return {
        **document,
        SECTION: dividend_buy_zone(
            price=inputs["Price"],
            dividend=inputs["DividendShare"],
            gross_yield=inputs["GrossYield"],
            payout_ratio=inputs["PayoutRatio"],
            target_yield=target_yield,
            dividend_tax=dividend_tax,
        ),
    }
