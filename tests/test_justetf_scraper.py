"""JustETFScraper (financials/providers/JustETF_provider.py).

The page fixtures in ``tests/fixtures/justetf_scraper`` are extracts of real profile
pages, reduced to what the scraper reads (see tests/fixtures/providers/README.md).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

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
    def test_french_profile_url_is_requested_by_default(self):
        fetch = FakeFetch(page("acc_fr"))

        JustETFScraper(fetch).extract_etf_data(ACC_ISIN)

        assert fetch.urls == [f"{ETF_PROFILE_URL}?isin={ACC_ISIN}"]
        assert "/fr/" in fetch.urls[0]

    def test_english_profile_url_is_requested_when_asked(self):
        fetch = FakeFetch(page("acc_en"))

        JustETFScraper(fetch, locale="en").extract_etf_data(ACC_ISIN)

        assert fetch.urls == [f"https://www.justetf.com/en/etf-profile.html?isin={ACC_ISIN}"]

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

    def test_listdir_os_error_falls_back_to_the_website(self, monkeypatch, tmp_path):
        fetch = FakeFetch(page("acc_en"))

        def mock_listdir(path):
            raise OSError("Permission denied")

        monkeypatch.setattr("os.listdir", mock_listdir)

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN, str(tmp_path))

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
            ("EUR 456,7 Mio.", "456,7 Mio.", 456.7, "EUR"),
            ("EUR 456.7 Mio.", "456.7 Mio.", 456.7, "EUR"),
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


def without_documents(name: str) -> str:
    """The page as the international English view serves it: no documents panel."""
    soup = BeautifulSoup(page(name), "html.parser")
    for panel in soup.select(".documentslist"):
        panel.decompose()
    return str(soup)


class RoutedFetch:
    """Answers each URL with the page of its language, and records the URLs."""

    def __init__(self, pages: dict[str, tuple[int, str]]):
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        locale = urlparse(url).path.strip("/").split("/", 1)[0]
        status_code, text = self.pages.get(locale, (404, ""))
        return SimpleNamespace(status_code=status_code, text=text)


def en_url(isin: str = ACC_ISIN) -> str:
    return f"https://www.justetf.com/en/etf-profile.html?isin={isin}"


def fr_url(isin: str = ACC_ISIN) -> str:
    return f"https://www.justetf.com/fr/etf-profile.html?isin={isin}"


class TestFactSheetPdf:
    def test_french_page_gives_the_french_fact_sheet(self):
        fetch = FakeFetch(page("acc_fr"))

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN)

        assert data["justETF_pdf_url"] == EXPECTED["acc_fr"]["expected"]["justETF_pdf_url"]
        assert data["justETF_pdf_url"].endswith("MR_FR_fr_IE00B4L5Y983_YES_2026-08-31.pdf")
        assert fetch.urls == [fr_url()]

    def test_english_page_without_documents_reads_the_fact_sheet_in_french(self):
        fetch = RoutedFetch({"en": (200, without_documents("acc_en")), "fr": (200, page("acc_fr"))})

        data = JustETFScraper(fetch, locale="en").extract_etf_data(ACC_ISIN)

        assert fetch.urls == [en_url(), fr_url()]
        assert data["justETF_pdf_url"] == EXPECTED["acc_fr"]["expected"]["justETF_pdf_url"]
        # The other figures stay those of the requested page.
        assert data["fundSize_etf"] == EXPECTED["acc_en"]["expected"]["fundSize_etf"]
        assert data["justETF_url"] == en_url()

    def test_failed_fallback_keeps_the_english_figures(self):
        fetch = RoutedFetch({"en": (200, without_documents("acc_en")), "fr": (503, "")})

        data = JustETFScraper(fetch, locale="en").extract_etf_data(ACC_ISIN)

        assert data["justETF_pdf_url"] == ""
        assert data["holdingCount_etf"] == 1252
        assert fetch.urls == [en_url(), fr_url()]

    def test_fallback_page_without_documents_gives_no_pdf(self):
        fetch = RoutedFetch(
            {"en": (200, without_documents("acc_en")), "fr": (200, without_documents("acc_fr"))}
        )

        data = JustETFScraper(fetch, locale="en").extract_etf_data(ACC_ISIN)

        assert data["justETF_pdf_url"] == ""
        assert len(fetch.urls) == 2

    def test_french_page_without_documents_is_not_read_twice(self):
        fetch = RoutedFetch({"fr": (200, without_documents("acc_fr"))})

        data = JustETFScraper(fetch).extract_etf_data(ACC_ISIN)

        assert data["justETF_pdf_url"] == ""
        assert fetch.urls == [fr_url()]

    def test_locale_of_the_call_wins_over_the_scraper_one(self):
        fetch = FakeFetch(page("acc_en"))

        JustETFScraper(fetch, locale="fr").extract_etf_data(ACC_ISIN, locale="en")

        assert fetch.urls == [en_url()]

    @pytest.mark.parametrize(
        "title",
        [
            "Fiche d’information (FR)",
            "Fiche d'information (FR)",
            "FICHE D‘INFORMATION",
            "Fiche dʼinformation",
            "Factsheet (EN)",
            "Fact sheet",
            "Fact-sheet (DE)",
        ],
    )
    def test_fact_sheet_titles(self, title):
        html = (
            '<a data-testid="etf-documents-panel_item-link" '
            f'href="https://x.test/doc.pdf" title="{title}">doc</a>'
        )
        data = {}

        JustETFScraper()._extract_fact_sheet_pdf(etree.HTML(html), data)

        assert data["justETF_pdf_url"] == "https://x.test/doc.pdf"

    def test_label_is_read_from_the_link_text_when_there_is_no_title(self):
        html = (
            '<a data-testid="etf-documents-panel_item-link" href="/docs/fs.pdf">'
            " Fiche d’information (FR) </a>"
        )
        data = {}

        JustETFScraper()._extract_fact_sheet_pdf(etree.HTML(html), data)

        assert data["justETF_pdf_url"] == "https://www.justetf.com/docs/fs.pdf"

    def test_fact_sheet_is_preferred_to_other_documents(self):
        html = (
            '<a data-testid="etf-documents-panel_item-link" href="https://x.test/kid.pdf" '
            "title=\"Document d'informations clés (FR)\">kid</a>"
            '<a data-testid="etf-documents-panel_item-link" href="https://x.test/other.pdf" '
            'title="Fiche produit">other</a>'
            '<a data-testid="etf-documents-panel_item-link" href="https://x.test/fs.pdf" '
            'title="Fiche d’information (FR)">fs</a>'
        )
        data = {}

        JustETFScraper()._extract_fact_sheet_pdf(etree.HTML(html), data)

        assert data["justETF_pdf_url"] == "https://x.test/fs.pdf"

    def test_no_fact_sheet_link(self):
        html = (
            '<a data-testid="etf-documents-panel_item-link" href="https://x.test/kid.pdf" '
            'title="KID">kid</a><a data-testid="etf-documents-panel_item-link" href=" " '
            'title="Factsheet">empty</a><a href="https://x.test/fs.pdf" title="Factsheet">x</a>'
        )
        data = {}

        JustETFScraper()._extract_fact_sheet_pdf(etree.HTML(html), data)

        assert data["justETF_pdf_url"] == ""

    def test_unexpected_error_leaves_an_empty_pdf_url(self):
        class BrokenDom:
            def xpath(self, _query):
                raise ValueError("broken")

        data = {}

        JustETFScraper()._extract_fact_sheet_pdf(BrokenDom(), data)

        assert data["justETF_pdf_url"] == ""


class TestProfileUrl:
    def test_full_url_is_read_as_given(self):
        url = f"https://www.justetf.com/fr/etf-profile.html?isin={ACC_ISIN}"
        fetch = FakeFetch(page("acc_fr"))

        data = JustETFScraper(fetch, locale="en").extract_etf_data(url)

        assert fetch.urls == [url]
        assert data["isin"] == ACC_ISIN
        assert data["justETF_url"] == url
        assert data["justETF_pdf_url"] == EXPECTED["acc_fr"]["expected"]["justETF_pdf_url"]

    def test_english_url_without_documents_falls_back_to_french(self):
        fetch = RoutedFetch({"en": (200, without_documents("acc_en")), "fr": (200, page("acc_fr"))})

        data = JustETFScraper(fetch).extract_etf_data(en_url())

        assert fetch.urls == [en_url(), fr_url()]
        assert data["justETF_url"] == en_url()
        assert data["justETF_pdf_url"] == EXPECTED["acc_fr"]["expected"]["justETF_pdf_url"]

    def test_url_is_used_to_find_local_files(self, tmp_path):
        (tmp_path / f"{ACC_ISIN}.html").write_text(page("acc_fr"), encoding="utf-8")
        fetch = FakeFetch(status_code=500)

        data = JustETFScraper(fetch).extract_etf_data(fr_url(), str(tmp_path))

        assert data == EXPECTED["acc_fr"]["expected"]
        assert fetch.urls == []

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (fr_url(), (ACC_ISIN, fr_url(), "fr")),
            (f"  {en_url()}  ", (ACC_ISIN, en_url(), "en")),
            (
                f"http://justetf.com/etf-profile.html?isin={ACC_ISIN}",
                (ACC_ISIN, f"http://justetf.com/etf-profile.html?isin={ACC_ISIN}", None),
            ),
            (ACC_ISIN, None),
            ("", None),
            ("https://www.justetf.com/fr/etf-profile.html", None),
            (f"https://evil.test/fr/etf-profile.html?isin={ACC_ISIN}", None),
            (f"https://justetf.com.evil.test/fr/?isin={ACC_ISIN}", None),
            (f"ftp://www.justetf.com/fr/?isin={ACC_ISIN}", None),
        ],
    )
    def test_parse_profile_url(self, value, expected):
        assert JustETF_provider.parse_profile_url(value) == expected

    @pytest.mark.parametrize(
        ("locale", "expected"),
        [("fr", "fr"), ("EN", "en"), (" de ", "de"), (None, "fr"), ("", "fr"), ("fr-FR", "fr"), ("../x", "fr")],
    )
    def test_normalize_locale(self, locale, expected):
        assert JustETF_provider.normalize_locale(locale) == expected

    def test_invalid_locale_of_the_scraper_falls_back_to_french(self):
        fetch = FakeFetch(page("acc_fr"))

        JustETFScraper(fetch, locale="english").extract_etf_data(ACC_ISIN)

        assert fetch.urls == [fr_url()]

    def test_public_functions_pass_the_locale(self, monkeypatch, tmp_path):
        fetch = FakeFetch(page("acc_en"))
        monkeypatch.setattr(JustETF_provider, "getRequest", fetch)

        justETFWebScraping(ACC_ISIN, locale="en")
        justETFScraping(ACC_ISIN, str(tmp_path), locale="en")

        assert fetch.urls == [en_url(), en_url()]
