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


class SessionProvider(Protocol):
    def get_session(self) -> Session: ...


TechnicalDatabase: TypeAlias = Session | SessionProvider | Callable[[], Session]


class SqlAlchemyTechnicalRepository:
    """Read asset identity and OHLCV series through SQLAlchemy sessions."""

    def __init__(self, database: TechnicalDatabase) -> None:
        self._database = database

    async def load_ohlcv(
        self,
        asset_id: int,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        return await run_sync(
            self._load_ohlcv_sync,
            asset_id,
            resolution,
            from_date,
            to_date,
            limit,
        )

    def _load_ohlcv_sync(
        self,
        asset_id: int,
        resolution: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        session, close_session = self._session()
        try:
            is_eod = resolution in {"1D", "1W", "1M"}
            table_name = "prices_eod" if is_eod else "prices_intraday"
            time_column = "time" if is_eod else "timestamp"
            clauses = []
            parameters = {
                "asset_id": asset_id,
                "resolution": resolution,
                "limit": limit,
            }
            if from_date:
                clauses.append(f"AND {time_column} >= :from_date")
                parameters["from_date"] = from_date
            if to_date:
                clauses.append(f"AND {time_column} <= :to_date")
                parameters["to_date"] = to_date

            query = f"""
                SELECT * FROM (
                    SELECT {time_column} AS timestamp, open, high, low, close, volume
                    FROM {table_name}
                    WHERE asset_id = :asset_id AND resolution = :resolution
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

    async def resolve_asset_id(self, ticker: str) -> int | None:
        normalized = ticker.strip().upper()
        session, close_session = self._session()

        def resolve() -> int | None:
            try:
                from models import Asset, AssetListing

                listing = (
                    session.query(AssetListing)
                    .filter(
                        AssetListing.ticker == normalized,
                        AssetListing.is_active.is_(True),
                    )
                    .order_by(
                        AssetListing.is_primary.desc(),
                        AssetListing.currency.asc(),
                        AssetListing.exchange.asc(),
                    )
                    .first()
                )
                if listing:
                    return listing.asset_id
                asset = (
                    session.query(Asset)
                    .filter(Asset.ticker == normalized, Asset.is_active.is_(True))
                    .first()
                )
                return asset.id if asset else None
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
