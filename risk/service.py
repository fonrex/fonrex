"""The risk of a listing, measured on the daily closes the database holds.

The closes are the ``adj_close`` of the daily bars of the listing (dividends
included), read by ``asset_listing_id`` (rule 12), over the last ``years`` years
before the last close stored. The risk-free rate of each day comes from
``macro/risk_free_rates.py``, in the currency of the listing; without one, every
metric is measured but Sharpe and Sortino, and a warning says why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.price_series import listing_identity, resolve_price_series_async, session_date
from macro.risk_free_rates import RiskFreeRateRepository, RiskFreeRates
from models import PriceEOD
from risk.metrics import MIN_RETURNS, RiskMetrics, RiskMetricsError, measure

DEFAULT_YEARS = 3
DAYS_PER_YEAR = 365.25


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


def window(closes: dict[date, float], years: int) -> dict[date, float]:
    """The closes of the last ``years`` years before the last close."""
    if not closes:
        return {}
    first = max(closes) - timedelta(days=round(DAYS_PER_YEAR * years))
    return {day: price for day, price in closes.items() if day >= first}


class RiskService:
    """Measures the risk of a listing from what the database holds."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], rates: RiskFreeRateRepository
    ):
        self.session_factory = session_factory
        self.rates = rates

    async def _listing(
        self, ticker: str, currency: str | None, exchange: str | None, isin: str | None
    ) -> tuple[dict[str, str | None] | None, dict[date, float]]:
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
            closes = {
                session_date(moment): float(adj if adj is not None else close)
                for moment, adj, close in rows
                if (adj if adj is not None else close) is not None
            }
        listing = {
            "ticker": identity.ticker,
            "isin": identity.isin,
            "currency": identity.currency,
            "exchange": identity.exchange,
        }
        return listing, closes

    async def measure(
        self,
        ticker: str,
        years: int = DEFAULT_YEARS,
        confidence: float = 0.95,
        currency: str | None = None,
        exchange: str | None = None,
        isin: str | None = None,
    ) -> RiskResult:
        listing, closes = await self._listing(ticker, currency, exchange, isin)
        if listing is None:
            raise RiskError(404, f"No listing found for {ticker}")
        closes = window(closes, years)
        if len(closes) <= MIN_RETURNS:
            raise RiskError(
                404,
                f"{len(closes)} daily prices stored for {listing['ticker']} over {years} years: "
                f"at least {MIN_RETURNS + 1} are needed; ingest them with POST /historical/ingest",
            )
        days = sorted(closes)[1:]
        risk_free = await self.rates.rates(listing["currency"], days)
        warnings = []
        if risk_free.rates is None:
            warnings.append(f"Sharpe and Sortino not computed: {risk_free.reason}")
        try:
            metrics = measure(closes, risk_free.rates, confidence)
        except RiskMetricsError as error:
            raise RiskError(422, str(error)) from error
        return RiskResult(listing["ticker"] or ticker, listing, years, metrics, risk_free, warnings)
