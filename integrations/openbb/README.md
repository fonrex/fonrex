# Fonrex × OpenBB Workspace

Connect your self-hosted Fonrex instance to OpenBB Workspace to access
EU market fundamentals, DCF valuations, technical indicators and news
directly inside OpenBB.

## Prerequisites

- A running Fonrex instance (self-hosted or Fonrex Relay), reachable
  from the internet or from your local network if running OpenBB
  Workspace Enterprise on-prem
- An active Fonrex Relay API key (`frx_live_...`) if using the hosted
  Cloud Relay, or no key required for a fully self-hosted instance
  with authentication disabled

## Setup

1. In OpenBB Workspace, right-click on your dashboard and select **"Add data"**
2. Enter your Fonrex instance URL (e.g. `https://your-fonrex-instance.com`)
3. OpenBB will automatically discover the available widgets via `/widgets.json`
4. If your instance requires authentication, add your API key as a
   custom header: `X-API-KEY: frx_live_...`
5. Import the **"Fonrex — EU Markets"** app from the marketplace, or add
   individual widgets to your own dashboard

## Available Widgets

| Widget ID | Name | Category | Type | Description |
|---|---|---|---|---|
| `fonrex_fundamentals` | Fonrex Fundamentals | Fundamentals | table | Multi-provider fundamentals: P/E, ROE, dividend yield, market cap |
| `fonrex_fundamentals_deep` | Fonrex Deep Fundamentals | Fundamentals | table | Financial statements, ESG scores, insider transactions, analyst ratings |
| `fonrex_eod` | Fonrex EOD History | Historical | chart | End-of-day OHLCV price history with auto-ingestion |
| `fonrex_history` | Fonrex OHLCV History | Historical | chart | OHLCV price history with date range filtering |
| `fonrex_quote` | Fonrex Quote | Market Data | metric | Latest real-time price snapshot |
| `fonrex_quotes_batch` | Fonrex Batch Quotes | Market Data | table | Batch price snapshots for multiple tickers |
| `fonrex_technical` | Fonrex Technical Indicator | Technical | chart | Single technical indicator (RSI, SMA, MACD, etc.) |
| `fonrex_technical_multi` | Fonrex Multi-Indicator | Technical | chart | Multiple indicators from a single database read |
| `fonrex_technical_chart` | Fonrex Technical Chart | Technical | chart | OHLCV + overlaid indicators, chart-ready |
| `fonrex_screener` | Fonrex Technical Screener | Technical | table | Screen stocks by indicator thresholds |
| `fonrex_news` | Fonrex News | News | table | Aggregated news from 7 providers with deduplication |
| `fonrex_news_feed` | Fonrex News Feed | News | table | Global financial news feed |
| `fonrex_dcf` | Fonrex DCF Valuation | Valuation | table | DCF intrinsic value (FCF+EPS+DDM) |
| `fonrex_dcf_compare` | Fonrex DCF Models Comparison | Valuation | table | Side-by-side comparison of all 3 DCF models |
| `fonrex_dcf_sensitivity` | Fonrex DCF Sensitivity Matrix | Valuation | table | WACC × terminal growth sensitivity matrix |
| `fonrex_insider_transactions` | Fonrex Insider Transactions | Fundamentals | table | SEC Form 4 insider trading data (US only) |
| `fonrex_etf_details` | Fonrex ETF Details | Fundamentals | table | UCITS ETF details from JustETF |
| `fonrex_index_constituents` | Fonrex Index Constituents | Market Data | table | Index constituents (S&P 500, CAC 40, NASDAQ 100, DAX) |
| `fonrex_macro_rates` | Fonrex Macro Rates | Macro | metric | Current macro-economic rates (FRED API) |

## Pre-assembled Apps

### Fonrex — EU Markets
A comprehensive dashboard for analyzing a single ticker:
- **Overview** tab: Quote, deep fundamentals, EOD chart
- **Valuation** tab: DCF valuation + sensitivity matrix
- **Technical** tab: Full technical chart with indicators
- **News** tab: Latest news from 7 providers

### Fonrex — Screener & Macro
An idea-generation dashboard:
- **Screener** tab: Technical screener (e.g. RSI < 30 for oversold stocks)
- **Macro Context** tab: Current FRED macro rates + index constituents

## Authentication

Fonrex supports two authentication methods, both resolving to the same
API key validation:

| Method | Header | Example |
|---|---|---|
| Bearer token (standard) | `Authorization: Bearer frx_live_...` | REST clients, curl |
| API key header (OpenBB) | `X-API-KEY: frx_live_...` | OpenBB Workspace |

Configure either one in OpenBB Workspace settings. The `X-API-KEY`
method is recommended for OpenBB as it matches OpenBB's native custom
header configuration.

## Support

- Documentation: https://docs.fonrex.io
- Issues: https://github.com/fonrex/fonrex/issues
