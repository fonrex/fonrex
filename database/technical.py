"""SQLAlchemy adapter for technical-analysis market data."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Protocol, TypeAlias

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from concurrency import run_sync
from database.price_series import resolve_price_series, session_timestamp
from technical.contracts import MarketSeries


class SessionProvider(Protocol):
    def get_session(self) -> Session: ...


TechnicalDatabase: TypeAlias = Session | SessionProvider | Callable[[], Session]


class SqlAlchemyTechnicalRepository:
    """Read asset identity and OHLCV series through SQLAlchemy sessions."""

    def __init__(self, database: TechnicalDatabase) -> None:
        self._database = database

    async def load_ohlcv(
        self,
        series: MarketSeries,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        return await run_sync(
            self._load_ohlcv_sync,
            series,
            resolution,
            from_date,
            to_date,
            limit,
        )

    def _load_ohlcv_sync(
        self,
        series: MarketSeries,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        session, close_session = self._session()
        try:
            is_eod = resolution in {"1D", "1W", "1M"}
            # End-of-day prices are stored per listing (one currency, one
            # exchange) and dated by session at midnight UTC; intraday prices
            # are stored per instrument.
            if is_eod:
                table_name, time_column = "prices_eod", "time"
                series_filter = "asset_listing_id = :series_id"
                series_id = series.listing_id
                lower = session_timestamp(from_date) if from_date else None
                upper = session_timestamp(to_date) if to_date else None
            else:
                table_name, time_column = "prices_intraday", "timestamp"
                series_filter = "asset_id = :series_id"
                series_id = series.asset_id
                lower, upper = from_date, to_date
            clauses = []
            parameters = {
                "series_id": series_id,
                "resolution": resolution,
                "limit": limit,
            }
            if lower:
                clauses.append(f"AND {time_column} >= :from_date")
                parameters["from_date"] = lower
            if upper:
                clauses.append(f"AND {time_column} <= :to_date")
                parameters["to_date"] = upper

            query = f"""
                SELECT * FROM (
                    SELECT {time_column} AS timestamp, open, high, low, close, volume
                    FROM {table_name}
                    WHERE {series_filter} AND resolution = :resolution
                      {" ".join(clauses)}
                    ORDER BY {time_column} DESC
                    LIMIT :limit
                ) sub
                ORDER BY timestamp ASC
            """
            rows = session.execute(text(query), parameters).mappings().all()
            return self._rows_to_dataframe(rows)
        finally:
            if close_session:
                session.close()

    async def resolve_series(self, ticker: str) -> MarketSeries | None:
        """Resolve a ticker with the rule shared by every reader of the prices."""

        def resolve() -> MarketSeries | None:
            session, close_session = self._session()
            try:
                series = resolve_price_series(session, ticker, active_only=True)
                if series is None:
                    return None
                return MarketSeries(asset_id=series.asset_id, listing_id=series.listing_id)
            finally:
                if close_session:
                    session.close()

        return await run_sync(resolve)

    def _session(self) -> tuple[Session, bool]:
        if hasattr(self._database, "get_session"):
            return self._database.get_session(), True
        if callable(self._database):
            return self._database(), True
        return self._database, False

    @staticmethod
    def _rows_to_dataframe(rows: list[Mapping[str, object]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        dataframe = pd.DataFrame([dict(row) for row in rows])
        dataframe.columns = [column.lower() for column in dataframe.columns]
        dataframe["timestamp"] = pd.to_datetime(dataframe["timestamp"], utc=True)
        dataframe.set_index("timestamp", inplace=True)
        for column in ["open", "high", "low", "close", "volume"]:
            if column in dataframe.columns:
                dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce").astype(
                    np.float64
                )
        dataframe.dropna(subset=["close"], inplace=True)
        dataframe.sort_index(inplace=True)
        return dataframe
