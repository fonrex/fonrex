# OECD 3-month interest rates

A reduced real answer of the OECD, captured on 2026-11-20. Tests read this file: no
test reaches the OECD.

The OECD publishes its Short-Term Economic Statistics through a free SDMX API
without key. One request returns the 3-month interbank rate (`IR3TIB`) of every
country, the reference area left empty in the key:

```
https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/.M.IR3TIB.PA._Z._Z._Z._Z.N?startPeriod=2025-06&dimensionAtObservation=AllDimensions
Accept: application/vnd.sdmx.data+csv; charset=utf-8
```

| File | What it shows |
|---|---|
| `ir3tib_monthly.csv` | 8 areas of the 45 of the answer: `EA20` (euro area), `CHE`, `GBR`, `SWE`, `NOR`, `DNK`, `POL`, `JPN`; the months from 2026-07 on, and for `GBR` its last three months |

How it was reduced, nothing else changed (column order, decimals, `\r\n` line ends
are as sent): the header and the rows of these areas and months are kept, in the
order of the answer (by area, then by month, the areas in no fixed order).

What the whole answer from 2025-06 showed (45 areas):

- every row is `FREQ` `M`, `MEASURE` `IR3TIB`, `UNIT_MEASURE` `PA` (percent a year),
  `UNIT_MULT` `0`, `OBS_STATUS` `A`; no empty value;
- the euro area is `EA20`; the other areas are ISO 3166 alpha-3 codes;
- most areas run to 2026-10, a month and a half before the capture; `JPN`, `CHN`
  and `COL` to 2026-09; `IDN` stops in 2025-12 and **`GBR` in 2026-02**;
- the Swiss rate is negative (`-0.0415` for 2026-10);
- a period without any value answers HTTP 404 with the body `NoRecordsFound`.
