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
cp .env.example .env
docker compose up
# → API running on http://localhost:5000
```

---

## Quickstart

### Requirements
- Docker + Docker Compose
- 4 GB RAM minimum (8 GB recommended)

### Start in 3 commands

```bash
# 1. Clone and configure
git clone https://github.com/fonrex/fonrex && cd fonrex
cp .env.example .env

# 2. Start (runs migrations automatically)
docker compose up -d

# 3. Import your first assets
docker compose exec fonrex-api python import_assets.py --file data/etf.csv
```

### First API calls

```bash
# Fundamentals
curl "http://localhost:5000/fundamental?ticker=AIR.PA"

# EOD history (auto-ingests if missing)
curl "http://localhost:5000/eod/AIR.PA?period=1y"

# Real-time quote (cached from WebSocket stream)
curl "http://localhost:5000/quote/AAPL"

# Technical indicators
curl "http://localhost:5000/technical/AIR.PA?indicator=rsi&period=14"

# DCF valuation
curl "http://localhost:5000/dcf/AIR.PA"

# Latest news
curl "http://localhost:5000/news/AIR.PA"

# Provider health monitoring
curl "http://localhost:5000/health/providers"
```

### WebSocket (real-time prices)

```javascript
const ws = new WebSocket("ws://localhost:5000/ws/realtime/AIR.PA");
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
| **OHLCV + adj_close** | Splits & dividends adjusted |
| **Multi-resolution** | 1D, 1W, 1M |
| **Auto-ingest** | Missing data fetched automatically on first request |

### Fundamentals (18 providers)
| Provider Group | Providers |
|---|---|
| **Core EU** | ZoneBourse, Boursorama, BourseDirect, Fortuneo, InvestirLesEchos |
| **Core Global** | Yahoo Finance, Google Finance, MSN, MorningStar, Investing.com |
| **US Premium** | Barron's, WSJ, MarketWatch, Gurufocus |
| **Specialized** | JustETF (UCITS ETFs), SEC Edgar (insider transactions), OpenFIGI, Index Constituents |

**Deep fundamentals stored in 8 dedicated tables:**
- `fundamentals_highlights` — 50+ metrics (P/E, ROE, ROA, EV/EBITDA, beta, short interest...)
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
Intrinsic value calculated entirely from data already in your database — no external API required.

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
  "assumptions": {
    "projection_years": 10,
    "terminal_growth": 0.02,
    "risk_free_rate": 0.04,
    "margin_of_safety": 0.15
  }
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

With 18 HTML-scraping providers, **silent data corruption** is the biggest risk: a CSS selector changes, a provider returns `0.8` instead of `24.0` for a P/E ratio, and the fallback runner accepts it because it's not `None`.

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
│  Pub/Sub    │  │  19 tables   │  │  18 providers         │
│  Health ∑   │  │  + 3 health  │  │  5 canary assets      │
└─────────────┘  └──────────────┘  └───────────────────────┘
```

### Docker services

```yaml
services:
  fonrex-api     # FastAPI + Gunicorn, port 5000
  fonrex-db      # TimescaleDB (PostgreSQL 16)
  fonrex-redis   # Redis 7, 256MB limit, allkeys-lru
  fonrex-migrate # Alembic migrations (one-shot, profile: migrate)
```

### Database schema (12 migrations)

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

provider_health_log       — TimescaleDB hypertable (30d retention, canary + realtime checks)
provider_health_daily     — daily aggregate per provider
provider_alerts           — active/resolved alerts with auto-resolution

ingest_log                — ingestion audit trail
usage_logs                — API request logs (cost_bucket included)
```

---

## Configuration

Copy `.env.example` to `.env` and adjust:

```env
# Database
POSTGRES_USER=fonrex
POSTGRES_PASSWORD=changeme
POSTGRES_DB=fonrex
DATABASE_URL=postgresql+psycopg2://fonrex:changeme@db:5432/fonrex

# Redis
REDIS_URL=redis://redis:6379/0

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
```

---

## Tests and Quality Checks

A `Makefile` is provided to simplify local development, testing, and quality checks. Run `make` or `make help` to see all available commands.

```bash
# Install development and quality dependencies
python -m pip install -r requirements-dev.txt

# Run the same quality gate as the CI (linting, syntax, migrations, test coverage)
make quality

# Run individual quality stages
make lint             # Ruff lint checks
make typecheck        # Annotation checks on application boundaries
make migration-check  # Alembic head verification
make test-cov         # Pytest with global and per-module coverage gates

# Run a specific test module
PYTHONPATH=. pytest tests/test_technical_indicators.py -v
```

The local quality gate blocks strict Ruff violations, invalid Python syntax, multiple
Alembic heads, test warnings, regressions, application coverage below 54%, and
coverage regressions in nine critical modules.
GitHub Actions runs this exact same quality check for every pull request and push to `main`.

**38 test files** covering:
- Asset identity resolution and ISIN deduplication
- 10 individual provider parsers (Barrons, WSJ, MarketWatch, Investing, JustETF, SEC Edgar, OpenFIGI, Fortuneo, Google Finance, Index Constituents)
- Historical ingestion (gap detection, normalization, cache invalidation)
- Real-time streaming (subscribe/unsubscribe/restore, WebSocket manager)
- Technical indicators (24 tests: RSI, SMA, EMA, MACD, Bollinger Bands, screener)
- News service (31 tests: 7 providers, URL dedup, title similarity, Redis cache)
- DCF service (10 tests: WACC, FCF, EPS, DDM, consensus, sensitivity matrix)
- Provider monitoring (44 tests: ValidationLayer range/consensus, CanaryMonitor, alerts, endpoints, Redis)
- Alembic migration consistency

---

## Roadmap

- [x] Provider health monitoring (ValidationLayer + CanaryMonitor)
- [ ] Zipline bundle (backtesting integration)
- [ ] Sentiment analysis on news articles
- [ ] Portfolio tracking endpoints
- [ ] Browser extension (Fonrex DevTools)
- [ ] DCF bulk valuation endpoint
- [ ] Webhook support for price alerts

---

## Contributing

Contributions are welcome. Please read `CONTRIBUTING.md` before opening a PR.

```bash
# Setup dev environment
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt

# Run the local quality checks (Ruff, migrations, tests) before submitting
make quality
```

**Adding a new provider:**
1. Create `financials/providers/myprovider.py` extending `BaseFinancialProvider`
2. Register in `main.py`
3. Add mappings in `import_assets.py`
4. Write tests in `tests/test_myprovider.py`

See [docs/adding-providers.md](docs/adding-providers.md) for details.

---

## License

MIT License — see [LICENSE](LICENSE).

The Cloud Relay service is governed by separate [Terms of Service](https://fonrex.io/terms).

---

<p align="center">
  Built with ❤️ for developers who want to own their financial data stack.
  <br/>
  <a href="https://fonrex.io">fonrex.io</a> · <a href="https://fonrex.io/docs/intro/">Docs</a>
</p>
