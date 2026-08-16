"""Shared SQLAlchemy async resources for the application lifecycle."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

# Recognised synchronous PostgreSQL driver schemes that can be safely
# replaced by ``postgresql+asyncpg://``.
_SYNC_PG_SCHEME_RE = re.compile(r"^postgresql(?:\+(?:psycopg2|psycopg2cffi|pg8000|pygresql))?://")


def get_async_database_url() -> Optional[str]:
    """Return the configured async URL, or ``None`` when DB is disabled.

    Resolution order:
    1. ``ASYNC_DATABASE_URL`` environment variable (used verbatim).
    2. ``DATABASE_URL`` environment variable, with the scheme automatically
       converted to ``postgresql+asyncpg://``.

    If ``DATABASE_URL`` uses an unrecognised scheme (e.g. ``mysql://``), a
    clear error is logged and ``None`` is returned instead of silently
    passing an invalid URL to asyncpg.
    """
    explicit = os.environ.get("ASYNC_DATABASE_URL")
    if explicit:
        return explicit

    sync_url = os.environ.get("DATABASE_URL")
    if not sync_url:
        return None

    if _SYNC_PG_SCHEME_RE.match(sync_url):
        async_url = _SYNC_PG_SCHEME_RE.sub("postgresql+asyncpg://", sync_url)
        logger.info(
            "ASYNC_DATABASE_URL non définie — déduite de DATABASE_URL "
            "(%s… → postgresql+asyncpg://…)",
            sync_url.split("://")[0],
        )
        return async_url

    # Unrecognised scheme — refuse to guess.
    logger.error(
        "❌ Impossible de déduire ASYNC_DATABASE_URL : le schéma de "
        "DATABASE_URL (%s) n'est pas un driver PostgreSQL synchrone reconnu. "
        "Définissez ASYNC_DATABASE_URL explicitement.",
        sync_url.split("://")[0],
    )
    return None


@dataclass
class AsyncDatabaseResources:
    """Own the single async engine and session factory of one app process."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def create(cls, database_url: Optional[str] = None) -> Optional[AsyncDatabaseResources]:
        url = database_url or get_async_database_url()
        if not url:
            return None
        engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        return cls(
            engine=engine,
            session_factory=async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            ),
        )

    async def close(self) -> None:
        await self.engine.dispose()
