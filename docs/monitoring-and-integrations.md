# Provider Health Monitoring, Client Integrations and Zipline Bundle

This document details real-time provider outlier validation (`ValidationLayer`), daily canary checks (`CanaryMonitor`), client integrations (OpenBB Workspace, Google Sheets Connector, outbound proxy), and the `zipline-reloaded` backtesting bundle. See [ARCHITECTURE.md](../ARCHITECTURE.md) for the main routes and canary catalog table.

## Provider Health Monitoring

The monitoring system detects **silent data corruption** caused by CSS selector or HTML format changes in the providers queried by `/fundamental`, and keeps the suspect values out of the answers. The provider runner alone only sees hard failures (timeout, no answer), not numerically false values.

### Monitoring Architecture

```mermaid
flowchart TD
    subgraph "Real-Time Validation"
        Runner["FinancialProviderRunner.run()"] --> VL["ValidationLayer.validate_results()"]
        VL --> Range{"Range Check\nper field"}
        Range -->|"out_of_range"| Reject["field = None\n→ fallback active"]
        Range -->|"ok"| Consensus{"Consensus Check\ninter-provider median"}
        Consensus -->|"deviation > 50%"| Reject
        Consensus -->|"ok"| Keep["Value kept"]
        Reject --> Log["Batch INSERT\nprovider_health_log"]
        Keep --> Log
    end

    subgraph "Daily Canary"
        Sched["APScheduler\n06:00 UTC"] --> CM["CanaryMonitor.run_all()"]
        CM --> Import["Dynamic import\nmonitored providers"]
        Import --> Check["Canary assets\n× expected fields"]
        Check --> Daily["Upsert\nprovider_health_daily"]
        Check --> Alert["Create / resolve\nprovider_alerts"]
        Check --> Redis["Redis Cache\nprovider:health:summary"]
    end
```

### ValidationLayer (`monitoring/validation_layer.py`)

The `ValidationLayer` sits in the `FinancialProviderRunner` between collecting results and returning them to the caller.

**Range checks** — Each validatable field is checked against a `FIELD_RANGES` dictionary defining plausible bounds:

| Field | Min | Max | Notes |
| --- | --- | --- | --- |
| `pe_ratio` | 0.5 | 1 000 | P/E too low = parsing error |
| `dividend_yield` | 0.0 | 0.50 | As a ratio, not % |
| `beta` | -3.0 | 5.0 | Extremes rare but possible |
| `price` | 0.001 | 1 000 000 | Covers penny stocks and BRK-A |
| `roe`, `roa` | -5.0 / -2.0 | 10.0 / 2.0 | As a ratio |
| ... | ... | ... | see `FIELD_RANGES` for the other fields |

**Unit normalisation** — In `FIELD_RANGES` and `CANARY_ASSETS`, percentage and rate fields (`dividend_yield`, `roe`, `roa`…) are expressed as ratios, while scraped providers return them as displayed percentages (`3.45` for 3.45 %). Absolute fields such as `price`, `pe_ratio` or `beta` keep their own unit and are never converted. `monitoring/units.py` declares, per provider, which fields are percentages (`PROVIDER_PERCENT_FIELDS`); the `ValidationLayer` and the `CanaryMonitor` convert them with `to_ratio()` before any range, consensus or canary check. Logged percentage fields are the normalised ratios and absolute fields are logged as received; the provider payload returned by the API keeps its native unit.

**Cross-provider consensus** — For each field, the `ValidationLayer` calculates the median of valid values (within range) across all providers. If a provider deviates by more than `VALIDATION_OUTLIER_THRESHOLD` (default 50%) from the consensus, its value is reset to `None`. The minimum threshold of providers to calculate the consensus is configurable (`VALIDATION_MIN_PROVIDERS`, default 2).

**Logging** — All checks are batch recorded in `provider_health_log` through the monitoring repository (`database/monitoring.py`). The `ValidationLayer` never raises an exception: any internal error is logged and silently ignored so as not to impact the main flow.

### CanaryMonitor (`monitoring/canary_monitor.py`)

The `CanaryMonitor` runs daily targeted checks on a few "canary" assets (highly liquid, known and stable values) to detect structural provider failures.

**Canary assets:**

| Ticker | Fields checked | Example expected range |
| --- | --- | --- |
| `AAPL` | pe_ratio, dividend_yield, beta, price | pe: [20, 45], price: [100, 500] |
| `AIR.PA` | pe_ratio, dividend_yield, beta, price | pe: [15, 60], price: [80, 300] |
| `BNP.PA` | pe_ratio, dividend_yield, pb_ratio, price | pe: [4, 15], div: [0.04, 0.12] |
| `MSFT` | pe_ratio, beta, price | pe: [25, 50], price: [200, 600] |
| `TSLA` | pe_ratio, beta, price | pe: [30, 800], beta: [1.5, 3.5] |

The price range written in the catalogue is only a fallback: when the database holds at least ten daily closes of the last 90 days for the canary asset, the expected range is their mean ± 3 standard deviations (`monitoring/price_ranges.py`), kept in memory for `CANARY_PRICE_RANGE_TTL_SECONDS`.

**Dynamic imports** — To avoid circular imports between `monitoring/` and `financials/providers/`, the `CanaryMonitor` uses `importlib.import_module()` to load each provider class on the fly via the `_PROVIDER_IMPORTS` dictionary of `monitoring/canary_catalog.py`, which also holds the canary assets and their expected ranges.

**Compatibility** — EU-only providers (`Boursorama`, `Fortuneo`, `BourseDirect`, `InvestirLesEchos`) are only tested on EU tickers (`AIR.PA`, `BNP.PA`).

**Parallel execution** — Providers are tested in parallel, three at a time by default (`CANARY_PROVIDER_SEMAPHORE`), each provider call limited to 15 seconds and the whole run to 120 seconds.

**Daily aggregation** — After each run, the monitor upserts counters by status, success rate, and the `is_healthy` flag into `provider_health_daily`.

**Alert system** — The monitor creates alerts in `provider_alerts` with deduplication (no active duplicate of the same type for the same provider):

| Alert type | Condition | Severity |
| --- | --- | --- |
| `canary_failed` | Value out of expected range | `warning` (1-2 failures) / `critical` (3+) |
| `high_outlier_rate` | Success rate < threshold | `warning` (< 85%) / `critical` (< 70%) |

`canary_failed` alerts are **auto-resolved** when all canary checks pass on the next run.

**Redis Cache** — The global health summary is written to Redis (`provider:health:summary`, TTL 1h) to allow quick reads from the `GET /health/providers` endpoint without SQL queries.

### Monitoring Endpoints (`routers/monitoring.py`)

The endpoints are declared in a dedicated router, included in `main.py` via `app.include_router()`. Dependencies (`canary_monitor`, `async_session_factory`, `redis_client`) are resolved on each request from `app.state`.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `VALIDATION_OUTLIER_THRESHOLD` | `0.50` | Consensus deviation threshold (50%) |
| `VALIDATION_MIN_PROVIDERS` | `2` | Minimum providers to calculate consensus |
| `CANARY_RUN_HOUR` | `6` | Daily canary UTC hour |
| `CANARY_PROVIDER_SEMAPHORE` | `3` | Canary parallelism |
| `CANARY_PRICE_RANGE_TTL_SECONDS` | `21600` | Dynamic price range validity duration (6 h) |
| `CANARY_PRICE_RANGE_NEGATIVE_TTL_SECONDS` | `300` | Delay before retry after impossible range calculation (5 min) |
| `ALERT_CANARY_CRITICAL` | `3` | Number of canary failures → critical alert |
| `ALERT_SUCCESS_RATE_CRITICAL` | `0.70` | Success rate → critical alert |
| `ALERT_SUCCESS_RATE_WARNING` | `0.85` | Success rate → warning alert |
| `ASYNC_DATABASE_URL` | (inferred) | Explicit asyncpg URL (otherwise inferred from `DATABASE_URL`) |

## Integrations

Both integrations are clients of the user's own instance. Neither goes through a Fonrex-operated server.

### OpenBB Workspace

```mermaid
flowchart LR
    OBB[OpenBB Workspace] -->|"GET /widgets.json, /apps.json (no key)"| Main[main.py]
    OBB -->|"GET /openbb/... with X-API-KEY"| Router[routers/openbb.py]
    Router -->|calls the route function| Routes[Fonrex routes]
    Routes --> Router
    Router -->|reshapes| Adapters[integrations/openbb/adapters.py]
    Adapters --> OBB
```

- `integrations/openbb/widgets.json` declares the widgets (each one names the `/openbb/...` route that feeds it and its parameters) and `apps.json` the pre-assembled dashboards. `main.py` reads both at startup and serves them without authentication, because OpenBB Workspace fetches them before any key is configured.
- OpenBB expects three shapes: a list of tiles for a *metric* widget, a Plotly figure for a *chart*, a flat list of rows for a *table*. `routers/openbb.py` therefore does not duplicate any logic: each route calls the Python function of the Fonrex route it adapts — with every parameter passed explicitly, a FastAPI default being a `Query` object outside a request — and hands the answer to a pure function of `integrations/openbb/adapters.py`.
- The browser calls the instance from the OpenBB origin: CORS accepts `OPENBB_ALLOWED_ORIGIN` (default `https://pro.openbb.co`), and the key travels in the `X-API-KEY` header. A read-only key is enough for every widget.
- `tests/test_openbb_integration.py` checks that each widget points to an existing route, that the dashboards only reference existing widgets, and calls every `/openbb` route function with its declared parameters; `tests/test_openbb_adapters.py` covers the reshaping.
- User documentation: [integrations/openbb/README.md](../integrations/openbb/README.md).

### Google Sheets Connector

`fonrex-sheets-connector/` is a Google Apps Script (`Code.gs`) bound to a spreadsheet template; it is not part of the Python application and is not deployed with it.

- Google runs the script on its own servers, which cannot reach `localhost`: the user exposes the instance through a tunnel of their choice (zrok, Cloudflare Tunnel, Tailscale Funnel…) and gives the script that HTTPS URL and a key. Both are stored in the Apps Script *user properties* of the Google account, never in a cell.
- The script only sends `GET` requests, with `Authorization: Bearer <key>`, to three routes: `/fundamental/deep?ticker=…`, `/dcf/{ticker}` and `/technical/{ticker}/multi?indicators=…` (menu refreshes), plus `/fundamental?ticker=…` for two of the four custom formulas. A key of `FONREX_READ_ONLY_API_KEYS` is what it should be given: it cannot clear the cache, clean the database, ingest or subscribe.
- The manifest (`appsscript.json`) has no fixed list of callable hosts, since the URL is the user's; `tests/test_docs_consistency.py` fails if a hosted API host comes back in the script or the documents.
- User documentation: [fonrex-sheets-connector/README.md](../fonrex-sheets-connector/README.md).

### Outbound Relay (not part of this repository)

Sites increasingly refuse direct requests from a personal connection. The planned answer, Fonrex Relay, is a separate project still in development: a relay of pages that an instance may route its provider requests through. It neither stores nor serves financial data, and Fonrex does not depend on it. The only part that lives here is the generic outbound proxy setting of the provider HTTP layer (`FONREX_PROXY_URL`, optionally limited to some providers with `FONREX_PROXY_PROVIDERS`), which works with any HTTP proxy.

## Zipline Bundle (Backtesting Integration)

The `zipline_bundle/` package exposes Fonrex historical data (`prices_eod` at daily resolution) to [`zipline-reloaded`](https://github.com/stefan-jansen/zipline-reloaded) without intermediary CSV exports.

- **Runtime boundary**: the FastAPI runtime **never** imports `zipline` (`zipline-reloaded` stays out of `requirements.txt`), and `zipline_bundle/data_source.py` only depends on `pandas` and `sqlalchemy` so the CI can test extraction in isolation.
- **Full guide**: prerequisites, ingestion flow, design decisions (`sid` allocation, `adj_close` precedence, session alignment), CLI (`preview` / `ingest`), and configuration variables are documented in [docs/zipline-bundle.md](zipline-bundle.md).
