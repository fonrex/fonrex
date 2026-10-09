"""Migration 016 — how each stored price series is adjusted

Revision ID: 016
Revises: 015
Create Date: 2026-10-09

Yahoo adjusts a whole history again after each split and each dividend. The
ingestion used to store Yahoo's dividend-adjusted prices as ``close`` and
``adj_close``, then add new bars to them: after a dividend or a split, the new
bars and the stored ones were adjusted on two different bases, and a false
return appeared where they met (about minus the dividend yield, or -75 % after
a four-for-one split).

The ingestion now stores the traded close (adjusted for splits only) in
``close`` and the close adjusted for dividends too in ``adj_close``, checks the
last stored bars against the source before adding new ones, and fetches the
whole series again when they differ. ``price_series_adjustments`` records, per
listing and resolution, the scheme of the stored bars and when the series was
last fetched in one piece.

No row is written here: the series stored before this revision have none, and
the next ingestion of each one fetches it again in full with the new scheme
(``scripts/ingest_all.py --force`` does it for the whole catalogue at once).
Until then, their bars are read as before. The prices are not touched by this
revision, which only creates a small table: the jobs of ``prices_eod`` are left
alone.
"""

import sqlalchemy as sa

from alembic import op

revision = "016"
down_revision = "015"


def upgrade() -> None:
    op.create_table(
        "price_series_adjustments",
        sa.Column(
            "asset_listing_id",
            sa.Integer(),
            sa.ForeignKey("asset_listings.id", ondelete="CASCADE"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("resolution", sa.String(3), primary_key=True),
        sa.Column("scheme", sa.String(40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("price_series_adjustments")
