import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models import Asset, AssetListing

logger = logging.getLogger(__name__)


class QueryService:
    """
    Service de requête asynchrone pour les données financières.
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

    async def get_asset_id(self, ticker: str) -> Optional[int]:
        """Récupère l'ID d'un actif à partir d'une cotation ou du ticker legacy."""
        async with self.async_session() as session:
            normalized_ticker = ticker.strip().upper()
            stmt = (
                select(AssetListing.asset_id)
                .where(AssetListing.ticker == normalized_ticker)
                .order_by(
                    AssetListing.is_primary.desc(),
                    AssetListing.currency.asc(),
                    AssetListing.exchange.asc(),
                )
            )
            result = await session.execute(stmt)
            asset_id = result.scalars().first()
            if asset_id:
                return asset_id

            stmt = select(Asset.id).where(Asset.ticker == normalized_ticker)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_history(
        self,
        ticker: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        interval: str = "1D",
    ) -> List[Dict[str, Any]]:
        """
        Récupère l'historique des prix pour un ticker donné.

        Args:
            ticker: Le symbole de l'actif.
            start_date: Date de début (optionnel).
            end_date: Date de fin (optionnel).
            interval: Résolution ('1D', '1W', '1M' ou legacy 'daily', 'weekly', 'monthly').
        """
        asset_id = await self.get_asset_id(ticker)
        if not asset_id:
            return []

        # Normalisation de l'intervalle en résolution
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
            # Query standard sur prices_eod en filtrant par resolution
            query = """
                SELECT time, open, high, low, close, adj_close, volume, resolution, source
                FROM prices_eod
                WHERE asset_id = :asset_id AND resolution = :resolution
            """
            params = {"asset_id": asset_id, "resolution": res}

            if start_date:
                query += " AND time >= :start_date"
                params["start_date"] = start_date

            if end_date:
                query += " AND time <= :end_date"
                params["end_date"] = end_date

            query += " ORDER BY time DESC"

            result = await session.execute(text(query), params)
            rows = result.mappings().all()

            # Fallback vers la vue weekly/monthly si aucune donnée EOD directe n'a été trouvée pour 1W ou 1M
            if not rows and res in ["1W", "1M"]:
                view_name = "prices_weekly" if res == "1W" else "prices_monthly"
                # Vérifier si la vue existe dans pg_matviews ou dans les agrégats continus TimescaleDB
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
                        WHERE asset_id = :asset_id
                    """
                    fallback_params = {"asset_id": asset_id}
                    if start_date:
                        fallback_query += " AND bucket >= :start_date"
                        fallback_params["start_date"] = start_date
                    if end_date:
                        fallback_query += " AND bucket <= :end_date"
                        fallback_params["end_date"] = end_date
                    fallback_query += " ORDER BY bucket DESC"

                    result = await session.execute(text(fallback_query), fallback_params)
                    rows = result.mappings().all()

            return [dict(row) for row in rows]

    async def get_history_range(self, ticker: str, resolution: str = "1D") -> Dict[str, Any]:
        """
        Récupère les dates minimales et maximales ainsi que le compte de données en base pour un actif.
        """
        asset_id = await self.get_asset_id(ticker)
        if not asset_id:
            return {"min_date": None, "max_date": None, "count": 0}

        async with self.async_session() as session:
            query = """
                SELECT MIN(time) as min_date, MAX(time) as max_date, COUNT(*) as count
                FROM prices_eod
                WHERE asset_id = :asset_id AND resolution = :resolution
            """
            result = await session.execute(
                text(query), {"asset_id": asset_id, "resolution": resolution}
            )
            row = result.mappings().first()
            if row and row["count"] > 0:
                min_dt = row["min_date"]
                max_dt = row["max_date"]
                return {
                    "min_date": min_dt.date() if min_dt else None,
                    "max_date": max_dt.date() if max_dt else None,
                    "count": row["count"],
                }
            return {"min_date": None, "max_date": None, "count": 0}

    async def close(self):
        if self._owns_engine:
            await self.engine.dispose()
