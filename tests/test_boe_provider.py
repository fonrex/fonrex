"""The monthly SONIA of the Bank of England (``financials/providers/boe.py``).

``tests/fixtures/boe/`` keeps real answers of the Bank of England (see its README).
No test reaches the Bank of England.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from financials.providers.boe import (
    AREA,
    BOE_API_URL,
    SERIES,
    BankOfEnglandProvider,
    BoEError,
    parse_sonia_csv,
    sonia_params,
)
from financials.providers.oecd import ShortRate

FIXTURES = Path(__file__).parent / "fixtures" / "boe"
URL_PART = "_iadb-fromshowcolumns.asp"
HEADER = "DATE,IUMASOIA"


def fixture(name: str) -> str:
    # Read as bytes: the files are kept exactly as the Bank sent them (\r\n line ends).
    return (FIXTURES / name).read_bytes().decode("utf-8")


class TestRealAnswer:
    def test_every_month_since_1997(self):
        rates = parse_sonia_csv(fixture("sonia_monthly.csv"))

        assert len(rates) == 358  # January 1997 to October 2026
        assert rates[0] == ShortRate("GBR", date(1997, 1, 1), Decimal("0.059005"))
        assert rates[-1] == ShortRate("GBR", date(2026, 10, 1), Decimal("0.037333"))
        assert [rate.period for rate in rates] == sorted(rate.period for rate in rates)
        assert {rate.area for rate in rates} == {AREA}

    def test_a_month_dated_by_its_last_day_is_dated_by_its_first(self):
        rates = {rate.period: rate.value for rate in parse_sonia_csv(fixture("sonia_monthly.csv"))}

        # "29 Feb 2024,5.1883": a leap year, kept as February.
        assert rates[date(2024, 2, 1)] == Decimal("0.051883")
        # The months after the last 3-month rate of the OECD for the United Kingdom.
        assert rates[date(2026, 3, 1)] == Decimal("0.037289")

    def test_a_period_without_value(self):
        assert parse_sonia_csv(fixture("empty_period.csv")) == []

    def test_an_unknown_series_answers_an_error_page(self):
        with pytest.raises(BoEError, match="not the CSV of IUMASOIA"):
            parse_sonia_csv(fixture("error_unknown_series.html"))


class TestAnswersThatCannotBeRead:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", "not the CSV of IUMASOIA"),
            ("DATE,IUDSOIA\r\n31 Oct 2026,3.98", "not the CSV of IUMASOIA"),
            (HEADER + "\r\n31 Oct 2026,3.7,1", "3 cells for 2 columns"),
            (HEADER + "\r\n2026-10-31,3.7", "'2026-10-31' is not a day"),
            (HEADER + "\r\n31 Oct 2026,n/a", "'n/a' is not a rate"),
            (HEADER + "\r\n31 Oct 2026,370", "'370' is not a rate in percent"),
            (HEADER + "\r\n31 Oct 2026,NaN", "'NaN' is not a rate in percent"),
            (HEADER + "\r\n31 Oct 2026,3.7\r\n30 Oct 2026,3.8", "2026-10 is given twice"),
        ],
    )
    def test_nothing_is_kept(self, text, message):
        with pytest.raises(BoEError, match=message):
            parse_sonia_csv(text)

    def test_a_byte_order_mark_a_blank_line_and_a_month_without_value(self):
        text = "﻿" + HEADER + "\r\n\r\n30 Sep 2026,\r\n31 Oct 2026,3.7333\r\n"
        assert parse_sonia_csv(text) == [ShortRate("GBR", date(2026, 10, 1), Decimal("0.037333"))]


class TestProvider:
    async def test_the_months_from_a_day(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=fixture("sonia_monthly.csv")))

        rates = await BankOfEnglandProvider().sonia_monthly(date(1990, 1, 15))

        assert len(rates) == 358
        (url,) = fake_network.urls()
        assert url.startswith(BOE_API_URL + "?")
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        assert query == sonia_params(date(1990, 1, 1))
        assert query["Datefrom"] == "01/Jan/1990"
        assert query["SeriesCodes"] == SERIES

    async def test_no_answer(self, fake_network):
        fake_network.get(URL_PART, *(httpx.Response(503, text="busy") for _ in range(3)))
        assert await BankOfEnglandProvider().sonia_monthly(date(2026, 1, 1)) is None

    async def test_an_error_page(self, fake_network):
        fake_network.get(URL_PART, httpx.Response(200, text=fixture("error_unknown_series.html")))
        with pytest.raises(BoEError, match="not the CSV"):
            await BankOfEnglandProvider().sonia_monthly(date(2026, 1, 1))
