"""The 3-month and 10-year interest rates of the OECD (``financials/providers/oecd.py``).

``tests/fixtures/oecd/ir3tib_monthly.csv`` and ``irlt_monthly.csv`` are reduced
real answers (see their README). No test reaches the OECD.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financials.providers.oecd import (
    CSV_MEDIA_TYPE,
    LONG_RATE,
    SHORT_RATE,
    OECDError,
    OECDProvider,
    ShortRate,
    parse_rates_csv,
    parse_short_rates_csv,
    rates_url,
    short_rates_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "oecd" / "ir3tib_monthly.csv"
URL_PART = "DSD_STES@DF_FINMARK,4.0/.M.IR3TIB.PA."
LONG_URL_PART = "DSD_STES@DF_FINMARK,4.0/.M.IRLT.PA."
LONG_FIXTURE = Path(__file__).parent / "fixtures" / "oecd" / "irlt_monthly.csv"
HEADER = (
    "DATAFLOW,REF_AREA,FREQ,MEASURE,UNIT_MEASURE,ACTIVITY,ADJUSTMENT,TRANSFORMATION,"
    "TIME_HORIZ,METHODOLOGY,TIME_PERIOD,OBS_VALUE,OBS_STATUS,UNIT_MULT,DECIMALS,BASE_PER"
)
FLOW = "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)"


def answer() -> str:
    # Read as bytes: the file is kept exactly as the OECD sent it (\r\n line ends).
    return FIXTURE.read_bytes().decode("utf-8")


def long_answer() -> str:
    return LONG_FIXTURE.read_bytes().decode("utf-8")


def row(area="CHE", period="2026-10", value="1.5", freq="M", measure="IR3TIB", unit="PA", mult="0"):
    return f"{FLOW},{area},{freq},{measure},{unit},_Z,_Z,_Z,_Z,N,{period},{value},A,{mult},2,"


def by_area(rates: list[ShortRate]) -> dict[str, dict[str, Decimal]]:
    found: dict[str, dict[str, Decimal]] = {}
    for rate in rates:
        found.setdefault(rate.area, {})[f"{rate.period:%Y-%m}"] = rate.value
    return found


class TestRealAnswer:
    def test_the_areas_and_their_months(self):
        rates = by_area(parse_short_rates_csv(answer()))

        assert sorted(rates) == ["CHE", "DNK", "EA20", "GBR", "JPN", "NOR", "POL", "SWE"]
        assert {area: (min(months), max(months)) for area, months in rates.items()} == {
            "CHE": ("2026-07", "2026-10"),
            "DNK": ("2026-07", "2026-10"),
            "EA20": ("2026-07", "2026-10"),
            "GBR": ("2025-12", "2026-02"),
            "JPN": ("2026-07", "2026-09"),
            "NOR": ("2026-07", "2026-10"),
            "POL": ("2026-07", "2026-10"),
            "SWE": ("2026-07", "2026-10"),
        }

    def test_rates_are_ratios(self):
        rates = by_area(parse_short_rates_csv(answer()))

        assert rates["EA20"]["2026-10"] == Decimal("0.02634682")
        assert rates["GBR"]["2026-02"] == Decimal("0.0362")
        assert rates["SWE"]["2026-10"] == Decimal("0.0202864")
        # The Swiss rate is negative: a value, not an error.
        assert rates["CHE"]["2026-10"] == Decimal("-0.000415")

    def test_by_area_then_month_with_the_status_of_the_source(self):
        rates = parse_short_rates_csv(answer())

        assert [(rate.area, rate.period) for rate in rates] == sorted(
            (rate.area, rate.period) for rate in rates
        )
        assert rates[0] == ShortRate("CHE", date(2026, 7, 1), Decimal("-0.0004489131"), "A")
        assert len(rates) == 30


class TestAnswersThatCannotBeRead:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", "no column REF_AREA"),
            ("<html><body>Maintenance</body></html>", "no column REF_AREA"),
            ("NoRecordsFound", "no column"),
            (HEADER.replace("OBS_VALUE", "VALUE") + "\n" + row(), "no column OBS_VALUE"),
            (HEADER + "\n" + row() + ",extra", "17 cells for 16 columns"),
            (HEADER + "\n" + row(freq="Q"), "Q/IR3TIB/PA is not a monthly 3-month rate"),
            (HEADER + "\n" + row(measure="IRLT"), "M/IRLT/PA is not a monthly 3-month rate"),
            (HEADER + "\n" + row(unit="PC"), "M/IR3TIB/PC is not a monthly 3-month rate"),
            (HEADER + "\n" + row(mult="3"), "unit multiplier '3'"),
            (HEADER + "\n" + row(area=""), "no reference area"),
            (HEADER + "\n" + row(period="2026-Q3"), "'2026-Q3' is not a month"),
            (HEADER + "\n" + row(period="2026-13"), "'2026-13' is not a month"),
            (HEADER + "\n" + row(value="n/a"), "'n/a' is not a rate"),
            (HEADER + "\n" + row(value="250"), "'250' is not a rate in percent"),
            (HEADER + "\n" + row(value="NaN"), "'NaN' is not a rate in percent"),
            (HEADER + "\n" + row() + "\n" + row(value="1.6"), "CHE 2026-10 is given twice"),
        ],
    )
    def test_nothing_is_kept(self, text, message):
        with pytest.raises(OECDError, match=message):
            parse_short_rates_csv(text)

    def test_a_byte_order_mark_a_blank_line_and_a_month_without_value(self):
        text = "﻿" + HEADER + "\n\n" + row(value="") + "\n" + row(area="NOR", value="4.78")
        assert parse_short_rates_csv(text) == [
            ShortRate("NOR", date(2026, 10, 1), Decimal("0.0478"), "A")
        ]

    def test_an_answer_without_status_column(self):
        header = "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE"
        rates = parse_short_rates_csv(header + "\nEA20,M,IR3TIB,PA,2026-10,2.5")
        assert rates == [ShortRate("EA20", date(2026, 10, 1), Decimal("0.025"))]


class TestProvider:
    async def test_every_country_in_one_request(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=answer()))

        rates = await OECDProvider().short_rates(date(2026, 7, 15))

        assert len(rates) == 30
        assert fake_network.urls() == [
            short_rates_url() + "?startPeriod=2026-07&dimensionAtObservation=AllDimensions"
        ]
        assert fake_network.requests[0].headers["Accept"] == CSV_MEDIA_TYPE

    def test_the_address(self):
        assert short_rates_url() == (
            "https://sdmx.oecd.org/public/rest/data/"
            "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/.M.IR3TIB.PA._Z._Z._Z._Z.N"
        )

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "3600"}),
            httpx.Response(503, text="busy"),
            httpx.Response(404, text="NoRecordsFound"),
        ],
    )
    async def test_a_refusal_is_not_asked_again(self, fake_network, response):
        # About 60 downloads an hour: asking again would only spend the allowance.
        fake_network.get(URL_PART, response, httpx.Response(200, text=answer()))

        assert await OECDProvider().short_rates(date(2026, 7, 1)) is None
        assert fake_network.calls(URL_PART) == 1
        assert fake_network.backoffs == []

    async def test_a_page_that_is_not_an_answer(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text="<html>Maintenance</html>"))
        with pytest.raises(OECDError, match="no column REF_AREA"):
            await OECDProvider().short_rates(date(2026, 7, 1))


class TestRealTenYearAnswer:
    def test_the_areas_and_their_months(self):
        rates = by_area(parse_rates_csv(long_answer(), LONG_RATE))

        # GBR goes on for the 10-year rate, unlike its 3-month rate; CHN is a month behind.
        assert sorted(rates) == ["CHE", "CHN", "DEU", "EA20", "GBR", "JPN", "SWE", "USA"]
        assert {area: (min(months), max(months)) for area, months in rates.items()} == {
            area: ("2026-07", "2026-09" if area == "CHN" else "2026-10") for area in rates
        }

    def test_rates_are_ratios(self):
        rates = by_area(parse_rates_csv(long_answer(), LONG_RATE))

        # EA20 is a mean of the whole euro area, above the German yield.
        assert rates["EA20"]["2026-10"] == Decimal("0.04173788")
        assert rates["DEU"]["2026-10"] == Decimal("0.03415909")
        assert rates["GBR"]["2026-10"] == Decimal("0.053393")
        assert rates["CHE"]["2026-10"] == Decimal("0.0051")
        assert rates["USA"]["2026-10"] == Decimal("0.0513")
        assert rates["CHN"]["2026-09"] == Decimal("0.0168")

    def test_by_area_then_month(self):
        rates = parse_rates_csv(long_answer(), LONG_RATE)

        assert [(rate.area, rate.period) for rate in rates] == sorted(
            (rate.area, rate.period) for rate in rates
        )
        assert rates[0] == ShortRate("CHE", date(2026, 7, 1), Decimal("0.0048"), "A")
        assert len(rates) == 31

    def test_a_10_year_answer_is_not_a_3_month_answer(self):
        with pytest.raises(OECDError, match="line 2: M/IRLT/PA is not a monthly 3-month rate"):
            parse_short_rates_csv(long_answer())


class TestTenYearRates:
    """The 10-year government bond yields (``IRLT``): the same dataflow, another measure."""

    def test_the_address(self):
        assert rates_url(LONG_RATE) == (
            "https://sdmx.oecd.org/public/rest/data/"
            "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/.M.IRLT.PA._Z._Z._Z._Z.N"
        )
        assert rates_url(SHORT_RATE) == short_rates_url()

    def test_a_10_year_row_is_read_as_a_ratio(self):
        text = HEADER + "\n" + row(area="DEU", measure="IRLT", value="3.42")
        assert parse_rates_csv(text, LONG_RATE) == [
            ShortRate("DEU", date(2026, 10, 1), Decimal("0.0342"), "A")
        ]

    def test_a_3_month_row_in_a_10_year_answer_is_refused(self):
        text = HEADER + "\n" + row(measure="IRLT") + "\n" + row(area="SWE", measure="IR3TIB")
        with pytest.raises(OECDError, match="line 3: M/IR3TIB/PA is not a monthly 10-year rate"):
            parse_rates_csv(text, LONG_RATE)

    @pytest.mark.parametrize("call", [rates_url, lambda measure: parse_rates_csv("", measure)])
    def test_an_unknown_measure_is_refused(self, call):
        with pytest.raises(ValueError, match="'IRSTCI' is not a rate of the OECD"):
            call("IRSTCI")

    async def test_every_country_in_one_request(self, fake_network):
        fake_network.get(LONG_URL_PART, httpx.Response(200, text=long_answer()))

        rates = await OECDProvider().long_rates(date(2026, 7, 15))

        assert len(rates) == 31
        assert fake_network.urls() == [
            rates_url(LONG_RATE) + "?startPeriod=2026-07&dimensionAtObservation=AllDimensions"
        ]
        assert fake_network.requests[0].headers["Accept"] == CSV_MEDIA_TYPE

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "3600"}),
            httpx.Response(503, text="busy"),
            httpx.Response(404, text="NoRecordsFound"),
        ],
    )
    async def test_a_refusal_is_not_asked_again(self, fake_network, response):
        fake_network.get(LONG_URL_PART, response, httpx.Response(200, text=HEADER))

        assert await OECDProvider().long_rates(date(2026, 7, 1)) is None
        assert fake_network.calls(LONG_URL_PART) == 1
        assert fake_network.backoffs == []

    async def test_a_3_month_answer_to_a_10_year_request_is_refused(self, fake_network):
        fake_network.get(LONG_URL_PART, httpx.Response(200, text=answer()))
        with pytest.raises(OECDError, match="is not a monthly 10-year rate"):
            await OECDProvider().long_rates(date(2026, 7, 1))
