"""The Commitments of Traders files of the CFTC (``financials/providers/cftc.py``).

The zips of ``tests/fixtures/cftc/`` are reduced real answers; ``expected.json`` gives
what a reading must find, read from the full CFTC files. No test reaches the CFTC.
"""

import csv
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest

from financials.providers.cftc import (
    DISAGGREGATED,
    HISTORY_LAST_YEAR,
    LEGACY,
    REPORTS,
    TFF,
    CftcError,
    CftcProvider,
    parse_cot_zip,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cftc"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
URL_PART = "www.cftc.gov/files/dea/history/"

# The year each fixture is asked for (a year up to 2016 gives the history file).
YEAR_OF = {
    "deacot2026.zip": 2026,
    "deacot2025.zip": 2025,
    "deacot1986_2016.zip": 1990,
    "fut_fin_txt_2026.zip": 2026,
    "fin_fut_txt_2006_2016.zip": 2010,
    "fut_disagg_txt_2026.zip": 2026,
    "fut_disagg_txt_hist_2006_2016.zip": 2006,
}
CATEGORIES = (
    "noncommercial",
    "commercial",
    "dealer",
    "asset_manager",
    "leveraged_funds",
    "other_reportable",
    "producer_merchant",
    "swap_dealer",
    "managed_money",
    "nonreportable",
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def read(name: str):
    return parse_cot_zip(fixture(name), EXPECTED[name]["report"], YEAR_OF[name])


def week_of(cot_file, code: str, day: str):
    (week,) = [w for w in cot_file.weeks if w.code == code and w.report_date.isoformat() == day]
    return week


def member_text(name: str) -> tuple[str, str]:
    with zipfile.ZipFile(FIXTURES / name) as archive:
        (member,) = archive.namelist()
        return member, archive.read(member).decode("utf-8")


def rezip(member: str, text: str | bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def changed(name: str, edit) -> bytes:
    """The fixture with its text changed by ``edit(lines) -> lines`` (line ends kept)."""
    member, text = member_text(name)
    return rezip(member, "".join(edit(text.splitlines(keepends=True))))


def expected_values(expected: dict) -> dict:
    """``{"leveraged_funds_long": 1}`` as ``{("leveraged_funds", "long"): 1}``; trader counts left out."""
    values = {}
    for key, value in expected.items():
        if key in ("name", "open_interest") or key.startswith("traders_"):
            continue
        category, side = key.rsplit("_", 1)
        assert category in CATEGORIES, key
        values[(category, "spreading" if side == "spreading" else side)] = value
    return values


class TestRealFiles:
    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_what_each_file_holds(self, name):
        expected, cot_file = EXPECTED[name], read(name)

        assert (cot_file.report, cot_file.file) == (expected["report"], name)
        assert len(cot_file.weeks) == expected["rows"]
        counts = {}
        for week in cot_file.weeks:
            counts[week.code] = counts.get(week.code, 0) + 1
        assert counts == expected["markets"]
        dates = cot_file.report_dates
        assert (dates[0].isoformat(), dates[-1].isoformat()) == (
            expected["first_day"],
            expected["last_day"],
        )

    @pytest.mark.parametrize(
        ("name", "spot"),
        [(name, spot) for name in sorted(EXPECTED) for spot in EXPECTED[name]["values"]],
    )
    def test_the_positions_of_a_market(self, name, spot):
        code, day = spot.split("@")
        expected = EXPECTED[name]["values"][spot]
        week = week_of(read(name), code, day)

        assert week.name == " ".join(expected["name"].split())
        assert week.open_interest == expected["open_interest"]
        found = {}
        for position in week.positions:
            found[(position.category, "long")] = position.long
            found[(position.category, "short")] = position.short
            if position.spreading is not None:
                found[(position.category, "spreading")] = position.spreading
        assert found == expected_values(expected)

    def test_lines_ordered_by_date_then_code(self):
        weeks = read("deacot2026.zip").weeks
        keys = [(week.report_date, week.code) for week in weeks]
        assert keys == sorted(keys)
        assert weeks[-1].report_date == date(2026, 10, 13)

    def test_the_categories_of_each_report(self):
        def categories(name):
            return [position.category for position in read(name).weeks[0].positions]

        assert categories("deacot2026.zip") == ["noncommercial", "commercial", "nonreportable"]
        assert categories("fut_fin_txt_2026.zip") == [
            "dealer",
            "asset_manager",
            "leveraged_funds",
            "other_reportable",
            "nonreportable",
        ]
        assert categories("fut_disagg_txt_2026.zip") == [
            "producer_merchant",
            "swap_dealer",
            "managed_money",
            "other_reportable",
            "nonreportable",
        ]

    def test_spreading_only_where_the_report_gives_it(self):
        legacy = {p.category: p for p in read("deacot2026.zip").weeks[0].positions}
        assert legacy["noncommercial"].spreading is not None
        assert legacy["commercial"].spreading is None and legacy["nonreportable"].spreading is None
        disaggregated = {p.category: p for p in read("fut_disagg_txt_2026.zip").weeks[0].positions}
        assert disaggregated["producer_merchant"].spreading is None

    def test_the_net_position(self):
        week = week_of(read("deacot2026.zip"), "13874A", "2026-10-13")
        noncommercial = week.positions[0]
        assert noncommercial.net == 224804 - 365799

    def test_the_tff_history_writes_dates_and_numbers_its_own_way(self):
        cot_file = read("fin_fut_txt_2006_2016.zip")
        assert (cot_file.first_year, cot_file.last_year) == (2006, 2016)
        first = week_of(cot_file, "13874A", "2006-06-13")
        assert first.open_interest == 2042655
        # A Monday report, in a holiday week.
        assert week_of(cot_file, "13874A", "2012-12-24").report_date.weekday() == 0

    def test_reports_twice_a_month_then_every_week(self):
        dates = [
            week.report_date for week in read("deacot1986_2016.zip").weeks if week.code == "088691"
        ]
        assert dates[:2] == [date(1986, 1, 15), date(1986, 1, 31)]
        assert date(1992, 9, 30) in dates and date(1992, 10, 6) in dates

    def test_a_negative_nonreportable_position_is_kept(self):
        week = week_of(read("deacot1986_2016.zip"), "043602", "1987-06-30")
        assert week.positions[-1].long == -984

    def test_a_market_without_contract_units(self):
        assert week_of(read("deacot1986_2016.zip"), "063731", "1995-04-04").contract_units is None
        units = week_of(read("deacot2026.zip"), "088691", "2026-10-13").contract_units
        assert units == "(CONTRACTS OF 100 TROY OUNCES)"

    def test_a_consolidated_market_adds_up_within_its_rounding(self):
        week = week_of(read("deacot2026.zip"), "20974+", "2026-09-29")
        longs = sum(p.long + (p.spreading or 0) for p in week.positions)
        assert longs - week.open_interest == 1

    def test_a_market_present_some_weeks_only(self):
        oats = [w.report_date for w in read("deacot2026.zip").weeks if w.code == "004603"]
        assert oats == [date(2026, 9, 1), date(2026, 10, 6)]

    def test_the_name_keeps_one_space_between_words(self):
        member, text = member_text("deacot2026.zip")
        content = rezip(member, text.replace("GOLD - COMMODITY", "GOLD  -   COMMODITY"))
        week = week_of(parse_cot_zip(content, LEGACY, 2026), "088691", "2026-10-13")
        assert week.name == "GOLD - COMMODITY EXCHANGE INC."


class TestReports:
    def test_the_file_of_a_year(self):
        legacy = REPORTS[LEGACY]
        assert legacy.file_name(2026) == "deacot2026.zip"
        assert legacy.file_name(2017) == "deacot2017.zip"
        assert legacy.file_name(HISTORY_LAST_YEAR) == "deacot1986_2016.zip"
        assert legacy.file_name(1986) == "deacot1986_2016.zip"
        assert REPORTS[TFF].file_name(2026) == "fut_fin_txt_2026.zip"
        assert REPORTS[TFF].file_name(2010) == "fin_fut_txt_2006_2016.zip"
        assert REPORTS[DISAGGREGATED].file_name(2026) == "fut_disagg_txt_2026.zip"
        assert REPORTS[DISAGGREGATED].file_name(2006) == "fut_disagg_txt_hist_2006_2016.zip"

    def test_the_years_a_file_holds(self):
        assert REPORTS[LEGACY].years_of_file(1999) == (1986, 2016)
        assert REPORTS[TFF].years_of_file(2016) == (2006, 2016)
        assert REPORTS[TFF].years_of_file(2017) == (2017, 2017)

    def test_the_first_report_dates(self):
        assert REPORTS[LEGACY].first_day == date(1986, 1, 15)
        assert REPORTS[TFF].first_day == REPORTS[DISAGGREGATED].first_day == date(2006, 6, 13)

    def test_a_year_before_the_report_has_no_file(self):
        with pytest.raises(CftcError, match="starts in 2006, not 2005"):
            REPORTS[TFF].file_name(2005)

    def test_the_url(self):
        assert REPORTS[LEGACY].url(2026) == "https://www.cftc.gov/files/dea/history/deacot2026.zip"


def first_line_of(code: str, day: str):
    """Edit the first line of a market on a date (the header stays line 0)."""

    def edit_with(change):
        def edit(lines):
            for number, line in enumerate(lines[1:], start=1):
                if f",{day}," in line and f",{code}," in line:
                    lines[number] = change(line)
                    return lines
            raise AssertionError((code, day))

        return edit

    return edit_with


GOLD = first_line_of("088691", "2026-10-13")


def cell_changed(line: str, index: int, value: str) -> str:
    (row,) = list(csv.reader([line]))
    row[index] = value
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerow(row)
    return out.getvalue()


class TestFilesThatCannotBeRead:
    def test_an_answer_that_is_not_a_zip(self):
        with pytest.raises(CftcError, match="not a zip file"):
            parse_cot_zip(fixture("not_a_zip.zip"), LEGACY, 2026)

    def test_a_zip_with_two_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("annual.txt", "a")
            archive.writestr("notes.txt", "b")
        with pytest.raises(CftcError, match="expected one .txt file"):
            parse_cot_zip(buffer.getvalue(), LEGACY, 2026)

    def test_a_zip_with_something_else_than_text(self):
        with pytest.raises(CftcError, match="expected one .txt file"):
            parse_cot_zip(rezip("annual.xls", b"\xd0\xcf"), LEGACY, 2026)

    def test_an_empty_file(self):
        with pytest.raises(CftcError, match="is empty"):
            parse_cot_zip(rezip("annual.txt", ""), LEGACY, 2026)

    def test_a_file_that_is_not_utf8(self):
        member, text = member_text("deacot2026.zip")
        content = rezip(member, text.encode("utf-8").replace(b"GOLD", b"G\xffLD"))
        with pytest.raises(CftcError, match="not UTF-8 text"):
            parse_cot_zip(content, LEGACY, 2026)

    def test_a_header_that_is_not_utf8(self):
        member, text = member_text("deacot2026.zip")
        content = rezip(member, b"\xff" + text.encode("utf-8"))
        with pytest.raises(CftcError, match="not UTF-8 text"):
            parse_cot_zip(content, LEGACY, 2026)

    def test_a_column_added(self):
        content = changed("deacot2026.zip", lambda lines: [lines[0].replace("\r\n", ',"New"\r\n')])
        with pytest.raises(CftcError, match="130 columns, not 129: its layout changed"):
            parse_cot_zip(content, LEGACY, 2026)

    def test_a_column_renamed(self):
        def edit(lines):
            lines[0] = lines[0].replace("Open Interest (All)", "Open Interest (Total)")
            return lines

        with pytest.raises(CftcError, match="no column \\['Open Interest \\(All\\)'\\]"):
            parse_cot_zip(changed("deacot2026.zip", edit), LEGACY, 2026)

    def test_a_column_given_twice(self):
        def edit(lines):
            lines[0] = lines[0].replace('"CFTC Region Code"', '"CFTC Commodity Code"')
            return lines

        with pytest.raises(CftcError, match="given twice"):
            parse_cot_zip(changed("deacot2026.zip", edit), LEGACY, 2026)

    def test_a_line_with_a_cell_missing(self):
        content = changed("deacot2026.zip", GOLD(lambda line: line.replace(",", "", 1)))
        with pytest.raises(CftcError, match="128 cells for 129 columns"):
            parse_cot_zip(content, LEGACY, 2026)

    @pytest.mark.parametrize(
        ("index", "value", "message"),
        [
            (3, "88691", "is not a CFTC contract market code"),
            (3, "088-91", "is not a CFTC contract market code"),
            (2, "13/10/2026", "is not a report date"),
            (1, "261012", "are not the same day"),
            (7, "lots", "is not a number of contracts"),
            (8, "12.5", "is not a number of contracts"),
            (8, ".", "is not a number of contracts"),
            (8, "-1", "is negative"),
            (10, "-1", "is negative"),
            (0, "   ", "has no name"),
            (8, "245301", "long positions of 088691 add up to"),
            (12, "296951", "short positions of 088691 add up to"),
        ],
    )
    def test_a_bad_cell(self, index, value, message):
        content = changed("deacot2026.zip", GOLD(lambda line: cell_changed(line, index, value)))
        with pytest.raises(CftcError, match=message):
            parse_cot_zip(content, LEGACY, 2026)

    def test_a_date_outside_the_file(self):
        def edit(line):
            line = cell_changed(line, 2, "2025-10-14")
            return cell_changed(line, 1, "251014")

        with pytest.raises(CftcError, match="is not in the file of 2026–2026"):
            parse_cot_zip(changed("deacot2026.zip", GOLD(edit)), LEGACY, 2026)

    def test_a_line_given_twice(self):
        content = changed("deacot2026.zip", GOLD(lambda line: line + line))
        with pytest.raises(CftcError, match="088691 on 2026-10-13 is given twice"):
            parse_cot_zip(content, LEGACY, 2026)

    def test_blank_lines_are_skipped(self):
        content = changed("deacot2026.zip", lambda lines: [*lines, "\r\n"])
        assert len(parse_cot_zip(content, LEGACY, 2026).weeks) == 50

    def test_an_unknown_report(self):
        with pytest.raises(CftcError, match="unknown report 'supplemental'"):
            parse_cot_zip(fixture("deacot2026.zip"), "supplemental", 2026)

    def test_the_file_of_another_report(self):
        with pytest.raises(CftcError, match="has 87 columns, not 129"):
            parse_cot_zip(fixture("fut_fin_txt_2026.zip"), LEGACY, 2026)


class TestProvider:
    async def test_reads_the_file_of_a_year(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, content=fixture("deacot2026.zip")))

        cot_file = await CftcProvider().report_file(LEGACY, 2026)

        assert cot_file.report_dates[-1] == date(2026, 10, 13)
        assert fake_network.urls() == ["https://www.cftc.gov/files/dea/history/deacot2026.zip"]
        assert "application/zip" in fake_network.requests[0].headers["Accept"]

    async def test_a_year_of_the_history_asks_the_history_file(self, fake_network):
        fake_network.get(
            URL_PART, httpx.Response(200, content=fixture("fin_fut_txt_2006_2016.zip"))
        )
        cot_file = await CftcProvider().report_file(TFF, 2012)
        assert (cot_file.first_year, cot_file.last_year) == (2006, 2016)
        assert fake_network.urls() == [
            "https://www.cftc.gov/files/dea/history/fin_fut_txt_2006_2016.zip"
        ]

    async def test_a_file_not_published_gives_nothing(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(404, text="<html>Not Found</html>"))
        assert await CftcProvider().report_file(LEGACY, 2027) is None
        assert fake_network.calls(URL_PART) == 1

    async def test_a_server_error_is_tried_again(self, fake_network):
        fake_network.get(
            URL_PART,
            httpx.Response(503, text="busy"),
            httpx.Response(200, content=fixture("fut_disagg_txt_2026.zip")),
        )
        cot_file = await CftcProvider().report_file(DISAGGREGATED, 2026)
        assert len(cot_file.weeks) == 24
        assert fake_network.calls(URL_PART) == 2

    async def test_a_server_error_every_time_gives_nothing(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(503, text="busy"))
        assert await CftcProvider().report_file(LEGACY, 2026) is None
        assert fake_network.calls(URL_PART) == CftcProvider.max_retries

    async def test_no_connection_gives_nothing(self, fake_network):
        fake_network.get(URL_PART, httpx.ConnectError("no route"))
        assert await CftcProvider().report_file(LEGACY, 2026) is None

    async def test_a_page_that_is_not_a_zip(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, content=fixture("not_a_zip.zip")))
        with pytest.raises(CftcError, match="not a zip file"):
            await CftcProvider().report_file(LEGACY, 2026)

    async def test_an_unknown_report_is_not_asked(self, fake_network):
        with pytest.raises(CftcError):
            await CftcProvider().report_file("supplemental", 2026)
        assert fake_network.requests == []

    async def test_a_year_before_the_report_is_not_asked(self, fake_network):
        with pytest.raises(CftcError):
            await CftcProvider().report_file(TFF, 2005)
        assert fake_network.requests == []
