#!/usr/bin/env python3
"""
Example client of the Fonrex realtime API.

This script shows how to:
1. Connect to the realtime WebSocket stream of a ticker and listen to it.
2. Call the REST routes for one snapshot (/quote) and a batch (/quotes).
3. Subscribe a ticker in the background.

Requirements:
    pip install httpx websockets

Run:
    FONREX_API_KEY=frx_live_... python scripts/example_realtime_client.py
"""

import asyncio
import json
import logging
import os
import sys
from urllib.parse import urlencode

import httpx
import websockets

# Basic logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("realtime_client")

BASE_URL_REST = "http://localhost:5000"
BASE_URL_WS = "ws://localhost:5000"

# The API requires a key by default: export FONREX_API_KEY before running this script.
API_KEY = os.environ.get("FONREX_API_KEY", "")
AUTH_HEADERS = {"X-API-KEY": API_KEY} if API_KEY else {}


async def listen_websocket(ticker: str):
    """
    Connect to the realtime WebSocket route of a ticker and listen to the stream.
    """
    ws_uri = f"{BASE_URL_WS}/ws/realtime/{ticker}"
    logger.info(f"Connecting to the WebSocket {ws_uri}...")

    try:
        # WebSocket clients cannot always set headers: the key travels as a query parameter.
        connect_uri = f"{ws_uri}?{urlencode({'token': API_KEY})}" if API_KEY else ws_uri
        async with websockets.connect(connect_uri) as websocket:
            logger.info(f"✅ Connected to the WebSocket stream of {ticker}!")
            logger.info("The server sends a snapshot first, then realtime ticks...")

            # Background task sending periodic pings (keep-alive)
            async def send_pings():
                while True:
                    await asyncio.sleep(30)
                    try:
                        logger.debug("Sending keep-alive ping...")
                        await websocket.send("ping")
                    except websockets.exceptions.ConnectionClosed:
                        break

            ping_task = asyncio.create_task(send_pings())

            try:
                # Message loop
                async for message in websocket:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    ticker_name = data.get("ticker")
                    ts = data.get("ts")
                    payload = data.get("data", {})

                    if msg_type == "snapshot":
                        logger.info(
                            f"📸 [SNAPSHOT] {ticker_name} | Last price: {payload.get('close')} | Vol: {payload.get('volume')} (source: {payload.get('source')} at {ts})"
                        )
                    elif msg_type == "tick":
                        logger.info(
                            f"⚡ [TICK] {ticker_name} | New price: {payload.get('close')} | Vol: {payload.get('volume')} | Open: {payload.get('open')} (received at {ts})"
                        )
                    elif msg_type == "pong":
                        logger.debug("Pong received from the server.")
                    else:
                        logger.info(f"📩 [MESSAGE] Type: {msg_type} | Data: {data}")

            finally:
                ping_task.cancel()

    except ConnectionRefusedError:
        logger.error(f"❌ Connection refused. Check that the API runs on {BASE_URL_REST}")
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"⚠️ WebSocket connection closed by the server: {e}")
    except Exception as e:
        logger.error(f"💥 WebSocket error: {e}", exc_info=True)


async def get_rest_snapshot(ticker: str) -> dict:
    """
    The last price snapshot of a ticker, from the REST route.
    """
    url = f"{BASE_URL_REST}/quote/{ticker}"
    async with httpx.AsyncClient(headers=AUTH_HEADERS) as client:
        try:
            response = await client.get(url, params={"subscribe_if_missing": "true"})
            if response.status_code == 200:
                data = response.json()
                logger.info(
                    f"🟢 [REST Quote] {ticker} -> Price: {data.get('price')} | "
                    f"Change: {data.get('change')} ({data.get('change_pct')}%) | "
                    f"Realtime: {data.get('is_realtime')} | Source: {data.get('source')}"
                )
                return data
            else:
                logger.warning(
                    f"🔴 Cannot read the snapshot of {ticker}: {response.status_code} - {response.text}"
                )
        except Exception as e:
            logger.error(f"REST quote request error: {e}")
    return {}


async def get_rest_quotes_batch(tickers: list[str]) -> dict:
    """
    The snapshots of several tickers in one batch call.
    """
    tickers_str = ",".join(tickers)
    url = f"{BASE_URL_REST}/quotes"
    async with httpx.AsyncClient(headers=AUTH_HEADERS) as client:
        try:
            response = await client.get(url, params={"tickers": tickers_str})
            if response.status_code == 200:
                data = response.json()
                logger.info(f"🟣 [REST Batch] Received {data.get('count')} quotes:")
                for ticker, quote in data.get("quotes", {}).items():
                    if quote:
                        q_data = quote.get("data", {})
                        logger.info(
                            f"   - {ticker}: Close={q_data.get('close')} | "
                            f"Realtime={quote.get('is_realtime')} | Source={quote.get('source')}"
                        )
                    else:
                        logger.warning(f"   - {ticker}: not found or unavailable")
                return data
            else:
                logger.warning(f"🔴 REST batch request failed: {response.status_code}")
        except Exception as e:
            logger.error(f"REST batch request error: {e}")
    return {}


async def main():
    if len(sys.argv) > 1:
        ticker = sys.argv[1].upper()
    else:
        ticker = "AAPL"

    logger.info("=== REST EXAMPLES ===")

    # 1. One snapshot (subscribes in the background when missing)
    await get_rest_snapshot(ticker)

    # 2. Another snapshot, a European ticker
    await get_rest_snapshot("AIR.PA")

    # 3. Batch of snapshots
    await get_rest_quotes_batch([ticker, "AIR.PA", "BNP.PA"])

    logger.info("=== WEBSOCKET EXAMPLE ===")
    logger.info(f"Listening to the WebSocket of {ticker}. Ctrl+C to quit.")

    await listen_websocket(ticker)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nClient stopped by the user.")
