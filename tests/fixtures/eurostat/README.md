# Eurostat answers

Reduced real answers of the Eurostat SDMX 2.1 API
(`https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{flow}/{key}?format=SDMX-CSV`),
captured on 2026-10-15. Rows are kept as Eurostat sent them (CRLF line ends);
only the period range was narrowed. Tests read these files: no test reaches Eurostat.

| File | Request (`{flow}/{key}?…`) |
|---|---|
| `hicp_total.csv` | `prc_hicp_minr/M.RCH_A.TOTAL.EA+FR+DE?startPeriod=2026-04` |
| `hicp_core.csv` | `prc_hicp_minr/M.RCH_A.TOT_X_NRG_FOOD.EA+FR?startPeriod=2026-06` |
| `unemployment.csv` | `une_rt_m/M.SA.TOTAL.PC_ACT.T.EA21+FR+DE?startPeriod=2026-06` |
| `gdp_growth.csv` | `namq_10_gdp/Q.CLV_PCH_PRE+CLV_PCH_SM.SCA.B1GQ.EA+FR?startPeriod=2025-Q3` |
| `sentiment.csv` | `ei_bssi_m_r2/M.BS-ESI-I+BS-CSMCI-BAL.SA.EA21+FR?startPeriod=2026-07` |
| `error_invalid_geo.xml` | `une_rt_m/M.SA.TOTAL.PC_ACT.T.EA+FR` — HTTP 400, `EA` is not a code of this flow |
| `error_unknown_dataset.xml` | `nope/M.X` — HTTP 404 |

`OBS_FLAG` `e` marks an estimate (the flash HICP of the latest month).
`expected.json` gives the values a parser must read: percentages as ratios
(`3.8` becomes `0.038`), the ESI as an index and the consumer confidence as a
balance, both stored as published; a period is the first day of the month or
quarter (`2026-Q2` becomes `2026-04-01`).
