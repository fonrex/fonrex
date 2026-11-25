# Bank of England: monthly SONIA

Real answers of the Interactive Statistical Database of the Bank of England,
captured on 2026-11-25. Tests read these files: no test reaches the Bank of England.

The Bank publishes the mean of each month of SONIA, the overnight rate of the
pound, as series `IUMASOIA` (percent a year), free and without key:

```
https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/1990&Dateto=now&SeriesCodes=IUMASOIA&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N
```

| File | What it shows |
|---|---|
| `sonia_monthly.csv` | The whole answer from 1990, unchanged (7,108 bytes, `\r\n` line ends): the header `DATE,IUMASOIA`, then one row a month from `31 Jan 1997` to `31 Oct 2026`, a month dated by its last day |
| `empty_period.csv` | The whole answer for a period without value (from November 2026): the header alone |
| `error_unknown_series.html` | The answer for an unknown series code (`IUMAXXXX`), HTTP 200 and `text/html`: reduced to its head up to the title `Error` and the message `Invalid series code value supplied.`; the navigation, the footer and the scripts are left out, the kept lines are as sent |

What the answers showed:

- the series starts in January 1997, when SONIA was introduced; October 2026 was
  already published on 2026-11-25, a few days after the end of the month;
- the values have up to four decimals and are never empty;
- the content type is `application/csv` for a series, `text/html` for an error.
