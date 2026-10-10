import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.price_series import (
    PriceSeries,
    listing_identity,
    resolve_price_series_async,
    session_date,
    session_timestamp,
)

logger = logging.getLogger(__name__)


class QueryService:
    """
    Asynchronous query service for financial data.
    """

    def __init__(self, database_url: str = None, session_factory=None, engine=None):
        if session_factory is not None:
            self.async_session = session_factory
            self.engine = engine
            self._owns_engine = False
            return

        if not database_url:
            database_url = os.environ.get(
                "DATABASE_URL", "postgresql://fonrex:fonrex_password@localhost:5432/fonrex"
            )

        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")

        self.engine = create_async_engine(database_url, echo=False)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self._owns_engine = True

    async def get_series(
        self,
        ticker: str,
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Optional[PriceSeries]:
        """Return the price series (instrument and listing) a ticker designates.

        The rule is the one used when the prices are written
        (``database/price_series.py``): what is read is what was ingested.
        ``currency``, ``exchange`` and ``isin`` name one listing among those
        sharing the ticker.
        """
        async with self.async_session() as session:
            return await resolve_price_series_async(
                session, ticker, currency=currency, exchange=exchange, isin=isin
            )

    async def get_listing(
        self,
        ticker: str,
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the identity of the listing a ticker designates, or ``None``.

        ``{"ticker", "isin", "currency", "exchange"}``: what a price answer shows
        so that a client can check which instrument it received.
        """
        series = await self.get_series(ticker, currency, exchange, isin)
        if not series:
            return None
        async with self.async_session() as session:
            row = (await session.execute(listing_identity(series.listing_id))).first()
        if not row:
            return None
        return {
            "ticker": row.ticker,
            "isin": row.isin,
            "currency": row.currency or None,
            # A listing whose exchange is unknown stores an empty string.
            "exchange": row.exchange or None,
        }

    async def get_asset_id(self, ticker: str) -> Optional[int]:
        """The asset id of a listing, or of a legacy ticker."""
        series = await self.get_series(ticker)
        return series.asset_id if series else None

    async def get_history(
        self,
        ticker: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        interval: str = "1D",
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Price history of a ticker.

        Args:
            ticker: Symbol of the asset.
            start_date: First day (optional).
            end_date: Last day (optional).
            interval: Resolution ('1D', '1W', '1M' or legacy 'daily', 'weekly', 'monthly').
        """
        series = await self.get_series(ticker, currency, exchange, isin)
        if not series:
            return []

        # Turn the interval into a resolution
        res_map = {
            "daily": "1D",
            "weekly": "1W",
            "monthly": "1M",
            "1d": "1D",
            "1w": "1W",
            "1m": "1M",
            "1D": "1D",
            "1W": "1W",
            "1M": "1M",
        }
        res = res_map.get(interval.strip(), "1D")

        async with self.async_session() as session:
            # Standard query on prices_eod, filtered by resolution
            # One series = one listing and one resolution. A bar is stored at
            # midnight UTC of its session date: the bounds are sent as such, so
            # the comparison does not depend on the time zone of the session.
            query = """
                SELECT time, open, high, low, close, adj_close, volume, resolution, source
                FROM prices_eod
                WHERE asset_listing_id = :listing_id AND resolution = :resolution
            """
            params = {"listing_id": series.listing_id, "resolution": res}

            if start_date:
                query += " AND time >= :start_date"
                params["start_date"] = session_timestamp(start_date)

            if end_date:
                query += " AND time <= :end_date"
                params["end_date"] = session_timestamp(end_date)

            query += " ORDER BY time DESC"

            result = await session.execute(text(query), params)
            rows = result.mappings().all()

            # Fall back on the weekly/monthly view when no 1W or 1M bar is stored
            if not rows and res in ["1W", "1M"]:
                view_name = "prices_weekly" if res == "1W" else "prices_monthly"
                # Check that the view exists in pg_matviews or among the TimescaleDB continuous aggregates
                check_view_query = """
                    SELECT 1 FROM pg_matviews WHERE matviewname = :view_name
                    UNION
                    SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = :view_name
                """
                view_exists = (
                    await session.execute(text(check_view_query), {"view_name": view_name})
                ).scalar()

                if view_exists:
                    fallback_query = f"""
                        SELECT bucket as time, open, high, low, close, volume
                        FROM {view_name}
                        WHERE asset_listing_id = :listing_id
                    """
                    fallback_params = {"listing_id": series.listing_id}
                    if start_date:
                        fallback_query += " AND bucket >= :start_date"
                        fallback_params["start_date"] = session_timestamp(start_date)
                    if end_date:
                        fallback_query += " AND bucket <= :end_date"
                        fallback_params["end_date"] = session_timestamp(end_date)
                    fallback_query += " ORDER BY bucket DESC"

                    result = await session.execute(text(fallback_query), fallback_params)
                    rows = result.mappings().all()

            return [dict(row) for row in rows]

    async def get_history_range(
        self,
        ticker: str,
        resolution: str = "1D",
        currency: Optional[str] = None,
        exchange: Optional[str] = None,
        isin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        First and last dates and number of stored bars of a listing.
        """
        series = await self.get_series(ticker, currency, exchange, isin)
        if not series:
            return {"min_date": None, "max_date": None, "count": 0}

        async with self.async_session() as session:
            query = """
                SELECT MIN(time) as min_date, MAX(time) as max_date, COUNT(*) as count
                FROM prices_eod
                WHERE asset_listing_id = :listing_id AND resolution = :resolution
            """
            result = await session.execute(
                text(query), {"listing_id": series.listing_id, "resolution": resolution}
            )
            row = result.mappings().first()
            if row and row["count"] > 0:
                min_dt = row["min_date"]
                max_dt = row["max_date"]
                return {
                    "min_date": session_date(min_dt) if min_dt else None,
                    "max_date": session_date(max_dt) if max_dt else None,
                    "count": row["count"],
                }
            return {"min_date": None, "max_date": None, "count": 0}

    async def close(self):
        if self._owns_engine:
            await self.engine.dispose()
