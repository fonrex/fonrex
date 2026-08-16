import asyncio
import logging
import os
import random
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from concurrency import run_sync
from database.query import QueryService
from database.service import DatabaseService
from historical.normalization import normalize_bars
from historical.providers import HistoricalMarketDataFetcher
from models import Asset, AssetListing, IngestLog, PriceEOD
from schemas.historical import IngestResult

logger = logging.getLogger(__name__)


class HistoricalIngestionService:
    """
    Historical EOD price data ingestion service.
    Handles multi-source (yfinance / TradingView) and multi-resolution (1D, 1W, 1M).
    """

    def __init__(
        self,
        db_service: DatabaseService,
        query_service: QueryService,
        redis_client: Optional[Any] = None,
        market_data_fetcher: HistoricalMarketDataFetcher | None = None,
    ):
        self.db_service = db_service
        self.query_service = query_service
        self.redis_client = redis_client
        self._market_data_fetcher = market_data_fetcher or HistoricalMarketDataFetcher()

        # Configuration depuis variables d'environnement
        self.concurrency = int(os.environ.get("INGEST_CONCURRENCY", 5))
        self.yf_delay = float(os.environ.get("INGEST_YF_DELAY", 0.5))
        self.tv_delay = float(os.environ.get("INGEST_TV_DELAY", 2.0))
        self.batch_size = int(os.environ.get("INGEST_BATCH_SIZE", 1000))

    async def ingest(
        self,
        ticker: str,
        resolution: str = "1D",
        source: str = "auto",
        force_refresh: bool = False,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
    ) -> IngestResult:
        """
        Ingère l'historique d'un ticker individuel.
        """
        start_time = time.time()
        resolution = resolution.upper()
        if resolution not in ["1D", "1W", "1M"]:
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="failed",
                error=f"Résolution non supportée: {resolution}",
            )

        # 1. Résoudre l'Asset et la cotation
        asset_id, listing_id, isin = await run_sync(self._resolve_asset_context, ticker)
        if not asset_id:
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="failed",
                error=f"Actif introuvable pour le ticker {ticker}",
            )

        # 2. Détecter les plages de dates à charger (Gap Detection)
        fetch_start, fetch_end, is_up_to_date = await self._detect_gaps(
            ticker, resolution, force_refresh, from_date, to_date
        )

        if is_up_to_date:
            duration_ms = int((time.time() - start_time) * 1000)
            await self._log_ingest(
                asset_id=asset_id,
                ticker=ticker,
                resolution=resolution,
                source=source,
                status="up_to_date",
                records_added=0,
                from_date=from_date,
                to_date=to_date,
                duration_ms=duration_ms,
            )
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="up_to_date",
                records_added=0,
                from_date=from_date,
                to_date=to_date,
                duration_ms=duration_ms,
            )

        # 3. Récupérer les données avec fallback
        logger.info(
            f"🔄 Ingestion {ticker} ({resolution}): {fetch_start} -> {fetch_end} via {source}"
        )
        fetch_result = await self._fetch_with_fallback(
            ticker, resolution, source, fetch_start, fetch_end
        )

        if not fetch_result or not fetch_result.get("bars"):
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = fetch_result.get("error") if fetch_result else "Aucune donnée récupérée"
            await self._log_ingest(
                asset_id=asset_id,
                ticker=ticker,
                resolution=resolution,
                source=source,
                status="failed",
                records_added=0,
                error_msg=error_msg,
                duration_ms=duration_ms,
            )
            return IngestResult(
                ticker=ticker,
                resolution=resolution,
                status="failed",
                source_used=source,
                records_added=0,
                duration_ms=duration_ms,
                error=error_msg,
            )

        bars = fetch_result["bars"]
        source_used = fetch_result["source_used"]

        # 4. Nettoyer et normaliser
        normalized_bars = self._normalize_bars(bars, asset_id, listing_id, resolution)

        # 5. Insérer en base (Upsert)
        records_added = await self._upsert_prices_eod(normalized_bars)

        # 6. Invalider le cache Redis
        await self._invalidate_cache(ticker)

        # 7. Logger l'opération
        duration_ms = int((time.time() - start_time) * 1000)
        status = "success" if records_added > 0 else "up_to_date"

        actual_from = min(b["time"].date() for b in normalized_bars) if normalized_bars else None
        actual_to = max(b["time"].date() for b in normalized_bars) if normalized_bars else None

        await self._log_ingest(
            asset_id=asset_id,
            ticker=ticker,
            resolution=resolution,
            source=source_used,
            status=status,
            records_added=records_added,
            from_date=actual_from,
            to_date=actual_to,
            duration_ms=duration_ms,
        )

        return IngestResult(
            ticker=ticker,
            resolution=resolution,
            status=status,
            source_used=source_used,
            records_added=records_added,
            from_date=actual_from,
            to_date=actual_to,
            duration_ms=duration_ms,
        )

    async def ingest_bulk(
        self,
        tickers: List[str],
        resolution: str = "1D",
        source: str = "auto",
        force_refresh: bool = False,
        concurrency: Optional[int] = None,
    ) -> List[IngestResult]:
        """
        Ingère une liste de tickers en parallèle avec limitation de concurrence.
        """
        sem_limit = concurrency or self.concurrency
        semaphore = asyncio.Semaphore(sem_limit)
        results = []

        async def worker(ticker: str):
            async with semaphore:
                # Délai aléatoire pour lisser la charge et éviter les bans
                await asyncio.sleep(random.uniform(0.1, 0.5))
                try:
                    res = await self.ingest(
                        ticker=ticker,
                        resolution=resolution,
                        source=source,
                        force_refresh=force_refresh,
                    )
                    results.append(res)
                except Exception as e:
                    logger.error(f"❌ Erreur critique lors de l'ingestion bulk de {ticker}: {e}")
                    results.append(
                        IngestResult(
                            ticker=ticker, resolution=resolution, status="failed", error=str(e)
                        )
                    )

        tasks = [worker(t) for t in tickers]
        await asyncio.gather(*tasks)
        return results

    async def _detect_gaps(
        self,
        ticker: str,
        resolution: str,
        force_refresh: bool,
        from_date: Optional[date],
        to_date: Optional[date],
    ) -> Tuple[date, date, bool]:
        """
        Détermine la plage de dates manquante.
        """
        today = date.today()
        end_date = to_date or today

        if force_refresh:
            start_date = from_date or (today - timedelta(days=365 * 10))  # 10 ans par défaut
            return start_date, end_date, False

        # Vérifier en base les données existantes
        db_range = await self.query_service.get_history_range(ticker, resolution)

        if db_range["count"] == 0:
            start_date = from_date or (today - timedelta(days=365 * 10))
            return start_date, end_date, False

        max_date_db = db_range["max_date"]

        # Si la date max en base est aujourd'hui ou hier, on considère à jour
        if max_date_db and max_date_db >= (today - timedelta(days=1)):
            if from_date and from_date < db_range["min_date"]:
                # Si l'utilisateur demande plus de données historiques (plus anciennes)
                return from_date, db_range["min_date"] - timedelta(days=1), False
            return max_date_db, end_date, True

        # Ingestion incrémentale
        start_date = max_date_db + timedelta(days=1)
        if start_date >= end_date:
            return max_date_db, end_date, True

        return start_date, end_date, False

    async def _fetch_with_fallback(
        self, ticker: str, resolution: str, source: str, start: date, end: date
    ) -> Optional[Dict[str, Any]]:
        """
        Tente de fetch les données depuis la source choisie, avec fallback si auto.
        """
        if source == "yfinance":
            return await self._fetch_yfinance(ticker, resolution, start, end)
        elif source == "tradingview":
            return await self._fetch_tradingview(ticker, resolution, start, end)

        # Mode auto: yfinance d'abord, puis TradingView en fallback
        yf_res = await self._fetch_yfinance(ticker, resolution, start, end)
        if yf_res and yf_res.get("bars"):
            return yf_res

        logger.warning(f"⚠️ Échec yfinance pour {ticker}, passage au fallback TradingView...")
        await asyncio.sleep(self.yf_delay)
        return await self._fetch_tradingview(ticker, resolution, start, end)

    async def _fetch_yfinance(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> Optional[Dict[str, Any]]:
        return await self._market_data_fetcher.fetch_yfinance(ticker, resolution, start, end)

    def _fetch_yfinance_sync(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> Dict[str, Any]:
        return self._market_data_fetcher._fetch_yfinance_sync(ticker, resolution, start, end)

    async def _fetch_tradingview(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> Optional[Dict[str, Any]]:
        return await self._market_data_fetcher.fetch_tradingview(ticker, resolution, start, end)

    def _fetch_tradingview_sync(
        self, ticker: str, resolution: str, start: date, end: date
    ) -> Dict[str, Any]:
        return self._market_data_fetcher._fetch_tradingview_sync(ticker, resolution, start, end)

    def _resolve_tv_symbol_sync(self, ticker: str) -> Optional[str]:
        return self._market_data_fetcher._resolve_tradingview_symbol(ticker)

    def _resolve_asset(self, ticker: str, session: Session) -> Tuple[Optional[int], Optional[int]]:
        """
        Résout un ticker en (asset_id, listing_id).
        """
        normalized = ticker.strip().upper()
        # 1. Rechercher dans les listings
        listing = (
            session.query(AssetListing)
            .filter(AssetListing.ticker == normalized)
            .order_by(
                AssetListing.is_primary.desc(),
                AssetListing.currency.asc(),
                AssetListing.exchange.asc(),
            )
            .first()
        )
        if listing:
            return listing.asset_id, listing.id

        # 2. Repli vers la table assets historique
        asset = session.query(Asset).filter(Asset.ticker == normalized).first()
        if asset:
            return asset.id, None

        return None, None

    def _normalize_bars(
        self, bars: List[Dict[str, Any]], asset_id: int, listing_id: Optional[int], resolution: str
    ) -> List[Dict[str, Any]]:
        return normalize_bars(bars, asset_id, listing_id, resolution)

    async def _upsert_prices_eod(self, bars: List[Dict[str, Any]]) -> int:
        """
        Effectue un batch upsert ultra-rapide des prix EOD.
        """
        if not bars:
            return 0

        return await run_sync(self._upsert_prices_eod_sync, bars)

    def _upsert_prices_eod_sync(self, bars: List[Dict[str, Any]]) -> int:
        session = self.db_service.get_session()
        total_inserted = 0
        try:
            # Upsert par morceaux pour éviter de saturer la mémoire et dépasser la limite de paramètres Postgres
            for i in range(0, len(bars), self.batch_size):
                chunk = bars[i : i + self.batch_size]

                stmt = pg_insert(PriceEOD.__table__)
                update_cols = {
                    c.name: stmt.excluded[c.name]
                    for c in PriceEOD.__table__.columns
                    if c.name not in ["time", "asset_id"]
                }

                upsert_stmt = stmt.on_conflict_do_update(
                    index_elements=["time", "asset_id"], set_=update_cols
                )

                session.execute(upsert_stmt, chunk)
                total_inserted += len(chunk)

            session.commit()
            return total_inserted
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Erreur lors de l'upsert des prix EOD: {e}")
            raise e
        finally:
            session.close()

    async def _invalidate_cache(self, ticker: str):
        """
        Invalide les clés de cache Redis pour ce ticker.
        """
        if not self.redis_client:
            return
        try:
            pattern = f"history:{ticker}:*"
            cursor = 0
            keys_to_delete = []
            while True:
                cursor, keys = await self.redis_client.scan(cursor=cursor, match=pattern, count=100)
                keys_to_delete.extend(keys)
                if cursor == 0:
                    break
            if keys_to_delete:
                await self.redis_client.delete(*keys_to_delete)
                logger.info(
                    f"🧹 Cache invalidé pour {ticker} ({len(keys_to_delete)} clés supprimées)"
                )
        except Exception as e:
            logger.warning(f"⚠️ Erreur d'invalidation Redis pour {ticker}: {e}")

    async def _log_ingest(
        self,
        asset_id: int,
        ticker: str,
        resolution: str,
        source: str,
        status: str,
        records_added: int = 0,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        error_msg: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ):
        """
        Crée un enregistrement de log d'ingestion.
        """
        await run_sync(
            self._log_ingest_sync,
            asset_id,
            ticker,
            resolution,
            source,
            status,
            records_added,
            from_date,
            to_date,
            error_msg,
            duration_ms,
        )

    def _resolve_asset_context(self, ticker):
        session = self.db_service.get_session()
        try:
            asset_id, listing_id = self._resolve_asset(ticker, session)
            asset = session.get(Asset, asset_id) if asset_id else None
            return asset_id, listing_id, asset.isin if asset else None
        finally:
            session.close()

    def _log_ingest_sync(
        self,
        asset_id: int,
        ticker: str,
        resolution: str,
        source: str,
        status: str,
        records_added: int,
        from_date: Optional[date],
        to_date: Optional[date],
        error_msg: Optional[str],
        duration_ms: Optional[int],
    ):
        session = self.db_service.get_session()
        try:
            log_entry = IngestLog(
                asset_id=asset_id,
                ticker=ticker,
                resolution=resolution,
                source=source,
                status=status,
                records_added=records_added,
                from_date=from_date,
                to_date=to_date,
                error_msg=error_msg,
                duration_ms=duration_ms,
            )
            session.add(log_entry)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"❌ Erreur lors de l'enregistrement du log d'ingestion: {e}")
        finally:
            session.close()
