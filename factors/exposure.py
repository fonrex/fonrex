"""Factor exposure of a listing: a time-series regression on the Fama/French factors.

    r_t - RF_t = alpha + sum_k beta_k * F_k,t + e_t

``r_t`` is the return of the listing over the period, in **US dollars** (the
factors of the Kenneth French library are in dollars for every region); a price
quoted in another currency is converted with the ECB reference rates of the day
(``macro/fx_rates.py``). Returns are computed on ``adj_close`` (dividends
included), from the daily closes of the listing (``database/price_series.py``):
a monthly return goes from the last close of a month to the last close of the
next one. The regression is ordinary least squares (numpy), with classical
standard errors.

The region of the factors follows the currency of the listing unless it is
given: USD for the US factors, the European currencies for the European ones,
the developed markets otherwise.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.price_series import listing_identity, resolve_price_series_async, session_date
from factors.store import FactorLibrary
from macro.fx_rates import EcbExchangeRates
from models import PriceEOD
from valuation.currency import MINOR_UNITS, currency_code

Model = Literal["ff3", "ff5", "carhart"]
Frequency = Literal["monthly", "daily"]
Region = Literal["us", "europe", "developed"]

# Model -> (datasets suffixes of the region, factors regressed on).
MODELS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ff3": (("3",), ("MKT_RF", "SMB", "HML")),
    "ff5": (("5",), ("MKT_RF", "SMB", "HML", "RMW", "CMA")),
    "carhart": (("3", "mom"), ("MKT_RF", "SMB", "HML", "MOM")),
}
EUROPEAN_CURRENCIES = {"EUR", "GBP", "CHF", "SEK", "DKK", "NOK", "ISK", "PLN", "CZK", "HUF"}
PERIODS_PER_YEAR = {"monthly": 12, "daily": 252}
DEFAULT_WINDOW = {"monthly": 60, "daily": 252}
MIN_PERIODS = {"monthly": 24, "daily": 60}
SHORT_WINDOW = {"monthly": 36, "daily": 126}
# An exchange rate older than this is not used for a close (ECB holidays are short).
FX_STALENESS = timedelta(days=7)


class ExposureError(Exception):
    """The exposure cannot be measured; ``status`` is the HTTP status to answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Coefficient:
    value: float
    std_error: float
    t_stat: float


@dataclass(frozen=True)
class Regression:
    alpha: Coefficient  # per period
    betas: dict[str, Coefficient]
    r_squared: float
    adj_r_squared: float
    residual_std: float  # per period
    periods: int


@dataclass
class ExposureResult:
    ticker: str
    listing: dict
    model: str
    region: str
    frequency: str
    datasets: list[str]
    price_currency: str | None
    converted_from: str | None
    start: date
    end: date
    regression: Regression
    warnings: list[str] = field(default_factory=list)


# ── Pure steps ───────────────────────────────────────────────────────────────


def region_of(currency: str | None) -> Region:
    if currency == "USD":
        return "us"
    if currency in EUROPEAN_CURRENCIES:
        return "europe"
    return "developed"


def major_currency(currency: str | None) -> str | None:
    """``GBX`` (pence) -> ``GBP``; a constant unit does not change a return."""
    code = currency_code(currency)
    return MINOR_UNITS[code][0] if code in MINOR_UNITS else code


def to_usd(
    closes: dict[date, float], usd_per_unit: dict[date, Decimal]
) -> tuple[dict[date, float], int]:
    """Closes in dollars with the rate of their day, else the last one within a week.

    Returns the converted closes and how many closes had no rate.
    """
    days = sorted(usd_per_unit)
    converted, missing = {}, 0
    index = 0
    last: tuple[date, Decimal] | None = None
    for day in sorted(closes):
        while index < len(days) and days[index] <= day:
            last = (days[index], usd_per_unit[days[index]])
            index += 1
        if last is None or day - last[0] > FX_STALENESS:
            missing += 1
            continue
        converted[day] = closes[day] * float(last[1])
    return converted, missing


def _month_end(day: date) -> date:
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def period_closes(closes: dict[date, float], frequency: Frequency) -> dict[date, float]:
    """Daily closes, or the last close of each month dated by the last day of the month."""
    if frequency == "daily":
        return dict(sorted(closes.items()))
    monthly: dict[date, float] = {}
    for day in sorted(closes):
        monthly[_month_end(day)] = closes[day]
    return monthly


def period_returns(closes: dict[date, float], frequency: Frequency) -> dict[date, float]:
    """Simple returns between consecutive periods; a missing month breaks the chain."""
    returns: dict[date, float] = {}
    previous: tuple[date, float] | None = None
    for day in sorted(closes):
        price = closes[day]
        if previous is not None and previous[1] > 0 and price > 0:
            consecutive = frequency == "daily" or _month_end(previous[0] + timedelta(days=1)) == day
            if consecutive:
                returns[day] = price / previous[1] - 1
        previous = (day, price)
    return returns


def align(
    returns: dict[date, float],
    factors: dict[date, dict[str, Decimal]],
    names: tuple[str, ...],
    window: int,
    end: date | None = None,
) -> list[tuple[date, float, list[float]]]:
    """The last ``window`` periods with a return, every factor and RF: (period, excess, factors)."""
    rows = []
    for day in sorted(returns):
        if end is not None and day > end:
            break
        values = factors.get(day)
        if values is None or "RF" not in values or any(name not in values for name in names):
            continue
        rows.append(
            (day, returns[day] - float(values["RF"]), [float(values[name]) for name in names])
        )
    return rows[-window:]


def regress(rows: list[tuple[date, float, list[float]]], names: tuple[str, ...]) -> Regression:
    """Ordinary least squares of the excess returns on the factors, with an intercept."""
    y = np.array([row[1] for row in rows])
    x = np.column_stack([np.ones(len(rows)), np.array([row[2] for row in rows])])
    n, k = x.shape
    coefficients, *_ = np.linalg.lstsq(x, y, rcond=None)
    residuals = y - x @ coefficients
    ssr = float(residuals @ residuals)
    sst = float(((y - y.mean()) ** 2).sum())
    variance = ssr / (n - k)
    covariance = variance * np.linalg.pinv(x.T @ x)
    errors = np.sqrt(np.clip(np.diag(covariance), 0, None))

    def coefficient(i: int) -> Coefficient:
        error = float(errors[i])
        value = float(coefficients[i])
        return Coefficient(value, error, value / error if error > 0 else float("nan"))

    r_squared = 1 - ssr / sst if sst > 0 else 0.0
    return Regression(
        alpha=coefficient(0),
        betas={name: coefficient(i + 1) for i, name in enumerate(names)},
        r_squared=r_squared,
        adj_r_squared=1 - (1 - r_squared) * (n - 1) / (n - k),
        residual_std=float(np.sqrt(variance)),
        periods=n,
    )


# ── The service ──────────────────────────────────────────────────────────────


class FactorExposure:
    """Measures the factor exposure of a listing from what the database holds."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        library: FactorLibrary,
        fx: EcbExchangeRates,
    ):
        self.session_factory = session_factory
        self.library = library
        self.fx = fx

    async def _listing(self, ticker, currency, exchange, isin):
        async with self.session_factory() as session:
            series = await resolve_price_series_async(
                session, ticker, currency=currency, exchange=exchange, isin=isin
            )
            if series is None:
                return None, None, {}
            identity = (await session.execute(listing_identity(series.listing_id))).one()
            rows = await session.execute(
                select(PriceEOD.timestamp, PriceEOD.adj_close, PriceEOD.close)
                .where(
                    PriceEOD.asset_listing_id == series.listing_id,
                    PriceEOD.resolution == "1D",
                )
                .order_by(PriceEOD.timestamp)
            )
            closes = {
                session_date(moment): adj if adj is not None else close
                for moment, adj, close in rows
                if (adj if adj is not None else close) is not None
            }
        listing = {
            "ticker": identity.ticker,
            "isin": identity.isin,
            "currency": identity.currency,
            "exchange": identity.exchange,
        }
        return series, listing, closes

    async def _factors(self, dataset: str, frequency: Frequency) -> dict:
        load = await self.library.status(dataset, frequency)
        if load is None or load.status == "stale":
            load = await self.library.refresh(dataset, frequency)
        if not load.periods:
            raise ExposureError(
                503, f"No factor returns stored for {dataset} ({frequency}): {load.reason}"
            )
        return await self.library.series(dataset, frequency)

    async def _usd_per_unit(self, currency: str) -> dict[date, Decimal]:
        for code in {currency, "USD"} - {"EUR"}:
            load = await self.fx.refresh(code)
            if load.last_day is None:
                raise ExposureError(503, f"No ECB exchange rate stored for {code}: {load.reason}")
        return await self.fx.rates(currency, "USD")

    async def measure(
        self,
        ticker: str,
        model: Model = "ff3",
        frequency: Frequency = "monthly",
        window: int | None = None,
        end: date | None = None,
        region: Region | None = None,
        currency: str | None = None,
        exchange: str | None = None,
        isin: str | None = None,
    ) -> ExposureResult:
        suffixes, names = MODELS[model]
        window = window or DEFAULT_WINDOW[frequency]
        series, listing, closes = await self._listing(ticker, currency, exchange, isin)
        if series is None:
            raise ExposureError(404, f"No listing found for {ticker}")
        if len(closes) < 2:
            raise ExposureError(
                404,
                f"No daily prices stored for {listing['ticker']}: ingest them with "
                "POST /historical/ingest first",
            )

        warnings: list[str] = []
        price_currency = major_currency(listing["currency"])
        converted_from = None
        if price_currency and price_currency != "USD":
            closes, missing = to_usd(closes, await self._usd_per_unit(price_currency))
            converted_from = price_currency
            if missing:
                warnings.append(
                    f"{missing} closes without an ECB rate of {price_currency} within a week "
                    "were left out."
                )
        elif price_currency is None:
            warnings.append("The listing has no currency: its prices are taken as US dollars.")

        region = region or region_of(price_currency)
        datasets = [f"{region}_{suffix}" for suffix in suffixes]
        factors: dict[date, dict[str, Decimal]] = {}
        for dataset in datasets:
            for day, values in (await self._factors(dataset, frequency)).items():
                factors.setdefault(day, {}).update(values)

        returns = period_returns(period_closes(closes, frequency), frequency)
        rows = align(returns, factors, names, window, end)
        if len(rows) < MIN_PERIODS[frequency]:
            raise ExposureError(
                422,
                f"{len(rows)} {frequency} periods with both a return and the factors; "
                f"at least {MIN_PERIODS[frequency]} are needed",
            )
        if len(rows) < min(window, SHORT_WINDOW[frequency]):
            warnings.append(f"Only {len(rows)} {frequency} periods: the estimates are imprecise.")
        return ExposureResult(
            ticker=ticker,
            listing=listing,
            model=model,
            region=region,
            frequency=frequency,
            datasets=datasets,
            price_currency=price_currency,
            converted_from=converted_from,
            start=rows[0][0],
            end=rows[-1][0],
            regression=regress(rows, names),
            warnings=warnings,
        )
