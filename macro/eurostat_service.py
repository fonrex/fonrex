"""Macro-economic indicators of Eurostat, kept period by period.

Eurostat publishes the statistics of the European Union through a free API
without key. A series is read in SDMX-CSV, one row per observation:
``{EUROSTAT_API_URL}/{flow}/{key}?format=SDMX-CSV``, the key naming one value
per dimension of the flow (``prc_hicp_minr/M.RCH_A.TOTAL.EA21``). The columns
used are ``TIME_PERIOD``, ``OBS_VALUE``, ``OBS_FLAG`` (``e`` for an estimate,
such as the flash inflation of the last month) and ``LAST UPDATE``.

The indicators are monthly or quarterly, by country, and their recent periods
are revised after their first release: they are kept in ``macro_observations``,
one row per period dated by its first day (``2026-Q2`` is 1 April 2026), a
revision replacing the value of its period. A refresh asks again for the last
periods stored, to take the revisions; a series read less than
``EUROSTAT_REFRESH_HOURS`` hours ago is not asked again. Rates published in
percent are stored as ratios (``3.8`` becomes ``0.038``); an index or a survey
balance is stored as published.

The euro area is ``EA21``, its composition since Bulgaria joined in 2026: the
flows of the labour market and of the surveys know no code for a changing
composition. ``EA`` is read as ``EA21``.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import MacroObservation, MacroSeriesLoad
from schemas.macro import RATE_UNIT
from settings import env_int

logger = logging.getLogger(__name__)

EUROSTAT_API_URL = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
SOURCE = "eurostat"
# The euro area in its composition since 2026 (EA20 before Bulgaria joined).
# Change it with the next enlargement: every flow publishes the new code back in time.
EURO_AREA = "EA21"
_GEO_ALIASES = {"EA": EURO_AREA}
_GEO_PATTERN = re.compile(r"^[A-Z0-9_]{2,10}$")
# ``LAST UPDATE`` of Eurostat is given in the time of Luxembourg, without zone.
_EUROSTAT_TIME = ZoneInfo("Europe/Luxembourg")
_PRECISION = Decimal("0.00000001")  # macro_observations.value is NUMERIC(18, 8)
_HUNDRED = Decimal(100)
_CHUNK = 5000


class EurostatError(ValueError):
    """Eurostat refused the request, or its answer could not be read: nothing is stored."""


@dataclass(frozen=True)
class EurostatIndicator:
    """An indicator: the flow of Eurostat, its key for a country, its frequency and unit."""

    name: str
    label: str
    flow: str
    key: str  # with ``{geo}`` where the country goes
    frequency: str  # "monthly" | "quarterly"
    unit: str = RATE_UNIT  # "ratio": published in percent; "index", "balance": as published
    # Periods asked again before the last one stored, to take the revisions.
    revised_periods: int = 24

    def series_key(self, geo: str) -> str:
        return self.key.format(geo=normalize_geo(geo))


INDICATORS: dict[str, EurostatIndicator] = {
    indicator.name: indicator
    for indicator in (
        EurostatIndicator(
            "hicp",
            "HICP inflation, annual rate of change",
            "prc_hicp_minr",
            "M.RCH_A.TOTAL.{geo}",
            "monthly",
        ),
        EurostatIndicator(
            "hicp_core",
            "HICP inflation excluding energy, food, alcohol and tobacco, annual rate",
            "prc_hicp_minr",
            "M.RCH_A.TOT_X_NRG_FOOD.{geo}",
            "monthly",
        ),
        EurostatIndicator(
            "unemployment",
            "Unemployment rate, seasonally adjusted",
            "une_rt_m",
            "M.SA.TOTAL.PC_ACT.T.{geo}",
            "monthly",
        ),
        EurostatIndicator(
            "gdp_qoq",
            "GDP growth on the previous quarter, chain-linked volumes",
            "namq_10_gdp",
            "Q.CLV_PCH_PRE.SCA.B1GQ.{geo}",
            "quarterly",
            revised_periods=8,
        ),
        EurostatIndicator(
            "gdp_yoy",
            "GDP growth on the same quarter of the previous year, chain-linked volumes",
            "namq_10_gdp",
            "Q.CLV_PCH_SM.SCA.B1GQ.{geo}",
            "quarterly",
            revised_periods=8,
        ),
        EurostatIndicator(
            "economic_sentiment",
            "Economic sentiment indicator (long-term average = 100)",
            "ei_bssi_m_r2",
            "M.BS-ESI-I.SA.{geo}",
            "monthly",
            unit="index",
        ),
        EurostatIndicator(
            "consumer_confidence",
            "Consumer confidence indicator (balance of answers)",
            "ei_bssi_m_r2",
            "M.BS-CSMCI-BAL.SA.{geo}",
            "monthly",
            unit="balance",
        ),
    )
}


def normalize_geo(geo: str) -> str:
    """The code of a country or area for Eurostat: upper case, ``EA`` read as ``EA21``."""
    code = (geo or "").strip().upper()
    code = _GEO_ALIASES.get(code, code)
    if not _GEO_PATTERN.match(code):
        raise ValueError(f"{geo!r} is not a country or area code")
    return code


def indicator(name: str) -> EurostatIndicator:
    """The indicator of that name (``KeyError`` when it is not known)."""
    return INDICATORS[name.strip().lower()]


def eurostat_url() -> str:
    return (os.environ.get("EUROSTAT_API_URL") or EUROSTAT_API_URL).strip().rstrip("/")


def series_url(item: EurostatIndicator, geo: str, base_url: str | None = None) -> str:
    """Address of one series: an indicator for one country or area."""
    return f"{base_url or eurostat_url()}/{item.flow}/{item.series_key(geo)}"


@dataclass(frozen=True)
class Observation:
    """One value of a series, read from Eurostat or from the database."""

    period: date
    value: Decimal
    status: str | None = None


@dataclass(frozen=True)
class SeriesAnswer:
    """What an answer of Eurostat holds for one series."""

    observations: list[Observation]
    source_updated_at: datetime | None = None


def parse_period(raw: str) -> date:
    """First day of a period of Eurostat: ``2026-09``, ``2026-Q2`` or ``2026``."""
    text = raw.strip()
    match = re.fullmatch(r"(\d{4})-Q([1-4])", text)
    if match:
        return date(int(match.group(1)), 3 * int(match.group(2)) - 2, 1)
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match and 1 <= int(match.group(2)) <= 12:
        return date(int(match.group(1)), int(match.group(2)), 1)
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1)
    raise EurostatError(f"{raw!r} is not a period")


def _last_update(raw: str | None) -> datetime | None:
    """``LAST UPDATE`` of Eurostat (``02/10/26 11:00:00``, Luxembourg time), in UTC."""
    if not raw or not raw.strip():
        return None
    try:
        local = datetime.strptime(raw.strip(), "%d/%m/%y %H:%M:%S")
    except ValueError:
        return None
    return local.replace(tzinfo=_EUROSTAT_TIME).astimezone(timezone.utc)


def parse_sdmx_csv(text: str, item: EurostatIndicator) -> SeriesAnswer:
    """The observations of an SDMX-CSV answer, as stored: rates as ratios, by period."""
    if not text.strip():
        return SeriesAnswer([])
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"TIME_PERIOD", "OBS_VALUE"} <= set(reader.fieldnames):
        raise EurostatError("the answer has no TIME_PERIOD and OBS_VALUE columns")
    observations: dict[date, Observation] = {}
    updated: datetime | None = None
    for row in reader:
        raw_period = (row.get("TIME_PERIOD") or "").strip()
        raw_value = (row.get("OBS_VALUE") or "").strip()
        if not raw_period or not raw_value:
            continue  # a period without value (confidential or not yet published)
        period = parse_period(raw_period)
        try:
            value = Decimal(raw_value)
        except InvalidOperation as error:
            raise EurostatError(f"{raw_period}: {raw_value!r} is not a number") from error
        if not value.is_finite():
            continue
        if item.unit == RATE_UNIT:
            value = value / _HUNDRED
        status = (row.get("OBS_FLAG") or "").strip() or None
        observations[period] = Observation(period, value.quantize(_PRECISION), status)
        row_updated = _last_update(row.get("LAST UPDATE"))
        if row_updated is not None and (updated is None or row_updated > updated):
            updated = row_updated
    return SeriesAnswer([observations[period] for period in sorted(observations)], updated)


def _refusal(response: httpx.Response) -> str:
    """The reason Eurostat gives in its error answer, or the HTTP status."""
    match = re.search(r"<faultstring>(.*?)</faultstring>", response.text, re.S)
    if match:
        return html.unescape(match.group(1)).strip()
    return f"HTTP {response.status_code}"


def _start_period(item: EurostatIndicator, start: date) -> str:
    if item.frequency == "quarterly":
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return f"{start.year}-{start.month:02d}"


async def download_series(
    item: EurostatIndicator, geo: str, start: date | None = None, base_url: str | None = None
) -> SeriesAnswer:
    """The observations of a series from ``start`` (all of them without it).

    Raises :class:`EurostatError` when Eurostat refuses the request (an unknown
    code answers 400, an unknown flow 404), ``httpx`` errors on any other failure.
    """
    params = {"format": "SDMX-CSV"}
    if start is not None:
        params["startPeriod"] = _start_period(item, start)
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(series_url(item, geo, base_url), params=params)
    if response.status_code in (400, 404):
        raise EurostatError(_refusal(response))
    response.raise_for_status()
    return parse_sdmx_csv(response.text, item)


def _periods_before(item: EurostatIndicator, period: date) -> date:
    """The period ``item.revised_periods`` periods before ``period``."""
    months = item.revised_periods * (3 if item.frequency == "quarterly" else 1)
    index = period.year * 12 + period.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class SeriesLoad:
    """What the database holds for one series, and what the last refresh did."""

    indicator: str
    geo: str
    status: str  # fetched, fresh, failed
    fetched_at: datetime | None = None
    first_period: date | None = None
    last_period: date | None = None
    received: int = 0
    reason: str | None = None


class EurostatService:
    """Indicators of Eurostat, stored period by period and refreshed on demand."""

    source = SOURCE

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, downloader=download_series
    ):
        self.session_factory = session_factory
        self.downloader = downloader
        self.refresh_after = timedelta(
            hours=env_int("EUROSTAT_REFRESH_HOURS", 24, minimum=1, maximum=24 * 30)
        )

    def _is_recent(self, fetched_at: datetime | None) -> bool:
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at < self.refresh_after

    async def refresh(self, name: str, geo: str, force: bool = False) -> SeriesLoad:
        """Ask Eurostat for a series, unless it was read recently.

        The first refresh reads the whole series; the next ones ask again for the
        last periods stored (``revised_periods``), so that a revision replaces
        the value of its period.
        """
        item, code = indicator(name), normalize_geo(geo)
        key = (SOURCE, item.name, code)
        async with self.session_factory() as session:
            load = await session.get(MacroSeriesLoad, key)
            last_period = await session.scalar(
                select(func.max(MacroObservation.period)).where(
                    MacroObservation.source == SOURCE,
                    MacroObservation.indicator == item.name,
                    MacroObservation.geo == code,
                )
            )
        if load is not None and not force and self._is_recent(load.fetched_at):
            return SeriesLoad(
                item.name, code, "fresh", load.fetched_at, load.first_period, load.last_period
            )

        start = _periods_before(item, last_period) if last_period else None
        try:
            answer = await self.downloader(item, code, start)
        except Exception as error:  # refused, network, HTTP status, unreadable answer
            message = str(error).splitlines()[0] if str(error) else ""
            reason = f"{type(error).__name__}: {message}" if message else type(error).__name__
            logger.warning("Eurostat %s for %s not refreshed: %s", item.name, code, reason)
            return SeriesLoad(
                item.name,
                code,
                "failed",
                load.fetched_at if load else None,
                load.first_period if load else None,
                last_period,
                reason=reason,
            )

        fetched_at = datetime.now(timezone.utc)
        rows = [
            {
                "source": SOURCE,
                "indicator": item.name,
                "geo": code,
                "period": observation.period,
                "frequency": item.frequency,
                "value": observation.value,
                "unit": item.unit,
                "status": observation.status,
                "fetched_at": fetched_at,
            }
            for observation in answer.observations
        ]
        async with self.session_factory() as session:
            async with session.begin():
                for chunk in range(0, len(rows), _CHUNK):
                    statement = insert(MacroObservation).values(rows[chunk : chunk + _CHUNK])
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["source", "indicator", "geo", "period"],
                            set_={
                                "value": statement.excluded.value,
                                "status": statement.excluded.status,
                                "fetched_at": statement.excluded.fetched_at,
                            },
                        )
                    )
                first_period, last_stored = (
                    await session.execute(
                        select(
                            func.min(MacroObservation.period), func.max(MacroObservation.period)
                        ).where(
                            MacroObservation.source == SOURCE,
                            MacroObservation.indicator == item.name,
                            MacroObservation.geo == code,
                        )
                    )
                ).one()
                await session.merge(
                    MacroSeriesLoad(
                        source=SOURCE,
                        indicator=item.name,
                        geo=code,
                        fetched_at=fetched_at,
                        first_period=first_period,
                        last_period=last_stored,
                        source_updated_at=answer.source_updated_at,
                    )
                )
        return SeriesLoad(
            item.name,
            code,
            "fetched",
            fetched_at,
            first_period,
            last_stored,
            received=len(rows),
        )

    async def observations(
        self, name: str, geo: str, start: date | None = None, end: date | None = None
    ) -> list[Observation]:
        """Stored values of a series, oldest first."""
        item, code = indicator(name), normalize_geo(geo)
        statement = select(
            MacroObservation.period, MacroObservation.value, MacroObservation.status
        ).where(
            MacroObservation.source == SOURCE,
            MacroObservation.indicator == item.name,
            MacroObservation.geo == code,
        )
        if start is not None:
            statement = statement.where(MacroObservation.period >= start)
        if end is not None:
            statement = statement.where(MacroObservation.period <= end)
        async with self.session_factory() as session:
            rows = (await session.execute(statement.order_by(MacroObservation.period))).all()
        return [Observation(period, value, status) for period, value, status in rows]
