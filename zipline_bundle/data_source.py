"""Data extraction layer for the Zipline bundle.

The bundle relies exclusively on already-ingested rows of ``prices_eod``
(daily resolution). The FonRex historical ingestion service is responsible
for filling that table upstream; the bundle only reads.

This module is Zipline-free on purpose: the tests exercise it directly
without pulling in ``zipline-reloaded`` and its heavy binary dependencies.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Iterator

import pandas as pd
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_DEFAULT_DATABASE_URL = "******localhost:5432/fonrex"

# Canonical daily resolution used everywhere in FonRex.
DAILY_RESOLUTION = "1D"

# Columns Zipline's daily bar writer expects on every asset DataFrame.
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class TickerMetadata:
    """Static identity of a ticker as required by ``AssetDBWriter.write``.

    ``sid`` is the integer Zipline uses as a compact asset identifier. It is
    assigned by the bundle in the order tickers are yielded from the data
    source (``0..n-1``) and is stable across a single ingestion run only.
    """

    sid: int
    symbol: str
    exchange: str
    asset_id: int
    asset_listing_id: int | None
    currency: str | None
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    auto_close_date: pd.Timestamp


@dataclass
class TickerBars:
    """Daily OHLCV history for a single ticker aligned on trading sessions."""

    metadata: TickerMetadata
    frame: pd.DataFrame = field(repr=False)


def _resolve_database_url(database_url: str | None) -> str:
    url = database_url or os.environ.get("DATABASE_URL") or _DEFAULT_DATABASE_URL
    # SQLAlchemy sync engine only accepts postgresql:// / postgresql+psycopg2://
    # style URLs. The async driver hint is silently rewritten so operators can
    # reuse the same ASYNC_DATABASE_URL for the bundle if convenient.
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def _to_pd_timestamp(value: date | datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


class FonRexBundleDataSource:
    """Fetch tickers and OHLCV bars from the FonRex Postgres database.

    Parameters
    ----------
    database_url:
        Explicit SQLAlchemy URL. When ``None`` the ``DATABASE_URL`` env var
        is used, falling back to the local docker-compose default.
    engine:
        Optional pre-built engine (mainly for tests). Takes precedence over
        ``database_url``. The caller keeps ownership when this is provided.
    tickers:
        Restrict extraction to these ticker symbols. Case-insensitive and
        matched against ``asset_listings.ticker`` first, then the legacy
        ``assets.ticker`` column. ``None`` means "every active listing".
    resolution:
        EOD resolution to pull. Defaults to ``"1D"``, the only one Zipline's
        daily bar writer can consume.
    """

    def __init__(
        self,
        database_url: str | None = None,
        engine: Engine | None = None,
        tickers: Iterable[str] | None = None,
        resolution: str = DAILY_RESOLUTION,
    ) -> None:
        if engine is not None:
            self._engine = engine
            self._owns_engine = False
        else:
            self._engine = create_engine(_resolve_database_url(database_url))
            self._owns_engine = True
        self._tickers = self._normalise_tickers(tickers)
        self._resolution = resolution

    # ------------------------------------------------------------------ API

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()

    def __enter__(self) -> FonRexBundleDataSource:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def iter_tickers(
        self,
        start_session: pd.Timestamp,
        end_session: pd.Timestamp,
        allowed_sessions: pd.DatetimeIndex | None = None,
    ) -> Iterator[TickerBars]:
        """Yield one :class:`TickerBars` per ticker with data in ``[start, end]``.

        ``allowed_sessions`` is the ordered list of Zipline calendar sessions
        the bundle should keep. When provided, rows falling outside those
        sessions are dropped so ``BcolzDailyBarWriter`` never receives a
        timestamp the calendar rejects.
        """
        start_ts = _to_pd_timestamp(start_session)
        end_ts = _to_pd_timestamp(end_session)
        listings = self._fetch_listings(start_ts, end_ts)
        for sid, listing in enumerate(listings):
            frame = self._fetch_bars(listing["asset_id"], start_ts, end_ts)
            if frame.empty:
                logger.info(
                    "Skipping %s (asset_id=%s): no EOD rows in [%s, %s]",
                    listing["symbol"],
                    listing["asset_id"],
                    start_ts.date(),
                    end_ts.date(),
                )
                continue
            if allowed_sessions is not None and len(allowed_sessions) > 0:
                frame = frame.reindex(frame.index.intersection(allowed_sessions))
                if frame.empty:
                    logger.info(
                        "Skipping %s (asset_id=%s): no row aligned on the trading calendar",
                        listing["symbol"],
                        listing["asset_id"],
                    )
                    continue
            metadata = self._build_metadata(sid, listing, frame)
            yield TickerBars(metadata=metadata, frame=frame)

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _normalise_tickers(tickers: Iterable[str] | None) -> tuple[str, ...] | None:
        if tickers is None:
            return None
        cleaned = tuple(sorted({t.strip().upper() for t in tickers if t and t.strip()}))
        return cleaned or None

    def _fetch_listings(
        self,
        start_session: pd.Timestamp,
        end_session: pd.Timestamp,
    ) -> list[dict]:
        """Return the ranked listing chosen per asset for the requested window.

        FonRex may expose several listings per asset (primary + dual-listed
        variants). The bundle picks the primary listing when available and
        falls back to the lowest ``asset_listings.id`` otherwise so that
        ``sid`` allocation stays deterministic across runs.
        """
        params: dict[str, object] = {
            "start_session": start_session.to_pydatetime(),
            "end_session": end_session.to_pydatetime() + pd.Timedelta(days=1),
            "resolution": self._resolution,
        }
        ticker_filter = ""
        if self._tickers is not None:
            params["tickers"] = list(self._tickers)
            ticker_filter = "AND upper(coalesce(al.ticker, a.ticker)) IN :tickers "

        query = text(
            f"""
            WITH ranked AS (
                SELECT
                    a.id                                       AS asset_id,
                    al.id                                      AS asset_listing_id,
                    upper(coalesce(al.ticker, a.ticker))       AS symbol,
                    coalesce(al.exchange, a.exchange, 'FONREX') AS exchange,
                    coalesce(al.currency, a.currency)          AS currency,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.id
                        ORDER BY
                            CASE WHEN al.is_primary THEN 0 ELSE 1 END,
                            CASE WHEN al.is_active  THEN 0 ELSE 1 END,
                            al.id
                    ) AS rank
                FROM assets a
                LEFT JOIN asset_listings al ON al.asset_id = a.id
                WHERE EXISTS (
                    SELECT 1 FROM prices_eod p
                    WHERE p.asset_id = a.id
                      AND p.resolution = :resolution
                      AND p.time >= :start_session
                      AND p.time <  :end_session
                )
                  {ticker_filter}
            )
            SELECT asset_id, asset_listing_id, symbol, exchange, currency
            FROM ranked
            WHERE rank = 1
            ORDER BY symbol
            """
        )
        if self._tickers is not None:
            query = query.bindparams(bindparam("tickers", expanding=True))

        with self._engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()

        listings = [dict(row) for row in rows]
        logger.debug("Resolved %d listing(s) for ingestion", len(listings))
        return listings

    def _fetch_bars(
        self,
        asset_id: int,
        start_session: pd.Timestamp,
        end_session: pd.Timestamp,
    ) -> pd.DataFrame:
        """Return a session-indexed DataFrame with the standard OHLCV columns.

        - ``adj_close`` overrides ``close`` when available (Zipline expects
          split/dividend-adjusted prices in the daily bundle).
        - Volumes are cast to ``float64`` because ``BcolzDailyBarWriter``
          re-casts them to ``uint32`` internally and complains on ``object``
          dtypes.
        """
        query = text(
            """
            SELECT
                time,
                open,
                high,
                low,
                close,
                adj_close,
                volume
            FROM prices_eod
            WHERE asset_id = :asset_id
              AND resolution = :resolution
              AND time >= :start_session
              AND time <  :end_session
            ORDER BY time ASC
            """
        )
        params = {
            "asset_id": asset_id,
            "resolution": self._resolution,
            "start_session": start_session.to_pydatetime(),
            "end_session": end_session.to_pydatetime() + pd.Timedelta(days=1),
        }
        with self._engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()

        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        frame = pd.DataFrame(rows)
        frame["time"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert("UTC")
        frame["time"] = frame["time"].dt.tz_localize(None).dt.normalize()
        frame = frame.set_index("time").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]

        close = frame["close"].astype(float)
        adj = frame["adj_close"].astype(float)
        # Prefer the adjusted price when the ingestion produced one, but never
        # crash the ingest if a provider omitted it on some rows.
        frame["close"] = adj.where(adj.notna(), close)

        for col in ("open", "high", "low"):
            frame[col] = frame[col].astype(float)

        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
        frame["volume"] = frame["volume"].clip(lower=0).astype(float)

        # Drop rows still missing any OHLC value: Zipline discards them anyway
        # and warns loudly. Volume of 0 is acceptable (halted sessions).
        frame = frame.dropna(subset=list(OHLCV_COLUMNS[:-1]))
        frame.index.name = "date"

        return frame[list(OHLCV_COLUMNS)]

    def _build_metadata(
        self,
        sid: int,
        listing: dict,
        frame: pd.DataFrame,
    ) -> TickerMetadata:
        first = _to_pd_timestamp(frame.index.min())
        last = _to_pd_timestamp(frame.index.max())
        # ``auto_close_date`` triggers position liquidation the day after the
        # last trade. Using ``last + 1 day`` mirrors the csvdir bundle.
        auto_close = last + pd.Timedelta(days=1)
        return TickerMetadata(
            sid=sid,
            symbol=listing["symbol"],
            exchange=listing["exchange"] or "FONREX",
            asset_id=listing["asset_id"],
            asset_listing_id=listing.get("asset_listing_id"),
            currency=listing.get("currency"),
            start_date=first,
            end_date=last,
            auto_close_date=auto_close,
        )
