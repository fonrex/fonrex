"""
RealtimePriceWorker — Worker asyncio qui maintient les connexions WebSocket
TradingView et distribue les prix temps réel via Redis Pub/Sub.

Design :
- Un asyncio.Task par ticker abonné
- Sémaphore global : max TV_MAX_CONNECTIONS connexions simultanées TradingView
- Reconnexion automatique en cas de déconnexion (backoff exponentiel)
- Distribution via Redis Pub/Sub canal "price:{ticker}"
- Cache Redis clé "quote:{ticker}" avec TTL configurable
- Persistance dans prices_intraday toutes les bougies 1min complètes

Lifecycle :
    worker = RealtimePriceWorker(redis_client, db_session_factory)
    await worker.start()           # Démarre le worker (lifespan FastAPI)
    await worker.subscribe("AIR.PA")
    await worker.unsubscribe("AIR.PA")
    await worker.stop()            # Arrêt propre (lifespan FastAPI)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional, Set

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Asset, PriceIntraday, RealtimeSubscription
from schemas.realtime import RealtimeTick

logger = logging.getLogger(__name__)

# ── Configuration depuis les variables d'environnement ──────────────────────
TV_MAX_CONNECTIONS = int(os.getenv("TV_MAX_CONNECTIONS", "10"))
RECONNECT_DELAY_INIT = int(os.getenv("TV_RECONNECT_DELAY", "5"))
RECONNECT_DELAY_MAX = 60
QUOTE_TTL = int(os.getenv("REALTIME_QUOTE_TTL", "60"))

# ── Clés Redis ───────────────────────────────────────────────────────────────
REDIS_QUOTE_KEY = "quote:{ticker}"  # Dernier prix (TTL QUOTE_TTL)
REDIS_PUBSUB_CHAN = "price:{ticker}"  # Canal Pub/Sub
REDIS_SUBS_SET = "realtime:subscriptions"  # Set des tickers actifs

# ── Mapping Yahoo ticker suffix → TradingView exchange ───────────────────────
TV_EXCHANGE_MAP: dict[str, str] = {
    ".PA": "EURONEXT",
    ".AS": "EURONEXT",
    ".BR": "EURONEXT",
    ".DE": "XETRA",
    ".F": "FWB",
    ".L": "LSE",
    ".MI": "MIL",
    ".MC": "BME",
    ".ST": "OMX",
    ".OL": "OSL",
    ".HE": "OMXHEX",
    ".SW": "SIX",
    ".TO": "TSX",
    ".AX": "ASX",
}

# Thread pool dédié pour exécuter le Streamer TradingView (synchrone)
_executor = ThreadPoolExecutor(max_workers=TV_MAX_CONNECTIONS + 2, thread_name_prefix="tv_stream")


class RealtimePriceWorker:
    """
    Worker central de streaming temps réel.
    S'instancie une seule fois au démarrage de l'API (lifespan FastAPI).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        async_session_factory: Callable[[], AsyncSession],
    ):
        self.redis = redis_client
        self._session_factory = async_session_factory

        # asyncio state
        self._tasks: Dict[str, asyncio.Task] = {}  # ticker → Task
        self._running: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

        # Rate-limiting TradingView
        self._semaphore = asyncio.Semaphore(TV_MAX_CONNECTIONS)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Démarre le worker.
        Restaure les abonnements actifs depuis realtime_subscriptions en base.
        Appeler dans le lifespan context manager FastAPI.
        """
        self._running = True
        logger.info("[RealtimeWorker] Démarrage...")

        active_subs = await self._load_active_subscriptions()
        for sub in active_subs:
            await self.subscribe(sub.ticker, persist=False)

        logger.info(f"[RealtimeWorker] {len(active_subs)} abonnements restaurés")

    async def stop(self) -> None:
        """
        Arrête proprement tous les streamers.
        Appeler dans le lifespan context manager FastAPI (shutdown).
        """
        self._running = False
        for ticker, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("[RealtimeWorker] Arrêté proprement")

    # ── Gestion des abonnements ──────────────────────────────────────────────

    async def subscribe(self, ticker: str, persist: bool = True) -> bool:
        """
        Abonne un ticker au streaming temps réel.

        1. Résoudre (tv_exchange, tv_symbol) depuis le ticker Yahoo
        2. Vérifier que l'asset existe en base
        3. Créer/activer la RealtimeSubscription (si persist=True)
        4. Lancer la asyncio.Task de streaming pour ce ticker
        5. Ajouter le ticker au Set Redis realtime:subscriptions

        Retourne True si abonnement réussi, False sinon.
        """
        ticker = ticker.upper()

        async with self._lock:
            if ticker in self._tasks and not self._tasks[ticker].done():
                logger.debug(f"[{ticker}] Déjà abonné")
                return True

        tv_exchange, tv_symbol = self._resolve_tv_symbol(ticker)

        # Vérifier que l'asset existe en base
        asset_id: Optional[int] = None
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(Asset.id).where(Asset.ticker == ticker).limit(1)
                )
                row = result.first()
                if row:
                    asset_id = row[0]
        except SQLAlchemyError as e:
            logger.warning(f"[{ticker}] Impossible de vérifier l'asset en base: {e}")

        if asset_id is None:
            logger.warning(f"[{ticker}] Asset introuvable en base — streaming sans persistance DB")

        # Créer/activer la subscription en base
        if persist and asset_id is not None:
            try:
                await self._upsert_subscription(
                    asset_id=asset_id,
                    ticker=ticker,
                    tv_exchange=tv_exchange,
                    tv_symbol=tv_symbol,
                )
            except SQLAlchemyError as e:
                logger.warning(f"[{ticker}] Erreur persistance subscription: {e}")

        # Lancer la task de streaming
        task = asyncio.create_task(
            self._stream_ticker(ticker, tv_exchange, tv_symbol, asset_id),
            name=f"stream_{ticker}",
        )
        async with self._lock:
            self._tasks[ticker] = task

        # Ajouter au Set Redis
        try:
            await self.redis.sadd(REDIS_SUBS_SET, ticker)
        except RedisError as e:
            logger.warning(f"[{ticker}] Erreur mise à jour Redis set: {e}")

        logger.info(f"[RealtimeWorker] ✅ Abonné à {ticker} ({tv_exchange}:{tv_symbol})")
        return True

    async def unsubscribe(self, ticker: str) -> bool:
        """
        Désabonne un ticker du streaming.

        1. Annuler la Task asyncio associée
        2. Marquer is_active=False dans realtime_subscriptions
        3. Retirer du Set Redis
        4. Supprimer la clé Redis quote:{ticker}

        Retourne True si désabonnement réussi.
        """
        ticker = ticker.upper()

        async with self._lock:
            task = self._tasks.pop(ticker, None)

        if task is None:
            return False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Désactiver en base
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(RealtimeSubscription)
                    .where(RealtimeSubscription.ticker == ticker)
                    .values(is_active=False)
                )
                await session.commit()
        except SQLAlchemyError as e:
            logger.warning(f"[{ticker}] Erreur désactivation DB: {e}")

        # Nettoyer Redis
        try:
            await self.redis.srem(REDIS_SUBS_SET, ticker)
            await self.redis.delete(REDIS_QUOTE_KEY.format(ticker=ticker))
        except RedisError as e:
            logger.warning(f"[{ticker}] Erreur nettoyage Redis: {e}")

        logger.info(f"[RealtimeWorker] 🔕 Désabonné de {ticker}")
        return True

    async def get_active_tickers(self) -> Set[str]:
        """Retourne l'ensemble des tickers actuellement streamés."""
        try:
            members = await self.redis.smembers(REDIS_SUBS_SET)
            return {m.decode() if isinstance(m, bytes) else m for m in members}
        except RedisError:
            # Fallback : inspecter les tasks locales
            async with self._lock:
                return set(self._tasks.keys())

    # ── Streaming par ticker ─────────────────────────────────────────────────

    async def _stream_ticker(
        self,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
        asset_id: Optional[int],
    ) -> None:
        """
        Task asyncio principale pour un ticker.
        Tourne en boucle infinie avec reconnexion automatique (backoff exponentiel).
        """
        delay = RECONNECT_DELAY_INIT

        while self._running:
            try:
                logger.info(f"[{ticker}] Connexion TradingView ({tv_exchange}:{tv_symbol})")
                await self._run_streamer(ticker, tv_exchange, tv_symbol, asset_id)
                delay = RECONNECT_DELAY_INIT  # reset si succès propre
            except asyncio.CancelledError:
                logger.info(f"[{ticker}] Streaming arrêté proprement")
                return
            except Exception as e:
                logger.warning(f"[{ticker}] Erreur streaming: {e!r}. Reconnexion dans {delay}s")
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _run_streamer(
        self,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
        asset_id: Optional[int],
    ) -> None:
        """
        Exécute le Streamer TradingView en mode streaming continu.

        tradingview-scraper est synchrone → `run_in_executor`.
        Le Streamer retourne un generator infini (streaming continu).
        timeframe="1" → bougies 1 minute
        numb_price_candles=1 → tick le plus récent uniquement
        """
        from tradingview_scraper.symbols.stream import Streamer

        loop = asyncio.get_event_loop()

        def _blocking_stream():
            """Exécuté dans le thread pool. Retourne un générateur."""
            streamer = Streamer(export_result=False, export_type="json")
            return streamer.stream(
                exchange=tv_exchange,
                symbol=tv_symbol,
                timeframe="1",
                numb_price_candles=1,
            )

        async with self._semaphore:
            # Créer le générateur dans le executor
            gen = await loop.run_in_executor(_executor, _blocking_stream)

            # Itérer sur le générateur de manière asynchrone
            while self._running:
                try:
                    raw_data = await loop.run_in_executor(_executor, next, gen)
                    if raw_data:
                        await self._process_tick(ticker, raw_data, asset_id)
                except StopIteration:
                    logger.info(f"[{ticker}] Générateur TradingView épuisé — reconnexion")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[{ticker}] Erreur lecture tick: {e}")
                    raise

    async def _process_tick(
        self,
        ticker: str,
        raw_data: dict,
        asset_id: Optional[int],
    ) -> None:
        """
        Traite un tick reçu de TradingView.

        1. Valider et normaliser les données → RealtimeTick
        2. Stocker dans Redis (SET quote:{ticker} EX QUOTE_TTL + PUBLISH price:{ticker})
        3. Si asset connu → upsert prices_intraday
        4. Mettre à jour last_tick_at et tick_count dans realtime_subscriptions
        """
        # ── Validation ───────────────────────────────────────────────────────
        try:
            close_raw = raw_data.get("close") or raw_data.get("Close")
            if not close_raw:
                return

            close_val = Decimal(str(close_raw))
            if close_val <= 0:
                logger.debug(f"[{ticker}] Tick rejeté: close={close_val}")
                return

            # Timestamp
            ts_raw = raw_data.get("timestamp") or raw_data.get("time")
            if ts_raw is None:
                tick_ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, (int, float)):
                tick_ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
            else:
                tick_ts = datetime.fromisoformat(str(ts_raw))
                if tick_ts.tzinfo is None:
                    tick_ts = tick_ts.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            # Ignorer les ticks trop anciens (>5min) ou dans le futur
            if tick_ts > now + timedelta(minutes=1):
                logger.debug(f"[{ticker}] Tick dans le futur ignoré: {tick_ts}")
                return
            if tick_ts < now - timedelta(minutes=5):
                logger.debug(f"[{ticker}] Tick trop ancien ignoré: {tick_ts}")
                return

            volume_raw = raw_data.get("volume") or raw_data.get("Volume")
            volume = int(volume_raw) if volume_raw is not None else None
            if volume is not None and volume < 0:
                volume = None

            def _to_dec(v) -> Optional[Decimal]:
                try:
                    return Decimal(str(v)) if v is not None else None
                except InvalidOperation:
                    return None

            tick = RealtimeTick(
                ticker=ticker,
                timestamp=tick_ts,
                open=_to_dec(raw_data.get("open") or raw_data.get("Open")),
                high=_to_dec(raw_data.get("high") or raw_data.get("High")),
                low=_to_dec(raw_data.get("low") or raw_data.get("Low")),
                close=close_val,
                volume=volume,
                source="tradingview",
            )

        except (ValueError, InvalidOperation, TypeError) as e:
            logger.warning(f"[{ticker}] Tick invalide ignoré: {e} — {raw_data}")
            return

        # ── Redis ────────────────────────────────────────────────────────────
        tick_json = tick.model_dump(mode="json")
        tick_str = json.dumps(tick_json, default=str)
        try:
            pipe = self.redis.pipeline()
            pipe.set(
                REDIS_QUOTE_KEY.format(ticker=ticker),
                tick_str,
                ex=QUOTE_TTL,
            )
            pipe.publish(
                REDIS_PUBSUB_CHAN.format(ticker=ticker),
                tick_str,
            )
            await pipe.execute()
        except RedisError as e:
            logger.warning(f"[{ticker}] Erreur Redis publish/set: {e}")

        # ── Persistance DB ───────────────────────────────────────────────────
        if asset_id is not None:
            try:
                await self._upsert_intraday(asset_id, tick)
            except SQLAlchemyError as e:
                logger.warning(f"[{ticker}] Erreur upsert prices_intraday: {e}")

            # Mettre à jour last_tick_at + tick_count
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(RealtimeSubscription)
                        .where(RealtimeSubscription.asset_id == asset_id)
                        .values(
                            last_tick_at=datetime.now(timezone.utc),
                            tick_count=RealtimeSubscription.tick_count + 1,
                        )
                    )
                    await session.commit()
            except SQLAlchemyError as e:
                logger.debug(f"[{ticker}] Erreur mise à jour subscription stats: {e}")

        logger.debug(f"[{ticker}] Tick traité: close={tick.close} ts={tick.timestamp}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_tv_symbol(self, ticker: str) -> tuple[str, str]:
        """
        Convertit un ticker Yahoo Finance en (tv_exchange, tv_symbol).

        Exemples :
        "AIR.PA" → ("EURONEXT", "AIR")
        "BNP.PA" → ("EURONEXT", "BNP")
        "BMW.DE" → ("XETRA", "BMW")
        "AAPL"   → ("NASDAQ", "AAPL")
        "TSLA"   → ("NASDAQ", "TSLA")
        """
        ticker_up = ticker.upper()
        for suffix, exchange in TV_EXCHANGE_MAP.items():
            if ticker_up.endswith(suffix.upper()):
                symbol = ticker[: -(len(suffix))]
                return exchange, symbol.upper()
        return "NASDAQ", ticker_up

    async def _load_active_subscriptions(self) -> list[RealtimeSubscription]:
        """Charge les abonnements actifs depuis realtime_subscriptions en base."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(RealtimeSubscription).where(
                        RealtimeSubscription.is_active == True  # noqa: E712
                    )
                )
                return list(result.scalars().all())
        except SQLAlchemyError as e:
            logger.warning(f"[RealtimeWorker] Impossible de charger les subscriptions: {e}")
            return []

    async def _upsert_subscription(
        self,
        asset_id: int,
        ticker: str,
        tv_exchange: str,
        tv_symbol: str,
    ) -> None:
        """Upsert d'une RealtimeSubscription (insert or update is_active=True)."""
        async with self._session_factory() as session:
            stmt = (
                pg_insert(RealtimeSubscription)
                .values(
                    asset_id=asset_id,
                    ticker=ticker,
                    tv_exchange=tv_exchange,
                    tv_symbol=tv_symbol,
                    is_active=True,
                    subscribed_at=datetime.now(timezone.utc),
                    tick_count=0,
                )
                .on_conflict_do_update(
                    constraint="uq_realtime_sub_asset",
                    set_={
                        "ticker": ticker,
                        "tv_exchange": tv_exchange,
                        "tv_symbol": tv_symbol,
                        "is_active": True,
                        "subscribed_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def _upsert_intraday(self, asset_id: int, tick: RealtimeTick) -> None:
        """
        Upsert d'un tick dans prices_intraday via SQLAlchemy Core.
        ON CONFLICT (timestamp, asset_id) DO UPDATE — atomique.
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(PriceIntraday)
                .values(
                    timestamp=tick.timestamp,
                    asset_id=asset_id,
                    open=tick.open,
                    high=tick.high,
                    low=tick.low,
                    close=tick.close,
                    volume=tick.volume,
                    resolution="1min",
                    source="tradingview",
                )
                .on_conflict_do_update(
                    index_elements=["timestamp", "asset_id"],
                    set_={
                        "open": tick.open,
                        "high": tick.high,
                        "low": tick.low,
                        "close": tick.close,
                        "volume": tick.volume,
                        "source": "tradingview",
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def get_quote_from_cache(self, ticker: str) -> Optional[dict]:
        """
        Récupère le dernier prix depuis Redis.
        Retourne None si pas de données ou TTL expiré.
        """
        try:
            raw = await self.redis.get(REDIS_QUOTE_KEY.format(ticker=ticker.upper()))
            if raw:
                return json.loads(raw)
        except (RedisError, json.JSONDecodeError, TypeError, UnicodeError) as e:
            logger.warning(f"[{ticker}] Erreur lecture Redis quote: {e}")
        return None
