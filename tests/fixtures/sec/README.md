# SEC XBRL answers

Reduced real answers of the SEC, captured on 2026-10-16. Tests read these files:
no test reaches the SEC.

| File | Request |
|---|---|
| `companyfacts_aapl.json` | `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json` (Apple, 10-K, US GAAP) |
| `companyfacts_jpm.json` | `…/companyfacts/CIK0000019617.json` (JPMorgan Chase, a bank, 10-K, US GAAP) |
| `companyfacts_sap.json` | `…/companyfacts/CIK0001000184.json` (SAP, 20-F, IFRS, in EUR) |
| `companyfacts_nvs.json` | `…/companyfacts/CIK0001114448.json` (Novartis, 20-F, IFRS, in USD) |
| `company_tickers_exchange.json` | `https://www.sec.gov/files/company_tickers_exchange.json` (12 of its 10 444 rows) |
| `error_unknown_cik.xml` | `…/companyfacts/CIK0000000001.json`: HTTP 404, an XML answer |

How they were reduced, nothing else changed (key order, numbers and text are as sent):

- only the concepts Fonrex reads are kept, and only the facts whose period ends
  between 2024-06-01 and 2026-12-31;
- the `description` of each concept is removed (long texts of the taxonomy);
- only the facts of the annual filings (10-K, 20-F) are kept, except Apple's
  revenue, which keeps its 10-Q facts: quarters and nine months, that an
  annual reading must leave out.

The full answers are large: 3.8 MB for Apple, 4.9 MB for Microsoft, 8 MB for
JPMorgan. `expected.json` gives what a reading must find.
