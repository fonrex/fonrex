"""Migration 015 — stored dividend yields become ratios

Revision ID: 015
Revises: 014
Create Date: 2026-10-04

``fundamentals_highlights.dividend_yield`` is read as a ratio by everything that
uses it (valuation, validation ranges, rendered document): 0.0032 for 0.32 %.
The enrichment stored the figure as Yahoo publishes it. Yahoo published a ratio,
then a percentage (0.32): the rows written since then are a hundred times too
large, and the dividend rebuilt from them by the valuation as well.

The enrichment now stores a ratio. This revision corrects the rows already
stored as percentages, without touching those that are ratios:

* when the row holds the dividend per share, the P/E and the earnings per share,
  the price is ``pe_ratio * eps_trailing`` and the yield is known: a stored value
  more than ten times larger is a percentage;
* otherwise a value above 0.25 is a percentage (a yield above 25 % is not).

A percentage below 0.25 % on a row without those three figures cannot be told
from a ratio: it stays as it is until the next refresh of the instrument
(``GET /fundamental/deep?ticker=...&refresh=true``).

Running the correction twice changes nothing: a corrected row no longer matches.
The downgrade leaves the data alone, the rows that were changed are not recorded.
"""

from alembic import op

revision = "015"
down_revision = "014"


def upgrade() -> None:
    op.execute(
        """
        UPDATE fundamentals_highlights
        SET dividend_yield = dividend_yield / 100.0
        WHERE dividend_yield IS NOT NULL
          AND CASE
                WHEN dividend_rate > 0 AND pe_ratio > 0 AND eps_trailing > 0
                  THEN dividend_yield > 10.0 * dividend_rate / (pe_ratio * eps_trailing)
                ELSE dividend_yield > 0.25
              END
        """
    )


def downgrade() -> None:
    """Nothing to undo in the schema; the corrected values stay ratios."""
