#!/usr/bin/env python
"""Read the ESEF annual reports of the shares of the catalogue into the database.

    python scripts/load_esef.py                          # every share with an ISIN
    python scripts/load_esef.py --ticker AIR.PA TTE.PA   # some shares
    python scripts/load_esef.py --force                  # read every filing again
    python scripts/load_esef.py --recheck-lei            # ask GLEIF for the LEI again

For each share, the LEI is asked to GLEIF with its ISIN and confirmed by the name
of the company; the filings of that LEI are then listed from filings.xbrl.org and
the filings never read are read, one per period. A filing weighs 0.1 to 32 MB:
the first reading of a large catalogue takes a while, the next ones only read the
new filings. A share read less than ESEF_REFRESH_HOURS ago is not asked again.
The funds, the shares without ISIN and the companies GLEIF does not confirm are
listed as "no LEI": filings.xbrl.org is not asked about them. The exit code is 1
when a share could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from database.lifecycle import AsyncDatabaseResources
from database.price_series import resolve_price_series_async
from financials.esef_lei import LeiResolver, SqlLeiStore
from financials.esef_statements import EsefLoad, EsefStatements
from financials.providers.esef import EsefProvider
from financials.sec_cik import NO_STATEMENTS_QUOTE_TYPES
from models import Asset

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ticker", nargs="+", help="tickers of the catalogue (default: every share with an ISIN)"
    )
    parser.add_argument("--force", action="store_true", help="list and read every filing again")
    parser.add_argument(
        "--recheck-lei", action="store_true", help="ask GLEIF for the LEI of each share again"
    )
    return parser.parse_args(argv)


def describe(name: str, load: EsefLoad) -> str:
    """One line for a share: its status, its LEI and what was read."""
    if load.status == "no_lei":
        return f"{name:<14} no LEI    {load.reason}"
    held = (
        f"{load.facts} values, last period {load.last_report_end}"
        if load.facts
        else "nothing stored"
    )
    line = f"{name:<14} {load.status:<8}  {load.lei}  {load.filings} filings, {held}"
    if load.read:
        line += f"  ({len(load.read)} read)"
    if load.refused:
        line += f"  (refused: {', '.join(sorted(load.refused))})"
    if load.reason:
        line += f"  ({load.reason})"
    return line


async def instruments(session_factory, tickers: list[str] | None) -> list[tuple[str, int | None]]:
    """The shares to read, by name: those asked, or every active share with an ISIN."""
    async with session_factory() as session:
        if tickers:
            found = []
            for ticker in tickers:
                series = await resolve_price_series_async(session, ticker)
                found.append((ticker.upper(), series.asset_id if series else None))
            return found
        rows = (
            await session.execute(
                select(Asset.id, Asset.ticker, Asset.quote_type)
                .where(Asset.is_active.is_(True), Asset.isin.is_not(None))
                .order_by(Asset.ticker)
            )
        ).all()
    return [
        (ticker, asset_id)
        for asset_id, ticker, quote_type in rows
        if (quote_type or "").strip().upper() not in NO_STATEMENTS_QUOTE_TYPES
    ]


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    failed = 0
    counts = {"read": 0, "no LEI": 0, "failed": 0, "unknown": 0}
    try:
        session_factory = resources.session_factory
        provider = EsefProvider()
        statements = EsefStatements(
            session_factory,
            provider,
            LeiResolver(SqlLeiStore(session_factory), provider.lei_records),
        )
        shares = await instruments(session_factory, arguments.ticker)
        for name, asset_id in shares:
            if asset_id is None:
                print(f"{name:<14} unknown   not in the catalogue")
                counts["unknown"] += 1
                failed += 1
                continue
            load = await statements.refresh(
                asset_id, force=arguments.force, recheck_lei=arguments.recheck_lei
            )
            print(describe(name, load), flush=True)
            if load.status == "no_lei":
                counts["no LEI"] += 1
            elif load.status == "failed":
                counts["failed"] += 1
                failed += 1
            else:
                counts["read"] += 1
    finally:
        await resources.close()
    summary = f"{len(shares)} shares: {counts['read']} read, {counts['no LEI']} without LEI, "
    summary += f"{counts['failed']} failed"
    if counts["unknown"]:
        summary += f", {counts['unknown']} unknown"
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
