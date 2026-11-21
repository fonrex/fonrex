# Progress & Multi-Session Handoff

## Current State

- **Verification**: `make fast` (rapid static & guard loop) and `make ci` (full quality gate after any harness or code change)
- **Active focus**: Risk metrics, phase 1 (see Active / Multi-Session Work below)

## Active / Multi-Session Work

- **Provider API resilience**: Recent releases added Dylan JSON API fallbacks for WallStreetJournal (`#51`), MarketWatch (`#50`), and Barron's (`#52`), and QuoteSummary support for MSN (`#55`). Remaining anti-bot blocked providers rely on `FONREX_PROXY_URL` (Fonrex Relay in development).
- **Risk metrics (`GET /risk/{ticker}`), phase 1**: computed in Fonrex (numpy), with no new dependency. Branch `feat/risk-metrics-engine`, one commit per step, one pull request after step 4. FinanceToolkit is not a dependency: its figures served once as reference values (`tests/test_risk_metrics.py`).
  1. Done: pure engine `risk/metrics.py` (volatility, drawdown, historical and Gaussian VaR/CVaR, Calmar, Sharpe, Sortino).
  2. Done: OECD 3-month rates (`financials/providers/oecd.py`, `macro/short_rates.py`, `scripts/load_short_rates.py`), stored monthly in `macro_observations` (source `oecd`, indicator `rate_3m`). Seen in the answer of 2026-11-20: the euro area is `EA20`; most areas run to the month before last, `JPN` one month less, and **`GBR` stops in 2026-02** — the repository must not carry it over for long (a source for GBP, such as SONIA of the Bank of England, is a later step).
  3. Done: `macro/risk_free_rates.py` — USD from the Treasury `3M` (a rate carried 7 days at most), other currencies from the OECD area of the currency (a month carried 3 months at most), `GBX` read as `GBP`; no rate and the reason for a currency without source or a day beyond the bounds (never `DCF_RISK_FREE_RATE`). GBP has no rate after May 2026 until another source is added.
  4. Done: `GET /risk/{ticker}` (`risk/service.py`, `routers/risk.py`, `schemas/risk.py`; OECD registered in `SPECIALIZED_PROVIDER_SPECS`). Phase 1 complete: open the pull request of the branch.
  Review fixes: the OECD rows are inserted 3,000 at a time (9 columns: under the 32,767 parameters asyncpg takes; 5,000 failed on the first reading of the whole history); an answer without the rate of a currency that has a source is not cached; an ingestion drops the `risk:` entries of the ticker. Copilot review: one price basis per window (`adj_close` only when every bar has one, else `close` and a warning); the OECD is not asked again for `OECD_RETRY_MINUTES` after a failure; `/risk` answers 503 when `db_available` is false; the confidence is written in full in the cache key.
  Later: a GBP rate source (SONIA of the Bank of England); `/risk` in the OpenBB widgets and the Sheets connector; the pages of the documentation site (EN/FR/ZH).
- **Module quality & coverage**: See the health summary in [docs/testing-and-quality.md](docs/testing-and-quality.md#module-quality-and-health-scores) and exact per-module floors in `scripts/check_coverage_distribution.py`.

## Next Steps

1. Raise the coverage floor of `monitoring/canary_monitor.py` in `scripts/check_coverage_distribution.py`.
2. Add direct unit tests for historical gap detection (`historical/ingestion_service.py`) and DCF weighted consensus (`valuation/dcf_service.py`).
3. Route the remaining direct HTTP clients (`macro/fred_service.py`, `macro/ecb_service.py`, `macro/eurostat_service.py`, `macro/fx_rates.py`, `factors/french_library.py`) through `financials/providers/base.py` (see Known Limits in `ARCHITECTURE.md`).
4. Add a retention purge or hypertable policy for `news_articles` (currently unbounded).

## Blockers / Notes

- Database integration tests (`tests/test_timescale_integration.py`) require Docker (`make test-db` or `FONREX_TEST_DATABASE_URL`).
- Do not lower any coverage floor in `scripts/check_coverage_distribution.py` or `pyproject.toml`; only raise floors as coverage improves.
