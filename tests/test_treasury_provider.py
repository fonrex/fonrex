"""The yield curve files of the US Treasury (``financials/providers/treasury.py``).

The files of ``tests/fixtures/treasury/`` are reduced real answers; ``expected.json``
gives what a reading must find. No test reaches the Treasury.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financials.providers.treasury import (
    CURVES,
    NOMINAL,
    REAL,
    TENORS,
    TENORS_BY_CODE,
    TreasuryError,
    TreasuryProvider,
    parse_curve_csv,
    tenor_of,
)

FIXTURES = Path(__file__).parent / "fixtures" / "treasury"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
FILES = sorted(name for name in EXPECTED if name.endswith(".csv"))
URL_PART = "/interest-rates/daily-treasury-rates.csv/"


def fixture(name: str) -> str:
    # Read as bytes: the files are kept exactly as the Treasury sent them.
    return (FIXTURES / name).read_bytes().decode("utf-8")


def read(name: str):
    year = int(name.removesuffix(".csv").rsplit("_", 1)[1])
    return parse_curve_csv(fixture(name), EXPECTED[name]["curve"], year)


def by_day(curve_file) -> dict[str, dict[str, Decimal]]:
    return {day.day.isoformat(): day.points for day in curve_file.days}


class TestRealFiles:
    @pytest.mark.parametrize("name", FILES)
    def test_what_each_file_holds(self, name):
        expected, curve_file = EXPECTED[name], read(name)
        days = by_day(curve_file)

        assert curve_file.curve == expected["curve"]
        assert len(days) == expected["days"]
        if expected["days"]:
            assert list(curve_file.tenors) == expected["tenors"]
            assert (min(days), max(days)) == (expected["first_day"], expected["last_day"])
        for day, points in expected.get("points", {}).items():
            assert days[day] == {tenor: Decimal(value) for tenor, value in points.items()}
        for day, absent in expected.get("missing", {}).items():
            assert not set(absent) & set(days[day])

    def test_the_days_oldest_first(self):
        days = [day.day for day in read("nominal_2026.csv").days]
        assert days == sorted(days) and days[-1] == date(2026, 10, 16)

    def test_a_zero_is_a_rate_and_an_empty_cell_is_none(self):
        zero_day = by_day(read("nominal_2020.csv"))["2020-03-25"]
        assert zero_day["1M"] == 0 and "1M" in zero_day
        before_the_6_weeks = by_day(read("nominal_2025.csv"))["2025-02-14"]
        assert "6W" not in before_the_6_weeks and before_the_6_weeks["1M"] == Decimal("0.0437")

    def test_negative_real_yields(self):
        assert by_day(read("real_2021.csv"))["2021-01-04"]["5Y"] == Decimal("-0.0162")

    def test_a_year_without_data(self):
        empty = read("empty_1989.csv")
        assert (empty.tenors, empty.days) == ((), ())

    def test_breakeven_and_spread_of_the_files(self):
        nominal = by_day(read("nominal_2026.csv"))["2026-10-16"]
        real = by_day(read("real_2026.csv"))["2026-10-16"]
        derived = EXPECTED["derived_2026-10-16"]
        assert {tenor: nominal[tenor] - real[tenor] for tenor in real} == {
            tenor: Decimal(value) for tenor, value in derived["breakeven"].items()
        }
        assert nominal["10Y"] - nominal["2Y"] == Decimal(derived["spread_10Y_2Y"])


class TestTenors:
    def test_the_codes_and_their_length(self):
        assert {tenor.code: float(tenor.months) for tenor in TENORS} == EXPECTED["tenor_months"]
        assert [tenor.months for tenor in TENORS] == sorted(tenor.months for tenor in TENORS)
        assert TENORS_BY_CODE["6W"].label == "1.5 Month"

    @pytest.mark.parametrize(
        ("label", "code"),
        [
            ("1 Mo", "1M"),
            ("1.5 Month", "6W"),
            ("10 Yr", "10Y"),
            ("10 YR", "10Y"),
            ('"30  Yr"', "30Y"),
        ],
    )
    def test_the_labels_of_both_curves(self, label, code):
        assert tenor_of(label) == code

    def test_a_new_column_is_refused(self):
        with pytest.raises(TreasuryError, match="unknown maturity column '15 Yr'"):
            tenor_of("15 Yr")


HEADER = 'Date,"1 Mo","10 Yr"'


class TestFilesThatCannotBeRead:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ('Day,"1 Mo"\n10/16/2026,4.12', "does not start with a Date column"),
            ('Date,"1 Mo","15 Yr"\n10/16/2026,4.12,5.00', "unknown maturity column"),
            ('Date,"10 Yr","10 YR"\n10/16/2026,5.09,5.09', "given twice"),
            (HEADER + "\n10/16/2026,4.12", "2 cells for 3 columns"),
            (HEADER + "\n2026-10-16,4.12,5.09", "is not a date MM/DD/YYYY"),
            (HEADER + "\n12/31/2025,4.12,5.09", "is not in the file of 2026"),
            (HEADER + "\n10/16/2026,4.12,N/A", "'N/A' is not a rate"),
            (HEADER + "\n10/16/2026,4.12,509", "is not a rate in percent"),
            (HEADER + "\n10/16/2026,4.12,NaN", "is not a rate in percent"),
            (HEADER + "\n10/16/2026,4.12,5.09\n10/16/2026,4.12,5.09", "is given twice"),
        ],
    )
    def test_nothing_is_kept(self, text, message):
        with pytest.raises(TreasuryError, match=message):
            parse_curve_csv(text, NOMINAL, 2026)

    def test_an_unknown_curve(self):
        with pytest.raises(TreasuryError, match="unknown curve 'breakeven'"):
            parse_curve_csv(HEADER, "breakeven", 2026)

    def test_a_byte_order_mark_a_blank_line_and_a_day_without_rate(self):
        text = "﻿" + HEADER + "\n10/16/2026,4.12,5.09\n\n10/15/2026,,\n"
        curve_file = parse_curve_csv(text, NOMINAL, 2026)
        assert [day.day for day in curve_file.days] == [date(2026, 10, 16)]


class TestProvider:
    async def test_reads_the_file_of_a_year(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=fixture("nominal_2026.csv")))

        curve_file = await TreasuryProvider().curve_year(NOMINAL, 2026)

        assert curve_file.days[-1].points["10Y"] == Decimal("0.0509")
        assert fake_network.urls() == [
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            "daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve"
            "&field_tdr_date_value=2026&page&_format=csv"
        ]

    async def test_the_real_curve(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=fixture("real_2026.csv")))
        curve_file = await TreasuryProvider().curve_year(REAL, 2026)
        assert curve_file.tenors == ("5Y", "7Y", "10Y", "20Y", "30Y")
        assert "type=daily_treasury_real_yield_curve" in fake_network.urls()[0]

    async def test_a_year_without_data(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=""))
        assert (await TreasuryProvider().curve_year(NOMINAL, 1989)).days == ()

    async def test_a_refusal_gives_nothing(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(403, text="<HTML>Access Denied</HTML>"))
        assert await TreasuryProvider().curve_year(NOMINAL, 2026) is None
        assert fake_network.calls(URL_PART) == 1

    async def test_a_server_error_is_tried_again(self, fake_network):
        fake_network.get(
            URL_PART,
            httpx.Response(503, text="busy"),
            httpx.Response(200, text=fixture("nominal_1990.csv")),
        )
        curve_file = await TreasuryProvider().curve_year(NOMINAL, 1990)
        assert curve_file.days[0].day == date(1990, 1, 2)
        assert fake_network.calls(URL_PART) == 2

    async def test_a_page_that_is_not_a_file(self, fake_network):
        fake_network.get(
            URL_PART, httpx.Response(200, text="<html><body>Maintenance</body></html>")
        )
        with pytest.raises(TreasuryError, match="does not start with a Date column"):
            await TreasuryProvider().curve_year(NOMINAL, 2026)

    async def test_an_unknown_curve_is_not_asked(self, fake_network):
        with pytest.raises(TreasuryError):
            await TreasuryProvider().curve_year("zero", 2026)
        assert fake_network.requests == []

    def test_the_first_years(self):
        assert (CURVES[NOMINAL].first_year, CURVES[REAL].first_year) == (1990, 2003)
