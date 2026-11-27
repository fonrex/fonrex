"""The one place where a statement row reaches the database.

Two producers fill ``financial_statements``: the Yahoo enrichment and the
stockanalysis backfill for the quarters Yahoo does not keep. They must not disagree
about what a row is, so the write itself lives here once: the key is the asset, the
statement type, the frequency and the period end, and a row that already exists is
updated in place rather than added twice.

The conversion of the figures stays with the producer that read them - each source
publishes a different scale, and a conversion done far from its page is a guess.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from database.service import DatabaseService

logger = logging.getLogger(__name__)

#: Columns that identify a row. Everything else is a figure and is written.
KEY_COLUMNS = ("asset_id", "statement_type", "period_type", "period_end")


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
