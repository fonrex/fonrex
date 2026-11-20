#!/usr/bin/env python
"""Download the 3-month interest rates of the OECD into the database.

    python scripts/load_short_rates.py                  # whole history the first time, then the last 24 months
    python scripts/load_short_rates.py --start 2020-01  # from a month
    python scripts/load_short_rates.py --force          # even when read less than OECD_REFRESH_HOURS ago

One request returns every country, from 1990 the first time. The OECD allows
about 60 downloads an hour: a refusal keeps the rates stored. The exit code is 1
when the rates could not be read.
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
from financials.providers.oecd import OECDProvider
from macro.short_rates import FAILED, FIRST_PERIOD, RatesLoad, ShortRates, StoredArea

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


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
    try:
        rates = ShortRates(resources.session_factory, OECDProvider().short_rates)
        load = await rates.refresh(force=arguments.force, start=arguments.start)
        areas = await rates.stored_areas()
    finally:
        await resources.close()
    print("\n".join(describe(load, areas)))
    return 1 if load.status == FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
