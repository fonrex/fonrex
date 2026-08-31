"""Command-line helpers for the FonRex Zipline bundle.

Provides two shortcuts that avoid touching ``~/.zipline/extension.py`` when
running from a container or a CI pipeline:

.. code-block:: bash

    # Preview which tickers would be ingested for a given window
    python -m zipline_bundle preview --start 2024-01-01 --end 2024-12-31

    # Register + trigger a Zipline ingest inline (equivalent to
    # `zipline ingest -b fonrex` once the extension file is installed).
    python -m zipline_bundle ingest \\
        --start 2024-01-01 --end 2024-12-31 \\
        --tickers AAPL,MSFT --calendar NYSE
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import pandas as pd

from zipline_bundle.data_source import FonRexBundleDataSource

logger = logging.getLogger("zipline_bundle.cli")


def _parse_tickers(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [chunk.strip().upper() for chunk in raw.split(",") if chunk.strip()]


def _parse_date(value: str) -> pd.Timestamp:
    try:
        return pd.Timestamp(date.fromisoformat(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid ISO date {value!r}. Expected YYYY-MM-DD."
        ) from exc


def _cmd_preview(args: argparse.Namespace) -> int:
    with FonRexBundleDataSource(
        database_url=args.database_url,
        tickers=_parse_tickers(args.tickers),
    ) as source:
        bars = list(source.iter_tickers(args.start, args.end))

    if not bars:
        logger.warning("No ticker matched the request.")
        return 1

    print(f"{'symbol':<12}{'exchange':<10}{'sid':>6}  {'first':<12}{'last':<12}rows")
    for entry in bars:
        meta = entry.metadata
        print(
            f"{meta.symbol:<12}{meta.exchange or '-':<10}{meta.sid:>6}  "
            f"{meta.start_date.date().isoformat():<12}"
            f"{meta.end_date.date().isoformat():<12}"
            f"{len(entry.frame)}"
        )
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    try:
        from zipline.data.bundles import ingest as zipline_ingest
    except ImportError as exc:
        raise SystemExit(
            "zipline-reloaded is required for `python -m zipline_bundle ingest`. "
            "Install it with `pip install zipline-reloaded`."
        ) from exc

    from zipline_bundle.bundle import register_fonrex_bundle

    register_fonrex_bundle(
        bundle_name=args.bundle_name,
        tickers=_parse_tickers(args.tickers),
        database_url=args.database_url,
        calendar_name=args.calendar,
        start_session=args.start,
        end_session=args.end,
    )
    zipline_ingest(args.bundle_name, show_progress=not args.quiet)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zipline_bundle",
        description="Operate the FonRex-backed Zipline data bundle.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--start", required=True, type=_parse_date, help="Ingest window start (YYYY-MM-DD)")
    common.add_argument("--end", required=True, type=_parse_date, help="Ingest window end (YYYY-MM-DD)")
    common.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated ticker whitelist. Defaults to every asset with EOD data.",
    )
    common.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL. Falls back to $DATABASE_URL then the local docker-compose default.",
    )

    preview = subparsers.add_parser("preview", parents=[common], help="Print the tickers and bars the bundle would ingest.")
    preview.set_defaults(func=_cmd_preview)

    ingest = subparsers.add_parser("ingest", parents=[common], help="Register and run a full Zipline ingest.")
    ingest.add_argument("--bundle-name", default="fonrex", help="Zipline bundle name to register (default: fonrex).")
    ingest.add_argument("--calendar", default="NYSE", help="Trading calendar name (default: NYSE).")
    ingest.add_argument("--quiet", action="store_true", help="Silence Zipline's progress bar.")
    ingest.set_defaults(func=_cmd_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
