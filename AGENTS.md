# Working on Fonrex — rules for coding agents and contributors

This file is the contract for anyone, human or AI agent (Gemini, Antigravity, Claude,
Copilot…), who changes this repository. Read it before writing code. Each rule below is
enforced by a test: if a test of the "guards" fails, fix the code, not the test.

## What Fonrex is — and is not

- Fonrex is **self-hosted software**. An instance fetches data from public sources and keeps
  it in the user's own database.
- There is **no Fonrex-operated data API**. Integrations (OpenBB Workspace, Google Sheets)
  connect to the user's own instance. Never point a client or a document to a hosted
  endpoint that would serve data.
- **Fonrex Relay** is a separate, optional service in development: an outbound relay for the
  provider requests. It relays pages; it does not store or serve financial data. This
  repository only needs the generic proxy setting (`FONREX_PROXY_URL`).

## Before you say a change is done

```bash
make install-dev   # Python 3.12 or 3.13: the exact versions of requirements-dev.lock
make ci            # lint, typing, migrations, tests, coverage
```

`make ci` is exactly what GitHub Actions runs. A change is not finished until it passes.
The CI also runs the database tests on a TimescaleDB service; locally they are skipped
unless `FONREX_TEST_DATABASE_URL` is set. Run them with `make test-db` (needs Docker)
after any change to a migration, to `prices_eod` or to SQL meant for PostgreSQL.
Never claim that something works without having run it; say what you could not verify.

## Invariants

| # | Rule | Enforced by |
|---|---|---|
| 1 | **One HTTP layer.** A provider never creates an HTTP client. Use `self._session()` (several requests sharing cookies), `self._get()`, `self._get_json()`, `self._post_json()` or `new_sync_client()` from `financials/providers/base.py`. Retries, pauses, concurrency limit and proxy live there only: no local retry loop. | `tests/test_provider_http_policy.py` |
| 2 | **Settings are real.** Read a setting with the helpers of `settings.py` (`env_int`, `env_decimal`, `env_choice`). Every variable of `.env.example` must be read by the code; document every new setting there. An invalid value falls back to the default with a warning, it never stops the API. | `tests/test_env_settings.py` |
| 3 | **Documents tell the truth.** A figure in `README.md` (providers, indicators, widgets) must match the code, and the tables of `ARCHITECTURE.md` list what exists: in the same change, add a new route to its routes table, a new migration to its migrations table, a new router or package to its module map. Do not write counts that go stale (test files, migrations, tables). Do not describe a hosted data API. | `tests/test_docs_consistency.py` |
| 4 | **Providers load, or the failure is visible.** Register a provider in `PROVIDER_SPECS` / `SPECIALIZED_PROVIDER_SPECS` (`main.py`). A provider that cannot be imported is reported by `/health`. | `tests/test_docs_consistency.py` |
| 5 | **Units are declared.** A provider returning percentages declares them in `monitoring/units.py`; validation and canary work on ratios. | `tests/test_provider_units.py` |
| 6 | **Coverage floors only go up.** Every module of `financials/providers/` has a floor in `scripts/check_coverage_distribution.py`. Raise it when coverage rises; never lower it. | `tests/test_coverage_gate.py`, `make test-cov` |
| 7 | **Secure by default.** Every route requires an API key unless listed as public in `main.py`. A route that changes something must not be a `GET`, so that read-only keys (`FONREX_READ_ONLY_API_KEYS`) cannot call it. A `POST` that only computes must be added to `_READ_ONLY_POST_PATHS` in `auth/dependencies.py`, with a test. | `tests/test_auth_defaults.py` |
| 8 | **The Docker image is self-contained.** Never exclude application code or migrations in `.dockerignore`; `.env` never goes into the image. | `tests/test_docker_image.py` |
| 9 | **The usage log never delays a response.** The middleware only queues an entry (`usage_recorder.py`); no database write on the request path. No IP address stored unless `USAGE_LOG_IP` asks for it. | `tests/test_usage_recorder.py` |
| 10 | **No real network in tests.** Provider tests use the `fake_network` fixture and the reduced real pages of `tests/fixtures/providers/`. | `tests/conftest.py` |
| 11 | **Versions are locked.** Docker, the CI and `make install-dev` install `requirements.lock` / `requirements-dev.lock` (exact versions, hash-checked), never the ranges of `requirements*.txt`. Refresh the locks with `make lock`; never edit them by hand. | `tests/test_dependency_lock.py` |
| 12 | **Prices belong to a listing.** A bar of `prices_eod` is identified by `(asset_listing_id, resolution, time)`, and `time` is the session date at midnight UTC. Resolve a ticker with `database/price_series.py`; never read or write `prices_eod` by `asset_id` alone, never store the UTC instant of an exchange-local midnight. | `tests/test_price_series.py`, `tests/test_timescale_integration.py` |
| 13 | **Source symbols are verified, never guessed.** A ticker of the catalogue is not the symbol of a data source (`SPFF` is another fund on Yahoo). Prices and fundamentals of a listing are fetched with the symbol resolved by `historical/yahoo_symbols.py` (ISIN of the instrument, currency of the listing, existing price); a listing without a verified symbol is not ingested and Yahoo is not asked about it. Never pass a ticker of the catalogue as typed to Yahoo, and never let a test reach Yahoo. | `tests/test_yahoo_symbols.py`, `tests/test_use_cases.py`, `tests/test_import_enrichment.py` |
| 14 | **Displayed numbers are read in one place.** A provider turns a text into a number with `parse_number` (a cell) or `find_number` (a sentence) of `financials/numbers.py`: sign, thousands separators, scales and currencies are handled there. No `float()` on a scraped text, no local search for digits. | `tests/test_numbers.py` |
| 15 | **A rendered figure names its source.** `/fundamental` chooses each figure in `financials/formatter.py` (Yahoo answer of the request, then stored figures, then scraped providers publishing the same quantity) and reports the choice in `Sources`. A provider figure of another nature (estimate, quarter) is never used as a fallback; the document holds ratios, not percentages. | `tests/test_financials_formatter.py` |
| 16 | **Nothing read from Redis is executed, nothing is deleted without bounds.** Cache entries are JSON: never `pickle` (or `dill`, `shelve`) in application code. A route that deletes data validates how much (`days_to_keep` of `/database/cleanup` is bounded in `database/maintenance.py`) and offers a `dry_run`. | `tests/test_cache_service.py`, `tests/test_database_cleanup.py` |
| 17 | **The CI tests on the database of an installation.** The `timescaledb` service of `.github/workflows/quality.yml` and `make test-db` use the image of the `db` service of `docker-compose.yml`; change the three together. A migration that moves or rewrites data comes with a test in `tests/test_timescale_integration.py`. | `tests/test_ci_workflow.py` |
| 18 | **Statements are read by fiscal year.** `financial_statements` holds three rows per fiscal year (income statement, balance sheet, cash flow), each filling only its own columns. A calculation reads them through `fiscal_years()` of `financials/fiscal_years.py`; never take "the latest row" or limit a query to N rows to get N years. Test a calculation on rows stored the way the enrichment stores them, not on a hand-made row carrying every column. | `tests/test_fiscal_years.py` |
| 19 | **A cache key holds every parameter that changes the answer.** A parameter left out of the key serves the answer of one request to another. When a route gains a parameter, add it to the key (or cache the complete answer and filter it, as `/fundamental/deep` does), with a test of two requests that differ by that parameter only. | `tests/test_cache_keys.py` |

## Conventions

- **Database.** New code uses the async session (`database/lifecycle.py`). Existing
  synchronous repositories are called through `concurrency.run_sync` only. Schema changes go
  through Alembic, with a single head (`make migration-check`).
- **Dependencies.** Runtime packages in `requirements.txt`, test-only packages in
  `requirements-dev.txt`. Declare every package the code imports; do not add one it does
  not import. After any change run `make lock` and commit the lock files (rule 11).
- **Language.** Code, comments, documents and commit messages in English.
- **Commits and pull requests.** Conventional Commits; the pull request title must start
  with `feat`, `fix`, `docs`, `chore`, `refactor`, `perf` or `test`, and its subject must
  not start with an uppercase letter.
- **Never commit** `.env`, `core` dumps, coverage reports, or generated caches.

## Adding a provider

1. Create `financials/providers/<name>_provider.py` extending `BaseFinancialProvider`.
   Send every request through `self._session()` or `self._get()` (rule 1).
2. Register it in `PROVIDER_SPECS` in `main.py` and add its mapping in `import_assets.py`.
3. Declare the fields it returns as percentages in `monitoring/units.py` (rule 5).
4. Add a reduced real page and its expected values to `tests/fixtures/providers/`, and
   network scenarios (search, page, refusal, server error) to `tests/test_provider_network.py`.
5. Add its coverage floor to `scripts/check_coverage_distribution.py` (rule 6).
6. Update the provider count in `README.md` (rule 3).

## When a guard test fails

A guard fails because the change broke a rule above. Fix the change. Lowering a floor,
deleting an assertion, adding an exclusion or skipping a test to get a green run is not a
fix; if the rule itself must change, say so explicitly in the pull request.
