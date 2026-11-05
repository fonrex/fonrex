"""Edge cases of the provider parsing helpers, beyond what the real pages cover."""

import pytest
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.Barrons_provider import BarronsProvider
from financials.providers.boursorama_provider import BoursoramaProvider
from financials.providers.GoogleFinance_provider import GoogleFinanceProvider
from financials.providers.Investing_provider import InvestingProvider
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


class TestSignAndThousandsAreKept:
    """Figures the providers used to read wrongly, each with its own search for digits.

    A loss is reported as a profit when the sign is dropped; ``1,234.5`` read up
    to its first separator gives ``1.0``.
    """

    def test_barrons_loss_per_share(self):
        html = page("<div><p>EPS</p><p>-$2.35</p></div><div><p>P/E Ratio</p><p>1,234.5</p></div>")
        metrics = BarronsProvider()._parse_page(html, "TST")

        assert metrics.eps == pytest.approx(-2.35)
        assert metrics.pe_ratio == pytest.approx(1234.5)

    def test_barrons_accounting_notation(self):
        html = page("<div><p>EPS</p><p>(2.35)</p></div>")

        assert BarronsProvider()._parse_page(html, "TST").eps == pytest.approx(-2.35)

    def test_marketwatch_loss_per_share(self):
        item = (
            '<ul><li class="kv__item"><small class="label">EPS</small>'
            '<span class="primary">-$1,002.35</span></li></ul>'
        )

        assert MarketwatchProvider()._find_value_by_label(page(item), ["EPS"]) == pytest.approx(
            -1002.35
        )

    def test_investing_loss_per_share(self):
        html = page(
            "<h1>Test SA</h1><dl><dt>EPS</dt><dd>-2.35</dd><dt>P/E Ratio</dt><dd>1,234.5</dd>"
            "<dt>Dividend Yield</dt><dd>1.5%</dd><dt>Beta</dt><dd>1.1</dd>"
            "<dt>Next Earnings Date</dt><dd>N/A</dd></dl>"
        )
        metrics = InvestingProvider()._parse_page(html, "TST")

        assert metrics.eps == pytest.approx(-2.35)
        assert metrics.pe_ratio == pytest.approx(1234.5)
        assert metrics.dividend_yield == pytest.approx(1.5)

    @pytest.mark.parametrize("text", ["Débloquer", "Unlock Pro+ (7 days)", "Q3 2026", "N/A"])
    def test_investing_text_in_a_figure_cell_is_no_figure(self, text):
        html = page(
            f"<h1>Test SA</h1><dl><dt>P/E Ratio</dt><dd>{text}</dd><dt>EPS</dt><dd>2.1</dd></dl>"
        )

        metrics = InvestingProvider()._parse_page(html, "TST")

        assert metrics.pe_ratio is None
        assert metrics.eps == pytest.approx(2.1)

    def test_investing_accented_range_label(self):
        html = HTMLParser(
            "<html lang='fr'><body><dl><dt>Écart 52 sem.</dt><dd>297,38-498,83</dd></dl></body></html>"
        )

        metrics = InvestingProvider()._parse_page(html, "TST")

        assert (metrics.week_52_low, metrics.week_52_high) == (297.38, 498.83)

    def test_zonebourse_typographic_minus_and_narrow_space(self):
        assert ZoneBourseProvider()._clean_number("\u22121\u202f234,5 M") == pytest.approx(
            -1_234_500_000
        )

    @pytest.mark.parametrize("text", ["MXN 12.5", "BRL 12.5", "KRW 12.5"])
    def test_google_currency_code_is_not_a_scale(self, text):
        assert GoogleFinanceProvider()._clean_number(text) == 12.5

    def test_google_scale_and_percentage(self):
        clean = GoogleFinanceProvider()._clean_number
        assert clean("102.47B") == pytest.approx(102_470_000_000)
        assert clean("-1.2B") == pytest.approx(-1_200_000_000)
        assert clean("5.67%") == pytest.approx(5.67)
        assert clean("1,234.56") == pytest.approx(1234.56)


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

    def test_barrons_next_data_and_dylan_and_fallback_parsing(self):
        import json

        from financials.models import FinancialMetrics

        next_data = {
            "props": {
                "pageProps": {
                    "titleCard": {
                        "companyName": "Credit Agricole S.A.",
                        "exchange": "France: Euronext Paris",
                    },
                    "keyDataCard": {
                        "dataPoints": [
                            {"key": "P/E Ratio", "value": {"primaryValue": "7.62"}},
                            {"key": "EPS", "value": {"primaryValue": "2.30"}},
                            {"key": "Yield", "value": {"primaryValue": "0.32%"}},
                            {"key": "Dividend", "value": {"primaryValue": "1.10"}},
                            {"key": "Market Cap", "value": {"primaryValue": "52,770,000,000"}},
                            {
                                "key": "Shares Outstanding",
                                "value": {"primaryValue": "3,010,000,000"},
                            },
                            {"key": "Public Float", "value": {"primaryValue": "1,070,000,000"}},
                            {"key": "Beta", "value": {"primaryValue": "0.87"}},
                            {"key": "Short Interest", "value": {"primaryValue": "9,670,000"}},
                            {"key": "% of Float Shorted", "value": {"primaryValue": "0.90%"}},
                            {"key": "Average Volume", "value": {"primaryValue": "5,190,000"}},
                            {"key": "52 Week Range", "value": {"primaryValue": "15.28 - 19.30"}},
                        ]
                    },
                    "yearRangeCard": {
                        "low": {"value": 15.28},
                        "high": {"value": 19.30},
                        "dayCurrent": {"value": 17.55},
                    },
                    "volumeCard": {
                        "averageVolumeForPeriod": {"value": 5190000},
                    },
                }
            }
        }
        html = page(
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
            "<div><p>52 Wk Range</p><p>N/A</p></div>"
            "<div><span>Beta</span><span>0.87</span></div>"
        )
        provider = BarronsProvider()
        metrics = provider._parse_page(html, "ACA")
        assert metrics.name == "Credit Agricole S.A."
        assert metrics.exchange_name == "France: Euronext Paris"
        assert metrics.price == pytest.approx(17.55)
        assert metrics.pe_ratio == pytest.approx(7.62)
        assert metrics.eps == pytest.approx(2.30)
        assert metrics.dividend_yield == pytest.approx(0.32)
        assert metrics.dividend_rate == pytest.approx(1.10)
        assert metrics.market_cap == pytest.approx(52_770_000_000.0)
        assert metrics.shares_outstanding == pytest.approx(3_010_000_000.0)
        assert metrics.float_shares == pytest.approx(1_070_000_000.0)
        assert metrics.beta == pytest.approx(0.87)
        assert metrics.shares_short == pytest.approx(9_670_000.0)
        assert metrics.short_percent_float == pytest.approx(0.90)
        assert metrics.average_volume == pytest.approx(5_190_000.0)
        assert metrics.week_52_low == pytest.approx(15.28)
        assert metrics.week_52_high == pytest.approx(19.30)

        bad_json_html = page('<script id="__NEXT_DATA__">not json</script>')
        assert provider._parse_page(bad_json_html, "ACA").pe_ratio is None

        assert provider.extract_entitlement_token("") is None
        assert provider.extract_entitlement_token("<html>no token</html>") is None
        assert (
            provider.extract_entitlement_token(
                'entitlementToken:"c1b7ca756343461b86565b61bbd2ac6f"'
            )
            == "c1b7ca756343461b86565b61bbd2ac6f"
        )

        m = FinancialMetrics(ticker="ACA")
        provider.read_dylan_quote(m, {})
        provider.read_dylan_quote(m, {"InstrumentResponses": [{}]})
        provider.read_dylan_match(m, {})
        provider.read_dylan_quote(
            m,
            {
                "InstrumentResponses": [
                    {
                        "Matches": [
                            {
                                "Trading": {"Last": {"Price": {"Iso": "EUR", "Value": 17.545}}},
                                "Financials": {"AnnualizedDividend": 1.1},
                                "TradingStatistics": {
                                    "PriceStatistics": [
                                        {"Range": {"Duration": {"ValueString": "P50D"}}},
                                    ],
                                    "VolumeStatistics": [
                                        {"Range": {"Duration": {"ValueString": "P50D"}}},
                                    ],
                                },
                            }
                        ]
                    }
                ]
            },
        )
        assert m.price == pytest.approx(17.545)
        assert m.currency == "EUR"
        assert m.dividend_rate == pytest.approx(1.1)

    def test_range_and_sibling_fallback_and_dylan_edge_cases(self):
        from financials.models import FinancialMetrics

        html = page(
            '<ul class="list list--kv">'
            '<li class="kv__item"><small class="label">52 Week Range</small>'
            '<span class="primary">N/A</span></li>'
            "</ul>"
            "<div><span>P/E Ratio</span><span>18.5</span></div>"
            "<div>Market Cap: $1.58T</div>"
            "<div>Ex-Dividend Date: Nov 5, 2026</div>"
            "<div>" + ("x" * 60) + "</div>"
        )
        provider = MarketwatchProvider()
        metrics = provider._parse_page(html, "TST")
        assert metrics.pe_ratio == pytest.approx(18.5)
        assert metrics.market_cap == pytest.approx(1.58e12)
        assert getattr(metrics, "dividend_rate", None) is None
        assert getattr(metrics, "week_52_low", None) is None
        assert provider._find_value_by_label(html, ["Dividend"]) is None

        assert provider.extract_entitlement_token("") is None
        assert provider.extract_entitlement_token("<html>no token</html>") is None

        m = FinancialMetrics(ticker="TST")
        provider.read_dylan_quote(m, {})
        provider.read_dylan_quote(m, {"InstrumentResponses": [{}]})
        provider.read_dylan_quote(
            m,
            {
                "InstrumentResponses": [
                    {
                        "Matches": [
                            {
                                "Trading": {"Last": {"Price": {"Iso": "EUR", "Value": 12.34}}},
                                "Financials": {
                                    "LastDividendPerShare": {"Iso": "EUR", "Value": 0.25},
                                },
                                "TradingStatistics": {
                                    "PriceStatistics": [
                                        {"Range": {"Duration": {"ValueString": "P50D"}}},
                                    ],
                                    "VolumeStatistics": [
                                        {"Range": {"Duration": {"ValueString": "P50D"}}},
                                    ],
                                },
                            }
                        ]
                    }
                ]
            },
        )
        assert m.price == pytest.approx(12.34)
        assert m.currency == "EUR"
        assert getattr(m, "dividend_rate", None) is None

        provider.read_dylan_quote(
            m,
            {
                "InstrumentResponses": [
                    {
                        "Matches": [
                            {
                                "Financials": {
                                    "AnnualizedDividend": {"Iso": "EUR", "Value": 1.00},
                                }
                            }
                        ]
                    }
                ]
            },
        )
        assert m.dividend_rate == pytest.approx(1.00)


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
            ("80,95 Md", 80_950_000_000),
            ("456,7 Mio", 456_700_000),
            ("3 k", 3_000),
        ],
    )
    def test_clean_number(self, text, expected):
        assert BoursoramaProvider()._clean_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "N/A", "-", "EUR", "80,95 Inconnu"])
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


class TestMsnQuoteSummary:
    @staticmethod
    def read(entry: dict) -> dict:
        metrics = FinancialMetrics(ticker="AIR")
        MsnProvider.read_quote_summary(metrics, entry)
        return metrics.model_dump(exclude_none=True)

    def test_name_of_the_quote_has_priority(self):
        parsed = self.read(
            {"quote": {"displayName": "Airbus SE"}, "equity": {"displayName": "Other"}}
        )

        assert parsed["name"] == "Airbus SE"

    def test_name_of_the_equity_when_the_quote_has_none(self):
        parsed = self.read({"quote": {"displayName": " "}, "equity": {"displayName": "Airbus"}})

        assert parsed["name"] == "Airbus"

    def test_name_of_the_company_when_no_listing_name(self):
        parsed = self.read({"quote": {}, "equity": {"company": {"name": "Airbus SE"}}})

        assert parsed["name"] == "Airbus SE"

    def test_zero_ratio_is_a_missing_value(self):
        parsed = self.read(
            {
                "quote": {"peRatio": 0.0, "yieldPercent": 0.0},
                "equity": {"analysis": {"keyMetrics": {"priceToBookRatio": 0, "eps": -1.2}}},
            }
        )

        assert "pe_ratio" not in parsed
        assert "pb_ratio" not in parsed
        # A share without dividend has a real zero yield.
        assert parsed["dividend_yield"] == 0.0
        assert parsed["eps"] == -1.2

    def test_unexpected_shapes_give_no_figure(self):
        parsed = self.read({"quote": "?", "equity": {"analysis": [], "company": None}})

        assert parsed == {"ticker": "AIR"}
