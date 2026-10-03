"""Read tunable settings from the environment, with safe fallbacks.

A setting documented in ``.env.example`` must be read by the code. These
helpers do it without letting a mistyped value stop the API at start-up: an
unset, empty, unreadable or out-of-range value falls back to the default, and
the last two are logged.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)


def _raw(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _in_range(
    name: str,
    value: int | Decimal,
    default: int | Decimal,
    minimum: int | Decimal | None,
    maximum: int | Decimal | None,
) -> int | Decimal:
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        logger.warning(
            "%s=%s is outside the accepted range [%s, %s]: using the default %s",
            name,
            value,
            "-inf" if minimum is None else minimum,
            "+inf" if maximum is None else maximum,
            default,
        )
        return default
    return value


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return an integer setting, or ``default`` when it is unset or unusable."""
    raw = _raw(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer: using the default %s", name, raw, default)
        return default
    return _in_range(name, value, default, minimum, maximum)


def env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    """Return a setting restricted to ``choices`` (case-insensitive), or ``default``."""
    raw = _raw(name)
    if raw is None:
        return default
    value = raw.lower()
    if value not in choices:
        logger.warning(
            "%s=%r is not one of %s: using the default %r", name, raw, ", ".join(choices), default
        )
        return default
    return value


def env_decimal(
    name: str,
    default: str,
    *,
    minimum: str | None = None,
    maximum: str | None = None,
) -> Decimal:
    """Return a decimal setting, or ``default`` when it is unset or unusable."""
    fallback = Decimal(default)
    raw = _raw(name)
    if raw is None:
        return fallback
    try:
        value = Decimal(raw)
    except InvalidOperation:
        logger.warning("%s=%r is not a number: using the default %s", name, raw, fallback)
        return fallback
    if not value.is_finite():
        logger.warning("%s=%r is not a finite number: using the default %s", name, raw, fallback)
        return fallback
    return _in_range(
        name,
        value,
        fallback,
        None if minimum is None else Decimal(minimum),
        None if maximum is None else Decimal(maximum),
    )
