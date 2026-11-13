# Progress & Multi-Session Handoff

## Current State

- **Last commit**: `cd21813` (`v2.16.0` + 1 — `fix(enrichment): store the asset profile during enrichment (#54)`)
- **Verification**: `make ci` (run after any harness or code change)
- **Active focus**: Harness engineering improvements (progressive disclosure in `docs/`, `PROGRESS.md`, `Makefile` & `AGENTS.md` ergonomics)

## Active / Multi-Session Work

- **Provider API resilience**: Recent releases added Dylan JSON API fallbacks for WallStreetJournal (`#51`), MarketWatch (`#50`), and Barron's (`#52`), and QuoteSummary support for MSN (`#55`). Remaining anti-bot blocked providers rely on `FONREX_PROXY_URL` (Fonrex Relay in development).
- **Module quality & coverage**: See the health score table in [docs/testing-and-quality.md](docs/testing-and-quality.md#module-quality-and-health-scores).

## Next Steps

1. Raise coverage floor of `monitoring/canary_monitor.py` (currently `44.0%` in `scripts/check_coverage_distribution.py`).
2. Add direct unit tests for historical gap detection (`historical/ingestion_service.py`, floor `64.0%`) and DCF weighted consensus (`valuation/dcf_service.py`).
3. Route `macro/fred_service.py` HTTP calls through `financials/providers/base.py` or a shared client policy (see Known Limits in `ARCHITECTURE.md`).
4. Add a retention purge or hypertable policy for `news_articles` (currently unbounded).

## Blockers / Notes

- Database integration tests (`tests/test_timescale_integration.py`) require Docker (`make test-db` or `FONREX_TEST_DATABASE_URL`).
- Do not lower any coverage floor in `scripts/check_coverage_distribution.py` or `pyproject.toml`; only raise floors as coverage improves.
