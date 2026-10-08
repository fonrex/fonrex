# Fonrex Sheets Connector

Connect your Google Sheets to **your own Fonrex instance** to retrieve financial data (fundamentals, DCF valuations, technical indicators) directly into your spreadsheet — without writing any code.

> **This template displays raw financial data for informational purposes only.** It does not constitute investment advice. All displayed values (including DCF valuations) are analytical outputs. Always do your own research before making any investment decisions.

---

## Prerequisites

- A running **self-hosted Fonrex instance** (`docker compose up -d`, see the main README)
- A **tunnel** that gives this instance a public HTTPS URL (see step 2)
- A Google account

Google Sheets runs on Google's servers: it cannot reach `localhost`. The connector
therefore calls *your* instance through a tunnel. No data goes through a Fonrex-operated
service, and no paid plan is involved.

---

## 4-Step Installation

### Step 1 — Create a read-only key for the spreadsheet

The key will be stored in your Google account, outside the machine running Fonrex.
Give the spreadsheet a key that can read data but cannot clear the cache, clean the
database or trigger ingestion. In the `.env` of your instance:

```bash
# generate a key
echo "frx_live_$(openssl rand -hex 24)"
```

```
FONREX_READ_ONLY_API_KEYS=frx_live_<the generated value>
```

Then restart the API: `docker compose up -d`.

### Step 2 — Expose your instance through a tunnel

Any tunnel giving an HTTPS URL works (zrok, Cloudflare Tunnel, Tailscale Funnel, ngrok…).
Example with [zrok](https://zrok.io), once your environment is enabled (`zrok2 enable <token>`):

```bash
# temporary URL, valid until you stop the command
zrok2 share public localhost:5000

# or a stable URL: reserve a name once, then share with it
zrok2 create name -n public myfonrex
zrok2 share public localhost:5000 -n public:myfonrex
# → https://myfonrex.share.zrok.io
```

With zrok 1.x the command is `zrok` instead of `zrok2`; refer to the zrok documentation
of your version. Prefer a stable URL: with a temporary one you must reconfigure the
spreadsheet each time the tunnel restarts.

Check the URL from another network before going further:

```bash
curl https://myfonrex.share.zrok.io/health
```

> **While the tunnel runs, your instance is reachable from the Internet.** Only `/health`,
> the API documentation, the OpenBB discovery files and static files (logos) answer without
> a key; everything else requires one. Keep `FONREX_AUTH_REQUIRED` enabled, never share a full-access key,
> and stop the tunnel when you do not need the spreadsheet.

### Step 3 — Copy the template

Click the link below to create your own copy of the template:

👉 **[Open the Fonrex Sheets template](https://docs.google.com/spreadsheets/d/1PUBLISHED_TEMPLATE_ID_XYZ_1234567890/copy)**

> You will get a personal copy of the file in your Google Drive. The original file will never be modified.

### Step 4 — Connect the spreadsheet to your instance

1. In your copy of the Sheet, open the **Fonrex** menu (at the top, next to "Help")
2. **Configure Instance URL** → paste the public URL of your tunnel (`https://…`)
3. **Configure API Key** → paste the read-only key created in step 1

> **Security**: the URL and the key are stored in the Google Apps Script User Properties — they are unique to your Google account, never stored in a cell, and are not shared if you share the document with someone else.

Then add your tickers:

1. Open the **Watchlist** sheet
2. Add your stock symbols in column A (starting from row 2), one per row  
   Examples: `AAPL`, `AIR.PA`, `MC.PA`, `MSFT`
3. Alternatively, use **Fonrex > Add Ticker to Watchlist** to add them via the menu

And run an initial refresh:

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
| **Instance and tunnel must be running** | A refresh fails when your instance or its tunnel is stopped |
| **One call per ticker per endpoint** | A refresh makes your instance query its providers for each ticker of the Watchlist |
| **Reads only** | The connector only sends `GET` requests; a read-only key is enough |

---

## Troubleshooting

| Error | Probable Cause | Solution |
|---|---|---|
| `Configure your instance URL first` | URL not configured | Fonrex > Configure Instance URL |
| `Configure API key first` | Key not configured | Fonrex > Configure API Key |
| `API key refused by your instance` | Key not listed in the `.env` of the instance, or API not restarted | Check `FONREX_READ_ONLY_API_KEYS`, then `docker compose up -d` |
| `Instance unreachable` | Tunnel stopped or URL changed | Restart the tunnel, update the URL if it changed |
| `The tunnel answered instead of Fonrex` | The tunnel runs but the instance does not | `docker compose ps`, then `docker compose up -d` |
| `Instance error or tunnel down (status 5xx)` | Error in the instance, or tunnel without backend | `docker compose logs fonrex-api` |
| `Ticker not found` | Symbol unknown to the API | Check ticker format (e.g., `AIR.PA` not `AIR`) |
| "Fonrex" menu missing | Script not authorized | Refresh the page, accept requested permissions |

---

## Security

- The script only calls the instance URL **you** configure, over HTTPS, with `GET` requests.
  No data is sent to any other server.
- The Apps Script manifest (`appsscript.json`) declares the OAuth scopes: read/write in the
  current spreadsheet only, and external requests (needed to call your instance). It has no
  fixed list of callable domains, because the URL of your tunnel is yours.
- Use a **read-only key** (`FONREX_READ_ONLY_API_KEYS`): even if the key leaks, it cannot
  clear the cache, clean the database, trigger ingestion or change subscriptions.

---

## Documentation & Support

- API Documentation: [docs.fonrex.io](https://docs.fonrex.io)
- zrok documentation: [zrok.io](https://zrok.io)
- GitHub: [github.com/fonrex/fonrex](https://github.com/fonrex/fonrex)

---

*Fonrex Sheets Connector v1.1 — This connector is provided "as is" for informational purposes only. It does not constitute investment advice.*
