# CFTC Commitments of Traders files

Reduced real answers of the CFTC, captured on 2026-10-18 from inside the
`fonrex-api` container (the same HTTPS stack as the application). Tests read these
files: no test reaches the CFTC.

The CFTC publishes one zip per report and year, free and without key, each holding a
single comma-separated text file:

```
https://www.cftc.gov/files/dea/history/{file}
```

| File | Report | Inner file | What it shows |
|---|---|---|---|
| `deacot2026.zip` | Legacy, 2026 | `annual.txt` | 8 weeks (2026-08-25 to 2026-10-13) of E-mini S&P 500, UST 10Y note, WTI, gold, euro, the Nasdaq-100 consolidated market `20974+`, and oats (`004603`), a market that is in the report some weeks only |
| `deacot2025.zip` | Legacy, 2025 | `annual.txt` | A full year (52 weeks) of E-mini S&P 500 and gold |
| `deacot1986_2016.zip` | Legacy, 1986 to 2016 in one file | `FUT86_16.txt` | Gold and the 10-year note on reports twice a month (1986, 1987, September 1992), then weekly (from October 1992), a holiday week (Friday 2001-12-28, then 2002-01-08), a negative non-reportable position (10-year note, 1987-06-30), and a market without contract units (cheddar cheese, 1995-04-04) |
| `fut_fin_txt_2026.zip` | Traders in Financial Futures (TFF), 2026 | `FinFutYY.txt` | 8 weeks of E-mini S&P 500, UST 10Y note and euro |
| `fin_fut_txt_2006_2016.zip` | TFF, 2006 to 2016 in one file | `F_TFF_2006_2016.txt` | The first report (2006-06-13), a Monday report (2012-12-24), the last week of 2016, and the Canadian dollar with empty "change" cells |
| `fut_disagg_txt_2026.zip` | Disaggregated, 2026 | `f_year.txt` | 8 weeks of WTI, gold and SRW wheat |
| `fut_disagg_txt_hist_2006_2016.zip` | Disaggregated, 2006 to 2016 in one file | `F_Disagg06_16.txt` | WTI and gold on the first report, a Monday report (2008-12-22) and the last week of 2016 |
| `not_a_zip.zip` | — | — | Made up, not from the CFTC: an HTML error page where a zip was expected |

How they were reduced, nothing else changed (quotes, padding, decimals and line ends
are as sent; the inner file keeps its name and date): the header and the lines of a
few markets and weeks are kept, in the order of the file.

What the full files have in common (checked on ten complete files: Legacy
1986–2016, 2017, 2025 and 2026; TFF 2006–2016, 2025 and 2026; Disaggregated
2006–2016, 2025 and 2026):

- Each zip holds exactly one `.txt` file, UTF-8, without byte order mark. Its name
  changes with the report and the period (`annual.txt`, `FinFutYY.txt`,
  `f_year.txt`, `FUT86_16.txt`…). Every line ends with `\r\n`, the last one too.
- The header is the same for every period of a report: Legacy has 129 columns
  with spaced names (`Noncommercial Positions-Long (All)`), TFF 87 and
  Disaggregated 191 with underscored names (`Lev_Money_Positions_Long_All`).
  One Legacy name starts with a space: `" Total Reportable Positions-Long (All)"`.
- A market is keyed by `CFTC Contract Market Code`, six characters that are not
  always digits (`13874A`, `1170E1`, `20974+`) and keep their leading zeros
  (`001602`). There is one line per market and report date, never two.
- The report date is the third column. It is `YYYY-MM-DD` everywhere except in the
  TFF file of 2006 to 2016, where it is `M/D/YYYY 12:00:00 AM`. The second column
  (`YYMMDD`) always gives the same day.
- Legacy reports were published twice a month, around the 15th and the last day
  of the month, on any weekday, until September 1992, and every week from October
  1992. Weekly reports are dated on Tuesday, except in holiday weeks: a Monday or
  a Wednesday, and four Fridays (1997-12-19, 2001-12-21, 2001-12-28, 2003-02-14).
- Positions are whole numbers, padded with spaces (`"  506774"`). The TFF file of
  2006 to 2016 writes them with six decimals (`22364.000000`). Position cells are
  never empty.
- A missing value is a `.` (the weekly changes when the market was not in the
  previous report, some trader counts), or an empty cell in the TFF file of 2006
  to 2016.
- Non-reportable positions can be negative in the Legacy history (51 lines from
  1986 to 2003, for example the 10-year note on 1987-06-30). No other position of
  the `(All)` columns is ever negative, and none is after 2003.
- Long positions add up to the open interest, and short positions do too:
  non-commercial + spreading + commercial + non-reportable (Legacy),
  dealer + asset manager + leveraged funds + other reportable, each with its
  spreading, + non-reportable (TFF and Disaggregated). The only exceptions are the
  consolidated markets whose code ends with `+` (`12460+`, `13874+`, `20974+`),
  off by 1 to 4 contracts because of rounding.
- Market names change while the code stays: `067651` was `CRUDE OIL, LIGHT 'SWEET'`,
  then `CRUDE OIL, LIGHT SWEET`, and is `WTI-PHYSICAL` today; `043602` went from
  `6.5-10 YEAR U.S. TREASURY NOTE` to `10-YEAR U.S. TREASURY NOTES` to
  `UST 10Y NOTE`.
- The files of 2026 end with the report of Tuesday 2026-10-13. The inner file is
  dated Thursday 2026-10-15; the zip was put online on Friday 2026-10-16 at
  19:27 GMT (`Last-Modified`), the usual release time of 3:30 p.m. in New York.
- A file that does not exist (a year before the first one, or not published yet,
  such as `deacot2027.zip`) answers `404` with an HTML page.

`expected.json` gives, for each fixture, what a reading must find: the report, the
inner file, the number of columns and lines, the lines per market, the first and
last report dates, and the positions of a few markets on one date (open interest
and long, short and spreading of each category, as whole numbers). These values
were read from the full CFTC files, not from the fixtures, then checked against
the fixtures.
