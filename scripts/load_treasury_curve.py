#!/usr/bin/env python
"""Download the daily yield curves of the US Treasury into the database.

    python scripts/load_treasury_curve.py                     # both curves, whole history
    python scripts/load_treasury_curve.py --curve nominal     # one curve
    python scripts/load_treasury_curve.py --start-year 2020   # some years
    python scripts/load_treasury_curve.py --force             # even the years up to date

The nominal curve starts in 1990, the real one in 2003: one file a year, a few
kilobytes each. A past year read after its end is not read again, the current
year only after TREASURY_REFRESH_HOURS. The exit code is 1 when a year could not
be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.lifecycle import AsyncDatabaseResources
from financials.providers.treasury import CURVES, TreasuryProvider
from macro.treasury_curves import CurveLoad, TreasuryCurves

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FIRST_YEAR = min(curve.first_year for curve in CURVES.values())


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--curve", nargs="+", choices=list(CURVES), default=list(CURVES))
    parser.add_argument(
        "--start-year",
        type=int,
        default=FIRST_YEAR,
        help=f"first year read (default {FIRST_YEAR}: the first year of each curve)",
    )
    parser.add_argument("--end-year", type=int, help="last year read (default: the current one)")
    parser.add_argument("--force", action="store_true", help="read even the years up to date")
    arguments = parser.parse_args(argv)
    if arguments.end_year is not None and arguments.start_year > arguments.end_year:
        parser.error("--start-year is after --end-year")
    return arguments


def _years(years: list[int]) -> str:
    if len(years) > 3 and years == list(range(years[0], years[-1] + 1)):
        return f"{years[0]}–{years[-1]}"
    return ", ".join(str(year) for year in years)


def describe(load: CurveLoad) -> str:
    held = f"{load.first_day} → {load.last_day}" if load.last_day else "nothing stored"
    line = f"{load.curve:<8} {load.status:<8} {held}"
    if load.years_read:
        line += f"  ({len(load.years_read)} years read: {_years(load.years_read)})"
    if load.years_failed:
        line += f"  (not read: {_years(load.years_failed)}, {load.reason})"
    return line


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    try:
        curves = TreasuryCurves(resources.session_factory, TreasuryProvider().curve_year)
        loads = [
            await curves.refresh(
                name, arguments.start_year, arguments.end_year, force=arguments.force
            )
            for name in arguments.curve
        ]
    finally:
        await resources.close()
    for load in loads:
        print(describe(load))
    return 1 if any(load.status == "failed" for load in loads) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
