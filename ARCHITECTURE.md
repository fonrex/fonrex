# Fonrex — Technical Architecture

This document describes the architecture observed in the source code. Its tables (routes, migrations, modules, cache lifetimes, canary assets) are compared with the code by `tests/test_docs_consistency.py`: a route, a migration or a router that is added without being described here fails the quality gate. The history of the changes is in the git log, not in this file.

Fonrex is a self-hosted FastAPI API that aggregates market data, fundamentals, asset metadata and financial news. It combines PostgreSQL/TimescaleDB, Redis, yfinance, asynchronous web providers, an ISIN-based CSV import, a multi-source news aggregation engine and an automated provider health monitoring system.

**Scope.** Fonrex runs on the machine of the person who uses it: the instance fetches public pages for its owner and keeps the result in its own database. There is no Fonrex-operated data API, no account and no billing in this code base. Two consequences shape the architecture: authentication protects a personal instance (a few keys set in `.env`, see [Startup](#startup)), and every client — OpenBB Workspace, the Google Sheets connector — talks to the user's own instance (see [Integrations](#integrations)).

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

1. Waits for PostgreSQL on `db:5432`; the start-up is aborted if it never answers.
2. Waits for Redis on `redis:6379`; an unreachable Redis only disables the cache.
3. Applies database migrations via Alembic (`alembic upgrade head`).
4. With `SEED_ON_FIRST_RUN=true` and an empty `assets` table, imports `data/etf.csv`.
5. Launches `gunicorn --workers $WEB_CONCURRENCY --worker-class uvicorn.workers.UvicornWorker main:app`.

`docker-compose.yml` loads `.env` into the API container (`env_file`), overrides `DATABASE_URL` and `REDIS_URL` to target the Compose services, sets `WEB_CONCURRENCY` (default 1) and clears the system proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`). PostgreSQL and Redis are published on `127.0.0.1` only.

Authentication is enforced by default (`auth/dependencies.py`): protected routes require a key listed in `FONREX_API_KEY`, `FONREX_RELAY_KEY` or `FONREX_API_KEYS` (full access), or in `FONREX_READ_ONLY_API_KEYS` (reads only: `GET` routes plus the two computation routes `POST /technical/batch` and `POST /dcf/{ticker}`; meant for clients holding the key outside the host, such as the Sheets connector). With no key configured every protected request is rejected (fail closed); the only opt-out is `FONREX_AUTH_REQUIRED=false` with no key configured. `main.py` logs the effective mode at startup.

A key is sent as `Authorization: Bearer <key>` or `X-API-KEY: <key>` (the custom header of OpenBB Workspace); the WebSocket route also accepts it as a query parameter (`api_key`, `token` or `key`) and checks it during the handshake, because HTTP middlewares do not see WebSocket connections. The paths that answer without a key are `/health`, `/docs`, `/redoc`, `/openapi.json`, `/widgets.json`, `/apps.json`, `/favicon.ico` and `/static/*`. A read-only key never starts a TradingView stream: `GET /quote/{ticker}`, `GET /openbb/quote/{ticker}` and the WebSocket serve what is already streamed, and the WebSocket sends a `not_streaming` message when the ticker is not. Cross-origin requests are accepted from the origins listed in `OPENBB_ALLOWED_ORIGIN` only (default `https://pro.openbb.co`).

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
| Operations | `routers/admin.py` | `database/maintenance.py`, `cache/service.py` | No |
| OpenBB | `routers/openbb.py` | calls the route functions of the other routers | — |

The use-case layer is therefore the target model, reached by three routers; the other features are services called directly. Architecture tests hold the split where it exists (`tests/test_exception_boundaries.py`): the `technical/` package imports neither FastAPI, SQLAlchemy, Redis nor the ORM models, and the `monitoring/` package neither SQLAlchemy nor the ORM models; the three port modules stay typed; the persistence and cache modules and the routers listed in that test use typed exceptions; four orchestrators have a size limit. In `main.py`, the routers, the use cases and the feature packages scanned by `tests/test_async_boundary.py`, blocking code is reached through `concurrency.run_sync()` only.

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
| `models.py` | Central ORM models: instruments, listings, provider mappings, EOD and intraday prices, realtime subscriptions, fundamentals (highlights, statements, earnings, ratings, ESG, trends, outstanding shares), ETFs, news articles, provider health logs/aggregates/alerts, macro rate cache, usage and ingestion logs. |
| `database/service.py` | Compatibility facade and SQLAlchemy composition root; owns the engine/session factory and delegates to specialized components. |
| `database/assets.py` | Asset identity/listing, mappings, and profile enrichment repository. |
| `database/fundamentals.py` | Standard and deep fundamentals read repository. |
| `database/maintenance.py` | Statistics, inventory of the tickers holding prices, and the bounded cleanup of old prices and logs (`MIN_DAYS_TO_KEEP`, dry run). |
| `database/usage.py` | API usage events persistence. |
| `database/migrations.py` | Read-only verification of the current revision against the Alembic head. |
| `database/component.py` | Shared foundation giving components short and explicit access to synchronous sessions. |
| `database/price_series.py` | The one rule that turns a ticker into a price series (instrument + listing), and the session-date convention of `prices_eod.time`. Used by the API paths that read or write a price series (ingestion, history, EOD, technical indicators, canary price ranges); the Zipline bundle ranks listings with its own query. |
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
| `scripts/ingest_all.py` | Administration CLI to trigger global historical ingestion of all database assets. |
| `scripts/seed_database.py` | Imports the default catalogue, then optionally enriches it and ingests daily prices (`make db-seed`). |
| `scripts/check_coverage_distribution.py` | Coverage floor of each module, checked by `make ci`, and the coverage summary published by the CI. |
| `scripts/example_realtime_client.py` | Example client of the realtime REST and WebSocket routes. |
| `fonrex-sheets-connector/Code.gs` | Google Apps Script of the spreadsheet template: reads fundamentals, DCF and indicators from the user's instance. |
| `financials/provider_runner.py` | Parallel orchestration of fundamental providers with mappings, timeouts, `raw_providers`, and optional injection of `ValidationLayer` for real-time outlier filtering. |
| `financials/enrichment/adapters.py` | Concrete yfinance adapters and ticker normalization injected into use cases by the HTTP router. |
| `financials/enrichment/yfinance_enricher.py` | Deep enrichment from Yahoo: writes highlights, financial statements, earnings history, analyst ratings, ESG scores, earnings trend and shares history. |
| `financials/providers/` | Asynchronous connectors to ZoneBourse, Google Finance, Boursorama, Barrons, WSJ, MarketWatch, MorningStar, Investing, Gurufocus, Fortuneo, BourseDirect, MSN, Investir Les Echos, YahooFinance, JustETF, SECEdgar, OpenFIGI, and IndexConstituents. |
| `financials/providers/base.py` | The one HTTP layer of every provider: client creation, retries, per-provider concurrency, optional outbound proxy. |
| `financials/providers/JustETF_provider.py` | Legacy synchronous justETF scraper, kept and hardened; it relies on `fundamental/tools/ToolsBox.py`. The API uses `financials/providers/justetf.py`. |
| `fundamental/tools/ToolsBox.py` | Legacy helpers still used by the legacy justETF scraper and by the Google Finance to Yahoo ticker conversion. |
| `financials/numbers.py` | The one reader of numbers displayed on a page (`parse_number`, `find_number`). |
| `financials/fiscal_years.py` | Puts the statement rows of a fiscal year together (`fiscal_years`): the one way the valuation and the solvency ratios read `financial_statements`. |
| `financials/models.py` | Pydantic models returned by the providers (`StandardFinancials`, `FinancialMetrics`, `StockSummary`). |
| `financials/router.py` | Legacy `/stocks` routes. |
| `financials/service.py` | Separate aggregator for `/stocks/{ticker}/financials`. |
| `financials/formatter.py` | Builds the EODHD-shaped fundamentals document (`FinancialsFormatter.to_eodhd`): picks each value from Yahoo, the stored figures or a like-for-like scraped field, and records its origin in the `Sources` section. |
| `financials/exchange.py` | Structured management and mapping of financial exchange codes for GuruFocus, Yahoo Finance, and Google Finance. |
| `cache/service.py` | Synchronous Redis abstraction, TTL per category, JSON serialization. An entry that is not JSON is a miss: nothing read from Redis is unpickled. |
| `cache/adapters.py` | Best-effort asynchronous JSON cache handed to the fundamentals use case: Redis failures are absorbed, programming errors are not. |
| `database/lifecycle.py` | Builds and closes the asynchronous engine and session factory; infers `ASYNC_DATABASE_URL` from `DATABASE_URL`. |
| `valuation/dcf_service.py` | Financial valuation service implementing DCF models (FCF, EPS, DDM), on-the-fly WACC calculation (CAPM cost of equity and cost of debt), and sensitivity matrix generation. |
| `schemas/dcf.py` | Pydantic v2 schemas for custom DCF and WACC calculation requests, as well as detailed responses and sensitivity matrices. |
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
| `schemas/news.py` | Pydantic v2 schemas for the news system: `RawNewsItem`, `NewsArticleSchema`, `NewsResponse`, `NewsFeedResponse`, `NewsLanguage` / `NewsSentiment` enums. |
| `schemas/macro.py` | Pydantic v2 schemas for macroeconomic rates response. |
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
| `routers/macro.py` | HTTP routes for macro-economic data: the rates of FRED (USD) and of the ECB (EUR), by currency. |
| `historical/providers.py` | yfinance and TradingView connectors for retrieving historical bars, and the choice between them (`fetch`): yfinance with the verified symbol, TradingView only for a line quoted in the currency of the listing. |
| `historical/normalization.py` | Pure OHLCV validation, deduplication, and normalization rules. |
| `schemas/monitoring.py` | Pydantic v2 schemas for monitoring: `ProviderStatus`, `ProviderHealthSummary`, `ValidationResult`, `AlertSchema`, `DailyStatSchema`, `ProviderDetailResponse`, `HealthStatsResponse`, `AlertSeverity` / `AlertType` enums; re-exports `CanaryCheckResult` and `HealthStatus` from `monitoring/models.py`. |
| `zipline_bundle/__init__.py` | Public API of the Zipline data bundle (`FonRexBundle`, `fonrex_equities`, `register_fonrex_bundle`, `FonRexBundleDataSource`). |
| `zipline_bundle/data_source.py` | Zipline-free SQLAlchemy extraction layer for `prices_eod`: listing ranking, session-aligned OHLCV frames, deterministic `sid` allocation. |
| `zipline_bundle/bundle.py` | Zipline `ingest` callable orchestrating `AssetDBWriter`, `BcolzDailyBarWriter`, and `SQLiteAdjustmentWriter` from the data source output. |
| `zipline_bundle/extension.py` | Sample `~/.zipline/extension.py` that registers the `fonrex` bundle from environment variables. |
| `zipline_bundle/cli.py` | `python -m zipline_bundle preview` and `ingest`, for pipelines that cannot edit `~/.zipline/extension.py`. |

## Data Model

The identity of an instrument is separated into three levels:

- `assets` represents the canonical financial instrument. An ISIN should ideally identify a single instrument.
- `asset_listings` represents a listing of an instrument: ticker, exchange, currency, source, primary/active status.
- `asset_mappings` represents the provider-specific identifiers for an instrument or a listing.

This separation is necessary because:

- The same ISIN can be listed under multiple tickers, exchanges, or currencies.
- The same ticker can designate different instruments depending on the market, for example a stock and an ETF.
- Not all providers accept the same identifier: some want a Yahoo ticker, others an ISIN, others a URL or an internal code.

```mermaid
erDiagram
    ASSETS ||--o{ ASSET_LISTINGS : "has listings"
    ASSETS ||--o{ ASSET_MAPPINGS : "has global mappings"
    ASSET_LISTINGS |o--o{ ASSET_MAPPINGS : "has provider mappings"
    ASSET_LISTINGS ||--o{ PRICES_EOD : "has a price series"
    ASSETS ||--o{ PRICES_EOD : "instrument of the series"
    ASSETS ||--o{ FUNDAMENTALS : "has fundamentals (legacy)"
    ASSETS ||--o| FUNDAMENTALS_HIGHLIGHTS : "has highlights"
    ASSETS ||--o{ FINANCIAL_STATEMENTS : "has financial statements"
    ASSETS ||--o{ EARNINGS_HISTORY : "has EPS history"
    ASSETS ||--o| ANALYST_RATINGS : "has ratings"
    ASSETS ||--o| ETF_DETAILS : "has ETF details"
    ASSETS ||--o{ ETF_HOLDINGS : "has ETF holdings"
    ASSETS ||--o| ESG_SCORES : "has ESG scores"
    ASSETS ||--o{ EARNINGS_TREND : "has estimates"
    ASSETS ||--o{ OUTSTANDING_SHARES_HISTORY : "has capital history"
    ASSETS ||--o{ INGEST_LOG : "has ingestion logs"
    ASSETS ||--o{ PRICES_INTRADAY : "has intraday prices"
    ASSETS ||--o| REALTIME_SUBSCRIPTIONS : "has a realtime subscription"
    ASSETS |o--o{ NEWS_ARTICLES : "has news"

    MACRO_RATES_CACHE {
        int id PK
        string series_id
        string label
        numeric value
        string unit
        date observation_date
        timestamp fetched_at
    }

    USAGE_LOGS {
        int id PK
        string api_key_id "fingerprint of the key, never the key"
        string endpoint
        string method
        int status_code
        int latency_ms
        string provider_used
        bool cache_hit
        string cost_bucket
        string ip_address
        text user_agent
        timestamp created_at
    }

    PROVIDER_HEALTH_LOG {
        int id PK
        timestamp checked_at PK
        string provider_name
        string ticker
        string field
        numeric value_received
        numeric value_expected_min
        numeric value_expected_max
        numeric consensus_value
        numeric deviation_pct
        string status
        string check_type
    }

    PROVIDER_HEALTH_DAILY {
        int id PK
        string provider_name
        date date
        int checks_total
        int checks_ok
        int checks_outlier
        int checks_null
        int checks_timeout
        numeric success_rate
        int avg_latency_ms
        bool canary_passed
        bool is_healthy
    }

    PROVIDER_ALERTS {
        int id PK
        string provider_name
        string alert_type
        string severity
        text description
        string ticker
        string field
        numeric value_received
        string value_expected
        timestamp created_at
        timestamp resolved_at
        bool is_resolved
        text resolution_note
    }

    ASSETS {
        int id PK
        string ticker
        string isin
        string name
        string display_name
        string official_symbol
        string exchange
        string currency
        string quote_type
        string sector
        string industry
        string logo_path
    }

    ASSET_LISTINGS {
        int id PK
        int asset_id FK
        string ticker
        string exchange
        string currency
        string source
        bool is_primary
        bool is_active
    }

    ASSET_MAPPINGS {
        int id PK
        int asset_id FK
        int asset_listing_id FK
        string provider_name
        string provider_ticker
        string provider_url
        string source
        float confidence_score
        bool is_active
        int failure_count
        timestamp last_verified_at
    }

    PRICES_EOD {
        int asset_listing_id PK, FK
        string resolution PK
        timestamp time PK
        int asset_id FK
        float open
        float high
        float low
        float close
        float adj_close
        bigint volume
        bool adjusted
        string source
    }

    PRICES_INTRADAY {
        timestamp timestamp PK
        int asset_id PK, FK
        float open
        float high
        float low
        float close
        bigint volume
        string resolution
        string source
    }

    REALTIME_SUBSCRIPTIONS {
        int id PK
        int asset_id FK "unique"
        string ticker
        string tv_exchange
        string tv_symbol
        bool is_active
        timestamp subscribed_at
        timestamp last_tick_at
        bigint tick_count
    }

    FUNDAMENTALS {
        timestamp timestamp PK
        int asset_id FK
        numeric market_cap
        numeric pe_ratio
        numeric dividend_yield
        json extra_metrics
    }

    FUNDAMENTALS_HIGHLIGHTS {
        int id PK
        int asset_id FK
        timestamp fetched_at
        string source
        numeric market_cap
        numeric enterprise_value
        numeric pe_ratio
        numeric roe
        numeric roa
        numeric dividend_yield
        numeric beta
        bigint shares_outstanding
        numeric debt_to_equity_ratio
        numeric debt_to_assets_ratio
        numeric net_debt_to_ebitda
        numeric interest_coverage_ratio
        numeric actual_cost_of_debt
        string cost_of_debt_source
    }

    FINANCIAL_STATEMENTS {
        int id PK
        int asset_id FK
        string statement_type
        string period_type
        date period_end
        numeric revenue
        numeric ebitda
        numeric net_income
        numeric total_assets
        numeric total_equity
        numeric total_debt
        numeric operating_cashflow
        numeric free_cashflow
    }

    EARNINGS_HISTORY {
        int id PK
        int asset_id FK
        string period
        date period_end
        numeric eps_actual
        numeric eps_estimate
        numeric surprise_pct
    }

    ANALYST_RATINGS {
        int id PK
        int asset_id FK
        string consensus
        numeric target_mean
        numeric target_low
        numeric target_high
        int nb_analysts
        int strong_buy
        int buy
        int hold
        int sell
        int strong_sell
    }

    ETF_DETAILS {
        int id PK
        int asset_id FK
        date inception_date
        numeric net_expense_ratio
        numeric total_net_assets
        string replication_method
        numeric return_1y
        numeric return_3y
        numeric return_5y
    }

    ETF_HOLDINGS {
        int id PK
        int etf_asset_id FK
        string holding_ticker
        string holding_name
        numeric weight
        string sector
        string country
    }

    ESG_SCORES {
        int id PK
        int asset_id FK
        numeric total_esg
        numeric environment_score
        numeric social_score
        numeric governance_score
        int controversy_level
    }

    EARNINGS_TREND {
        int id PK
        int asset_id FK
        string period
        numeric revenue_avg
        numeric revenue_growth
        numeric eps_avg
        numeric eps_growth
    }

    OUTSTANDING_SHARES_HISTORY {
        int id PK
        int asset_id FK
        date date
        bigint shares
        string period_type
    }

    INGEST_LOG {
        int id PK
        int asset_id FK
        string ticker
        string resolution
        string source
        string status
        int records_added
        date from_date
        date to_date
        string error_msg
        int duration_ms
        timestamp created_at
    }

    NEWS_ARTICLES {
        int id PK
        int asset_id FK
        string title
        text summary
        string url UK
        string image_url
        string source
        string provider
        string author
        timestamp published_at
        timestamp fetched_at
        string sentiment
        numeric sentiment_score
        array related_tickers
        array related_isin
        string language
    }
```

Constraints, indexes, and compatibility:

- The diagram shows the columns that matter to understand the model, not every column: `models.py` is the reference.
- `AssetListing` is unique by `(asset_id, ticker, exchange, currency)` — constraint `uq_asset_listing_identity`, created with the table and re-asserted by migration 009.
- `AssetMapping` is unique by `(asset_listing_id, provider_name)` (constraint `uq_asset_listing_provider`). `asset_listing_id` may be empty: the mapping then applies to every listing of the instrument, and PostgreSQL does not treat two such rows as duplicates. `source` says where the identifier comes from (`csv_import`, `manual`, `isin_search`, `ticker_check`, `symbol_not_found`) and `last_verified_at` when it was last confirmed: see [Source Symbol](#3-source-symbol-historicalyahoo_symbolspy).
- `RealtimeSubscription` has a unique constraint `uq_realtime_sub_asset` on `asset_id`.
- `assets.isin` is protected by the unique partial index `uq_assets_isin_not_null` (`WHERE isin IS NOT NULL`), created by migrations 001 and 004. Migration 009 merges the duplicates left in older databases and creates the index where it was still missing. A single `Asset` per ISIN is guaranteed at the database level.
- The old columns `assets.ticker`, `assets.exchange`, and `assets.currency` remain for legacy compatibility, but reliable identity goes through `asset_listings`.
- `prices_eod` holds one row per **listing, resolution and trading session**: its primary key is `(asset_listing_id, resolution, time)`. Prices belong to a listing (a ticker on an exchange, in a currency), not to the instrument: the EUR and USD listings of one ETF are two series, and a weekly bar never replaces a daily one. `time` is the date of the session on its exchange, stored as midnight UTC of that date (8 January in Paris and in New York are both `2024-01-08 00:00+00`). `asset_id` is kept on the row for instrument-wide statistics, with the index `ix_prices_eod_asset_resolution_time` on `(asset_id, resolution, time)`.
- `prices_eod` is a **TimescaleDB hypertable** on `time`, compressed by `(asset_listing_id, resolution)` with a policy that compresses the chunks older than 14 days (migration 014).
- `prices_weekly` and `prices_monthly` are TimescaleDB continuous aggregates of the daily bars, per listing, refreshed once a day. `QueryService.get_history()` reads them only when no `1W` or `1M` row is stored for the listing.
- `prices_intraday` is a **TimescaleDB hypertable** partitioned daily (`chunk_time_interval => INTERVAL '1 day'`) and has a composite index `ix_prices_intraday_asset_ts` on `(asset_id, timestamp)`. An automatic retention policy purges candles older than 30 days.
- `ingest_log` has indexes on `asset_id` and `created_at` to efficiently track ingestion activity.
- `fundamentals_highlights` holds the last snapshot of an instrument (valuation, profitability, dividend, short interest, solvency), written by `YFinanceEnricher` (`financials/enrichment/yfinance_enricher.py`) and read by `DatabaseService.get_deep_fundamentals()`; `fetched_at` dates it. The enricher updates the row of the instrument or creates it: there is one row per instrument by convention, no constraint enforces it. `dividend_yield` is a ratio (`0.0032` for 0.32 %); migration 015 converted the percentages stored before.
- `fundamentals` is the legacy table of the first schema: nothing writes it any more, and its only reader (`DatabaseService.get_fundamental_data()`, as a fallback for an instrument without highlights) is not called by any route.
- `financial_statements` holds **one row per statement type**, fiscal period and frequency: the income statement, the balance sheet and the cash-flow statement of a fiscal year are three rows, each filling only its own columns. A calculation never reads "the latest row": it goes through `financials/fiscal_years.py`, which puts the rows of a year together (the debt of the balance sheet next to the interest of the income statement).
- `esg_scores` additionally contains 15 boolean columns naming the controversial activities of the company (tobacco, coal, gambling, etc.), from Yahoo Finance.
- `etf_details` and `etf_holdings` are read by `get_deep_fundamentals()` for an ETF, but nothing in the application writes them: they stay empty unless they are filled by hand. `/etf/{isin}/details` answers from JustETF and Redis, without these tables.
- `earnings_trend` stores consensus estimates over four time horizons: `0q`, `+1q`, `0y`, `+1y`.
- `news_articles` is unique on the `url` column (a unique constraint without a chosen name: PostgreSQL calls it `news_articles_url_key`), allowing the idempotent upsert `ON CONFLICT (url) DO UPDATE`. Three additional indexes cover `(asset_id, published_at)`, `published_at` (global feed), and `provider` (stats by source). Migration 010 asks TimescaleDB for a 90-day retention policy, which only applies to a hypertable: `news_articles` is a standard table, so **nothing purges old articles today**.
- `provider_health_log` is a **TimescaleDB hypertable** with automatic 30-day retention. Composite primary key `(id, checked_at)` imposed by TimescaleDB (the partitioning column must be in the PK). Index `(provider_name, checked_at)` and `(status)`. Each row represents a value check; `check_type` is `canary`, `realtime` or `consensus`.
- `provider_health_daily` contains a daily aggregate per provider, with unique constraint `uq_provider_health_daily(provider_name, date)`. Columns: counters by status, success rate, average latency, canary result, and `is_healthy` flag.
- `provider_alerts` stores alerts with index `(provider_name, is_resolved)` and `(severity, is_resolved)`. Two alert types are raised today, `canary_failed` and `high_outlier_rate`; `consecutive_nulls` and `latency_spike` are named in the schema but no code raises them. Canary alerts are auto-resolved when all checks pass.
- `macro_rates_cache` keeps the series read from FRED, unique by `(series_id, observation_date)`.
- `usage_logs` holds one row per API request, with indexes on `created_at` alone and combined with `endpoint`, `api_key_id` and `provider_used` (see [Cache And Logging](#cache-and-logging)).
- Two objects exist in the database without an ORM model: the `index_constituents` table (migration 003), which no code reads or writes — index constituents are fetched and kept in Redis — and the `fundamentals_v2` view (migration 002).

## Identity Resolution

`DatabaseService.get_asset_context()` is the central point for resolving an asset. It returns:

- `asset`: the ORM instrument.
- `listing`: the selected listing.
- `details`: the API profile ready to be exposed.
- `mappings`: the applicable active provider mappings, as a dictionary keyed by the provider name in lower case. A mapping attached to the selected listing replaces the instrument-wide mapping of the same provider.

Main rules:

- An ISIN search favors the corresponding instrument, then chooses a preferred listing.
- A ticker-only search is ambiguous: the active listings bearing the ticker are ranked, in this order: a stock before an ETF or a fund, a listing in USD before the others, a listing whose `source` is `import_assets` before the others (the value written by the legacy row-by-row import; the current importer writes `csv_import`, so this criterion no longer separates the listings of a catalogue), the primary listing before the secondary ones.
- The `exchange` and `currency` parameters explicitly narrow the search to a specific listing.
- When no listing matches, the instrument is searched by its own (legacy) ticker or official symbol, then by the ticker without its suffix (`AIR.PA` then `AIR`) — on the place the suffix names only (see below).
- Missing profile fields can be completed, but existing values are not overwritten.
- Enrichment checks ISIN and `quote_type` compatibility to avoid injecting stock metadata into a homonymous ETF.

This logic notably fixes cases where `TSLA` could refer to Tesla Inc. or an ETP/ETF bearing the same ticker.

**A suffix names a place** (`database/ticker_suffix.py`). The bare symbol is not an identity: `AIR` is Airbus in Paris and AAR Corp in New York. A lookup without the suffix therefore takes a listing only on the place the Yahoo suffix names: its exchange is one of the codes of that place (`.PA`: `XPAR`, `EPA`, `PAR`, `PA`…), or its exchange is empty or unknown and it is quoted in a currency of that place (EUR for `.PA`, GBP or GBX for `.L`). A listing on another known exchange or in another currency is never taken: `AIR.PA` without an Airbus listing answers `404` instead of AAR Corp. A suffix that names no exchange (`BRK.B` is a share class) gives no lookup without it. The rule is shared by the identity lookup above, the price series below, the valuation (`DCFService`) and the news (`NewsService`).

Prices follow a second, simpler rule, written once in `database/price_series.py` (`resolve_price_series` and its asynchronous twin): every API path that reads or writes `prices_eod` uses it, so a ticker cannot designate one listing when prices are written and another when they are read. For the requested ticker, then for the ticker without its suffix on the place that suffix names, it takes the listing bearing that ticker — the primary one first, then by currency and exchange in alphabetical order — and otherwise the preferred listing of the instrument whose own ticker it is. `currency` and `exchange` narrow the choice, and `isin` keeps the listings of one instrument only — a bare ticker may belong to several instruments (`NEM` is Newmont and Nemetschek), which the currency alone does not always tell apart; with `isin`, the fallback to the ticker without its suffix also stays within that instrument. `GET /eod` and `GET /ticker/{symbol}/history` return the `listing` they read (`ticker`, `isin`, `currency`, `exchange`). An instrument without any listing has no price series.

The two rules can therefore choose different listings for the same bare ticker: `/fundamental` prefers the USD listing, the price routes the primary listing. Passing `currency` or `exchange` removes the ambiguity between the listings of one instrument in both; `isin` (price routes) or an ISIN search (`/fundamental`) removes it between instruments.

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

OpenBB Workspace routes (`routers/openbb.py`). Each one calls the route function of the Fonrex route it adapts, with the same parameters, and reshapes the answer (see [Integrations](#integrations)):

| Method | Endpoint | Adapts | Widget type |
| --- | --- | --- | --- |
| GET | `/openbb/quote/{ticker}` | `/quote/{ticker}` | metric |
| GET | `/openbb/macro/rates` | `/macro/rates` | metric |
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

### `/fundamental` Endpoint

`GET /fundamental` accepts `ticker`, `isin`, `exchange`, `currency`, `provider` (one name, or several separated by commas, instead of all), `fmt` (`eodhd`, the default rendered document, or `raw`, the answers of the providers) and `nocache`. The complete answer is cached for one hour under `fundamental:{ticker}:{exchange}:{currency}:{fmt}:{providers}` — `providers` is `all` or the sorted list asked for, so the answer of one provider is never served to a request for all of them.

Simplified flow:

```mermaid
sequenceDiagram
    participant Client
    participant API as GetFundamentals (use case)
    participant DB as DatabaseService
    participant Sym as YahooSymbolResolver
    participant YF as Yahoo / yfinance
    participant Runner as FinancialProviderRunner
    participant P as Providers
    participant VL as ValidationLayer
    participant SEC as SEC EDGAR

    Client->>API: GET /fundamental?ticker=TSLA or ?isin=US88160R1014
    API->>DB: get_asset_context(ticker, isin, exchange, currency)
    DB-->>API: asset_profile + listing + mappings
    API->>Sym: Yahoo symbol verified for the listing
    Sym-->>API: symbol, or the reason why there is none
    opt Symbol verified, or ticker outside the catalogue
        API->>YF: profile enrichment with that symbol
        YF-->>API: compatible metadata
        API->>DB: update_asset_profile_from_metadata(), reloads the context
    end
    par Providers
        API->>Runner: run(ticker, isin, mappings, verified symbols)
        Runner->>P: async parallel calls
        P-->>Runner: metrics / URLs / identifiers
        Runner->>VL: validate_results(ticker, results)
        VL-->>Runner: filtered results (outliers → None)
        Runner-->>API: provider results + raw_providers
    and Insider transactions
        API->>SEC: Form 4 filings (15 seconds at most)
        SEC-->>API: transactions, or nothing
    end
    API->>DB: get_deep_fundamentals(asset_id)
    DB-->>API: stored statements, ratings, earnings
    API-->>Client: document rendered by FinancialsFormatter (fmt=eodhd) or raw results
```

The `FinancialProviderRunner` accepts an optional `validation_layer` parameter. When provided, the results of all providers are passed to `ValidationLayer.validate_results()` **after** the `asyncio.gather` and **before** returning to the caller. Invalidated values (out of range or consensus outliers) are reset to `None`, forcing the formatter to use the values of other providers.

The `FinancialProviderRunner` chooses the search term per provider in this order:

0. For Yahoo, the symbol verified for the listing (`historical/yahoo_symbols.py`): nothing derived from the ticker or the exchange replaces it. A listing of the catalogue without a verified symbol is **not** sent to Yahoo — its bare ticker may be another instrument (`SPFF` is a US fund on Yahoo) — and the Yahoo entry of the answer says why. The profile enrichment and `/fundamental/deep` follow the same rule. A ticker that is not in the catalogue is asked as typed.
1. For GoogleFinance and Gurufocus, the ticker built from the exchange of the profile (`EPA:AIR`, `AIR.PA`) when that exchange is known: it replaces a mapping. The same rule exists for Yahoo and only applies to a request that reaches it without a verified symbol.
2. Active `provider_url` mapping.
3. Active `provider_ticker` mapping.
4. The ISIN, for ZoneBourse, Investing, wallStreetJournal, Marketwatch, Fortuneo, BourseDirect, Boursorama, Gurufocus and InvestirLesEchos, when none of the above applied.
5. Specific provider default, for example the resolved ticker for MSN during an ISIN search.
6. Requested ticker.

The term that was used is reported per provider in `raw_providers`.

Providers are called in parallel with a timeout. An error or timeout on one provider does not block the other results. A scraped provider that answers with an ISIN other than the one of the instrument (a site searched by ticker may return a homonym) is reported as an error instead of being returned under the instrument's ISIN. The SEC lookup of insider transactions runs at the same time and never holds the answer more than 15 seconds. It is made for a share whose ISIN is American or whose listing is quoted in USD (Accenture has an Irish ISIN and files with the SEC), never for a fund, and for a ticker that is not in the catalogue only when it has no exchange suffix or the suffix `.US` or `.NAS`.

**Rendered document (`fmt=eodhd`, `financials/formatter.py`).** Each figure of `Highlights`, `Valuation`, `SharesStats`, `Technicals`, `SplitsDividends` and `AnalystRatings` is chosen in this order, and the choice is reported in the `Sources` section (`{"Highlights": {"PERatio": "YahooFinance", "PEGRatio": "database (2026-10-01)"}}`):

1. the Yahoo payload fetched for this request;
2. the figures stored in the database by the deep enrichment — an earlier Yahoo answer, reported as `database (date of the fetch)`; they answer when Yahoo is not asked (listing without a verified symbol) or lacks the figure;
3. for the trailing P/E, the earnings per share and the dividend yield only, the scraped providers that publish the same quantity (GoogleFinance, Barrons, Marketwatch, wallStreetJournal, Investing).

Boursorama and ZoneBourse publish estimates for the current fiscal year, GoogleFinance publishes revenue and margins of the last quarter: these are other quantities and are never used as a fallback; they remain available with `fmt=raw`. The document holds ratios: a percentage published by a source (Yahoo `dividendYield`, the fields declared in `monitoring/units.py`) is converted. Zero is a value; only a missing figure (or NaN, infinity, a text) falls back to the next source.

**Numbers displayed on pages** are read by `financials/numbers.py` (`parse_number` for a cell, `find_number` for a sentence): sign (including the typographic minus and accounting parentheses), thousands separators (any kind of space, comma or dot according to the page), scales (`k`, `M`, `Md`, `B`, `T`), currencies. A text that is more than a number is not half read.

### `/fundamental/deep` Endpoint

`GET /fundamental/deep` (`ticker` or `isin`, `refresh`, `sections`) returns structured data read by `DatabaseService.get_deep_sections()`: the last highlights (`FundamentalsHighlights`), quarterly/annual financial statements (`FinancialStatement`), actual vs estimated EPS history (`EarningsHistory`), and consensus analyst ratings (`AnalystRatings`). `sections` is `all` or a list among `highlights`, `statements`, `earnings`, `ratings`. The answer is kept 24 hours in Redis under `deep:{asset_id}` (an instrument, not a ticker: `AIR` is AAR Corp and the catalogue ticker of Airbus), always with every section: a request receives the sections it asked for, whatever the request that filled the entry. `refresh=true` ignores the cached answer and fetches again. The figures are fetched from Yahoo with the symbol verified for the listing (`meta.symbol`); without one nothing is fetched and `meta.note` gives the reason — the answer is then what the database already held (`meta.source` is `database`) and is not cached. `dividend_yield` is stored as a ratio.

### Specialized Endpoints

- **`GET /insider-transactions/{ticker}`**: queries `SECEdgar` for insider transaction declarations (Form 4). Result cached for 12 hours. The filings are listed by `data.sec.gov/submissions/CIK….json`; each Form 4 is read from its XML data file, named by `primaryDocument` without its `xsl…/` folder (that folder holds the version rendered for reading).
- **`GET /etf/{isin}/details`**: queries `JustETF` for ETF metadata (fees, AUM, replication method, 1/3/5 year performance, top holdings). An ISIN known as something other than an ETF is refused. The answer is cached 24 hours in Redis; it is **not** written to the `etf_details` and `etf_holdings` tables, which no code fills today.
- **`GET /index/{index_name}/constituents`**: queries `IndexConstituents` for S&P500, CAC40, NASDAQ100, or DAX components. The result is cached 7 days in Redis.
- **`GET /quote/{ticker}`**: returns a `QuoteSnapshot` from the Redis cache (`quote:ticker`). If missing, returns the delayed Yahoo price of the ticker **as typed** (`source: yfinance`, `is_realtime: false`); this fallback does not go through the verified symbol of the listing. A `GET` changes nothing by default: the realtime subscription is started in the background only with `subscribe_if_missing=true` and a full-access key; a read-only key never starts it. `GET /openbb/quote/{ticker}` (the OpenBB quote widget) never starts it, whatever the key: its ticker is streamed once subscribed with `POST /realtime/subscribe`.
- **`GET /quotes`**: batch version of the above for a list of tickers separated by commas, cut to the first 20. It never starts a subscription.

## Asset Import

### Custom CSV Asset Import (`import_assets.py`)

`import_assets.py` is an ISIN-deduplicated import pipeline for CSV files located in `data`. Without `--file` nor `--dir`, it imports `data/etf.csv` and `data/stocks.csv` when they exist, otherwise every CSV file of `data/isin_data/`.

Typical command in Docker:
```bash
# Standard import
docker compose exec fonrex-api python import_assets.py --file data/etf.csv

# Simulation without writing
docker compose exec fonrex-api python import_assets.py --file data/etf.csv --dry-run

# Every CSV file of a directory
docker compose exec fonrex-api python import_assets.py --dir data/isin_data

# yfinance enrichment only, for one instrument or for the instruments of a file
docker compose exec fonrex-api python import_assets.py --enrich-only --isin US0378331005
docker compose exec fonrex-api python import_assets.py --enrich-only --file data/etf.csv --limit 100
```

`--enrich-only` needs `--isin` or `--file`: it does not walk the whole catalogue. `--batch-size` (default 200) sets the number of rows per transaction and `--verbose` the detailed log.

The script resolves `--file` portably:
- Absolute path: used as is.
- Existing relative path: path relative to the current directory.
- Simple name: `/app/data/isin_data/<name>` in the container, then `/app/<name>`.

#### `AssetImporter` Pipeline

```
parse_csv(file_path)
  → List[CSVRow]  (validation + dedup on isin|ticker|currency key)
       ↓
AssetImporter.run(rows)
  → _process_batch()  per BATCH_SIZE_IMPORT chunk
       ↓ grouping by ISIN
    _upsert_asset()          — 1 Asset per ISIN (INSERT or UPDATE)
    _upsert_listing()        — 1 AssetListing per (asset_id, ticker, exchange, currency)
    _create_default_mappings() — YahooFinance + GoogleFinance
       ↓
    commit() if not dry_run
```

Validation steps in `parse_csv`:
- `name` not empty, `ticker` ≤ 20 characters.
- `isin`: regex `[A-Z]{2}[A-Z0-9]{10}`.
- `productType` ∈ `{STOCK, ETF}`, `currency`: 3 uppercase letters.
- Deduplication on key `(isin, ticker, currency)` — same ISIN / different currencies = two valid `CSVRow`s.

Primary listing logic (`determine_is_primary`):
1. First listing → `is_primary = True`.
2. Primary already exists → `False`.
3. Currency in `PRIMARY_CURRENCIES` {USD, GBP, JPY, CHF, CAD, AUD} and no primary yet → `True`.

The import itself makes no network call. The `YahooFinance` mapping it creates is the ticker of the file, marked `source = 'csv_import'` and not verified: it is replaced by the verified symbol the first time the listing is ingested or asked for its fundamentals (see [Source Symbol](#3-source-symbol-historicalyahoo_symbolspy)). `--enrich-only` is a separate step: it runs the deep enrichment of `financials/enrichment/yfinance_enricher.py` (highlights, statements, earnings, ratings…) for one instrument or for the instruments of a file. Yahoo is asked with the symbol verified for the primary listing of the instrument (the same resolver as the price ingestion); an instrument without a verified symbol is left as it is, and the log says why.

The module also holds the Yahoo metadata lookup (`fetch_yfinance_data`, used by the profile enrichment of `/fundamental`), a logo download that nothing calls (`download_logo`, `LOGO_TOKEN`) and the row-level helpers (`get_or_create_asset`, `get_or_create_listing`, `upsert_mapping`) kept for `tests/test_asset_listings_import.py`.

The import runs at API startup only when `SEED_ON_FIRST_RUN=true` and the `assets` table is empty (`entrypoint.sh`); otherwise it is run by hand or with `make db-seed`.

### ISIN Duplicates Cleanup (`scripts/clean_isin_duplicates.py`)

Standalone tool intended for existing databases before or after migration 009.

```bash
# Diagnostic (read only)
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py --diagnose-only

# Rehearsal: the cleanup is executed, then rolled back
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py --dry-run

# Cleanup + unique index (production)
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py --create-index

# Specific ISIN
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py \
  --isin US0378331005 --diagnose-only
```

Cleanup steps (`clean_duplicates`):
1. Re-parent `asset_listings` to the canonical asset `MIN(id)` per ISIN.
2. Delete `asset_mappings` that became duplicates after re-parenting.
3. Delete `asset_listings` that became duplicates.
4. Delete duplicated assets that are now without listings.

`--create-index` creates, when they are missing, the unique index `uq_assets_isin_not_null` and the constraint `uq_asset_listing_identity`.

## EOD And Historical Data

The `prices_eod` table (TimescaleDB hypertable) is the single source of end-of-day prices: every route reads it, and only the ingestion service writes it.

### Historical Data Paths

- **Read**: `GET /ticker/{symbol}/history` calls `QueryService.get_history()`, which reads the bars of the listing stored at the requested resolution; for `1W` and `1M`, when no bar is stored at that resolution, it reads the `prices_weekly` / `prices_monthly` aggregates computed from the daily bars. This route never ingests: a listing that was never ingested answers an empty list.
- **Read with ingestion**: `GET /eod/{ticker}` follows the same read path with a distinct response formatting (JSON/CSV, configurable order). When the read returns **nothing** for the requested range, it ingests the listing and reads again; stored prices that are merely out of date are served as they are.
- **Write / Ingestion**: `POST /historical/ingest` (unit) or `POST /historical/ingest/bulk` (multi-asset) calls `HistoricalIngestionService` to feed `prices_eod`. `scripts/ingest_all.py` does it for the whole catalogue.

The technical indicators read `prices_eod` at the requested resolution only: a weekly or monthly indicator needs bars ingested at `1W` or `1M`, it does not use the aggregates.

### Historical Ingestion Pipeline (`HistoricalIngestionService`)

When an ingestion process is launched for an asset, the following steps are executed:

```mermaid
flowchart TD
    Start[Start Ingestion] --> Resolve[1. Asset and Listing Resolution]
    Resolve --> Gap[2. Time Gap Detection]
    Gap --> Check{Up to date?}
    Check -- Yes --o LogUpToDate[Log status=up_to_date] --> End[End Ingestion]
    Check -- No --> Symbol[3. Verified Source Symbol]
    Symbol --> Fetch[4. Data Fetch with Fallback]
    Fetch --> Normalize[5. Normalization and Validation]
    Normalize --> Upsert[6. Batch Upsert ON CONFLICT in prices_eod]
    Upsert --> Invalidate[7. Redis Cache Invalidation]
    Invalidate --> LogSuccess[8. Logging in ingest_log] --> End
```

#### 1. Series Resolution (`_resolve_asset`)
The requested ticker is resolved to a price series — an instrument and one of its listings — by `database/price_series.py`: the listing bearing that ticker (primary first, then by currency, exchange and id), otherwise the preferred listing of the instrument whose own (legacy) ticker it is; then the same two lookups without the ticker suffix (`AIR.PA` → `AIR`), on the place the suffix names only. The history and EOD routes, the technical indicators (among active listings only) and the canary price ranges use the same rule, so the series that is read is the one that was written. The Zipline bundle has its own query (see its section). An instrument without any listing has no price series and cannot be ingested.

#### 2. Time Gap Detection (`_detect_gaps`)
The service queries `QueryService.get_history_range()` to obtain the minimum and maximum dates present in the database for this asset and resolution. 
- If no data is present, the system fetches the default history (up to 10 years).
- If the maximum date in the database corresponds to today or yesterday, the asset is marked as up to date.
- Otherwise, the system performs an incremental ingestion starting from the day after the maximum date up to today.
- A `from_date` older than the first stored date is fetched even when the series is up to date: the range from `from_date` to the day before the first stored date.
- With `force_refresh` nothing is compared: the requested range (ten years by default) and the stored range are fetched again in one piece.

#### 2b. One Adjustment Basis per Series (`historical/adjustment.py`)
Yahoo adjusts a whole history again after each split (`open`, `high`, `low`, `close`) and each dividend (`adj_close`). A series is written in one piece, then completed bar by bar; adding new bars to stored ones adjusted on an older basis would create a false return where they meet (about minus the dividend yield, or -75 % after a four-for-one split). `fetch_on_one_basis()` prevents it:
- `price_series_adjustments` records, per listing and resolution, the scheme of the stored bars (`close:splits;adj_close:splits+dividends`) and when the series was last fetched in one piece. It is written after a first ingestion and after each full fetch.
- To complete a series, the window is fetched together with the last five stored bars (the first five when older sessions are asked). If the source gives the same prices for them (relative difference up to 1e-4), only the sessions outside the stored range are written. If not — a split or a dividend since the last ingestion — the stored range and the window are fetched again in one piece and replace the stored bars; if that fetch fails, nothing is written and the ingestion fails with the reason.
- A stored bar without `adj_close` (TradingView) while the source now gives one also triggers a full fetch, so that the dividend-adjusted prices of the series are all on one basis.
- A series without a recorded scheme (stored before migration 016, when `close` held the dividend-adjusted price) is fetched again in full at its next ingestion; `scripts/ingest_all.py --force` does it for the whole catalogue.
- The result says why a whole series was fetched again in `note` (`Whole history fetched again: ...`).

#### 3. Source Symbol (`historical/yahoo_symbols.py`)
A ticker of the catalogue is not a Yahoo symbol: `SPFF` is a bond UCITS ETF in EUR in the catalogue and a US fund in USD on Yahoo; `EUCO` is `SYBC.DE` on Yahoo. Before any fetch, the symbol of the listing is resolved by `YahooSymbolResolver`:

1. a symbol already verified (or set by hand, `source = 'manual'`) in the `YahooFinance` row of `asset_mappings` is reused;
2. otherwise Yahoo is searched by the **ISIN** of the instrument; the candidates are ranked (same ticker first, traded lines before fund lines, then Yahoo's score) and the first one quoted in the **currency of the listing** with an existing price is kept;
3. failing that, a ticker that names its exchange (`GOVY.SW`) is tried as it is; a ticker without suffix only for an instrument with no ISIN or a North-American one (a Yahoo symbol without suffix is a North-American listing);
4. the answer is stored in the mapping: the symbol, `source` (`isin_search` or `ticker_check`) and `last_verified_at` on success; on failure the mapping is switched off (`source = 'symbol_not_found'`) and the listing is not looked up again for 24 hours, unless `force_refresh` is asked.

A listing without a verified symbol is not fetched from Yahoo: `GOVY` in CHF, for which Yahoo only has a EUR line, gets no price rather than the price of another line. An unreachable Yahoo concludes nothing and records nothing.

#### 4. Data Fetch with Fallback (`HistoricalMarketDataFetcher.fetch`)
- **Yahoo Finance**: The main connector calls `yfinance` (blocking) through `concurrency.run_sync()` with `auto_adjust=False`: `open`, `high`, `low` and `close` are the traded prices as Yahoo publishes them (adjusted for splits only), `adj_close` is Yahoo's close adjusted for splits and dividends (the close when Yahoo gives none), with `adjusted = true`. Returns including dividends are computed from `adj_close`. A bar is dated by its trading session: yfinance returns midnight in the time zone of the exchange, and the date of that local midnight is kept (converting the instant to UTC would date every European or Asian session the day before).
- **TradingView (Fallback)**: In case of failure or limitations from Yahoo Finance (in `auto` mode), the system switches to a temporary WebSocket client connected to TradingView's realtime streams, resolving the symbol via `tradingview-scraper`. Bars are asked with the `splits` adjustment: their `close` is comparable with Yahoo's, and their `adj_close` is empty (readers fall back on `close`). The line is searched by the verified Yahoo symbol without its suffix, or by the ticker when the listing has none (TradingView is therefore tried for a listing that Yahoo does not know), and accepted only if it is quoted in the currency of the listing; a ticker written `EXCHANGE:SYMBOL` is used as it is; the result reports the symbol used (`provider_symbol`) and why Yahoo was not the source (`note`). After an ingestion every cached answer computed from the prices of the ticker is dropped (`history`, `eod`, `technical`, `dcf` keys).

#### 5. Normalization and Validation (`_normalize_bars`)
Received candles undergo quality filters:
- Exclusion of null or `NaN` values on Open, High, Low, Close.
- Automatic correction of inconsistencies (e.g. if `high < low`, the values are swapped; ensure `high` and `low` contain the absolute extremes of the candle).
- A negative volume is set to zero, a missing one too.
- A second bar with the same date is dropped.

#### 6. Optimized Batch Upsert (`_upsert_prices_eod`, `historical/price_writer.py`)
To quickly insert thousands of rows without saturating memory, normalized candles are split into blocks (`batch_size` default to 1000) and inserted via a Native PostgreSQL instruction `INSERT ... ON CONFLICT (asset_listing_id, resolution, time) DO UPDATE`. With `force_refresh`, or when the whole series was fetched again (step 2b), the stored bars of the same listing and resolution inside the fetched range are deleted first, in the same transaction: the range ends up holding exactly the fetched bars (no leftover from an earlier adjustment basis or a wrongly dated row). 

#### 7. Redis Cache Invalidation
Every cached answer computed from the prices of the ticker is dropped via asynchronous scan and delete operations (`SCAN` + `DEL`): the `history:`, `eod:`, `technical:` and `dcf:` keys of that ticker, in both forms a key may take (`XETR:SPFF` and `XETR_SPFF`). The screener results (`technical:screen:*`) are not tied to a ticker and expire on their own.

#### 8. Ingestion Logging (`_log_ingest`)
A detailed execution log is stored in the `ingest_log` table (job status, source used, number of rows added, date range, execution duration in ms, and optional error message).

### Bulk Ingestion

The `ingest_bulk` method handles processing multiple tickers in parallel:
- Concurrency limitation via an asynchronous semaphore: the `concurrency` field of the request (default 5, between 1 and 20). `INGEST_CONCURRENCY` only applies to a caller that passes nothing.
- Introduction of a short random delay (0.1 to 0.5 s) before each ticker, to avoid IP bans by data providers.
- The tickers are ingested without a listing choice: a ticker shared by several listings designates its primary one. A single ingestion accepts `currency` and `exchange`.

Settings: `INGEST_YF_DELAY` (0.5 s, pause before falling back to TradingView), `INGEST_BATCH_SIZE` (1000 rows per insert). `INGEST_TV_DELAY` is read and used by nothing.

## Real-Time Streaming

The system integrates a real-time price streaming pipeline connected to TradingView WebSockets, distributed to clients via Redis Pub/Sub, and persisted in TimescaleDB.

### Streaming Architecture

```mermaid
sequenceDiagram
    participant Client as WebSocket Client (WS)
    participant FastAPI as FastAPI (routers/realtime.py + ws_manager)
    participant PubSub as Redis Pub/Sub (price:ticker)
    participant Cache as Redis Cache (quote:ticker)
    participant Worker as RealtimePriceWorker
    participant Pool as Scraper ThreadPoolExecutor
    participant TV as TradingView WebSocket
    participant DB as TimescaleDB

    Client->>FastAPI: Connection /ws/realtime/{ticker}
    FastAPI->>FastAPI: API key check (closed with code 1008 if refused)
    FastAPI-->>Client: Connection accepted, added to the group of the ticker

    FastAPI->>Worker: subscribe(ticker) if not active and the key may write
    alt Ticker not yet streamed
        Worker->>DB: Upsert in realtime_subscriptions (if the instrument is known)
        Worker->>Pool: run_in_executor(_blocking_stream)
        Pool->>TV: Connection and WebSocket subscription
    end

    FastAPI->>Cache: Fetch latest Snapshot
    Cache-->>FastAPI: JSON Snapshot (if existing)
    FastAPI-->>Client: Send immediate snapshot
    FastAPI->>PubSub: subscribe(price:ticker)

    Note over TV, Worker: Continuous tick stream loop
    Worker->>Pool: next tick of the stream
    Pool->>TV: waits for the next message
    TV-->>Pool: New Tick (OHLCV 1min)
    Pool-->>Worker: raw tick
    Worker->>Worker: Normalize & Validate tick (RealtimeTick)
    Worker->>Cache: SET quote:ticker (TTL 60s)
    Worker->>PubSub: PUBLISH price:ticker
    Worker->>DB: Upsert prices_intraday (if the instrument is known)
    Worker->>DB: Update realtime_subscriptions (tick_count, last_tick_at)

    PubSub-->>FastAPI: Pub/Sub message received
    FastAPI->>Client: Tick sent to this client
```

### Key Principles

1. **Worker Lifecycle**: The `RealtimePriceWorker` starts and stops cleanly via the FastAPI lifespan hook. On initialization, it queries the `realtime_subscriptions` table and automatically restores all active subscriptions.
2. **Lazy Load Subscription**:
   - Accessing `GET /quote/{ticker}` checks the Redis cache. If absent, it returns yfinance (delayed) prices; it triggers the subscription in the background only when asked with `subscribe_if_missing=true`.
   - Connecting to `WS /ws/realtime/{ticker}` immediately subscribes the ticker with the worker if necessary.
   - A read-only key never triggers a subscription, on either route: it is served what a full-access key (or `POST /realtime/subscribe`) already made the worker stream. On the WebSocket it first receives a `not_streaming` message when the ticker is not streamed.
   - `POST /realtime/subscribe` accepts 50 tickers per request.
3. **Throttling & Robustness (Backoff)**:
   - The thread pool is throttled to a maximum of simultaneous connections via a global semaphore (`TV_MAX_CONNECTIONS`, default 10).
   - In case of a network error or TradingView disconnection, the worker applies an automatic reconnection mechanism with exponential backoff (first delay `TV_RECONNECT_DELAY`, 5 seconds by default, doubled at each attempt up to a maximum of 60 seconds). A snapshot stays `REALTIME_QUOTE_TTL` seconds (60 by default) in Redis.
4. **One listener per connection**: each WebSocket connection subscribes to the `price:{ticker}` channel and sends every tick to its own client, once. The `ConnectionManager` (`ws_manager`) keeps the connections grouped by ticker for `GET /realtime/status`; its `broadcast` is no longer on the path of a tick (it delivered each tick once per connected client), and the server sends no ping of its own (`broadcast_ping` exists and is never called). A client may send `ping` (answered `{"type": "pong"}`) or `unsubscribe` (closes the connection).
5. **TradingView symbol**: the worker derives the TradingView exchange from the suffix of the ticker (`AIR.PA` → `EURONEXT:AIR`, `.DE` → `XETRA`…, `TV_EXCHANGE_MAP`); a ticker without suffix is taken for a NASDAQ line. Unlike the end-of-day ingestion, the realtime path does not verify the line against the ISIN and the currency of a listing: the instrument is looked up by its legacy ticker in `assets`, intraday candles are stored per instrument (`prices_intraday.asset_id`), and the listing choice of the price routes (`currency`, `exchange`) does not exist here.
6. **One process**: subscriptions, streams and the connection manager live in the memory of the API process, which is why the API runs a single worker by default.

## Technical Indicators

The system integrates an on-the-fly technical indicator calculation engine powered by the `pandas-ta` library and leveraging historical data stored in TimescaleDB.

### Supported Indicators & Categories
The engine dynamically calculates **18 technical indicators** divided into four fundamental categories:
- **Trend**: SMA, EMA, WMA, DEMA, TEMA, VWAP (VWAP available only on intraday data with daily reset).
- **Momentum**: RSI, MACD, Stochastic, CCI, ROC, MOM.
- **Volatility**: Bollinger Bands, ATR, Keltner Channels.
- **Volume**: OBV, A/D Line, MFI.

### Calculation Cycle
1. The ticker is resolved to its price series with `database/price_series.py`.
2. The last `limit` bars of the series are loaded (`TECHNICAL_DEFAULT_LIMIT`, 500 by default, between 10 and 5000), or the bars between `from_date` and `to_date`.
3. The engine refuses the calculation when the series is shorter than the indicator needs (`InsufficientHistoricalData`, HTTP 422).
4. `pandas-ta` computes the indicator on those bars. No extra bar is loaded before the requested window: the first values of a series, for which the indicator does not have enough history yet, are returned as `null`, and an exponential average (EMA, MACD, RSI) converges over the first bars of the window. A caller that needs converged values on a period asks for a longer window.

Domain errors are translated at the HTTP boundary: unknown indicator or resolution not supported by the indicator → 400, no data → 404, not enough data or failed calculation → 422.

### Performance Optimization
1. **Single DataFrame (Multi-calculation)**: The `calculate_multi` method allows calculating a list of indicators in a single pass. OHLCV data of the asset is read once from the database (TimescaleDB) as a `DataFrame`, then each indicator is calculated in memory on this same `DataFrame`, thus avoiding redundant SQL queries.
2. **Bounded concurrency**:
   - `POST /technical/batch` accepts at most `TECHNICAL_MAX_BATCH_TICKERS` tickers (20) and `TECHNICAL_MAX_BATCH_INDICATORS` indicators (10), and computes five tickers at a time (`Semaphore(5)`).
   - `GET /technical/screen` evaluates one indicator on a **sample** of the catalogue, not on all of it: the primary active listings of at most `2 × limit` instruments that have prices, `limit` listings at most (50 by default), 25 calculations at a time. The result is cached 15 minutes.

### Redis Cache Strategy
Indicator calculation results are transparently cached in Redis with time-to-live (TTL) tailored to the data granularity:
- **End-Of-Day (EOD) Data**: 3600 seconds for `1D`, 7200 for `1W`, 14400 for `1M` (`technical/catalog.py`).
- **Intraday Data**: 60 seconds for `1min`, 120 for `5min`.
- Only the single-indicator calculation (`GET /technical/{ticker}`, also used by the screener) reads this cache. The multi-indicator, chart and batch routes compute every time and store each indicator for the single route.
- The key is `technical:{TICKER}:{resolution}:{indicator}:{parameters}`, followed by the dates when given and by the number of bars loaded (`limit`): two windows are two entries.
- `TECHNICAL_CACHE_ENABLED=false` switches this cache off. A Redis read or write error is logged and the calculation goes on without cache.

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

## Fundamental Providers

Fundamental providers are declared in `main.py` in two tuples, imported when the module is loaded, and executed by `FinancialProviderRunner`: all the providers asked for a request run in parallel, each with a 12-second limit.

**Queried by `/fundamental`** (`PROVIDER_SPECS`): ZoneBourse, GoogleFinance, Boursorama, Barrons, wallStreetJournal, Marketwatch, MorningStar, Investing, Gurufocus, Fortuneo, BourseDirect, Msn, InvestirLesEchos, YahooFinance. The `provider` parameter restricts a request to some of them.

**Specialized providers** (`SPECIALIZED_PROVIDER_SPECS`), each behind its own route:
- `SECEdgar` – insider transactions (US only, Form 4)
- `JustETF` – ETF metadata (fees, AUM, replication, performance)
- `OpenFIGI` – identifier lookup (ISIN or ticker → FIGI, name, exchange code); it is loaded and no route calls it
- `IndexConstituents` – index constituents (S&P500, CAC40, NASDAQ100, DAX)

The providers of `/fundamental` return a `FinancialMetrics` Pydantic model (MorningStar returns a dictionary). By default the answer of a provider keeps the fields declared by the model and drops the extra ones the provider added; the extra fields are kept for a provider named in the `provider` parameter, and always for YahooFinance, whose extra fields feed the rendered document. The names above are the ones accepted by the `provider` parameter (in any case) and the keys of the answer.

Sites protected by an anti-bot service increasingly refuse a direct request from a personal connection (typically with a `403`); a provider then reports an error and the others answer. The outbound proxy setting of the HTTP layer (`FONREX_PROXY_URL`, see [News Providers](#news-providers)) is the place where such a relay plugs in.

`financials/service.py` provides a separate, older aggregator for `/stocks/{ticker}/financials`:

1. Providers by ticker, currently `YFinanceProvider`.
2. Eventual ISIN extraction.
3. Providers by ISIN, currently `BourseDirectProvider`.
4. Merging of the first non-null values into a `StandardFinancials` model.

This route asks Yahoo for the ticker as typed and uses neither the catalogue, the verified symbols nor the `ValidationLayer`; `/fundamental` is the maintained path.

## Financial News Service

The `NewsService` (`news/news_service.py`) is the central news aggregation component. It orchestrates the news providers listed below, deduplicates articles, persists them in the database, and exposes them via Redis.

### Pipeline Architecture

```mermaid
flowchart TD
    Client -->|GET /news/ticker| API[routers/news.py]
    API --> NS[NewsService.get_news]
    NS --> Cache{Redis Cache\nhit?}
    Cache -->|Yes| Response[NewsResponse cached=True]
    Cache -->|No| Resolve[Asset + Mappings Resolution]
    Resolve --> Gather[asyncio.gather — all providers]
    Gather --> P1[YFinance]
    Gather --> P2[Google Finance]
    Gather --> P3[ZoneBourse]
    Gather --> P4[Boursorama]
    Gather --> P5[Investing.com]
    Gather --> P6[MarketWatch]
    Gather --> P7[MSN Finance]
    P1 & P2 & P3 & P4 & P5 & P6 & P7 --> LangFilter[Language filter]
    LangFilter --> Dedup[Deduplication\nURL + Title similarity]
    Dedup --> Cut[Cut to limit]
    Cut --> Upsert[Upsert news_articles\nON CONFLICT url\nif the instrument is known]
    Upsert --> SetCache[SET Redis TTL 1800s]
    SetCache --> Response2[NewsResponse cached=False]
```

### Deduplication

Deduplication occurs in two passes:

1. **URL Normalization**: lowercase, UTM parameter removal (`utm_source`, `utm_medium`, etc.), `#` fragment removal, trailing slash removal. Of two articles pointing to the same normalized URL, the first one received is kept.
2. **Title Similarity**: `difflib.SequenceMatcher` compares normalized titles (lowercase, punctuation removed). If the ratio reaches the `NEWS_DEDUP_SIMILARITY` threshold (default `0.85`), the most recent article is kept. This method is external dependency-free.

Articles are sorted by `published_at DESC` after deduplication, then cut to the requested `limit`. Each provider is asked for twice that number. With `language`, an article whose language is known and different is removed before deduplication.

The articles are written to `news_articles` only when the ticker is known to the catalogue. The cache key is `news:{ticker}:{limit}:{language}` (`all` without a language filter): each language has its own entry, since the filter is applied before the articles are kept.

### Provider Mappings

The `NewsService` loads the active mappings of the instrument and hands each provider the one whose `provider_name`, in lower case, equals the name of the news provider. Three providers read it: ZoneBourse (`zonebourse`), Boursorama (`boursorama`) and Investing.com (`investing_com`). The first two share the mapping of the fundamental provider of the same name; Investing.com needs a mapping named `investing_com` — the `Investing` mapping of the fundamentals is not read — and returns nothing without it. The four other providers build their request from the ticker alone (exchange suffix, etc.).

### News Providers

| Provider | Source | Method | Language |
| --- | --- | --- | --- |
| `YFinanceNewsProvider` | Yahoo Finance | Native JSON `ticker.news` via `run_sync()`; asked with the ticker as typed | `en` |
| `GoogleFinanceNewsProvider` | Google Finance | HTML embedded JSON + BS4 fallback, relative dates | deduced from the ticker suffix |
| `ZoneBourseNewsProvider` | ZoneBourse | HTML BeautifulSoup, FR dates | `fr` |
| `BoursoramaNewsProvider` | Boursorama | HTML BeautifulSoup, `<time datetime>` ISO | `fr` |
| `InvestingComNewsProvider` | Investing.com | browser-like headers, Cloudflare challenge detection, `-news` slug | `en` |
| `MarketWatchNewsProvider` | MarketWatch | `countrycode` EU, `<time dateTime>` | `en` |
| `MSNFinanceNewsProvider` | MSN Finance | Internal JSON endpoint + HTML fallback | `en` |

All providers — news, fundamentals scrapers and specialised providers — inherit from `BaseFinancialProvider` (`financials/providers/base.py`), the single place where an HTTP client is created: `_session()` (several requests sharing cookies, used by the scrapers for "search then page"), `_get()`, `_get_json()`, `_post_json()` and `new_sync_client()` (legacy synchronous justETF scraper). The module applies one policy to all of them: three attempts by default, with an exponential pause (1 s, then 2 s) on network errors and on the statuses 429, 500, 502, 503 and 504 (the other statuses, 401/403/404 among them, are final), a limit of simultaneous requests per provider (`FONREX_PROVIDER_MAX_CONCURRENCY`, 4 by default, or a class-level `_semaphore`), and an optional outbound proxy (`FONREX_PROXY_URL`, restricted to some providers with `FONREX_PROXY_PROVIDERS`). `tests/test_provider_http_policy.py` fails if a provider creates its own client. No pause follows the last attempt, and a `Retry-After` longer than 10 seconds is capped. Helpers: User-Agent rotation, `_safe_float()` (the common number reader, `financials/numbers.py`). An exception in a news provider is caught by `_safe_fetch` and returns `[]` without blocking the others. The providers that read Yahoo through the `yfinance` library (fundamentals, news) do not go through this HTTP client: the policy above does not apply to them.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `NEWS_CACHE_TTL` | `1800` | Redis TTL in seconds |
| `NEWS_DEFAULT_LIMIT` | `20` | Default number of articles |
| `NEWS_MAX_LIMIT` | `100` | Maximum accepted limit |
| `NEWS_DEDUP_SIMILARITY` | `0.85` | Title similarity threshold for deduplication |

## Financial Valuation and DCF Models

The financial valuation module calculates the theoretical intrinsic value of an asset by crossing three classic Discounted Cash Flow (DCF) methodologies, supplemented by dynamic Weighted Average Cost of Capital (WACC) calculation and sensitivity analysis.

### Calculation Architecture

```mermaid
flowchart TD
    Client -->|GET or POST /dcf/ticker| API[routers/valuation.py]
    API --> Service["DCFService.compute_dcf"]
    Service --> DB["Data Loading from PostgreSQL"]
    DB --> HL["FundamentalsHighlights : Beta, MarketCap"]
    DB --> FS["FinancialStatements : Debt, EBIT, Interest, Taxes"]
    DB --> ET["EarningsTrend : growth consensus"]
    DB --> AR["AnalystRatings : analyst target"]
    
    Service --> FRED["FREDService: Dynamic Risk-Free Rate"]
    FRED --> WACC["On-the-fly WACC Calculation"]
    WACC --> Ke["Cost of equity: CAPM = Dynamic Rf + Beta * ERP"]
    WACC --> Kd["Cost of debt: Interest / Debt"]
    WACC --> Weight["Weightings: MarketCap vs Debt"]
    WACC --> Clamp["Regulatory clamping: 5% to 20%"]
    
    Service --> Models["Calculation of requested Models"]
    Models --> FCF["FCF Model: Discounted free cash flows + Gordon Growth"]
    Models --> EPS["EPS Model: EPS Growth + Terminal P/E multiple"]
    Models --> DDM["DDM Model: Discounted dividends"]
    
    Service --> Consensus["Consensus: Weighted average of models"]
    Consensus --> Output["DCFResult Response"]
```

The service computes from what the database already holds: the highlights, the annual financial statements, the earnings trend and the analyst ratings written by the deep enrichment (`GET /fundamental/deep`), and the latest daily close of the main listing. It fetches nothing itself: a ticker that was never enriched answers `404` with the name of the missing data.

The statements are read as **fiscal years**: the three rows of a year (income statement, balance sheet, cash flow) are put together by `financials/fiscal_years.py`, and the valuation looks back on the last five fiscal years. "The latest statement" is therefore a whole year — the debt and the cash of its balance sheet, the interest, the tax and the operating profit of its income statement, the dividends of its cash-flow statement. The enrichment stores those figures (`EBIT`, `Interest Expense`, `Tax Provision`, average share counts, dividends paid) since this rule exists; an instrument enriched before keeps them empty until `GET /fundamental/deep?refresh=true`, and the valuation uses its documented defaults meanwhile.

### Currency of the valuation

A valuation is made in the **currency of the statements** (`valuation/currency.py`): the enrichment records Yahoo's `financialCurrency` on every row of `financial_statements.currency` (migration 018 made the rows stored before unknown, `NULL`, instead of a `USD` written by default). When the statements name none, the valuation takes the currency of the listing of the share price, then the currency of the instrument, then USD. The answer gives it in `currency`, and the currency of the listing whose last close is the share price in `price_currency`.

- **Risk-free rate of that currency.** The cash flows are discounted with the rate of their currency: EUR uses the ECB AAA 10-year rate (`ecb_*`), USD the FRED 10-year Treasury rate (`fred_*`). Another currency, or a currency whose source gives nothing, uses `DCF_RISK_FREE_RATE` (`env_fallback`): a euro company is never discounted with the US rate. `wacc.risk_free_rate_currency` names the currency of the rate used, and is empty for the configured rate and for a rate given in the request.
- **Price in the same currency.** A price quoted in a minor unit is turned into its major one (`GBX`/`GBp` pence into GBP, `ZAC` into ZAR, `ILA` into ILS) before it is compared with the intrinsic value. A price in another currency (a US listing of a European company) is not converted: `consensus_upside_pct`, the `upside_pct` of each model and of each sensitivity cell are then empty, and `warnings` says why.
- **Upside of each model.** `models.{fcf,eps,ddm}.upside_pct` is the upside of that model's value over the price (it used to be 0).

### Supported Models Detail

1. **Free Cash Flow (FCF) Model**: 
   - Starts from the average of the free cash flows of the last three fiscal years (when none is published: operating cash flow minus capital expenditures, or net income) and projects it over $N$ years (default 5 years, configurable from 3 to 10 years). The growth rate is the historical FCF growth, failing that the revenue growth, bounded to 0–20 %, failing that 4 %.
   - Calculates terminal value applying the Gordon Growth Model (perpetual growth).
   - Deducts net debt (Total Debt - Cash) from the Enterprise Value to get the equity value, then divides by the number of outstanding shares to get the intrinsic value per share.

2. **Earnings Per Share (EPS) Model**: 
   - Estimates EPS growth over the projection period from historical growth rate and analyst consensus (`EarningsTrend`), moving linearly towards the terminal growth rate.
   - Calculates future terminal value using a terminal P/E multiple: the current P/E of the company (trailing, else forward) bounded to 10–30, or 15 when it is unknown.
   - Discounts the projected EPS and terminal price at the cost of equity (CAPM) to get the stock's fair value.

3. **Dividend Discount Model (DDM)**: 
   - Gordon discount model on projected dividends.
   - Requires actual dividend distribution by the company. Without a dividend the model cannot be applied: asked alone it is an error, and `GET /dcf/{ticker}/compare` then returns the two other models with a zero-valued DDM entry carrying the warning.

### FRED Macro-Economic Service

The `FREDService` provides the 10-Year Treasury Constant Maturity Rate (`DGS10`), used as the risk-free rate in WACC calculations. It looks, in this order, at Redis (6 hours, `MACRO_RATES_CACHE_TTL`), at the value stored in `macro_rates_cache` when it was read from FRED less than that lifetime ago, then at the FRED API (`FRED_API_KEY`). A stored value that is older is only what is left when FRED cannot be asked (no key) or does not answer. An observation FRED already gave is confirmed in place — value and date of reading — instead of adding a row. Rates are ratios (`0.0412` for 4.12 %, `unit` = `ratio`; rows stored before say `percent` and are read as ratios) and may be zero or negative. Each rate says how fresh it is (`freshness`): `live` (read from FRED for this answer), `cached` (Redis, or stored and read from FRED less than one cache lifetime ago) or `stale` (an older stored value: no key, or no answer; it is not put in Redis, so FRED is asked again next time). Without any value, the risk-free rate is `DCF_RISK_FREE_RATE` (4 % by default, read once at start-up). The WACC reports the rate used (`risk_free_rate`), its source (`risk_free_rate_source`: `fred_live`, `fred_cached`, `fred_stale`, `env_fallback` or `client_override`) and its observation date (`risk_free_rate_date`).

The `ECBService` reads the series of the European Central Bank Data Portal (`https://data-api.ecb.europa.eu/service/data/{flow}/{key}?lastNObservations=5&format=csvdata`, free and without key; `ECB_API_URL` replaces the address) through the same cache as FRED (Redis key `macro:ecb:{flow.key}`, `macro_rates_cache` with `source = 'ecb'`). It keeps the latest observation with a value: the 10-year spot rate of the AAA euro area government yield curve (`YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y`, the euro risk-free rate), the deposit facility rate (`FM.D.U2.EUR.4F.KR.DFR.LEV`) and the Composite Indicator of Systemic Stress (`CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX`, an index stored as published). `get_euro_risk_free_rate()` reports `ecb_live`, `ecb_cached` or `ecb_stale`, or nothing when no value is known: the caller chooses the fallback.

`GET /macro/rates` gives the rates of both sources, each tagged with its `source` (`fred`, `ecb`) and its `currency` (`USD`, `EUR`): `rates` lists the US 10-year Treasury rate, then the euro AAA 10-year rate, the deposit facility rate and the CISS (an index, not a ratio). `currency=USD` or `currency=EUR` keeps the series of one source; another currency answers `422`, as no source publishes its rates. `risk_free_rate` is the 10-year rate of the currency asked, the US one when none is asked (the answer before the ECB was a source). A source that did not start is left out (`503` when it is the only one asked, or when none started). `GET /openbb/macro/rates` takes the same `currency` (empty: both) and shows one card per series: rates as percentages, the CISS with four decimals, the date of the observation and `stale` when the value is an older stored one.

### Weighting and Consensus

The final intrinsic value (consensus) is calculated by combining active models according to default or custom weights in the request:
- **Default weightings**: FCF (50%), EPS (30%), DDM (20%). If the DDM model is impossible (no dividends), its weighting is proportionally redistributed or excluded with readjustment of remaining weights.

### Sensitivity Analysis

The service provides the `compute_sensitivity` method exposed on `GET /dcf/{ticker}/sensitivity`:
- Generates a two-way matrix varying the WACC rate (Y axis) and terminal growth rate (X axis).
- Provides for each cell the intrinsic value and the upside/downside potential % relative to the current closing price.

The current price is the last daily close stored for the preferred listing of the instrument; without stored prices it is estimated from the stored highlights (P/E × EPS, otherwise the middle of the 52-week range).

### Protection and Resilience

The engine incorporates safeguards against mathematical anomalies:
- **WACC Clamping**: The calculated WACC is limited within the `[5%, 20%]` interval to prevent extreme financial structure or missing data from totally distorting the calculations.
- **Discount rate vs Terminal Growth**: If the terminal growth rate comes within half a point of the discount rate (the WACC for the FCF model, the cost of equity for the EPS and DDM models) — which would make the denominator of the Gordon formula tiny or negative — it is capped at that rate minus 0.5 % and a `warning` is added to the result.
- **Missing inputs**: beta defaults to 1, the tax rate to 25 % when it cannot be computed or falls outside 0–100 %, the cost of debt to the risk-free rate plus 2 points when interest or debt is missing; a negative intrinsic value is reported as 0 with a warning.

### Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `DCF_CACHE_TTL` | `21600` | Redis cache TTL in seconds (6 h) |
| `DCF_DEFAULT_PROJECTION_YEARS` | `5` | Default number of projection years |
| `DCF_RISK_FREE_RATE` | `0.04` | Risk-free rate used when the source of the currency of the statements (FRED for USD, the ECB for EUR) gives none and none is stored, and for every other currency; as a ratio (`0.04` = 4 %; between -0.1 and 0.5) |
| `DCF_EQUITY_RISK_PREMIUM` | `0.055` | Default equity risk premium (e.g. 5.5%) |
| `DCF_TERMINAL_GROWTH_RATE` | `0.025` | Perpetual terminal growth rate (e.g. 2.5%) |

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

**Unit normalisation** — `FIELD_RANGES` and `CANARY_ASSETS` are expressed as ratios, while scraped providers return some fields as displayed percentages (`3.45` for 3.45 %). `monitoring/units.py` declares, per provider, which fields are percentages (`PROVIDER_PERCENT_FIELDS`); the `ValidationLayer` and the `CanaryMonitor` convert them with `to_ratio()` before any range, consensus or canary check. Logged values are the normalised ratios; the provider payload returned by the API keeps its native unit.

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
| `TSLA` | pe_ratio, beta, price | pe: [30, 300], beta: [1.5, 3.5] |

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
- User documentation: [integrations/openbb/README.md](integrations/openbb/README.md).

### Google Sheets Connector

`fonrex-sheets-connector/` is a Google Apps Script (`Code.gs`) bound to a spreadsheet template; it is not part of the Python application and is not deployed with it.

- Google runs the script on its own servers, which cannot reach `localhost`: the user exposes the instance through a tunnel of their choice (zrok, Cloudflare Tunnel, Tailscale Funnel…) and gives the script that HTTPS URL and a key. Both are stored in the Apps Script *user properties* of the Google account, never in a cell.
- The script only sends `GET` requests, with `Authorization: Bearer <key>`, to three routes: `/fundamental/deep?ticker=…`, `/dcf/{ticker}` and `/technical/{ticker}/multi?indicators=…` (menu refreshes), plus `/fundamental?ticker=…` for two of the four custom formulas. A key of `FONREX_READ_ONLY_API_KEYS` is what it should be given: it cannot clear the cache, clean the database, ingest or subscribe.
- The manifest (`appsscript.json`) has no fixed list of callable hosts, since the URL is the user's; `tests/test_docs_consistency.py` fails if a hosted API host comes back in the script or the documents.
- User documentation: [fonrex-sheets-connector/README.md](fonrex-sheets-connector/README.md).

### Outbound Relay (not part of this repository)

Sites increasingly refuse direct requests from a personal connection. The planned answer, Fonrex Relay, is a separate project still in development: a relay of pages that an instance may route its provider requests through. It neither stores nor serves financial data, and Fonrex does not depend on it. The only part that lives here is the generic outbound proxy setting of the provider HTTP layer (`FONREX_PROXY_URL`, optionally limited to some providers with `FONREX_PROXY_PROVIDERS`), which works with any HTTP proxy.

## Tests

Project unit and integration tests are based on `pytest`. The suite never talks to a real service: `tests/conftest.py` clears the credentials of the developer's shell, points Redis and the database to unreachable addresses, and makes any real Yahoo lookup fail (`tests/test_suite_isolation.py`). The only tests that need a server are the database tests, run on a throwaway TimescaleDB.

General command:

```bash
PYTHONPATH=. pytest
```

### Quality Chain and CI

The local `make ci` command is also the GitHub Actions CI entry point (`.github/workflows/quality.yml`). It successively executes:

1. Strict Ruff on style errors, full Pyflakes, imports, exception chaining, and safe Python modernizations.
2. Annotation checking on application boundaries.
3. `compileall` on the repository excluding temporary environments and directories.
4. Unique Alembic head verification.
5. Tests with blocking warnings, branch coverage, XML/JSON reports, minimal 70% global threshold, and a dedicated floor per module in `scripts/check_coverage_distribution.py`: the critical application modules and every module of `financials/providers/`. A provider module without a declared floor fails the gate.

The coverage thresholds and Ruff selection constitute a progressive foundation: they must never be lowered. A floor is the measured coverage rounded down and is raised whenever the coverage of its module rises; the CI run summary lists the floors that lag behind and shows the coverage per module on each pull request. Development dependencies are isolated in `requirements-dev.txt`.

Versions are locked: `requirements.txt` and `requirements-dev.txt` declare ranges, `requirements.lock` and `requirements-dev.lock` record the exact versions and hashes resolved from them for Python 3.12–3.13 (`make lock`, `[tool.uv]` in `pyproject.toml`). The Docker image, the CI and `make install-dev` install the lock files with `pip --require-hashes`, so the gate tests the packages that the image runs. `tests/test_dependency_lock.py` fails when a lock no longer matches its requirements file or is no longer what gets installed.

Test coverage, by theme:

**Identity, catalogue and prices**

- `tests/test_asset_identity.py`: asset/listing resolution, profile enrichment, metadata compatibility.
- `tests/test_import_assets.py`: complete `AssetImporter` pipeline — `parse_csv` (validation, dedup, normalization), `determine_exchange`, `determine_is_primary`, `AssetImporter` (multi-listing, idempotency, dry-run, default mappings, `ImportStats`).
- `tests/test_asset_listings_import.py`: multi-listing import for the same ISIN, mappings, fallback logos, Yahoo search by ISIN.
- `tests/test_price_series.py`: a price series belongs to one listing and one resolution and is dated by session — ticker resolution rule, session dates of fetched bars for exchanges on both sides of Greenwich, listings and resolutions that no longer overwrite each other, forced refresh replacing a range, history read per listing.
- `tests/test_ticker_suffix.py`: `AIR.PA` is Airbus in Paris and never AAR Corp (`AIR` in New York) — exchange codes and currency of a place, another European exchange, share classes, for the price series, the identity lookup, the valuation and the news.
- `tests/test_yahoo_symbols.py`: the source symbol of a listing is verified, never guessed — real cases of the ETF catalogue (homonym in another currency, dead line ranked first by Yahoo, ticker that differs from the Yahoo symbol, listing with no line in its currency), reuse and retry of a stored answer, TradingView line checked by currency, listing chosen in the request. An autouse fixture (`tests/conftest.py`) makes any real Yahoo lookup from a test fail.
- `tests/test_import_enrichment.py`: the enrichment started by the import asks Yahoo with the symbol verified for the primary listing, and skips an instrument that has none.
- `tests/test_historical_ingestion.py`: candle normalization, an ingestion with a simulated yfinance, removal of every cached answer computed from the prices. Gap detection has no test of its own: the tests that run an ingestion replace it.
- `tests/test_historical_providers.py`: the Yahoo and TradingView fetchers — failures returned as provider errors, prices kept to eight significant digits, TradingView frames, symbol resolution.
- `tests/test_timescale_integration.py`: the real migrations on a real TimescaleDB: migration of old-shape prices including compressed chunks, key, compression and aggregates, upsert into a compressed chunk, reads independent of the session time zone, stored dividend yields converted to ratios (015), cleanup of compressed chunks, downgrade and upgrade, a migration that waits for a session holding the prices without holding them itself. Skipped unless `FONREX_TEST_DATABASE_URL` is set; the CI sets it and starts a `timescaledb` service of the image of `docker-compose.yml`, and `make test-db` runs them locally on a throwaway container of that image.
- `tests/test_ci_workflow.py`: the CI service, `make test-db` and `docker-compose.yml` use the same database image, and the database tests cannot be skipped on GitHub Actions.
- `tests/test_migrations.py`: despite its name, it does **not** run the migrations: it creates the schema from the ORM models on SQLite (tables, columns and unique constraints of the fundamentals tables), validates the Pydantic schemas of the deep fundamentals, and checks that the Alembic files exist and can be imported. The migrations themselves are executed by `tests/test_timescale_integration.py`.
- `tests/test_alembic_authority.py`: Alembic is the only schema authority — no `create_all` fallback in the runtime, a database is accepted only at the current revision.
- `tests/test_database_cleanup.py`: bounds and dry run of `POST /database/cleanup`.
- `tests/test_database_components.py`, `tests/test_monitoring_repository.py`, `tests/test_async_database_url.py`: the database facade and its components, the monitoring repository, the inference of the asynchronous database URL.
- `tests/test_zipline_bundle.py`: unit tests exercising the Zipline bundle without requiring `zipline-reloaded` — SQLite in-memory `prices_eod`, primary listing ranking, ticker whitelist filter, session alignment via a `sessions_in_range` stub, `adj_close` precedence over `close`, NaN volume normalisation, empty-input fall-through, default database address, and orchestration of fake `AssetDBWriter` / `BcolzDailyBarWriter` / `SQLiteAdjustmentWriter` writers by `FonRexBundle.ingest`.

**Fundamentals and providers**

- `tests/test_provider_runner.py`: parallel execution, correct deserialization of mappings and `provider_url`, timeouts, provider mappings, MSN default for ISIN search.
- `tests/test_use_cases.py`: the use cases without HTTP — fundamentals (verified Yahoo symbol, refused listing, identity, enrichment, cache), deep fundamentals, specialised lookups, real-time quotes, with injected ports.
- `tests/test_financials_formatter.py`: the rendered `/fundamental` document — source of each figure, stored figures when Yahoo is not asked, ratios, zero kept as a value.
- `tests/test_numbers.py`: the common reader of displayed numbers (signs, thousands separators, scales, currencies, dates and double figures refused), and no provider converts a text by itself.
- `tests/test_yfinance_enricher.py`: deep fundamentals enrichment — highlights, statements, earnings, ratings, full run and partial failure. ESG scores, earnings trend and shares history have no test.
- `tests/test_solvency_ratios.py`: solvency ratios and weighted average cost of debt computed by the enricher.
- `tests/test_provider_real_pages.py` and `tests/test_provider_parsing_helpers.py`: provider parsers run on reduced extracts of real pages (`tests/fixtures/providers/`), with the expected values recorded next to each extract.
- `tests/test_provider_network.py`, `tests/test_base_provider_http.py` and `tests/test_provider_http_policy.py`: symbol search, download, retries and error handling of every provider against a simulated network (`fake_network` fixture, no real request); one HTTP policy, and no provider creating its own client.
- `tests/test_provider_units.py`: percentages returned by providers are normalised to ratios before validation and canary checks.
- `tests/test_barrons_provider.py`, `tests/test_investir_les_echos_provider.py`, `tests/test_fortuneo_provider.py`, `tests/test_wall_street_journal_provider.py`: choice of the search result of the site, and metrics returned from it when the page is blocked.
- `tests/test_justetf_provider.py`: `JustETFProvider` — percentage and amount parsing, answer of the API, fallback on the page.
- `tests/test_justetf_scraper.py`: the legacy scraper of justETF profile pages on real extracts (French and English pages give the same figures, the page of another ETF is rejected, errors return only the ISIN).
- `tests/test_sec_edgar_provider.py`: Form 4 insider transactions extraction from SEC EDGAR.
- `tests/test_openfigi_provider.py`: OpenFIGI lookup by ISIN, batches of 100, request body and headers with and without an API key.
- `tests/test_index_constituents_provider.py`: parsing of the Wikipedia pages of the S&P 500 and of the CAC 40, network failure, dispatch by index.
- `tests/test_google_url_provider.py`: not a test. It is a script to run by hand that asks the real Google Finance; pytest collects nothing from it.

**News, indicators, valuation, realtime, monitoring**

- `tests/test_news_service.py`: the YFinance, Google Finance and ZoneBourse news providers (fetch, parsing, silent exceptions), URL+UTM+trailing slash deduplication and title similarity (`difflib`), Redis cache (hit/miss), resilience (one provider crashes → others continue), PostgreSQL upsert, language filter, and URL normalization.
- `tests/test_investing_provider.py`, `tests/test_marketwatch_provider.py`, `tests/test_boursorama_provider.py`, `tests/test_msn_provider.py`: the **news** providers of these sites (address, page and date parsing, Cloudflare detection), not their fundamentals.
- `tests/test_technical_indicators.py`: RSI, SMA, EMA, MACD, Bollinger Bands on known price patterns, number of empty leading points, error handling (unknown indicator, too few bars), Redis cache, VWAP, and screener.
- `tests/test_dcf_service.py`: WACC calculation (CAPM, cost of debt, 5%-20% bounds), FCF, EPS, and DDM projection and discount models with their fallbacks, safeguards against division by zero or negative denominators (when growth exceeds WACC), and sensitivity matrices shape, on statements given one row per year. The weighted consensus has no assertion of its own.
- `tests/test_fiscal_years.py`: statements stored by the enrichment as they really are (three rows per fiscal year), then read by the valuation, the sensitivity matrix and the solvency ratios — five fiscal years, cost of debt and tax rate from the statements, free-cash-flow base over three years, net debt, dividends.
- `tests/test_dcf_wacc_dynamic_sources.py`, `tests/test_fred_service.py`: source of the risk-free rate and of the cost of debt (FRED, stored value, request override, fallback); FRED series read from Redis, from the API, and on a day without observation; a stored rate that is old is refreshed, a recent one is not asked again, and it remains the fallback when FRED fails.
- `tests/test_dcf_currency.py`: currency of the valuation (statements, then listing, then instrument), pence turned into pounds, no upside across currencies, the risk-free rate chosen by currency (ECB for EUR, FRED for USD, the configured rate otherwise), on statements stored by the enricher.
- `tests/test_ecb_service.py`: ECB answers read in CSV (percent stored as ratios, the CISS as published, the latest observation with a value), through the shared cache (live, cached, stale), and the euro area rates listed with their source and currency.
- `tests/test_macro_rates_route.py`: `GET /macro/rates` with and without `currency` (both sources, one source, a currency without source, a source not started) and the OpenBB cards, one per series.
- `tests/test_macro_rate_cache.py`: the shared cache of the macro sources when a layer fails — Redis down for reading or writing, a database that refuses reads and writes, a source that cannot be asked — and odd ECB answers (a value that is not a number, rows out of order, an unknown series).
- `tests/test_realtime.py`: complete behavior of realtime streaming (subscribe, unsubscribe, restore, process_tick), WebSocket connection manager, a tick delivered once to each client of a ticker, REST quote endpoints, and fallback policies.
- `tests/test_monitoring.py`: unit tests covering the `ValidationLayer` (range checks on exact bounds, outlier consensus, filtered median, `validate_results` integration with outlier/out-of-range rejection, never-raises, dict/Pydantic field extraction), the `CanaryMonitor` (EU-only compatibility, canary checks ok/out-of-range/null/boundary, daily stats aggregation, Redis update via `fakeredis`), Pydantic schemas (`ProviderStatus`, `ProviderHealthSummary`, `DailyStatSchema`, `HealthStatsResponse`), and router endpoints (`TestClient`: 503 without config, canary trigger, Redis read via `httpx.AsyncClient`).
- `tests/test_cache_service.py`: readable keys and duration per category, purge of a ticker, a pickled or unreadable entry is a miss and runs nothing, no application module imports `pickle`.
- `tests/test_cache_keys.py`: a cached answer is served only to the request it answers — providers of `/fundamental`, sections of `/fundamental/deep`, language of the news, number of insider filings, window of an indicator.
- `tests/test_usage_logging.py` and `tests/test_usage_recorder.py`: usage log persistence, deferred batch writes, IP handling, excluded paths, retention purge, and a shutdown that waits for the write in progress, even when it fails.
- `tests/test_router_integration.py`, `tests/test_extracted_routers.py`: the DCF, news and other routers through `TestClient`; each path registered once, services read from the application state.
- `tests/test_openbb_adapters.py`, `tests/test_openbb_integration.py`: OpenBB formatters, `widgets.json` / `apps.json` consistent with the routes, every `/openbb` route function called with its declared parameters, CORS and `X-API-KEY` header (see [Integrations](#integrations)).

**Guards**

- `tests/test_auth_defaults.py`: authentication enforced by default, explicit opt-out, read-only keys.
- `tests/test_env_settings.py`, `tests/test_docker_image.py`, `tests/test_coverage_gate.py`, `tests/test_docs_consistency.py`, `tests/test_dependency_lock.py`: guards — every setting of `.env.example` is read by the code, the Docker image holds what the application needs, every provider has a coverage floor, the figures and the tables of the documents (routes, migrations, modules, cache lifetimes, canary assets) match the code, and the lock files match the requirements and are what Docker and the CI install.
- `tests/test_exception_boundaries.py`, `tests/test_async_boundary.py`, `tests/test_warning_hygiene.py`, `tests/test_suite_isolation.py`: architecture rules — typed exceptions where the split into ports exists, no framework import in the technical and monitoring cores, size limits of the orchestrators, a single bridge to blocking code, no `datetime.utcnow()` nor legacy `Query.get()`, and a suite cut off from the developer's Redis and database.
- `tests/test_coverage_balance.py`: boundaries that had little coverage (resilient cache adapter, technical repository, profile enricher).

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

## Zipline Bundle (Backtesting Integration)

The `zipline_bundle/` package exposes Fonrex historical data to the [`zipline-reloaded`](https://github.com/stefan-jansen/zipline-reloaded) backtesting engine without exporting intermediary CSV files or shipping a parallel dataset.

### Runtime Boundary

- The FastAPI runtime **never** imports `zipline`. `zipline-reloaded` is an optional developer dependency documented in [docs/zipline-bundle.md](docs/zipline-bundle.md) and is not listed in `requirements.txt` to keep the API container free of `bcolz`, `empyrical`, `tables`, and other heavy transitive dependencies.
- `zipline_bundle.data_source` is Zipline-free by design: it only depends on `pandas` and `sqlalchemy` (both already required by Fonrex), which allows the CI to exercise the extraction pipeline in isolation.

### Ingestion Flow

```mermaid
flowchart LR
    subgraph "Zipline CLI"
        CLI["zipline ingest -b fonrex"]
        Ext["~/.zipline/extension.py"]
    end
    subgraph Fonrex
        Data["FonRexBundleDataSource\n(SQLAlchemy sync)"]
        Bundle["FonRexBundle.ingest"]
    end
    subgraph "Zipline Writers"
        Assets["AssetDBWriter\nassets.sqlite"]
        Bars["BcolzDailyBarWriter\ndaily_equities.bcolz"]
        Adj["SQLiteAdjustmentWriter\nadjustments.sqlite"]
    end
    subgraph "Fonrex Storage"
        DB[(PostgreSQL / TimescaleDB\nprices_eod)]
    end

    CLI --> Ext --> Bundle
    Bundle --> Data
    Data --> DB
    Bundle --> Assets
    Bundle --> Bars
    Bundle --> Adj
```

### Design Decisions

- **Reads `prices_eod` at daily resolution only.** The Zipline daily bar writer accepts one bar per session; minute-level data (`prices_intraday`) is not exposed for now.
- **One listing per instrument.** Among the listings that have daily prices in the window, the bundle keeps one per instrument: the primary one, then an active one, then the lowest `asset_listings.id`. It has its own query for this and does not use `database/price_series.py`; the other listings of an instrument (its other currencies) are not exposed.
- **Deterministic `sid` allocation.** `sid` values are assigned in the alphabetical order of the symbols, starting at 0. Two consecutive ingests over the same window produce identical `sid` mappings. A listing whose rows all fall outside the trading calendar is skipped and leaves its number unused; the metadata table assumes numbers without gap, so such a case adds an empty row to it (known defect, never met with a calendar matching the listings).
- **`adj_close` wins over `close`.** When a row exposes `adj_close`, the bundle propagates it as the Zipline `close` so backtests run on split/dividend-adjusted prices out of the box. The ingestion stores Yahoo's adjusted close in both columns, so the two are equal for ingested rows.
- **Session alignment.** The bundle intersects rows with `calendar.sessions_in_range(start, end)` before handing them to `BcolzDailyBarWriter`, which otherwise rejects timestamps outside the trading calendar. With an older calendar API the `all_sessions` attribute is used instead; when neither exists, the alignment step is skipped.
- **Empty adjustments.** Fonrex does not track corporate actions as first-class rows yet: the bundle passes empty splits/dividends DataFrames to `SQLiteAdjustmentWriter` so the schema initialises correctly, and relies on `adj_close` for adjusted pricing. Extending the bundle is a matter of filling those DataFrames from a future `corporate_actions` table.

### Public API

```python
from zipline_bundle import (
    FonRexBundle,              # ingest callable class
    FonRexBundleDataSource,    # SQL extraction (test-friendly)
    fonrex_equities,           # factory returning a Zipline-compatible ingest
    register_fonrex_bundle,    # convenience wrapper around zipline.data.bundles.register
    TickerMetadata,
    TickerBars,
)
```

### CLI

Two subcommands are exposed as `python -m zipline_bundle`:

- `preview --start <date> --end <date> [--tickers ...] [--database-url ...]` — prints the tickers, sid allocation, and row counts the bundle would generate. Does not require Zipline to be installed.
- `ingest --start <date> --end <date> [--tickers ...] [--database-url ...] [--bundle-name ...] [--calendar ...] [--quiet]` — registers the bundle in-process and triggers `zipline.data.bundles.ingest`. Requires `zipline-reloaded`.

### Configuration Reference

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://fonrex:fonrex_password@localhost:5432/fonrex` (the default of `.env.example`) | SQLAlchemy URL. `postgresql+asyncpg://` URLs are auto-normalised to the sync driver. |
| `FONREX_BUNDLE_NAME` | `fonrex` | Bundle name registered with Zipline. |
| `FONREX_BUNDLE_TICKERS` | *(empty)* | Comma-separated whitelist. Empty means "every asset with EOD rows in the window". |
| `FONREX_BUNDLE_CALENDAR` | `NYSE` | Zipline trading calendar name. Use `XPAR`, `XETR`, `XLON`, `XSWX` etc. for non-US markets. |

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
- **Ambiguous tickers.** Without `currency` / `exchange`, a ticker shared by several listings resolves to one of them by rule, not by intent, and not by the same rule for prices and for fundamentals (see [Identity Resolution](#identity-resolution)); the bulk ingestion, the technical and real-time routes, `/fundamental/deep` and the Zipline bundle cannot name a listing.
- **Screener.** `GET /technical/screen` evaluates a sample of the catalogue (see [Technical Indicators](#technical-indicators)), not all of it.
- **Tables, settings and files without a writer or a caller.** `etf_details`, `etf_holdings` and `index_constituents` are never written; `fundamentals` is never written and no route reads it; `usage_logs.cost_bucket` is never filled; the `OpenFIGI` provider is loaded and no route calls it; `download_logo` (`LOGO_TOKEN`) is never called; `INGEST_TV_DELAY` is read and used by nothing; two alert types are declared and never raised.
- **Investing.com news.** The news provider reads a mapping named `investing_com`, which the import never creates: it returns nothing unless such a mapping is added by hand.
- **Untested paths.** The gap detection of the ingestion, the weighted consensus of the DCF, and the ESG, earnings-trend and shares-history parts of the enrichment have no test of their own; `tests/test_google_url_provider.py` is a manual script.
- **Statements enriched before the fiscal-year rule.** Their interest, tax, operating profit, share counts and dividends are empty until the instrument is enriched again (`GET /fundamental/deep?refresh=true`).
- **Layers.** Most features are services called directly by their router (see [Layers](#layers)); `macro/fred_service.py` creates its own HTTP client outside the provider HTTP layer.
- **Providers blocked by anti-bot services.** From a personal connection several sites refuse the request (typically with a `403`); their entry of the answer is then an error. This is expected until a relay is configured (see [Outbound Relay](#outbound-relay-not-part-of-this-repository)).
