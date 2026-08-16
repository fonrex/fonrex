"""Alembic environment configuration for Fonrex.

Supports:
- Online migrations (direct DB connection)
- Offline migrations (SQL script generation)
- Autogenerate from SQLAlchemy models
- TimescaleDB-aware autogenerate filtering
- Credentials from environment variables
"""

import logging
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ── Logging ──────────────────────────────────────────────────────────────────
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# ── Import models Base for autogenerate ──────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Base  # noqa: E402

target_metadata = Base.metadata

# ── Database URL from environment ────────────────────────────────────────────


def get_database_url() -> str:
    """Read DATABASE_URL from environment, with fallback to individual vars."""
    url = os.environ.get("DATABASE_URL")
    if url:
        # SQLAlchemy requires postgresql+psycopg2:// not postgres://
        return url.replace("postgres://", "postgresql+psycopg2://", 1)

    user = os.environ.get("DB_USER", "fonrex")
    password = os.environ.get("DB_PASSWORD", "fonrex_password")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "fonrex")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


# ── TimescaleDB hypertable exclusion ─────────────────────────────────────────
def include_object(object, name, type_, reflected, compare_to):
    """Ignore TimescaleDB internal schemas, never application hypertables."""
    schema = getattr(object, "schema", None)
    if schema in {"_timescaledb_catalog", "_timescaledb_internal", "timescaledb_information"}:
        return False
    return True


# ── Offline migrations (--sql mode) ──────────────────────────────────────────


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online migrations (live DB connection) ───────────────────────────────────


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


# ── Entry point ───────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
