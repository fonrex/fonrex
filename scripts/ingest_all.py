#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Command-line script running the bulk EOD history ingestion of every active asset.
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import redis.asyncio as redis

# Add the parent directory so that the modules can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.query import QueryService
from database.service import DatabaseService
from historical.ingestion_service import HistoricalIngestionService

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def run_bulk_ingest(resolution: str, source: str, force_refresh: bool, concurrency: int):
    start_time = time.time()

    # 1. Services
    db_service = DatabaseService()
    query_service = QueryService()

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis_client = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    ingestion_service = HistoricalIngestionService(
        db_service=db_service, query_service=query_service, redis_client=redis_client
    )

    try:
        # 2. Active tickers
        logger.info("🔍 Reading the active tickers from the database...")
        tickers = await query_service.get_all_tickers_for_bulk_ingest()
        logger.info(f"📋 {len(tickers)} active tickers found.")

        if not tickers:
            logger.warning("⚠️ No active ticker to ingest.")
            return

        # 3. Bulk ingestion
        logger.info(
            f"🔄 Ingesting the history of {len(tickers)} tickers ({resolution}) from {source}..."
        )
        results = await ingestion_service.ingest_bulk(
            tickers=tickers,
            resolution=resolution,
            source=source,
            force_refresh=force_refresh,
            concurrency=concurrency,
        )

        # 4. Results
        success_count = sum(1 for r in results if r.status == "success")
        up_to_date_count = sum(1 for r in results if r.status == "up_to_date")
        failed_count = sum(1 for r in results if r.status == "failed")

        duration = time.time() - start_time
        logger.info("=========================================")
        logger.info("📊 BULK INGESTION REPORT")
        logger.info(f"⏱️ Total duration: {duration:.2f} seconds")
        logger.info(f"✅ Succeeded: {success_count}")
        logger.info(f"🔄 Already up to date: {up_to_date_count}")
        logger.info(f"❌ Failed: {failed_count}")
        logger.info("=========================================")

        if failed_count > 0:
            logger.warning("⚠️ Some tickers failed:")
            for r in results:
                if r.status == "failed":
                    logger.warning(f"  - {r.ticker}: {r.error}")

    finally:
        # 5. Close the connections
        await query_service.close()
        await redis_client.close()


def main():
    parser = argparse.ArgumentParser(description="Bulk EOD history ingestion.")
    parser.add_argument(
        "--resolution",
        type=str,
        default="1D",
        choices=["1D", "1W", "1M"],
        help="Resolution to ingest (default: 1D)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="auto",
        choices=["auto", "yfinance", "tradingview"],
        help="Data source (default: auto)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fetch the whole history again (ignores gap detection)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Maximum number of parallel requests (default: 5)",
    )

    args = parser.parse_args()

    # Run the event loop
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
