#!/usr/bin/env python
"""Download the ECB reference rates of the euro into the database.

    python scripts/load_fx_rates.py                      # the default currencies
    python scripts/load_fx_rates.py --currency USD GBP   # some currencies
    python scripts/load_fx_rates.py --force              # even if read recently

The first run reads the whole history (since 1999); the next ones only the days
not stored yet. The exit code is 1 when a currency could not be read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.lifecycle import AsyncDatabaseResources
from macro.fx_rates import DEFAULT_CURRENCIES, EcbExchangeRates, FxLoad

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--currency", nargs="+", default=list(DEFAULT_CURRENCIES))
    parser.add_argument("--force", action="store_true", help="ask the ECB even if read recently")
    return parser.parse_args(argv)


def describe(load: FxLoad) -> str:
    held = f"{load.first_day} → {load.last_day}" if load.last_day else "nothing stored"
    line = f"{load.currency:<4} {load.status:<8} {held}"
    if load.status == "fetched":
        line += f"  ({load.added} days read)"
    return f"{line}  ({load.reason})" if load.reason else line


async def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    resources = AsyncDatabaseResources.create()
    if resources is None:
        print("DATABASE_URL (or ASYNC_DATABASE_URL) is not set.", file=sys.stderr)
        return 2
    try:
        rates = EcbExchangeRates(resources.session_factory)
        loads = [await rates.refresh(code, force=arguments.force) for code in arguments.currency]
    finally:
        await resources.close()
    for load in loads:
        print(describe(load))
    return 1 if any(load.status == "failed" for load in loads) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
