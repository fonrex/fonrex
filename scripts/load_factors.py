#!/usr/bin/env python
"""Download the factor files of the Kenneth French library into the database.

    python scripts/load_factors.py                          # every dataset, monthly
    python scripts/load_factors.py --dataset us_3 europe_3 --frequency monthly daily
    python scripts/load_factors.py --force                  # even if read recently

A file read less than FACTORS_REFRESH_DAYS days ago is not asked again unless
--force is given. The exit code is 1 when a file could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.lifecycle import AsyncDatabaseResources
from factors.french_library import DATASETS, FREQUENCIES
from factors.store import FactorLibrary, FactorLoad

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", nargs="+", choices=sorted(DATASETS), default=sorted(DATASETS))
    parser.add_argument("--frequency", nargs="+", choices=FREQUENCIES, default=["monthly"])
    parser.add_argument("--force", action="store_true", help="download even if read recently")
    return parser.parse_args(argv)


def describe(load: FactorLoad) -> str:
    held = (
        f"{load.periods} periods {load.first_period} → {load.last_period}"
        if load.periods
        else "nothing stored"
    )
    line = f"{load.dataset:<14} {load.frequency:<8} {load.status:<8} {held}"
    return f"{line}  ({load.reason})" if load.reason else line


async def load(library: FactorLibrary, arguments: argparse.Namespace) -> list[FactorLoad]:
    return [
        await library.refresh(dataset, frequency, force=arguments.force)
        for dataset in arguments.dataset
        for frequency in arguments.frequency
    ]


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    try:
        loads = await load(FactorLibrary(resources.session_factory), arguments)
    finally:
        await resources.close()
    for result in loads:
        print(describe(result))
    return 1 if any(result.status == "failed" for result in loads) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
