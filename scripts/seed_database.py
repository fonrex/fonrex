#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Professional database initialization and seeding script for Fonrex.

Imports default assets (or a custom file/directory), creates listings
and provider mappings, then optionally enriches metadata (yfinance)
and downloads initial 1D price history.

Usage:
    python scripts/seed_database.py
    python scripts/seed_database.py --dir data/isin_data/
    python scripts/seed_database.py --enrich --ingest-history
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Add root directory to PATH
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from database.query import QueryService
from database.service import DatabaseService
from import_assets import AssetImporter, CSVRow, enrich_batch, parse_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("seed_database")



def resolve_seed_files(file_arg: str = None, dir_arg: str = None) -> list[Path]:
    """Resolves the list of CSV files to import."""
    if dir_arg:
        dir_path = Path(dir_arg)
        if not dir_path.is_absolute():
            dir_path = ROOT_DIR / dir_arg
        if not dir_path.exists() or not dir_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {dir_path}")
        files = sorted([p for p in dir_path.glob("*.csv") if p.is_file()])
        if not files:
            raise FileNotFoundError(f"No CSV files found in {dir_path}")
        return files

    if file_arg:
        file_path = Path(file_arg)
        if not file_path.is_absolute():
            file_path = ROOT_DIR / file_arg
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return [file_path]

    # Default fallback: data/etf.csv / data/stocks.csv then data/isin_data/
    fallback_files = []
    for name in ["etf.csv", "stocks.csv"]:
        p = ROOT_DIR / "data" / name
        if p.exists():
            fallback_files.append(p)
    if fallback_files:
        logger.info("🌱 Using default seed datasets: %s", [p.name for p in fallback_files])
        return fallback_files

    isin_dir = ROOT_DIR / "data" / "isin_data"
    if isin_dir.exists() and isin_dir.is_dir():
        files = sorted([p for p in isin_dir.glob("*.csv") if p.is_file()])
        if files:
            logger.info("📁 Using local isin_data directory (%d files)", len(files))
            return files

    raise FileNotFoundError("No seed dataset found in data/ (etf.csv, stocks.csv) or data/isin_data/")


async def run_seeding(
    files: list[Path],
    dry_run: bool = False,
    batch_size: int = 200,
    enrich: bool = False,
    ingest_history: bool = False,
):
    start_time = time.time()
    logger.info("🚀 Starting asset catalog initialization...")

    # 1. Database check
    db_service = DatabaseService()
    if not db_service.check_connection():
        logger.error("❌ Unable to connect to database.")
        sys.exit(1)

    if not db_service.check_migrations():
        logger.warning("⚠️ Alembic migrations are pending. Run `alembic upgrade head`.")

    # 2. Parse CSV files
    all_rows: list[CSVRow] = []
    total_dup = 0
    total_inv = 0

    for f_path in files:
        logger.info("📄 Reading %s", f_path.name)
        rows, nb_dup, nb_inv = parse_csv(f_path)
        all_rows.extend(rows)
        total_dup += nb_dup
        total_inv += nb_inv

    logger.info(
        "📊 Total rows parsed: %d (%d valid, %d duplicates, %d invalid)",
        len(all_rows) + total_dup + total_inv,
        len(all_rows),
        total_dup,
        total_inv,
    )

    if not all_rows:
        logger.warning("⚠️ No valid rows to import.")
        return

    # 3. Synchronous import via AssetImporter
    session = db_service.get_session()
    imported_assets: list[tuple[int, str]] = []

    try:
        importer = AssetImporter(session, dry_run=dry_run, batch_size=batch_size)
        stats = importer.run(all_rows)
        stats.total_csv_rows = len(all_rows) + total_dup + total_inv
        stats.duplicates_in_csv = total_dup
        stats.invalid_rows = total_inv
        stats.print_summary()

        if not dry_run:
            # Fetch active asset IDs and Tickers for enrichment
            from models import Asset
            active_assets = session.query(Asset.id, Asset.ticker).filter(Asset.is_active.is_(True)).all()
            imported_assets = [(a.id, a.ticker) for a in active_assets if a.ticker]

    except Exception as exc:
        session.rollback()
        logger.exception("❌ Error during import: %s", exc)
        sys.exit(1)
    finally:
        session.close()

    if dry_run:
        logger.info("🔍 DRY-RUN mode finished — no permanent changes made.")
        return

    # 4. yfinance enrichment (if requested)
    if enrich and imported_assets:
        logger.info("✨ Launching yfinance deep enrichment (%d assets)...", len(imported_assets))
        await enrich_batch(db_service, imported_assets, batch_size=5)

    # 5. EOD historical ingestion (if requested)
    if ingest_history and imported_assets:
        logger.info("📈 Launching EOD historical ingestion for assets...")
        try:
            import redis.asyncio as redis

            from historical.ingestion_service import HistoricalIngestionService


            query_svc = QueryService()
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            redis_client = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)

            ingest_svc = HistoricalIngestionService(
                db_service=db_service, query_service=query_svc, redis_client=redis_client
            )
            tickers = [t for _, t in imported_assets]
            results = await ingest_svc.ingest_bulk(
                tickers=tickers, resolution="1D", source="auto", force_refresh=False, concurrency=5
            )
            success = sum(1 for r in results if r.status in ("success", "up_to_date"))
            logger.info("✅ Historical ingestion completed: %d/%d successful", success, len(tickers))

            await query_svc.close()
            await redis_client.close()
        except Exception as e:
            logger.warning("⚠️ Historical ingestion partially failed: %s", e)

    elapsed = time.time() - start_time
    logger.info("🎉 Complete initialization finished successfully in %.2f seconds!", elapsed)


def main():
    parser = argparse.ArgumentParser(description="Database initialization and seeding script for Fonrex")
    parser.add_argument("--file", help="Specific CSV file to import")
    parser.add_argument("--dir", help="Directory containing CSV files to import")
    parser.add_argument("--batch-size", type=int, default=200, help="Batch import size (default: 200)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying database")
    parser.add_argument("--enrich", action="store_true", help="Automatically enrich yfinance metadata")
    parser.add_argument("--ingest-history", action="store_true", help="Download initial 1D price history")

    args = parser.parse_args()

    try:
        files = resolve_seed_files(file_arg=args.file, dir_arg=args.dir)
    except FileNotFoundError as err:
        logger.error("❌ %s", err)
        sys.exit(1)

    asyncio.run(
        run_seeding(
            files=files,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            enrich=args.enrich,
            ingest_history=args.ingest_history,
        )
    )


if __name__ == "__main__":
    main()
