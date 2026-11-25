# Progress & Multi-Session Handoff

## Current State

- **Verification**: `make fast` (rapid static & guard loop) and `make ci` (full quality gate after any harness or code change)
- **Active focus**: DCF risk-free rate by currency, phase 2 of issue #70 (see Active / Multi-Session Work below)

## Active / Multi-Session Work

- **Provider API resilience**: Recent releases added Dylan JSON API fallbacks for WallStreetJournal (`#51`), MarketWatch (`#50`), and Barron's (`#52`), and QuoteSummary support for MSN (`#55`). Remaining anti-bot blocked providers rely on `FONREX_PROXY_URL` (Fonrex Relay in development).
- **Risk metrics (`GET /risk/{ticker}`), phase 1**: computed in Fonrex (numpy), with no new dependency. Branch `feat/risk-metrics-engine`, one commit per step, one pull request after step 4. FinanceToolkit is not a dependency: its figures served once as reference values (`tests/test_risk_metrics.py`).
  1. Done: pure engine `risk/metrics.py` (volatility, drawdown, historical and Gaussian VaR/CVaR, Calmar, Sharpe, Sortino).
  2. Done: OECD 3-month rates (`financials/providers/oecd.py`, `macro/short_rates.py`, `scripts/load_short_rates.py`), stored monthly in `macro_observations` (source `oecd`, indicator `rate_3m`). Seen in the answer of 2026-11-20: the euro area is `EA20`; most areas run to the month before last, `JPN` one month less, and **`GBR` stops in 2026-02** — the repository must not carry it over for long (a source for GBP, such as SONIA of the Bank of England, is a later step).
  3. Done: `macro/risk_free_rates.py` — USD from the Treasury `3M` (a rate carried 7 days at most), other currencies from the OECD area of the currency (a month carried 3 months at most), `GBX` read as `GBP`; no rate and the reason for a currency without source or a day beyond the bounds (never `DCF_RISK_FREE_RATE`). GBP has no rate after May 2026 until another source is added.
  4. Done: `GET /risk/{ticker}` (`risk/service.py`, `routers/risk.py`, `schemas/risk.py`; OECD registered in `SPECIALIZED_PROVIDER_SPECS`). Phase 1 complete: open the pull request of the branch.
  Review fixes: the OECD rows are inserted 3,000 at a time (9 columns: under the 32,767 parameters asyncpg takes; 5,000 failed on the first reading of the whole history); an answer without the rate of a currency that has a source is not cached; an ingestion drops the `risk:` entries of the ticker. Copilot review: one price basis per window (`adj_close` only when every bar has one, else `close` and a warning); the OECD is not asked again for `OECD_RETRY_MINUTES` after a failure; `/risk` answers 503 when `db_available` is false; the confidence is written in full in the cache key.
  Follow-ups of phase 1, branch `feat/risk-phase-1-follow-ups` (one pull request, one commit per step):
  1. Done: the FinanceToolkit reference figures matched within 1e-15 in absolute terms (`tests/test_risk_metrics.py`; one unit of the last place on a drawdown of -0.038 is already a relative gap above 1e-15).
  2. Done: the series of the OECD across the end of the LIBOR, checked on 2026-11-25: `EA20` is the monthly mean of the 3-month Euribor (so the ECB is not read), `CHE` follows the SARON with no break.
  3. Done: GBP from the monthly SONIA of the Bank of England (`financials/providers/boe.py`, series `IUMASOIA`, since 1997), kept by `ShortRates` with `SONIA_SERIES` (source `boe`, indicator `rate_on`, `geo` `GBR`; `BOE_REFRESH_HOURS`, `BOE_RETRY_MINUTES`); `scripts/load_short_rates.py` reads both sources (`--source` for one).
  4. Done: the `fonrex_risk` OpenBB widget (`GET /openbb/risk/{ticker}`, in the Factors tab of the EU Markets app) and the `FONREX_RISK` formula of the Sheets connector.
  Later: the pages of the documentation site (EN/FR/ZH) for `/risk`, its widget, its formula and the new settings (separate repository).
- **DCF risk-free rate by currency, phase 2 of issue #70**: branch `feat/dcf-long-rates`, one commit per step, one pull request after step 4. Decisions (to write on the issue): D1 EUR from the ECB, then the OECD `DEU` (not `EA20`, a mean of the whole euro area); D2 USD from FRED, then the Treasury curve, then the OECD `USA`; D3 one request a measure at the OECD; D4 a monthly rate at most 3 months old; D5 a DCF on `DCF_RISK_FREE_RATE` is not cached when its currency has a source.
  1. Done: the OECD 10-year yields (`IRLT`) read by `OECDProvider.long_rates` (fixture `tests/fixtures/oecd/irlt_monthly.csv`, captured on 2026-11-25: 44 areas, `GBR` goes on, no `ISL` nor `IDN`).
  2. Done: stored in `macro_observations` (source `oecd`, indicator `rate_10y`) by `ShortRates` with `OECD_LONG_SERIES`; `scripts/load_short_rates.py --source oecd_10y`.
  3. Done: `macro/long_rates.py` (`LongRateRepository`): the latest 10-year rate of a currency, `oecd_live` / `oecd_cached` / `oecd_stale`, or no rate with the reason.
  4. Done: the DCF discounts with the 10-year rate of its currency (`valuation/dcf_service.py`), `DCF_RISK_FREE_RATE` only without source, with a warning saying why; the routes `/dcf` do not cache such a valuation when the currency has a source. Phase 2 complete: open the pull request of the branch, tick 2.1 and 2.2 on issue #70.
  Later: phase 3 of issue #70 (the missing lines of the financial statements); the pages of the documentation site (EN/FR/ZH) for the DCF rate sources, the new source codes and the warning (separate repository).
- **Module quality & coverage**: See the health summary in [docs/testing-and-quality.md](docs/testing-and-quality.md#module-quality-and-health-scores) and exact per-module floors in `scripts/check_coverage_distribution.py`.

## Next Steps

1. Raise the coverage floor of `monitoring/canary_monitor.py` in `scripts/check_coverage_distribution.py`.
2. Add direct unit tests for historical gap detection (`historical/ingestion_service.py`) and DCF weighted consensus (`valuation/dcf_service.py`).
3. Route the remaining direct HTTP clients (`macro/fred_service.py`, `macro/ecb_service.py`, `macro/eurostat_service.py`, `macro/fx_rates.py`, `factors/french_library.py`) through `financials/providers/base.py` (see Known Limits in `ARCHITECTURE.md`).
4. Add a retention purge or hypertable policy for `news_articles` (currently unbounded).

## Blockers / Notes

- Database integration tests (`tests/test_timescale_integration.py`) require Docker (`make test-db` or `FONREX_TEST_DATABASE_URL`).
- Do not lower any coverage floor in `scripts/check_coverage_distribution.py` or `pyproject.toml`; only raise floors as coverage improves.
