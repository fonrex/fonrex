"""Migration 019 — the 1970-01-01 dates read from Yahoo become unknown

Revision ID: 019
Revises: 018
Create Date: 2026-10-10

Yahoo gives the dates of ``Ticker.info`` (``exDividendDate``,
``dateShortInterest``) as seconds since 1970. The enrichment read them as
nanoseconds: ``fundamentals_highlights.dividend_ex_date`` and
``shares_short_date`` were 1970-01-01 on every row written. The enrichment now
reads seconds; the dates already stored are set to NULL (unknown) until the next
refresh of the instrument (``GET /fundamental/deep?ticker=...&refresh=true``).

No real ex-dividend or short-interest date of a listed company is 1970-01-01.
The downgrade leaves the data alone: the wrong dates are not worth restoring.
"""

from alembic import op

revision = "019"
down_revision = "018"


def upgrade() -> None:
    for column in ("dividend_ex_date", "shares_short_date"):
        op.execute(
            f"UPDATE fundamentals_highlights SET {column} = NULL WHERE {column} = DATE '1970-01-01'"
        )


def downgrade() -> None:
    """Nothing to undo in the schema; the dates stay unknown."""
