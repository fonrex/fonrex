"""Edge cases of the provider parsing helpers, beyond what the real pages cover."""

import pytest
from selectolax.parser import HTMLParser

from financials.providers.boursorama_provider import BoursoramaProvider
from financials.providers.GoogleFinance_provider import GoogleFinanceProvider
from financials.providers.Marketwatch_provider import MarketwatchProvider
from financials.providers.Msn_provider import MsnProvider
from financials.providers.ZoneBourse_provider import ZoneBourseProvider


def page(body: str, head: str = "") -> HTMLParser:
    return HTMLParser(f"<html><head>{head}</head><body>{body}</body></html>")


class TestZoneBourseNumbers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("80,95 Md", 80_950_000_000),
            ("1,2 Mds", 1_200_000_000),
            ("12 M", 12_000_000),
            ("3 k", 3_000),
            ("25,5x", 25.5),
            ("1,81 %", 1.81),
            ("80 945 151 250", 80_945_151_250),
            ("-12,96 Md", -12_960_000_000),
        ],
    )
    def test_clean_number(self, text, expected):
        assert ZoneBourseProvider()._clean_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "-", "Md", "n.a."])
    def test_clean_number_without_figure(self, text):
        assert ZoneBourseProvider()._clean_number(text) is None

    def test_visible_currency_is_read_from_its_exact_title(self):
        cell = page(
            "<table><tr><th>"
            '<span class="efd_USD c-none"><span title="91 103 152 786"> 91,1 Md </span></span>'
            '<span class="efd_EUR "><span title="80 945 151 250"> 80,95 Md </span></span>'
            "</th></tr></table>"
        ).css_first("th")

        assert ZoneBourseProvider()._cell_number(cell) == pytest.approx(80_945_151_250)

    def test_visible_currency_without_title_uses_the_abbreviated_amount(self):
        cell = page(
            '<table><tr><th><span class="efd_EUR "><span> 80,95 Md </span></span></th></tr></table>'
        ).css_first("th")

        assert ZoneBourseProvider()._cell_number(cell) == pytest.approx(80_950_000_000)


class TestZoneBourseRows:
    def _parse(self, table_rows: str):
        html = page(f"<h1>Cours Test SA</h1><table>{table_rows}</table>")
        return ZoneBourseProvider()._parse_page(html, "TST", "https://example.test/page")

    def test_label_in_header_cell(self):
        metrics = self._parse("<tr><th>PER</th><td>12,5x</td><td>11,0x</td></tr>")

        assert metrics.pe_ratio == pytest.approx(12.5)

    def test_label_in_first_data_cell(self):
        metrics = self._parse("<tr><td>Rendement</td><td>3,4 %</td></tr>")

        assert metrics.dividend_yield == pytest.approx(3.4)

    def test_several_pairs_on_one_row_and_first_estimate_wins(self):
        metrics = self._parse(
            "<tr><td>PER 2026 *</td><th>25,5x</th><td>PER 2027 *</td><th>21,4x</th>"
            "<td>Rendement 2026 *</td><th>1,81 %</th></tr>"
        )

        assert metrics.pe_ratio == pytest.approx(25.5)
        assert metrics.dividend_yield == pytest.approx(1.81)

    def test_label_containing_per_is_not_a_pe_ratio(self):
        metrics = self._parse("<tr><td>Performance 1 an</td><td>50,7 %</td></tr>")

        assert metrics.pe_ratio is None

    def test_heading_prefix_is_removed_from_the_name(self):
        assert self._parse("").name == "Test SA"


class TestMarketwatchKeyData:
    def _find(self, items: str, labels: list[str]):
        html = page(f'<ul class="list list--kv">{items}</ul>')
        return MarketwatchProvider()._find_value_by_label(html, labels)

    def test_value_with_thousands_separator(self):
        item = (
            '<li class="kv__item"><small class="label">EPS</small>'
            '<span class="primary">$1,234.50</span></li>'
        )

        assert self._find(item, ["EPS"]) == pytest.approx(1234.5)

    def test_missing_value_is_none(self):
        item = (
            '<li class="kv__item"><small class="label">Yield</small>'
            '<span class="primary">N/A</span></li>'
        )

        assert self._find(item, ["Yield", "Dividend Yield"]) is None

    def test_label_must_match_exactly(self):
        item = (
            '<li class="kv__item"><small class="label">Ex-Dividend Date</small>'
            '<span class="primary">Aug 10, 2026</span></li>'
        )

        assert self._find(item, ["Dividend"]) is None


class TestBoursoramaHelpers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("6,89 EUR", 6.89),
            ("1,49%", 1.49),
            ("1 234,56", 1234.56),
            ("1 234,56", 1234.56),
            ("+1,5%", 1.5),
            ("-0,25", -0.25),
            ("31,27", 31.27),
        ],
    )
    def test_clean_number(self, text, expected):
        assert BoursoramaProvider()._clean_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "N/A", "-", "EUR"])
    def test_clean_number_without_figure(self, text):
        assert BoursoramaProvider()._clean_number(text) is None

    def test_isin_without_ticker_is_kept(self):
        html = page('<h2 class="c-faceplate__isin">ISIN : FR0000120271</h2>')

        assert BoursoramaProvider()._parse_page(html, "TTE").isin == "FR0000120271"

    def test_unrecognised_isin_heading_is_returned_as_is(self):
        html = page('<h2 class="c-faceplate__isin">indisponible</h2>')

        assert BoursoramaProvider()._parse_page(html, "TTE").isin == "indisponible"

    def test_eligibility_fallback_ignores_navigation_menus(self):
        html = page(
            '<ul><li class="c-navigation-horizontal__list-item">Présentation PEA-PME</li></ul>'
            "<ul><li>PEA</li><li>SRD</li><li>PEA</li></ul>"
        )

        assert BoursoramaProvider()._extract_eligibility(html) == ["PEA", "SRD"]


class TestGoogleFinanceName:
    def test_name_from_title(self):
        html = page(
            "<h1>Finance</h1>",
            head="<title>TotalEnergies SE (TTE) Stock Price &amp; News - Google Finance</title>",
        )

        assert GoogleFinanceProvider._extract_name(html) == "TotalEnergies SE"

    def test_product_heading_is_not_a_company_name(self):
        assert GoogleFinanceProvider._extract_name(page("<h1>Finance</h1>")) is None

    def test_heading_fallback(self):
        assert GoogleFinanceProvider._extract_name(page("<h1>Airbus SE</h1>")) == "Airbus SE"


class TestMsnName:
    def test_heading_has_priority(self):
        html = page("<h1>Airbus SE</h1>", head="<title>AIR : Other - MSN Finances</title>")

        assert MsnProvider()._parse_page(html, "AIR").name == "Airbus SE"

    def test_unrelated_title_gives_no_name(self):
        html = page("", head="<title>MSN</title>")

        parsed = MsnProvider()._parse_page(html, "AIR").model_dump(exclude_none=True)

        assert "name" not in parsed
