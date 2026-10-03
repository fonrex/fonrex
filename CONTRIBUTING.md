# Contributing to Fonrex

Contributions are welcome — bug reports, provider fixes, new providers, documentation.

1. Read [AGENTS.md](AGENTS.md): it lists the rules of the code base and the tests that
   enforce them. It applies to human contributors and to AI coding agents alike.
2. Set up the development environment (Python 3.12):

   ```bash
   python -m venv venv && source venv/bin/activate
   make install-dev          # the exact versions of requirements-dev.lock
   ```

3. Make your change with its tests, then run the same gate as the CI:

   ```bash
   make ci
   ```

   A change to a migration, to `prices_eod` or to SQL written for PostgreSQL is also run
   on a real TimescaleDB, as the CI does (needs Docker):

   ```bash
   make test-db
   ```

4. Open a pull request. Its title follows Conventional Commits (`feat`, `fix`, `docs`,
   `chore`, `refactor`, `perf` or `test`), e.g. `fix(providers): read the dividend yield of Boursorama`.

To add a data provider, follow "Adding a provider" in [AGENTS.md](AGENTS.md).

By contributing you agree that your work is released under the project's
[AGPL-3.0 license](LICENSE).
