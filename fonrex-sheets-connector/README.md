# Fonrex Sheets Connector

Connect your Google Sheets to the Fonrex API to retrieve financial data (fundamentals, DCF valuations, technical indicators) directly into your spreadsheet — without writing any code or setting up a server.

> **This template displays raw financial data for informational purposes only.** It does not constitute investment advice. All displayed values (including DCF valuations) are analytical outputs. Always do your own research before making any investment decisions.

---

## Prerequisites

- An active **Fonrex Relay** account (paid plan required)
- An API key in the `frx_live_...` format — available at [fonrex.io/relay](https://fonrex.io/relay)
- A Google account

---

## 3-Step Installation

### Step 1 — Copy the template

Click the link below to create your own copy of the template:

👉 **[Open the Fonrex Sheets template](https://docs.google.com/spreadsheets/d/1PUBLISHED_TEMPLATE_ID_XYZ_1234567890/copy)**

> You will get a personal copy of the file in your Google Drive. The original file will never be modified.

### Step 2 — Configure your API key

1. In your copy of the Sheet, click on the **Fonrex** menu (at the top, between "Help" and the other menus)
2. Click on **Configure API Key**
3. Paste your API key (`frx_live_...`) into the field and confirm

> **Security**: Your key is stored in the Google Apps Script User Properties — it is unique to your Google account, never stored in a cell, and will not be shared if you share the document with someone else.

### Step 3 — Add your tickers

1. Open the **Watchlist** sheet
2. Add your stock symbols in column A (starting from row 2), one per row  
   Examples: `AAPL`, `AIR.PA`, `MC.PA`, `MSFT`
3. Alternatively, use **Fonrex > Add Ticker to Watchlist** to add them via the menu

Then run an initial refresh:

- **Fonrex > Refresh Fundamentals** → populates the *Fundamentals* sheet
- **Fonrex > Refresh DCF Valuations** → populates the *DCF* sheet
- **Fonrex > Refresh Technical Indicators** → populates the *Technicals* sheet

---

## Template Sheets

| Sheet | Content |
|---|---|
| **Config** | Connection status, last refresh date, legal warning |
| **Watchlist** | List of tracked tickers (column A, starting from row 2) |
| **Fundamentals** | P/E, ROE, ROA, Market Cap, Dividend Yield, Beta, 52W High/Low… |
| **DCF** | Consensus value, current price, consensus upside %, WACC, FCF value |
| **Technicals** | RSI 14, MACD, SMA 50/200, EMA 20, Bollinger Bands, ATR 14 |
| **Charts** | Native charts based on imported data (to be configured freely) |

---

## Custom Formulas (Optional)

You can also use formulas directly in any cell:

| Formula | Description |
|---|---|
| `=FONREX_PE("AIR.PA")` | P/E ratio |
| `=FONREX_DIVIDEND_YIELD("AIR.PA")` | Dividend yield (decimal) |
| `=FONREX_INTRINSIC_VALUE("AIR.PA")` | DCF consensus value |
| `=FONREX_RSI("AAPL")` | 14-period RSI |

> ⚠️ **30-minute cache**: Custom formulas are cached by Google for 30 minutes. For fresh data, use the **Refresh** buttons in the menu instead of these formulas.

---

## Limitations

| Limitation | Detail |
|---|---|
| **Manual refresh** | No real-time automatic updates — Apps Script does not support WebSockets |
| **Formula cache** | `=FONREX_PE(...)` etc.: 30-minute cache imposed by Google, not configurable |
| **API Quota** | The connector consumes your Fonrex Relay quota normally (one call per ticker per endpoint) |
| **Paid plan required** | The connector does not work with the free tier — an active API key is required |
| **Restricted domain** | The script is configured to only call `api.fonrex.io` — no other external requests are possible |

---

## Troubleshooting

| Error | Probable Cause | Solution |
|---|---|---|
| `Configure API key first` | Key not configured | Fonrex > Configure API Key |
| `Invalid or expired API key` | Incorrect or expired key | Check on fonrex.io/relay |
| `Quota exceeded` | Plan exhausted | Check your Fonrex Relay quota |
| `Ticker not found` | Symbol unknown to the API | Check ticker format (e.g., `AIR.PA` not `AIR`) |
| `Fonrex service temporarily unavailable` | API downtime | Try again in a few minutes |
| "Fonrex" menu missing | Script not authorized | Refresh the page, accept requested permissions |

---

## Security

The Apps Script manifest (`appsscript.json`) explicitly declares:

- The necessary OAuth scopes (read/write only in the current spreadsheet)
- The whitelist of callable domains: **`api.fonrex.io` only**

No data is sent to any server other than the Fonrex API.

---

## Documentation & Support

- API Documentation: [docs.fonrex.io](https://docs.fonrex.io)
- Get an API key: [fonrex.io/relay](https://fonrex.io/relay)
- GitHub: [github.com/fonrex/fonrex](https://github.com/fonrex/fonrex)

---

*Fonrex Sheets Connector v1.0 — This connector is provided "as is" for informational purposes only. It does not constitute investment advice.*
