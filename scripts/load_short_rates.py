#!/usr/bin/env python
"""Download the short-term interest rates of the OECD and of the Bank of England into the database.

    python scripts/load_short_rates.py                  # whole history the first time, then the last 24 months
    python scripts/load_short_rates.py --start 2020-01  # from a month
    python scripts/load_short_rates.py --force          # even when read less than *_REFRESH_HOURS ago
    python scripts/load_short_rates.py --source boe     # one source only (oecd, boe)

The OECD gives the 3-month rate of every country in one request, from 1990 the
first time; it allows about 60 downloads an hour: a refusal keeps the rates
stored. The Bank of England gives the monthly SONIA, the rate of the pound, from
1997. The exit code is 1 when the rates of a source could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.lifecycle import AsyncDatabaseResources
from financials.providers.boe import BankOfEnglandProvider
from financials.providers.oecd import OECDProvider
from macro.short_rates import (
    FAILED,
    FIRST_PERIOD,
    OECD_SERIES,
    SONIA_SERIES,
    RateSeries,
    RatesLoad,
    RatesReader,
    ShortRates,
    StoredArea,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# Each source: its title in the output, its series, and the reader of its rates.
SOURCES = {
    "oecd": ("OECD, 3-month rates", OECD_SERIES, lambda: OECDProvider().short_rates),
    "boe": ("Bank of England, SONIA", SONIA_SERIES, lambda: BankOfEnglandProvider().sonia_monthly),
}


def month(text: str) -> date:
    try:
        year, number = (int(part) for part in text.split("-"))
        return date(year, number, 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month YYYY-MM") from error


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--start",
        type=month,
        help=f"first month read, YYYY-MM (default: {FIRST_PERIOD:%Y-%m} the first time, "
        "then the last 24 months)",
    )
    parser.add_argument("--force", action="store_true", help="read even when up to date")
    parser.add_argument(
        "--source",
        choices=sorted(SOURCES),
        help="read one source only (default: every source)",
    )
    arguments = parser.parse_args(argv)
    if arguments.start is not None and arguments.start < FIRST_PERIOD:
        parser.error(f"--start is before {FIRST_PERIOD:%Y-%m}")
    return arguments


def describe(load: RatesLoad, areas: list[StoredArea]) -> list[str]:
    line = f"{load.status:<8}"
    if load.received:
        line += f" {load.received} rates of {len(load.areas)} areas from {load.start:%Y-%m}"
    if load.reason:
        line += f" ({load.reason})"
    lines = [line]
    for area in areas:
        held = (
            f"{area.first_period:%Y-%m} → {area.last_period:%Y-%m}"
            if area.first_period and area.last_period
            else "nothing stored"
        )
        lines.append(f"  {area.area:<6} {held}")
    return lines


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    names = [arguments.source] if arguments.source else list(SOURCES)
    failed = False
    try:
        for name in names:
            title, series, reader = SOURCES[name]
            load, areas = await read(resources, series, reader(), arguments)
            print("\n".join([title, *describe(load, areas)]))
            failed = failed or load.status == FAILED
    finally:
        await resources.close()
    return 1 if failed else 0


async def read(
    resources: AsyncDatabaseResources,
    series: RateSeries,
    reader: RatesReader,
    arguments: argparse.Namespace,
) -> tuple[RatesLoad, list[StoredArea]]:
    """Read the rates of one source, then the months stored for each of its areas."""
    rates = ShortRates(resources.session_factory, reader, series=series)
    load = await rates.refresh(force=arguments.force, start=arguments.start)
    return load, await rates.stored_areas()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
