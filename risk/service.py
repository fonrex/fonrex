"""The risk of a listing, measured on the daily closes the database holds.

The closes are those of the daily bars of the listing, read by
``asset_listing_id`` (rule 12), over the last ``years`` years before the last
close stored. They are all on one basis: the ``adj_close`` (dividends included)
when every bar of the window has one, else the traded ``close`` of every bar,
with a warning: mixing both would make, on the day the basis changes, a return
of the dividends paid since (a bar of TradingView after bars of Yahoo).

The risk-free rate of each day comes from ``macro/risk_free_rates.py``, in the
currency of the listing; without one, every metric is measured but Sharpe and
Sortino, and a warning says why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.price_series import listing_identity, resolve_price_series_async, session_date
from macro.risk_free_rates import RiskFreeRateRepository, RiskFreeRates
from models import PriceEOD
from risk.metrics import MIN_RETURNS, RiskMetrics, RiskMetricsError, measure

DEFAULT_YEARS = 3
DAYS_PER_YEAR = 365.25

Bar = tuple[float | None, float | None]  # (adj_close, close) of a day
T = TypeVar("T")


class RiskError(Exception):
    """The risk of a listing cannot be measured; ``status`` is the HTTP status to answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class RiskResult:
    ticker: str
    listing: dict[str, str | None]
    years: int
    metrics: RiskMetrics
    risk_free: RiskFreeRates
    warnings: list[str] = field(default_factory=list)


def window(closes: Mapping[date, T], years: int) -> dict[date, T]:
    """The closes of the last ``years`` years before the last close."""
    if not closes:
        return {}
    first = max(closes) - timedelta(days=round(DAYS_PER_YEAR * years))
    return {day: price for day, price in closes.items() if day >= first}


def closes_on_one_basis(bars: Mapping[date, Bar]) -> tuple[dict[date, float], str | None]:
    """The ``adj_close`` of every day when each has one, else the ``close`` of every
    day and a warning saying how many lack it."""
    lacking = sum(1 for adjusted, _close in bars.values() if adjusted is None)
    if not lacking:
        return {day: adj for day, (adj, _close) in bars.items() if adj is not None}, None
    warning = (
        f"{lacking} of {len(bars)} daily closes have no adjusted close: every return is "
        "measured on the traded closes, dividends not included"
    )
    return {day: close for day, (_adj, close) in bars.items() if close is not None}, warning


class RiskService:
    """Measures the risk of a listing from what the database holds."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], rates: RiskFreeRateRepository
    ):
        self.session_factory = session_factory
        self.rates = rates

    async def _listing(
        self, ticker: str, currency: str | None, exchange: str | None, isin: str | None
    ) -> tuple[dict[str, str | None] | None, dict[date, Bar]]:
        async with self.session_factory() as session:
            series = await resolve_price_series_async(
                session, ticker, currency=currency, exchange=exchange, isin=isin
            )
            if series is None:
                return None, {}
            identity = (await session.execute(listing_identity(series.listing_id))).one()
            rows = await session.execute(
                select(PriceEOD.timestamp, PriceEOD.adj_close, PriceEOD.close)
                .where(
                    PriceEOD.asset_listing_id == series.listing_id,
                    PriceEOD.resolution == "1D",
                )
                .order_by(PriceEOD.timestamp)
            )
            bars: dict[date, Bar] = {
                session_date(moment): (
                    float(adj) if adj is not None else None,
                    float(close) if close is not None else None,
                )
                for moment, adj, close in rows
                if adj is not None or close is not None
            }
        listing = {
            "ticker": identity.ticker,
            "isin": identity.isin,
            "currency": identity.currency,
            "exchange": identity.exchange,
        }
        return listing, bars

    async def measure(
        self,
        ticker: str,
        years: int = DEFAULT_YEARS,
        confidence: float = 0.95,
        currency: str | None = None,
        exchange: str | None = None,
        isin: str | None = None,
    ) -> RiskResult:
        listing, bars = await self._listing(ticker, currency, exchange, isin)
        if listing is None:
            raise RiskError(404, f"No listing found for {ticker}")
        closes, basis_warning = closes_on_one_basis(window(bars, years))
        if len(closes) <= MIN_RETURNS:
            raise RiskError(
                404,
                f"{len(closes)} daily prices stored for {listing['ticker']} over {years} years: "
                f"at least {MIN_RETURNS + 1} are needed; ingest them with POST /historical/ingest",
            )
        days = sorted(closes)[1:]
        risk_free = await self.rates.rates(listing["currency"], days)
        warnings = [basis_warning] if basis_warning else []
        if risk_free.rates is None:
            warnings.append(f"Sharpe and Sortino not computed: {risk_free.reason}")
        try:
            metrics = measure(closes, risk_free.rates, confidence)
        except RiskMetricsError as error:
            raise RiskError(422, str(error)) from error
        return RiskResult(listing["ticker"] or ticker, listing, years, metrics, risk_free, warnings)
