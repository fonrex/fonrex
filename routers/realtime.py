"""HTTP and WebSocket adapters for realtime use cases."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

from routers.errors import raise_http_error
from schemas.realtime import (
    QuoteSnapshot,
    SubscriptionRequest,
    SubscriptionStatus,
    WebSocketMessage,
)
from use_cases.errors import UseCaseError
from use_cases.realtime import (
    GetQuote,
    GetQuotesBatch,
    GetRealtimeStatus,
    SubscribeTickers,
    UnsubscribeTicker,
    require_worker,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])


def _state_dependency(name: str):
    def dependency(connection: HTTPConnection):
        return getattr(connection.app.state, name, None)

    return dependency


get_realtime_worker = _state_dependency("realtime_worker")
get_redis_client = _state_dependency("redis_client")
get_ws_manager = _state_dependency("ws_manager")


def _require_realtime_worker(worker):
    try:
        return require_worker(worker)
    except UseCaseError as error:
        raise_http_error(error)


@router.websocket("/ws/realtime/{ticker}")
async def websocket_realtime(
    websocket: WebSocket,
    ticker: str,
    worker=Depends(get_realtime_worker),
    redis_client=Depends(get_redis_client),
    ws_manager=Depends(get_ws_manager),
):
    """
    WebSocket de streaming prix temps réel pour un ticker.

    Protocol client/serveur :
    - Connexion → le serveur envoie immédiatement le snapshot actuel (si disponible)
    - Push continu → le serveur envoie un message à chaque nouveau tick Redis
    - Client peut envoyer "ping" → serveur répond {"type":"pong"}
    - Client peut envoyer "unsubscribe" → ferme la connexion proprement

    Format des messages serveur :
    {
        "type": "tick",
        "ticker": "AIR.PA",
        "data": { "close": 138.35, "volume": 12500, ... },
        "ts": "2026-05-22T14:32:01Z"
    }
    """
    ticker = ticker.upper()
    worker = _require_realtime_worker(worker)

    await ws_manager.connect(ticker, websocket)

    # S'assurer que le ticker est streamé par le worker
    active_tickers = await worker.get_active_tickers()
    if ticker not in active_tickers:
        asyncio.create_task(worker.subscribe(ticker))

    pubsub = None
    try:
        # Snapshot immédiat depuis Redis
        quote = await worker.get_quote_from_cache(ticker)
        if quote:
            await websocket.send_json(
                WebSocketMessage(
                    type="snapshot",
                    ticker=ticker,
                    data=quote,
                    ts=datetime.now(timezone.utc),
                ).model_dump(mode="json")
            )

        # Abonnement Redis Pub/Sub
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"price:{ticker}")

        async def _redis_listener():
            """Lit les messages Redis Pub/Sub et les envoie aux clients WS."""
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    raw = message["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    tick_data = json.loads(raw)
                    msg = WebSocketMessage(
                        type="tick",
                        ticker=ticker,
                        data=tick_data,
                        ts=datetime.now(timezone.utc),
                    )
                    await ws_manager.broadcast(ticker, msg.model_dump(mode="json"))
                except Exception as e:
                    logger.debug(f"[WS:{ticker}] Erreur traitement message Redis: {e}")

        async def _client_listener():
            """Écoute les messages entrants du client (ping, unsubscribe)."""
            while True:
                try:
                    data = await websocket.receive_text()
                    if data.strip().lower() == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif data.strip().lower() == "unsubscribe":
                        await websocket.close(code=1000)
                        return
                except WebSocketDisconnect:
                    return
                except Exception:
                    return

        # Exécuter les deux listeners en parallèle
        redis_task = asyncio.create_task(_redis_listener())
        client_task = asyncio.create_task(_client_listener())

        done, pending = await asyncio.wait(
            [redis_task, client_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"[WS:{ticker}] Erreur: {e}")
    finally:
        ws_manager.disconnect(ticker, websocket)
        if pubsub:
            try:
                await pubsub.unsubscribe(f"price:{ticker}")
                await pubsub.aclose()
            except Exception:
                pass


@router.get("/quote/{ticker}", response_model=QuoteSnapshot)
async def get_quote(
    ticker: str,
    subscribe_if_missing: bool = True,
    worker=Depends(get_realtime_worker),
):
    try:
        return await GetQuote(worker).execute(ticker, subscribe_if_missing)
    except UseCaseError as error:
        raise_http_error(error)


@router.post("/realtime/subscribe", response_model=list[SubscriptionStatus])
async def subscribe_tickers(
    request: SubscriptionRequest,
    worker=Depends(get_realtime_worker),
):
    try:
        return await SubscribeTickers(worker).execute(request.tickers)
    except UseCaseError as error:
        raise_http_error(error)


@router.delete("/realtime/subscribe/{ticker}")
async def unsubscribe_ticker(
    ticker: str,
    worker=Depends(get_realtime_worker),
):
    try:
        return await UnsubscribeTicker(worker).execute(ticker)
    except UseCaseError as error:
        raise_http_error(error)


@router.get("/realtime/status")
async def get_realtime_status(
    worker=Depends(get_realtime_worker),
    ws_manager=Depends(get_ws_manager),
):
    return await GetRealtimeStatus(worker, ws_manager).execute()


@router.get("/quotes")
async def get_quotes_batch(
    tickers: str,
    worker=Depends(get_realtime_worker),
    redis_client=Depends(get_redis_client),
):
    return await GetQuotesBatch(worker, redis_client).execute(tickers)
