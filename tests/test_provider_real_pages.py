"""Provider parsers exercised against extracts of real pages.

See ``tests/fixtures/providers/README.md`` for how the fixtures are built.
Synthetic pages only prove a parser agrees with itself; these extracts prove
it against the markup the site actually serves.
"""

import json
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from financials.providers.Barrons_provider import BarronsProvider
from financials.providers.BourseDirect_provider import BourseDirectProvider
from financials.providers.boursorama_provider import BoursoramaProvider
from financials.providers.Fortuneo_provider import FortuneoProvider
from financials.providers.GoogleFinance_provider import GoogleFinanceProvider
from financials.providers.Investing_provider import InvestingProvider
from financials.providers.InvestirLesEchos_provider import InvestirLesEchosProvider
from financials.providers.Marketwatch_provider import MarketwatchProvider
from financials.providers.MorningStar_provider import MorningStarProvider
from financials.providers.Msn_provider import MsnProvider
from financials.providers.wallStreetJournal_provider import WallStreetJournalProvider
from financials.providers.ZoneBourse_provider import ZoneBourseProvider

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
PAGE_URL = "https://example.test/page"

# How to run each provider's HTML parser on a document.
PARSERS = {
    "Barrons": lambda page, ticker: BarronsProvider()._parse_page(page, ticker),
    "BourseDirect": lambda page, ticker: BourseDirectProvider()._parse_page(page, ticker),
    "Boursorama": lambda page, ticker: BoursoramaProvider()._parse_page(page, ticker),
    "Fortuneo": lambda page, ticker: FortuneoProvider()._parse_page(page, ticker),
    "GoogleFinance": lambda page, ticker: GoogleFinanceProvider()._parse_page(page, ticker),
    "Investing": lambda page, ticker: InvestingProvider()._parse_page(page, ticker),
    "InvestirLesEchos": lambda page, ticker: InvestirLesEchosProvider()._parse_page(page, ticker),
    "Marketwatch": lambda page, ticker: MarketwatchProvider()._parse_page(page, ticker),
    "MorningStar": lambda page, ticker: MorningStarProvider()._parse_page(page, ticker, PAGE_URL),
    "Msn": lambda page, ticker: MsnProvider()._parse_page(page, ticker),
    "wallStreetJournal": lambda page, ticker: WallStreetJournalProvider()._parse_page(
        page, ticker
    ),
    "ZoneBourse": lambda page, ticker: ZoneBourseProvider()._parse_page(page, ticker, PAGE_URL),
}

# Echoed inputs, not information read from the page.
INPUT_FIELDS = {"ticker", "provider_url", "isin", "morningstar_url"}
FINANCIAL_FIELDS = (
    "pe_ratio",
    "dividend_yield",
    "eps",
    "revenue",
    "net_income",
    "ebitda",
    "price",
    "operating_margin",
    "profit_margin",
)

# What a scraper receives when the site's bot protection rejects it. Reduced from
# the real HTTP 401 response served by marketwatch.com (tokens removed).
BOT_CHALLENGE_PAGE = (
    '<html lang="fr"><head><title>marketwatch.com</title></head>'
    '<body style="margin:0"><p id="cmsg">Please enable JS and disable any ad blocker</p>'
    "</body></html>"
)
EMPTY_PAGE = "<html><head></head><body></body></html>"


def parse(provider: str, html: str, ticker: str) -> dict:
    result = PARSERS[provider](HTMLParser(html), ticker)
    if isinstance(result, dict):
        return {key: value for key, value in result.items() if value is not None}
    return result.model_dump(exclude_none=True)


def load_fixtures() -> list[tuple[str, dict, str]]:
    fixtures = []
    for meta_path in sorted(FIXTURES.glob("*.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        html = meta_path.with_suffix(".html").read_text(encoding="utf-8")
        fixtures.append((meta_path.stem, meta, html))
    return fixtures


FIXTURE_CASES = load_fixtures()
KNOWN_GAPS = [
    pytest.param(
        name,
        meta,
        html,
        field,
        gap["displayed"],
        id=f"{name}-{field}",
        marks=pytest.mark.xfail(strict=True, reason=gap["reason"]),
    )
    for name, meta, html in FIXTURE_CASES
    for field, gap in meta["known_gaps"].items()
]


def same_value(actual, displayed) -> bool:
    if isinstance(displayed, list):
        return isinstance(actual, list) and sorted(actual) == sorted(displayed)
    if isinstance(displayed, (int, float)) and not isinstance(displayed, bool):
        return isinstance(actual, (int, float)) and actual == pytest.approx(displayed, rel=1e-3)
    return actual == displayed


class TestRealPages:
    def test_fixtures_are_present(self):
        providers = {meta["provider"] for _name, meta, _html in FIXTURE_CASES}
        assert providers == {
            "Barrons",
            "Boursorama",
            "Fortuneo",
            "GoogleFinance",
            "InvestirLesEchos",
            "Marketwatch",
            "Msn",
            "ZoneBourse",
        }

    @pytest.mark.parametrize(
        ("name", "meta", "html"), FIXTURE_CASES, ids=[case[0] for case in FIXTURE_CASES]
    )
    def test_parser_reads_the_real_page(self, name, meta, html):
        parsed = parse(meta["provider"], html, meta["ticker"])

        for field, expected in meta["expected"].items():
            assert same_value(parsed.get(field), expected), (
                f"{name}: {field} = {parsed.get(field)!r}, the page displays {expected!r}"
            )

    @pytest.mark.parametrize(
        ("name", "meta", "html"), FIXTURE_CASES, ids=[case[0] for case in FIXTURE_CASES]
    )
    def test_every_parsed_field_is_accounted_for(self, name, meta, html):
        """A new field returned by a parser must be checked against the page."""
        parsed = parse(meta["provider"], html, meta["ticker"])

        documented = set(meta["expected"]) | set(meta["known_gaps"]) | INPUT_FIELDS
        assert set(parsed) <= documented, (
            f"{name}: add {sorted(set(parsed) - documented)} to the fixture's expected values"
        )

    @pytest.mark.parametrize(("name", "meta", "html", "field", "displayed"), KNOWN_GAPS)
    def test_known_gap(self, name, meta, html, field, displayed):
        """Values the real page displays but the parser misses or gets wrong."""
        parsed = parse(meta["provider"], html, meta["ticker"])

        assert same_value(parsed.get(field), displayed)


class TestUnusablePages:
    """A page without data must yield no figure and never raise."""

    @pytest.mark.parametrize("provider", sorted(PARSERS))
    @pytest.mark.parametrize(
        "html", [EMPTY_PAGE, BOT_CHALLENGE_PAGE], ids=["empty", "bot-challenge"]
    )
    def test_no_figure_is_invented(self, provider, html):
        parsed = parse(provider, html, "AIR")

        invented = {field: parsed[field] for field in FINANCIAL_FIELDS if field in parsed}
        assert not invented

    @pytest.mark.parametrize(
        ("name", "meta", "html"), FIXTURE_CASES, ids=[case[0] for case in FIXTURE_CASES]
    )
    def test_page_without_its_figures(self, name, meta, html):
        """Removing every digit from a real page must not leave stale figures."""
        blanked = "".join("" if char.isdigit() else char for char in html)

        parsed = parse(meta["provider"], blanked, meta["ticker"])

        invented = {field: parsed[field] for field in FINANCIAL_FIELDS if field in parsed}
        assert not invented


class TestFortuneoRealSearch:
    def test_search_response_resolves_to_the_listing_page(self):
        payload = json.loads((FIXTURES / "search" / "fortuneo_isin.json").read_text("utf-8"))

        item = FortuneoProvider._select_item(payload, "NL0000235190")
        result = FortuneoProvider._search_result_from_item(item, "NL0000235190")

        # Of the two identical listings, the one carrying a price is preferred.
        assert item["cours"] == pytest.approx(189.3)
        assert result["provider_url"] == (
            "https://bourse.fortuneo.fr/actions/cours-airbus-AIR-NL0000235190-23"
        )
        assert result["isin"] == "NL0000235190"
        assert result["ticker"] == "AIR"
        assert result["currency"] == "EUR"
