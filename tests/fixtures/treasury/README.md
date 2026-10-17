# US Treasury yield curve files

Reduced real answers of the US Treasury, captured on 2026-10-17. Tests read these
files: no test reaches the Treasury.

The Treasury publishes one CSV file per year and curve, free and without key:

```
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type={type}&field_tdr_date_value={year}&page&_format=csv
```

| File | `type` and year | What it shows |
|---|---|---|
| `nominal_2026.csv` | `daily_treasury_yield_curve`, 2026 | The 14 maturities of today, from `1 Mo` to `30 Yr`, with `1.5 Month` |
| `nominal_2025.csv` | same, 2025 | `1.5 Month` starts on 2025-02-18: empty cells before |
| `nominal_2022.csv` | same, 2022 | `4 Mo` starts on 2022-10-19: empty cells before |
| `nominal_2018.csv` | same, 2018 | `2 Mo` starts on 2018-10-16: empty cells before |
| `nominal_2002.csv` | same, 2002 | `30 Yr` stops after 2002-02-15: empty last cells after |
| `nominal_2004.csv` | same, 2004 | No `30 Yr` column at all (2003 to 2005) |
| `nominal_1990.csv` | same, 1990 | The first year: 9 maturities, from `3 Mo` |
| `real_2026.csv` | `daily_treasury_real_yield_curve`, 2026 | Real yields (TIPS), `5 YR` to `30 YR` |
| `real_2003.csv` | same, 2003 | The first year of real yields: `5 YR`, `7 YR`, `10 YR` |
| `empty_1989.csv` | `daily_treasury_yield_curve`, 1989 | A year without data: HTTP 200 and an empty body |

How they were reduced, nothing else changed (quotes, decimals, line ends are as sent):

- the header and a few days are kept: the latest and the earliest day of the year,
  and the two days around the start or the end of a maturity;
- days stay in the order of the file, the latest first.

What every file of 1990 to 2026 has in common (checked on all 37 years of both curves):

- `Date` is `MM/DD/YYYY`; values are percents with two decimals, never negative,
  never `N/A`; a maturity not published that day is an empty cell;
- lines end with `\n`, the last line has none, there is no byte order mark;
- the nominal labels are `1 Mo`, `1.5 Month`, `2 Mo`, `3 Mo`, `4 Mo`, `6 Mo`, `1 Yr`,
  `2 Yr`, `3 Yr`, `5 Yr`, `7 Yr`, `10 Yr`, `20 Yr`, `30 Yr`; the real ones are
  `5 YR`, `7 YR`, `10 YR`, `20 YR`, `30 YR`;
- a full year has 248 to 252 days; the file of every year (`.../all/all?...`)
  is refused (HTTP 403), so the history is read one year at a time.

`expected.json` gives what a reading must find: the maturities of each file in
order (as codes: `1M`, `6W` for `1.5 Month`, `10Y`…), the number of days, the
points of one day as ratios (`0.0509` for 5.09 %), the maturities absent on a
day, and, for 2026-10-16, the breakeven inflation (nominal minus real) and the
10-year minus 2-year spread.
