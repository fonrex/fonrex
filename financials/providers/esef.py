"""EsefProvider — the annual reports of European listed companies (ESEF), and the LEI of an ISIN.

Since the fiscal years starting in 2020, a company listed on a regulated market of
the European Union files its annual report in the European Single Electronic
Format (ESEF): an XHTML document whose figures are tagged in XBRL with the IFRS
taxonomy. Two official sources, free and without key, are read:

* **GLEIF** (``https://api.gleif.org``): the Legal Entity Identifier (LEI) of an
  ISIN. The filings are kept by LEI, not by ISIN nor ticker.
* **filings.xbrl.org**, run by XBRL International: the ESEF filings it collected,
  with each one converted to xBRL-JSON. Its data may be used without restriction;
  the index is not complete (no German filing is found) and the API may change.

The xBRL-JSON of a filing is a list of facts: a concept (``ifrs-full:Revenue``,
or an extension of the company, ``tot:...``), an entity, a period, a unit and a
value. The details that matter (see ``tests/fixtures/esef/README.md``):

* the end of a period is **exclusive**: ``2025-01-01T00:00:00/2026-01-01T00:00:00``
  is the year 2025, the instant ``2026-01-01T00:00:00`` a balance at 2025-12-31;
* a fact with a dimension other than ``concept``, ``entity``, ``period``,
  ``unit`` and ``language`` is a breakdown (segment, component of equity): only the
  others are totals, the breakdowns are counted and left out;
* the period of a filing given by the index (``period_end``) can be wrong: the
  periods are read from the facts;
* the file of a large company weighs up to 32 MB, almost all of it text of the
  notes: only the facts with a unit are numbers.

A file is read whole or not at all: an unknown format, a fact of another entity,
a period that is not a date, a value that is not a number or an unknown prefix
raise ``EsefError``. Two facts of the same concept, unit and period with
different values are left out both, and named in ``EsefReport.conflicts``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from concurrency import run_sync
from financials.providers.base import BaseFinancialProvider

logger = logging.getLogger(__name__)

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
FILINGS_HOST = "filings.xbrl.org"
FILINGS_URL = f"https://{FILINGS_HOST}"
SOURCE = "ESEF annual reports, filings.xbrl.org (XBRL International); LEI from GLEIF"
# Both sources are official: the requests say who sends them.
USER_AGENT = "Fonrex (+https://github.com/fonrex/fonrex)"

XBRL_JSON = "https://xbrl.org/2021/xbrl-json"
LEI_SCHEME = "http://standards.iso.org/iso/17442"
ISO4217 = "http://www.xbrl.org/2003/iso4217"
XBRLI = "http://www.xbrl.org/2003/instance"

PAGE_SIZE = 100
# A company files one report a year, some four: 20 pages are far more than any has.
MAX_PAGES = 20

_LEI = re.compile(r"[0-9A-Z]{18}[0-9]{2}")
_ISIN = re.compile(r"[A-Z]{2}[0-9A-Z]{9}[0-9]")
_CURRENCY = re.compile(r"[A-Z]{3}")
_CORE_DIMENSIONS = frozenset({"concept", "entity", "period", "unit", "language"})


class EsefError(ValueError):
    """An answer of GLEIF or filings.xbrl.org could not be read: nothing of it is kept."""


# ── GLEIF ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeiRecord:
    """An entity of GLEIF: its LEI, its names and the country of its legal address."""

    lei: str
    legal_name: str
    other_names: tuple[str, ...]
    country: str | None
    entity_status: str | None  # ACTIVE | INACTIVE
    registration_status: str | None  # ISSUED, LAPSED, RETIRED, ...


def parse_lei_records(payload: Any) -> list[LeiRecord]:
    """The records of an answer of ``/lei-records`` (none for an unknown ISIN)."""
    data = _data(payload, "GLEIF")
    records = []
    for item in data:
        lei = item.get("id") if isinstance(item, dict) else None
        if not isinstance(lei, str) or not _LEI.fullmatch(lei):
            raise EsefError(f"GLEIF: {lei!r} is not a LEI")
        entity = (item.get("attributes") or {}).get("entity") or {}
        legal_name = _text((entity.get("legalName") or {}).get("name"))
        if legal_name is None:
            raise EsefError(f"GLEIF: the entity {lei} has no legal name")
        other_names = tuple(
            name
            for name in (_text(other.get("name")) for other in entity.get("otherNames") or [])
            if name is not None
        )
        registration = (item.get("attributes") or {}).get("registration") or {}
        records.append(
            LeiRecord(
                lei=lei,
                legal_name=legal_name,
                other_names=other_names,
                country=_text((entity.get("legalAddress") or {}).get("country")),
                entity_status=_text(entity.get("status")),
                registration_status=_text(registration.get("status")),
            )
        )
    return records


# ── filings.xbrl.org: the index ──────────────────────────────────────────────


@dataclass(frozen=True)
class Filing:
    """A filing of the index of filings.xbrl.org.

    ``listed_period_end`` is the period given by the index: it can be wrong (the
    filing of Pernod Ricard listed at 2021-12-31 holds the year ended 2021-06-30);
    the periods of the figures are those of the facts. ``json_url`` is ``None``
    when filings.xbrl.org could not convert the filing.
    """

    fxo_id: str  # "529900S21EQ1BO4ESM68-2025-12-31-ESEF-FR-0"
    lei: str
    listed_period_end: date | None
    country: str | None
    json_url: str | None
    viewer_url: str | None
    report_url: str | None
    package_url: str | None
    error_count: int | None
    warning_count: int | None
    inconsistency_count: int | None
    added: datetime | None
    processed: datetime | None
    sha256: str | None


@dataclass(frozen=True)
class FilingsPage:
    filings: tuple[Filing, ...]
    next_url: str | None


def parse_filings(payload: Any, lei: str) -> FilingsPage:
    """A page of ``/api/entities/{lei}/filings``: the filings of ``lei`` only."""
    data = _data(payload, "filings.xbrl.org")
    filings = []
    for item in data:
        attributes = (item.get("attributes") if isinstance(item, dict) else None) or {}
        fxo_id = _text(attributes.get("fxo_id"))
        if fxo_id is None:
            raise EsefError("filings.xbrl.org: a filing has no fxo_id")
        if not fxo_id.startswith(f"{lei}-"):
            raise EsefError(f"filings.xbrl.org: {fxo_id} is not a filing of {lei}")
        filings.append(
            Filing(
                fxo_id=fxo_id,
                lei=lei,
                listed_period_end=_day(attributes.get("period_end"), fxo_id),
                country=_text(attributes.get("country")),
                json_url=_filings_url(attributes.get("json_url"), fxo_id),
                viewer_url=_filings_url(attributes.get("viewer_url"), fxo_id),
                report_url=_filings_url(attributes.get("report_url"), fxo_id),
                package_url=_filings_url(attributes.get("package_url"), fxo_id),
                error_count=_count(attributes.get("error_count"), fxo_id),
                warning_count=_count(attributes.get("warning_count"), fxo_id),
                inconsistency_count=_count(attributes.get("inconsistency_count"), fxo_id),
                added=_moment(attributes.get("date_added"), fxo_id),
                processed=_moment(attributes.get("processed"), fxo_id),
                sha256=_text(attributes.get("sha256")),
            )
        )
    links = payload.get("links") or {}
    next_url = links.get("next") if isinstance(links, dict) else None
    return FilingsPage(tuple(filings), _filings_url(next_url, "the next page"))


# ── filings.xbrl.org: the xBRL-JSON of a filing ──────────────────────────────


@dataclass(frozen=True)
class EsefFact:
    """A number of a filing: a total, without breakdown.

    ``concept`` keeps the prefix of the filing (``ifrs-full:Revenue``,
    ``tot:RevenueFromContractsWithCustomersExcludingExciseTax``); ``namespace`` is
    the one of that prefix. A period is its last day and its length in days: 0 for
    a balance at a date, about 365 for a year, about 181 for a half-year.
    """

    concept: str
    namespace: str
    unit: str  # "EUR", "EUR/shares", "shares", "pure"
    period_start: date | None
    period_end: date
    duration_days: int
    value: Decimal
    decimals: int | None

    @property
    def prefix(self) -> str:
        return self.concept.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.concept.split(":", 1)[1]

    @property
    def key(self) -> tuple:
        return (self.concept, self.unit, self.period_end, self.duration_days)


@dataclass(frozen=True)
class EsefReport:
    """The numbers of a filing: the totals, the breakdowns left out, the conflicts dropped."""

    fxo_id: str
    lei: str
    facts: tuple[EsefFact, ...]
    breakdowns: int
    conflicts: tuple[str, ...]
    taxonomy: tuple[str, ...]  # the schemas of the filing

    @property
    def period_ends(self) -> tuple[date, ...]:
        """The last days of the periods of the facts, latest first."""
        return tuple(sorted({fact.period_end for fact in self.facts}, reverse=True))


def parse_xbrl_json(content: bytes | str, lei: str, fxo_id: str) -> EsefReport:
    """Read the xBRL-JSON of the filing ``fxo_id`` of ``lei``: its numbers, without breakdown."""
    try:
        document = json.loads(content)
    except (ValueError, UnicodeDecodeError) as error:
        raise EsefError(f"{fxo_id}: not JSON ({error})") from error
    if not isinstance(document, dict):
        raise EsefError(f"{fxo_id}: not an xBRL-JSON document")
    info = document.get("documentInfo")
    if not isinstance(info, dict) or info.get("documentType") != XBRL_JSON:
        found = info.get("documentType") if isinstance(info, dict) else None
        raise EsefError(f"{fxo_id}: not an xBRL-JSON document ({found!r})")
    namespaces = info.get("namespaces")
    facts = document.get("facts")
    if not isinstance(namespaces, dict) or not isinstance(facts, dict) or not facts:
        raise EsefError(f"{fxo_id}: no namespace or no fact")

    kept: dict[tuple, EsefFact] = {}
    conflicts: set[tuple] = set()
    breakdowns = 0
    for fact_id, raw in facts.items():
        where = f"{fxo_id}, fact {fact_id}"
        dimensions = raw.get("dimensions") if isinstance(raw, dict) else None
        if not isinstance(dimensions, dict):
            raise EsefError(f"{where}: no dimensions")
        _check_entity(dimensions.get("entity"), namespaces, lei, where)
        if not dimensions.get("unit"):
            continue  # a text: the notes, the names, the dates
        if set(dimensions) - _CORE_DIMENSIONS:
            breakdowns += 1
            continue
        if raw.get("value") is None:
            continue  # nil: the company says it has no value
        fact = _fact(raw, dimensions, namespaces, where)
        known = kept.get(fact.key)
        if known is None:
            kept[fact.key] = fact
        elif known.value != fact.value:
            conflicts.add(fact.key)
        elif _precision(fact) > _precision(known):
            kept[fact.key] = fact
    for key in conflicts:
        del kept[key]
    taxonomy = info.get("taxonomy") or []
    return EsefReport(
        fxo_id=fxo_id,
        lei=lei,
        facts=tuple(sorted(kept.values(), key=lambda f: (f.concept, f.unit, f.period_end))),
        breakdowns=breakdowns,
        conflicts=tuple(
            f"{concept} {unit} {end} ({days} days)"
            for concept, unit, end, days in sorted(conflicts, key=str)
        ),
        taxonomy=tuple(str(schema) for schema in taxonomy if isinstance(schema, str)),
    )


def _check_entity(entity: Any, namespaces: dict, lei: str, where: str) -> None:
    prefix, _, identifier = str(entity or "").partition(":")
    if namespaces.get(prefix) != LEI_SCHEME or identifier != lei:
        raise EsefError(f"{where}: a fact of {entity!r}, not of {lei}")


def _fact(raw: dict, dimensions: dict, namespaces: dict, where: str) -> EsefFact:
    concept = str(dimensions.get("concept") or "")
    prefix, _, name = concept.partition(":")
    if not name or prefix not in namespaces:
        raise EsefError(f"{where}: {concept!r} is not a concept of a known prefix")
    start, end, days = _period(dimensions.get("period"), where)
    value = _value(raw.get("value"), where)
    decimals = raw.get("decimals")
    return EsefFact(
        concept=concept,
        namespace=namespaces[prefix],
        unit=_unit(str(dimensions["unit"]), namespaces, where),
        period_start=start,
        period_end=end,
        duration_days=days,
        value=value,
        decimals=decimals if isinstance(decimals, int) and not isinstance(decimals, bool) else None,
    )


def _period(text: Any, where: str) -> tuple[date | None, date, int]:
    """``start/end`` or an instant, the end exclusive: the last day it covers."""
    if not isinstance(text, str):
        raise EsefError(f"{where}: no period")
    if "/" in text:
        first, _, after = text.partition("/")
        start, stop = _midnight(first, where), _midnight(after, where)
        days = (stop - start).days
        if days <= 0:
            raise EsefError(f"{where}: the period {text} ends before it starts")
        return start, stop - timedelta(days=1), days
    return None, _midnight(text, where) - timedelta(days=1), 0


def _midnight(text: str, where: str) -> date:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as error:
        raise EsefError(f"{where}: {text!r} is not a date") from error
    if (moment.hour, moment.minute, moment.second, moment.microsecond) != (0, 0, 0, 0):
        raise EsefError(f"{where}: {text!r} is not the start of a day")
    return moment.date()


def _value(raw: Any, where: str) -> Decimal:
    # xBRL-JSON writes canonical numbers ("73420000000.0"): they are read exactly.
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise EsefError(f"{where}: {raw!r} is not a number")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as error:
        raise EsefError(f"{where}: {raw!r} is not a number") from error
    if not value.is_finite():
        raise EsefError(f"{where}: {raw!r} is not a number")
    return value


def _unit(text: str, namespaces: dict, where: str) -> str:
    """``iso4217:EUR`` → ``EUR``, ``iso4217:EUR/xbrli:shares`` → ``EUR/shares``."""
    parts = []
    for measure in text.split("/"):
        prefix, _, local = measure.partition(":")
        namespace = namespaces.get(prefix)
        if not local or namespace is None:
            raise EsefError(f"{where}: {text!r} is not a unit of a known prefix")
        if namespace == ISO4217:
            if not _CURRENCY.fullmatch(local):
                raise EsefError(f"{where}: {local!r} is not a currency")
            parts.append(local)
        elif namespace == XBRLI:
            parts.append(local)  # shares, pure
        else:
            parts.append(measure)  # a unit of the company: kept as written
    return "/".join(parts)


def _precision(fact: EsefFact) -> int:
    return 10**6 if fact.decimals is None else fact.decimals


# ── Small readers ────────────────────────────────────────────────────────────


def _data(payload: Any, source: str) -> list:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise EsefError(f"{source}: not a JSON:API answer")
    return payload["data"]


def _text(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    return text or None


def _day(raw: Any, where: str) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as error:
        raise EsefError(f"{where}: {raw!r} is not a date") from error


def _moment(raw: Any, where: str) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError as error:
        raise EsefError(f"{where}: {raw!r} is not a date and time") from error
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _count(raw: Any, where: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise EsefError(f"{where}: {raw!r} is not a count")
    return raw


def _filings_url(raw: Any, where: str) -> str | None:
    """A link of the index, made absolute; only filings.xbrl.org is ever asked."""
    if raw in (None, ""):
        return None
    text = str(raw)
    if text.startswith("/"):
        return FILINGS_URL + text
    parts = urlsplit(text)
    if parts.scheme != "https" or parts.hostname != FILINGS_HOST:
        raise EsefError(f"{where}: {text!r} is not a link of {FILINGS_HOST}")
    return text


# ── Provider ─────────────────────────────────────────────────────────────────


class EsefProvider(BaseFinancialProvider):
    """The LEI of an ISIN (GLEIF), the ESEF filings of a LEI and their numbers."""

    name = "ESEF"
    # The xBRL-JSON of a large company weighs up to 32 MB.
    timeout = 120.0
    # filings.xbrl.org states no limit but asks to be used with care.
    _semaphore = asyncio.Semaphore(2)

    def _headers(self, accept: str) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": accept}

    async def lei_records(self, isin: str) -> list[LeiRecord] | None:
        """The GLEIF entities of an ISIN (``None`` when GLEIF did not answer).

        Most ISINs of a share give one record, the issuer; an unknown ISIN gives none.
        """
        if not _ISIN.fullmatch(isin or ""):
            raise EsefError(f"{isin!r} is not an ISIN")
        payload = await self._get_json(
            GLEIF_URL,
            headers=self._headers("application/vnd.api+json"),
            params={"filter[isin]": isin},
        )
        return None if payload is None else parse_lei_records(payload)

    async def filings(self, lei: str) -> list[Filing] | None:
        """Every filing of ``lei`` in the index (``None`` when it could not be read whole)."""
        if not _LEI.fullmatch(lei or ""):
            raise EsefError(f"{lei!r} is not a LEI")
        url: str | None = f"{FILINGS_URL}/api/entities/{lei}/filings"
        params: dict | None = {"page[size]": PAGE_SIZE}
        filings: list[Filing] = []
        pages = 0
        while url is not None:
            if pages == MAX_PAGES:
                raise EsefError(
                    f"filings.xbrl.org: {lei} has more than {MAX_PAGES} pages of filings"
                )
            payload = await self._get_json(
                url, headers=self._headers("application/vnd.api+json"), params=params
            )
            if payload is None:
                return None
            page = parse_filings(payload, lei)
            pages += 1
            filings.extend(page.filings)
            url, params = page.next_url, None  # the next link carries its parameters
        return filings

    async def report(self, filing: Filing) -> EsefReport | None:
        """The numbers of a filing (``None`` when it has no xBRL-JSON or was not given)."""
        if filing.json_url is None:
            logger.info("[%s] %s has no xBRL-JSON", self.name, filing.fxo_id)
            return None
        try:
            async with self._session() as client:
                response = await client.get(
                    filing.json_url, headers=lambda: self._headers("application/json")
                )
        except httpx.HTTPError as error:
            logger.warning("[%s] GET %s failed: %s", self.name, filing.json_url, error)
            return None
        if response.status_code != 200:
            logger.warning("[%s] GET %s → %d", self.name, filing.json_url, response.status_code)
            return None
        # Reading 30 MB of JSON takes a second: not on the event loop.
        return await run_sync(parse_xbrl_json, response.content, filing.lei, filing.fxo_id)
