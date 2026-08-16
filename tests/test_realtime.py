import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# S'assurer que le dossier racine est dans le PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fakeredis.aioredis

import main
from main import app
from models import RealtimeSubscription
from realtime.connection_manager import ConnectionManager
from realtime.worker import RealtimePriceWorker


@pytest.mark.asyncio
async def test_connection_manager_drops_disconnected_clients_only():
    manager = ConnectionManager()
    disconnected = AsyncMock()
    disconnected.send_json.side_effect = RuntimeError("closed")
    manager._connections["AAPL"] = {disconnected}

    await manager.broadcast("AAPL", {"type": "tick"})

    assert manager.get_subscriber_count("AAPL") == 0


@pytest.mark.asyncio
async def test_connection_manager_exposes_unexpected_programming_errors():
    manager = ConnectionManager()
    broken = AsyncMock()
    broken.send_json.side_effect = ValueError("invalid payload")
    manager._connections["AAPL"] = {broken}

    with pytest.raises(ValueError, match="invalid payload"):
        await manager.broadcast("AAPL", {"type": "tick"})


# ── FIXTURES ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_redis():
    """Crée une instance asynchrone isolée de FakeRedis (synchrone pour pytest)."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        try:
            asyncio.run(client.flushall())
        except Exception:
            pass


@pytest.fixture
def mock_session():
    """Mock une session SQLAlchemy asynchrone."""

    class MockResult:
        def __init__(self, first_val=None, all_val=None):
            self.first_val = first_val
            self.all_val = all_val or []

        def first(self):
            return self.first_val

        def scalars(self):
            return self

        def all(self):
            return self.all_val

    class AsyncSessionMock:
        def __init__(self):
            self.execute = AsyncMock()
            self.commit = AsyncMock()
            self.rollback = AsyncMock()
            self.close = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    return AsyncSessionMock()


@pytest.fixture
def session_factory(mock_session):
    """Factory qui retourne notre mock de session."""

    def _factory():
        return mock_session

    return _factory


@pytest.fixture
def test_app_client(fake_redis, session_factory):
    """Configure l'application FastAPI de test avec les mocks et retourne un client de test."""
    old_state_worker = getattr(app.state, "realtime_worker", None)
    old_state_redis = getattr(app.state, "redis_client", None)
    old_state_ws_manager = getattr(app.state, "ws_manager", None)

    # Recréer le manager pour isoler les connexions WS
    ws_manager = main.ConnectionManager()

    # Créer le worker de test
    worker = RealtimePriceWorker(fake_redis, session_factory)
    worker._running = True
    app.state.realtime_worker = worker
    app.state.redis_client = fake_redis
    app.state.ws_manager = ws_manager

    client = TestClient(app)
    yield client, worker

    # Restauration
    app.state.realtime_worker = old_state_worker
    app.state.redis_client = old_state_redis
    app.state.ws_manager = old_state_ws_manager


# Helper pour simuler une Task asyncio awaitable et annulable
class AwaitableMockTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def __await__(self):
        async def _dummy():
            pass

        return _dummy().__await__()


# ── TESTS DU WORKER ───────────────────────────────────────────────────────────


class TestRealtimePriceWorker:
    def test_resolve_tv_symbol(self):
        """Vérifie la conversion des symboles Yahoo → TradingView."""
        worker = RealtimePriceWorker(None, None)
        assert worker._resolve_tv_symbol("AIR.PA") == ("EURONEXT", "AIR")
        assert worker._resolve_tv_symbol("BMW.DE") == ("XETRA", "BMW")
        assert worker._resolve_tv_symbol("AAPL") == ("NASDAQ", "AAPL")
        assert worker._resolve_tv_symbol("aapl") == ("NASDAQ", "AAPL")

    @pytest.mark.asyncio
    async def test_subscribe_valid_ticker(self, fake_redis, mock_session, session_factory):
        """Vérifie l'abonnement nominal d'un ticker."""
        # Configurer le mock pour retourner l'existence de l'asset
        mock_result = MagicMock()
        mock_result.first.return_value = (123,)
        mock_session.execute.return_value = mock_result

        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        # Mocker la tâche de streaming en arrière-plan pour éviter de lancer le scraper réel
        with patch.object(worker, "_stream_ticker", return_value=None):
            success = await worker.subscribe("AIR.PA")
            assert success is True

            # Vérifier l'insertion de l'abonnement
            assert (
                mock_session.execute.call_count == 2
            )  # 1. select Asset.id, 2. upsert RealtimeSubscription
            mock_session.commit.assert_called_once()

            # Vérifier l'ajout au set Redis
            active = await worker.get_active_tickers()
            assert "AIR.PA" in active
            assert "AIR.PA" in worker._tasks

    @pytest.mark.asyncio
    async def test_process_tick_stores_redis(self, fake_redis, mock_session, session_factory):
        """Vérifie qu'un tick valide est sauvegardé dans le cache Redis."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        raw_tick = {
            "close": 150.5,
            "open": 149.0,
            "high": 151.0,
            "low": 148.5,
            "volume": 5000,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        }

        # Traiter le tick avec persistance DB activée (asset_id défini)
        await worker._process_tick("AAPL", raw_tick, asset_id=42)

        # Vérifier dans le cache Redis
        cached = await worker.get_quote_from_cache("AAPL")
        assert cached is not None
        assert cached["ticker"] == "AAPL"
        assert float(cached["close"]) == 150.5
        assert float(cached["open"]) == 149.0
        assert cached["volume"] == 5000
        assert cached["source"] == "tradingview"

        # Doit déclencher upsert intraday et mise à jour de la subscription
        assert mock_session.execute.call_count == 2
        assert mock_session.commit.call_count == 2

    @pytest.mark.asyncio
    async def test_process_tick_publishes_pubsub(self, fake_redis, mock_session, session_factory):
        """Vérifie qu'un nouveau tick est publié sur le canal Redis Pub/Sub."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        pubsub = fake_redis.pubsub()
        await pubsub.subscribe("price:MSFT")

        raw_tick = {
            "close": 320.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        }

        await worker._process_tick("MSFT", raw_tick, asset_id=None)

        # Consommer la confirmation de subscription
        sub_msg = await pubsub.get_message(timeout=1.0)
        assert sub_msg["type"] == "subscribe"

        # Consommer le tick publié
        tick_msg = await pubsub.get_message(timeout=1.0)
        assert tick_msg is not None
        assert tick_msg["type"] == "message"

        data = json.loads(tick_msg["data"])
        assert data["ticker"] == "MSFT"
        assert float(data["close"]) == 320.0

    @pytest.mark.asyncio
    async def test_process_tick_invalid_price_rejected(
        self, fake_redis, mock_session, session_factory
    ):
        """Vérifie les rejets de ticks incorrects."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        # Prix négatif ou nul
        await worker._process_tick(
            "AAPL",
            {"close": 0.0, "timestamp": int(datetime.now(timezone.utc).timestamp())},
            asset_id=None,
        )
        assert await worker.get_quote_from_cache("AAPL") is None

        # Timestamp dans le futur (>1min)
        future_ts = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
        await worker._process_tick("AAPL", {"close": 150.0, "timestamp": future_ts}, asset_id=None)
        assert await worker.get_quote_from_cache("AAPL") is None

        # Timestamp trop vieux (>5min)
        old_ts = int((datetime.now(timezone.utc) - timedelta(minutes=6)).timestamp())
        await worker._process_tick("AAPL", {"close": 150.0, "timestamp": old_ts}, asset_id=None)
        assert await worker.get_quote_from_cache("AAPL") is None

    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self, fake_redis, mock_session, session_factory):
        """Vérifie la reconnexion avec backoff exponentiel si le stream échoue."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        call_count = 0

        async def mock_run_streamer(ticker, tv_exchange, tv_symbol, asset_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network failure")
            else:
                worker._running = False  # Arrête la boucle infinie de stream_ticker

        with patch.object(worker, "_run_streamer", side_effect=mock_run_streamer):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await worker._stream_ticker("AAPL", "NASDAQ", "AAPL", asset_id=None)

                assert call_count == 2
                # Vérifier que le sommeil correspond au backoff initial (5s par défaut)
                mock_sleep.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_unsubscribe_cancels_task(self, fake_redis, mock_session, session_factory):
        """Vérifie la désinscription propre d'un ticker."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        mock_task = AwaitableMockTask()
        worker._tasks["AAPL"] = mock_task
        await fake_redis.sadd("realtime:subscriptions", "AAPL")
        await fake_redis.set("quote:AAPL", "dummy")

        success = await worker.unsubscribe("AAPL")
        assert success is True
        assert "AAPL" not in worker._tasks
        assert mock_task.cancelled is True

        # Nettoyage DB et Redis
        assert mock_session.execute.call_count == 1
        assert mock_session.commit.call_count == 1
        assert await fake_redis.get("quote:AAPL") is None
        assert "AAPL" not in (await worker.get_active_tickers())

    @pytest.mark.asyncio
    async def test_restore_subscriptions_on_start(self, fake_redis, mock_session, session_factory):
        """Vérifie que start() restaure les abonnements marqués actifs dans la base."""
        sub1 = RealtimeSubscription(
            id=1, asset_id=10, ticker="AAPL", tv_exchange="NASDAQ", tv_symbol="AAPL", is_active=True
        )
        sub2 = RealtimeSubscription(
            id=2, asset_id=20, ticker="MSFT", tv_exchange="NASDAQ", tv_symbol="MSFT", is_active=True
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [sub1, sub2]
        mock_session.execute.return_value = mock_result

        worker = RealtimePriceWorker(fake_redis, session_factory)

        with patch.object(worker, "subscribe", new_callable=AsyncMock) as mock_subscribe:
            await worker.start()

            assert worker._running is True
            assert mock_subscribe.call_count == 2
            mock_subscribe.assert_any_call("AAPL", persist=False)
            mock_subscribe.assert_any_call("MSFT", persist=False)


# ── TESTS DES ENDPOINTS WS & REST ─────────────────────────────────────────────


class TestWebSocketEndpoint:
    def test_connect_receives_snapshot(self, test_app_client, fake_redis):
        """Vérifie que la connexion WebSocket envoie immédiatement le snapshot actuel."""
        client, worker = test_app_client

        # Pré-charger une quote dans Redis
        now_str = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "ticker": "AAPL",
            "close": 150.5,
            "timestamp": now_str,
            "source": "tradingview",
        }
        asyncio.run(fake_redis.set("quote:AAPL", json.dumps(snapshot)))

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect("/ws/realtime/AAPL") as ws:
                # Recevoir snapshot
                resp = ws.receive_json()
                assert resp["type"] == "snapshot"
                assert resp["ticker"] == "AAPL"
                assert resp["data"]["close"] == 150.5

    def test_tick_broadcast_to_subscribers(self, test_app_client, fake_redis):
        """Vérifie qu'un nouveau message Pub/Sub est poussé en temps réel aux clients WS."""
        client, worker = test_app_client

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect("/ws/realtime/AAPL") as ws:
                # Vider le snapshot initial s'il y en a un (ici aucun n'est dans Redis)

                # Simuler la réception d'un nouveau tick par Redis Pub/Sub
                new_tick = {
                    "ticker": "AAPL",
                    "close": 152.3,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                asyncio.run(fake_redis.publish("price:AAPL", json.dumps(new_tick)))

                # Réceptionner le tick poussé
                resp = ws.receive_json()
                assert resp["type"] == "tick"
                assert resp["ticker"] == "AAPL"
                assert resp["data"]["close"] == 152.3

    def test_websocket_ping_pong_and_unsubscribe(self, test_app_client):
        """Vérifie les messages de contrôle du client WS (ping, unsubscribe)."""
        client, worker = test_app_client

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect("/ws/realtime/AAPL") as ws:
                # Envoyer un ping
                ws.send_text("ping")
                resp = ws.receive_json()
                assert resp["type"] == "pong"

                # Envoyer unsubscribe (doit clore la connexion)
                ws.send_text("unsubscribe")
                # Doit se clore proprement sans exception
                try:
                    ws.receive_json()
                    pytest.fail("La connexion WS aurait dû être fermée.")
                except Exception:
                    pass


class TestQuoteEndpoint:
    def test_quote_from_redis_cache(self, test_app_client, fake_redis):
        """Vérifie que GET /quote/{ticker} renvoie le cache Redis s'il est frais."""
        client, worker = test_app_client

        now_str = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "ticker": "AAPL",
            "close": 180.25,
            "timestamp": now_str,
            "source": "tradingview",
        }
        asyncio.run(fake_redis.set("quote:AAPL", json.dumps(snapshot)))

        response = client.get("/quote/AAPL")
        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "AAPL"
        assert float(data["price"]) == 180.25
        assert data["is_realtime"] is True
        assert data["source"] == "tradingview"

    def test_quote_fallback_yfinance(self, test_app_client):
        """Vérifie que GET /quote/{ticker} bascule sur yfinance en l'absence de cache."""
        client, worker = test_app_client

        # Mocker yfinance
        class FakeFastInfo:
            last_price = 145.2
            previous_close = 144.0

        mock_ticker = MagicMock()
        mock_ticker.fast_info = FakeFastInfo()

        with patch("yfinance.Ticker", return_value=mock_ticker):
            # subscribe_if_missing=False pour ne pas lancer de tâche asynchrone
            response = client.get("/quote/AAPL?subscribe_if_missing=false")
            assert response.status_code == 200
            data = response.json()
            assert data["ticker"] == "AAPL"
            assert float(data["price"]) == 145.2
            assert data["is_realtime"] is False
            assert data["source"] == "yfinance"

    def test_quote_triggers_subscription(self, test_app_client):
        """Vérifie qu'appeler GET /quote/ déclenche un abonnement asynchrone si le ticker est absent."""
        client, worker = test_app_client

        class FakeFastInfo:
            last_price = 100.0
            previous_close = 100.0

        mock_ticker = MagicMock()
        mock_ticker.fast_info = FakeFastInfo()

        with patch("yfinance.Ticker", return_value=mock_ticker):
            with patch.object(worker, "subscribe", new_callable=AsyncMock) as mock_sub:
                response = client.get("/quote/TSLA?subscribe_if_missing=true")
                assert response.status_code == 200

                # Attendre un court instant pour laisser l'event loop exécuter la task
                asyncio.run(asyncio.sleep(0.01))
                mock_sub.assert_called_once_with("TSLA")


class TestManualSubscriptionEndpoints:
    def test_subscribe_and_unsubscribe_endpoints(self, test_app_client, mock_session):
        """Vérifie les endpoints d'inscription et de désinscription manuelles."""
        client, worker = test_app_client

        # Setup mocks
        mock_result = MagicMock()
        mock_result.first.return_value = (99,)
        mock_session.execute.return_value = mock_result

        with patch.object(worker, "_stream_ticker", return_value=None):
            # Test POST /realtime/subscribe
            response = client.post("/realtime/subscribe", json={"tickers": ["AAPL", "MSFT"]})
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert data[0]["ticker"] == "AAPL"
            assert data[0]["is_active"] is True
            assert data[1]["ticker"] == "MSFT"

            # Test DELETE /realtime/subscribe/AAPL
            # Pré-charger AAPL dans les tâches locales pour faire comme s'il était actif
            worker._tasks["AAPL"] = AwaitableMockTask()
            response = client.delete("/realtime/subscribe/AAPL")
            assert response.status_code == 200
            assert response.json()["status"] == "unsubscribed"

    def test_status_endpoint(self, test_app_client, fake_redis):
        """Vérifie le fonctionnement de l'endpoint d'état global."""
        client, worker = test_app_client

        # Mocker les abonnements actifs sur le worker
        worker._tasks["AAPL"] = AsyncMock()
        asyncio.run(fake_redis.sadd("realtime:subscriptions", "AAPL"))

        response = client.get("/realtime/status")
        assert response.status_code == 200
        data = response.json()
        assert data["streaming_count"] == 1
        assert "AAPL" in data["active_tickers"]
        assert data["worker_running"] is True

    def test_quotes_batch_endpoint(self, test_app_client, fake_redis):
        """Vérifie l'endpoint de lecture par lot GET /quotes."""
        client, worker = test_app_client

        # Pré-charger AAPL dans Redis
        now_str = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "ticker": "AAPL",
            "close": 150.5,
            "timestamp": now_str,
        }
        asyncio.run(fake_redis.set("quote:AAPL", json.dumps(snapshot)))

        # Mocker yfinance pour MSFT
        class FakeFastInfo:
            last_price = 320.0
            previous_close = 319.0

        mock_ticker = MagicMock()
        mock_ticker.fast_info = FakeFastInfo()

        with patch("yfinance.Ticker", return_value=mock_ticker):
            response = client.get("/quotes?tickers=AAPL,MSFT")
            assert response.status_code == 200
            data = response.json()
            assert data["count"] == 2
            assert "AAPL" in data["quotes"]
            assert "MSFT" in data["quotes"]

            # AAPL provient de Redis
            assert data["quotes"]["AAPL"]["is_realtime"] is True
            assert float(data["quotes"]["AAPL"]["data"]["close"]) == 150.5

            # MSFT provient de yfinance
            assert data["quotes"]["MSFT"]["is_realtime"] is False
            assert float(data["quotes"]["MSFT"]["data"]["close"]) == 320.0
