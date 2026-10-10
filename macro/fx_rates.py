"""Euro foreign exchange reference rates of the ECB, kept day by day.

The factor returns of the Kenneth French library are in US dollars: a listing
quoted in another currency is compared with them in dollars. The ECB publishes
every business day a reference rate of the euro against about thirty
currencies, free and without key: ``EXR/D.{currency}.EUR.SP00.A`` gives the
units of ``currency`` for one euro (``1.0850`` US dollars). Any rate between two
currencies goes through the euro: dollars per pound = (USD per EUR) / (GBP per EUR).

``fx_rates`` keeps the whole history of each currency (since 1999). A refresh
asks only for the days after the last one stored, a few days earlier to take a
late correction; a currency read less than ``FX_RATES_REFRESH_HOURS`` hours ago is
not asked again.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from macro.ecb_service import ECB_API_URL
from models import FxRate, FxRateLoad
from settings import env_int

logger = logging.getLogger(__name__)

# Currencies kept by default: those of the listings Fonrex compares in dollars.
DEFAULT_CURRENCIES = ("USD", "GBP", "CHF", "SEK", "DKK", "NOK", "JPY", "CAD", "AUD", "HKD")
FIRST_DAY = date(1999, 1, 4)  # first reference rates of the euro
# Days asked again before the last one stored, for a late correction.
_OVERLAP = timedelta(days=7)
_PRECISION = Decimal("0.00000001")
_CHUNK = 5000


class FxRatesError(ValueError):
    """An answer of the ECB could not be read: nothing is stored from it."""


def _code(currency: str) -> str:
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"{currency!r} is not a currency code")
    return code


def ecb_url() -> str:
    return (os.environ.get("ECB_API_URL") or ECB_API_URL).strip().rstrip("/")


def series_url(currency: str, base_url: str | None = None) -> str:
    """Address of the daily reference rate of a currency against the euro."""
    return f"{base_url or ecb_url()}/EXR/D.{_code(currency)}.EUR.SP00.A"


def parse_fx_csv(text: str) -> dict[str, dict[date, Decimal]]:
    """Rates of an ECB CSV answer, by currency then day (units of currency per euro)."""
    rates: dict[str, dict[date, Decimal]] = {}
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"TIME_PERIOD", "OBS_VALUE"} <= set(reader.fieldnames):
        raise FxRatesError("the answer has no TIME_PERIOD and OBS_VALUE columns")
    for row in reader:
        currency = (row.get("CURRENCY") or "").strip()
        if not currency:  # EXR.D.USD.EUR.SP00.A: the currency is the third part of the key
            parts = (row.get("KEY") or "").split(".")
            currency = parts[2] if len(parts) > 2 else ""
        raw_day = (row.get("TIME_PERIOD") or "").strip()
        raw_value = (row.get("OBS_VALUE") or "").strip()
        if not currency or not raw_day or not raw_value:
            continue  # a day without rate (the value is left empty)
        try:
            day = date.fromisoformat(raw_day)
            value = Decimal(raw_value)
        except (ValueError, InvalidOperation) as error:
            raise FxRatesError(f"{currency} {raw_day}: {raw_value!r} cannot be read") from error
        if not value.is_finite() or value <= 0:
            continue
        rates.setdefault(currency.upper(), {})[day] = value.quantize(_PRECISION)
    return rates


async def download_rates(
    currency: str, start: date, base_url: str | None = None
) -> dict[date, Decimal]:
    """Daily rates of a currency against the euro from ``start`` (raises on failure)."""
    params = {"startPeriod": start.isoformat(), "format": "csvdata", "detail": "dataonly"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            series_url(currency, base_url), params=params, headers={"Accept": "text/csv"}
        )
        response.raise_for_status()
    if not response.text.strip():
        return {}  # nothing published since ``start``
    return parse_fx_csv(response.text).get(_code(currency), {})


@dataclass(frozen=True)
class FxLoad:
    """What the database holds for one currency, and what the last refresh did."""

    currency: str
    status: str  # fetched, fresh, failed
    fetched_at: datetime | None = None
    first_day: date | None = None
    last_day: date | None = None
    added: int = 0
    reason: str | None = None


class EcbExchangeRates:
    """Reference rates of the euro, stored day by day and refreshed from the ECB."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, downloader=download_rates
    ):
        self.session_factory = session_factory
        self.downloader = downloader
        self.refresh_after = timedelta(
            hours=env_int("FX_RATES_REFRESH_HOURS", 12, minimum=1, maximum=24 * 30)
        )

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < self.refresh_after

    async def refresh(self, currency: str, force: bool = False) -> FxLoad:
        """Ask the ECB for the days not stored yet, unless the currency was read recently."""
        code = _code(currency)
        if code == "EUR":
            return FxLoad("EUR", "fresh")
        async with self.session_factory() as session:
            load = await session.get(FxRateLoad, code)
            last_day = await session.scalar(
                select(func.max(FxRate.rate_date)).where(FxRate.currency == code)
            )
        if load is not None and not force and self._is_recent(load.fetched_at):
            return FxLoad(code, "fresh", load.fetched_at, load.first_day, load.last_day)

        start = max(FIRST_DAY, last_day - _OVERLAP) if last_day else FIRST_DAY
        try:
            rates = await self.downloader(code, start)
        except Exception as error:  # network, HTTP status, unreadable answer
            message = str(error).splitlines()[0] if str(error) else ""
            reason = f"{type(error).__name__}: {message}" if message else type(error).__name__
            logger.warning("Exchange rates of %s not refreshed: %s", code, reason)
            first = load.first_day if load else None
            return FxLoad(
                code, "failed", load.fetched_at if load else None, first, last_day, reason=reason
            )

        fetched_at = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            async with session.begin():
                rows = [
                    {"currency": code, "rate_date": day, "per_eur": value}
                    for day, value in sorted(rates.items())
                ]
                for chunk in range(0, len(rows), _CHUNK):
                    statement = insert(FxRate).values(rows[chunk : chunk + _CHUNK])
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["currency", "rate_date"],
                            set_={"per_eur": statement.excluded.per_eur},
                        )
                    )
                first_day, last_stored = (
                    await session.execute(
                        select(func.min(FxRate.rate_date), func.max(FxRate.rate_date)).where(
                            FxRate.currency == code
                        )
                    )
                ).one()
                await session.merge(
                    FxRateLoad(
                        currency=code,
                        fetched_at=fetched_at,
                        first_day=first_day,
                        last_day=last_stored,
                    )
                )
        return FxLoad(code, "fetched", fetched_at, first_day, last_stored, added=len(rates))

    async def per_euro(
        self, currency: str, start: date | None = None, end: date | None = None
    ) -> dict[date, Decimal]:
        """Stored units of ``currency`` for one euro, by day."""
        code = _code(currency)
        statement = select(FxRate.rate_date, FxRate.per_eur).where(FxRate.currency == code)
        if start is not None:
            statement = statement.where(FxRate.rate_date >= start)
        if end is not None:
            statement = statement.where(FxRate.rate_date <= end)
        async with self.session_factory() as session:
            rows = (await session.execute(statement.order_by(FxRate.rate_date))).all()
        return dict(rows)

    async def rates(
        self, base: str, quote: str, start: date | None = None, end: date | None = None
    ) -> dict[date, Decimal]:
        """Units of ``quote`` for one unit of ``base``, on the days both are known.

        ``rates("GBP", "USD")`` gives dollars per pound: (USD per EUR) / (GBP per EUR).
        """
        base, quote = _code(base), _code(quote)
        if base == quote:
            raise ValueError("the two currencies are the same")
        quote_per_eur = None if quote == "EUR" else await self.per_euro(quote, start, end)
        base_per_eur = None if base == "EUR" else await self.per_euro(base, start, end)
        days = set((quote_per_eur or base_per_eur or {}).keys())
        if quote_per_eur is not None and base_per_eur is not None:
            days &= set(base_per_eur)
        one = Decimal(1)
        return {
            day: (
                (quote_per_eur[day] if quote_per_eur is not None else one)
                / (base_per_eur[day] if base_per_eur is not None else one)
            ).quantize(_PRECISION)
            for day in sorted(days)
        }
