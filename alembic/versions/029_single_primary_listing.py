"""Migration 029 — at most one primary listing per instrument

Revision ID: 029
Revises: 028
Create Date: 2026-10-29

The CSV importer marked a listing primary whenever the other listings of its ISIN
had been stored by an earlier batch or an earlier import: an instrument could end
up with several primary listings, and ``/eod`` then chose among them by currency.
The importer now sees the stored listings.

This revision keeps one primary listing per instrument and unmarks the others:
an active one before an inactive one, then the oldest (lowest ``id``), the order
of ``preferred_listing_of_asset``. An instrument never ends up with an inactive
primary while an active one existed. It then creates the
partial unique index ``uq_asset_listings_one_primary`` on ``(asset_id)
WHERE is_primary``, so that a second primary listing can no longer be stored.

Running the repair twice changes nothing. The downgrade drops the index and
leaves the data alone: the listings that were unmarked are not recorded.
"""

from alembic import op

revision = "029"
down_revision = "028"


def upgrade() -> None:
    op.execute(
        """
        UPDATE asset_listings AS l
        SET is_primary = false
        WHERE l.is_primary
          AND EXISTS (
              SELECT 1 FROM asset_listings AS older
              WHERE older.asset_id = l.asset_id
                AND older.is_primary
                AND older.id <> l.id
                AND (older.is_active > l.is_active
                     OR (older.is_active = l.is_active AND older.id < l.id))
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_listings_one_primary
        ON asset_listings (asset_id) WHERE is_primary
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_asset_listings_one_primary")
