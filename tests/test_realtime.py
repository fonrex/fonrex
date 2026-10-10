import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Make sure the root folder is on the PYTHONPATH
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
    """An isolated asynchronous FakeRedis instance (synchronous for pytest)."""
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
    """Mock of an asynchronous SQLAlchemy session."""

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
    """Factory returning our session mock."""

    def _factory():
        return mock_session

    return _factory


@pytest.fixture
def test_app_client(fake_redis, session_factory):
    """Set up the FastAPI test application with the mocks and return a test client."""
    old_state_worker = getattr(app.state, "realtime_worker", None)
    old_state_redis = getattr(app.state, "redis_client", None)
    old_state_ws_manager = getattr(app.state, "ws_manager", None)

    # Create the manager again to isolate the WS connections
    ws_manager = main.ConnectionManager()

    # Create the test worker
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


# Helper simulating an awaitable and cancellable asyncio Task
class AwaitableMockTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def __await__(self):
        async def _dummy():
            pass

        return _dummy().__await__()


# ── WORKER TESTS ──────────────────────────────────────────────────────────────


class TestRealtimePriceWorker:
    def test_resolve_tv_symbol(self):
        """Yahoo symbols are turned into TradingView symbols."""
        worker = RealtimePriceWorker(None, None)
        assert worker._resolve_tv_symbol("AIR.PA") == ("EURONEXT", "AIR")
        assert worker._resolve_tv_symbol("BMW.DE") == ("XETRA", "BMW")
        assert worker._resolve_tv_symbol("AAPL") == ("NASDAQ", "AAPL")
        assert worker._resolve_tv_symbol("aapl") == ("NASDAQ", "AAPL")

    @pytest.mark.asyncio
    async def test_subscribe_valid_ticker(self, fake_redis, mock_session, session_factory):
        """Nominal subscription of a ticker."""
        # The mock says the asset exists
        mock_result = MagicMock()
        mock_result.first.return_value = (123,)
        mock_session.execute.return_value = mock_result

        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        # Mock the background streaming task, so as not to start the real scraper
        with patch.object(worker, "_stream_ticker", return_value=None):
            success = await worker.subscribe("AIR.PA")
            assert success is True

            # The subscription was inserted
            assert (
                mock_session.execute.call_count == 2
            )  # 1. select Asset.id, 2. upsert RealtimeSubscription
            mock_session.commit.assert_called_once()

            # The ticker was added to the Redis set
            active = await worker.get_active_tickers()
            assert "AIR.PA" in active
            assert "AIR.PA" in worker._tasks

    @pytest.mark.asyncio
    async def test_process_tick_stores_redis(self, fake_redis, mock_session, session_factory):
        """A valid tick is stored in the Redis cache."""
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

        # Process the tick with database storage on (asset_id set)
        await worker._process_tick("AAPL", raw_tick, asset_id=42)

        # Check the Redis cache
        cached = await worker.get_quote_from_cache("AAPL")
        assert cached is not None
        assert cached["ticker"] == "AAPL"
        assert float(cached["close"]) == 150.5
        assert float(cached["open"]) == 149.0
        assert cached["volume"] == 5000
        assert cached["source"] == "tradingview"

        # Must upsert the intraday bar and update the subscription
        assert mock_session.execute.call_count == 2
        assert mock_session.commit.call_count == 2

    @pytest.mark.asyncio
    async def test_process_tick_publishes_pubsub(self, fake_redis, mock_session, session_factory):
        """A new tick is published on the Redis Pub/Sub channel."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        pubsub = fake_redis.pubsub()
        await pubsub.subscribe("price:MSFT")

        raw_tick = {
            "close": 320.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        }

        await worker._process_tick("MSFT", raw_tick, asset_id=None)

        # Read the subscription confirmation
        sub_msg = await pubsub.get_message(timeout=1.0)
        assert sub_msg["type"] == "subscribe"

        # Read the published tick
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
        """Wrong ticks are rejected."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        # Negative or zero price
        await worker._process_tick(
            "AAPL",
            {"close": 0.0, "timestamp": int(datetime.now(timezone.utc).timestamp())},
            asset_id=None,
        )
        assert await worker.get_quote_from_cache("AAPL") is None

        # Timestamp in the future (>1min)
        future_ts = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
        await worker._process_tick("AAPL", {"close": 150.0, "timestamp": future_ts}, asset_id=None)
        assert await worker.get_quote_from_cache("AAPL") is None

        # Timestamp trop vieux (>5min)
        old_ts = int((datetime.now(timezone.utc) - timedelta(minutes=6)).timestamp())
        await worker._process_tick("AAPL", {"close": 150.0, "timestamp": old_ts}, asset_id=None)
        assert await worker.get_quote_from_cache("AAPL") is None

    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self, fake_redis, mock_session, session_factory):
        """Reconnection with exponential backoff when the stream fails."""
        worker = RealtimePriceWorker(fake_redis, session_factory)
        worker._running = True

        call_count = 0

        async def mock_run_streamer(ticker, tv_exchange, tv_symbol, asset_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network failure")
            else:
                worker._running = False  # Stops the endless loop of stream_ticker

        with patch.object(worker, "_run_streamer", side_effect=mock_run_streamer):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await worker._stream_ticker("AAPL", "NASDAQ", "AAPL", asset_id=None)

                assert call_count == 2
                # The sleep is the initial backoff (5s by default)
                mock_sleep.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_unsubscribe_cancels_task(self, fake_redis, mock_session, session_factory):
        """Clean unsubscription of a ticker."""
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
        """start() restores the subscriptions marked active in the database."""
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


# ── WS & REST ROUTE TESTS ─────────────────────────────────────────────────────


class TestWebSocketEndpoint:
    def test_connect_receives_snapshot(self, test_app_client, fake_redis):
        """The WebSocket connection sends the current snapshot at once."""
        client, worker = test_app_client

        # Preload a quote into Redis
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
        """A new Pub/Sub message is pushed to the WS clients as it arrives."""
        client, worker = test_app_client

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect("/ws/realtime/AAPL") as ws:
                # Drain the initial snapshot, if any (none is in Redis here)

                # Simulate a new tick received through Redis Pub/Sub
                new_tick = {
                    "ticker": "AAPL",
                    "close": 152.3,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                asyncio.run(fake_redis.publish("price:AAPL", json.dumps(new_tick)))

                # Receive the pushed tick
                resp = ws.receive_json()
                assert resp["type"] == "tick"
                assert resp["ticker"] == "AAPL"
                assert resp["data"]["close"] == 152.3

    def test_each_client_receives_a_tick_once(self, test_app_client, fake_redis):
        """Two clients on one ticker: one tick published, one message each.

        Every connection listened to the channel and broadcast to the whole
        group, so each client received a tick as many times as there were clients.
        """
        client, worker = test_app_client

        def tick(close):
            return json.dumps({"ticker": "AAPL", "close": close})

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with (
                client.websocket_connect("/ws/realtime/AAPL") as first,
                client.websocket_connect("/ws/realtime/AAPL") as second,
            ):
                asyncio.run(fake_redis.publish("price:AAPL", tick(152.3)))
                assert first.receive_json()["data"]["close"] == 152.3
                assert second.receive_json()["data"]["close"] == 152.3

                # The next message of each client is the next tick, not a copy
                # of the first one.
                asyncio.run(fake_redis.publish("price:AAPL", tick(152.9)))
                assert first.receive_json()["data"]["close"] == 152.9
                assert second.receive_json()["data"]["close"] == 152.9

    def test_websocket_ping_pong_and_unsubscribe(self, test_app_client):
        """Control messages of the WS client (ping, unsubscribe)."""
        client, worker = test_app_client

        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect("/ws/realtime/AAPL") as ws:
                # Send a ping
                ws.send_text("ping")
                resp = ws.receive_json()
                assert resp["type"] == "pong"

                # Send unsubscribe (must close the connection)
                ws.send_text("unsubscribe")
                # Must close cleanly without an exception
                try:
                    ws.receive_json()
                    pytest.fail("The WS connection should have been closed.")
                except Exception:
                    pass

    def test_websocket_requires_key_by_default(self, test_app_client, monkeypatch):
        """Without any auth configuration, the WS connection is refused (secure by default)."""
        from starlette.websockets import WebSocketDisconnect

        client, _worker = test_app_client
        for name in ("FONREX_API_KEY", "FONREX_RELAY_KEY", "FONREX_API_KEYS", "FONREX_AUTH_REQUIRED"):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/realtime/AAPL"):
                pass
        assert exc.value.code == 1008

        # A well-formed key that is not configured is not a valid credential.
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/realtime/AAPL",
                headers={"X-API-KEY": "frx_live_well_formed_but_unknown"},
            ):
                pass
        assert exc.value.code == 1008

    def test_websocket_auth_enforcement_when_configured(self, test_app_client, monkeypatch):
        """The WS connection needs a valid API key when FONREX_API_KEY is set."""
        from starlette.websockets import WebSocketDisconnect

        client, worker = test_app_client
        monkeypatch.setenv("FONREX_API_KEY", "frx_live_ws_secret_999")

        # 1. Not authenticated -> rejected at once with code 1008
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/realtime/AAPL"):
                pass
        assert exc.value.code == 1008

        # 2. Invalid key -> code 1008
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/realtime/AAPL",
                headers={"X-API-KEY": "frx_live_bad_key"},
            ):
                pass
        assert exc.value.code == 1008

        # 3. Valid key through Authorization: Bearer
        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect(
                "/ws/realtime/AAPL",
                headers={"Authorization": "Bearer frx_live_ws_secret_999"},
            ) as ws:
                ws.send_text("ping")
                assert ws.receive_json() == {"type": "pong"}

        # 4. Valid key through the token query parameter
        with patch.object(worker, "subscribe", new_callable=AsyncMock):
            with client.websocket_connect(
                "/ws/realtime/AAPL?token=frx_live_ws_secret_999",
            ) as ws:
                ws.send_text("ping")
                assert ws.receive_json() == {"type": "pong"}


class TestQuoteEndpoint:
    def test_quote_from_redis_cache(self, test_app_client, fake_redis):
        """GET /quote/{ticker} returns the Redis cache when it is fresh."""
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
        """GET /quote/{ticker} falls back on yfinance without cache."""
        client, worker = test_app_client

        # Mocker yfinance
        class FakeFastInfo:
            last_price = 145.2
            previous_close = 144.0

        mock_ticker = MagicMock()
        mock_ticker.fast_info = FakeFastInfo()

        with patch("yfinance.Ticker", return_value=mock_ticker):
            # subscribe_if_missing=False so that no asynchronous task starts
            response = client.get("/quote/AAPL?subscribe_if_missing=false")
            assert response.status_code == 200
            data = response.json()
            assert data["ticker"] == "AAPL"
            assert float(data["price"]) == 145.2
            assert data["is_realtime"] is False
            assert data["source"] == "yfinance"

    def test_quote_triggers_subscription(self, test_app_client):
        """GET /quote/ starts an asynchronous subscription when the ticker is missing."""
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

                # Wait a moment so that the event loop runs the task
                asyncio.run(asyncio.sleep(0.01))
                mock_sub.assert_called_once_with("TSLA")


class TestManualSubscriptionEndpoints:
    def test_subscribe_and_unsubscribe_endpoints(self, test_app_client, mock_session):
        """Manual subscription and unsubscription routes."""
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
            # Preload AAPL into the local tasks, as if it were active
            worker._tasks["AAPL"] = AwaitableMockTask()
            response = client.delete("/realtime/subscribe/AAPL")
            assert response.status_code == 200
            assert response.json()["status"] == "unsubscribed"

    def test_status_endpoint(self, test_app_client, fake_redis):
        """The overall status route."""
        client, worker = test_app_client

        # Mock the active subscriptions of the worker
        worker._tasks["AAPL"] = AsyncMock()
        asyncio.run(fake_redis.sadd("realtime:subscriptions", "AAPL"))

        response = client.get("/realtime/status")
        assert response.status_code == 200
        data = response.json()
        assert data["streaming_count"] == 1
        assert "AAPL" in data["active_tickers"]
        assert data["worker_running"] is True

    def test_quotes_batch_endpoint(self, test_app_client, fake_redis):
        """The batch route GET /quotes."""
        client, worker = test_app_client

        # Preload AAPL into Redis
        now_str = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "ticker": "AAPL",
            "close": 150.5,
            "timestamp": now_str,
        }
        asyncio.run(fake_redis.set("quote:AAPL", json.dumps(snapshot)))

        # Mock yfinance for MSFT
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

            # AAPL comes from Redis
            assert data["quotes"]["AAPL"]["is_realtime"] is True
            assert float(data["quotes"]["AAPL"]["data"]["close"]) == 150.5

            # MSFT comes from yfinance
            assert data["quotes"]["MSFT"]["is_realtime"] is False
            assert float(data["quotes"]["MSFT"]["data"]["close"]) == 320.0
