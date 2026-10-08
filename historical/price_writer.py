"""Write end-of-day bars: one row per listing, resolution and trading session."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from models import PriceEOD

# Identity of a bar, and conflict target of the upsert.
PRICE_KEY = ("asset_listing_id", "resolution", "time")


def write_price_bars(
    session: Session,
    bars: list[dict[str, Any]],
    batch_size: int = 1000,
    replace: bool = False,
) -> int:
    """Upsert normalized bars; return how many were written. The caller commits.

    A bar already stored for the same listing, resolution and session is updated
    in place. With ``replace``, the stored bars of each listing and resolution
    that fall inside the range of ``bars`` are deleted first: the range ends up
    holding exactly the new bars (nothing left from an earlier fetch).
    """
    table = PriceEOD.__table__
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert

    if replace:
        for listing_id, resolution in {(bar["asset_listing_id"], bar["resolution"]) for bar in bars}:
            times = [
                bar["time"]
                for bar in bars
                if bar["asset_listing_id"] == listing_id and bar["resolution"] == resolution
            ]
            session.execute(
                delete(table).where(
                    table.c.asset_listing_id == listing_id,
                    table.c.resolution == resolution,
                    table.c.time >= min(times),
                    table.c.time <= max(times),
                )
            )

    written = 0
    # By chunks: bounded memory, and under the parameter limit of PostgreSQL.
    for start in range(0, len(bars), batch_size):
        chunk = bars[start : start + batch_size]
        statement = insert(table)
        updated = {
            column.name: statement.excluded[column.name]
            for column in table.columns
            if column.name not in PRICE_KEY
        }
        session.execute(
            statement.on_conflict_do_update(index_elements=list(PRICE_KEY), set_=updated), chunk
        )
        written += len(chunk)
    return written
