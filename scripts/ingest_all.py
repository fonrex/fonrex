#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script CLI pour lancer l'ingestion EOD historique de masse pour tous les actifs actifs.
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import redis.asyncio as redis

# Ajouter le chemin parent pour pouvoir importer les modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.query import QueryService
from database.service import DatabaseService
from historical.ingestion_service import HistoricalIngestionService

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def run_bulk_ingest(resolution: str, source: str, force_refresh: bool, concurrency: int):
    start_time = time.time()

    # 1. Initialisation des services
    db_service = DatabaseService()
    query_service = QueryService()

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_client = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    ingestion_service = HistoricalIngestionService(
        db_service=db_service, query_service=query_service, redis_client=redis_client
    )

    try:
        # 2. Récupérer les tickers actifs
        logger.info("🔍 Récupération des tickers actifs depuis la base de données...")
        tickers = await query_service.get_all_tickers_for_bulk_ingest()
        logger.info(f"📋 {len(tickers)} tickers actifs trouvés.")

        if not tickers:
            logger.warning("⚠️ Aucun ticker actif trouvé pour l'ingestion.")
            return

        # 3. Lancer l'ingestion bulk
        logger.info(
            f"🔄 Lancement de l'ingestion historique pour {len(tickers)} tickers ({resolution}) via {source}..."
        )
        results = await ingestion_service.ingest_bulk(
            tickers=tickers,
            resolution=resolution,
            source=source,
            force_refresh=force_refresh,
            concurrency=concurrency,
        )

        # 4. Analyser les résultats
        success_count = sum(1 for r in results if r.status == "success")
        up_to_date_count = sum(1 for r in results if r.status == "up_to_date")
        failed_count = sum(1 for r in results if r.status == "failed")

        duration = time.time() - start_time
        logger.info("=========================================")
        logger.info("📊 RAPPORT D'INGESTION DE MASSE")
        logger.info(f"⏱️ Durée totale : {duration:.2f} secondes")
        logger.info(f"✅ Succès : {success_count}")
        logger.info(f"🔄 Déjà à jour : {up_to_date_count}")
        logger.info(f"❌ Échecs : {failed_count}")
        logger.info("=========================================")

        if failed_count > 0:
            logger.warning("⚠️ Certains tickers ont échoué à l'ingestion :")
            for r in results:
                if r.status == "failed":
                    logger.warning(f"  - {r.ticker}: {r.error}")

    finally:
        # 5. Nettoyage des connexions
        await query_service.close()
        await redis_client.close()


def main():
    parser = argparse.ArgumentParser(description="Ingestion EOD historique en masse.")
    parser.add_argument(
        "--resolution",
        type=str,
        default="1D",
        choices=["1D", "1W", "1M"],
        help="Résolution temporelle à ingérer (default: 1D)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="auto",
        choices=["auto", "yfinance", "tradingview"],
        help="Source de données à utiliser (default: auto)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Forcer le rafraîchissement complet de l'historique (ignore gap detection)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Nombre maximal de requêtes parallèles (default: 5)",
    )

    args = parser.parse_args()

    # Exécuter la boucle event loop
    asyncio.run(
        run_bulk_ingest(
            resolution=args.resolution,
            source=args.source,
            force_refresh=args.force,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
