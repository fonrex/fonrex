#!/usr/bin/env python
"""Report how many shares of the catalogue have each line of the financial statements.

    python scripts/statements_coverage.py                 # a Markdown table
    python scripts/statements_coverage.py --json          # the counts as JSON
    python scripts/statements_coverage.py --ticker AAPL AIR.PA

For each source (Yahoo, the SEC, the ESEF filings) and each line the ratios and
scores read (issue #70), the share of the instruments whose latest fiscal year
holds the line, among the instruments that have a fiscal year from that source.
The report reads the database only: nothing is asked to Yahoo, the SEC, GLEIF or
filings.xbrl.org, and nothing is written. Run it after ``scripts/load_sec.py
--force``, ``scripts/load_esef.py`` and an enrichment of the catalogue.

Banks and insurers publish no current assets nor liabilities: a second table
leaves out the instruments of the financial sector (``sector`` or ``gic_sector``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from database.lifecycle import AsyncDatabaseResources
from database.price_series import resolve_price_series_async
from financials.esef_lei import LeiResolver, SqlLeiStore
from financials.esef_statements import EsefStatements
from financials.fiscal_years import fiscal_years
from financials.sec_cik import NO_STATEMENTS_QUOTE_TYPES, SecCikResolver, SecTableError, SqlCikStore
from financials.sec_statements import SecStatements
from models import Asset, FinancialStatement

SOURCES = ("yahoo", "sec", "esef")
#: The lines added by migration 030, then the lines Yahoo now fills (issue #70, phase 3).
LINES = (
    "total_current_assets",
    "total_current_liabilities",
    "ppe_net",
    "accounts_payable",
    "income_before_tax",
    "accounts_receivable",
    "inventory",
    "retained_earnings",
    "long_term_debt",
    "goodwill",
    "intangible_assets",
    "short_term_investments",
    "depreciation_amortization",
    "sga_expense",
    "rd_expense",
    "stock_repurchases",
)
FINANCIAL_SECTORS = frozenset({"financial services", "financials", "financial"})


@dataclass(frozen=True)
class Latest:
    """The latest fiscal year of an instrument from one source: which lines it holds."""

    source: str
    asset_id: int
    financial: bool
    figures: dict[str, Any]


def figures_of(year: Any, lines: Iterable[str] = LINES) -> dict[str, Any]:
    """The lines of a fiscal year, whether read from Yahoo or from a filing."""
    if hasattr(year, "value"):  # financials.xbrl_mapping.FiscalYear
        return {line: year.value(line) for line in lines}
    return {line: getattr(year, line, None) for line in lines}


def is_financial(sector: str | None, gic_sector: str | None) -> bool:
    return any((name or "").strip().lower() in FINANCIAL_SECTORS for name in (sector, gic_sector))


def coverage(
    latests: Iterable[Latest], *, without_financials: bool = False, lines=LINES
) -> dict[str, dict[str, tuple[int, int]]]:
    """For each source and line: (instruments whose latest year holds it, instruments)."""
    counts = {source: {line: [0, 0] for line in lines} for source in SOURCES}
    for latest in latests:
        if without_financials and latest.financial:
            continue
        for line in lines:
            cell = counts[latest.source][line]
            cell[1] += 1
            if latest.figures.get(line) is not None:
                cell[0] += 1
    return {
        source: {line: (filled, total) for line, (filled, total) in by_line.items()}
        for source, by_line in counts.items()
    }


def _cell(filled: int, total: int) -> str:
    if not total:
        return "—"
    return f"{round(100 * filled / total)} % ({filled}/{total})"


def render(counts: dict[str, dict[str, tuple[int, int]]], title: str) -> str:
    lines = [f"### {title}", "", "| Line | Yahoo | SEC | ESEF |", "|---|---|---|---|"]
    for line in next(iter(counts.values())):
        cells = " | ".join(_cell(*counts[source][line]) for source in SOURCES)
        lines.append(f"| `{line}` | {cells} |")
    return "\n".join(lines)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ticker", nargs="+", help="tickers of the catalogue (default: every share)"
    )
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    return parser.parse_args(argv)


async def _no_sec_table() -> None:
    raise SecTableError("not asked by the coverage report")


async def _no_gleif(isin: str) -> None:
    return None  # "GLEIF did not answer": nothing is asked, nothing remembered


async def shares(session_factory, tickers: list[str] | None) -> list[tuple[int, bool]]:
    """The instruments to report on: (id, of the financial sector)."""
    async with session_factory() as session:
        query = select(Asset.id, Asset.quote_type, Asset.sector, Asset.gic_sector)
        if tickers:
            ids = []
            for ticker in tickers:
                series = await resolve_price_series_async(session, ticker)
                if series is not None:
                    ids.append(series.asset_id)
            query = query.where(Asset.id.in_(ids))
        else:
            query = query.where(Asset.is_active.is_(True))
        rows = (await session.execute(query.order_by(Asset.id))).all()
    return [
        (asset_id, is_financial(sector, gic_sector))
        for asset_id, quote_type, sector, gic_sector in rows
        if (quote_type or "").strip().upper() not in NO_STATEMENTS_QUOTE_TYPES
    ]


async def latest_years(session_factory, instruments: list[tuple[int, bool]]) -> list[Latest]:
    sec = SecStatements(
        session_factory, None, SecCikResolver(SqlCikStore(session_factory), _no_sec_table)
    )
    esef = EsefStatements(
        session_factory, None, LeiResolver(SqlLeiStore(session_factory), _no_gleif)
    )
    found = []
    for asset_id, financial in instruments:
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(FinancialStatement).where(
                            FinancialStatement.asset_id == asset_id,
                            FinancialStatement.period_type == "annual",
                        )
                    )
                )
                .scalars()
                .all()
            )
        years = {"yahoo": fiscal_years(rows, limit=1)}
        years["sec"] = (await sec.annual(asset_id, refresh=False))[1]
        years["esef"] = (await esef.annual(asset_id, refresh=False))[1]
        for source, of_source in years.items():
            if of_source:
                found.append(Latest(source, asset_id, financial, figures_of(of_source[0])))
    return found


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    try:
        instruments = await shares(resources.session_factory, arguments.ticker)
        latests = await latest_years(resources.session_factory, instruments)
    finally:
        await resources.close()
    every = coverage(latests)
    without = coverage(latests, without_financials=True)
    if arguments.json:
        print(json.dumps({"instruments": len(instruments), "all": every, "non_financial": without}))
    else:
        print(f"{len(instruments)} instruments; latest fiscal year of each source.\n")
        print(render(every, "Every instrument"))
        print()
        print(render(without, "Without banks and insurers"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
