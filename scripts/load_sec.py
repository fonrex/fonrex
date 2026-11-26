#!/usr/bin/env python
"""Read the SEC filings of the shares of the catalogue into the database.

    python scripts/load_sec.py                           # every share of the catalogue
    python scripts/load_sec.py --ticker AAPL JPM         # some shares
    python scripts/load_sec.py --force                   # read every company again
    python scripts/load_sec.py --recheck-cik             # look the CIK up again

For each share, the CIK is looked up in the table of tickers of the SEC (a US
listing of the catalogue is needed), then the facts of the company are read from
the SEC (``companyfacts``) and the concepts Fonrex maps are stored. Only those
concepts are kept: when the mapping reads a new concept (migration 030), the
companies must be read again with ``--force``. A company read less than
SEC_XBRL_REFRESH_HOURS ago is not asked again without it. The funds are left
out. The exit code is 1 when a share could not be read.
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
from financials.providers.sec_edgar import SECEdgarProvider
from financials.sec_cik import NO_STATEMENTS_QUOTE_TYPES, SecCikResolver, SqlCikStore
from financials.sec_statements import SecLoad, SecStatements
from models import Asset

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Errors in a row after which the reading stops: the database (or the machine) is
# down, and every next share would fail the same way.
ERRORS_IN_A_ROW = 20


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ticker", nargs="+", help="tickers of the catalogue (default: every share)"
    )
    parser.add_argument(
        "--force", action="store_true", help="read every company again, even if read recently"
    )
    parser.add_argument(
        "--recheck-cik", action="store_true", help="look the CIK of each share up again"
    )
    return parser.parse_args(argv)


def describe(name: str, load: SecLoad) -> str:
    """One line for a share: its status, its CIK and what is stored."""
    if load.status == "no_cik":
        return f"{name:<14} no CIK    {load.reason}"
    held = f"{load.facts} facts, last filed {load.last_filed}" if load.facts else "nothing stored"
    line = f"{name:<14} {load.status:<8}  {load.cik}  {held}"
    if load.reason:
        line += f"  ({load.reason})"
    return line


async def instruments(session_factory, tickers: list[str] | None) -> list[tuple[str, int | None]]:
    """The shares to read, by name: those asked, or every active share (no fund)."""
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
                .where(Asset.is_active.is_(True))
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
    in_a_row = 0
    stopped = None
    shares: list[tuple[str, int | None]] = []
    counts = {"read": 0, "no CIK": 0, "failed": 0, "unknown": 0}
    try:
        session_factory = resources.session_factory
        provider = SECEdgarProvider()
        statements = SecStatements(
            session_factory,
            provider,
            SecCikResolver(SqlCikStore(session_factory), provider.ticker_table),
        )
        shares = await instruments(session_factory, arguments.ticker)
        for name, asset_id in shares:
            if asset_id is None:
                print(f"{name:<14} unknown   not in the catalogue")
                counts["unknown"] += 1
                failed += 1
                continue
            try:
                load = await statements.refresh(
                    asset_id, force=arguments.force, recheck_cik=arguments.recheck_cik
                )
            except Exception as error:  # one company never stops the others
                message = str(error).splitlines()[0][:200] if str(error) else ""
                print(f"{name:<14} error     {type(error).__name__}: {message}", flush=True)
                counts["failed"] += 1
                failed += 1
                in_a_row += 1
                if in_a_row == ERRORS_IN_A_ROW:
                    stopped = name
                    break
                continue
            in_a_row = 0
            print(describe(name, load), flush=True)
            if load.status == "no_cik":
                counts["no CIK"] += 1
            elif load.status == "failed":
                counts["failed"] += 1
                failed += 1
            else:
                counts["read"] += 1
    finally:
        await resources.close()
    summary = f"{len(shares)} shares: {counts['read']} read, {counts['no CIK']} without CIK, "
    summary += f"{counts['failed']} failed"
    if counts["unknown"]:
        summary += f", {counts['unknown']} unknown"
    print(summary)
    if stopped:
        print(
            f"Stopped at {stopped}: {ERRORS_IN_A_ROW} errors in a row. Check the database, "
            "then run the script again: the companies read are not read again."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
