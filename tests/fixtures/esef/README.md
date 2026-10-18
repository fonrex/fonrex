# ESEF fixtures (filings.xbrl.org)

Real answers of filings.xbrl.org and GLEIF, downloaded on 18 October 2026, for six
companies that each test a trap:

| Folder key | Company | ISIN | LEI | Trap |
|---|---|---|---|---|
| `total` | TotalEnergies SE | FR0000120271 | 529900S21EQ1BO4ESM68 | French company reporting in **USD**; one filing per language (`-GB-`, `-FR-`); two revenues (with and without excise tax) |
| `airbus` | Airbus SE | NL0000235190 | MINO79WLOO247M1IL051 | filed in the Netherlands |
| `bnp` | BNP Paribas | FR0000131104 | R0MUWSFPU8MPRO8K5P83 | bank: no revenue, many extension concepts (`bnpp:`); two filings per year (`-0`, `-1`) |
| `pernod` | Pernod Ricard | FR0000120693 | 52990097YFPX9J0H5D87 | fiscal year ending in **June**; the filing listed as `2021-12-31` holds the year ended 2021-06-30 |
| `novo` | Novo Nordisk A/S | DK0062498333 | 549300DAQ1CVT6CXN342 | reports in **DKK**; files its **quarterly** reports too |
| `sap` | SAP SE | DE0007164600 | 529900D6BF99LW9R2E68 | German: **no filing** on filings.xbrl.org |

## Files

- `<key>_gleif.json`: `GET https://api.gleif.org/api/v1/lei-records?filter[isin]=<ISIN>`.
- `<key>_filings.json`: `GET https://filings.xbrl.org/api/entities/<LEI>/filings?page[size]=100`.
- `<fxo_id>.json`: the xBRL-JSON of a filing (`json_url` of the filing), the oldest and the
  latest of each company.
- `errors_filings.json`: `GET /api/filings?filter[country]=FR&sort=-error_count`: the API
  answers **500** to a sort on `error_count`.
- `expected.json`: values read straight from the facts (concept, period, value), to test
  the reader against.

The xBRL-JSON files are **trimmed**: the originals weigh 0.1 to 32 MB, almost all of it
text blocks of the notes. Only the facts with a unit are kept, and `documentInfo` keeps
`documentType`, `features`, `namespaces` and `taxonomy`. The annual statements read from
the trimmed files are the same as from the originals.

## What the files show

- **Periods**: `"2025-01-01T00:00:00/2026-01-01T00:00:00"` for a year, `"2026-01-01T00:00:00"`
  for a balance. The end is **exclusive**: both mean the year, or the balance, at
  2025-12-31.
- **Units**: `iso4217:EUR`, `iso4217:EUR/xbrli:shares`, `xbrli:shares`; text facts have none.
- **Values** are strings, sometimes `"1305000000"`, sometimes `"73420000000.0"`.
- **Dimensions**: a fact with a dimension other than `concept`, `entity`, `period`, `unit`,
  `language` is a breakdown (segment, component of equity): only the others are totals.
- **The period of a filing** given by the API (`period_end`) can be wrong (Pernod): the
  periods are read from the facts. Quarterly and half-year facts (Novo) are not fiscal years.
- **Concepts**: revenue is `Revenue` (Airbus, Novo) or `RevenueFromContractsWithCustomers`
  (TotalEnergies, Pernod). A company without minority interests may tag only `ProfitLoss`
  and `Equity` (Novo). The weighted average number of shares is often not tagged.
- **Duplicates**: the same year can be filed in several languages or twice (`-0`, `-1`).
- **Errors**: `error_count` is given per filing (BNP 2024: 3, Novo 2023: 80).
