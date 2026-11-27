"""The one place where a statement row reaches the database.

Two producers fill ``financial_statements``: the Yahoo enrichment and the
stockanalysis backfill for the quarters Yahoo does not keep. They must not disagree
about what a row is, so the write itself lives here once: the key is the asset, the
statement type, the frequency and the period end. Two writes exist: ``upsert_rows``
replaces what is stored for a period (the Yahoo enrichment asked for that period), and
``insert_rows`` adds only the periods that are absent, leaving every stored row untouched.

The conversion of the figures stays with the producer that read them - each source
publishes a different scale, and a conversion done far from its page is a guess.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

if TYPE_CHECKING:
    from database.service import DatabaseService

logger = logging.getLogger(__name__)

#: Columns that identify a row. Everything else is a figure and is written.
KEY_COLUMNS = ("asset_id", "statement_type", "period_type", "period_end")


def _insert_that_ignores_a_row_that_exists(
    dialect: str, table: Table
) -> Callable[[dict[str, Any]], Any]:
    """An INSERT that leaves an existing row alone, as the database supports it.

    ``ON CONFLICT DO NOTHING`` makes the database itself the arbiter, so two enrichments
    running at once cannot both decide a period is missing and one overwrite the other.
    Without it the statement is a plain INSERT: the unique constraint refuses the duplicate
    and the caller sees the error - a weaker guarantee, never a silent overwrite.
    """
    key = [table.c[column] for column in KEY_COLUMNS]
    dialect_insert = {"postgresql": postgresql_insert, "sqlite": sqlite_insert}.get(dialect)
    if dialect_insert is None:
        return lambda values: table.insert().values(**values)
    return lambda values: (
        dialect_insert(table).values(**values).on_conflict_do_nothing(index_elements=key)
    )


def insert_rows(db_service: DatabaseService, rows: list[dict[str, Any]]) -> int:
    """Add the rows whose period is not stored yet; return how many were added.

    The caller's own list of known periods may be out of date by the time the rows are
    written - another enrichment may have added the same quarter while the page was in
    flight. The decision therefore belongs to the database, not to a value read before the
    request: a row that already exists is skipped, never updated, so a period keeps the
    source that wrote it first.
    """
    from models import FinancialStatement

    if not rows:
        return 0

    table = FinancialStatement.__table__
    session = db_service.get_session()
    added = 0
    try:
        build = _insert_that_ignores_a_row_that_exists(session.get_bind().dialect.name, table)
        for values in rows:
            result = session.execute(build(values))
            added += max(result.rowcount or 0, 0)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return added


def upsert_rows(db_service: DatabaseService, rows: list[dict[str, Any]]) -> int:
    """Write the rows, one per (asset, type, frequency, period end); return how many.

    A row whose key exists is updated: the producer asked for this period, so its
    figures are the ones that belong there. The caller decides which periods to hand
    over, and a caller that wants to fill gaps only passes the missing ones.
    """
    from models import FinancialStatement

    if not rows:
        return 0

    session = db_service.get_session()
    count = 0
    try:
        for values in rows:
            existing = (
                session.query(FinancialStatement)
                .filter_by(**{column: values[column] for column in KEY_COLUMNS})
                .first()
            )
            if existing is not None:
                for column, value in values.items():
                    if column not in KEY_COLUMNS:
                        setattr(existing, column, value)
            else:
                session.add(FinancialStatement(**values))
            count += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return count
