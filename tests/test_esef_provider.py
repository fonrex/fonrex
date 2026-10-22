"""The ESEF filings and the LEI of an ISIN (``financials/providers/esef.py``).

The files of ``tests/fixtures/esef/`` are real answers of GLEIF and filings.xbrl.org,
the xBRL-JSON trimmed to its facts with a unit; ``expected.json`` gives values read
straight from the facts. No test reaches GLEIF or filings.xbrl.org.
"""

import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financials.providers.esef import (
    FILINGS_URL,
    GLEIF_URL,
    MAX_PAGES,
    USER_AGENT,
    EsefError,
    EsefProvider,
    Filing,
    parse_filings,
    parse_lei_records,
    parse_xbrl_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "esef"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
REPORTS = sorted(path.stem for path in FIXTURES.glob("*-ESEF-*.json"))

TOTAL = "529900S21EQ1BO4ESM68"
AIRBUS = "MINO79WLOO247M1IL051"
BNP = "R0MUWSFPU8MPRO8K5P83"
PERNOD = "52990097YFPX9J0H5D87"
NOVO = "549300DAQ1CVT6CXN342"
SAP = "529900D6BF99LW9R2E68"

AIRBUS_2025 = f"{AIRBUS}-2025-12-31-ESEF-NL-0"
TOTAL_2025 = f"{TOTAL}-2025-12-31-ESEF-FR-0"
PERNOD_2021 = f"{PERNOD}-2021-12-31-ESEF-FR-0"
NOVO_H1_2026 = f"{NOVO}-2026-06-30-ESEF-DK-0"
BNP_2025 = f"{BNP}-2025-12-31-ESEF-FR-1"


def payload(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def report(fxo_id: str):
    return parse_xbrl_json((FIXTURES / f"{fxo_id}.json").read_bytes(), fxo_id[:20], fxo_id)


def fact(esef_report, concept, end, days):
    found = [
        f
        for f in esef_report.facts
        if f.concept == concept and f.period_end == end and f.duration_days == days
    ]
    assert len(found) == 1, (concept, end, days, found)
    return found[0]


def document(fxo_id: str = AIRBUS_2025) -> dict:
    return payload(f"{fxo_id}.json")


def first_fact(content: dict) -> dict:
    return next(iter(content["facts"].values()))


def read(content: dict, lei: str = AIRBUS, fxo_id: str = AIRBUS_2025):
    return parse_xbrl_json(json.dumps(content), lei, fxo_id)


def filing(fxo_id: str = AIRBUS_2025, json_url: str | None = "default") -> Filing:
    url = f"{FILINGS_URL}/{fxo_id}.json" if json_url == "default" else json_url
    return Filing(
        fxo_id=fxo_id,
        lei=fxo_id[:20],
        listed_period_end=None,
        country=None,
        json_url=url,
        viewer_url=None,
        report_url=None,
        package_url=None,
        error_count=0,
        warning_count=0,
        inconsistency_count=0,
        added=None,
        processed=None,
        sha256=None,
    )


# ── GLEIF ────────────────────────────────────────────────────────────────────


class TestLeiRecords:
    @pytest.mark.parametrize(
        ("key", "lei", "name", "country"),
        [
            ("total", TOTAL, "TotalEnergies SE", "FR"),
            ("airbus", AIRBUS, "AIRBUS SE", "NL"),
            ("bnp", BNP, "BNP PARIBAS", "FR"),
            ("pernod", PERNOD, "PERNOD RICARD", "FR"),
            ("novo", NOVO, "NOVO NORDISK A/S", "DK"),
            ("sap", SAP, "SAP SE", "DE"),
        ],
    )
    def test_the_issuer_of_an_isin(self, key, lei, name, country):
        (record,) = parse_lei_records(payload(f"{key}_gleif.json"))
        assert (record.lei, record.legal_name, record.country) == (lei, name, country)
        assert (record.entity_status, record.registration_status) == ("ACTIVE", "ISSUED")

    def test_other_names_are_kept(self):
        content = payload("total_gleif.json")
        content["data"][0]["attributes"]["entity"]["otherNames"] = [
            {"name": "  TOTAL   S.A. "},
            {"name": ""},
        ]
        (record,) = parse_lei_records(content)
        assert record.other_names == ("TOTAL S.A.",)

    def test_an_unknown_isin_gives_no_record(self):
        assert parse_lei_records({"data": [], "meta": {}}) == []

    @pytest.mark.parametrize(
        "content",
        [
            None,
            {"errors": []},
            {"data": [{"id": "not a lei"}]},
            {"data": [{"id": TOTAL, "attributes": {"entity": {}}}]},
        ],
    )
    def test_an_answer_that_is_not_a_record_is_refused(self, content):
        with pytest.raises(EsefError):
            parse_lei_records(content)


# ── The index of filings.xbrl.org ────────────────────────────────────────────


class TestFilings:
    def test_the_filings_of_a_company(self):
        page = parse_filings(payload("total_filings.json"), TOTAL)
        assert len(page.filings) == 8
        assert page.next_url is None
        assert {f.country for f in page.filings} == {"FR", "GB"}  # one filing per language
        latest = max(page.filings, key=lambda f: (f.listed_period_end, f.fxo_id))
        assert latest.fxo_id == f"{TOTAL}-2025-12-31-ESEF-GB-0"
        assert latest.json_url.startswith(f"{FILINGS_URL}/{TOTAL}/2025-12-31/ESEF/GB/0/")
        assert latest.viewer_url.endswith("/reports/ixbrlviewer.html")
        assert latest.processed.tzinfo is UTC

    def test_counts_and_dates_of_a_filing(self):
        filings = {f.fxo_id: f for f in parse_filings(payload("novo_filings.json"), NOVO).filings}
        assert len(filings) == 23
        with_errors = filings[f"{NOVO}-2023-12-31-ESEF-DK-0"]
        assert (with_errors.error_count, with_errors.warning_count) == (80, 2)
        first = filings[f"{NOVO}-2023-03-31-ESEF-DK-0"]
        assert first.added == datetime(2023, 5, 9, 10, 52, 51, 2270, tzinfo=UTC)
        assert first.inconsistency_count == 0
        assert first.sha256.startswith("9c002d62")

    def test_the_period_listed_is_kept_as_listed(self):
        filings = {
            f.fxo_id: f for f in parse_filings(payload("pernod_filings.json"), PERNOD).filings
        }
        # Listed at 2021-12-31, the filing holds the year ended 2021-06-30 (see TestReports).
        assert filings[PERNOD_2021].listed_period_end == date(2021, 12, 31)

    def test_a_company_without_filing(self):
        assert parse_filings(payload("sap_filings.json"), SAP).filings == ()

    def test_a_filing_of_another_company_is_refused(self):
        with pytest.raises(EsefError, match="not a filing of"):
            parse_filings(payload("total_filings.json"), AIRBUS)

    def test_a_link_to_another_site_is_refused(self):
        content = payload("airbus_filings.json")
        content["data"][0]["attributes"]["json_url"] = "https://example.com/report.json"
        with pytest.raises(EsefError, match="not a link of filings.xbrl.org"):
            parse_filings(content, AIRBUS)

    def test_the_next_page(self):
        content = payload("airbus_filings.json")
        content["links"]["next"] = f"{FILINGS_URL}/api/entities/{AIRBUS}/filings?page%5Bnumber%5D=2"
        assert parse_filings(content, AIRBUS).next_url.endswith("page%5Bnumber%5D=2")

    def test_a_filing_without_conversion(self):
        content = payload("airbus_filings.json")
        content["data"][0]["attributes"]["json_url"] = None
        assert parse_filings(content, AIRBUS).filings[0].json_url is None

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("fxo_id", None, "no fxo_id"),
            ("period_end", "31/12/2021", "not a date"),
            ("processed", "yesterday", "not a date and time"),
            ("error_count", -1, "not a count"),
            ("warning_count", "3", "not a count"),
        ],
    )
    def test_a_bad_attribute_is_refused(self, field, value, message):
        content = payload("airbus_filings.json")
        content["data"][0]["attributes"][field] = value
        with pytest.raises(EsefError, match=message):
            parse_filings(content, AIRBUS)

    def test_attributes_not_given(self):
        content = payload("airbus_filings.json")
        attributes = content["data"][0]["attributes"]
        for field in ("period_end", "processed", "error_count", "viewer_url"):
            attributes[field] = None
        first = parse_filings(content, AIRBUS).filings[0]
        assert (first.listed_period_end, first.processed, first.error_count) == (None, None, None)
        assert first.viewer_url is None

    def test_an_answer_that_is_not_json_api_is_refused(self):
        with pytest.raises(EsefError, match="not a JSON:API answer"):
            parse_filings(payload("errors_filings.json"), AIRBUS)


# ── The xBRL-JSON of a filing ────────────────────────────────────────────────


class TestReports:
    @pytest.mark.parametrize("fxo_id", sorted(EXPECTED))
    def test_values_read_straight_from_the_facts(self, fxo_id):
        esef_report = report(fxo_id)
        for expected in EXPECTED[fxo_id]:
            first, _, after = expected["period"].partition("/")
            matches = [
                f
                for f in esef_report.facts
                if f.concept == expected["concept"]
                and f.value == Decimal(expected["value"])
                and (f.duration_days > 0) == bool(after)
            ]
            assert len(matches) == 1, expected

    def test_the_end_of_a_period_is_exclusive(self):
        airbus = report(AIRBUS_2025)
        revenue = fact(airbus, "ifrs-full:Revenue", date(2025, 12, 31), 365)
        assert (revenue.period_start, revenue.unit) == (date(2025, 1, 1), "EUR")
        assert revenue.value == Decimal("73420000000")
        assert revenue.decimals == -6
        assets = fact(airbus, "ifrs-full:Assets", date(2025, 12, 31), 0)
        assert assets.period_start is None
        assert assets.value == Decimal("134939000000")

    def test_units(self):
        airbus = report(AIRBUS_2025)
        eps = fact(airbus, "ifrs-full:DilutedEarningsLossPerShare", date(2025, 12, 31), 365)
        assert (eps.unit, eps.value) == ("EUR/shares", Decimal("6.6"))
        assert {f.unit for f in report(TOTAL_2025).facts} == {"USD", "USD/shares", "EUR/shares"}

    def test_a_french_company_reporting_in_dollars(self):
        total = report(TOTAL_2025)
        revenue = fact(
            total, "ifrs-full:RevenueFromContractsWithCustomers", date(2025, 12, 31), 365
        )
        assert (revenue.unit, revenue.value) == ("USD", Decimal("201196000000"))
        own = fact(
            total,
            "tot:RevenueFromContractsWithCustomersExcludingExciseTax",
            date(2025, 12, 31),
            365,
        )
        assert (own.prefix, own.name) == (
            "tot",
            "RevenueFromContractsWithCustomersExcludingExciseTax",
        )
        assert own.namespace.startswith("http://www.totalenergies.com/")

    def test_the_periods_come_from_the_facts(self):
        # Listed at 2021-12-31 by the index: the fiscal year ended 2021-06-30.
        pernod = report(PERNOD_2021)
        assert pernod.period_ends[0] == date(2021, 6, 30)
        profit = fact(
            pernod, "ifrs-full:ProfitLossAttributableToOwnersOfParent", date(2021, 6, 30), 365
        )
        assert profit.period_start == date(2020, 7, 1)

    def test_a_half_year(self):
        revenue = fact(report(NOVO_H1_2026), "ifrs-full:Revenue", date(2026, 6, 30), 181)
        assert (revenue.unit, revenue.value) == ("DKK", Decimal("175311000000"))

    def test_breakdowns_and_texts_are_left_out(self):
        bnp = report(BNP_2025)
        assert bnp.breakdowns == 465
        assert len(bnp.facts) == 290
        assert all(f.unit for f in bnp.facts)
        assert bnp.conflicts == ()
        assert bnp.taxonomy == ("http://invest.bnpparibas/xbrl/2026-02-20/bnpp-2025-12-31.xsd",)

    @pytest.mark.parametrize("fxo_id", REPORTS)
    def test_every_filing_is_read(self, fxo_id):
        esef_report = report(fxo_id)
        assert esef_report.lei == fxo_id[:20]
        assert len(esef_report.facts) > 200
        assert len({f.key for f in esef_report.facts}) == len(esef_report.facts)

    def test_a_fact_given_twice_is_kept_once_the_most_precise(self):
        content = document()
        revenue = next(
            f
            for f in content["facts"].values()
            if f["dimensions"]["concept"] == "ifrs-full:Revenue"
            and f["dimensions"]["period"].startswith("2025")
        )
        rounded = copy.deepcopy(revenue)
        rounded["decimals"] = -9
        precise = copy.deepcopy(revenue)
        precise["decimals"] = 0
        content["facts"] = {"a": rounded, "b": precise, "c": revenue}
        (kept,) = read(content).facts
        assert kept.decimals == 0

    def test_two_values_of_one_fact_drop_it(self):
        content = document()
        revenue = next(
            f
            for f in content["facts"].values()
            if f["dimensions"]["concept"] == "ifrs-full:Revenue"
            and f["dimensions"]["period"].startswith("2025")
        )
        other = copy.deepcopy(revenue)
        other["value"] = "1.0"
        content["facts"]["conflicting"] = other
        esef_report = read(content)
        assert not [
            f
            for f in esef_report.facts
            if f.concept == "ifrs-full:Revenue" and f.period_end == date(2025, 12, 31)
        ]
        assert esef_report.conflicts == ("ifrs-full:Revenue EUR 2025-12-31 (365 days)",)
        # The other years of the concept are kept.
        assert fact(esef_report, "ifrs-full:Revenue", date(2024, 12, 31), 366)

    def test_a_text_is_left_out(self):
        content = document()
        text = copy.deepcopy(first_fact(content))
        del text["dimensions"]["unit"]
        text["dimensions"]["language"] = "en"
        text["value"] = "<p>Notes to the consolidated financial statements</p>"
        content["facts"] = {"text": text, "number": first_fact(content)}
        esef_report = read(content)
        assert len(esef_report.facts) == 1
        assert esef_report.breakdowns == 0

    def test_a_nil_value_is_left_out(self):
        content = document()
        nil = copy.deepcopy(first_fact(content))
        nil["value"] = None
        content["facts"] = {"nil": nil}
        assert read(content).facts == ()

    def test_values_without_decimals_and_as_numbers(self):
        content = document()
        number = copy.deepcopy(first_fact(content))
        number.pop("decimals", None)
        number["value"] = 12
        content["facts"] = {"n": number}
        (kept,) = read(content).facts
        assert (kept.value, kept.decimals) == (Decimal(12), None)

    def test_a_unit_of_the_company_is_kept_as_written(self):
        content = document()
        content["documentInfo"]["namespaces"]["utr"] = "http://www.xbrl.org/2009/utr"
        tonnes = copy.deepcopy(first_fact(content))
        tonnes["dimensions"]["unit"] = "utr:t"
        content["facts"] = {"t": tonnes}
        assert read(content).facts[0].unit == "utr:t"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            (lambda c: c.update({"documentInfo": {"documentType": "x"}}), "not an xBRL-JSON"),
            (lambda c: c.pop("documentInfo"), "not an xBRL-JSON"),
            (lambda c: c.update({"facts": {}}), "no fact"),
            (lambda c: first_fact(c).pop("dimensions"), "no dimensions"),
            (lambda c: first_fact(c)["dimensions"].update(entity=f"scheme:{TOTAL}"), "not of"),
            (lambda c: first_fact(c)["dimensions"].update(entity=AIRBUS), "not of"),
            (lambda c: first_fact(c)["dimensions"].update(concept="nope:Revenue"), "known prefix"),
            (lambda c: first_fact(c)["dimensions"].update(concept="Revenue"), "known prefix"),
            (lambda c: first_fact(c)["dimensions"].pop("period"), "no period"),
            (
                lambda c: first_fact(c)["dimensions"].update(period="2025-13-01T00:00:00"),
                "not a date",
            ),
            (
                lambda c: first_fact(c)["dimensions"].update(period="2025-01-01T12:00:00"),
                "not the start of a day",
            ),
            (
                lambda c: first_fact(c)["dimensions"].update(
                    period="2026-01-01T00:00:00/2025-01-01T00:00:00"
                ),
                "ends before it starts",
            ),
            (lambda c: first_fact(c).update(value="12 M€"), "not a number"),
            (lambda c: first_fact(c).update(value="NaN"), "not a number"),
            (lambda c: first_fact(c).update(value=True), "not a number"),
            (lambda c: first_fact(c)["dimensions"].update(unit="iso4217:euro"), "not a currency"),
            (lambda c: first_fact(c)["dimensions"].update(unit="money:EUR"), "known prefix"),
            (lambda c: first_fact(c)["dimensions"].update(unit="EUR"), "known prefix"),
        ],
    )
    def test_a_bad_file_is_refused_whole(self, change, message):
        content = document()
        change(content)
        with pytest.raises(EsefError, match=message):
            read(content)

    @pytest.mark.parametrize("content", [b"<html>", b"[1, 2]", b"\xff\xfe"])
    def test_a_page_that_is_not_xbrl_json(self, content):
        with pytest.raises(EsefError):
            parse_xbrl_json(content, AIRBUS, AIRBUS_2025)


# ── The provider on the network ──────────────────────────────────────────────


GLEIF_PART = "api.gleif.org/api/v1/lei-records"
FILINGS_PART = f"filings.xbrl.org/api/entities/{AIRBUS}/filings"


def json_response(content, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=content)


class TestProvider:
    async def test_the_lei_of_an_isin(self, fake_network):
        fake_network.get(GLEIF_PART, json_response(payload("airbus_gleif.json")))
        (record,) = await EsefProvider().lei_records("NL0000235190")
        assert record.lei == AIRBUS
        (request,) = fake_network.requests
        assert str(request.url).startswith(GLEIF_URL)
        assert request.url.params["filter[isin]"] == "NL0000235190"
        assert request.headers["User-Agent"] == USER_AGENT

    async def test_gleif_not_answering_gives_nothing(self, fake_network):
        fake_network.get(GLEIF_PART, httpx.Response(503, text="busy"))
        assert await EsefProvider().lei_records("NL0000235190") is None

    async def test_something_else_than_an_isin_is_not_asked(self, fake_network):
        with pytest.raises(EsefError, match="not an ISIN"):
            await EsefProvider().lei_records("AIR.PA")
        assert fake_network.requests == []

    async def test_the_filings_of_a_lei(self, fake_network):
        fake_network.get(FILINGS_PART, json_response(payload("airbus_filings.json")))
        filings = await EsefProvider().filings(AIRBUS)
        assert len(filings) == 5
        (request,) = fake_network.requests
        assert request.url.params["page[size]"] == "100"
        assert request.headers["Accept"] == "application/vnd.api+json"

    async def test_the_pages_are_followed(self, fake_network):
        first = payload("airbus_filings.json")
        second = copy.deepcopy(first)
        first["data"], second["data"] = first["data"][:2], second["data"][2:]
        first["links"]["next"] = f"{FILINGS_URL}/api/entities/{AIRBUS}/filings?page%5Bnumber%5D=2"
        fake_network.get("page%5Bnumber%5D=2", json_response(second))
        fake_network.get(FILINGS_PART, json_response(first))
        filings = await EsefProvider().filings(AIRBUS)
        assert [f.fxo_id for f in filings] == [
            item["attributes"]["fxo_id"] for item in payload("airbus_filings.json")["data"]
        ]
        assert fake_network.calls("filings.xbrl.org") == 2

    async def test_too_many_pages_are_refused(self, fake_network):
        looping = payload("airbus_filings.json")
        looping["links"]["next"] = f"{FILINGS_URL}/api/entities/{AIRBUS}/filings?page%5Bnumber%5D=2"
        fake_network.get(FILINGS_PART, json_response(looping))
        with pytest.raises(EsefError, match="more than"):
            await EsefProvider().filings(AIRBUS)
        assert fake_network.calls("filings.xbrl.org") == MAX_PAGES

    async def test_a_page_not_read_gives_nothing(self, fake_network):
        fake_network.get(FILINGS_PART, httpx.Response(500, text="Unknown error"))
        assert await EsefProvider().filings(AIRBUS) is None

    async def test_something_else_than_a_lei_is_not_asked(self, fake_network):
        with pytest.raises(EsefError, match="not a LEI"):
            await EsefProvider().filings("FR0000120271")

    async def test_the_numbers_of_a_filing(self, fake_network):
        fake_network.get(
            f"filings.xbrl.org/{AIRBUS_2025}.json",
            httpx.Response(200, content=(FIXTURES / f"{AIRBUS_2025}.json").read_bytes()),
        )
        esef_report = await EsefProvider().report(filing())
        assert fact(esef_report, "ifrs-full:Revenue", date(2025, 12, 31), 365).value == Decimal(
            "73420000000"
        )
        assert fake_network.requests[0].headers["User-Agent"] == USER_AGENT

    async def test_a_filing_without_xbrl_json_is_not_asked(self, fake_network):
        assert await EsefProvider().report(filing(json_url=None)) is None
        assert fake_network.requests == []

    async def test_a_file_not_given_gives_nothing(self, fake_network):
        fake_network.get("filings.xbrl.org/", httpx.Response(404, text="Not Found"))
        assert await EsefProvider().report(filing()) is None

    async def test_a_server_error_is_tried_again(self, fake_network):
        fake_network.get(
            "filings.xbrl.org/",
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, content=(FIXTURES / f"{AIRBUS_2025}.json").read_bytes()),
        )
        assert await EsefProvider().report(filing()) is not None
        assert fake_network.calls("filings.xbrl.org") == 2

    async def test_no_connection_gives_nothing(self, fake_network):
        fake_network.get("filings.xbrl.org/", httpx.ConnectError("no route"))
        assert await EsefProvider().report(filing()) is None

    async def test_a_file_of_another_company_is_refused(self, fake_network):
        fake_network.get(
            "filings.xbrl.org/",
            httpx.Response(200, content=(FIXTURES / f"{AIRBUS_2025}.json").read_bytes()),
        )
        with pytest.raises(EsefError, match="not of"):
            await EsefProvider().report(filing(f"{TOTAL}-2025-12-31-ESEF-FR-0"))


# ── What the first reading of a real catalogue showed ────────────────────────


class TestFirstCatalogueReading:
    async def test_a_lei_the_index_does_not_know_has_no_filing(self, fake_network):
        fake_network.get(FILINGS_PART, httpx.Response(404, text="Not Found"))
        assert await EsefProvider().filings(AIRBUS) == []
        assert fake_network.calls("filings.xbrl.org") == 1  # 404 is not tried again

    async def test_a_second_page_not_found_is_a_reading_not_done(self, fake_network):
        first = payload("airbus_filings.json")
        first["links"]["next"] = f"{FILINGS_URL}/api/entities/{AIRBUS}/filings?page%5Bnumber%5D=2"
        fake_network.get("page%5Bnumber%5D=2", httpx.Response(404, text="Not Found"))
        fake_network.get(FILINGS_PART, json_response(first))
        assert await EsefProvider().filings(AIRBUS) is None

    async def test_an_index_that_is_not_json(self, fake_network):
        fake_network.get(FILINGS_PART, httpx.Response(200, text="<html>maintenance</html>"))
        with pytest.raises(EsefError, match="not JSON"):
            await EsefProvider().filings(AIRBUS)

    async def test_gleif_asking_to_wait_is_asked_again(self, fake_network):
        fake_network.get(
            GLEIF_PART,
            httpx.Response(429, text="Too Many Requests"),
            json_response(payload("airbus_gleif.json")),
        )
        (record,) = await EsefProvider().lei_records("NL0000235190")
        assert record.lei == AIRBUS
        assert fake_network.backoffs == [2.0]

    def test_the_candidate_recommendation_of_xbrl_json(self):
        content = document()
        content["documentInfo"]["documentType"] = "https://xbrl.org/CR/2021-02-03/xbrl-json"
        assert len(read(content).facts) == len(report(AIRBUS_2025).facts)

    def test_a_footnote_has_no_entity_and_is_left_out(self):
        content = document()
        content["facts"]["footnote1"] = {
            "value": "Restated, see note 2.",
            "dimensions": {"concept": "xbrl:note", "language": "en"},
        }
        content["facts"]["fn-2"] = {"value": "See note 4", "dimensions": {"concept": "xbrl:note"}}
        assert len(read(content).facts) == len(report(AIRBUS_2025).facts)

    def test_the_names_written_in_latin_letters(self):
        content = payload("airbus_gleif.json")
        entity = content["data"][0]["attributes"]["entity"]
        entity["otherNames"] = [{"name": "AIRBUS GROUP SE", "language": "en"}]
        entity["transliteratedOtherNames"] = [
            {"name": "AIRBUS SE", "type": "AUTO_ASCII_TRANSLITERATED_LEGAL_NAME"},
            {"name": "AIRBUS GROUP SE", "type": "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME"},
            "not a name",
        ]
        (record,) = parse_lei_records(content)
        assert record.other_names == ("AIRBUS GROUP SE", "AIRBUS SE")

    async def test_no_connection_to_the_index_gives_nothing(self, fake_network):
        fake_network.get(FILINGS_PART, httpx.ConnectError("no route"))
        assert await EsefProvider().filings(AIRBUS) is None
