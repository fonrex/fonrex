<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white" />
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" />
  <img src="https://img.shields.io/badge/TimescaleDB-hypertable-orange" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" />
  <img src="https://img.shields.io/badge/License-AGPL--3.0-green" />
</p>

<h1 align="center">Fonrex</h1>
<p align="center"><strong>Open-source financial data infrastructure — self-hosted, EU-first, no subscription required.</strong></p>
<p align="center">
  <a href="#what-is-fonrex">What is Fonrex?</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#endpoints">Endpoints</a> ·
  <a href="#features">Features</a> ·
  <a href="#vs-fmp-premium">vs FMP Premium</a> ·
  <a href="#architecture">Architecture</a>
</p>

---

## What is Fonrex?

Fonrex is a **self-hosted financial data API** that aggregates market data, fundamentals, real-time prices, technical indicators, news and DCF valuations — all from a single Docker stack you control.

**No monthly fee. No rate limits. No vendor lock-in. Your data, your infrastructure.**

**LLM Integration**: You can directly feed our documentation to LLMs using this URL: [https://fonrex.io/llms.txt](https://fonrex.io/llms.txt)

```bash
git clone https://github.com/fonrex/fonrex
cd fonrex
cp .env.example .env   # then set FONREX_API_KEY in .env (see Quickstart)
mkdir -p logs
docker compose up
# → API running on http://localhost:5000
```

---

## Quickstart

### Requirements
- Docker + Docker Compose
- 4 GB RAM minimum (8 GB recommended)

### Start in 5 commands

```bash
# 1. Clone and configure
git clone https://github.com/fonrex/fonrex && cd fonrex
cp .env.example .env

# 2. Set your API key (required: the API rejects every request until one is configured)
export FONREX_API_KEY="frx_live_$(openssl rand -hex 24)"
sed -i.bak "s/^FONREX_API_KEY=.*/FONREX_API_KEY=$FONREX_API_KEY/" .env && rm .env.bak

# 3. Ensure log directory exists
mkdir -p logs

# 4. Start (runs migrations automatically)
docker compose up -d

# 5. Import your first assets
docker compose exec fonrex-api python import_assets.py --file data/etf.csv
```

Docker Compose loads `.env` into the API container. Change `POSTGRES_PASSWORD` in `.env`
**before** the first start as well. PostgreSQL and Redis are published on `127.0.0.1` only:
they are reachable from the host, never from the network.

The API image is self-contained: it holds the code, the database migrations and the seed
files (`data/*.csv`). Compose only mounts what the application writes at runtime: `./logs`
and `./static/logos` (downloaded logos). `.env` is never copied into the image.

```bash
# After updating the code (git pull, local edit): rebuild the image
docker compose up -d --build

# Development: run the code of this folder without rebuilding at each change
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

### Authentication

Authentication is **on by default**. Every route except `/health`, `/docs`, `/redoc`,
`/openapi.json`, `/widgets.json`, `/apps.json` and `/static` requires one of the keys
configured in `FONREX_API_KEY` (or `FONREX_API_KEYS`, comma-separated), sent as either:

```
Authorization: Bearer frx_live_...
X-API-KEY: frx_live_...
```

WebSocket clients that cannot set headers may pass `?token=frx_live_...`.

**Read-only keys.** Keys listed in `FONREX_READ_ONLY_API_KEYS` can query data but cannot
clear the cache, clean the database, trigger ingestion or change subscriptions. Use one for
any client that stores the key outside the machine running Fonrex — the Google Sheets
connector, or a dashboard reaching your instance through a tunnel.

For a purely local instance you can opt out with `FONREX_AUTH_REQUIRED=false` (and no key
configured). This opens every route, including cache and database administration — never
do it on a reachable host.

### First API calls

```bash
AUTH="X-API-KEY: $FONREX_API_KEY"

# Fundamentals
curl -H "$AUTH" "http://localhost:5000/fundamental?ticker=AIR.PA"

# EOD history (auto-ingests if missing)
curl -H "$AUTH" "http://localhost:5000/eod/AIR.PA?period=1y"

# Real-time quote (cached from WebSocket stream)
curl -H "$AUTH" "http://localhost:5000/quote/AAPL"

# Technical indicators
curl -H "$AUTH" "http://localhost:5000/technical/AIR.PA?indicator=rsi&period=14"

# DCF valuation
curl -H "$AUTH" "http://localhost:5000/dcf/AIR.PA"

# Macro-economic rates: USD (FRED) and EUR (ECB), or one currency
curl -H "$AUTH" "http://localhost:5000/macro/rates"
curl -H "$AUTH" "http://localhost:5000/macro/rates?currency=EUR"

# Latest news
curl -H "$AUTH" "http://localhost:5000/news/AIR.PA"

# Provider health monitoring
curl -H "$AUTH" "http://localhost:5000/health/providers"
```

### WebSocket (real-time prices)

```javascript
const ws = new WebSocket("ws://localhost:5000/ws/realtime/AIR.PA?token=" + FONREX_API_KEY);
ws.onmessage = (e) => {
  const { type, data } = JSON.parse(e.data);
  if (type === "tick") console.log(`${data.close} €`);
};
```

---

## Features

### Market Data
| Feature | Details |
|---|---|
| **EOD History** | 20+ years via yfinance + TradingView fallback |
| **Intraday 1min** | Live streaming via TradingView WebSocket |
| **Real-time Prices** | WebSocket push + Redis cache (60s TTL) |
| **Batch Quotes** | `GET /quotes?tickers=AIR.PA,BNP.PA,AAPL` |
| **OHLCV + adj_close** | OHLC as traded (adjusted for splits); `adj_close` also adjusted for dividends. A split or a dividend after the last ingestion is detected and the series is fetched again in one piece |
| **Multi-resolution** | 1D, 1W, 1M |
| **Auto-ingest** | Missing data fetched automatically on first request |

### Fundamentals (18 providers)
| Provider Group | Providers |
|---|---|
| **Core EU** | ZoneBourse, Boursorama, BourseDirect, Fortuneo, InvestirLesEchos |
| **Core Global** | Yahoo Finance, Google Finance, MSN, MorningStar, Investing.com |
| **US Premium** | Barron's, WSJ, MarketWatch, Gurufocus |
| **Specialized** | JustETF (UCITS ETFs), SEC Edgar (insider transactions), OpenFIGI, Index Constituents |

`GET /fundamental` returns one document in the EODHD layout. Each figure comes from Yahoo,
then from the figures stored in the database, then from the scraped providers that publish
the same quantity; the `Sources` section names the source of every figure. Ratios are ratios (a 0.32 % dividend
yield is `0.0032`). For a listing of your catalogue, Yahoo is asked with the symbol verified
for it — never with the bare ticker, which may be another instrument.

**Deep fundamentals stored in 8 dedicated tables:**
- `fundamentals_highlights` — 50+ metrics (P/E, ROE, ROA, EV/EBITDA, beta, solvency ratios, short interest...)
- `financial_statements` — income, balance sheet, cash flow (annual + quarterly)
- `earnings_history` — EPS actual vs estimate, surprise %
- `earnings_trend` — analyst consensus (0q, +1q, 0y, +1y)
- `analyst_ratings` — consensus, target price, buy/hold/sell counts
- `esg_scores` — E/S/G scores + 15 controversy flags (tobacco, weapons, coal...)
- `etf_details` — TER, AUM, replication method, 1/3/5Y returns
- `etf_holdings` — top holdings with weights

### Technical Indicators (18 indicators)
```
Trend     : SMA, EMA, WMA, DEMA, TEMA, VWAP
Momentum  : RSI, MACD, Stochastic, CCI, ROC, MOM
Volatility: Bollinger Bands, ATR, Keltner Channels
Volume    : OBV, A/D Line, MFI
```

```bash
# Single indicator
GET /technical/AIR.PA?indicator=rsi&period=14

# Multi-indicator (single DB read)
GET /technical/AIR.PA/multi?indicators=sma_20,ema_50,rsi_14,macd

# Screener: RSI < 30 (oversold)
GET /technical/screen?indicator=rsi&operator=lt&value=30

# Chart-ready OHLCV + indicators
GET /technical/AIR.PA/chart?indicators=sma_20,bbands_20
```

### DCF Valuation (3 models)
Intrinsic value calculated using fundamentals and dynamic macro-economic data, with local cache: the cash flows are discounted with the risk-free rate of their currency (US Treasury from FRED for USD, AAA euro area rate from the ECB for EUR). The cost of equity is the CAPM by default; `POST /dcf/{ticker}` with `wacc_params.cost_of_equity_model` = `ff3`, `ff5` or `carhart` takes it from the Fama/French factors instead (betas of the listing, long-run premia of the factors).

| Model | When used | Formula |
|---|---|---|
| **FCF** (50% weight) | 3+ years positive FCF | FCFF + Gordon Growth terminal value |
| **EPS** (30% weight) | EPS TTM available | EPS growth + P/E terminal multiple |
| **DDM** (20% weight) | dividend_yield > 2% | Gordon Growth on dividends |

```bash
# Default (auto model selection, 5Y projection)
GET /dcf/AIR.PA

# Custom parameters
POST /dcf/AIR.PA
{
  "models": ["fcf", "eps", "ddm"],
  "projection_years": 10,
  "terminal_growth_rate": 0.02,
  "wacc_params": {"risk_free_rate": 0.04}
}

# Compare all 3 models
GET /dcf/AIR.PA/compare

# Sensitivity matrix (WACC × g_terminal)
GET /dcf/AIR.PA/sensitivity
```

### News (7 providers)
| Provider | Coverage | Language |
|---|---|---|
| Yahoo Finance | Global | Multi |
| Google Finance | Global (aggregates Reuters, Bloomberg) | Multi |
| ZoneBourse | EU / France | FR |
| Boursorama | France | FR |
| Investing.com | Global | Multi |
| MarketWatch | US + EU | Multi |
| MSN Finance | Global (aggregates AP, Reuters) | Multi |

Deduplication: URL normalization (UTM removal) + title similarity (`difflib`, threshold 0.85).

### Provider Monitoring (Phase 12)

With 13 HTML-scraping providers, **silent data corruption** is the biggest risk: a CSS selector changes, a provider returns `0.8` instead of `24.0` for a P/E ratio, and the fallback runner accepts it because it's not `None`.

Fonrex solves this with two layers of automated protection:

| Layer | When | What it does |
|---|---|---|
| **ValidationLayer** | Every request (real-time) | Range checks (is P/E between 0.5–1000?) + consensus cross-validation (>50% deviation from median = outlier → rejected) |
| **CanaryMonitor** | Daily (06:00 UTC via APScheduler) | Tests each provider against 5 known-good "canary" stocks (AAPL, AIR.PA, BNP.PA, MSFT, TSLA) with expected ranges |

**Alert system** — automatic alert creation/resolution:
- `canary_failed` — canary value out of expected range
- `high_outlier_rate` — provider success rate below threshold
- Auto-resolves when subsequent canary checks pass

```bash
# Global provider health dashboard
GET /health/providers

# Detailed provider stats (7 or 30 days)
GET /health/providers/ZoneBourse?days=30

# Active alerts
GET /health/alerts?severity=critical

# Trigger manual canary check
POST /health/canary/run

# Validation quality statistics
GET /health/stats
```

### Data Import
```bash
# Import from CSV
docker compose exec fonrex-api python import_assets.py --file data/stocks.csv

# Dry-run (no writes)
docker compose exec fonrex-api python import_assets.py --file data/stocks.csv --dry-run

# Trigger historical ingestion for all assets
docker compose exec fonrex-api python scripts/ingest_all.py

# Fetch every series again in one piece (once, after upgrading to migration 016)
docker compose exec fonrex-api python scripts/ingest_all.py --force

# Clean ISIN duplicates (safe, idempotent)
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py --dry-run
docker compose exec fonrex-api python scripts/clean_isin_duplicates.py --create-index
```

**CSV format:**
```csv
name,ticker,isin,productType,currency
Apple Inc,AAPL,US0378331005,STOCK,USD
Apple Inc,APC,US0378331005,STOCK,EUR
Airbus SE,AIR,NL0000235190,STOCK,EUR
```

Multi-currency is handled correctly: one row in `assets`, one row per listing in `asset_listings`.

---

## Endpoints

| Method | Endpoint | Description | Cache TTL |
|---|---|---|---|
| GET | `/health` | Service health (DB + Redis) | — |
| GET | `/fundamental` | Multi-provider fundamentals | 7d |
| GET | `/fundamental/deep` | Deep fundamentals (statements, ESG, ratings) | 24h |
| GET | `/eod/{ticker}` | EOD history JSON/CSV (auto-ingest) | 24h |
| GET | `/ticker/{symbol}/history` | OHLCV history | 1h |
| POST | `/historical/ingest` | Trigger single asset ingestion | — |
| POST | `/historical/ingest/bulk` | Bulk ingestion | — |
| WS | `/ws/realtime/{ticker}` | Real-time price streaming | — |
| GET | `/quote/{ticker}` | Latest price snapshot | 60s |
| GET | `/quotes` | Batch price snapshots | 60s |
| GET | `/technical/{ticker}` | Single indicator | 1h (EOD) / 60s (intraday) |
| GET | `/technical/{ticker}/multi` | Multi-indicator (single read) | 1h |
| GET | `/technical/{ticker}/chart` | OHLCV + indicators for charting | 1h |
| GET | `/technical/screen` | Indicator-based screener | 15min |
| GET | `/news/{ticker}` | News from 7 providers | 30min |
| GET | `/news/feed` | Global news feed | 30min |
| GET | `/dcf/{ticker}` | DCF intrinsic value (FCF+EPS+DDM consensus) | 6h |
| POST | `/dcf/{ticker}` | Custom DCF (no cache) | — |
| GET | `/dcf/{ticker}/compare` | All 3 models comparison | 6h |
| GET | `/dcf/{ticker}/sensitivity` | WACC × g_terminal matrix | 6h |
| GET | `/insider-transactions/{ticker}` | SEC Form 4 (US only) | 12h |
| GET | `/etf/{isin}/details` | ETF details from JustETF | 24h |
| GET | `/index/{name}/constituents` | S&P500, CAC40, NASDAQ100, DAX | 7d |
| GET | `/assets/by-isin/{isin}` | Asset + all listings by ISIN | — |
| GET | `/health/providers` | Provider health summary (Redis→DB fallback) | — |
| GET | `/health/providers/{name}` | Detailed provider health + daily stats | — |
| GET | `/health/alerts` | Active alerts (filter by severity/provider) | — |
| POST | `/health/alerts/{id}/resolve` | Manual alert resolution | — |
| POST | `/health/canary/run` | Trigger canary check (background) | — |
| GET | `/health/canary/history` | Historical canary results | — |
| GET | `/health/stats` | Global validation quality statistics | — |
| GET | `/macro/rates` | Current macro-economic rates: US 10Y Treasury (FRED); euro AAA 10Y, ECB deposit facility rate, CISS stress index (ECB). `currency=USD` or `EUR` keeps one source | 6h |
| GET | `/factors/{dataset}` | Fama/French factor returns (`us_3`, `europe_5`, `us_mom`…), monthly or daily, from the Kenneth French Data Library | stored, refreshed after 7 days |
| GET | `/factors/exposure/{ticker}` | Exposure of a listing to the Fama/French factors (`model=ff3`, `ff5` or `carhart`): betas, alpha, R², on returns in US dollars | — |

---

## vs FMP Premium

FMP Premium costs **$59/month ($708/year)** and doesn't cover European markets without upgrading to Ultimate ($149/month).

| Feature | FMP Premium $59/mo | Fonrex (free, self-hosted) |
|---|---|---|
| EOD history (30y) | ✅ US+UK+CA | ✅ Global (yfinance) |
| Intraday 5min+ | ✅ Limited | ✅ yfinance 60d |
| Real-time prices | ✅ REST polling | ✅ **WebSocket push** |
| EU markets (XPAR, XETRA...) | ❌ Ultimate only | ✅ **Native** |
| UCITS ETFs | ❌ Limited | ✅ **justETF integrated** |
| ESG Scores | ❌ Not included | ✅ Included |
| Insider transactions | ❌ Not in Premium | ✅ SEC Edgar |
| Short interest | ❌ Not available | ✅ Included |
| DCF Valuation | ✅ Simple | ✅ **3 models + sensitivity** |
| Rate limits | ⚠️ 750 req/min | ✅ **Unlimited (self-hosted)** |
| Data ownership | ❌ Monthly rental | ✅ **You own it** |
| Self-hosted | ❌ Cloud only | ✅ **Docker, your servers** |
| News (7 providers) | ✅ Basic | ✅ Multi-provider + dedup |

**Over 2 years: Fonrex saves ~€1,400 vs FMP Premium, with broader EU coverage.**

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Client (browser / app)                    │
│   REST HTTP               WebSocket                          │
└────────────┬──────────────────┬──────────────────────────────┘
             │                  │
┌────────────▼──────────────────▼──────────────────────────────┐
│                   FastAPI (port 5000)                        │
│  /fundamental  /eod  /quote  /technical  /dcf  /news         │
│  /health/*  ConnectionManager  RealtimePriceWorker           │
│  ValidationLayer  CanaryMonitor (APScheduler)                │
└──────┬────────────────┬──────────────────┬───────────────────┘
       │                │                  │
┌──────▼──────┐  ┌──────▼───────┐  ┌───────▼───────────────┐
│   Redis 7   │  │  TimescaleDB │  │   External Sources    │
│  Cache &    │  │  PostgreSQL  │  │  yfinance / TV WS     │
│  Pub/Sub    │  │  hypertables │  │  18 providers         │
│  Health ∑   │  │  + 3 health  │  │  5 canary assets      │
└─────────────┘  └──────────────┘  └───────────────────────┘
```

### Docker services

```yaml
services:
  fonrex-api     # FastAPI + Gunicorn, port 5000
  fonrex-db      # TimescaleDB (PostgreSQL 16), published on 127.0.0.1:5432 only
  fonrex-redis   # Redis 7, 256MB limit, allkeys-lru, published on 127.0.0.1:6379 only
  fonrex-migrate # Alembic migrations (one-shot, profile: migrate)
```

### Database schema

```
assets                    — 1 row per ISIN (unique index)
  asset_listings          — N rows per ISIN (multi-exchange, multi-currency)
    asset_mappings        — provider-specific identifiers

prices_eod                — TimescaleDB hypertable (1D/1W/1M)
prices_intraday           — TimescaleDB hypertable (1min, 30d retention)
realtime_subscriptions    — active TradingView WS streams

fundamentals_highlights   — 50+ daily snapshot metrics
financial_statements      — income / balance / cashflow (annual + quarterly)
earnings_history          — EPS actual vs estimate
earnings_trend            — analyst consensus (0q, +1q, 0y, +1y)
analyst_ratings           — consensus + target price
esg_scores                — E/S/G + 15 controversy flags
etf_details               — TER, AUM, replication, performance
etf_holdings              — top holdings with weights
outstanding_shares_history
news_articles             — 90d retention, dedup on URL

macro_rates_cache         — FRED and ECB macro-economic series cache

provider_health_log       — TimescaleDB hypertable (30d retention, canary + realtime checks)
provider_health_daily     — daily aggregate per provider
provider_alerts           — active/resolved alerts with auto-resolution

ingest_log                — ingestion audit trail
usage_logs                — local API request journal (no IP by default, purged with age)
```

---

## Configuration

Copy `.env.example` to `.env` and adjust:

```env
# Authentication (required by default — see Quickstart › Authentication)
FONREX_API_KEY=frx_live_...

# Database
# With Docker Compose, DATABASE_URL and REDIS_URL are overridden to target the
# `db` and `redis` services; DATABASE_URL is then built from POSTGRES_PASSWORD.
POSTGRES_USER=fonrex
POSTGRES_PASSWORD=changeme
POSTGRES_DB=fonrex
DATABASE_URL=postgresql://fonrex:changeme@localhost:5432/fonrex

# Redis
REDIS_URL=redis://localhost:6379/0

# Historical ingestion
INGEST_CONCURRENCY=5
INGEST_YF_DELAY=0.5
INGEST_TV_DELAY=2.0
INGEST_BATCH_SIZE=1000

# Real-time streaming
TV_MAX_CONNECTIONS=10
TV_RECONNECT_DELAY=5
REALTIME_QUOTE_TTL=60

# Technical indicators
TECHNICAL_DEFAULT_LIMIT=500

# News
NEWS_CACHE_TTL=1800
NEWS_DEFAULT_LIMIT=20
NEWS_DEDUP_SIMILARITY=0.85

# DCF Valuation
DCF_RISK_FREE_RATE=0.04
DCF_EQUITY_RISK_PREMIUM=0.055
DCF_TERMINAL_GROWTH_RATE=0.025
DCF_DEFAULT_PROJECTION_YEARS=5

# Provider Monitoring
VALIDATION_OUTLIER_THRESHOLD=0.50   # Consensus deviation threshold
VALIDATION_MIN_PROVIDERS=2          # Min providers for consensus
CANARY_RUN_HOUR=6                   # Canary daily run hour (UTC)
ALERT_SUCCESS_RATE_WARNING=0.85     # Success rate → warning alert
ALERT_SUCCESS_RATE_CRITICAL=0.70    # Success rate → critical alert

# Optional: OpenFIGI (free key at openfigi.com)
OPENFIGI_API_KEY=

# Usage log (local journal of the calls received by your instance)
USAGE_LOG_IP=none                   # none | truncated | full
USAGE_LOG_RETENTION_DAYS=90         # Older rows are deleted daily (0 = keep)

# Outbound requests of the providers
FONREX_PROVIDER_MAX_CONCURRENCY=4   # Simultaneous requests per provider
FONREX_PROXY_URL=                   # Optional outbound proxy (yours or a relay service)
FONREX_PROXY_PROVIDERS=             # Optional: only these providers use the proxy
```

Every provider sends its requests through one shared HTTP layer
(`financials/providers/base.py`): same retry policy (network errors, 429 and 5xx are retried
with a growing pause; 401, 403 and 404 are final), same limit of simultaneous requests per
provider, and the same optional proxy. A website that starts refusing your IP can be routed
through `FONREX_PROXY_URL` without touching any provider.

---

## Troubleshooting

### Every request returns `401 Missing API key` or `403`
Authentication is on by default. Set `FONREX_API_KEY` in `.env`, restart the API
(`docker compose up -d`) and send the key with each request. The startup logs state the
effective mode (`docker compose logs fonrex-api | grep -i auth`).

### `.env` created before authentication became mandatory
`.env` is now loaded into the API container. Older copies of `.env.example` had comments
after empty values (`FRED_API_KEY=   # Optional…`), which Docker Compose reads as the value
itself. Re-create your file from the current `.env.example`, or move those comments to
their own lines.

### Where the database is stored, and how to back it up
The database files are kept in the Docker volume `timescale_data`, mounted on the data
directory of the TimescaleDB image (`PGDATA=/home/postgres/pgdata/data`). They survive
`docker compose down` and a rebuild; only `docker compose down -v` (or `make docker-clean`)
deletes them.

```bash
# Backup (one file, on the host)
docker compose exec -T db pg_dump -U fonrex -d fonrex -Fc > fonrex.dump

# Restore into an empty database: start the database alone, restore, then start the API
docker compose up -d db
docker compose exec -T db psql -U fonrex -d fonrex \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" -c "SELECT timescaledb_pre_restore();"
docker compose exec -T db pg_restore -U fonrex -d fonrex -Fc < fonrex.dump
docker compose exec -T db psql -U fonrex -d fonrex -c "SELECT timescaledb_post_restore();"
docker compose up -d
```

This is the [TimescaleDB logical backup procedure](https://www.tigerdata.com/docs/deploy/self-hosted/backup-and-restore/logical-backup);
restore with the same TimescaleDB version that made the dump.

### Upgrading an installation whose database was not on a volume
Before this fix the volume was mounted on `/var/lib/postgresql/data`, a path the
`timescaledb-ha` image does not use: the database lived inside the `fonrex-db` container and
was deleted with it. To check an existing installation:

```bash
docker exec fonrex-db psql -U fonrex -d fonrex -tc "show data_directory"
docker inspect fonrex-db --format '{{range .Mounts}}{{.Destination}} {{end}}'
```

If the data directory is not one of the mounted destinations, **make a backup (command
above) before `docker compose up -d` with the new `docker-compose.yml`**: Compose recreates
the database container, and the old container takes its data with it. Then restore the
backup as shown above, or import and ingest again.

### A ticker returns no price, or the wrong listing
The tickers of the catalogue are not Yahoo symbols (`EUCO` is `SYBC.DE` on Yahoo, and `SPFF`
alone is a US fund). Fonrex finds the Yahoo symbol of a listing from the ISIN of the
instrument, keeps it only if Yahoo quotes it in the currency of the listing, and stores it.
A listing for which nothing matches is **not** ingested, and the answer says why:

```json
{"error": "No data found", "reason": "No Yahoo symbol quoted in CHF for ISIN IE00B3S5XW04; Yahoo offers SYBB.DE (EUR)"}
```

- Several listings share a ticker (`GOVY` in EUR and in CHF): name the one you want with
  `currency` or `exchange` — `GET /eod/GOVY?period=1mo&currency=CHF`. Without it the primary
  listing is used.
- Several instruments share a bare ticker (`NEM` is Newmont in USD, its Australian line in AUD
  and Nemetschek in EUR, three ISINs): name the instrument with its ISIN, and the listing with the currency —
  `GET /eod/NEM?period=1y&isin=US6516391066&currency=USD`. `GET /eod` and
  `GET /ticker/{symbol}/history` answer with the `listing` they read
  (`ticker`, `isin`, `currency`, `exchange`), so that a client can check it.
- To see which symbol was used: `POST /historical/ingest?ticker=EUCO` returns `provider_symbol`.
  When the prices come from TradingView, `note` says why Yahoo was not the source
  (no symbol quoted in the currency of the listing, or no bar for the verified symbol).
- To replace a series fetched before this check existed, or to look the symbol up again:
  `POST /historical/ingest?ticker=<ticker>&force_refresh=true`.
- To set a symbol yourself when you know the right line (it is then trusted as it is):

```bash
docker compose exec -T db psql -U fonrex -d fonrex -c "
  INSERT INTO asset_mappings (asset_id, asset_listing_id, provider_name, provider_ticker,
                              source, is_active, failure_count, created_at, updated_at)
  SELECT l.asset_id, l.id, 'YahooFinance', 'GOVY.SW', 'manual', true, 0, now(), now()
  FROM asset_listings l WHERE l.ticker = 'GOVY' AND l.currency = 'CHF'
  ON CONFLICT (asset_listing_id, provider_name)
  DO UPDATE SET provider_ticker = EXCLUDED.provider_ticker, source = 'manual', is_active = true"
```

### Upgrading to per-listing prices (migration 014)
Daily, weekly and monthly prices are now stored per listing and dated by trading session.
The migration runs by itself at the next start (`docker compose up -d --build`) and converts
the existing rows; nothing has to be downloaded again. Back the database up first (see
above). What changes:

- the listings of one instrument (the same ETF in EUR and in USD) no longer share one
  series, and a weekly bar no longer replaces the daily bar of the same day;
- European and Asian sessions are no longer dated the day before.

Rows that were already stored are re-dated from their time of day. If a series looks wrong
after the upgrade, replace it: `POST /historical/ingest?ticker=<ticker>&force_refresh=true`.

To run the database tests against your own TimescaleDB (a temporary database is created
and dropped):

```bash
FONREX_TEST_DATABASE_URL=postgresql://fonrex:<password>@localhost:5432/fonrex \
    pytest tests/test_timescale_integration.py
```

### Deleting old prices
`POST /database/cleanup` deletes the prices older than `days_to_keep` days (730 when the body
is empty) and the logs older than 30 days. A first ingestion fetches ten years of history:
a cleanup with the default value removes eight of them. Count before deleting:

```bash
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
     -d '{"days_to_keep": 3650, "dry_run": true}' http://localhost:5000/database/cleanup
```

`dry_run` deletes nothing and returns what a real run would delete. A value below 30 is
refused: zero or a negative number would empty the price history.

### Common Docker Issues

#### 1. Permission Denied on `logs` volume (`chown permission denied`)
On macOS or Linux, Docker Desktop may fail to initialize volume permissions if the `./logs` directory is missing or owned by root.
```bash
mkdir -p logs
chmod 777 logs
docker compose up -d
```

#### 2. Container Name Conflict (`container name "/fonrex-db" is already in use`)
If previous containers with the same names already exist on your host:
```bash
docker rm -f fonrex-db fonrex-redis fonrex-api
docker compose up -d
```

---

## Tests and Quality Checks

A `Makefile` is provided to simplify local development, testing, and quality checks. Run `make` or `make help` to see all available commands.

```bash
# Install development and quality dependencies (exact versions of the lock file)
make install-dev

# Run the same quality gate as the CI (linting, syntax, migrations, test coverage)
make ci

# Run individual quality stages
make lint             # Ruff lint checks
make typecheck        # Annotation checks on application boundaries
make migration-check  # Alembic head verification
make test-cov         # Pytest with global and per-module coverage gates

# Run a specific test module
PYTHONPATH=. pytest tests/test_technical_indicators.py -v

# Run the database tests on a throwaway TimescaleDB container (needs Docker)
make test-db
```

The database tests (`tests/test_timescale_integration.py`: migrations applied to existing
data, price storage on the compressed hypertable, cleanup) need a TimescaleDB server and
are skipped without one. `make test-db` starts a temporary container of the image used by
`docker-compose.yml`, on port 54329, runs them and removes it; your own database is not
touched. To run them inside `make ci`, give the address of any TimescaleDB server — each
run creates and drops its own database on it:

```bash
FONREX_TEST_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/postgres make ci
```

The local quality gate blocks strict Ruff violations, invalid Python syntax, multiple
Alembic heads, test warnings, regressions, application coverage below 70%, and
coverage regressions in the modules listed in `scripts/check_coverage_distribution.py`:
the critical application modules and every data provider, each with its own floor.
A provider without a declared floor fails the gate, so a new one cannot be merged untested.
GitHub Actions runs this exact same quality check for every pull request and push to `main`,
with the database tests on a TimescaleDB service of the same image as `docker-compose.yml`,
and publishes the coverage per module in the summary of the run (pull request → Checks).

The test suite covers:
- Asset identity resolution and ISIN deduplication
- Provider parsers, checked against reduced extracts of real pages (`tests/fixtures/providers/`)
- Provider search, download and retries, against a simulated network (no real request is sent)
- Historical ingestion (gap detection, normalization, cache invalidation)
- Real-time streaming (subscribe/unsubscribe/restore, WebSocket manager)
- Technical indicators (RSI, SMA, EMA, MACD, Bollinger Bands, screener)
- News service (providers, URL dedup, title similarity, Redis cache)
- DCF service (WACC, FCF, EPS, DDM, consensus, sensitivity matrix)
- Provider monitoring (ValidationLayer range/consensus, CanaryMonitor, alerts, endpoints, Redis)
- Authentication defaults and read-only keys
- Alembic migration consistency
- Migrations, price storage and cleanup on a real TimescaleDB (in the CI, or `make test-db`)
- Guards that keep this document honest: provider, indicator and widget counts, settings of
  `.env.example` really read by the code, Docker image content

---

## Roadmap

- [x] Provider health monitoring (ValidationLayer + CanaryMonitor)
- [x] Zipline bundle (backtesting integration) — see [docs/zipline-bundle.md](docs/zipline-bundle.md)
- [ ] Sentiment analysis on news articles
- [ ] Portfolio tracking endpoints
- [ ] Browser extension (Fonrex DevTools)
- [ ] DCF bulk valuation endpoint
- [ ] Webhook support for price alerts

---

## Scope: self-hosted, and what Fonrex Relay will be

Fonrex is software you run yourself. Your instance fetches the data from public sources and
keeps it in your own database: there is no Fonrex-operated data API, and the integrations
below (OpenBB Workspace, Google Sheets) connect to **your** instance.

**Fonrex Relay** is a separate, optional paid service, still in development. It will be an
outbound relay that your instance can route its provider requests through. It relays pages;
it does not store or serve financial data. Fonrex does not depend on it.

---

## Integrations

### Google Sheets

The [Fonrex Sheets Connector](fonrex-sheets-connector/README.md) fills a spreadsheet with
fundamentals, DCF valuations and technical indicators from your instance, reached through a
tunnel (zrok or equivalent) with a read-only key.

### OpenBB Workspace

Fonrex integrates natively with [OpenBB Workspace](https://openbb.co) —
connect your self-hosted instance to access fundamentals, DCF valuations,
technical indicators and news directly inside OpenBB's dashboard environment.

![OpenBB Workspace Example](img/openBB-Workspace-example.png)

👉 See [integrations/openbb/README.md](integrations/openbb/README.md) for
setup instructions and the full list of 21 available widgets.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.
The rules of the code base — and the tests that enforce them — are in [AGENTS.md](AGENTS.md);
they apply to human contributors and to AI coding agents alike.

```bash
# Setup dev environment
python -m venv venv && source venv/bin/activate
make install-dev

# Run the local quality checks (Ruff, migrations, tests) before submitting
make ci
```

**Locked dependencies.** `requirements.txt` and `requirements-dev.txt` list what the project
needs, as ranges. `requirements.lock` and `requirements-dev.lock` record the exact versions
that were tested, with their hashes; the Docker image, the CI and `make install-dev` install
those, so two builds of the same commit contain the same packages. After editing a
requirements file run `make lock` (needs [uv](https://docs.astral.sh/uv/)); to move every
package to its latest allowed version run `make lock-upgrade`, then `make ci`.

**Adding a new provider:**
1. Create `financials/providers/myprovider.py` extending `BaseFinancialProvider`; send every
   request with `self._session()` or `self._get()` — never create an HTTP client yourself
2. Register in `main.py`
3. Add mappings in `import_assets.py`
4. Write tests in `tests/test_myprovider.py`

See "Adding a provider" in [AGENTS.md](AGENTS.md) for the full checklist.

---

## License

AGPL-3.0 License — see [LICENSE](LICENSE) and [DISCLAIMER](DISCLAIMER.md).

---

<p align="center">
  Built with ❤️ for developers who want to own their financial data stack.
  <br/>
  <a href="https://fonrex.io">fonrex.io</a> · <a href="https://fonrex.io/docs/intro/">Docs</a>
</p>
