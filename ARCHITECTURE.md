# Fonrex — Technical Architecture

This document describes the architecture observed in the source code. Its tables (routes, migrations, modules, cache lifetimes, canary assets) are compared with the code by `tests/test_docs_consistency.py`: a route, a migration or a router that is added without being described here fails the quality gate. The history of the changes is in the git log, not in this file.

Fonrex is a self-hosted FastAPI API that aggregates market data, fundamentals, asset metadata and financial news. It combines PostgreSQL/TimescaleDB, Redis, yfinance, asynchronous web providers, an ISIN-based CSV import, a multi-source news aggregation engine and an automated provider health monitoring system.

**Scope.** Fonrex runs on the machine of the person who uses it: the instance fetches public pages for its owner and keeps the result in its own database. There is no Fonrex-operated data API, no account and no billing in this code base. Two consequences shape the architecture: authentication protects a personal instance (a few keys set in `.env`, see [Startup](#startup)), and every client — OpenBB Workspace, the Google Sheets connector — talks to the user's own instance (see [Integrations](docs/monitoring-and-integrations.md#integrations)).

## Overview

```mermaid
flowchart TD
    subgraph Client Space
        Client[HTTP / WS Client]
        OpenBB[OpenBB Workspace]
        Sheets[Google Sheets connector]
    end

    subgraph API [FastAPI Service]
        Main[main.py + routers]
        WS[WebSocket handler - routers/realtime.py]
        CM[ConnectionManager]
        Worker[RealtimePriceWorker]
        VL[ValidationLayer]
        Canary[CanaryMonitor]
    end

    subgraph External Sources
        TV[TradingView WebSocket]
        YF[yfinance / Yahoo Finance]
        Providers[Fundamental Providers]
    end

    subgraph Storage
        Redis[(Redis Cache & Pub/Sub)]
        DB[(PostgreSQL / TimescaleDB)]
    end

    Client -- REST HTTP --> Main
    Client -- WebSocket --> WS
    OpenBB -- "/widgets.json, /openbb/*" --> Main
    Sheets -- "GET through a tunnel" --> Main
    WS -- Subscribes to the price channel --> Redis
    WS -- Registers the connection --> CM
    
    Worker -- Streams ticks --> TV
    Worker -- Cache & Publish --> Redis
    Worker -- Persist 1min candles --> DB
    
    Main --> Redis
    Main --> DB
    Main --> YF
    Main --> Providers
    Main --> NS

    VL --> DB
    Canary --> Providers
    Canary --> DB
    Canary --> Redis

    subgraph News [NewsService]
        NS[news_service.py]
        NP1[YFinance News]
        NP2[Google Finance News]
        NP3[ZoneBourse News]
        NP4[Boursorama News]
        NP5[Investing.com News]
        NP6[MarketWatch News]
        NP7[MSN Finance News]
        NS --> NP1
        NS --> NP2
        NS --> NP3
        NS --> NP4
        NS --> NP5
        NS --> NP6
        NS --> NP7
    end

    NS --> Redis
    NS --> DB
```

The Docker runtime (`docker-compose.yml`) consists of four services:

- `fonrex-api`: Python 3.12 image running as a non-root user, FastAPI served by Gunicorn with Uvicorn workers, published on port `5000` of the host (all interfaces: the API key is what protects it on a shared network).
  It loads `.env`, written for a run outside Docker (`localhost`): `docker-compose.yml` overrides `DATABASE_URL` and `REDIS_URL` with the addresses of the `db` and `redis` services and empties `ASYNC_DATABASE_URL`, which is then derived from that `DATABASE_URL`. `tests/test_docker_image.py` fails if an address of `.env.example` is not overridden.
- `db` (container `fonrex-db`): `timescale/timescaledb-ha:pg16` image, target database `fonrex`, created by `postgres-init.sh`. Its files are stored in the `timescale_data` volume, mounted on the data directory of that image (`PGDATA=/home/postgres/pgdata/data`, not the `/var/lib/postgresql/data` of the official postgres image); `tests/test_docker_image.py` fails if the volume is mounted anywhere else.
- `redis` (container `fonrex-redis`): Redis 7 without password, application cache with `allkeys-lru` policy, 256 MB memory limit.
- `fonrex-migrate`: Alembic migration container (`migrate` Docker profile), executes `alembic upgrade head` in isolation outside the API lifecycle.

The API runs **one** Gunicorn worker by default (`WEB_CONCURRENCY=1`). The realtime worker, the canary scheduler and the usage recorder live inside the API process: each additional worker would open its own TradingView streams and run its own daily canary.

The API image is self-contained: it holds the code, the Alembic migrations and the seed files (`data/*.csv`). Compose only mounts what the application writes at runtime (`./logs`, `./static/logos`); `docker-compose.dev.yml` adds the `.:/app` mount for development. The image installs the exact package versions of `requirements.lock`, each checked against its hash.

## Startup

The Docker startup goes through `entrypoint.sh`:

1. Waits for PostgreSQL at the host and port of `DATABASE_URL` (`db:5432` in Compose, and when the URL gives no host; `database_address()` of `database/lifecycle.py`); the start-up is aborted if it never answers.
2. Waits for Redis on `redis:6379`; an unreachable Redis only disables the cache.
3. Applies database migrations via Alembic (`alembic upgrade head`).
4. With `SEED_ON_FIRST_RUN=true` and an empty `assets` table, imports `data/etf.csv`.
5. Launches `gunicorn --workers $WEB_CONCURRENCY --worker-class uvicorn.workers.UvicornWorker main:app`.

`docker-compose.yml` loads `.env` into the API container (`env_file`), overrides `DATABASE_URL` and `REDIS_URL` to target the Compose services, sets `WEB_CONCURRENCY` (default 1) and clears the system proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`). PostgreSQL and Redis are published on `127.0.0.1` only.

**External database.** To use a PostgreSQL server outside Compose (it needs the TimescaleDB extension, as the `db` image provides), keep `docker-compose.yml` as it is and put the address in `docker-compose.override.yml`, which `docker compose` loads on its own. The override points both services that reach the database to it, removes their dependency on `db`, and puts `db` behind a profile so that it no longer starts:

```yaml
services:
  db:
    profiles: [local-db]
  fonrex-api:
    environment:
      - DATABASE_URL=postgresql://fonrex:${POSTGRES_PASSWORD}@db.example.com:5432/fonrex
    depends_on: !override
      redis:
        condition: service_healthy
  fonrex-migrate:
    environment:
      - DATABASE_URL=postgresql://fonrex:${POSTGRES_PASSWORD}@db.example.com:5432/fonrex
    depends_on: !reset []
```

The `!override` and `!reset` tags need Docker Compose v2.24 or later; an older version rejects the file with a YAML error. The entrypoint then waits for `db.example.com:5432` instead of `db:5432`. With `db` stopped, the backup command of the README (`docker compose exec -T db pg_dump ...`) no longer applies: run `pg_dump` against the external server instead, for example `pg_dump -h db.example.com -U fonrex -d fonrex -Fc > fonrex.dump`.

Authentication is enforced by default (`auth/dependencies.py`): protected routes require a key listed in `FONREX_API_KEY`, `FONREX_RELAY_KEY` or `FONREX_API_KEYS` (full access), or in `FONREX_READ_ONLY_API_KEYS` (reads only: `GET` routes plus the two computation routes `POST /technical/batch` and `POST /dcf/{ticker}`; meant for clients holding the key outside the host, such as the Sheets connector). With no key configured every protected request is rejected (fail closed); the only opt-out is `FONREX_AUTH_REQUIRED=false` with no key configured. `main.py` logs the effective mode at startup.

A key is sent as `Authorization: Bearer <key>` or `X-API-KEY: <key>` (the custom header of OpenBB Workspace); the WebSocket route also accepts it as a query parameter (`api_key`, `token` or `key`) and checks it during the handshake, because HTTP middlewares do not see WebSocket connections. The paths that answer without a key (`PUBLIC_PATHS` in `main.py`) are `/health`, `/docs`, `/redoc`, `/openapi.json`, `/widgets.json`, `/apps.json`, `/favicon.ico` and `/static/*`. Because the key is checked by a middleware, `main.py` declares both headers as security schemes in the OpenAPI document, and marks the operations of the public paths as needing none, so that the *Authorize* button of Swagger UI (`/docs`) sends the key with each *Try it out* call; Swagger UI keeps it across page reloads. A read-only key never starts a TradingView stream: `GET /quote/{ticker}`, `GET /openbb/quote/{ticker}` and the WebSocket serve what is already streamed, and the WebSocket sends a `not_streaming` message when the ticker is not. Cross-origin requests are accepted from the origins listed in `OPENBB_ALLOWED_ORIGIN` only (default `https://pro.openbb.co`).

At application startup, `main.py` initializes:

- `AsyncDatabaseResources`: the asynchronous engine and session factory shared by the history queries, the realtime worker, the news and the monitoring.
- `DatabaseService` for synchronous ORM accesses.
- `QueryService` for asynchronous historical queries.
- `UsageRecorder`, the background writer of the usage log.
- An asynchronous Redis client (history, technical, DCF, news and fundamentals caches, realtime pub/sub).
- `CacheService`, the synchronous Redis abstraction (EOD answers, deep fundamentals, specialised providers).
- `FinancialsAggregator` for the legacy `/stocks` routes.
- `HistoricalIngestionService` for EOD ingestion endpoints.
- `TechnicalIndicatorService` for on-the-fly indicator calculations.
- `RealtimePriceWorker` which restores all active subscriptions from `realtime_subscriptions`.
- `NewsService` for `/news` routes, initialized with the shared async SQLAlchemy session factory and async Redis client.
- `FREDService` for macro-economic series extraction (e.g. risk-free rate).
- `ECBService` for euro area rates from the ECB Data Portal (euro risk-free rate, deposit facility rate, systemic stress).
- `DCFService` for the valuation routes.
- `ValidationLayer` for real-time validation of values returned by providers, and `CanaryMonitor` for daily provider health checks, both over the same `SqlAlchemyMonitoringRepository`.
- `AsyncIOScheduler` (APScheduler) to schedule the daily canary check execution (default 06:00 UTC, configurable via `CANARY_RUN_HOUR`).
- The OpenBB Workspace definitions (`integrations/openbb/widgets.json` and `apps.json`), read once.
- All these objects are published in `app.state` and resolved by FastAPI dependencies; no mutable service is kept in a global module variable.

The providers need no connection and are set up when `main.py` is imported: the classes of `PROVIDER_SPECS` are kept in a registry and instantiated by the runner for each request, the four providers of `SPECIALIZED_PROVIDER_SPECS` are instantiated once. A provider that cannot be imported is left out, and `GET /health` lists it under `providers.unavailable`. The start is tolerant: without an asynchronous database URL the realtime worker, the news service and the monitoring are not created; the worker, news, FRED, DCF and monitoring are each started on their own and leave an empty slot on failure. The routes that need a missing service answer `503`; the rest of the API starts.

`main.py` never creates or modifies the schema. On startup, it checks the connection and then compares the stored revision in `alembic_version` with the expected head. An unmigrated or unreachable database is declared unavailable (`db_available = False`) and the routes that need it answer `503`; the Docker entrypoint applies `alembic upgrade head` beforehand.

## Layers

The code is organised by feature (one package per domain) and, inside a feature, in three levels:

1. **HTTP adapters** (`routers/`): parse the request, call the level below, translate errors into HTTP statuses.
2. **Application logic**: use cases (`use_cases/`) or feature services (`historical/`, `technical/`, `news/`, `valuation/`, `monitoring/`, `macro/`).
3. **Adapters to the outside**: SQLAlchemy repositories (`database/`), Redis (`cache/`), providers (`financials/providers/`, `news/providers/`, `historical/providers.py`).

How far each feature follows this split today:

| Feature | Router | Application logic | Depends on abstractions (ports)? |
| --- | --- | --- | --- |
| Fundamentals | `routers/fundamentals.py` | `use_cases/fundamentals.py` | Yes — `use_cases/ports.py`; the router is the composition root |
| Specialised providers | `routers/specialized.py` | `use_cases/specialized.py` | Yes |
| Realtime | `routers/realtime.py` | `use_cases/realtime.py` | Partly — the WebSocket protocol is handled in the router |
| Technical indicators | `routers/technical.py` | `technical/indicator_service.py` | Yes — `technical/contracts.py`; the screener is written in the router |
| Monitoring | `routers/monitoring.py` | `monitoring/` | Yes — `monitoring/ports.py`; the read queries of the routes are written in the router |
| History and EOD | `routers/historical.py`, `routers/assets.py` | `historical/ingestion_service.py`, `database/query.py` | No — the routers call the services and build the cache keys |
| Valuation | `routers/valuation.py` | `valuation/dcf_service.py` | No — the service reads the ORM models directly |
| News | `routers/news.py` | `news/news_service.py` | No — the service holds SQL, cache and provider calls |
| Macro | `routers/macro.py` | `macro/fred_service.py`, `macro/ecb_service.py` | No — each service has its own HTTP client |
| Commitments of Traders | `routers/cot.py` | `macro/cot_reports.py` | No — the router calls the service directly |
| Factors | `routers/factors.py` | `factors/store.py`, `factors/french_library.py`, `factors/exposure.py` | No — the router calls the factor library directly |
| Operations | `routers/admin.py` | `database/maintenance.py`, `cache/service.py` | No |
| OpenBB | `routers/openbb.py` | calls the route functions of the other routers | — |

The use-case layer is therefore the target model, reached by three routers; the other features are services called directly. Architecture tests hold the split where it exists (`tests/test_exception_boundaries.py`): the `technical/` package imports neither FastAPI, SQLAlchemy, Redis nor the ORM models, and the `monitoring/` package neither SQLAlchemy nor the ORM models; the three port modules stay typed; the persistence and cache modules and the routers listed in that test use typed exceptions; four orchestrators have a size limit. In `main.py`, the routers, the use cases and the feature packages scanned by `tests/test_async_boundary.py`, blocking code is reached through `concurrency.run_sync()` only.

## Topic Documentation

Detailed domain designs live in `docs/` (loaded on demand so this file stays focused on the system map and machine-checked tables):

- [docs/data-model.md](docs/data-model.md) — Relational & TimescaleDB schema (`models.py`), constraints, indexes, and identity/listing resolution rules.
- [docs/historical-and-import.md](docs/historical-and-import.md) — CSV asset import (`import_assets.py`), duplicate cleanup scripts, EOD ingestion pipeline, TradingView real-time streaming, and technical indicators.
- [docs/fundamentals-and-providers.md](docs/fundamentals-and-providers.md) — `/fundamental`, `/fundamental/deep`, specialized endpoints, web providers, and financial news aggregation.
- [docs/macro-factors-valuation.md](docs/macro-factors-valuation.md) — Kenneth French factor returns, DCF/WACC valuation, FRED/ECB/Eurostat/Treasury/CFTC macro series, and SEC/ESEF XBRL filings.
- [docs/monitoring-and-integrations.md](docs/monitoring-and-integrations.md) — `ValidationLayer`, `CanaryMonitor`, OpenBB Workspace, Google Sheets connector, outbound relay, and Zipline bundle ([docs/zipline-bundle.md](docs/zipline-bundle.md)).
- [docs/testing-and-quality.md](docs/testing-and-quality.md) — Test suite isolation, `make ci` quality gate, thematic test catalog, and module health scores.

## Module Map

| Module | Responsibility |
| --- | --- |
| `main.py` | FastAPI composition root: service lifecycle, provider registry, authentication and usage log middlewares, router mounting, and the two OpenBB discovery files (`/widgets.json`, `/apps.json`). |
| `auth/dependencies.py` | API keys of the instance: where a key is read in a request, full-access and read-only keys, what a read-only key may call, the default (enforced) and the opt-out. |
| `settings.py` | Reads tunable settings from the environment (`env_int`, `env_decimal`, `env_choice`): an unset, unreadable or out-of-range value falls back to the default instead of stopping the API. |
| `usage_recorder.py` | In-memory queue and background writer of the usage log; IP storage mode and retention. |
| `documentation.py` | The JSON description of the API returned by `GET /`. |
| `routers/admin.py` | Health, cache and database administration routes. |
| `routers/openbb.py` | OpenBB Workspace routes (`/openbb/*`): call the route functions of the other routers and reshape their answers into the metric, chart and table contracts of the widgets. |
| `integrations/openbb/adapters.py` | Pure functions turning a Fonrex answer into OpenBB metric tiles, Plotly figures or flat table rows. |
| `routers/news.py` | HTTP news routes, with `NewsService` injection via `app.state`. |
| `routers/valuation.py` | DCF, comparison, and sensitivity HTTP routes, with Redis cache and `DCFService` injection. |
| `routers/technical.py` | HTTP adapter for indicators, charts, batch, and screener; translates technical business errors into HTTP statuses and defers synchronous SQL accesses outside the event loop. |
| `routers/historical.py` | Individual/bulk ingestion and historical consultation routes, with asynchronous Redis cache. |
| `routers/assets.py` | Asset identity, listings, and EOD routes; orchestrates reading, auto-ingestion, and JSON/CSV formats. |
| `routers/fundamentals.py` | HTTP adapter and fundamentals composition root; assembles provider implementations, formatters, and enrichers behind application ports. |
| `routers/specialized.py` | HTTP adapters for SEC EDGAR, JustETF, and index constituents use cases. |
| `routers/realtime.py` | REST adapters for realtime use cases and WebSocket protocol management. |
| `routers/dependencies.py` | Common FastAPI dependencies resolving services from `app.state`. |
| `routers/errors.py` | Translates transport-independent application errors into HTTP responses. |
| `use_cases/fundamentals.py` | Fundamentals collection and read use cases; orchestrates only application ports, without FastAPI, SQLAlchemy, ORM model, yfinance, or concrete provider imports. |
| `use_cases/ports.py` | Structural contracts owned by the application layer for persistence, cache, providers, formatting, and fundamental enrichments. |
| `use_cases/specialized.py` | SEC EDGAR, JustETF, and indices use cases, with validation, cache, and application errors. |
| `use_cases/realtime.py` | Realtime quote, multi-quotes, subscription, unsubscription, and status use cases. |
| `use_cases/errors.py` | Application error vocabulary without FastAPI dependency (`InvalidInput`, `ResourceNotFound`, `DependencyUnavailable`, `UpstreamFailure`). |
| `concurrency.py` | Boundary between asynchronous orchestration and blocking adapters, via `run_sync()` and the standard worker pool; the only other pool is the dedicated TradingView stream pool of `realtime/worker.py`. |
| `models.py` | Central ORM models: instruments, listings, provider mappings, EOD and intraday prices, realtime subscriptions, fundamentals (highlights, statements, earnings, ratings, ESG, trends, outstanding shares), ETFs, news articles, provider health logs/aggregates/alerts, macro rate cache, macro-economic series by period (Eurostat), facts of the XBRL filings of the SEC, daily yield curves of the US Treasury, weekly Commitments of Traders reports of the CFTC, ESEF filings of European listed companies and their facts, factor returns, euro exchange rates, usage and ingestion logs. |
| `database/service.py` | Compatibility facade and SQLAlchemy composition root; owns the engine/session factory and delegates to specialized components. |
| `database/assets.py` | Asset identity/listing, mappings, and profile enrichment repository. |
| `database/fundamentals.py` | Standard and deep fundamentals read repository. |
| `database/maintenance.py` | Statistics, inventory of the tickers holding prices, and the bounded cleanup of old prices and logs (`MIN_DAYS_TO_KEEP`, dry run). |
| `database/usage.py` | API usage events persistence. |
| `database/migrations.py` | Read-only verification of the current revision against the Alembic head. |
| `database/component.py` | Shared foundation giving components short and explicit access to synchronous sessions. |
| `database/price_series.py` | The one rule that turns a ticker into a price series (instrument + listing), and the session-date convention of `prices_eod.time`. Used by the API paths that read or write a price series (ingestion, history, EOD, technical indicators, canary price ranges); the Zipline bundle ranks listings with its own query. |
| `database/isin.py` | `clean_yahoo_isin`: the ISIN field of a Yahoo answer, `None` for a placeholder (`-`, `N/A`…). Shared by the enrichment, the import and the profile write, which refuses metadata whose ISIN is another instrument's. |
| `database/ticker_suffix.py` | The places named by Yahoo suffixes (`.PA`, `.DE`, `.L`…): exchange codes and currencies. A ticker looked up without its suffix designates a listing on that place only. |
| `database/query.py` | Async queries for the history of a listing from `prices_eod` or the `prices_weekly` / `prices_monthly` aggregates. |
| `database/schemas.py` | Internal Pydantic schemas for database entities (e.g., `PriceEOD`). |
| `historical/ingestion_service.py` | Orchestration of an ingestion: listing, verified source symbol, missing range, fetch, write, cache invalidation, log. |
| `historical/yahoo_symbols.py` | Finds the Yahoo symbol of a listing from the ISIN of its instrument, verifies it (currency of the listing, existing price) and remembers it in the `YahooFinance` mapping; a listing without a verified symbol is not ingested. |
| `historical/price_writer.py` | Upsert of end-of-day bars on `(asset_listing_id, resolution, time)`, with replacement of a range on forced refresh. |
| `realtime/worker.py` | `RealtimePriceWorker`: lifecycle, TradingView streaming, Redis pub/sub, and intraday persistence. |
| `realtime/connection_manager.py` | WebSocket client groups and realtime message broadcasting, independent of the market worker. |
| `schemas/historical.py` | Pydantic v2 schemas for historical ingestion (resolutions, sources, request/response structures). |
| `schemas/realtime.py` | Pydantic v2 schemas for realtime ticks, quote snapshots, and WebSocket messages. |
| `schemas/fundamentals.py` | Pydantic v2 schemas for financial highlights, financial statements, and earnings history. |
| `schemas/technical.py` | Pydantic v2 schemas for time series and technical indicator results. |
| `import_assets.py` | ISIN-deduplicated CSV import pipeline: `parse_csv` → `AssetImporter` (grouping by ISIN, `_upsert_asset` / `_upsert_listing` / `_create_default_mappings`); Yahoo/yfinance enrichment and logos in kept legacy functions. |
| `scripts/clean_isin_duplicates.py` | Standalone diagnostic and ISIN duplicate cleanup tool: `diagnose_duplicates`, `clean_duplicates` (re-parenting → deletion), `create_unique_index`; CLI with `--dry-run`, `--create-index`, `--isin`, `--diagnose-only`. |
| `scripts/clean_listing_duplicates.py` | Removes the listings that a CSV re-import duplicated after the enrichment filled the exchange: `find_duplicates`, `clean_duplicates`; CLI with `--dry-run`, `--isin`. A duplicate holding prices is reported and kept. |
| `scripts/ingest_all.py` | Administration CLI to trigger global historical ingestion of all database assets. |
| `scripts/seed_database.py` | Imports the default catalogue, then optionally enriches it and ingests daily prices (`make db-seed`). |
| `scripts/check_coverage_distribution.py` | Coverage floor of each module, checked by `make ci`, and the coverage summary published by the CI. |
| `scripts/example_realtime_client.py` | Example client of the realtime REST and WebSocket routes. |
| `fonrex-sheets-connector/Code.gs` | Google Apps Script of the spreadsheet template: reads fundamentals, DCF and indicators from the user's instance. |
| `financials/provider_runner.py` | Parallel orchestration of fundamental providers with mappings, timeouts, `raw_providers`, and optional injection of `ValidationLayer` for real-time outlier filtering. |
| `financials/enrichment/adapters.py` | Concrete yfinance adapters and ticker normalization injected into use cases by the HTTP router. |
| `financials/enrichment/yfinance_enricher.py` | Deep enrichment from Yahoo: fills the blank profile fields of the asset (`profile_metadata`, shared with `fetch_yfinance_data`), writes highlights, financial statements, earnings history, analyst ratings, ESG scores, earnings trend and shares history. |
| `financials/providers/` | Asynchronous connectors to ZoneBourse, Google Finance, Boursorama, Barrons, WSJ, MarketWatch, MorningStar, Investing, Gurufocus, Fortuneo, BourseDirect, MSN, Investir Les Echos, YahooFinance, JustETF, SECEdgar (insider transactions, the tickers of the SEC and the XBRL facts of a company), OpenFIGI, IndexConstituents, Treasury (the daily yield curves of the US Treasury), CFTC (the weekly Commitments of Traders reports: Legacy, Traders in Financial Futures and Disaggregated), and ESEF (the LEI of an ISIN from GLEIF, the ESEF filings of a LEI and their numbers from filings.xbrl.org). |
| `financials/providers/base.py` | The one HTTP layer of every provider: client creation, retries, per-provider concurrency, optional outbound proxy. |
| `financials/providers/JustETF_provider.py` | Legacy synchronous justETF scraper, kept and hardened; it relies on `fundamental/tools/ToolsBox.py`. The API uses `financials/providers/justetf.py`. |
| `fundamental/tools/ToolsBox.py` | Legacy helpers still used by the legacy justETF scraper and by the Google Finance to Yahoo ticker conversion. |
| `financials/numbers.py` | The one reader of numbers displayed on a page (`parse_number`, `find_number`). |
| `financials/sec_cik.py` | The CIK of an instrument at the SEC, verified and never guessed: looked up only through a US listing of the catalogue (USD, US exchange, US ticker), confirmed by the company name of the SEC table, kept in the `SEC` mapping of the instrument. |
| `financials/esef_lei.py` | The Legal Entity Identifier of an instrument, under which its ESEF filings are kept: asked to GLEIF with the ISIN of the instrument (never a ticker), confirmed by the name of the entity (its legal name or another name, the legal forms left out), kept in the `LEI` mapping of the instrument. |
| `financials/xbrl_mapping.py` | From the XBRL facts of the SEC and of the ESEF filings to annual statements: the concepts (US GAAP, then IFRS) that fill each column of `financial_statements`, one value per period (the latest filing), fiscal years of 350 to 380 days from the annual reports, read in the currency of the latest report. |
| `financials/sec_statements.py` | The annual statements filed with the SEC for an instrument whose CIK is verified: the facts of the company read from `companyfacts`, kept in `xbrl_facts` (a restatement replaces the first figure, an older filing never does), read again after `SEC_XBRL_REFRESH_HOURS`. |
| `financials/esef_statements.py` | The annual statements of the ESEF filings for an instrument whose LEI is verified: the filings of the company listed from filings.xbrl.org into `esef_filings`, one filing read per period of the index (the one processed last, so one language and the latest version), each filing read once (again only when its file changed), its totals kept in `esef_facts` (a restatement replaces the first figure, an older filing never does), the true period of a filing taken from its facts; read as annual statements by `financials/xbrl_mapping.py` (fiscal years and balances only, half-years and quarters stored but left out); the index read again after `ESEF_REFRESH_HOURS`, a filing set to be read again read before that. |
| `financials/sec_check.py` | Checks the annual statements stored from Yahoo against the SEC filings (and, with the same rules, against the ESEF filings), fiscal year by fiscal year: years matched by their end (within two weeks), payments compared as amounts, `SEC_CHECK_TOLERANCE` (1 %) as the agreement; cash and equity, defined differently by the two sources, are not compared; another currency stops the comparison of a year. The ESEF check also leaves out the operating income, the interest expense and the capex (`ESEF_NOT_COMPARED`). |
| `financials/fiscal_years.py` | Puts the statement rows of a fiscal year together (`fiscal_years`): the one way the valuation and the solvency ratios read `financial_statements`. |
| `financials/models.py` | Pydantic models returned by the providers (`StandardFinancials`, `FinancialMetrics`, `StockSummary`). |
| `financials/router.py` | Legacy `/stocks` routes. |
| `financials/service.py` | Separate aggregator for `/stocks/{ticker}/financials`. |
| `financials/formatter.py` | Builds the fundamentals document in the EODHD-compatible layout (`FinancialsFormatter.to_eodhd`): picks each value from Yahoo, the stored figures or a like-for-like scraped field, and records its origin in the `Sources` section. |
| `financials/exchange.py` | Structured management and mapping of financial exchange codes for GuruFocus, Yahoo Finance, and Google Finance. |
| `cache/service.py` | Synchronous Redis abstraction, TTL per category, JSON serialization. An entry that is not JSON is a miss: nothing read from Redis is unpickled. |
| `cache/adapters.py` | Best-effort asynchronous JSON cache handed to the fundamentals use case: Redis failures are absorbed, programming errors are not. |
| `database/lifecycle.py` | Builds and closes the asynchronous engine and session factory; infers `ASYNC_DATABASE_URL` from `DATABASE_URL`. |
| `valuation/dcf_service.py` | Financial valuation service implementing DCF models (FCF, EPS, DDM), on-the-fly WACC calculation (CAPM cost of equity and cost of debt), and sensitivity matrix generation. |
| `schemas/dcf.py` | Pydantic v2 schemas for custom DCF and WACC calculation requests, as well as detailed responses and sensitivity matrices. |
| `schemas/cot.py` | Pydantic v2 schemas of the `/cot` routes: markets, positions, signals, extremes and the reading of the CFTC. |
| `technical/indicator_service.py` | Business orchestrator independent of FastAPI for simple and multiple calculations; delegates calculation, persistence, and cache to targeted components. |
| `technical/catalog.py` | Declarative and typed registry of 18 indicators, default parameters, and TTL policies. |
| `technical/contracts.py` | Independent ports for market data, cache, WebSockets, and technical parameters. |
| `technical/calculation_engine.py` | pandas/pandas-ta engine without I/O: calculation, period validation, and conversion to business series. |
| `database/technical.py` | SQLAlchemy adapter of the technical port: resolves a ticker to its series and reads its OHLCV (per listing for end-of-day prices, per instrument for intraday). |
| `cache/technical.py` | Redis adapter for technical result serialization and caching. |
| `technical/errors.py` | Transport-independent business errors for invalid indicators, incompatible resolutions, and missing or insufficient data. |
| `news/news_service.py` | Main news orchestrator: parallel fetch of the news providers, URL + title similarity deduplication (`difflib`), upsert `ON CONFLICT` in `news_articles`, Redis 30 min cache, language filter, global feed from DB. |
| `news/providers/yfinance_news.py` | News via yfinance `ticker.news` (native JSON, epoch → UTC datetime, `run_sync()`). |
| `news/providers/google_finance_news.py` | Google Finance news: JSON embedded in HTML + BeautifulSoup fallback, exchange resolution (`.PA` → `EPA`, `.DE` → `ETR`, etc.), relative dates. |
| `news/providers/zonebourse_news.py` | ZoneBourse news (FR): HTML BeautifulSoup, French dates, `fr` language. |
| `news/providers/boursorama_news.py` | Boursorama news (FR): HTML BeautifulSoup, `<time datetime>` ISO, `fr` language. |
| `news/providers/investing_news.py` | Investing.com news: browser-like headers, Cloudflare challenge page detection, `-news` slug. |
| `news/providers/marketwatch_news.py` | MarketWatch news: `countrycode` query param for EU tickers, `<time dateTime>`. |
| `news/providers/msn_finance_news.py` | MSN Finance news: internal JSON endpoint + HTML fallback. |
| `macro/__init__.py` | Macro package marker (the service is imported from `macro.fred_service`). |
| `macro/rate_cache.py` | Cache shared by the macro sources: Redis, then the stored value when it is recent, then the source, the stored value being the fallback (`stale`). |
| `macro/fred_service.py` | Reads FRED series (the US risk-free rate `DGS10`) through `macro/rate_cache.py`. |
| `macro/ecb_service.py` | Reads ECB Data Portal series in CSV (euro AAA 10-year spot rate, deposit facility rate, CISS) through `macro/rate_cache.py`; rates published in percent are stored as ratios. |
| `macro/fx_rates.py` | ECB reference rates of the euro kept day by day in `fx_rates` (asynchronous session), refreshed from the days not stored yet; conversion between two currencies through the euro. |
| `macro/treasury_curves.py` | The daily yield curves of the US Treasury (par curve since 1990, real curve since 2003) kept day by day in `yield_curve_points` (asynchronous session): a year is read only when a request needs it, a past year read after its end is never read again, the current year is read again after `TREASURY_REFRESH_HOURS`; the curve of a day (or of the business day before) and the history of some maturities. |
| `macro/cot_reports.py` | The Commitments of Traders reports of the CFTC (Legacy since 1986, TFF and Disaggregated since June 2006) kept week by week in `cot_weeks` and `cot_positions` (asynchronous session): a year is read only when a request needs it, the years up to 2016 come together from the history file of their report and are never read again, a later past year read from 15 January of the next year on is never read again, the current year is read again after `COT_REFRESH_HOURS`; the markets stored with their latest name, the lines of a market, and its signals (net position, share of the open interest, weekly change, COT index over a window of weeks, estimated day of publication). |
| `macro/eurostat_service.py` | Indicators of Eurostat read in SDMX-CSV (HICP inflation and core inflation, unemployment rate, GDP growth, economic sentiment, consumer confidence), by country, kept period by period in `macro_observations` (asynchronous session); a refresh asks again for the last periods stored to take the revisions. |
| `factors/french_library.py` | Catalogue of the Kenneth French Data Library datasets (US, Europe, developed markets; 3 factors, 5 factors, momentum; monthly and daily), download of a zipped file and parsing of its CSV into ratios. |
| `factors/store.py` | Factor returns stored in `factor_returns` (asynchronous session): a file is stored whole, replacing the previous one, and not downloaded again before `FACTORS_REFRESH_DAYS`. |
| `factors/exposure.py` | Factor exposure of a listing: daily closes of the listing (`adj_close`), converted in US dollars with the ECB rates, monthly or daily returns, alignment with the factors and ordinary least squares (numpy). |
| `factors/cost_of_equity.py` | Cost of equity from the Fama/French factors for the valuation: betas of the listing and long-run premia of the factors of its region. |
| `scripts/load_factors.py` | Command line download of the factor files into the database. |
| `scripts/load_fx_rates.py` | Command line download of the ECB reference rates of the euro into the database. |
| `scripts/load_treasury_curve.py` | Command line download of the daily yield curves of the US Treasury into the database (both curves, the whole history by default). |
| `scripts/load_cot.py` | Command line download of the Commitments of Traders reports of the CFTC into the database (the three reports, the whole history by default). |
| `scripts/load_esef.py` | Command line reading of the ESEF annual reports of the shares of the catalogue (every active share with an ISIN, funds left out, or the tickers given): the LEI asked to GLEIF, the filings never read read, one line per share; an error on one share is printed and the next shares are read; after 20 errors in a row (the database down) the reading stops. |
| `schemas/news.py` | Pydantic v2 schemas for the news system: `RawNewsItem`, `NewsArticleSchema`, `NewsResponse`, `NewsFeedResponse`, `NewsLanguage` / `NewsSentiment` enums. |
| `schemas/macro.py` | Pydantic v2 schemas for the macro-economic rates and for the Eurostat indicators (latest values, series, refresh). |
| `schemas/sec.py` | Pydantic v2 schemas of the SEC routes: statements as filed, the check against Yahoo, the refresh. |
| `schemas/esef.py` | Pydantic v2 schemas of the ESEF routes: statements as filed with the link of their filing, the check against Yahoo, the filings of the index and their reading, the refresh. |
| `schemas/factors.py` | Pydantic v2 schemas of the factor routes: datasets, what is stored of a file, series of factor returns. |
| `monitoring/__init__.py` | Monitoring package exposing `ValidationLayer` and `CanaryMonitor`. |
| `monitoring/models.py` | Pydantic-independent business models for canary results and statuses. |
| `monitoring/ports.py` | Persistence contracts required by validation and canary controls. |
| `monitoring/canary_catalog.py` | Canary assets catalog, market compatibilities, and provider deferred import registry. |
| `monitoring/price_ranges.py` | Statistical calculation, concurrent locking, and TTL caching of dynamic price ranges. |
| `monitoring/units.py` | Declares which fields each provider returns as percentages, and converts them to ratios before validation. |
| `monitoring/validation_layer.py` | Real-time validation of provider values: range checks, inter-provider consensus, and logging via an injected port. |
| `monitoring/canary_monitor.py` | Daily orchestration of canary controls, aggregates, and alerts via ports. |
| `database/monitoring.py` | SQLAlchemy monitoring adapter: price history, logs, aggregates, and alerts. |
| `routers/monitoring.py` | REST monitoring endpoints (`/health/*`) whose dependencies are resolved from `app.state`. |
| `routers/macro.py` | HTTP routes for macro-economic data: the rates of FRED (USD) and of the ECB (EUR), by currency; the indicators of Eurostat by country (latest values, series, refresh); the yield curves of the US Treasury (curve of a day with comparisons and breakeven inflation, maturities and spreads over time, refresh). |
| `routers/cot.py` | HTTP routes of the Commitments of Traders reports of the CFTC: the markets stored, the positions of every type of trader in a market, the signals of one type of trader (net position, share of the open interest, change, COT index), the latest positioning of several markets, the most extreme first, refresh. |
| `routers/sec.py` | HTTP routes of the annual statements filed with the SEC: statements, check against Yahoo, refresh; the instrument of the ticker, then its verified CIK. |
| `routers/esef.py` | HTTP routes of the ESEF filings of European listed companies: statements, check against Yahoo, filings, refresh; the instrument of the ticker, then its verified LEI. |
| `routers/factors.py` | HTTP routes of the factor returns: the datasets, the series of one dataset, the refresh of the files. |
| `historical/providers.py` | yfinance and TradingView connectors for retrieving historical bars, and the choice between them (`fetch`): yfinance with the verified symbol, TradingView only for a line quoted in the currency of the listing. |
| `historical/normalization.py` | Pure OHLCV validation, deduplication, and normalization rules. |
| `schemas/monitoring.py` | Pydantic v2 schemas for monitoring: `ProviderStatus`, `ProviderHealthSummary`, `ValidationResult`, `AlertSchema`, `DailyStatSchema`, `ProviderDetailResponse`, `HealthStatsResponse`, `AlertSeverity` / `AlertType` enums; re-exports `CanaryCheckResult` and `HealthStatus` from `monitoring/models.py`. |
| `zipline_bundle/__init__.py` | Public API of the Zipline data bundle (`FonRexBundle`, `fonrex_equities`, `register_fonrex_bundle`, `FonRexBundleDataSource`). |
| `zipline_bundle/data_source.py` | Zipline-free SQLAlchemy extraction layer for `prices_eod`: listing ranking, session-aligned OHLCV frames, deterministic `sid` allocation. |
| `zipline_bundle/bundle.py` | Zipline `ingest` callable orchestrating `AssetDBWriter`, `BcolzDailyBarWriter`, and `SQLiteAdjustmentWriter` from the data source output. |
| `zipline_bundle/extension.py` | Sample `~/.zipline/extension.py` that registers the `fonrex` bundle from environment variables. |
| `zipline_bundle/cli.py` | `python -m zipline_bundle preview` and `ingest`, for pipelines that cannot edit `~/.zipline/extension.py`. |

## Main Endpoints

### Routes Table

Every route of the application is listed here, and `tests/test_docs_consistency.py` fails when the table and the application differ. Unless stated otherwise a route requires an API key; a read-only key may call the `GET` routes and the two computation routes marked *(read-only allowed)*.

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/` | Description of the API as JSON (`documentation.py`) |
| GET | `/health` | State of the API: Redis status and cache lifetimes, providers loaded and providers that could not be imported. No key required. The database is not probed |
| GET | `/widgets.json` | OpenBB Workspace widget definitions. No key required |
| GET | `/apps.json` | OpenBB Workspace pre-assembled dashboards. No key required |
| GET | `/assets/by-isin/{isin}` | Asset and all its listings by ISIN |
| GET | `/listings` | Search for listings (ticker/isin/exchange/currency) |
| GET | `/eod/{ticker}` | EOD prices of a listing from `prices_eod`, JSON or CSV, by `period` or `from`/`to`; ingests the listing when nothing is stored for the request; `currency` / `exchange` choose one listing among those sharing the ticker |
| POST | `/cache/clear` | Deletes the cached `/eod` answers (`eod:*` keys) — not the other caches |
| POST | `/cache/clear/{ticker}` | Deletes the cached `/eod` answers of one ticker |
| GET | `/cache/stats` | Redis information and the `/eod` entries per ticker |
| GET | `/database/stats` | Database statistics |
| GET | `/database/tickers` | Tickers that have stored prices, with their first and last dates, row count and last ingestion |
| GET | `/database/ticker/{ticker}` | Specific ticker statistics in the database |
| POST | `/database/cleanup` | Deletes prices older than `days_to_keep` days (default 730, between 30 and 36500 — a smaller value is refused, it would empty the history) and logs older than 30 days; `dry_run: true` only counts |
| GET | `/fundamental` | Multi-provider fundamentals (see below), cached 1 hour |
| GET | `/fundamental/deep` | Deep fundamentals (highlights, financial statements, earnings, ratings) |
| GET | `/stocks` | Legacy: name, sector and price of ten US large caps, read from Yahoo at each call |
| GET | `/stocks/{ticker}/financials` | Legacy aggregator (`financials/service.py`): Yahoo by ticker, then BourseDirect by ISIN, merged; cached 24 h |
| POST | `/historical/ingest` | Triggering a unit ingestion (`currency` / `exchange` choose the listing; `force_refresh` replaces the series and looks the source symbol up again) |
| POST | `/historical/ingest/bulk` | Bulk ingestion with concurrency control (the primary listing of each ticker) |
| GET | `/ticker/{symbol}/history` | OHLCV history of a listing, read from the database only (no ingestion); `currency` / `exchange` choose the listing |
| GET | `/insider-transactions/{ticker}` | SEC EDGAR insider transactions (Form 4) |
| GET | `/etf/{isin}/details` | ETF details from JustETF |
| GET | `/index/{index_name}/constituents` | Index constituents (S&P500, CAC40, NASDAQ100, DAX) |
| WS | `/ws/realtime/{ticker}` | Realtime price streaming |
| GET | `/quote/{ticker}` | Latest price snapshot |
| GET | `/quotes` | Snapshots of several tickers (the first 20 of the list) |
| POST | `/realtime/subscribe` | Streaming subscription (50 tickers per request at most) |
| DELETE | `/realtime/subscribe/{ticker}` | Unsubscription |
| GET | `/realtime/status` | Active subscriptions status |
| GET | `/technical/{ticker}` | Single technical indicator calculation |
| GET | `/technical/{ticker}/multi` | Multi-indicator calculation |
| GET | `/technical/{ticker}/chart` | OHLCV chart data + indicators |
| POST | `/technical/batch` | Multi-ticker / multi-indicator batch *(read-only allowed)* |
| GET | `/technical/list` | List of available indicators |
| GET | `/technical/screen` | Screener with indicator conditions |
| GET | `/news/stats` | Global news system statistics |
| GET | `/news/feed` | Global news feed (all assets, filterable by language and tickers) |
| GET | `/news/{ticker}` | News for a ticker (Redis 30 min cache, all news providers, dedup) |
| POST | `/news/{ticker}/refresh` | Force background refresh (BackgroundTask) |
| GET | `/dcf/{ticker}` | Default DCF intrinsic valuation (FCF model, Redis 6h cache) |
| POST | `/dcf/{ticker}` | Custom DCF valuation (custom growth rate, WACC, and weightings, no cache) *(read-only allowed)* |
| GET | `/dcf/{ticker}/compare` | Comparison and consensus of the 3 DCF models (FCF, EPS, DDM, Redis 6h cache) |
| GET | `/dcf/{ticker}/sensitivity` | Intrinsic value sensitivity matrix generation (WACC vs Terminal Growth, Redis 6h cache) |
| GET | `/health/providers` | Health status of all providers (Redis → DB fallback) |
| GET | `/health/providers/{provider_name}` | Provider health detail (daily stats, recent failures, alerts) |
| GET | `/health/alerts` | Active/resolved alerts (filterable by severity, provider, include_resolved) |
| POST | `/health/alerts/{alert_id}/resolve` | Manual resolution of an alert with optional note |
| POST | `/health/canary/run` | Manual canary check trigger (background, by provider or global) |
| GET | `/health/canary/history` | Canary results history (filterable by provider, ticker, period) |
| GET | `/health/stats` | Global data quality statistics (7 days, validity rate, reliable providers) |
| GET | `/macro/rates` | Current macro-economic rates of FRED (USD) and of the ECB (EUR); `currency=USD` or `EUR` keeps one source |
| GET | `/macro/indicators` | Latest value of each Eurostat indicator for one country or area (`geo`, `EA` by default, read as `EA21`; `indicator` to keep some), with the previous period and the change; a series missing or older than `EUROSTAT_REFRESH_HOURS` is read first |
| GET | `/macro/indicators/{indicator}` | Values of one Eurostat indicator over time (`geo`: up to 10 countries or areas separated by commas, `start`, `end`), periods dated by their first day, rates as ratios |
| POST | `/macro/indicators/refresh` | Read Eurostat series into the database (`geo`, `indicator`, `force`) |
| GET | `/macro/treasury/curve` | Yield curve of the US Treasury on one day (`curve` nominal or real, `date`: the business day on or before it, the latest by default), shortest maturity first, with the breakeven inflation (nominal minus real) and up to 5 `compare` curves (a date or 1W, 1M, 3M, 1Y… before) giving the change of each maturity; a year missing or the current year older than `TREASURY_REFRESH_HOURS` is read first |
| GET | `/macro/treasury/series` | Maturities (`10Y`) or spreads of two (`10Y-2Y`) of a Treasury curve, day by day (`tenor`: up to 6, `curve`, `start`: 10 years before `end` by default, `end`), rates as ratios |
| POST | `/macro/treasury/refresh` | Read years of the Treasury curves into the database (`curve`: both by default, `start_year`, `end_year`: the current year by default, `force`) |
| GET | `/cot/markets` | Markets of the Commitments of Traders reports stored (`report`: legacy, tff, disaggregated, all three by default; `q`: part of the name or code; `key`: the key markets only), with the latest name, the report dates stored and whether it is a key market; the current year of each report is read first when needed |
| GET | `/cot/positions` | Positions of every type of trader in one market (`market`: CFTC contract market code, `report`: legacy by default, `start`: 3 years before `end` by default, `end`), report by report in contracts, with the estimated day of publication; the years of the period are read first when needed |
| GET | `/cot/signals` | Positioning of one type of trader in one market (`category`: the speculators of the report by default, `window`: weeks of the COT index, 156 by default), report by report: net position, its share of the open interest, its change, the COT index, the estimated day of publication |
| GET | `/cot/extremes` | Latest report of several markets (`market`: up to 20, the key markets of the report by default), the COT index farthest from 50 first; the markets without report are listed apart |
| POST | `/cot/refresh` | Read years of the Commitments of Traders reports into the database (`report`: all three by default, `start_year`, `end_year`: the current year by default, `force`) |
| GET | `/sec/{ticker}/statements` | Annual statements the company filed with the SEC (XBRL), most recent year first (`years`, `isin`): each figure with its concept, form, filing date and EDGAR link; `404` for an instrument without SEC filer (no verified CIK) |
| GET | `/sec/{ticker}/check` | The annual statements stored from Yahoo checked against the SEC filings, year by year: `match`, `mismatch`, `missing_yahoo`, `missing_sec` within `SEC_CHECK_TOLERANCE` |
| POST | `/sec/{ticker}/refresh` | Look the CIK up again and read the facts of the company from the SEC now |
| GET | `/esef/{ticker}/statements` | Annual statements the company filed in ESEF, most recent year first (`years`, `isin`): each figure with its IFRS concept, its filing (`fxo_id`, true period) and its link in the viewer of filings.xbrl.org; the filings read first when missing or older than `ESEF_REFRESH_HOURS`. `404` when the instrument has no verified LEI (no ISIN, a fund, a name GLEIF does not confirm) |
| GET | `/esef/{ticker}/check` | The annual statements stored from Yahoo checked against the ESEF filings, year by year: `match`, `mismatch`, `missing_yahoo`, `missing_esef` within `SEC_CHECK_TOLERANCE`, each figure with its link in the viewer |
| GET | `/esef/{ticker}/filings` | The filings of the company in the index of filings.xbrl.org, latest period first: links, counts of errors, and what their reading found (status, true period, values, breakdowns, conflicts, reason of a refusal) |
| POST | `/esef/{ticker}/refresh` | Ask GLEIF for the LEI again, list the filings and read every period again now |
| GET | `/factors` | Factor datasets of the Kenneth French library and what is stored of each file |
| GET | `/factors/{dataset}` | Factor returns of one dataset (`frequency`, `start`, `end`), as ratios in US dollars; a file missing or older than `FACTORS_REFRESH_DAYS` is downloaded first |
| POST | `/factors/refresh` | Download files of the library (`dataset`, `frequency`, `force`) |
| GET | `/factors/exposure/{ticker}` | Exposure of a listing to the Fama/French factors (`model` ff3, ff5 or carhart; `frequency`, `window`, `end`, `region`): betas, alpha, R², on returns in US dollars |

OpenBB Workspace routes (`routers/openbb.py`). Each one calls the route function of the Fonrex route it adapts, with the same parameters, and reshapes the answer (see [Integrations](docs/monitoring-and-integrations.md#integrations)):

| Method | Endpoint | Adapts | Widget type |
| --- | --- | --- | --- |
| GET | `/openbb/quote/{ticker}` | `/quote/{ticker}` | metric |
| GET | `/openbb/macro/rates` | `/macro/rates` | metric |
| GET | `/openbb/macro/indicators` | `/macro/indicators` | metric |
| GET | `/openbb/macro/indicators/{indicator}/chart` | `/macro/indicators/{indicator}` | chart |
| GET | `/openbb/macro/treasury/curve/chart` | `/macro/treasury/curve` | chart |
| GET | `/openbb/macro/treasury/series/chart` | `/macro/treasury/series` | chart |
| GET | `/openbb/cot/signals/chart` | `/cot/signals` | chart |
| GET | `/openbb/cot/extremes` | `/cot/extremes` | table |
| GET | `/openbb/factors/exposure/{ticker}` | `/factors/exposure/{ticker}` | table |
| GET | `/openbb/sec/{ticker}/check` | `/sec/{ticker}/check` | table |
| GET | `/openbb/esef/{ticker}/check` | `/esef/{ticker}/check` | table |
| GET | `/openbb/factors/{dataset}/chart` | `/factors/{dataset}` | chart |
| GET | `/openbb/eod/{ticker}` | `/eod/{ticker}` | chart |
| GET | `/openbb/ticker/{symbol}/history` | `/ticker/{symbol}/history` | chart |
| GET | `/openbb/technical/{ticker}` | `/technical/{ticker}` | chart |
| GET | `/openbb/technical/{ticker}/multi` | `/technical/{ticker}/multi` | chart |
| GET | `/openbb/technical/{ticker}/chart` | `/technical/{ticker}/chart` | chart |
| GET | `/openbb/technical/screen` | `/technical/screen` | table |
| GET | `/openbb/fundamental` | `/fundamental` | table |
| GET | `/openbb/fundamental/deep` | `/fundamental/deep` | table |
| GET | `/openbb/quotes` | `/quotes` | table |
| GET | `/openbb/dcf/{ticker}` | `/dcf/{ticker}` | table |
| GET | `/openbb/dcf/{ticker}/compare` | `/dcf/{ticker}/compare` | table |
| GET | `/openbb/dcf/{ticker}/sensitivity` | `/dcf/{ticker}/sensitivity` | table |
| GET | `/openbb/news/feed` | `/news/feed` | table |
| GET | `/openbb/news/{ticker}` | `/news/{ticker}` | table |
| GET | `/openbb/insider-transactions/{ticker}` | `/insider-transactions/{ticker}` | table |
| GET | `/openbb/etf/{isin}/details` | `/etf/{isin}/details` | table |
| GET | `/openbb/index/{index_name}/constituents` | `/index/{index_name}/constituents` | table |

For detailed `/fundamental`, `/fundamental/deep` and specialized endpoint flows, see [docs/fundamentals-and-providers.md](docs/fundamentals-and-providers.md).

## Cache And Logging

Redis holds only answers that can be computed again: losing it loses nothing. A key holds every parameter that changes the answer (`tests/test_cache_keys.py`): a parameter left out makes a request receive the answer of another one. Two clients coexist, a synchronous one (`CacheService`, called through `run_sync()`) and an asynchronous one (`app.state.redis_client`). Every entry is JSON.

What is cached, and for how long:

| Answer | Key | Lifetime | Where the lifetime is set |
| --- | --- | --- | --- |
| `GET /eod/{ticker}` | `eod:{TICKER}:{period}:…` | 86 400 s (24 h) | `CacheService`, category `eod` |
| `GET /ticker/{symbol}/history` | `history:{SYMBOL}:…` | 86 400 s (24 h) | `CacheService`, category `history` |
| `GET /fundamental` | `fundamental:{ticker}:{exchange}:{currency}:{fmt}:{providers}` | 3 600 s (1 h) | `use_cases/fundamentals.py` |
| `GET /fundamental/deep` | `deep:{asset_id}` (every section) | 86 400 s (24 h) | `CacheService`, category `highlights` |
| `GET /insider-transactions/{ticker}` | `insider_transactions:{TICKER}:limit-{limit}` | 43 200 s (12 h) | `CacheService`, category `insider_transactions` |
| `GET /etf/{isin}/details` | `etf_details:{ISIN}` | 86 400 s (24 h) | `CacheService`, category `etf_details` |
| `GET /index/{index_name}/constituents` | `index_constituents:{INDEX}` | 604 800 s (7 d) | `CacheService`, category `index_constituents` |
| `GET /stocks/{ticker}/financials` | `financials:{ticker}` | 86 400 s (24 h) | `financials/service.py` |
| Technical indicators | `technical:{TICKER}:{resolution}:…:{limit}` | 60 s to 4 h by resolution | `technical/catalog.py` |
| `GET /technical/list` | `technical:list` | 86 400 s (24 h) | `routers/technical.py` |
| `GET /technical/screen` | `technical:screen:…` | 900 s (15 min) | `routers/technical.py` |
| `GET /news/{ticker}` | `news:{ticker}:{limit}:{language}` | 1 800 s (30 min) | `NEWS_CACHE_TTL` |
| `GET /dcf/*` | `dcf:{TICKER}:…` | 21 600 s (6 h) | `DCF_CACHE_TTL` |
| FRED series | `macro:fred:{series}` | 21 600 s (6 h) | `MACRO_RATES_CACHE_TTL` |
| Last realtime quote | `quote:{ticker}` | 60 s | `REALTIME_QUOTE_TTL` |
| Provider health summary | `provider:health:summary` | 3 600 s (1 h) | `monitoring/canary_monitor.py` |

`CacheService.DEFAULT_TTLS` declares more categories than the six used above (`fundamentals`, `metadata`, `mappings`, `technical_*`, `news`, `dcf`…): they are reported by `/health` and `/cache/stats` but no code stores an entry under them — the features concerned set their own lifetime, as the table shows. A category that is not declared lasts `CACHE_TTL` seconds (300 by default). The news feed (`GET /news/feed`) is read from the database and is not cached. The asynchronous client also carries the `price:{ticker}` Pub/Sub channels of the streaming.

Invalidation:

- An ingestion drops the `history`, `eod`, `technical` and `dcf` entries of the ticker.
- `POST /cache/clear` and `POST /cache/clear/{ticker}` delete `eod:*` entries only; `GET /cache/stats` counts the same entries. The other caches expire on their own, or are bypassed per request (`nocache` on `/fundamental`, `refresh` on `/fundamental/deep` and the specialised routes, `force_refresh` on `/news/{ticker}` and `/dcf/*`).

Application logs go to the standard output (`logging.basicConfig(level=INFO)`), read with `docker compose logs`.

Usage logging is centralized in the FastAPI middleware and `usage_recorder.py`:

- Each HTTP request is queued in memory when the response is ready; the response never waits for the database. `/health`, `/static`, `/docs`, `/redoc`, `/openapi.json` and `/favicon.ico` are not recorded. The queue holds 10,000 entries at most; beyond that the new ones are dropped.
- A background task (`UsageRecorder`) writes the pending entries to `usage_logs` in one transaction every 5 seconds, and once more at shutdown. If the database is unavailable the entries are dropped.
- Stored fields include endpoint, method, status, latency, providers used, cache hit, the user-agent (its first 256 characters) and a fingerprint of the API key (prefix and the first characters of its SHA-256, never the key). The `cost_bucket` column exists and no route fills it. The caller's IP address is **not** stored by default: `USAGE_LOG_IP` selects `none` (default), `truncated` (network only: IPv4 /24, IPv6 /48) or `full`.
- Rows older than `USAGE_LOG_RETENTION_DAYS` (90 by default, `0` to keep everything) are deleted once a day.
- It is the local usage journal of a self-hosted instance, not a billing record.

## Provider Health Monitoring

Real-time outlier validation (`ValidationLayer`) and daily canary checks (`CanaryMonitor`) are detailed in [docs/monitoring-and-integrations.md](docs/monitoring-and-integrations.md).

**Canary assets:**

| Ticker | Fields checked | Example expected range |
| --- | --- | --- |
| `AAPL` | pe_ratio, dividend_yield, beta, price | pe: [20, 45], price: [100, 500] |
| `AIR.PA` | pe_ratio, dividend_yield, beta, price | pe: [15, 60], price: [80, 300] |
| `BNP.PA` | pe_ratio, dividend_yield, pb_ratio, price | pe: [4, 15], div: [0.04, 0.12] |
| `MSFT` | pe_ratio, beta, price | pe: [25, 50], price: [200, 600] |
| `TSLA` | pe_ratio, beta, price | pe: [30, 800], beta: [1.5, 3.5] |

## Alembic Migrations

Every file of `alembic/versions/` is listed here (`tests/test_docs_consistency.py`). The chain is linear, with a single head.

| Revision | File | Changes |
| --- | --- | --- |
| 001 | `001_initial_schema.py` | Snapshot of the schema that existed before Alembic: `assets` (with the unique partial index on the ISIN), `asset_listings` (with `uq_asset_listing_identity`), `asset_mappings`, `prices_eod` and `fundamentals` (hypertables), `usage_logs`, and the legacy tables `stock_data`, `data_requests`, `cache_status`. Each table is created only if absent |
| 002 | `002_refonte_fundamentals.py` | Creation of `fundamentals_highlights`, `financial_statements`, `earnings_history`, `analyst_ratings`, `etf_details`, `etf_holdings`; the legacy `fundamentals` table is kept; compatibility view `fundamentals_v2` |
| 003 | `003_index_constituents.py` | Creation of the `index_constituents` table (not used by the code today) |
| 004 | `004_fix_assets_columns.py` | Addition of the profile columns missing from `assets` (currency, sector, industry, ISIN, quote type, display name, country, logo path, profile…) |
| 005 | `005_premium_fields.py` | Short-interest, TTM and growth columns on `fundamentals_highlights`; GICS columns on `assets`; creation of `earnings_trend`, `esg_scores`, `outstanding_shares_history` |
| 006 | `006_prices_eod_resolution.py` | Addition of `resolution`, `adjusted`, `source` to `prices_eod`; creation of `ingest_log` |
| 007 | `007_realtime_tables.py` | Creation of `prices_intraday` hypertable (30-day retention) + `realtime_subscriptions` table |
| 008 | `008_drop_legacy_tables.py` | **Destructive**: drops the legacy price tables (`stock_data`, `data_requests`, `cache_status`, `daily_stock_records`, `weekly_stock_records`, `monthly_stock_records`, `yearly_stock_records`), replaced by `prices_eod`. The downgrade recreates them empty |
| 009 | `009_fix_assets_isin_unique.py` | Cleanup of existing `assets.isin` duplicates (listings/mappings re-parenting → orphaned deletion); creation, where they are missing, of the unique partial index `uq_assets_isin_not_null` on `assets(isin) WHERE isin IS NOT NULL` and of the constraint `uq_asset_listing_identity` |
| 010 | `010_news_articles.py` | Creation of `news_articles` table (FK → `assets.id`, unique `url`, 3 indexes); request for a 90-day TimescaleDB retention policy, without effect since the table is not a hypertable |
| 011 | `011_provider_health.py` | Creation of `provider_health_log` (composite PK `(id, checked_at)`, TimescaleDB hypertable conversion, 30 days retention, 2 indexes), `provider_health_daily` (unique `(provider_name, date)`, 1 index), `provider_alerts` (2 indexes on `(provider_name, is_resolved)` and `(severity, is_resolved)`) |
| 012 | `012_alembic_schema_authority.py` | Alembic takeover of hypertables, compression, and weekly/monthly continuous aggregates historically created by the PostgreSQL bootstrap. |
| 013 | `013_solvency_ratios.py` | Addition of solvency ratios (`debt_to_equity_ratio`, etc.) and cost of debt to `fundamentals_highlights`; creation of `macro_rates_cache` table. |
| 014 | `014_prices_per_listing.py` | Rebuild of `prices_eod` with the key `(asset_listing_id, resolution, time)`: existing rows are attached to their listing (a listing is created for an instrument that has prices and none), re-dated to their session (midnight UTC) and merged; compression re-enabled, segmented by listing and resolution; `prices_weekly` / `prices_monthly` recreated per listing from the daily bars, with real-time aggregation and a daily refresh policy. Before touching the tables, both directions delete the compression and refresh jobs of the price tables (waiting for a running one) and lock `prices_eod`: a TimescaleDB job running during the migration deadlocked with it. |
| 015 | `015_dividend_yield_as_ratio.py` | Data correction: `fundamentals_highlights.dividend_yield` values stored as percentages (as Yahoo publishes them) are divided by 100, the rows already stored as ratios are left alone; no schema change. |
| 016 | `016_price_series_adjustments.py` | `price_series_adjustments` table: the adjustment scheme of each stored price series (listing and resolution) and when it was last fetched in one piece. No row is written: the series stored before have none and are fetched again in full at their next ingestion; prices are not touched. |
| 017 | `017_macro_rates_source.py` | `macro_rates_cache` holds several sources: `series_id` widened from 30 to 60 characters (ECB series are named `flow.key`, e.g. `YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y`), new `source` column (`fred`, `ecb`), set to `fred` for the rows already stored. The downgrade drops the rows whose name exceeds 30 characters. |
| 018 | `018_statements_currency_unknown.py` | `financial_statements.currency` loses its `USD` default and the stored rows become NULL (unknown): the enrichment never wrote it. The enrichment now stores Yahoo's `financialCurrency`; until an instrument is enriched again, the valuation takes the currency of the listing of the share price. The downgrade sets the unknown currencies back to USD. |
| 019 | `019_yahoo_epoch_dates.py` | `fundamentals_highlights.dividend_ex_date` and `shares_short_date` equal to 1970-01-01 become NULL: Yahoo gives these dates in seconds since 1970 and the enrichment read them as nanoseconds. The enrichment now reads seconds (and also stores `dividend_pay_date` from `dividendDate`); the dates come back at the next refresh. The downgrade leaves the data alone. |
| 020 | `020_factor_returns.py` | `factor_returns` (dataset, frequency, period end, factor → value as a ratio) and `factor_dataset_loads` (last download of each file of the Kenneth French library). The downgrade drops both tables. |
| 021 | `021_fx_rates.py` | `fx_rates` (currency, day → units of the currency for one euro, ECB reference rate) and `fx_rate_loads` (last refresh of each currency). The downgrade drops both tables. |
| 022 | `022_macro_observations.py` | `macro_observations` (source, indicator, geographic area, period → value, unit, flag of the source; Eurostat indicators, monthly or quarterly, a revision replacing the value of its period) and `macro_series_loads` (last refresh of each series). The downgrade drops both tables. |
| 023 | `023_xbrl_facts.py` | `xbrl_facts` (CIK, taxonomy, concept, unit, period end and length in days → value, the filing it was read from: accession, form, date filed) and `sec_company_loads` (last reading of each company from the SEC). The value of the latest filing is kept: a restatement replaces the earlier one. The downgrade drops both tables. |
| 024 | `024_yield_curves.py` | `yield_curve_points` (source, curve, day, maturity → rate as a ratio, length of the maturity in months; the par curves of the US Treasury, nominal and real) and `yield_curve_loads` (last reading of each year of a curve). The downgrade drops both tables. |
| 025 | `025_cot_reports.py` | `cot_weeks` (report, market code, report date → name of the market that week, contract units, open interest), `cot_positions` (long, short and spreading of each type of trader of a line, removed with their line) and `cot_loads` (last reading of each year of a report and the file that held it); the Commitments of Traders reports of the CFTC. The downgrade drops the three tables. |
| 026 | `026_esef_filings.py` | `esef_filings` (a filing of the index of filings.xbrl.org: its LEI, the period the index gives it, its country, links and counts of errors, and what its last reading found: status, true period of its facts, counts of facts, breakdowns and conflicts), `esef_facts` (one value per LEI, concept, unit and period, the one of the latest filing, removed with its filing) and `esef_entity_loads` (last reading of the filings of each company); the ESEF filings of European listed companies. The downgrade drops the three tables. |
| 027 | `027_esef_long_prefixes.py` | `esef_facts.taxonomy` and `esef_facts.unit` take 255 characters (40 before): the prefix of the extension of a company can be longer (`ecclesiasticalinsuranceofficepubliclimitedcompany`). The downgrade gives them back 40 characters. |
| 028 | `028_esef_text_keys.py` | `esef_facts.taxonomy`, `concept` and `unit` become `text`: a company names the concepts and units of its extension as it wishes, and a catalogue of 3 000 European companies held names longer than 255 characters. The downgrade gives them back 255 characters. |
| 029 | `029_single_primary_listing.py` | Data correction then guard: an instrument keeps a single primary listing, an active one before an inactive one, then the oldest (lowest `id`), and its other listings marked primary by the CSV importer are unmarked; the partial unique index `uq_asset_listings_one_primary` on `asset_listings (asset_id) WHERE is_primary` then refuses a second one. The downgrade drops the index and leaves the data alone. |

## Vigilance Points

- Routes and use cases are asynchronous, but synchronous SQLAlchemy, `CacheService`, pandas, and yfinance remain blocking adapters. Any invocation from the event loop must go through `concurrency.run_sync()`; an architectural test forbids scattered calls to `asyncio.to_thread`/`run_in_executor`. The TradingView streamer keeps its dedicated executor, adapted to its long-running blocking generator.
- **Alembic is the sole schema authority** (`alembic/versions/`). The Docker entrypoint executes `alembic upgrade head`; the runtime only checks the revision and contains no fallback `create_all` or compatibility DDL. SQLite tests explicitly create their isolated schema.
- Core layers use typed exceptions: `SQLAlchemyError` for persistence, `RedisError` and serialization errors for cache, `ValueError` or use case errors for business inputs. Bare `Exception` catches are only allowed at resilience boundaries isolating a provider, a batch element, a WebSocket stream, or a shutdown phase. An architectural test forbids bare `except:` and general catches in persistence, cache, and main routes.
- Application timestamps use timezone-aware UTC `datetime` (`datetime.now(timezone.utc)` or `datetime.now(UTC)`); `datetime.utcnow()` is forbidden by an architectural test. SQLAlchemy code uses `Session.get()` instead of the legacy `Query.get()` (an architectural test forbids the latter); `session.query()` is still widely used. Dependency warnings are filtered at the concerned import point, with a precise message. `pytest.ini` holds one suite-wide filter, for a deprecation raised inside `anyio`.
- In TimescaleDB, the `prices_eod` table is partitioned on time. SQLAlchemy maps the `timestamp` attribute but the underlying physical column is named `'time'`. It is imperative to target `'time'` in raw SQL queries, indexes, and `index_elements` upsert clauses to avoid insertion or indexing failures. Migrations 012 and 014 handle compression and the weekly/monthly continuous aggregates.
- A query on `prices_eod` always names a listing and a resolution (`asset_listing_id = … AND resolution = …`), and resolves the ticker with `database/price_series.py`; filtering on `asset_id` alone mixes the listings — and the currencies — of an instrument. Three readers still count rows per instrument, which is right for a count and for nothing else: `/database/tickers`, `/database/ticker/{ticker}` and the list of instruments of the screener. Bounds on `time` are sent as midnight-UTC instants (`session_timestamp()`), and a `time` read back is turned into a date with `session_date()`, so that nothing depends on the time zone of the database session.
- Rows migrated by revision 014 were re-dated from the time of day of their old instant; a TradingView-sourced bar of an exchange opening on the hour between 13:00 and 15:59 UTC (São Paulo, Buenos Aires, Lima) may be dated one day late. `POST /historical/ingest` with `force_refresh` replaces a series and settles any doubt. TradingView bars are dated by the UTC date of the session opening, which is one day early for the few exchanges opening before midnight UTC (Sydney in summer time).
- Migration 009 cleans up existing `assets.isin` duplicates and enforces the unique partial index. On a database containing many duplicates, first run `scripts/clean_isin_duplicates.py --diagnose-only` to estimate the impact before `alembic upgrade head`.
- Bare tickers remain inherently ambiguous. Business calls should favor `isin`, or `ticker + exchange + currency` when exact listing matters. The technical routes, the bulk ingestion, the real-time routes and the Zipline bundle do not accept `currency` / `exchange`: they work on the listing the bare ticker designates.
- Web providers can change their HTML or block some requests. The runner isolates errors by provider, but partial results should be considered normal. The `ValidationLayer` resets suspect values to `None`; the `CanaryMonitor` only records and alerts.
- The CSV import makes no network call. The optional enrichment step (`import_assets.py --enrich-only`, `scripts/seed_database.py --enrich`) depends on yfinance: on a large catalogue its duration depends on the network and on provider-side limits.
- `SECEdgar` only covers US-listed companies (EDGAR system). Insider transactions for European companies are not available via this provider.
- Deep yfinance enrichment (highlights, ESG, earnings trend) depends on Yahoo API quotas. In case of rate limiting, premium tables may be partially populated.
- Canary ranges (`CANARY_ASSETS`) and validation ranges (`FIELD_RANGES`) must be periodically reviewed if the fundamentals of reference assets evolve significantly (e.g., AAPL split, BNP dividend policy change).
- When a provider is added, or changes the unit of a field, `PROVIDER_PERCENT_FIELDS` in `monitoring/units.py` must be updated; `tests/test_provider_units.py` runs each provider's parser and fails when the declared unit no longer matches.
- Authentication is secure by default. Tests run with `FONREX_AUTH_REQUIRED=false` (`tests/conftest.py`); an instance must set `FONREX_API_KEY`, and give a read-only key to any client that holds it outside the machine (spreadsheet, dashboard reached through a tunnel). The API port is published on every interface of the host and Redis has no password: the key is the only protection of an instance on a shared network or behind a tunnel.
- The API is designed for one process (`WEB_CONCURRENCY=1`): realtime streams, the canary scheduler, the usage queue and the WebSocket groups live in its memory.
- The `CanaryMonitor` uses dynamic imports (`importlib`) to load provider classes. If a provider is renamed or moved, the `_PROVIDER_IMPORTS` mapping in `monitoring/canary_catalog.py` must be updated.
- The `ASYNC_DATABASE_URL` is automatically inferred from `DATABASE_URL` when its scheme is `postgresql://` or a synchronous PostgreSQL driver (`postgresql+psycopg2://`, `+psycopg2cffi`, `+pg8000`, `+pygresql`): the scheme becomes `postgresql+asyncpg://`. With any other scheme nothing is inferred, an error is logged and the asynchronous features are off: `ASYNC_DATABASE_URL` must then be set explicitly.
- Several call sites of the asynchronous Redis client still use `setex`, which `redis-py` announces as deprecated; `CacheService` already uses `set(..., ex=)`.

### Known Limits

Behaviours observed in the code that a reader should not have to discover. They are not rules to preserve: each one is a candidate for a fix, and this list must shrink with them.

- **`/eod` and stale prices.** `GET /eod/{ticker}` ingests only when it finds nothing; refreshing a series is an explicit `POST /historical/ingest`. The gap detection considers a series up to date when its last bar is today's or yesterday's.
- **Retention.** `POST /database/cleanup` keeps 730 days by default while an ingestion loads ten years: calling it with the default removes most of what was ingested. `news_articles` is never purged.
- **Cache administration.** `POST /cache/clear` only removes `/eod` answers.
- **Yahoo outside the verified symbol.** The delayed quote of `/quote` and `/quotes`, the Yahoo news provider and `/stocks/*` ask Yahoo for the ticker as typed. For a ticker that names another instrument on Yahoo, they describe that other instrument. The end-of-day prices, `/fundamental`, `/fundamental/deep` and the enrichment started by the import do not have this flaw.
- **Ambiguous tickers.** Without `currency` / `exchange`, a ticker shared by several listings resolves to one of them by rule, not by intent, and not by the same rule for prices and for fundamentals (see [Identity Resolution](docs/data-model.md#identity-resolution)); the bulk ingestion, the technical and real-time routes, `/fundamental/deep` and the Zipline bundle cannot name a listing.
- **Screener.** `GET /technical/screen` evaluates a sample of the catalogue (see [Technical Indicators](docs/historical-and-import.md#technical-indicators)), not all of it.
- **Tables, settings and files without a writer or a caller.** `etf_details`, `etf_holdings` and `index_constituents` are never written; `fundamentals` is never written and no route reads it; `usage_logs.cost_bucket` is never filled; the `OpenFIGI` provider is loaded and no route calls it; `download_logo` (`LOGO_TOKEN`) is never called; `INGEST_TV_DELAY` is read and used by nothing; two alert types are declared and never raised.
- **Investing.com news.** The news provider reads a mapping named `investing_com`, which the import never creates: it returns nothing unless such a mapping is added by hand.
- **Untested paths.** The gap detection of the ingestion, the weighted consensus of the DCF, and the ESG, earnings-trend and shares-history parts of the enrichment have no test of their own; `tests/test_google_url_provider.py` is a manual script.
- **Statements enriched before the fiscal-year rule.** Their interest, tax, operating profit, share counts and dividends are empty until the instrument is enriched again (`GET /fundamental/deep?refresh=true`).
- **Layers.** Most features are services called directly by their router (see [Layers](#layers)); `macro/fred_service.py`, `macro/ecb_service.py`, `macro/eurostat_service.py`, `macro/fx_rates.py` and `factors/french_library.py` create their own HTTP clients outside the provider HTTP layer.
- **Providers blocked by anti-bot services.** From a personal connection several sites refuse the request (typically with a `403`); their entry of the answer is then an error. This is expected until a relay is configured (see [Outbound Relay](docs/monitoring-and-integrations.md#outbound-relay-not-part-of-this-repository)).
