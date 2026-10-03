"""JustETFScraper (financials/providers/JustETF_provider.py).

The page fixtures in ``tests/fixtures/justetf_scraper`` are extracts of real profile
pages, reduced to what the scraper reads (see tests/fixtures/providers/README.md).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup
from lxml import etree

from financials.providers import JustETF_provider
from financials.providers.JustETF_provider import (
    ETF_PROFILE_URL,
    JustETFScraper,
    justETFScraping,
    justETFWebScraping,
)

FIXTURES = Path(__file__).parent / "fixtures" / "justetf_scraper"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
ACC_ISIN = "IE00B4L5Y983"
DIST_ISIN = "IE00B0M62Q58"


def page(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


class FakeFetch:
    """Stands in for the HTTP call and records the requested URLs."""

    def __init__(self, text: str = "", status_code: int = 200, error: Exception | None = None):
        self.response = SimpleNamespace(status_code=status_code, text=text)
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.response


def header(testid: str, text: str) -> etree._Element:
    return etree.HTML(f'<html><body><div data-testid="{testid}">{text}</div></body></html>')


class TestRealPages:
    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_profile_page_is_read(self, name):
        case = EXPECTED[name]
        fetch = FakeFetch(page(name))

        data = JustETFScraper(fetch).extract_etf_data(case["isin"])

        url = data.pop("justETF_url")
        assert data == case["expected"]
        assert url == fetch.urls[0]

    def test_french_and_english_pages_give_the_same_figures(self):
        english = EXPECTED["acc_en"]["expected"]
        french = EXPECTED["acc_fr"]["expected"]

        for field in ("fees_etf", "holdingCount_etf", "fundSizeMillions_etf", "dividendsPolicy_etf"):
            assert french[field] == english[field], field

    def test_expected_values_are_the_ones_displayed(self):
        accumulating = EXPECTED["acc_en"]["expected"]
        distributing = EXPECTED["dist_en"]["expected"]

        assert accumulating["long_name"] == "iShares Core MSCI World UCITS ETF USD (Acc)"
        assert accumulating["fees_etf"] == pytest.approx(0.20)
        assert accumulating["holdingCount_etf"] == 1252
        assert accumulating["fundSize_etf"] == "129 628 m"
        assert accumulating["fundSizeMillions_etf"] == 129628
        assert accumulating["fundSizeCurrency_etf"] == "EUR"
        assert accumulating["dividendsPolicy_etf"] == "Capitalisation"
        assert distributing["dividendsPolicy_etf"] == "Distribution"
        assert distributing["fees_etf"] == pytest.approx(0.50)
        assert accumulating["logo"].startswith("https://www.justetf.com/images/logo/")
        assert accumulating["justETF_pdf_url"].endswith(".pdf")


class TestWebScraping:
    def test_english_profile_url_is_requested(self):
        fetch = FakeFetch(page("acc_en"))

        JustETFScraper(fetch).extract_etf_data(ACC_ISIN)

        assert fetch.urls == [f"{ETF_PROFILE_URL}?isin={ACC_ISIN}"]
        assert "/en/" in fetch.urls[0]

    def test_isin_is_escaped_in_the_url(self):
        fetch = FakeFetch("", status_code=404)

        JustETFScraper(fetch).extract_etf_data("IE00 B4L5&x=1")

        assert fetch.urls == [f"{ETF_PROFILE_URL}?isin=IE00%20B4L5%26x%3D1"]

    def test_http_error_status_returns_only_the_isin(self):
        data = JustETFScraper(FakeFetch("blocked", status_code=403)).extract_etf_data(ACC_ISIN)

        assert data == {"isin": ACC_ISIN}

    def test_network_error_returns_only_the_isin(self):
        fetch = FakeFetch(error=httpx.ConnectError("unreachable"))

        assert JustETFScraper(fetch).extract_etf_data(ACC_ISIN) == {"isin": ACC_ISIN}

    def test_page_of_another_etf_is_rejected(self):
        data = JustETFScraper(FakeFetch(page("dist_en"))).extract_etf_data(ACC_ISIN)

        assert data == {"isin": ACC_ISIN}

    def test_page_without_profile_is_rejected(self):
        fetch = FakeFetch("<html><body><h1>ETF Screener</h1></body></html>")

        assert JustETFScraper(fetch).extract_etf_data(ACC_ISIN) == {"isin": ACC_ISIN}

    def test_missing_isin_does_not_trigger_a_request(self):
        fetch = FakeFetch(page("acc_en"))

        assert JustETFScraper(fetch).extract_etf_data("") == {"isin": ""}
        assert fetch.urls == []

    def test_default_client_is_used_when_none_is_given(self, monkeypatch):
        fetch = FakeFetch(page("acc_en"))
        monkeypatch.setattr(JustETF_provider, "getRequest", fetch)

        data = justETFWebScraping(ACC_ISIN)

        assert data["long_name"] == EXPECTED["acc_en"]["expected"]["long_name"]
        assert len(fetch.urls) == 1

    def test_default_client_sends_browser_headers_and_follows_redirects(self, monkeypatch):
        seen = {}

        class RecordingClient:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def get(self, url):
                seen["url"] = url
                return SimpleNamespace(status_code=200, text="")

        monkeypatch.setattr(JustETF_provider.httpx, "Client", RecordingClient)

        JustETF_provider.getRequest("https://example.test/page")

        assert seen["url"] == "https://example.test/page"
        assert seen["follow_redirects"] is True
        assert seen["timeout"] == JustETF_provider.REQUEST_TIMEOUT
        assert "Mozilla" in seen["headers"]["User-Agent"]


class TestLocalFiles:
    def test_matching_file_is_read_without_any_request(self, tmp_path):
        (tmp_path / f"{ACC_ISIN}.html").write_text(page("acc_en"), encoding="utf-8")
        fetch = FakeFetch(status_code=500)

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN, str(tmp_path))

        assert data == EXPECTED["acc_en"]["expected"]
        assert fetch.urls == []

    def test_backward_compatible_function(self, tmp_path):
        (tmp_path / f"justetf_{ACC_ISIN}.html").write_text(page("acc_fr"), encoding="utf-8")

        assert justETFScraping(ACC_ISIN, str(tmp_path)) == EXPECTED["acc_fr"]["expected"]

    def test_file_of_another_etf_falls_back_to_the_website(self, tmp_path):
        (tmp_path / f"{ACC_ISIN}.html").write_text(page("dist_en"), encoding="utf-8")
        fetch = FakeFetch(page("acc_en"))

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN, str(tmp_path))

        assert data["long_name"] == EXPECTED["acc_en"]["expected"]["long_name"]
        assert len(fetch.urls) == 1

    def test_rejected_file_leaves_no_partial_data(self, tmp_path):
        (tmp_path / f"{ACC_ISIN}.html").write_text(page("dist_en"), encoding="utf-8")

        data = JustETFScraper(FakeFetch(status_code=404)).extract_etf_data(ACC_ISIN, str(tmp_path))

        assert data == {"isin": ACC_ISIN}

    def test_unreadable_file_is_skipped(self, tmp_path):
        (tmp_path / f"a_{ACC_ISIN}.html").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
        (tmp_path / f"b_{ACC_ISIN}.html").write_text(page("acc_en"), encoding="utf-8")
        (tmp_path / f"c_{ACC_ISIN}").mkdir()

        data = JustETFScraper(FakeFetch(status_code=500)).extract_etf_data(ACC_ISIN, str(tmp_path))

        assert data == EXPECTED["acc_en"]["expected"]

    def test_missing_directory_falls_back_to_the_website(self, tmp_path):
        fetch = FakeFetch(page("acc_en"))

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN, str(tmp_path / "absent"))

        assert data["holdingCount_etf"] == 1252
        assert len(fetch.urls) == 1


class TestIsinValidation:
    def _validate(self, html: str, isin: str = ACC_ISIN) -> bool:
        return JustETFScraper()._validate_isin_match(BeautifulSoup(html, "html.parser"), isin)

    def test_canonical_url(self):
        html = f'<link rel="canonical" href="https://www.justetf.com/en/etf-profile.html?isin={ACC_ISIN}">'

        assert self._validate(html) is True
        assert self._validate(html, DIST_ISIN) is False

    def test_comparison_ignores_case(self):
        html = f'<link rel="canonical" href="https://x.test/p?isin={ACC_ISIN.lower()}">'

        assert self._validate(html) is True

    def test_header_isin_when_there_is_no_canonical_url(self):
        html = f'<span data-testid="etf-profile-header_isin-value">{ACC_ISIN}</span>'

        assert self._validate(html) is True
        assert self._validate(html, DIST_ISIN) is False

    def test_page_without_any_isin_is_rejected(self):
        assert self._validate("<html><body></body></html>") is False

    def test_empty_expected_isin_is_rejected(self):
        assert self._validate('<link rel="canonical" href="https://x.test/p?isin=">', "") is False


class TestFieldParsing:
    @pytest.mark.parametrize(
        ("text", "size", "millions", "currency"),
        [
            ("EUR 129,628 m", "129 628 m", 129628, "EUR"),
            ("EUR 129 628 M", "129 628 M", 129628, "EUR"),
            ("EUR 1.234 Mio.", "1 234 Mio.", 1234, "EUR"),
            ("USD 743 m", "743 m", 743, "USD"),
            ("743 M", "743 M", 743, None),
            ("<span> EUR 8,289 </span> m  ", "8 289 m", 8289, "EUR"),
            ("EUR 12 bn", "12 bn", None, "EUR"),
            ("n/a", "n/a", None, None),
        ],
    )
    def test_fund_size(self, text, size, millions, currency):
        data = {}

        JustETFScraper()._extract_fund_size(
            header("etf-profile-header_fund-size-value-wrapper", text), data
        )

        assert data == {
            "fundSize_etf": size,
            "fundSizeMillions_etf": millions,
            "fundSizeCurrency_etf": currency,
        }

    def test_fund_size_missing(self):
        data = {}

        JustETFScraper()._extract_fund_size(etree.HTML("<html><body></body></html>"), data)

        assert data == {
            "fundSize_etf": "",
            "fundSizeMillions_etf": None,
            "fundSizeCurrency_etf": None,
        }

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("0.20% p.a.", 0.2), ("0,20% p.a.", 0.2), ("0,07 % p.a.", 0.07), ("1% p.a.", 1.0)],
    )
    def test_fees(self, text, expected):
        data = {}

        JustETFScraper()._extract_fees(header("etf-profile-header_ter-value", text), data)

        assert data["fees_etf"] == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "n/a"])
    def test_fees_missing_keep_the_historical_default(self, text):
        data = {}

        JustETFScraper()._extract_fees(header("etf-profile-header_ter-value", text), data)

        assert data["fees_etf"] == 0.0

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("1,252", 1252), ("1 252", 1252), ("1 252", 1252), ("1.252", 1252), ("87", 87)],
    )
    def test_holding_count(self, text, expected):
        data = {}

        JustETFScraper()._extract_holding_count(
            header("etf-profile-header_holdings-value", text), data
        )

        assert data["holdingCount_etf"] == expected

    def test_holding_count_missing_keeps_the_historical_default(self):
        data = {}

        JustETFScraper()._extract_holding_count(
            header("etf-profile-header_holdings-value", "-"), data
        )

        assert data["holdingCount_etf"] == 0

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Accumulating", "Capitalisation"),
            ("Thesaurierend", "Capitalisation"),
            ("Capitalisation", "Capitalisation"),
            ("Distributing", "Distribution"),
            ("Ausschüttend", "Distribution"),
            ("DISTRIBUTING", "Distribution"),
            ("Unknown label", "Unknown label"),
        ],
    )
    def test_dividends_policy(self, text, expected):
        data = {}

        JustETFScraper()._extract_dividends_policy(
            header("etf-profile-header_distribution-policy-value", text), data
        )

        assert data["dividendsPolicy_etf"] == expected

    def test_name_from_header_when_title_id_is_absent(self):
        data = {}

        JustETFScraper()._extract_etf_name(
            header("etf-profile-header_etf-name", "Some UCITS ETF"), data
        )

        assert data["long_name"] == "Some UCITS ETF"

    def test_absolute_logo_url_is_kept(self):
        html = (
            '<span data-testid="etf-profile-header_provider-logo-image" '
            "style=\"background-image:url('https://cdn.example.test/logo.svg')\"></span>"
        )
        data = {}

        JustETFScraper()._extract_logo(BeautifulSoup(html, "html.parser"), data, is_local=False)

        assert data["logo"] == "https://cdn.example.test/logo.svg"
