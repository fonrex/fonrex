"""Zipline bundle exposing FonRex EOD prices to Zipline backtests.

This package implements a ``zipline-reloaded`` data bundle backed by the
FonRex TimescaleDB ``prices_eod`` table. It is optional at runtime: only the
CLI and helper factories require ``zipline-reloaded`` to be installed. The
data source layer works without Zipline so unit tests can exercise the SQL
extraction pipeline in isolation.

Typical usage in ``~/.zipline/extension.py``::

    from zipline_bundle import register_fonrex_bundle

    register_fonrex_bundle(
        bundle_name="fonrex",
        tickers=["AAPL", "MSFT"],
        calendar_name="NYSE",
    )

See ``docs/zipline-bundle.md`` for a full walkthrough.
"""

from zipline_bundle.bundle import (
    FonRexBundle,
    fonrex_equities,
    register_fonrex_bundle,
)
from zipline_bundle.data_source import (
    FonRexBundleDataSource,
    TickerBars,
    TickerMetadata,
)

__all__ = [
    "FonRexBundle",
    "FonRexBundleDataSource",
    "TickerBars",
    "TickerMetadata",
    "fonrex_equities",
    "register_fonrex_bundle",
]
