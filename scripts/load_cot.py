#!/usr/bin/env python
"""Download the Commitments of Traders reports of the CFTC into the database.

    python scripts/load_cot.py                         # the three reports, whole history
    python scripts/load_cot.py --report tff            # one report
    python scripts/load_cot.py --start-year 2020       # some years
    python scripts/load_cot.py --force                 # even the years up to date

Legacy starts in 1986, TFF and Disaggregated in June 2006. The years up to 2016 are
one file per report (Legacy: 23 MB), then one file a year of 1 to 3 MB: the whole
history takes about 15 to 20 minutes. The years up to 2016 are read once; a later
past year is not read again once read from 15 January of the next year on, the
current year only after COT_REFRESH_HOURS. The exit code is 1 when a year could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.lifecycle import AsyncDatabaseResources
from financials.providers.cftc import REPORTS, CftcProvider
from macro.cot_reports import CotReports, ReportLoad

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FIRST_YEAR = min(report.first_year for report in REPORTS.values())
_WIDTH = max(len(name) for name in REPORTS)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", nargs="+", choices=list(REPORTS), default=list(REPORTS))
    parser.add_argument(
        "--start-year",
        type=int,
        default=FIRST_YEAR,
        help=f"first year read (default {FIRST_YEAR}: the first year of each report)",
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


def describe(load: ReportLoad) -> str:
    held = f"{load.first_day} → {load.last_day}" if load.last_day else "nothing stored"
    line = f"{load.report:<{_WIDTH}} {load.status:<8} {held}"
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
        reports = CotReports(resources.session_factory, CftcProvider().report_file)
        loads = [
            await reports.refresh(
                name, arguments.start_year, arguments.end_year, force=arguments.force
            )
            for name in arguments.report
        ]
    finally:
        await resources.close()
    for load in loads:
        print(describe(load))
    return 1 if any(load.status == "failed" for load in loads) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
