# Zipline Bundle (Backtesting Integration)

FonRex ships a first-class [`zipline-reloaded`](https://github.com/stefan-jansen/zipline-reloaded) data bundle backed by the `prices_eod` TimescaleDB table. It lets you run Zipline backtests directly against your FonRex database without exporting CSVs or maintaining a parallel dataset.

## Table of contents

- [Prerequisites](#prerequisites)
- [Install the extension](#install-the-extension)
- [Ingest data](#ingest-data)
- [Backtest against the bundle](#backtest-against-the-bundle)
- [Configuration reference](#configuration-reference)
- [Trading calendars](#trading-calendars)
- [Adjustments (splits & dividends)](#adjustments-splits--dividends)
- [Programmatic API](#programmatic-api)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- A running FonRex Postgres database with EOD rows already ingested by `HistoricalIngestionService` (see the `POST /historical/ingest` and `POST /historical/ingest/bulk` endpoints).
- `zipline-reloaded>=3.0` installed in the same Python environment as FonRex.

```bash
pip install zipline-reloaded
```

The dependency is intentionally left out of `requirements.txt`: the FastAPI service never imports Zipline at runtime, so we do not want to pull its heavy transitive dependencies (bcolz, empyrical, exchange-calendars, tables…) into the API container.

## Install the extension

Zipline discovers bundles through `~/.zipline/extension.py`. Copy the sample shipped with the repository and let it register the `fonrex` bundle:

```bash
mkdir -p ~/.zipline
cp zipline_bundle/extension.py ~/.zipline/extension.py
```

Verify the registration:

```bash
zipline bundles
# fonrex <no ingestions>
```

## Ingest data

```bash
export DATABASE_URL="postgresql://fonrex:fonrex@localhost:5432/fonrex"
zipline ingest -b fonrex
```

You can also drive the ingestion without touching the extension file. The bundle exposes a helper CLI that registers the bundle in-process and triggers `zipline ingest` on the fly:

```bash
python -m zipline_bundle ingest \
    --start 2020-01-01 \
    --end 2024-12-31 \
    --tickers AAPL,MSFT,GOOGL \
    --calendar NYSE
```

Preview the tickers and OHLCV bars the bundle would produce, without touching Zipline at all:

```bash
python -m zipline_bundle preview --start 2024-01-01 --end 2024-12-31
```

## Backtest against the bundle

Once the bundle is ingested, any Zipline algorithm can consume it:

```python
from zipline import run_algorithm
from zipline.api import order_target_percent, symbol

def initialize(context):
    context.asset = symbol("AAPL")

def handle_data(context, data):
    order_target_percent(context.asset, 1.0)

result = run_algorithm(
    start=pd.Timestamp("2023-01-03"),
    end=pd.Timestamp("2023-12-29"),
    initialize=initialize,
    handle_data=handle_data,
    capital_base=100_000,
    bundle="fonrex",
    trading_calendar=get_calendar("NYSE"),
)
```

## Configuration reference

The extension file honours the following environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://fonrex:fonrex@localhost:5432/fonrex` | SQLAlchemy URL used by the bundle to read `prices_eod`. Async URLs (`postgresql+asyncpg://`) are auto-normalised. |
| `FONREX_BUNDLE_NAME` | `fonrex` | Bundle name registered with Zipline. |
| `FONREX_BUNDLE_TICKERS` | *(empty)* | Comma-separated whitelist. Empty means ingest every asset with EOD rows in the ingest window. |
| `FONREX_BUNDLE_CALENDAR` | `NYSE` | Trading calendar name. See below. |

## Trading calendars

Zipline aligns bars on the sessions of a trading calendar. `NYSE` covers US-listed instruments; for other exchanges use the matching `exchange-calendars` code:

| Market | Calendar name |
| --- | --- |
| Nasdaq / NYSE | `NYSE` (default) |
| Euronext Paris | `XPAR` |
| Deutsche Börse Xetra | `XETR` |
| London Stock Exchange | `XLON` |
| Six Swiss Exchange | `XSWX` |

If your database mixes several markets, ingest one bundle per calendar with distinct names:

```python
register_fonrex_bundle(
    bundle_name="fonrex_us",
    tickers=["AAPL", "MSFT"],
    calendar_name="NYSE",
)
register_fonrex_bundle(
    bundle_name="fonrex_paris",
    tickers=["AIR.PA", "BNP.PA"],
    calendar_name="XPAR",
)
```

Rows whose date falls outside the calendar's trading sessions are dropped by the bundle to avoid `BcolzDailyBarWriter` rejecting them.

## Adjustments (splits & dividends)

FonRex does not track corporate actions as first-class rows yet. The bundle takes a pragmatic approach:

- The `close` column of the daily bars is populated from `adj_close` when available, so backtests already run on adjusted prices (matching the historical behaviour of Yahoo Finance ingestion).
- Empty splits and dividends DataFrames are handed to Zipline's `SQLiteAdjustmentWriter` to keep the schema initialised.

If you later add corporate action tables to FonRex, extend `zipline_bundle/bundle.py` to populate the `splits` and `dividends` DataFrames — the writer plumbing is already in place.

## Programmatic API

For automated pipelines, everything is available as importable symbols:

```python
from zipline_bundle import (
    FonRexBundle,           # low-level ingest callable (takes writer objects)
    fonrex_equities,        # factory that returns the ingest callable
    register_fonrex_bundle, # registers the bundle with Zipline's registry
    FonRexBundleDataSource, # zipline-free SQL extraction layer (useful in tests)
)
```

For example, to run an ingest inside a Python script:

```python
from zipline_bundle import register_fonrex_bundle
from zipline.data.bundles import ingest

register_fonrex_bundle(
    bundle_name="fonrex",
    tickers=["AAPL", "MSFT"],
    calendar_name="NYSE",
)
ingest("fonrex", show_progress=True)
```

## Troubleshooting

**`zipline bundles` doesn't list `fonrex`** — Ensure `~/.zipline/extension.py` exists, contains `register_fonrex_bundle(...)`, and that the current interpreter can import `zipline_bundle`. When running from a checkout, prepend the repository path to `PYTHONPATH`.

**`sqlalchemy.exc.OperationalError: could not connect to server`** — The bundle uses the synchronous SQLAlchemy driver. Verify `DATABASE_URL` reaches the Postgres server and that credentials are valid.

**"no data for window"** warning — Ingest daily bars for the requested tickers via `POST /historical/ingest/bulk` before running `zipline ingest`.

**Row dropped: "no row aligned on the trading calendar"** — The ticker's exchange does not match the chosen `calendar_name`. Register a second bundle with the correct calendar (see [Trading calendars](#trading-calendars)).
