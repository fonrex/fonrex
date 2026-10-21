# Fonrex × OpenBB Workspace

Connect your self-hosted Fonrex instance to OpenBB Workspace to access
EU market fundamentals, DCF valuations, technical indicators and news
directly inside OpenBB.

## Prerequisites

- A running self-hosted Fonrex instance, reachable from the internet
  (for example through a tunnel) or from your local network if running
  OpenBB Workspace Enterprise on-prem
- An API key of that instance (authentication is required by default):
  the one set in `FONREX_API_KEY`, or preferably a read-only key from
  `FONREX_READ_ONLY_API_KEYS`, which is enough for every widget. No key
  is needed only for a local instance started with
  `FONREX_AUTH_REQUIRED=false`

## Setup

1. In OpenBB Workspace, right-click on your dashboard and select **"Add data"**
2. Enter your Fonrex instance URL (e.g. `https://your-fonrex-instance.com`)
3. OpenBB will automatically discover the available widgets via `/widgets.json`
4. Add your API key as a custom header: `X-API-KEY: frx_live_...`
   (skip this step only if authentication is disabled on your instance)
5. Import the **"Fonrex — EU Markets"** app from the marketplace, or add
   individual widgets to your own dashboard

## Available Widgets

| Widget ID | Name | Category | Type | Description |
|---|---|---|---|---|
| `fonrex_fundamentals` | Fonrex Fundamentals | Fundamentals | table | Multi-provider fundamentals: P/E, ROE, dividend yield, market cap |
| `fonrex_fundamentals_deep` | Fonrex Deep Fundamentals | Fundamentals | table | Financial statements, ESG scores, insider transactions, analyst ratings |
| `fonrex_eod` | Fonrex EOD History | Historical | chart | End-of-day OHLCV price history with auto-ingestion |
| `fonrex_history` | Fonrex OHLCV History | Historical | chart | OHLCV price history with date range filtering |
| `fonrex_quote` | Fonrex Quote | Market Data | metric | Latest real-time price snapshot (real time once the ticker is subscribed with `POST /realtime/subscribe`, delayed price otherwise) |
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
| `fonrex_sec_check` | Fonrex SEC Check | Fundamentals | table | Annual statements stored from Yahoo checked against the SEC filings (XBRL), year by year (parameter `years`, 3 by default): one row per figure, gaps beyond `SEC_CHECK_TOLERANCE` first, with the XBRL concept and the link to the filing. Only for an instrument with a US listing in the catalogue |
| `fonrex_esef_check` | Fonrex ESEF Check | Fundamentals | table | Annual statements stored from Yahoo checked against the ESEF annual reports (filings.xbrl.org), year by year (parameter `years`, 3 by default): one row per figure, gaps beyond `SEC_CHECK_TOLERANCE` first, with the IFRS concept and the link to the filing in the viewer. Only for a share of the catalogue whose LEI GLEIF confirms (no German filing is found) |
| `fonrex_index_constituents` | Fonrex Index Constituents | Market Data | table | Index constituents (S&P 500, CAC 40, NASDAQ 100, DAX) |
| `fonrex_macro_rates` | Fonrex Macro Rates | Macro | metric | Current macro-economic rates, one card per series: US 10Y Treasury (FRED); euro AAA 10Y, ECB deposit rate, CISS (ECB). Parameter `currency`: USD, EUR or both |
| `fonrex_macro_indicators` | Fonrex Macro Indicators | Macro | metric | Latest Eurostat indicators of a country or of the euro area (parameter `geo`, `EA` by default): inflation (HICP) and core inflation, unemployment rate, GDP growth on the quarter and on the year, economic sentiment, consumer confidence; `delta` is the change on the previous period (percentage points for a rate) |
| `fonrex_macro_indicator_chart` | Fonrex Macro Indicator Chart | Macro | chart | One Eurostat indicator over time, one line per country (parameter `geo`, up to 10 separated by commas, `EA,DE,FR,IT,ES` by default), last 10 years by default (parameter `start`). Source: Eurostat |
| `fonrex_treasury_curve` | Fonrex Treasury Curve | Macro | chart | US Treasury yield curve of a day, 1 month to 30 years, shortest maturity first, with the curves of the days compared (parameter `compare`, `1M,1Y` by default: offsets 1W, 1M, 3M, 1Y or dates); `curve` nominal (par yields) or real (TIPS), `date` the latest by default. Source: US Treasury |
| `fonrex_treasury_spread` | Fonrex Treasury Spread | Macro | chart | US Treasury maturities (`10Y`) or spreads (`10Y-2Y`) over time, separated by commas (parameter `tenor`, `10Y-2Y` by default), with a zero line for a spread (below it, the curve is inverted); last 10 years by default (parameter `start`). Source: US Treasury |
| `fonrex_cot_positioning` | Fonrex COT Positioning | Macro | chart | CFTC Commitments of Traders: net position of a type of trader in one futures market (parameter `market`, a CFTC contract market code, `13874A` by default; `report` tff, disaggregated or legacy; `category`, the speculators of the report when empty) and its COT index, 0 to 100 over `window` weeks (156 by default), on a right axis; last 3 years by default (parameter `start`). Source: CFTC
| `fonrex_cot_extremes` | Fonrex COT Extremes | Macro | table | CFTC Commitments of Traders: latest report of the key markets of a report (or of the codes of `market`, separated by commas), the COT index farthest from 50 first: net position, its share of the open interest, its weekly change, the report date and its estimated day of publication, and “Top of window” (index 90 or more) or “Bottom of window” (10 or less): where the net position stands in its window, not whether it is long or short (a net short position can be at the top of its window). Source: CFTC
| `fonrex_factor_exposure` | Fonrex Factor Exposure | Factors | table | Fama/French exposure of a listing (model ff3, ff5 or carhart; monthly or daily): annualised alpha, betas with standard errors and t-stats, R², residual volatility, periods and warnings. Returns are in US dollars (other currencies converted with the ECB reference rates) |
| `fonrex_factor_returns` | Fonrex Factor Returns | Factors | chart | Cumulative returns of the factors of a dataset (US, Europe or developed markets; 3 factors, 5 factors or momentum), in US dollars, last 10 years by default (parameter `start`). Source: Kenneth R. French Data Library |

## Pre-assembled Apps

### Fonrex — EU Markets
A comprehensive dashboard for analyzing a single ticker:
- **Overview** tab: Quote, deep fundamentals, EOD chart
- **Valuation** tab: DCF valuation + sensitivity matrix
- **Technical** tab: Full technical chart with indicators
- **News** tab: Latest news from 7 providers
- **Watchlist** tab: Batch quotes
- **Factors** tab: Fama/French 5-factor exposure of the ticker + European factor returns

### Fonrex — Screener & Macro
An idea-generation dashboard:
- **Screener** tab: Technical screener (e.g. RSI < 30 for oversold stocks)
- **Macro Context** tab: Current macro rates (FRED and ECB), Eurostat indicators of the euro area, inflation of the main euro area countries over time, the US Treasury curve (today, 1 month and 1 year before) and the 10-year minus 2-year spread, the positioning of the leveraged funds on the E-mini S&P 500 and the most extreme positioning of the financial futures (CFTC) + index constituents

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

- Documentation: https://fonrex.io
- Issues: https://github.com/fonrex/fonrex/issues
