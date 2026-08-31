"""Zipline ``ingest`` callable backed by the FonRex Postgres database.

The bundle materialises three artefacts on disk that Zipline reads back at
backtest time:

- ``assets.sqlite`` with the equity metadata (symbol, exchange, date range),
- ``daily_equities.bcolz`` with the OHLCV daily bars,
- ``adjustments.sqlite`` with the (currently empty) splits/dividends.

FonRex does not yet track splits or dividends as first-class rows; the
``adj_close`` column of ``prices_eod`` already captures the cumulative
adjustment applied by upstream providers, so the daily bar writer receives
adjusted prices directly. Empty adjustment DataFrames are handed to Zipline
so the writer still initialises correctly.
"""

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

from zipline_bundle.data_source import (
    OHLCV_COLUMNS,
    FonRexBundleDataSource,
    TickerBars,
    TickerMetadata,
)

logger = logging.getLogger(__name__)


_DIVIDEND_COLUMNS = (
    "sid",
    "amount",
    "ex_date",
    "record_date",
    "declared_date",
    "pay_date",
)
_SPLIT_COLUMNS = ("sid", "ratio", "effective_date")


def _empty_adjustment_frames() -> dict[str, pd.DataFrame]:
    """Return empty splits/dividends DataFrames matching Zipline's schema."""
    return {
        "splits": pd.DataFrame(columns=list(_SPLIT_COLUMNS)),
        "dividends": pd.DataFrame(columns=list(_DIVIDEND_COLUMNS)),
    }


def _build_metadata_frame(records: Iterable[TickerMetadata]) -> pd.DataFrame:
    """Build the DataFrame consumed by ``AssetDBWriter.write(equities=...)``."""
    dtype = [
        ("start_date", "datetime64[ns]"),
        ("end_date", "datetime64[ns]"),
        ("auto_close_date", "datetime64[ns]"),
        ("symbol", "object"),
        ("exchange", "object"),
    ]
    records = list(records)
    frame = pd.DataFrame(np.empty(len(records), dtype=dtype))
    for row in records:
        frame.loc[row.sid, "start_date"] = row.start_date
        frame.loc[row.sid, "end_date"] = row.end_date
        frame.loc[row.sid, "auto_close_date"] = row.auto_close_date
        frame.loc[row.sid, "symbol"] = row.symbol
        frame.loc[row.sid, "exchange"] = row.exchange
    return frame


class FonRexBundle:
    """Callable Zipline bundle backed by ``prices_eod`` rows.

    Instances are cheap to build and safe to register once at import time.
    A fresh SQLAlchemy engine is opened on every call to :meth:`ingest`,
    because Zipline ingests are invoked from a short-lived CLI process.
    """

    def __init__(
        self,
        tickers: Iterable[str] | None = None,
        database_url: str | None = None,
        resolution: str = "1D",
    ) -> None:
        self._tickers = tuple(tickers) if tickers is not None else None
        self._database_url = database_url
        self._resolution = resolution

    def ingest(
        self,
        environ,
        asset_db_writer,
        minute_bar_writer,
        daily_bar_writer,
        adjustment_writer,
        calendar,
        start_session,
        end_session,
        cache,
        show_progress,
        output_dir,
    ) -> None:
        """Populate the Zipline writers with FonRex Postgres data.

        The signature matches Zipline's ``register`` contract byte-for-byte:
        ``core.ingest`` calls this positional-only tuple and relies on the
        writers being fully populated before returning.
        """
        del cache, output_dir, minute_bar_writer  # unused: daily-only bundle

        database_url = self._database_url or environ.get("DATABASE_URL")
        allowed_sessions = self._allowed_sessions(calendar, start_session, end_session)

        with FonRexBundleDataSource(
            database_url=database_url,
            tickers=self._tickers,
            resolution=self._resolution,
        ) as source:
            iterator = source.iter_tickers(
                start_session=start_session,
                end_session=end_session,
                allowed_sessions=allowed_sessions,
            )
            collected: list[TickerBars] = list(iterator)

        if not collected:
            logger.warning(
                "FonRex bundle produced no data for window [%s, %s]",
                pd.Timestamp(start_session).date(),
                pd.Timestamp(end_session).date(),
            )
            asset_db_writer.write(equities=_build_metadata_frame([]))
            daily_bar_writer.write(iter(()), show_progress=show_progress)
            adjustment_writer.write(**_empty_adjustment_frames())
            return

        logger.info(
            "FonRex bundle writing %d ticker(s) between %s and %s",
            len(collected),
            collected[0].frame.index.min().date(),
            max(bars.frame.index.max() for bars in collected).date(),
        )

        # Zipline expects (sid, DataFrame) tuples with an OHLCV-only frame.
        def _bar_iterator() -> Iterable[tuple[int, pd.DataFrame]]:
            for bars in collected:
                yield bars.metadata.sid, bars.frame[list(OHLCV_COLUMNS)]

        daily_bar_writer.write(_bar_iterator(), show_progress=show_progress)
        asset_db_writer.write(
            equities=_build_metadata_frame(bars.metadata for bars in collected)
        )
        adjustment_writer.write(**_empty_adjustment_frames())

    # ------------------------------------------------------------------ util

    @staticmethod
    def _allowed_sessions(calendar, start_session, end_session) -> pd.DatetimeIndex | None:
        """Return the trading sessions covered by the bundle window, if any.

        Zipline's ``TradingCalendar`` API varies across versions; the bundle
        supports both ``sessions_in_range`` (recent releases) and the older
        ``all_sessions`` attribute. When neither is available we skip the
        alignment step and let the writer handle any mismatch.
        """
        if calendar is None:
            return None
        try:
            sessions = calendar.sessions_in_range(start_session, end_session)
        except AttributeError:
            sessions = getattr(calendar, "all_sessions", None)
            if sessions is None:
                return None
            sessions = sessions[(sessions >= start_session) & (sessions <= end_session)]
        if getattr(sessions, "tz", None) is not None:
            sessions = sessions.tz_convert("UTC").tz_localize(None)
        return sessions.normalize()


def fonrex_equities(
    tickers: Iterable[str] | None = None,
    database_url: str | None = None,
    resolution: str = "1D",
):
    """Return a Zipline ``ingest`` callable configured for FonRex.

    Mirrors the signature convention of ``csvdir_equities`` from
    ``zipline.data.bundles.csvdir`` so operators can drop this in their
    ``~/.zipline/extension.py`` file:

    .. code-block:: python

        from zipline.data.bundles import register
        from zipline_bundle import fonrex_equities

        register(
            "fonrex",
            fonrex_equities(tickers=["AAPL", "MSFT"]),
            calendar_name="NYSE",
        )
    """
    return FonRexBundle(
        tickers=tickers,
        database_url=database_url,
        resolution=resolution,
    ).ingest


def register_fonrex_bundle(
    bundle_name: str = "fonrex",
    tickers: Iterable[str] | None = None,
    database_url: str | None = None,
    resolution: str = "1D",
    calendar_name: str = "NYSE",
    start_session: pd.Timestamp | None = None,
    end_session: pd.Timestamp | None = None,
) -> None:
    """Register the bundle with Zipline's global bundle registry.

    Delegates to ``zipline.data.bundles.register`` and is a no-op-friendly
    helper so users only need one import in their ``extension.py``. Raises
    :class:`ImportError` if ``zipline-reloaded`` is not installed.
    """
    try:
        from zipline.data.bundles import register
    except ImportError as exc:  # pragma: no cover - runtime hint only
        raise ImportError(
            "zipline-reloaded is required to register the FonRex bundle. "
            "Install it with `pip install zipline-reloaded`."
        ) from exc

    register(
        bundle_name,
        fonrex_equities(
            tickers=tickers,
            database_url=database_url,
            resolution=resolution,
        ),
        calendar_name=calendar_name,
        start_session=start_session,
        end_session=end_session,
    )
