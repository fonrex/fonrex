#!/usr/bin/env python3
"""
Exemple de client pour l'API Temps Réel Fonrex (FonRex Pro).

Ce script montre comment :
1. Se connecter et écouter le flux WebSocket temps réel pour un ticker particulier.
2. Effectuer des requêtes REST pour obtenir des snapshots unitaires (/quote) et en lot (/quotes).
3. Configurer l'auto-abonnement en tâche de fond.

Dépendances requises :
    pip install httpx websockets

Lancement :
    python scripts/example_realtime_client.py
"""

import asyncio
import json
import logging
import sys

import httpx
import websockets

# Configuration de base des logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("realtime_client")

BASE_URL_REST = "http://localhost:5000"
BASE_URL_WS = "ws://localhost:5000"


async def listen_websocket(ticker: str):
    """
    Se connecte à l'endpoint WebSocket temps réel pour un ticker donné et écoute le flux.
    """
    ws_uri = f"{BASE_URL_WS}/ws/realtime/{ticker}"
    logger.info(f"Connexion WebSocket à {ws_uri}...")

    try:
        async with websockets.connect(ws_uri) as websocket:
            logger.info(f"✅ Connecté au flux WebSocket pour {ticker} !")
            logger.info("Le serveur envoie d'abord un snapshot, puis des ticks temps réel...")

            # Démarre une tâche parallèle pour envoyer des pings périodiques (keep-alive)
            async def send_pings():
                while True:
                    await asyncio.sleep(30)
                    try:
                        logger.debug("Envoi ping keep-alive...")
                        await websocket.send("ping")
                    except websockets.exceptions.ConnectionClosed:
                        break

            ping_task = asyncio.create_task(send_pings())

            try:
                # Boucle de réception des messages
                async for message in websocket:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    ticker_name = data.get("ticker")
                    ts = data.get("ts")
                    payload = data.get("data", {})

                    if msg_type == "snapshot":
                        logger.info(
                            f"📸 [SNAPSHOT] {ticker_name} | Dernier prix: {payload.get('close')} | Vol: {payload.get('volume')} (source: {payload.get('source')} à {ts})"
                        )
                    elif msg_type == "tick":
                        logger.info(
                            f"⚡ [TICK] {ticker_name} | Nouveau prix: {payload.get('close')} | Vol: {payload.get('volume')} | Open: {payload.get('open')} (reçu à {ts})"
                        )
                    elif msg_type == "pong":
                        logger.debug("Reçu pong du serveur.")
                    else:
                        logger.info(f"📩 [MESSAGE] Type: {msg_type} | Data: {data}")

            finally:
                ping_task.cancel()

    except ConnectionRefusedError:
        logger.error(f"❌ Connexion refusée. Assurez-vous que l'API est lancée sur {BASE_URL_REST}")
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"⚠️ Connexion WebSocket fermée par le serveur : {e}")
    except Exception as e:
        logger.error(f"💥 Erreur WebSocket: {e}", exc_info=True)


async def get_rest_snapshot(ticker: str) -> dict:
    """
    Récupère le dernier snapshot de prix pour un ticker via l'endpoint REST.
    """
    url = f"{BASE_URL_REST}/quote/{ticker}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params={"subscribe_if_missing": "true"})
            if response.status_code == 200:
                data = response.json()
                logger.info(
                    f"🟢 [REST Quote] {ticker} -> Prix: {data.get('price')} | "
                    f"Variation: {data.get('change')} ({data.get('change_pct')}%) | "
                    f"Temps réel: {data.get('is_realtime')} | Source: {data.get('source')}"
                )
                return data
            else:
                logger.warning(
                    f"🔴 Impossible de récupérer le snapshot pour {ticker} : {response.status_code} - {response.text}"
                )
        except Exception as e:
            logger.error(f"Erreur requête REST quote: {e}")
    return {}


async def get_rest_quotes_batch(tickers: list[str]) -> dict:
    """
    Récupère les snapshots pour plusieurs tickers en un seul appel batch.
    """
    tickers_str = ",".join(tickers)
    url = f"{BASE_URL_REST}/quotes"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params={"tickers": tickers_str})
            if response.status_code == 200:
                data = response.json()
                logger.info(f"🟣 [REST Batch] Reçu {data.get('count')} quotes :")
                for ticker, quote in data.get("quotes", {}).items():
                    if quote:
                        q_data = quote.get("data", {})
                        logger.info(
                            f"   - {ticker} : Close={q_data.get('close')} | "
                            f"Temps Réel={quote.get('is_realtime')} | Source={quote.get('source')}"
                        )
                    else:
                        logger.warning(f"   - {ticker} : Non trouvé ou non disponible")
                return data
            else:
                logger.warning(f"🔴 Échec de la requête REST batch : {response.status_code}")
        except Exception as e:
            logger.error(f"Erreur requête REST batch: {e}")
    return {}


async def main():
    if len(sys.argv) > 1:
        ticker = sys.argv[1].upper()
    else:
        ticker = "AAPL"

    logger.info("=== DÉBUT DES EXEMPLES REST ===")

    # 1. Snapshot unitaire (s'abonne automatiquement en arrière-plan si manquant)
    await get_rest_snapshot(ticker)

    # 2. Snapshot de repli / autre ticker européen
    await get_rest_snapshot("AIR.PA")

    # 3. Snapshot en lot (batch)
    await get_rest_quotes_batch([ticker, "AIR.PA", "BNP.PA"])

    logger.info("=== DÉBUT DE L'EXEMPLE WEBSOCKET ===")
    logger.info(f"Démarrage de l'écoute WebSocket pour {ticker}. Ctrl+C pour quitter.")

    await listen_websocket(ticker)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nArrêt du client demandé par l'utilisateur.")
