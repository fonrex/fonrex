# -*- coding: utf-8 -*-
"""Unit tests for database.lifecycle.get_async_database_url."""

import os
from unittest.mock import patch

from database.lifecycle import get_async_database_url


class TestGetAsyncDatabaseUrl:
    """Verify explicit, deduced, and rejected URL resolution."""

    # ---- ASYNC_DATABASE_URL explicitly set --------------------------------

    def test_explicit_async_url_returned_verbatim(self):
        env = {
            "ASYNC_DATABASE_URL": "postgresql+asyncpg://u:p@host/db",
            "DATABASE_URL": "postgresql+psycopg2://u:p@host/db",
        }
        with patch.dict(os.environ, env, clear=False):
            assert get_async_database_url() == "postgresql+asyncpg://u:p@host/db"

    # ---- Deduction from DATABASE_URL --------------------------------------

    def test_bare_postgresql_scheme(self):
        """postgresql:// → postgresql+asyncpg:// (most common Docker case)."""
        env = {"DATABASE_URL": "postgresql://fonrex:pw@db:5432/fonrex"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            result = get_async_database_url()
            assert result == "postgresql+asyncpg://fonrex:pw@db:5432/fonrex"

    def test_psycopg2_scheme(self):
        """postgresql+psycopg2:// → postgresql+asyncpg:// (the reported bug)."""
        env = {"DATABASE_URL": "postgresql+psycopg2://fonrex:pw@localhost:5432/fonrex"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            result = get_async_database_url()
            assert result == "postgresql+asyncpg://fonrex:pw@localhost:5432/fonrex"

    def test_psycopg2cffi_scheme(self):
        env = {"DATABASE_URL": "postgresql+psycopg2cffi://u:p@h/d"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            assert get_async_database_url() == "postgresql+asyncpg://u:p@h/d"

    def test_pg8000_scheme(self):
        env = {"DATABASE_URL": "postgresql+pg8000://u:p@h/d"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            assert get_async_database_url() == "postgresql+asyncpg://u:p@h/d"

    # ---- Already async (passthrough via ASYNC_DATABASE_URL) ---------------

    def test_asyncpg_scheme_passthrough(self):
        """If ASYNC_DATABASE_URL is set to asyncpg, it's returned as-is."""
        env = {"ASYNC_DATABASE_URL": "postgresql+asyncpg://u:p@h/d"}
        with patch.dict(os.environ, env, clear=False):
            assert get_async_database_url() == "postgresql+asyncpg://u:p@h/d"

    # ---- No URL configured -----------------------------------------------

    def test_no_url_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            assert get_async_database_url() is None

    # ---- Unrecognised scheme → explicit error -----------------------------

    def test_unrecognised_scheme_returns_none(self, caplog):
        """mysql:// or other non-PG schemes must not silently pass through."""
        env = {"DATABASE_URL": "mysql://u:p@h/d"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            result = get_async_database_url()
            assert result is None
            assert "Impossible de déduire ASYNC_DATABASE_URL" in caplog.text

    def test_sqlite_scheme_returns_none(self, caplog):
        env = {"DATABASE_URL": "sqlite:///test.db"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            result = get_async_database_url()
            assert result is None
            assert "Impossible de déduire ASYNC_DATABASE_URL" in caplog.text

    # ---- Edge cases -------------------------------------------------------

    def test_explicit_takes_priority_over_database_url(self):
        """ASYNC_DATABASE_URL wins even if DATABASE_URL is also set."""
        env = {
            "ASYNC_DATABASE_URL": "postgresql+asyncpg://explicit@h/d",
            "DATABASE_URL": "postgresql://sync@h/d",
        }
        with patch.dict(os.environ, env, clear=False):
            assert get_async_database_url() == "postgresql+asyncpg://explicit@h/d"

    def test_only_first_scheme_occurrence_replaced(self):
        """Ensure only the scheme prefix is replaced, not occurrences in path."""
        env = {"DATABASE_URL": "postgresql://u:p@h/postgresql"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ASYNC_DATABASE_URL", None)
            result = get_async_database_url()
            assert result == "postgresql+asyncpg://u:p@h/postgresql"
