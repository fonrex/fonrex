"""Keep a stored price series on one adjustment basis.

Yahoo adjusts a whole history again after each split (``close``, ``open``,
``high``, ``low``) and each dividend (``adj_close``): the prices of past sessions
change. A series is fetched in one piece, then completed bar by bar. If the new
bars were simply added after a split or a dividend, they would be adjusted on
another basis than the stored ones, and a false return would appear where they
meet — about minus the dividend yield, or -75 % after a four-for-one split.

So an ingestion that completes a series also fetches its last stored bars
(:data:`OVERLAP_SESSIONS`) and compares them with the source
(:func:`adjustment_changed`). When they differ, the whole series is fetched again
and replaces the stored one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from concurrency import run_sync
from database.price_series import session_date
from models import PriceEOD, PriceSeriesAdjustment

ADJUSTMENT_SCHEME = "close:splits;adj_close:splits+dividends"
"""How the bars written today are adjusted: ``close`` is the traded close
(adjusted for splits only), ``adj_close`` also accounts for dividends."""

# Stored bars fetched again next to the new ones, to compare them with the source.
OVERLAP_SESSIONS = 5

# Relative difference above which a stored price no longer matches the source.
# Prices are kept to eight significant digits: an unchanged price differs by far
# less, the smallest dividends by far more.
ADJUSTMENT_TOLERANCE = 1e-4


def _day(value: date | datetime) -> date:
    return session_date(value)


def _differs(stored: float | None, fetched: float | None) -> bool:
    if stored is None or fetched is None:
        return False
    if stored == fetched:
        return False
    scale = max(abs(stored), abs(fetched))
    return scale > 0 and abs(stored - fetched) / scale > ADJUSTMENT_TOLERANCE


def adjustment_changed(stored: Iterable[dict[str, Any]], fetched: Iterable[dict[str, Any]]) -> bool:
    """Whether the source now gives other prices for sessions already stored.

    ``stored`` and ``fetched`` are bars with ``time``, ``close`` and ``adj_close``.
    Sessions present on one side only are not compared.

    TradingView gives no ``adj_close``. A stored bar without one while the source
    now gives one counts as a change: the series is then fetched again in full,
    so that its dividend-adjusted prices are all known on one basis again.
    """
    fetched_by_day = {_day(bar["time"]): bar for bar in fetched}
    for bar in stored:
        fresh = fetched_by_day.get(_day(bar["time"]))
        if fresh is None:
            continue
        if _differs(bar.get("close"), fresh.get("close")):
            return True
        stored_adjusted, fetched_adjusted = bar.get("adj_close"), fresh.get("adj_close")
        if stored_adjusted is None and fetched_adjusted is not None:
            return True
        if _differs(stored_adjusted, fetched_adjusted):
            return True
    return False


@dataclass(frozen=True)
class StoredSeries:
    """First and last stored sessions, bar count and recorded scheme of a series."""

    first: Optional[date]
    last: Optional[date]
    count: int
    scheme: Optional[str]


def stored_series(session: Session, listing_id: int, resolution: str) -> StoredSeries:
    first, last, count = session.execute(
        select(func.min(PriceEOD.timestamp), func.max(PriceEOD.timestamp), func.count()).where(
            PriceEOD.asset_listing_id == listing_id, PriceEOD.resolution == resolution
        )
    ).one()
    scheme = session.execute(
        select(PriceSeriesAdjustment.scheme).where(
            PriceSeriesAdjustment.asset_listing_id == listing_id,
            PriceSeriesAdjustment.resolution == resolution,
        )
    ).scalar()
    return StoredSeries(
        first=session_date(first) if first else None,
        last=session_date(last) if last else None,
        count=count or 0,
        scheme=scheme,
    )


def edge_bars(
    session: Session, listing_id: int, resolution: str, newest: bool, limit: int
) -> list[dict[str, Any]]:
    """The ``limit`` newest (or oldest) stored bars of a series, oldest first."""
    order = PriceEOD.timestamp.desc() if newest else PriceEOD.timestamp.asc()
    rows = session.execute(
        select(PriceEOD.timestamp, PriceEOD.close, PriceEOD.adj_close)
        .where(PriceEOD.asset_listing_id == listing_id, PriceEOD.resolution == resolution)
        .order_by(order)
        .limit(limit)
    ).all()
    bars = [{"time": row[0], "close": row[1], "adj_close": row[2]} for row in rows]
    return sorted(bars, key=lambda bar: _day(bar["time"]))


@dataclass
class SeriesFetch:
    """What :func:`fetch_on_one_basis` fetched, and whether it is the whole series."""

    result: Optional[dict[str, Any]] = None
    bars: list[dict[str, Any]] | None = None
    rebase_reason: Optional[str] = None
    whole_series: bool = False
    error: Optional[str] = None

    @property
    def note(self) -> Optional[str]:
        """The note of the source, and why the whole series was fetched again."""
        notes = [
            (self.result or {}).get("note"),
            self.rebase_reason and f"Whole history fetched again: {self.rebase_reason}",
        ]
        return "; ".join(note for note in notes if note) or None


Fetch = Callable[[date, date], Awaitable[Optional[dict[str, Any]]]]


async def fetch_on_one_basis(
    fetch: Fetch,
    normalize: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    get_session: Callable[[], Session],
    listing_id: int,
    resolution: str,
    start: date,
    end: date,
    force_refresh: bool,
) -> SeriesFetch:
    """Fetch the bars of ``start``..``end`` so that the series stays on one basis.

    - Nothing stored: the window is fetched; it is the whole series.
    - Forced refresh, or a series stored before the scheme was recorded: the
      stored range and the window are fetched again in one piece.
    - Otherwise the window is fetched with the stored bars next to it
      (:data:`OVERLAP_SESSIONS`). If the source gives other prices for them, the
      stored range and the window are fetched again in one piece; if not, only
      the bars outside the stored range are kept.

    A series fetched in one piece is to replace the stored range (``whole_series``).
    """
    stored = await run_sync(_in_session, get_session, stored_series, listing_id, resolution)
    reason: Optional[str] = None
    edge: list[dict[str, Any]] = []
    forward = stored.count > 0 and start > stored.last
    if stored.count:
        if force_refresh:
            reason = "forced refresh"
        elif stored.scheme != ADJUSTMENT_SCHEME:
            reason = "stored before the adjustment scheme was recorded"
        else:
            edge = await run_sync(
                _in_session,
                get_session,
                edge_bars,
                listing_id,
                resolution,
                forward,
                OVERLAP_SESSIONS,
            )
            if edge and forward:
                start = min(start, _day(edge[0]["time"]))
            elif edge:
                end = max(end, _day(edge[-1]["time"]))
    if reason:
        start, end = min(start, stored.first), max(end, stored.last)

    result = await fetch(start, end)
    if not result or not result.get("bars"):
        return SeriesFetch(error=result.get("error") if result else "Aucune donnée récupérée")
    bars = normalize(result["bars"])

    if edge and adjustment_changed(edge, bars):
        # A split or a dividend since the last ingestion: the source has adjusted
        # the stored sessions again. The new bars alone would not match them.
        reason = "the source adjusted the stored bars again (split or dividend)"
        result = await fetch(min(start, stored.first), max(end, stored.last))
        if not result or not result.get("bars"):
            why = result.get("error") if result else None
            return SeriesFetch(
                error="The stored history no longer matches the source (split or dividend) "
                "and could not be fetched again: nothing was written" + (f" ({why})" if why else "")
            )
        bars = normalize(result["bars"])
    elif edge:
        # The stored bars still match: only the new sessions are written.
        if forward:
            bars = [bar for bar in bars if _day(bar["time"]) > stored.last]
        else:
            bars = [bar for bar in bars if _day(bar["time"]) < stored.first]

    return SeriesFetch(
        result=result,
        bars=bars,
        rebase_reason=reason,
        whole_series=reason is not None or not stored.count,
    )


async def record_scheme(
    get_session: Callable[[], Session], listing_id: int, resolution: str
) -> None:
    """Record that the whole series has just been written with :data:`ADJUSTMENT_SCHEME`."""
    await run_sync(
        _in_session, get_session, record_series_scheme, listing_id, resolution, commit=True
    )


def _in_session(get_session: Callable[[], Session], work, *args, commit: bool = False):
    """Run ``work(session, *args)`` in a session of its own."""
    session = get_session()
    try:
        result = work(session, *args)
        if commit:
            session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_series_scheme(session: Session, listing_id: int, resolution: str) -> None:
    """Record that the stored series follows :data:`ADJUSTMENT_SCHEME`. The caller commits."""
    table = PriceSeriesAdjustment.__table__
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    now = datetime.now(UTC)
    statement = insert(table).values(
        asset_listing_id=listing_id, resolution=resolution, scheme=ADJUSTMENT_SCHEME, fetched_at=now
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["asset_listing_id", "resolution"],
            set_={"scheme": ADJUSTMENT_SCHEME, "fetched_at": now},
        )
    )
