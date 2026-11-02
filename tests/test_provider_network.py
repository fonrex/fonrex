"""Network layer of the financial providers, against a simulated network.

Covers what the page-parsing tests cannot: symbol search, page download,
retries and failures. No request leaves the machine (see ``fake_network`` in
``tests/conftest.py``).

Search and page bodies come from ``tests/fixtures/providers`` when a real
capture exists; the others are written by hand from the fields the code reads.
The failure scenarios reproduce what the sites really answer: HTTP 401 with a
bot challenge (MarketWatch), HTTP 403 (Cloudflare on Investing and Gurufocus),
a redirect to another site (Morningstar), HTTP 200 with a generic page (Google).
"""

import json
from pathlib import Path

import httpx
import pytest

from financials.models import FinancialMetrics
from financials.providers.Barrons_provider import BarronsProvider
from financials.providers.BourseDirect_provider import BourseDirectProvider
from financials.providers.boursorama_provider import BoursoramaProvider
from financials.providers.Fortuneo_provider import FortuneoProvider
from financials.providers.GoogleFinance_provider import GoogleFinanceProvider
from financials.providers.Gurufocus_provider import GurufocusProvider
from financials.providers.Investing_provider import InvestingProvider
from financials.providers.InvestirLesEchos_provider import InvestirLesEchosProvider
from financials.providers.Marketwatch_provider import MarketwatchProvider
from financials.providers.MorningStar_provider import MorningStarProvider
from financials.providers.Msn_provider import MsnProvider
from financials.providers.wallStreetJournal_provider import WallStreetJournalProvider
from financials.providers.ZoneBourse_provider import ZoneBourseProvider

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
AIRBUS_ISIN = "NL0000235190"

BOT_CHALLENGE = httpx.Response(
    401,
    text='<html><body><p id="cmsg">Please enable JS and disable any ad blocker</p></body></html>',
)
CLOUDFLARE_CHALLENGE = httpx.Response(403, text="<html><title>Just a moment...</title></html>")
GOOGLE_GENERIC_PAGE = (
    "<html><head><title>Google Finance</title></head><body><h1>Finance</h1></body></html>"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def ok(text: str) -> httpx.Response:
    return httpx.Response(200, text=text)


def ok_json(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


def status(code: int) -> httpx.Response:
    return httpx.Response(code, text="")


def dump(metrics) -> dict:
    return metrics if isinstance(metrics, dict) else metrics.model_dump(exclude_none=True)


# ── Boursorama ───────────────────────────────────────────────────────────────


class TestBoursorama:
    SEARCH = "boursorama.com/recherche/ajax"
    PAGE = "boursorama.com/cours/1rPAIR/"

    def _routes(self, net, search=None, page=None):
        net.get(self.SEARCH, *(search or [ok(fixture("search/boursorama_isin.html"))]))
        net.get(self.PAGE, *(page or [ok(fixture("boursorama_air.html"))]))

    async def test_search_then_page(self, fake_network):
        self._routes(fake_network)

        metrics = await BoursoramaProvider().get_financials(AIRBUS_ISIN)

        assert metrics.pe_ratio == pytest.approx(31.27)
        assert metrics.isin == AIRBUS_ISIN
        assert metrics.provider_url == "https://www.boursorama.com/cours/1rPAIR/"
        assert fake_network.urls() == [
            f"https://www.boursorama.com/recherche/ajax?query={AIRBUS_ISIN}&searchId=",
            "https://www.boursorama.com/cours/1rPAIR/",
        ]

    async def test_unknown_symbol_makes_no_page_request(self, fake_network):
        fake_network.get(
            self.SEARCH, ok("<html><body><ul class='search__list'></ul></body></html>")
        )

        assert await BoursoramaProvider().get_financials("ZZZZ") is None
        assert fake_network.calls("/cours/") == 0

    async def test_rate_limited_search_is_retried(self, fake_network):
        self._routes(
            fake_network,
            search=[status(429), status(503), ok(fixture("search/boursorama_isin.html"))],
        )

        metrics = await BoursoramaProvider().get_financials(AIRBUS_ISIN)

        assert metrics.pe_ratio == pytest.approx(31.27)
        assert fake_network.calls(self.SEARCH) == 3
        assert len(fake_network.backoffs) == 2
        assert fake_network.backoffs[1] > fake_network.backoffs[0]

    async def test_page_unavailable_after_all_retries(self, fake_network):
        self._routes(fake_network, page=[status(503)])

        assert await BoursoramaProvider(max_retries=3).get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.PAGE) == 3

    async def test_missing_page_is_not_retried(self, fake_network):
        self._routes(fake_network, page=[status(404)])

        assert await BoursoramaProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.PAGE) == 1
        assert fake_network.backoffs == []

    async def test_network_error_is_retried_then_recovers(self, fake_network):
        self._routes(
            fake_network,
            page=[httpx.ConnectError("connection reset"), ok(fixture("boursorama_air.html"))],
        )

        metrics = await BoursoramaProvider().get_financials(AIRBUS_ISIN)

        assert metrics.dividend_yield == pytest.approx(1.49)
        assert fake_network.calls(self.PAGE) == 2

    async def test_persistent_network_error_gives_none(self, fake_network):
        fake_network.get(self.SEARCH, httpx.ConnectTimeout("timed out"))

        assert await BoursoramaProvider(max_retries=2).get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.SEARCH) == 2


# ── ZoneBourse ───────────────────────────────────────────────────────────────


class TestZoneBourse:
    SEARCH = "zonebourse.com/async/search/quick"
    PAGE = "zonebourse.com/cours/action/AIRBUS-SE-4637/"

    def _routes(self, net, search=None, page=None):
        net.post(self.SEARCH, *(search or [ok(fixture("search/zonebourse_isin.json"))]))
        net.get(self.PAGE, *(page or [ok(fixture("zonebourse_air.html"))]))

    async def test_search_then_page(self, fake_network):
        self._routes(fake_network)

        metrics = await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        assert metrics.pe_ratio == pytest.approx(25.5)
        assert metrics.dividend_yield == pytest.approx(1.81)
        assert metrics.name == "Airbus SE"
        assert metrics.provider_url == "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"

    async def test_isin_of_the_page_never_replaces_the_ticker(self, fake_network):
        """Issue #32: the ISIN read on the page was written into ``ticker``."""
        page = (
            "<html><body><h1>Cours Airbus SE</h1>"
            "<ul><li>ISIN : NL0000235190</li></ul></body></html>"
        )
        self._routes(fake_network, page=[ok(page)])

        metrics = await ZoneBourseProvider().get_financials("AIR.PA")

        assert metrics.ticker == "AIR.PA"
        assert metrics.isin == "NL0000235190"

    async def test_target_price_above_a_thousand_keeps_its_thousands(self, fake_network):
        """A space separates the thousands: "2 150,00 EUR" is 2150, not 2."""
        page = (
            "<html><body><h1>Cours Hermès International</h1>"
            '<div class="grid py-5"><div class="c">Objectif de cours Moyen</div>'
            '<div class="c-auto"><span class="last">2\u202f150,00</span>EUR</div></div>'
            '<div class="grid py-5"><div class="c">Nombre d\'Analystes</div>'
            '<div class="c-auto">1 024</div></div>'
            "</body></html>"
        )
        self._routes(fake_network, page=[ok(page)])

        metrics = await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        assert metrics.target_price == pytest.approx(2150.0)
        assert metrics.analyst_count == 1024

    async def test_search_is_a_form_post_with_the_identifier(self, fake_network):
        self._routes(fake_network)

        await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        search = fake_network.requests[0]
        body = search.content.decode()
        assert search.method == "POST"
        assert f"search={AIRBUS_ISIN}" in body
        assert "type=company" in body
        assert search.headers["X-Requested-With"] == "XMLHttpRequest"

    @pytest.mark.parametrize(
        "search_response",
        [
            status(403),
            ok("<html>not json</html>"),
            ok_json({"error": False, "data": ""}),
            ok_json({"error": False, "data": "<table><tr><td>Aucun résultat</td></tr></table>"}),
            ok_json([]),
            httpx.ConnectTimeout("timed out"),
        ],
        ids=["http-403", "not-json", "empty-data", "no-result-row", "unexpected-shape", "timeout"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.post(self.SEARCH, search_response)

        assert await ZoneBourseProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls("/cours/") == 0

    async def test_page_server_error_is_retried_with_growing_pause(self, fake_network):
        self._routes(
            fake_network, page=[status(502), status(504), ok(fixture("zonebourse_air.html"))]
        )

        metrics = await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        assert metrics.revenue == pytest.approx(80_945_151_250)
        assert fake_network.calls(self.PAGE) == 3
        assert len(fake_network.backoffs) == 2
        assert fake_network.backoffs[1] > fake_network.backoffs[0]

    async def test_missing_page_is_not_retried(self, fake_network):
        self._routes(fake_network, page=[status(404)])

        assert await ZoneBourseProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.PAGE) == 1

    async def test_page_timeouts_exhaust_the_retries(self, fake_network):
        self._routes(fake_network, page=[httpx.ReadTimeout("timed out")])

        assert await ZoneBourseProvider(max_retries=3).get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.PAGE) == 3
        assert len(fake_network.backoffs) == 2

    async def test_session_headers_are_fixed_and_user_agent_is_configurable(
        self, fake_network, monkeypatch
    ):
        from financials.providers.ZoneBourse_provider import (
            DEFAULT_USER_AGENT,
            USER_AGENT_ENV_VAR,
        )

        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        self._routes(
            fake_network, page=[status(502), status(504), ok(fixture("zonebourse_air.html"))]
        )

        await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        # Search + 3 page attempts all share the exact same User-Agent (no rotation).
        assert [r.headers["User-Agent"] for r in fake_network.requests] == [DEFAULT_USER_AGENT] * 4
        page_req = fake_network.requests[1]
        assert page_req.headers["Sec-Fetch-Dest"] == "document"
        assert page_req.headers["Sec-Fetch-Mode"] == "navigate"
        assert page_req.headers["Sec-Fetch-Site"] == "none"

        fake_network.requests.clear()
        monkeypatch.setenv(USER_AGENT_ENV_VAR, "  CustomZoneBourseAgent/1.0  ")
        self._routes(fake_network)

        await ZoneBourseProvider().get_financials(AIRBUS_ISIN)

        assert [r.headers["User-Agent"] for r in fake_network.requests] == [
            "CustomZoneBourseAgent/1.0"
        ] * 2

    async def test_direct_provider_url_skips_the_search(self, fake_network):
        url = "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"
        fake_network.get(self.PAGE, ok(fixture("zonebourse_air.html")))

        metrics = await ZoneBourseProvider().get_financials("AIR.PA", provider_url=url)
        assert metrics.ticker == "AIR.PA"
        assert metrics.provider_url == url
        assert fake_network.calls(self.SEARCH) == 0

        fake_network.get(self.PAGE, ok(fixture("zonebourse_air.html")))
        by_path = await ZoneBourseProvider().fetch(
            ticker="/cours/action/AIRBUS-SE-4637/",
        )
        assert by_path.provider_url == url
        assert fake_network.calls(self.SEARCH) == 0

    async def test_isin_is_searched_before_ticker_and_falls_back_to_ticker(self, fake_network):
        search_rows = (
            "<table>"
            '<tr data-href="/cours/action/SANOFI-4698/">'
            '<td><i class="flag flag--16 flag__fr"></i></td>'
            '<td><a>SANOFI <span class="txt-muted">EUR</span></a></td>'
            '<td class="table-child--w80">SAN</td></tr>'
            '<tr data-href="/cours/action/BANCO-SANTANDER-S-A-69308/">'
            '<td><i class="flag flag--16 flag__es"></i></td>'
            '<td><a>BANCO SANTANDER <span class="txt-muted">EUR</span></a></td>'
            '<td class="table-child--w80">SAN</td></tr>'
            "</table>"
        )
        fake_network.post(
            self.SEARCH,
            ok_json({"error": False, "data": "<table></table>"}),
            ok_json({"error": False, "data": search_rows}),
        )
        fake_network.get(
            "zonebourse.com/cours/action/BANCO-SANTANDER-S-A-69308/",
            ok("<html><body><h1>Cours Banco Santander</h1></body></html>"),
        )

        metrics = await ZoneBourseProvider().get_financials("SAN.MC", isin="ES0113900J37")

        assert [r.content.decode() for r in fake_network.requests[:2]] == [
            "search=ES0113900J37&type=company",
            "search=SAN&type=company",
        ]
        assert (
            metrics.provider_url
            == "https://www.zonebourse.com/cours/action/BANCO-SANTANDER-S-A-69308/"
        )

    @pytest.mark.parametrize(
        ("ticker", "expected_url"),
        [
            ("SAN.PA", "https://www.zonebourse.com/cours/action/SANOFI-4698/"),
            ("SAN.MC", "https://www.zonebourse.com/cours/action/BANCO-SANTANDER-S-A-69308/"),
            ("AZN.ST", "https://www.zonebourse.com/cours/action/ASTRAZENECA-PLC-4000930/"),
            ("SAN.DE", None),
        ],
    )
    def test_select_url_filters_by_flag_and_currency_and_strips_cotations_suffix(
        self, ticker, expected_url
    ):
        html = (
            "<table>"
            '<tr data-href=""><td>ignored</td></tr>'
            '<tr data-href="/cours/action/SANOFI-4698/">'
            '<td><i class="flag flag--16 flag__fr"></i></td>'
            '<td><a>SANOFI <span class="txt-muted">EUR</span></a></td>'
            '<td class="table-child--w80">SAN</td></tr>'
            '<tr data-href="/cours/action/BANCO-SANTANDER-S-A-69308/">'
            '<td><i class="flag flag--16 flag__es"></i></td>'
            '<td><a>BANCO SANTANDER <span class="txt-muted">EUR</span></a></td>'
            '<td class="table-child--w80">SAN</td></tr>'
            '<tr data-href="/cours/action/ASTRAZENECA-PLC-4000930/cotations/9065830">'
            '<td><i class="flag flag--16 flag__se"></i></td>'
            '<td><a>ASTRAZENECA PLC <span class="txt-muted">SEK</span></a></td>'
            '<td class="table-child--w80">AZN</td></tr>'
            "</table>"
        )
        assert ZoneBourseProvider.select_url(html, ticker) == expected_url

    def test_select_url_handles_partial_place_metadata_and_share_classes(self):
        flag_only = (
            '<table><tr data-href="/cours/action/AIRBUS-SE-4637/">'
            '<td><i class="flag flag__fr"></i></td>'
            '<td class="table-child--w80">AIR</td></tr></table>'
        )
        currency_only = (
            '<table><tr data-href="/cours/action/AIRBUS-SE-4637/">'
            '<td><span class="txt-muted">EUR</span></td>'
            '<td class="table-child--w80">AIR</td></tr></table>'
        )
        symbol_only = (
            '<table><tr data-href="/cours/action/BERKSHIRE-4812/">'
            '<td class="table-child--w80">BRK.B</td></tr></table>'
        )
        assert (
            ZoneBourseProvider.select_url(flag_only, "AIR.PA")
            == "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"
        )
        assert (
            ZoneBourseProvider.select_url(currency_only, "AIR.PA")
            == "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"
        )
        assert ZoneBourseProvider.select_url(symbol_only, "BRK.B.PA") is None
        assert (
            ZoneBourseProvider.select_url(symbol_only, "BRK.B")
            == "https://www.zonebourse.com/cours/action/BERKSHIRE-4812/"
        )

    async def test_empty_identifier_returns_none_without_network(self, fake_network):
        assert await ZoneBourseProvider().get_financials("   ") is None
        assert fake_network.requests == []

    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            (
                "http://www.zonebourse.com/cours/action/AIRBUS-SE-4637/",
                "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/",
            ),
            (
                "https://zonebourse.com/cours/action/AIRBUS-SE-4637/?tab=1",
                "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/?tab=1",
            ),
            ("http://169.254.169.254/latest/meta-data/", None),
            ("https://www.zonebourse.com.evil.example/cours/action/X-1/", None),
            ("https://user@evil.example/cours/action/X-1/", None),
            ("https://www.zonebourse.com:8443/cours/action/X-1/", None),
            ("https://www.zonebourse.com/async/search/quick", None),
            ("AIR.PA", None),
        ],
    )
    def test_direct_url_only_follows_zonebourse_quote_pages(self, identifier, expected):
        assert ZoneBourseProvider.direct_url(identifier) == expected

    async def test_foreign_provider_url_is_refused_and_the_search_is_used(self, fake_network):
        self._routes(fake_network)

        metrics = await ZoneBourseProvider().get_financials(
            "AIR.PA", provider_url="http://169.254.169.254/latest/meta-data/"
        )

        assert metrics.provider_url == "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"
        assert all(r.url.host == "www.zonebourse.com" for r in fake_network.requests)

    async def test_foreign_url_given_as_ticker_makes_no_request(self, fake_network):
        assert await ZoneBourseProvider().get_financials("http://evil.example/cours/x/") is None
        assert fake_network.requests == []

    def test_select_url_never_picks_an_unreadable_row_for_a_ticker(self):
        """A row whose cells cannot be read is not taken because it comes first."""
        html = '<table><tr data-href="/cours/action/AIRBUS-SE-4637/"></tr></table>'

        assert ZoneBourseProvider.select_url(html, "AIR.PA") is None
        assert ZoneBourseProvider.select_url(html, "AIR") is None
        # An ISIN names one instrument: its first row is the answer.
        assert (
            ZoneBourseProvider.select_url(html, AIRBUS_ISIN)
            == "https://www.zonebourse.com/cours/action/AIRBUS-SE-4637/"
        )

    def test_select_url_prefers_the_united_states_for_a_ticker_without_suffix(self):
        def row(href, flag, currency):
            return (
                f'<tr data-href="{href}">'
                f'<td><i class="flag flag--16 flag__{flag}"></i></td>'
                f'<td><a>TESLA <span class="txt-muted">{currency}</span></a></td>'
                '<td class="table-child--w80">TSLA</td></tr>'
            )

        german = row("/cours/action/TESLA-DE-1/", "de", "EUR")
        american = row("/cours/action/TESLA-INC-6344549/", "us", "USD")

        assert (
            ZoneBourseProvider.select_url(f"<table>{german}{american}</table>", "TSLA")
            == "https://www.zonebourse.com/cours/action/TESLA-INC-6344549/"
        )
        # A preference, not a filter: without a United States row the symbol still matches.
        assert (
            ZoneBourseProvider.select_url(f"<table>{german}</table>", "TSLA")
            == "https://www.zonebourse.com/cours/action/TESLA-DE-1/"
        )
        # A known suffix keeps its place.
        assert (
            ZoneBourseProvider.select_url(f"<table>{american}{german}</table>", "TSLA.DE")
            == "https://www.zonebourse.com/cours/action/TESLA-DE-1/"
        )

    ASTRAZENECA = "https://www.zonebourse.com/cours/action/ASTRAZENECA-PLC-4000930/"
    ASTRAZENECA_PAGE = (
        "<html><body><h1>Cours AstraZeneca PLC</h1>"
        '<div class="is__realtime-last"><span class="js-last">11 234,0</span>'
        "<sup>GBP</sup></div>"
        '<div class="grid py-5"><div class="c">Objectif de cours Moyen</div>'
        '<div class="c-auto"><span class="last">12 500,00</span>GBP</div></div>'
        "<table><tr><td>Chiffre d'affaires</td><td>45,8 Md</td></tr></table>"
        "</body></html>"
    )

    async def test_page_of_another_listing_leaves_out_the_listing_figures(self, fake_network):
        """``AZN.ST`` is quoted in SEK; the synthesis page shows the GBP listing."""
        fake_network.get(
            "zonebourse.com/cours/action/ASTRAZENECA-PLC-4000930/", ok(self.ASTRAZENECA_PAGE)
        )

        metrics = await ZoneBourseProvider().get_financials("AZN.ST", provider_url=self.ASTRAZENECA)

        assert metrics.name == "AstraZeneca PLC"
        assert metrics.price is None
        assert metrics.currency is None
        assert metrics.target_price is None

    @pytest.mark.parametrize("ticker", ["AZN.L", "AZN", None])
    async def test_page_of_the_listing_asked_keeps_its_figures(self, fake_network, ticker):
        fake_network.get(
            "zonebourse.com/cours/action/ASTRAZENECA-PLC-4000930/", ok(self.ASTRAZENECA_PAGE)
        )

        metrics = await ZoneBourseProvider().get_financials(ticker, provider_url=self.ASTRAZENECA)

        assert metrics.price == pytest.approx(11234.0)
        assert metrics.currency == "GBP"
        assert metrics.target_price == pytest.approx(12500.0)


# ── Google Finance ───────────────────────────────────────────────────────────


class TestGoogleFinance:
    QUOTE = "google.com/finance/quote/"
    SEARCH = "google.com/finance/_/FinHubUi/data/batchexecute"

    async def test_yahoo_ticker_is_converted_for_the_url(self, fake_network):
        fake_network.get(self.QUOTE + "AIR:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("AIR.PA")

        assert metrics.pe_ratio == pytest.approx(25.21)
        assert metrics.name == "Airbus SE"
        assert metrics.provider_url == "https://www.google.com/finance/quote/AIR:EPA"
        assert metrics.ticker == "EPA:AIR"

    async def test_exchange_prefixed_ticker(self, fake_network):
        fake_network.get(self.QUOTE + "AIR:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("EPA:AIR")

        assert metrics.provider_url == "https://www.google.com/finance/quote/AIR:EPA"

    async def test_quote_url_is_used_as_is(self, fake_network):
        fake_network.get(self.QUOTE + "AIR:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials(
            "https://www.google.com/finance/quote/AIR:EPA?hl=en"
        )

        assert metrics.pe_ratio == pytest.approx(25.21)
        assert fake_network.urls() == ["https://www.google.com/finance/quote/AIR:EPA"]
        assert metrics.ticker == "EPA:AIR"

    async def test_request_asks_for_the_english_page(self, fake_network):
        fake_network.get(self.QUOTE + "AIR:EPA", ok(fixture("googlefinance_air.html")))

        await GoogleFinanceProvider().get_financials("AIR.PA")

        assert fake_network.requests[0].headers["Accept-Language"].startswith("en-US")

    async def test_us_ticker_falls_back_to_the_next_exchange(self, fake_network):
        # Google answers an unknown SYMBOL:EXCHANGE with HTTP 200 and its home page.
        fake_network.get(self.QUOTE + "IBM:NASDAQ", ok(GOOGLE_GENERIC_PAGE))
        fake_network.get(self.QUOTE + "IBM:NYSE", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("IBM")

        assert metrics.provider_url == "https://www.google.com/finance/quote/IBM:NYSE"
        assert metrics.ticker == "NYSE:IBM"
        assert metrics.pe_ratio == pytest.approx(25.21)

    async def test_unknown_us_ticker_tries_every_exchange_then_gives_none(self, fake_network):
        fake_network.get(self.QUOTE, ok(GOOGLE_GENERIC_PAGE))

        assert await GoogleFinanceProvider().get_financials("ZZZZQQ") is None
        assert [url.rsplit(":", 1)[1] for url in fake_network.urls()] == [
            "NASDAQ",
            "NYSE",
            "NYSEAMERICAN",
            "NYSEARCA",
            "OTCMKTS",
        ]

    async def test_unknown_european_ticker_gives_none_without_fallback(self, fake_network):
        fake_network.get(self.QUOTE, ok(GOOGLE_GENERIC_PAGE))

        assert await GoogleFinanceProvider().get_financials("ZZZZ.PA") is None
        assert len(fake_network.requests) == 1

    @pytest.mark.parametrize(
        "marker", ["Your search did not match any finance results", "We couldn't find any match"]
    )
    async def test_legacy_not_found_messages(self, fake_network, marker):
        fake_network.get(self.QUOTE, ok(f"<html><body>{marker}</body></html>"))

        assert await GoogleFinanceProvider().get_financials("ZZZZ.PA") is None

    async def test_rate_limit_is_retried(self, fake_network):
        fake_network.get(self.QUOTE + "AIR:EPA", status(429), ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("AIR.PA")

        assert metrics.pe_ratio == pytest.approx(25.21)
        assert len(fake_network.requests) == 2
        assert len(fake_network.backoffs) == 1

    async def test_missing_page_is_not_retried(self, fake_network):
        fake_network.get(self.QUOTE, status(404))

        assert await GoogleFinanceProvider().get_financials("AIR.PA") is None
        assert len(fake_network.requests) == 1

    async def test_network_errors_exhaust_the_retries(self, fake_network):
        fake_network.get(self.QUOTE, httpx.ConnectError("unreachable"))

        assert await GoogleFinanceProvider(max_retries=3).get_financials("AIR.PA") is None
        assert len(fake_network.requests) == 3

    async def test_symbol_exchange_ticker_is_kept(self, fake_network):
        fake_network.get(self.QUOTE + "ACA:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("ACA:EPA")

        assert metrics.provider_url == "https://www.google.com/finance/quote/ACA:EPA"
        assert metrics.ticker == "EPA:ACA"
        assert metrics.isin is None

    async def test_exchange_symbol_ticker_is_swapped(self, fake_network):
        fake_network.get(self.QUOTE + "ACA:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("EPA:ACA")

        assert metrics.provider_url == "https://www.google.com/finance/quote/ACA:EPA"

    async def test_isin_is_resolved_by_the_search(self, fake_network):
        fake_network.post(self.SEARCH, ok(fixture("search/googlefinance_isin.txt")))
        fake_network.get(self.QUOTE + "ACA:EPA", ok(fixture("googlefinance_air.html")))

        metrics = await GoogleFinanceProvider().get_financials("FR0000045072")

        assert metrics.provider_url == "https://www.google.com/finance/quote/ACA:EPA"
        assert metrics.ticker == "EPA:ACA"
        assert metrics.isin == "FR0000045072"
        search = fake_network.requests[0]
        assert search.method == "POST"
        form = dict(httpx.QueryParams(search.content.decode()))
        assert json.loads(form["f.req"]) == [
            [["XB3kn", json.dumps(["FR0000045072", None, 1]), None, "generic"]]
        ]

    async def test_isin_without_listing_gives_none_without_page(self, fake_network):
        fake_network.post(self.SEARCH, ok(rpc_answer([])))

        assert await GoogleFinanceProvider().get_financials("XX0000000000") is None
        assert fake_network.urls(self.QUOTE) == []

    async def test_isin_search_refused_gives_none(self, fake_network):
        fake_network.post(self.SEARCH, status(400))

        assert await GoogleFinanceProvider().get_financials("FR0000045072") is None
        assert fake_network.urls(self.QUOTE) == []

    async def test_isin_search_network_error_gives_none(self, fake_network):
        fake_network.post(self.SEARCH, httpx.ConnectError("unreachable"))

        assert await GoogleFinanceProvider(max_retries=1).get_financials("FR0000045072") is None

    async def test_isin_page_missing_does_not_try_us_exchanges(self, fake_network):
        fake_network.post(self.SEARCH, ok(fixture("search/googlefinance_isin.txt")))
        fake_network.get(self.QUOTE, ok(GOOGLE_GENERIC_PAGE))

        assert await GoogleFinanceProvider().get_financials("FR0000045072") is None
        assert fake_network.urls(self.QUOTE) == ["https://www.google.com/finance/quote/ACA:EPA"]


def rpc_answer(listings, chunked=False) -> str:
    """Answer of the Google Finance search service for (symbol, exchange, country) listings."""
    results = [
        [
            "/g/x",
            None,
            None,
            ["/g/x", [symbol, exchange], "Name", 0, "EUR", [], None, 1.0, "#000", country],
        ]
        for symbol, exchange, country in listings
    ]
    entry = json.dumps([["wrb.fr", "XB3kn", json.dumps([results]), None, None, None, "generic"]])
    if chunked:
        return f')]}}\'\n\n{len(entry)}\n{entry}\n57\n[["di",178]]\n'
    return f")]}}'\n\n{entry}"


class TestGoogleFinanceIsinListing:
    """Choice of the listing among the answers of the search service."""

    pick = staticmethod(GoogleFinanceProvider._pick_listing)
    parse = staticmethod(GoogleFinanceProvider._parse_search_listings)

    def test_real_answer_lists_every_place(self):
        listings = self.parse(fixture("search/googlefinance_isin.txt"))

        assert [(item["symbol"], item["exchange"], item["country"]) for item in listings] == [
            ("ACA", "EPA", "FR"),
            ("ACA", "VIE", "AT"),
            ("XCA", "FRA", "DE"),
            ("CRARF", "OTCMKTS", "US"),
            ("ACAFN", "BMV", "MX"),
        ]

    def test_chunked_answer_is_read(self):
        text = rpc_answer([("AIR", "EPA", "FR")], chunked=True)

        assert self.parse(text) == [{"symbol": "AIR", "exchange": "EPA", "country": "FR"}]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            ")]}'\n\nnot json",
            ')]}\'\n\n[["wrb.fr","XB3kn","not json"]]',
            ')]}\'\n\n[["wrb.fr","XB3kn","[]"]]',
            ')]}\'\n\n[["wrb.fr","XB3kn","[[[\\"/g/x\\"],[1,2,3,[\\"/g/x\\",[null,\\"EPA\\"]]]]]"]]',
        ],
    )
    def test_unreadable_answers_give_no_listing(self, text):
        assert self.parse(text) == []

    def test_listing_of_the_isin_country_wins(self):
        listings = self.parse(rpc_answer([("ACA", "VIE", "AT"), ("ACA", "EPA", "FR")]))

        assert self.pick(listings, "FR0000045072") == "ACA:EPA"

    def test_first_regulated_listing_without_one_in_the_country(self):
        # Airbus: Dutch ISIN, main listing in Paris.
        listings = self.parse(
            rpc_answer([("EADSY", "OTCMKTS", "US"), ("AIR", "EPA", "FR"), ("AIR", "FRA", "DE")])
        )

        assert self.pick(listings, AIRBUS_ISIN) == "AIR:EPA"

    def test_otc_listing_is_skipped_even_in_the_isin_country(self):
        listings = self.parse(rpc_answer([("CRARF", "OTCMKTS", "US"), ("ACA", "EPA", "FR")]))

        assert self.pick(listings, "US0000000001") == "ACA:EPA"

    def test_otc_listing_is_kept_when_alone(self):
        listings = self.parse(rpc_answer([("CRARF", "OTCMKTS", "US")]))

        assert self.pick(listings, "FR0000045072") == "CRARF:OTCMKTS"

    def test_no_listing(self):
        assert self.pick([], "FR0000045072") is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("FR0000045072", True),
            ("nl0000235190", True),
            ("AIR.PA", False),
            ("FR000004507X", False),
        ],
    )
    def test_isin_detection(self, value, expected):
        assert GoogleFinanceProvider._is_isin(value) is expected

    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("ACA:EPA", "ACA:EPA"),
            ("EPA:ACA", "ACA:EPA"),
            ("0700:HKG", "0700:HKG"),
            ("HKG:0700", "0700:HKG"),
            ("ACU:NYSEAMERICAN", "ACU:NYSEAMERICAN"),
            ("NESN:SWX", "NESN:SWX"),
            ("SWX:NESN", "NESN:SWX"),
            ("005930:KRX", "005930:KRX"),
            ("KRX:005930", "005930:KRX"),
            ("ICE:NYSE", "ICE:NYSE"),
            ("NYSE:ICE", "ICE:NYSE"),
            ("CRARF:OTC", "CRARF:OTC"),
            ("AIR.PA", "AIR:EPA"),
            ("AIR.XX", "AIR:XX"),
            ("AIR:XX", "AIR:XX"),
            ("IBM", "IBM:NASDAQ"),
        ],
    )
    def test_ticker_normalization(self, ticker, expected):
        assert GoogleFinanceProvider()._normalize_ticker(ticker) == expected


# ── Barron's, MarketWatch, WSJ: Dow Jones autocomplete then quote page ───────

DOW_JONES_SYMBOLS = {
    "symbols": [
        {
            "ticker": "AAPL",
            "country": "US",
            "type": "stock",
            "score": 10,
            "company": "Apple Inc.",
            "isin": "US0378331005",
            "exchangeIsoCode": "XNAS",
            "exchange": "Nasdaq",
        },
        {"ticker": "AAPL", "country": "MX", "type": "stock", "score": 99, "company": "Apple MX"},
    ]
}

WSJ_QUOTE_PAGE = """
<html><body><h1>Apple Inc.</h1><ul>
  <li>P/E Ratio: 38.25</li><li>EPS: 8.72</li><li>Dividend Yield: 0.32%</li>
</ul></body></html>
"""
WSJ_PEOPLE_PAGE = """
<html><body>
<table class="cr_mod_insider"><tbody><tr>
  <td>Last 3 Months</td>
  <td><span class="data_data">6 Purchases</span><span class="data_data">0 Sales</span></td>
  <td><span class="data_data">289,500 Purchased</span><span class="data_data">0 Sold</span></td>
</tr></tbody></table>
<table class="cr_mod_transactions"><tbody><tr>
  <td>08/01/2026</td><td>Jane Doe</td><td>1,000</td><td>Acquisition</td><td>$333,000</td>
</tr></tbody></table>
</body></html>
"""

DOW_JONES_CASES = [
    pytest.param(
        BarronsProvider,
        "api.barrons.com/api/autocomplete/search",
        "https://www.barrons.com/market-data/stocks/aapl",
        "barrons_aapl.html",
        id="Barrons",
    ),
    pytest.param(
        MarketwatchProvider,
        "api.wsj.net/api/autocomplete/search",
        "https://www.marketwatch.com/investing/stock/aapl",
        "marketwatch_aapl.html",
        id="Marketwatch",
    ),
]


@pytest.mark.parametrize(
    ("provider_class", "search_url", "page_url", "page_fixture"), DOW_JONES_CASES
)
class TestDowJonesQuotePages:
    async def test_search_then_page(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, ok(fixture(page_fixture)))

        metrics = await provider_class().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert metrics.eps == pytest.approx(8.72)
        assert metrics.dividend_yield == pytest.approx(0.32)
        # The US listing is preferred to a better-scored foreign one.
        assert metrics.name == "Apple Inc."
        assert metrics.isin == "US0378331005"
        assert metrics.provider_url == page_url
        assert "q=AAPL" in fake_network.urls(search_url)[0]

    async def test_bot_challenge_on_the_page_keeps_the_search_data(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, BOT_CHALLENGE)

        metrics = dump(await provider_class().get_financials("AAPL"))

        assert metrics == {
            "name": "Apple Inc.",
            "ticker": "AAPL",
            "isin": "US0378331005",
            "provider_url": page_url,
            "exchange": "XNAS",
            "exchange_name": "Nasdaq",
            "instrument_type": "stock",
        }

    async def test_page_is_fetched_again_after_a_failure(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, status(500), ok(fixture(page_fixture)))

        metrics = await provider_class().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert fake_network.calls(page_url) == 2

    @pytest.mark.parametrize(
        "search_response",
        [
            status(401),
            ok("not json"),
            ok_json({"symbols": []}),
            ok_json({}),
            httpx.ConnectError("unreachable"),
        ],
        ids=["http-401", "not-json", "no-symbol", "no-key", "network-error"],
    )
    async def test_unusable_search_gives_none(
        self, fake_network, provider_class, search_url, page_url, page_fixture, search_response
    ):
        fake_network.get(search_url, search_response)

        assert await provider_class().get_financials("AAPL") is None
        assert fake_network.calls(page_url) == 0

    async def test_foreign_listing_gets_a_country_code(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        symbols = {
            "symbols": [{"ticker": "AIR", "country": "FR", "type": "stock", "company": "Airbus"}]
        }
        fake_network.get(search_url, ok_json(symbols))
        fake_network.get("/air", BOT_CHALLENGE)

        metrics = await provider_class().get_financials("AIR")

        assert metrics.provider_url.endswith("/air?countrycode=fr")


DYLAN_BARRONS_ACA_RESPONSE = {
    "InstrumentResponses": [
        {
            "RequestId": "STOCK/FR/XPAR/ACA",
            "Matches": [
                {
                    "Instrument": {
                        "CommonName": "Credit Agricole S.A.",
                        "Ticker": "ACA",
                        "Isin": "FR0000045072",
                        "Exchange": {
                            "CommonName": "France: Euronext Paris",
                            "CountryCode": "FR",
                            "IsoCode": "XPAR",
                        },
                    },
                    "CompositeTrading": {
                        "Last": {"Price": {"Iso": "EUR", "Value": 17.545}},
                    },
                    "TradingStatistics": {
                        "PriceStatistics": [
                            {
                                "Range": {"Duration": {"ValueString": "P364D"}},
                                "Minimum": {"Value": 15.275},
                                "Maximum": {"Value": 19.295},
                            }
                        ],
                        "VolumeStatistics": [
                            {
                                "Range": {"Duration": {"ValueString": "P3M"}},
                                "Average": 5192363.26,
                            }
                        ],
                    },
                    "Financials": {
                        "PriceToEarningsRatio": 7.6249,
                        "LastEarningsPerShare": {"Iso": "EUR", "Value": 2.301},
                        "Yield": 0.317,
                        "AnnualizedDividend": None,
                        "LastDividendPerShare": {"Iso": "EUR", "Value": 1.1},
                        "MarketCapitalization": {"Iso": "EUR", "Value": 52766402330.0},
                        "SharesOutstanding": 3007489446.0,
                        "PublicFloat": 1072227496.0,
                        "Beta": 0.87,
                        "ShortInterest": 9671492.0,
                        "PercentOfFloatShorted": 0.902,
                    },
                }
            ],
        }
    ]
}


ACA_SYMBOLS = {
    "symbols": [
        {
            "ticker": "FR:ACA",
            "country": "FR",
            "type": "stock",
            "score": 1.0,
            "company": "Credit Agricole S.A.",
            "isin": "FR0000045072",
            "exchangeIsoCode": "XPAR",
            "exchange": "France: Euronext Paris",
            "chartingSymbol": "STOCK/FR/XPAR/ACA",
        }
    ]
}


class TestBarronsDylanAndTokens:
    SEARCH = "api.barrons.com/api/autocomplete/search"
    DYLAN = "api.barrons.com/api/dylan/quotes/v2/comp/quoteByDialect"
    ACA_PAGE = "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"

    async def test_dylan_fallback_when_page_is_blocked(self, fake_network):
        symbols = {
            "symbols": [
                {
                    "ticker": "FR:ACA",
                    "country": "FR",
                    "type": "stock",
                    "score": 1.0,
                    "company": "Credit Agricole S.A.",
                    "isin": "FR0000045072",
                    "exchangeIsoCode": "XPAR",
                    "exchange": "France: Euronext Paris",
                    "chartingSymbol": "STOCK/FR/XPAR/ACA",
                }
            ]
        }
        fake_network.get(self.SEARCH, ok_json(symbols))
        fake_network.get(self.ACA_PAGE, BOT_CHALLENGE)
        fake_network.get(self.DYLAN, ok_json(DYLAN_BARRONS_ACA_RESPONSE))

        metrics = await BarronsProvider().get_financials("ACA.PA")

        assert metrics.name == "Credit Agricole S.A."
        assert metrics.ticker == "ACA"
        assert metrics.isin == "FR0000045072"
        assert metrics.exchange == "XPAR"
        assert metrics.exchange_name == "France: Euronext Paris"
        assert metrics.instrument_type == "stock"
        assert metrics.price == pytest.approx(17.545)
        assert metrics.currency == "EUR"
        assert metrics.pe_ratio == pytest.approx(7.6249)
        assert metrics.eps == pytest.approx(2.301)
        assert metrics.dividend_yield == pytest.approx(0.317)
        assert metrics.dividend_rate == pytest.approx(1.1)
        assert metrics.beta == pytest.approx(0.87)
        assert metrics.market_cap == pytest.approx(52766402330.0)
        assert metrics.shares_outstanding == pytest.approx(3007489446.0)
        assert metrics.float_shares == pytest.approx(1072227496.0)
        assert metrics.shares_short == pytest.approx(9671492.0)
        assert metrics.short_percent_float == pytest.approx(0.902)
        assert metrics.week_52_low == pytest.approx(15.275)
        assert metrics.week_52_high == pytest.approx(19.295)
        assert metrics.average_volume == pytest.approx(5192363.26)

    async def test_dynamic_token_refresh_on_expired_default_token(self, fake_network, monkeypatch):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.delenv("BARRONS_TOKEN", raising=False)

        new_token = "c1b7ca756343461b86565b61bbd2ac6f"
        js_bundle = f'const cfg={{dylanEntitlementToken:"{new_token}"}};'
        symbols = {
            "symbols": [
                {
                    "ticker": "ACA",
                    "country": "FR",
                    "type": "stock",
                    "score": 1.0,
                    "company": "Credit Agricole S.A.",
                    "isin": "FR0000045072",
                    "exchangeIsoCode": "XPAR",
                    "exchange": "France: Euronext Paris",
                    "chartingSymbol": "STOCK/FR/XPAR/ACA",
                }
            ]
        }
        fake_network.get(self.SEARCH, status(400), ok_json(symbols))
        fake_network.get(BarronsProvider.TOKEN_SCRIPT_URL, ok(js_bundle))
        fake_network.get(self.ACA_PAGE, ok(fixture("barrons_aca.html")))

        metrics = await BarronsProvider().get_financials("FR:ACA")

        assert metrics.name == "Credit Agricole S.A."
        assert metrics.pe_ratio == pytest.approx(7.19)
        assert BarronsProvider._dynamic_token == new_token
        assert fake_network.calls(self.SEARCH) == 2
        assert fake_network.requests[2].headers["dylan2010.entitlementtoken"] == new_token

    async def test_european_isin_and_url_input_prefer_primary_exchange_over_otc(self, fake_network):
        aca_symbols = {
            "symbols": [
                {
                    "ticker": "CRARF",
                    "country": "US",
                    "exchangeIsoCode": "OOTC",
                    "exchange": "U.S.: OTC",
                    "isin": "FR0000045072",
                    "type": "stock",
                    "score": 1.0,
                    "company": "Credit Agricole S.A.",
                    "chartingSymbol": "STOCK/US/OOTC/CRARF",
                },
                {
                    "ticker": "FR:ACA",
                    "country": "FR",
                    "exchangeIsoCode": "XPAR",
                    "exchange": "France: Euronext Paris",
                    "isin": "FR0000045072",
                    "type": "stock",
                    "score": 0.9,
                    "company": "Credit Agricole S.A.",
                    "chartingSymbol": "STOCK/FR/XPAR/ACA",
                },
            ]
        }
        fake_network.get(self.SEARCH, ok_json(aca_symbols))
        fake_network.get(self.ACA_PAGE, ok(fixture("barrons_aca.html")))

        p = BarronsProvider()
        by_isin = await p.get_financials("FR0000045072")
        by_url = await p.get_financials(
            "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"
        )
        async with p._session() as client:
            search_sym = await p._search_symbol(client, "ACA.PA")

        assert by_isin.ticker == "ACA"
        assert by_isin.exchange == "XPAR"
        assert by_url.provider_url == self.ACA_PAGE
        assert search_sym == ("ACA", "FR")

    async def test_german_url_and_custom_env_token_and_error_paths(self, fake_network, monkeypatch):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.setenv("BARRONS_TOKEN", "custom_barrons_token_123")

        sap_symbols = {
            "symbols": [
                {
                    "ticker": "XE:SAP",
                    "country": "XE",
                    "exchangeIsoCode": "XETR",
                    "isin": "DE0007164600",
                    "type": "stock",
                    "score": 1.0,
                    "company": "SAP SE",
                    "chartingSymbol": "STOCK/XE/XETR/SAP",
                }
            ]
        }
        fake_network.get(self.SEARCH, ok_json(sap_symbols))
        fake_network.get("/sap?countrycode=xe", BOT_CHALLENGE)
        fake_network.get(self.DYLAN, status(503))

        p = BarronsProvider(max_retries=1)
        m1 = await p.get_financials("https://www.barrons.com/market-data/stocks/sap?countrycode=de")
        assert await p.get_financials("   ") is None

        assert m1.exchange == "XETR"
        assert (
            fake_network.requests[0].headers["dylan2010.entitlementtoken"]
            == "custom_barrons_token_123"
        )

    async def test_token_refresh_failure_from_page_fallback(self, fake_network, monkeypatch):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.delenv("BARRONS_TOKEN", raising=False)

        fake_network.get(self.SEARCH, status(400))
        fake_network.get(BarronsProvider.TOKEN_SCRIPT_URL, httpx.ConnectError("down"))
        fake_network.get("/market-data/stocks/aca", ok("<html>no token</html>"))

        p = BarronsProvider(max_retries=1)
        assert await p.get_financials("ACA") is None
        async with p._session() as client:
            assert await p._search_symbol(client, "ACA") == ("ACA", "US")
        assert fake_network.urls("/market-data/stocks/aca")[0] == (
            "https://www.barrons.com/market-data/stocks/aca"
        )

    async def test_expired_env_token_is_replaced_by_a_fresh_one(self, fake_network, monkeypatch):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.setenv("BARRONS_TOKEN", "0" * 32)

        new_token = "c1b7ca756343461b86565b61bbd2ac6f"
        js_bundle = f'const cfg={{dylanEntitlementToken:"{new_token}"}};'
        fake_network.get(self.SEARCH, status(400), ok_json(ACA_SYMBOLS))
        fake_network.get(BarronsProvider.TOKEN_SCRIPT_URL, ok(js_bundle))
        fake_network.get(self.ACA_PAGE, ok(fixture("barrons_aca.html")))

        metrics = await BarronsProvider().get_financials("FR:ACA")

        assert metrics.pe_ratio == pytest.approx(7.19)
        searches = [r for r in fake_network.requests if self.SEARCH in str(r.url)]
        assert [r.headers["dylan2010.entitlementtoken"] for r in searches] == ["0" * 32, new_token]

    async def test_token_is_read_from_the_page_when_the_script_is_unavailable(
        self, fake_network, monkeypatch
    ):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.delenv("BARRONS_TOKEN", raising=False)

        page_token = "a" * 32
        page_with_token = (
            f'<html><script>window.cfg={{"entitlementToken":"{page_token}"}}</script></html>'
        )
        fake_network.get(self.SEARCH, status(400), ok_json(ACA_SYMBOLS))
        fake_network.get(BarronsProvider.TOKEN_SCRIPT_URL, status(404))
        fake_network.get(self.ACA_PAGE, ok(page_with_token), ok(fixture("barrons_aca.html")))

        metrics = await BarronsProvider().get_financials("ACA.PA")

        assert metrics.pe_ratio == pytest.approx(7.19)
        assert fake_network.calls(self.SEARCH) == 2
        searches = [r for r in fake_network.requests if self.SEARCH in str(r.url)]
        assert searches[1].headers["dylan2010.entitlementtoken"] == page_token

    async def test_rejected_dylan_quote_is_retried_with_a_fresh_token(
        self, fake_network, monkeypatch
    ):
        monkeypatch.setattr(BarronsProvider, "TOKEN", None)
        monkeypatch.setattr(BarronsProvider, "_dynamic_token", None)
        monkeypatch.delenv("BARRONS_TOKEN", raising=False)

        new_token = "c1b7ca756343461b86565b61bbd2ac6f"
        js_bundle = f'const cfg={{dylanEntitlementToken:"{new_token}"}};'
        fake_network.get(self.SEARCH, ok_json(ACA_SYMBOLS))
        fake_network.get(self.ACA_PAGE, BOT_CHALLENGE)
        fake_network.get(BarronsProvider.TOKEN_SCRIPT_URL, ok(js_bundle))
        fake_network.get(self.DYLAN, status(401), ok_json(DYLAN_BARRONS_ACA_RESPONSE))

        metrics = await BarronsProvider().get_financials("ACA.PA")

        assert metrics.price == pytest.approx(17.545)
        assert metrics.pe_ratio == pytest.approx(7.6249)
        quotes = [r for r in fake_network.requests if self.DYLAN in str(r.url)]
        assert len(quotes) == 2
        assert quotes[1].headers["dylan2010.entitlementtoken"] == new_token
        assert quotes[1].url.params["EntitlementToken"] == new_token


DYLAN_TSLA_RESPONSE = {
    "InstrumentResponses": [
        {
            "RequestId": "STOCK/US/XNAS/TSLA",
            "Matches": [
                {
                    "Instrument": {
                        "CommonName": "Tesla Inc.",
                        "Ticker": "TSLA",
                        "Isin": "US88160R1014",
                        "Exchange": {
                            "CommonName": "U.S.: Nasdaq",
                            "CountryCode": "US",
                            "IsoCode": "XNAS",
                        },
                    },
                    "CompositeTrading": {
                        "Last": {"Price": {"Iso": "USD", "Value": 399.81}},
                    },
                    "TradingStatistics": {
                        "PriceStatistics": [
                            {
                                "Range": {"Duration": {"ValueString": "P364D"}},
                                "Minimum": {"Value": 297.38},
                                "Maximum": {"Value": 498.83},
                            }
                        ],
                        "VolumeStatistics": [
                            {
                                "Range": {"Duration": {"ValueString": "P3M"}},
                                "Average": 36680891.58,
                            }
                        ],
                    },
                    "Financials": {
                        "PriceToEarningsRatio": 439.79,
                        "LastEarningsPerShare": {"Iso": "USD", "Value": 0.91},
                        "Yield": None,
                        "AnnualizedDividend": None,
                        "LastDividendPerShare": None,
                        "MarketCapitalization": {"Iso": "USD", "Value": 1580352188975.2},
                        "SharesOutstanding": 3952758000.0,
                        "PublicFloat": 2824207492.4,
                        "Beta": 2.06,
                        "ShortInterest": 69350469.0,
                        "PercentOfFloatShorted": 2.46,
                    },
                }
            ],
        }
    ]
}


class TestMarketwatch:
    SEARCH = "api.wsj.net/api/autocomplete/search"
    DYLAN = "api.wsj.net/api/dylan/quotes/v2/comp/quoteByDialect"
    TSLA_PAGE = "https://www.marketwatch.com/investing/stock/tsla"

    async def test_dylan_fallback_when_page_is_blocked(self, fake_network):
        symbols = {
            "symbols": [
                {
                    "ticker": "TSLA",
                    "country": "US",
                    "type": "Stock",
                    "score": 1,
                    "company": "Tesla Inc.",
                    "isin": "US88160R1014",
                    "exchangeIsoCode": "XNAS",
                    "exchange": "U.S.: Nasdaq",
                    "chartingSymbol": "STOCK/US/XNAS/TSLA",
                }
            ]
        }
        fake_network.get(self.SEARCH, ok_json(symbols))
        fake_network.get(self.TSLA_PAGE, BOT_CHALLENGE)
        fake_network.get(self.DYLAN, ok_json(DYLAN_TSLA_RESPONSE))

        metrics = await MarketwatchProvider().get_financials("TSLA")

        assert metrics.name == "Tesla Inc."
        assert metrics.price == pytest.approx(399.81)
        assert metrics.currency == "USD"
        assert metrics.pe_ratio == pytest.approx(439.79)
        assert metrics.eps == pytest.approx(0.91)
        assert metrics.beta == pytest.approx(2.06)
        assert metrics.market_cap == pytest.approx(1580352188975.2)
        assert metrics.shares_outstanding == pytest.approx(3952758000.0)
        assert metrics.float_shares == pytest.approx(2824207492.4)
        assert metrics.shares_short == pytest.approx(69350469.0)
        assert metrics.short_percent_float == pytest.approx(2.46)
        assert metrics.week_52_low == pytest.approx(297.38)
        assert metrics.week_52_high == pytest.approx(498.83)
        assert metrics.average_volume == pytest.approx(36680891.58)

    async def test_dynamic_token_refresh_on_expired_default_token(self, fake_network, monkeypatch):
        monkeypatch.setattr(MarketwatchProvider, "TOKEN", None)
        monkeypatch.setattr(MarketwatchProvider, "_dynamic_token", None)
        monkeypatch.delenv("MARKETWATCH_TOKEN", raising=False)

        fresh_token = "11223344556677889900aabbccddeeff"
        page_with_token = (
            f'<div dylan-entitlement-token="{fresh_token}">'
            "<h1>Tesla Inc.</h1></div>"
        )
        fake_network.get(self.SEARCH, status(400), ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(self.TSLA_PAGE, ok(page_with_token))
        fake_network.get(
            "https://www.marketwatch.com/investing/stock/aapl",
            ok(fixture("marketwatch_aapl.html")),
        )

        metrics = await MarketwatchProvider().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert MarketwatchProvider._dynamic_token == fresh_token
        assert f"entitlementToken={fresh_token}" in fake_network.urls(self.SEARCH)[1]

    async def test_yahoo_suffix_and_european_isin_avoid_otc(self, fake_network):
        airbus_symbols = {
            "symbols": [
                {
                    "ticker": "EADSF",
                    "country": "US",
                    "exchangeIsoCode": "OOTC",
                    "isin": "NL0000235190",
                    "type": "Stock",
                    "score": 1.0,
                    "company": "Airbus SE",
                },
                {
                    "ticker": "FR:AIR",
                    "country": "FR",
                    "exchangeIsoCode": "XPAR",
                    "isin": "NL0000235190",
                    "type": "Stock",
                    "score": 0.8,
                    "company": "Airbus SE",
                },
            ]
        }
        fake_network.get(self.SEARCH, ok_json(airbus_symbols))
        fake_network.get("/air?countrycode=fr", BOT_CHALLENGE)

        by_isin = await MarketwatchProvider().get_financials("NL0000235190")
        by_suffix = await MarketwatchProvider().get_financials("AIR.PA")

        assert by_isin.exchange == "XPAR"
        assert by_isin.provider_url.endswith("/air?countrycode=fr")
        assert by_suffix.exchange == "XPAR"
        assert "q=FR%3AAIR" in fake_network.urls(self.SEARCH)[1]

    async def test_url_input_and_configured_token(self, fake_network, monkeypatch):
        monkeypatch.setattr(MarketwatchProvider, "TOKEN", "custom_env_token_123")
        fake_network.get(self.SEARCH, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get("investing/stock/aapl", BOT_CHALLENGE)

        metrics = await MarketwatchProvider().get_financials(
            "https://www.marketwatch.com/investing/stock/aapl?countrycode=us"
        )

        assert metrics.ticker == "AAPL"
        assert "entitlementToken=custom_env_token_123" in fake_network.urls(self.SEARCH)[0]

    async def test_german_url_and_colon_prefixed_ticker_and_isin_country_match(
        self, fake_network, monkeypatch
    ):
        monkeypatch.setattr(MarketwatchProvider, "TOKEN", None)
        monkeypatch.setattr(MarketwatchProvider, "_dynamic_token", "cached_dynamic_token_000000000000")
        monkeypatch.delenv("MARKETWATCH_TOKEN", raising=False)

        sap_symbols = {
            "symbols": [
                {
                    "ticker": "XE:SAP",
                    "country": "XE",
                    "exchangeIsoCode": "XETR",
                    "isin": "DE0007164600",
                    "type": "Stock",
                    "score": 1.0,
                    "company": "SAP SE",
                    "chartingSymbol": "STOCK/XE/XETR/SAP",
                }
            ]
        }
        fake_network.get(self.SEARCH, ok_json(sap_symbols))
        fake_network.get("/sap?countrycode=xe", BOT_CHALLENGE)
        fake_network.get(self.DYLAN, status(503))

        p = MarketwatchProvider(max_retries=1)
        m1 = await p.get_financials("https://www.marketwatch.com/investing/stock/sap?countrycode=de")
        m2 = await p.get_financials("XE:SAP")
        m3 = await p.get_financials("DE0007164600")
        assert await p.get_financials("   ") is None

        assert m1.exchange == "XETR"
        assert m2.exchange == "XETR"
        assert m3.exchange == "XETR"
        assert "entitlementToken=cached_dynamic_token_000000000000" in fake_network.urls(self.SEARCH)[0]

    async def test_token_refresh_failure_and_dylan_error(self, fake_network, monkeypatch):
        monkeypatch.setattr(MarketwatchProvider, "TOKEN", None)
        monkeypatch.setattr(MarketwatchProvider, "_dynamic_token", None)
        monkeypatch.delenv("MARKETWATCH_TOKEN", raising=False)

        fake_network.get(self.SEARCH, status(400))
        fake_network.get(self.TSLA_PAGE, httpx.ConnectError("down"))

        p = MarketwatchProvider(max_retries=1)
        assert await p.get_financials("TSLA") is None


# WSJ ranks listings by score, without the US preference of the two sites above.
WSJ_SYMBOLS = {
    "symbols": [
        {"ticker": "AAPL", "country": "MX", "type": "stock", "score": 10, "company": "Apple MX"},
        {
            "ticker": "AAPL",
            "country": "US",
            "type": "stock",
            "score": 99,
            "company": "Apple Inc.",
            "isin": "US0378331005",
            "exchangeIsoCode": "XNAS",
            "chartingSymbol": "STOCK/US/XNAS/AAPL",
        },
    ]
}


class TestWallStreetJournal:
    SEARCH = "api.wsj.net/api/autocomplete/search"
    PAGE = "https://www.wsj.com/market-data/quotes/US/XNAS/AAPL"

    async def test_french_listing_is_preferred_when_one_exists(self, fake_network):
        symbols = {
            "symbols": [
                {
                    "ticker": "AIR",
                    "country": "US",
                    "type": "stock",
                    "score": 99,
                    "exchangeIsoCode": "XNYS",
                },
                {
                    "ticker": "AIR",
                    "country": "FR",
                    "type": "stock",
                    "score": 10,
                    "exchangeIsoCode": "XPAR",
                },
            ]
        }
        fake_network.get(self.SEARCH, ok_json(symbols))
        fake_network.get("wsj.com/market-data/quotes/", BOT_CHALLENGE)

        metrics = await WallStreetJournalProvider(max_retries=1).get_financials("AIR")

        assert metrics.provider_url == "https://www.wsj.com/market-data/quotes/FR/XPAR/AIR"

    @pytest.mark.parametrize("token", [None, "public-site-token"], ids=["no-token", "token"])
    async def test_entitlement_token_is_sent_only_when_configured(
        self, fake_network, monkeypatch, token
    ):
        monkeypatch.setattr(WallStreetJournalProvider, "TOKEN", token)
        fake_network.get(self.SEARCH, ok_json({"symbols": []}))

        await WallStreetJournalProvider().get_financials("AAPL")

        assert fake_network.requests[0].headers.get("dylan2010.entitlementtoken") == token

    async def test_quote_and_insider_pages(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(WSJ_SYMBOLS))
        fake_network.get(self.PAGE + "/company-people", ok(WSJ_PEOPLE_PAGE))
        fake_network.get(self.PAGE, ok(WSJ_QUOTE_PAGE))

        metrics = await WallStreetJournalProvider().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert metrics.provider_url == self.PAGE
        assert metrics.insider_transactions == {
            "Summary": {
                "Last 3 Months": {
                    "transactions": "6 Purchases 0 Sales",
                    "shares": "289,500 Purchased / 0 Sold",
                }
            },
            "Transactions": [
                {
                    "date": "08/01/2026",
                    "ownerName": "Jane Doe",
                    "shares": "1,000",
                    "description": "Acquisition",
                    "value": "$333,000",
                }
            ],
        }

    async def test_search_sends_the_site_headers(self, fake_network):
        fake_network.get(self.SEARCH, ok_json({"symbols": []}))

        await WallStreetJournalProvider().get_financials("AAPL")

        search = fake_network.requests[0]
        assert search.headers["Referer"] == "https://www.wsj.com/"
        assert search.headers["Origin"] == "https://www.wsj.com"

    async def test_blocked_pages_keep_the_search_data(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(WSJ_SYMBOLS))
        fake_network.get(self.PAGE, BOT_CHALLENGE)

        metrics = dump(await WallStreetJournalProvider(max_retries=2).get_financials("AAPL"))

        assert metrics["name"] == "Apple Inc."
        assert metrics["provider_url"] == self.PAGE
        assert "pe_ratio" not in metrics
        assert "insider_transactions" not in metrics
        # A refusal (401) is final: the quote page and the insider page are each
        # requested once, without retry and without pause.
        assert fake_network.calls(self.PAGE) == 2
        assert fake_network.backoffs == []

    async def test_unusable_search_gives_none(self, fake_network):
        fake_network.get(self.SEARCH, status(403))

        assert await WallStreetJournalProvider().get_financials("AAPL") is None


# ── Investing ────────────────────────────────────────────────────────────────

INVESTING_QUOTES = {
    "quotes": [
        {
            "url": "/equities/apple-computer-inc",
            "symbol": "AAPL",
            "description": "Apple Inc",
            "exchange": "NASDAQ",
            "type": "Stock",
        }
    ]
}
INVESTING_PAGE = """
<html><body><h1>Apple Inc (AAPL)</h1><dl>
  <dt>P/E Ratio</dt><dd>38.25</dd><dt>EPS</dt><dd>8.72</dd>
</dl></body></html>
"""


class TestInvesting:
    SEARCH_INNER = "investing.com/search/service/SearchInnerPage"
    SEARCH = "api.investing.com/api/search/v2/search"
    PAGE = "https://www.investing.com/equities/apple-computer-inc"

    async def test_search_then_page(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert metrics.eps == pytest.approx(8.72)
        assert metrics.provider_url == self.PAGE
        assert metrics.exchange == "NASDAQ"
        # The search is an XHR of the site; the page is a navigation typed in the
        # address bar (Sec-Fetch-Site: none), which carries no Referer.
        assert fake_network.requests[0].headers["Referer"] == "https://www.investing.com/"
        assert "Referer" not in fake_network.requests[1].headers

    async def test_falls_back_to_v2_search_when_inner_search_returns_no_quote(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json({"quotes": []}))
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert fake_network.calls(self.SEARCH_INNER) == 1
        assert fake_network.calls(self.SEARCH) == 1

    async def test_session_headers_are_fixed_and_user_agent_is_configurable(
        self, fake_network, monkeypatch
    ):
        from financials.providers.Investing_provider import (
            DEFAULT_USER_AGENT,
            USER_AGENT_ENV_VAR,
        )

        monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, status(502), status(504), ok(INVESTING_PAGE))

        await InvestingProvider().get_financials("AAPL")

        assert [r.headers["User-Agent"] for r in fake_network.requests] == [DEFAULT_USER_AGENT] * 4
        page_req = fake_network.requests[1]
        assert page_req.headers["Sec-Fetch-Dest"] == "document"
        assert page_req.headers["Sec-Fetch-Mode"] == "navigate"
        assert page_req.headers["Sec-Fetch-Site"] == "none"

        fake_network.requests.clear()
        monkeypatch.setenv(USER_AGENT_ENV_VAR, "  CustomInvestingAgent/1.0  ")
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        await InvestingProvider().get_financials("AAPL")

        assert [r.headers["User-Agent"] for r in fake_network.requests] == [
            "CustomInvestingAgent/1.0"
        ] * 2

    async def test_cloudflare_challenge_keeps_the_search_data(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, CLOUDFLARE_CHALLENGE)

        metrics = dump(await InvestingProvider().get_financials("AAPL"))

        assert metrics == {
            "ticker": "AAPL",
            "provider_url": self.PAGE,
            "name": "Apple Inc",
            "exchange": "NASDAQ",
            "instrument_type": "Stock",
        }

    async def test_page_network_error_keeps_the_search_data(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, httpx.ReadTimeout("timed out"))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.name == "Apple Inc"

    async def test_dd_after_whitespace_is_read(self, fake_network):
        """Issue #32: a newline between <dt> and <dd> made every key figure missing."""
        page = (
            "<html lang='en'><body><h1>Apple Inc (AAPL)</h1><dl>\n"
            "  <dt>P/E Ratio</dt>\n  <dd>30.5</dd>\n"
            "  <dt>EPS</dt>\n  <!-- trailing -->\n  <dd>8.72</dd>\n"
            "  <dt>Dividend (Yield)</dt>\n  <dd>1.04 (0.41%)</dd>\n"
            "  <dt>52 wk Range</dt>\n  <dd>164.08 - 260.10</dd>\n"
            "  <dt>Market Cap</dt>\n  <dd>3.86T</dd>\n"
            "  <dt>Average Vol. (3m)</dt>\n  <dd>36.61M</dd>\n"
            "  <dt>ISIN</dt>\n  <dd>US0378331005</dd>\n"
            "  <dt>Next Earnings Date</dt>\n  <dd>Jan 29, 2027</dd>\n"
            "</dl></body></html>"
        )
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(page))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.name == "Apple Inc"
        assert metrics.pe_ratio == pytest.approx(30.5)
        assert metrics.eps == pytest.approx(8.72)
        assert metrics.dividend_rate == pytest.approx(1.04)
        assert metrics.dividend_yield == pytest.approx(0.41)
        assert (metrics.week_52_low, metrics.week_52_high) == (164.08, 260.10)
        assert metrics.market_cap == pytest.approx(3.86e12)
        assert metrics.average_volume == pytest.approx(36.61e6)
        assert metrics.isin == "US0378331005"

    async def test_isin_query_is_recorded(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("US0378331005")

        assert metrics.isin == "US0378331005"

    async def test_isin_is_searched_before_ticker_and_falls_back_to_ticker(self, fake_network):
        airbus_quotes = {
            "quotes": [
                {
                    "link": "/equities/aar-corp",
                    "symbol": "AIR",
                    "name": "AAR Corp",
                    "flag": "USA",
                    "exchange": "NYSE",
                    "type": "Stock - NYSE",
                    "pair_type": "equities",
                },
                {
                    "link": "/equities/eads",
                    "symbol": "AIR",
                    "name": "Airbus SE",
                    "flag": "France",
                    "exchange": "Paris",
                    "type": "Stock - Paris",
                    "pair_type": "equities",
                },
            ]
        }
        fake_network.post(
            self.SEARCH_INNER,
            ok_json({"quotes": []}),
            ok_json(airbus_quotes),
        )
        fake_network.get(self.SEARCH, ok_json({"quotes": []}))
        fake_network.get(
            "https://www.investing.com/equities/eads",
            ok(
                "<html><body><h1>Airbus SE (AIR)</h1><dl><dt>P/E Ratio</dt><dd>24.8</dd></dl></body></html>"
            ),
        )

        metrics = await InvestingProvider().fetch(ticker="AIR.PA", isin=AIRBUS_ISIN)

        assert metrics.provider_url == "https://www.investing.com/equities/eads"
        # Found by ticker only: the ISIN asked is not stated for it (the page shows none).
        assert metrics.isin is None
        assert metrics.pe_ratio == pytest.approx(24.8)

    @pytest.mark.parametrize(
        ("query", "expected_link"),
        [
            ("AIR.PA", "/equities/eads"),
            ("AIR", "/equities/aar-corp"),
            ("AIR.DE", None),
        ],
    )
    def test_select_quote_filters_by_place_and_prefers_us_for_bare_ticker(
        self, query, expected_link
    ):
        data = {
            "quotes": [
                {
                    "link": "/equities/aar-corp",
                    "symbol": "AIR",
                    "name": "AAR Corp",
                    "flag": "USA",
                    "exchange": "NYSE",
                    "pair_type": "equities",
                },
                {
                    "link": "/equities/eads",
                    "symbol": "AIR",
                    "name": "Airbus SE",
                    "flag": "France",
                    "exchange": "Paris",
                    "pair_type": "equities",
                },
            ]
        }
        quote = InvestingProvider._select_quote(data, query)
        assert (quote.get("link") if quote else None) == expected_link

    def test_select_quote_handles_partial_place_metadata_and_share_classes(self):
        flag_only = {"quotes": [{"link": "/equities/eads", "symbol": "AIR", "flag": "France"}]}
        exchange_only = {
            "quotes": [{"link": "/equities/eads", "symbol": "AIR", "exchange": "Paris"}]
        }
        symbol_only = {"quotes": [{"link": "/equities/berkshire", "symbol": "BRK.B"}]}

        assert InvestingProvider._select_quote(flag_only, "AIR.PA")["link"] == "/equities/eads"
        assert InvestingProvider._select_quote(exchange_only, "AIR.PA")["link"] == "/equities/eads"
        assert InvestingProvider._select_quote(symbol_only, "BRK.B.PA") is None
        assert (
            InvestingProvider._select_quote(symbol_only, "BRK.B")["link"] == "/equities/berkshire"
        )

    AIRBUS_LISTINGS = {
        "quotes": [
            {
                "link": "/equities/airbus-group-de",
                "symbol": "AIR",
                "name": "Airbus SE",
                "flag": "Germany",
                "exchange": "Xetra",
                "pair_type": "equities",
            },
            {
                "link": "/equities/eads",
                "symbol": "AIR",
                "name": "Airbus SE",
                "flag": "France",
                "exchange": "Paris",
                "pair_type": "equities",
            },
        ]
    }

    @pytest.mark.parametrize(
        ("ticker", "expected_link"),
        [
            ("AIR.PA", "/equities/eads"),
            ("AIR.DE", "/equities/airbus-group-de"),
            ("AIR.MI", None),
            (None, "/equities/airbus-group-de"),
        ],
    )
    def test_isin_listings_are_chosen_by_the_place_of_the_ticker(self, ticker, expected_link):
        quote = InvestingProvider._select_quote(self.AIRBUS_LISTINGS, AIRBUS_ISIN, ticker=ticker)
        assert (quote.get("link") if quote else None) == expected_link

    def test_isin_with_bare_ticker_prefers_a_us_listing(self):
        data = {
            "quotes": [
                {"link": "/equities/apple-computer-inc-de", "symbol": "APC", "flag": "Germany"},
                {"link": "/equities/apple-computer-inc", "symbol": "AAPL", "flag": "USA"},
            ]
        }
        quote = InvestingProvider._select_quote(data, "US0378331005", ticker="AAPL")
        assert quote["link"] == "/equities/apple-computer-inc"

    async def test_isin_search_reads_the_listing_of_the_place(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(self.AIRBUS_LISTINGS))
        fake_network.get(
            "https://www.investing.com/equities/eads",
            ok(
                "<html><body><h1>Airbus SE (AIR)</h1><dl><dt>ISIN</dt><dd>NL0000235190</dd>"
                "</dl></body></html>"
            ),
        )

        metrics = await InvestingProvider().get_financials("AIR.PA", isin=AIRBUS_ISIN)

        assert metrics.provider_url == "https://www.investing.com/equities/eads"
        assert metrics.isin == AIRBUS_ISIN
        assert fake_network.calls(self.SEARCH_INNER) == 1
        assert fake_network.calls(self.SEARCH) == 0

    async def test_isin_without_the_place_falls_back_to_the_ticker_search(self, fake_network):
        xetra_only = {"quotes": [self.AIRBUS_LISTINGS["quotes"][0]]}
        fake_network.post(self.SEARCH_INNER, ok_json(xetra_only), ok_json(self.AIRBUS_LISTINGS))
        fake_network.get(self.SEARCH, ok_json({"quotes": []}))
        fake_network.get("https://www.investing.com/equities/eads", ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("AIR.PA", isin=AIRBUS_ISIN)

        assert metrics.provider_url == "https://www.investing.com/equities/eads"
        assert fake_network.calls(self.SEARCH_INNER) == 2

    async def test_page_showing_another_isin_gives_none(self, fake_network):
        fake_network.post(self.SEARCH_INNER, ok_json(INVESTING_QUOTES))
        fake_network.get(
            self.PAGE,
            ok(
                "<html><body><h1>Apple Inc (AAPL)</h1><dl><dt>ISIN</dt><dd>US0378331005</dd>"
                "</dl></body></html>"
            ),
        )

        assert await InvestingProvider().get_financials("AAPL", isin=AIRBUS_ISIN) is None

    def test_bare_ticker_without_its_symbol_gives_none(self):
        data = {"quotes": [{"link": "/equities/other", "symbol": "ZZZW", "flag": "USA"}]}
        assert InvestingProvider._select_quote(data, "ZZZZ") is None

    def test_search_link_to_another_host_is_ignored(self):
        data = {"quotes": [{"link": "https://evil.example/equities/aapl", "symbol": "AAPL"}]}
        assert InvestingProvider._select_quote(data, "AAPL") is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("/equities/eads", "https://www.investing.com/equities/eads"),
            ("eads", "https://www.investing.com/equities/eads"),
            (
                "http://www.investing.com/equities/eads?x=1#y",
                "https://www.investing.com/equities/eads",
            ),
            ("https://fr.investing.com/equities/eads", "https://fr.investing.com/equities/eads"),
            (
                "https://WWW.Investing.com/equities/eads/",
                "https://www.investing.com/equities/eads/",
            ),
            ("http://169.254.169.254/latest/meta-data/", None),
            ("https://evil.example/equities/eads", None),
            ("https://www.investing.com.evil.example/equities/eads", None),
            ("https://www.investing.com:8443/equities/eads", None),
            ("https://user:pw@www.investing.com/equities/eads", None),
            ("https://www.investing.com/news/eads", None),
            ("https://www.investing.com/equities/../admin", None),
            ("//evil.example/equities/eads", None),
            ("https://www.investing.com:bad/equities/eads", None),
            ("../etc/passwd", None),
            ("", None),
        ],
    )
    def test_equity_page_url_only_accepts_investing_equity_pages(self, value, expected):
        from financials.providers.Investing_provider import equity_page_url

        assert equity_page_url(value) == expected

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"identifier": "http://169.254.169.254/latest/meta-data/"},
            {"identifier": "AAPL", "provider_url": "https://evil.example/equities/aapl"},
        ],
        ids=["identifier", "provider_url"],
    )
    async def test_foreign_url_is_refused_without_any_request(self, fake_network, kwargs):
        assert await InvestingProvider().get_financials(**kwargs) is None
        assert fake_network.requests == []

    async def test_french_edition_is_kept(self, fake_network):
        page = "https://fr.investing.com/equities/apple-computer-inc"
        fake_network.get(page, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials(page + "?utm=x")

        assert metrics.provider_url == page
        assert str(fake_network.requests[0].url) == page

    async def test_page_path_and_provider_url_skip_the_search(self, fake_network):
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("/equities/apple-computer-inc")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert fake_network.calls(self.SEARCH_INNER) == 0
        assert fake_network.calls(self.SEARCH) == 0

        fake_network.get(self.PAGE, ok(INVESTING_PAGE))
        direct = await InvestingProvider().get_financials(
            "AAPL", isin="US0378331005", provider_url=self.PAGE
        )
        assert direct.ticker == "AAPL"
        assert direct.isin == "US0378331005"
        assert direct.provider_url == self.PAGE
        assert fake_network.calls(self.SEARCH_INNER) == 0

    async def test_empty_identifier_returns_none_without_network(self, fake_network):
        assert await InvestingProvider().get_financials("   ") is None
        assert fake_network.requests == []

    @pytest.mark.parametrize(
        "search_response",
        [CLOUDFLARE_CHALLENGE, ok("not json"), ok_json({"quotes": []}), httpx.ConnectError("down")],
        ids=["http-403", "not-json", "no-quote", "network-error"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.post(self.SEARCH_INNER, search_response)
        fake_network.get(self.SEARCH, search_response)

        assert await InvestingProvider().get_financials("AAPL") is None


# ── Fortuneo ─────────────────────────────────────────────────────────────────


class TestFortuneo:
    SEARCH = "bourse.fortuneo.fr/api/search"
    PAGE = "https://bourse.fortuneo.fr/actions/cours-airbus-AIR-NL0000235190-23"

    async def test_search_then_page(self, fake_network):
        fake_network.get(self.SEARCH, ok(fixture("search/fortuneo_isin.json")))
        fake_network.get(self.PAGE, ok(fixture("fortuneo_air.html")))

        metrics = await FortuneoProvider().get_financials(AIRBUS_ISIN)

        assert metrics.name == "AIRBUS"
        assert metrics.ticker == "AIR"
        assert metrics.isin == AIRBUS_ISIN
        assert metrics.price == pytest.approx(189.3)
        assert metrics.provider_url == self.PAGE
        assert fake_network.urls(self.SEARCH) == [
            f"https://bourse.fortuneo.fr/api/search?term={AIRBUS_ISIN}"
        ]

    @pytest.mark.parametrize(
        "page_response",
        [status(500), status(404), httpx.ConnectError("down")],
        ids=["http-500", "http-404", "network-error"],
    )
    async def test_page_failure_keeps_the_search_data(self, fake_network, page_response):
        fake_network.get(self.SEARCH, ok(fixture("search/fortuneo_isin.json")))
        fake_network.get(self.PAGE, page_response)

        metrics = await FortuneoProvider().get_financials(AIRBUS_ISIN)

        assert metrics.name == "AIRBUS"
        assert metrics.price == pytest.approx(189.3)

    async def test_page_path_skips_the_search(self, fake_network):
        fake_network.get(self.PAGE, ok(fixture("fortuneo_air.html")))

        metrics = await FortuneoProvider().get_financials(
            "/actions/cours-airbus-AIR-NL0000235190-23"
        )

        assert metrics.name == "AIRBUS"
        assert fake_network.calls(self.SEARCH) == 0

    @pytest.mark.parametrize(
        "search_response",
        [status(503), ok("not json"), ok_json({"market": {}}), httpx.ReadTimeout("timed out")],
        ids=["http-503", "not-json", "no-item", "timeout"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.get(self.SEARCH, search_response)

        assert await FortuneoProvider().get_financials(AIRBUS_ISIN) is None


# ── BourseDirect ─────────────────────────────────────────────────────────────

BOURSEDIRECT_PATH = "/fr/marche/euronext-paris/airbus-NL0000235190-AIR-EUR-XPAR/seance"
BOURSEDIRECT_PAGE = """
<html><head><meta property="og:description" content="AIRBUS (NL0000235190) : cours en direct"></head>
<body><h1>AIRBUS</h1><span class="sticker sticker-default-reverse">AIR</span>
<span class="quotation-last">189,30</span><span class="quotation-variation">+1,99 %</span>
</body></html>
"""


class TestBourseDirect:
    SEARCH = "boursedirect.fr/api/search/"
    PAGE = "https://www.boursedirect.fr" + BOURSEDIRECT_PATH

    def _search(self):
        return ok_json({"instruments": {"data": [{"url": BOURSEDIRECT_PATH}]}})

    async def test_search_then_page(self, fake_network):
        fake_network.get(self.SEARCH, self._search())
        fake_network.get(self.PAGE, ok(BOURSEDIRECT_PAGE))

        metrics = await BourseDirectProvider().get_financials(AIRBUS_ISIN)

        assert metrics.name == "AIRBUS"
        assert metrics.ticker == "AIR"
        assert metrics.isin == AIRBUS_ISIN
        assert metrics.price == pytest.approx(189.3)
        assert metrics.change_percent == pytest.approx(1.99)
        assert metrics.provider_url == self.PAGE
        assert fake_network.urls(self.SEARCH)[0].endswith(f"/api/search/{AIRBUS_ISIN}")

    @pytest.mark.parametrize(
        "page_response",
        [status(403), status(500), httpx.ConnectError("down")],
        ids=["http-403", "http-500", "network-error"],
    )
    async def test_page_failure_gives_none(self, fake_network, page_response):
        fake_network.get(self.SEARCH, self._search())
        fake_network.get(self.PAGE, page_response)

        assert await BourseDirectProvider().get_financials(AIRBUS_ISIN) is None

    @pytest.mark.parametrize(
        "search_response",
        [
            status(404),
            ok("not json"),
            ok_json({"instruments": {"data": []}}),
            ok_json({}),
            httpx.ConnectError("down"),
        ],
        ids=["http-404", "not-json", "no-instrument", "no-key", "network-error"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.get(self.SEARCH, search_response)

        assert await BourseDirectProvider().get_financials(AIRBUS_ISIN) is None


# ── MSN ──────────────────────────────────────────────────────────────────────


def msn_summary() -> httpx.Response:
    """A real answer of the QuoteSummary of MSN for Airbus (tests/fixtures/providers/msn)."""
    return ok(fixture("msn/quote_summary_ae7bur.json"))


class TestMsn:
    SEARCH = "services.bingapis.com/contentservices-finance.csautosuggest/api/v1/Query"
    SUMMARY = "assets.msn.com/service/Finance/QuoteSummary"
    PAGE = "https://www.msn.com/fr-ca/finances/details-de-l-action/air-fr-stock/fi-ae7bur?id=ae7bur"

    async def test_search_then_quote_summary(self, fake_network):
        fake_network.get(self.SEARCH, ok(fixture("search/msn_ticker.json")))
        fake_network.get(self.SUMMARY, msn_summary())

        metrics = await MsnProvider().get_financials("AIR")

        assert metrics.name == "Airbus SE"
        assert metrics.provider_url == self.PAGE
        assert metrics.currency == "EUR"
        assert metrics.price == pytest.approx(198.88)
        assert metrics.pe_ratio == pytest.approx(25.4286)
        assert metrics.dividend_yield == pytest.approx(1.599)
        assert metrics.market_cap == pytest.approx(158_536_000_000)
        assert metrics.week_52_high == pytest.approx(221.3)
        assert metrics.week_52_low == pytest.approx(157.42)
        assert metrics.eps == pytest.approx(7.7981)
        assert metrics.payout_ratio == pytest.approx(38.4628)
        assert metrics.debt_to_equity == pytest.approx(0.4693)
        assert metrics.pb_ratio == pytest.approx(6.03)
        assert metrics.revenue == pytest.approx(73_420_000_000)
        assert metrics.net_income == pytest.approx(5_221_000_000)
        assert metrics.profit_margin == pytest.approx(6.7557)
        assert metrics.roe == pytest.approx(24.4975)
        assert metrics.roa == pytest.approx(4.4063)
        assert metrics.roic == pytest.approx(8.3491)
        assert metrics.beta == pytest.approx(1.2485)
        assert metrics.sector == "Industrials"
        assert metrics.industry == "Aerospace & Defense"
        # The page of msn.com is never downloaded: the figures come from the service.
        assert fake_network.calls("www.msn.com") == 0
        summary_url = fake_network.urls(self.SUMMARY)[0]
        assert "ids=ae7bur" in summary_url
        assert "apikey=" in summary_url
        assert "query=AIR" in fake_network.urls(self.SEARCH)[0]

    async def test_api_key_can_be_replaced(self, fake_network, monkeypatch):
        monkeypatch.setenv("MSN_FINANCE_API_KEY", " new-key ")
        fake_network.get(self.SEARCH, ok(fixture("search/msn_ticker.json")))
        fake_network.get(self.SUMMARY, msn_summary())

        await MsnProvider().get_financials("AIR")

        assert "apikey=new-key&" in fake_network.urls(self.SUMMARY)[0]

    async def test_stock_entry_already_decoded(self, fake_network):
        entry = json.loads(json.loads(fixture("search/msn_ticker.json"))["data"]["stocks"][0])
        fake_network.get(self.SEARCH, ok_json({"data": {"stocks": [entry]}}))
        fake_network.get(self.SUMMARY, msn_summary())

        metrics = await MsnProvider().get_financials("AIR")

        assert metrics.provider_url == self.PAGE

    async def test_isin_query_returns_no_stock(self, fake_network):
        # Real answer of the API to an ISIN query.
        fake_network.get(self.SEARCH, ok_json({"count": 1, "data": {"stocks": []}}))

        assert await MsnProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls("msn.com") == 0

    @pytest.mark.parametrize(
        "summary_response",
        [
            status(500),
            status(401),
            httpx.ReadTimeout("timed out"),
            ok("not json"),
            ok_json([]),
            ok_json({"error": "?"}),
            # Real answer for an identifier MSN does not know.
            ok_json([{"instrumentId": "zzzzzz", "id": "zzzzzz", "_t": "QuoteSummary"}]),
        ],
        ids=["http-500", "http-401", "timeout", "not-json", "empty", "not-a-list", "no-quote"],
    )
    async def test_summary_failure_gives_none(self, fake_network, summary_response):
        fake_network.get(self.SEARCH, ok(fixture("search/msn_ticker.json")))
        fake_network.get(self.SUMMARY, summary_response)

        assert await MsnProvider().get_financials("AIR") is None

    @pytest.mark.parametrize(
        "search_response",
        [
            status(500),
            ok("not json"),
            ok_json({"data": {"stocks": ["not json either"]}}),
            ok_json({"data": {"stocks": [{"OS001Index": "air"}]}}),
            httpx.ConnectError("down"),
        ],
        ids=["http-500", "not-json", "undecodable-entry", "entry-without-id", "network-error"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.get(self.SEARCH, search_response)

        assert await MsnProvider().get_financials("AIR") is None


# ── Investir Les Échos ───────────────────────────────────────────────────────

LES_ECHOS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"
LES_ECHOS_HITS = {
    "categories": [
        {
            "hits": [
                {
                    "fields": {
                        "DISPLAY_NAME": {"v": "Airbus"},
                        "M_SYMB": {"v": "AIR"},
                        "ISIN": {"v": AIRBUS_ISIN},
                        "MIC": {"v": "XPAR"},
                    }
                }
            ]
        }
    ]
}


class TestInvestirLesEchos:
    TOKEN_PAGE = "investir.lesechos.fr/recherche"
    SEARCH = "lesechosprx.solutions.webfg.ch"
    PAGE = "https://investir.lesechos.fr/cours/actions/airbus-air-nl0000235190-xpar"

    def _routes(self, net, search=None, page=None):
        net.get(self.TOKEN_PAGE, ok(f'<script>{{"token":"{LES_ECHOS_TOKEN}"}}</script>'))
        net.get(self.SEARCH, *(search or [ok_json(LES_ECHOS_HITS)]))
        net.get(self.PAGE, *(page or [ok(fixture("investirlesechos_air.html"))]))

    async def test_token_then_search_then_page(self, fake_network):
        self._routes(fake_network)

        metrics = await InvestirLesEchosProvider().get_financials("AIR")

        assert metrics.name == "Airbus"
        assert metrics.price == pytest.approx(189.3)
        assert metrics.change_percent == pytest.approx(1.99)
        assert metrics.isin == AIRBUS_ISIN
        assert metrics.provider_url == self.PAGE
        search = fake_network.requests[1]
        assert search.headers["Authorization"] == f"Bearer {LES_ECHOS_TOKEN}"
        assert "q=AIR" in str(search.url)

    async def test_token_is_fetched_once_per_provider(self, fake_network):
        self._routes(fake_network)
        provider = InvestirLesEchosProvider()

        await provider.get_financials("AIR")
        await provider.get_financials("AIR")

        assert fake_network.calls(self.TOKEN_PAGE) == 1
        assert fake_network.calls(self.SEARCH) == 2

    @pytest.mark.parametrize(
        "token_response",
        [ok("<html>no token here</html>"), status(403), httpx.ConnectError("down")],
        ids=["no-token", "http-403", "network-error"],
    )
    async def test_without_token_there_is_no_search(self, fake_network, token_response):
        fake_network.get(self.TOKEN_PAGE, token_response)

        assert await InvestirLesEchosProvider().get_financials("AIR") is None
        assert fake_network.calls(self.SEARCH) == 0

    @pytest.mark.parametrize(
        "search_response",
        [status(401), ok("not json"), ok_json({"categories": []}), httpx.ReadTimeout("timed out")],
        ids=["http-401", "not-json", "no-hit", "timeout"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        self._routes(fake_network, search=[search_response])

        assert await InvestirLesEchosProvider().get_financials("AIR") is None
        assert fake_network.calls(self.PAGE) == 0

    @pytest.mark.parametrize(
        "page_response",
        [status(404), httpx.ConnectError("down")],
        ids=["http-404", "network-error"],
    )
    async def test_page_failure_keeps_the_search_data(self, fake_network, page_response):
        self._routes(fake_network, page=[page_response])

        metrics = dump(await InvestirLesEchosProvider().get_financials("AIR"))

        assert metrics == {
            "name": "Airbus",
            "ticker": "AIR",
            "isin": AIRBUS_ISIN,
            "provider_url": self.PAGE,
        }


# ── Gurufocus ────────────────────────────────────────────────────────────────

GURUFOCUS_SEARCH = [
    {"type": "news", "data": {"symbol": "AIR"}},
    {"type": "stock", "data": {"symbol": "AIR", "exchange": "NYSE"}},
    {"type": "stock", "data": {"symbol": "AIR", "exchange": "XPAR"}},
]


def gurufocus_page(ticker: str = "XPAR:AIR") -> str:
    """A summary page shaped as GuruFocus serves it (tests/fixtures/providers/gurufocus_tsla.html)."""

    def row(code: str, label: str, value: str) -> str:
        return (
            f'<tr><td></td><td><a href="https://www.gurufocus.com/term/{code}/{ticker}">{label}</a>'
            f'</td><td><span class="p-l-sm"> {value}</span></td></tr>'
        )

    return (
        "<html><head><title>3M and 52-week figures | GuruFocus</title></head><body>"
        "<nav>Top 25 GF Score 100 Screener 2026</nav><h1>Airbus SE</h1>"
        f'<a href="https://www.gurufocus.com/term/gf-score/{ticker}">GF Score™: '
        '<span class="t-primary">81</span> <span class="t-label">/100</span></a>'
        '<table class="summary-table">'
        + row("fscore", "Piotroski F-Score", '7<span class="t-label">/9</span>')
        + row("mscore", "Beneish M-Score", "-2.45")
        + row("zscore", "Altman Z-Score", "3.1")
        + row("roic", "ROIC %", "12.3")
        + row("pettm", "PE Ratio", "28.4")
        + "</table></body></html>"
    )


class TestGurufocus:
    SEARCH = "gurufocus.com/reader/_api/_search"

    async def test_exchange_suffix_selects_the_matching_listing(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get("/stock/XPAR:AIR/summary", ok(gurufocus_page()))

        metrics = await GurufocusProvider().get_financials("AIR.PA")

        assert metrics.ticker == "XPAR:AIR"
        assert metrics.provider_url == "https://www.gurufocus.com/stock/XPAR:AIR/summary"
        assert metrics.name == "Airbus SE"
        assert metrics.gf_score == 81
        assert metrics.piotroski_score == 7
        assert metrics.beneish_m_score == pytest.approx(-2.45)
        assert metrics.altman_z_score == pytest.approx(3.1)
        assert metrics.roic == pytest.approx(12.3)
        assert metrics.pe_ratio == pytest.approx(28.4)
        assert "text=AIR" in fake_network.urls(self.SEARCH)[0]
        assert fake_network.requests[0].headers["Referer"] == "https://www.gurufocus.com/"
        # One page for every figure, asked as a page
        assert fake_network.calls("/stock/") == 1
        assert fake_network.calls("/term/") == 0
        page_request = fake_network.requests[-1]
        assert page_request.headers["Accept"].startswith("text/html")

    async def test_us_ticker_takes_the_first_exact_symbol(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get("/stock/AIR/summary", ok(gurufocus_page("AIR")))

        metrics = await GurufocusProvider().get_financials("AIR")

        # A US listing has no exchange in the address of its page
        assert metrics.ticker == "NYSE:AIR"
        assert metrics.provider_url == "https://www.gurufocus.com/stock/AIR/summary"
        assert metrics.piotroski_score == 7

    async def test_gurufocus_ticker_skips_the_search(self, fake_network):
        fake_network.get("/stock/AAPL/summary", ok(gurufocus_page("AAPL")))

        metrics = await GurufocusProvider().get_financials("NAS:AAPL")

        assert metrics.piotroski_score == 7
        assert fake_network.calls(self.SEARCH) == 0

    @pytest.mark.parametrize(
        "search_response",
        [CLOUDFLARE_CHALLENGE, ok("not json"), ok_json([]), httpx.ConnectError("down")],
        ids=["http-403", "not-json", "no-result", "network-error"],
    )
    async def test_failed_search_falls_back_to_the_exchange_mapping(
        self, fake_network, search_response
    ):
        fake_network.get(self.SEARCH, search_response)
        fake_network.get("/stock/XPAR:AIR/summary", ok(gurufocus_page()))

        metrics = await GurufocusProvider().get_financials("AIR.PA")

        assert metrics.ticker == "XPAR:AIR"
        assert metrics.piotroski_score == 7

    async def test_lowercase_ticker_gives_the_same_ticker_on_both_paths(self, fake_network):
        fake_network.get(self.SEARCH, CLOUDFLARE_CHALLENGE)
        fake_network.get("/stock/XPAR:AIR/summary", ok(gurufocus_page()))

        metrics = await GurufocusProvider().get_financials("air.pa")

        assert metrics.ticker == "XPAR:AIR"
        assert metrics.provider_url == "https://www.gurufocus.com/stock/XPAR:AIR/summary"

    @pytest.mark.parametrize(
        "page_response",
        [CLOUDFLARE_CHALLENGE, status(500), httpx.ReadTimeout("timed out")],
        ids=["http-403", "http-500", "timeout"],
    )
    async def test_unreadable_page_leaves_the_figures_empty(self, fake_network, page_response):
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get("/stock/", page_response)

        metrics = dump(await GurufocusProvider().get_financials("AIR.PA"))

        assert metrics == {
            "ticker": "XPAR:AIR",
            "provider_url": "https://www.gurufocus.com/stock/XPAR:AIR/summary",
        }

    async def test_numbers_outside_the_rows_are_never_taken(self, fake_network):
        """Issue #32: the first number of the page (title, menu) was taken for a score."""
        page = (
            "<html><head><title>3M Co (MMM) | GuruFocus</title></head><body>"
            "<nav>Top 25 GF Score 100</nav><h1>3M Co</h1>"
            "<p>Piotroski F-Score 2026 screener: 9 new ideas</p></body></html>"
        )
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get("/stock/", ok(page))

        metrics = dump(await GurufocusProvider().get_financials("AIR.PA"))

        assert set(metrics) == {"ticker", "provider_url", "name"}

    @pytest.mark.parametrize(
        ("label", "revenue"),
        [
            ("Revenue (TTM) (Mil $)", 1_234e6),
            ("Revenue (TTM) (Bil €)", 1_234e9),
            ("Revenue (TTM) (Thou ¥)", 1_234e3),
            ("Revenue (TTM)", 1_234.0),
        ],
    )
    async def test_revenue_takes_the_scale_of_its_label(self, fake_network, label, revenue):
        page = gurufocus_page().replace(
            "</table>",
            '<tr><td></td><td><a href="https://www.gurufocus.com/term/revenue/XPAR:AIR">'
            f"{label}</a></td><td><span>1,234</span></td></tr></table>",
        )
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get("/stock/", ok(page))

        metrics = await GurufocusProvider().get_financials("AIR.PA")

        assert metrics.revenue == pytest.approx(revenue)

    async def test_an_impossible_f_score_is_dropped(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(GURUFOCUS_SEARCH))
        fake_network.get(
            "/stock/", ok(gurufocus_page().replace('7<span class="t-label">/9</span>', "12"))
        )

        metrics = dump(await GurufocusProvider().get_financials("AIR.PA"))

        assert "piotroski_score" not in metrics
        assert metrics["roic"] == pytest.approx(12.3)

    async def test_exchange_suffix_never_takes_another_symbol_or_exchange(self, fake_network):
        """Searching SAN.PA must not take XPAR:AF or NYSE:SAN when XPAR:SAN is absent."""
        fake_network.get(
            self.SEARCH,
            ok_json(
                [
                    {"type": "stock", "data": {"symbol": "AF", "exchange": "XPAR"}},
                    {"type": "stock", "data": {"symbol": "SAN", "exchange": "NYSE"}},
                ]
            ),
        )
        fake_network.get("/stock/XPAR:SAN/summary", ok(gurufocus_page("XPAR:SAN")))

        metrics = await GurufocusProvider().get_financials("SAN.PA")

        assert metrics.ticker == "XPAR:SAN"
        assert metrics.provider_url == "https://www.gurufocus.com/stock/XPAR:SAN/summary"
        assert fake_network.requests[0].headers["Accept"].startswith("text/html")

    @pytest.mark.parametrize(
        ("nuxt_script", "expected"),
        [
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:87}}}(0));",
                87.0,
            ),
            (
                "window.__NUXT__=(function(a,b,bZ){return {stock:{gf_score:bZ}}}(void 0,.93,95));",
                95.0,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:0}}}(0));",
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:missing}}}(87));",
                None,
            ),
            (
                "window.__NUXT__={gf_score:bZ};",
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:a}}}(unquoted));",
                None,
            ),
            (
                'window.__NUXT__=(function(a){return {peers:[{symbol:"F",gf_score:41}],'
                'stock:{name:"a}{,gf_score:12",competitors:[{gf_score:41}],gf_score:87}}}(0));',
                87.0,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{peers:[{gf_score:41}]}}}(0));",
                None,
            ),
            (
                "window.__NUXT__=(function(a,b,c,d,e){return {stock:{gf_score:e},x:[a,b,c,d]}}"
                "(void 0,Array(2),new Date(0),{k:1,'q':\"(\"},64));",
                64.0,
            ),
            (
                'window.__NUXT__=(function(a,b){return {stock:{gf_score:b}}}("x}}(1,2\\"",72));',
                72.0,
            ),
            (
                "window.__NUXT__=(function(usd1){return {stock:{gf_score:usd1}}}(77));",
                77.0,
            ),
            (
                'window.__NUXT__=(function(a){return {stock:{gf_score:a}}}("81"));',
                81.0,
            ),
            (
                'window.__NUXT__=(function(a){return {stock:{gf_score:a}}}("\\x"));',
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:a}}}(true));",
                None,
            ),
            (
                "window.__NUXT__=(function(a,b){return {stock:{gf_score:b}}}(1));",
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:a}}});",
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:a}}}(87",
                None,
            ),
            (
                "window.__NUXT__=(function(a){return {stock:{gf_score:a}}",
                None,
            ),
        ],
        ids=[
            "literal",
            "iife-parameter",
            "zero-not-applicable",
            "unknown-parameter",
            "not-an-iife",
            "unparseable-args",
            "peer-scores-ignored",
            "only-a-peer-score",
            "other-args-not-json",
            "iife-marker-in-a-string",
            "currency-like-parameter",
            "number-in-a-string",
            "invalid-string",
            "not-a-number",
            "missing-argument",
            "function-never-called",
            "unclosed-call",
            "unclosed-body",
        ],
    )
    async def test_gf_score_falls_back_to_server_rendered_nuxt_state(
        self, fake_network, nuxt_script, expected
    ):
        page = (
            "<html><body><h1>Tesla Inc</h1>"
            "<script>var analytics = true;</script>"
            f"<script>{nuxt_script}</script>"
            "</body></html>"
        )
        fake_network.get("/stock/TSLA/summary", ok(page))

        metrics = await GurufocusProvider().get_financials("NAS:TSLA")

        assert metrics.gf_score == expected
        assert fake_network.requests[0].headers["Accept"].startswith("text/html")


# ── Morningstar ──────────────────────────────────────────────────────────────

MORNINGSTAR_FIXTURES = FIXTURES / "morningstar"


def morningstar(name: str) -> httpx.Response:
    """A real answer of the Morningstar services (tests/fixtures/providers/morningstar)."""
    return ok_json(json.loads((MORNINGSTAR_FIXTURES / name).read_text(encoding="utf-8")))


class TestMorningStar:
    SCREENER = "lt.morningstar.com/api/rest.svc/9vehuxllxs/security/screener"
    OVERVIEW = "sal-service/v1/stock/data/equityOverview"
    PROFILE = "sal-service/v1/stock/data/companyProfile"
    TRADING = "sal-service/v1/stock/tradingInformation/"
    TESLA = "US88160R1014"
    TESLA_PAGE = "https://global.morningstar.com/fr/investissements/actions/0P0000OQN8/cours"

    def _services(self, net, overview=None, profile=None, trading=None, ms_id="0P0000OQN8"):
        net.get(self.OVERVIEW, overview or morningstar(f"overview_{ms_id}.json"))
        net.get(self.PROFILE, profile or morningstar(f"profile_{ms_id}.json"))
        net.get(self.TRADING, trading or morningstar(f"trading_{ms_id}.json"))

    async def test_isin_then_overview_and_profile(self, fake_network):
        fake_network.get(self.SCREENER, morningstar("screener_US88160R1014.json"))
        self._services(fake_network)

        metrics = await MorningStarProvider().get_financials(self.TESLA)

        assert isinstance(metrics, FinancialMetrics)
        assert metrics.morningstar_id == "0P0000OQN8"
        assert metrics.isin == self.TESLA
        assert metrics.name == "Tesla Inc"
        assert (metrics.price, metrics.currency) == (407.46, "USD")
        assert metrics.market_cap == pytest.approx(1_592_554_978_643.7)
        assert metrics.pe_ratio == pytest.approx(447.758242)
        assert (metrics.ps_ratio, metrics.pb_ratio) == (13.926894, 18.037454)
        assert (metrics.week_52_low, metrics.week_52_high) == (297.38, 498.83)
        # A ratio, as the service gives it
        assert metrics.dividend_yield == 0.0
        assert metrics.investment_style == "Large Growth"
        assert (metrics.sector, metrics.industry) == ("Consumer Cyclical", "Auto Manufacturers")
        assert metrics.employees == 134785
        assert metrics.website == "www.tesla.com"
        assert metrics.provider_url == self.TESLA_PAGE
        # The premium-only placeholders ("_PO_") are never read as values
        assert getattr(metrics, "fair_value", None) is None
        assert not hasattr(metrics, "morningstar_pdf_url")

    async def test_trading_information(self, fake_network):
        fake_network.get(self.SCREENER, morningstar("screener_US88160R1014.json"))
        self._services(fake_network)

        metrics = await MorningStarProvider().get_financials(self.TESLA)

        assert metrics.beta == pytest.approx(1.942994)
        # Given in millions of shares
        assert metrics.shares_outstanding == pytest.approx(3_952_730_200)
        assert metrics.float_shares == pytest.approx(3_536_285_000)
        # Given as a count of shares, and as a percentage of the float
        assert metrics.shares_short == pytest.approx(69_350_469)
        assert metrics.short_percent_float == pytest.approx(1.9611)
        assert metrics.short_ratio == pytest.approx(2.043)
        # The overview's figures are kept when the two give the same field
        assert metrics.market_cap == pytest.approx(1_592_554_978_643.7)

    async def test_trading_carries_the_realtime_token(self, fake_network, monkeypatch):
        self._services(fake_network)

        await MorningStarProvider().get_financials("0P0000OQN8")
        default = next(r for r in fake_network.requests if self.TRADING in str(r.url))
        monkeypatch.setenv("MORNINGSTAR_SAL_REALTIME_TOKEN", "new-token")
        fake_network.requests.clear()
        await MorningStarProvider().get_financials("0P0000OQN8")
        replaced = next(r for r in fake_network.requests if self.TRADING in str(r.url))

        assert default.headers["X-API-REALTIME-E"].startswith("eyJlbmMiOiJBMTI4R0NNIi")
        assert (
            "X-API-REALTIME-E"
            not in next(r for r in fake_network.requests if self.OVERVIEW in str(r.url)).headers
        )
        assert replaced.headers["X-API-REALTIME-E"] == "new-token"

    async def test_a_yahoo_ticker_is_searched_without_its_suffix(self, fake_network):
        """Review of #39: the screener finds nothing for "AIR.PA", only for "AIR"."""
        fake_network.get(self.SCREENER, morningstar("screener_NL0000235190.json"))
        fake_network.get("sal-service", status(404))

        metrics = await MorningStarProvider().get_financials("AIR.PA")

        search = fake_network.requests[0]
        assert search.url.params["term"] == "AIR"
        assert search.url.params["pageSize"] == "50"
        assert metrics.morningstar_id == "0P00009WFE"

    async def test_requests_carry_the_isin_and_the_public_key(self, fake_network):
        fake_network.get(self.SCREENER, morningstar("screener_US88160R1014.json"))
        self._services(fake_network)

        await MorningStarProvider().get_financials(self.TESLA)

        search = fake_network.requests[0]
        assert search.url.params["term"] == self.TESLA
        overview = next(r for r in fake_network.requests if self.OVERVIEW in str(r.url))
        assert overview.url.params["shareClassId"] == "0P0000OQN8"
        assert overview.headers["ApiKey"] == "lstzFDEOhfFNMLikKa0am9mgEKLBl49T"

    async def test_the_key_can_be_replaced(self, fake_network, monkeypatch):
        monkeypatch.setenv("MORNINGSTAR_SAL_API_KEY", "new-key")
        self._services(fake_network)

        await MorningStarProvider().get_financials("0P0000OQN8")

        assert all(r.headers["ApiKey"] == "new-key" for r in fake_network.requests)

    async def test_a_listing_in_another_currency(self, fake_network):
        """0P0000A5ZK is SCOR quoted in Swiss francs: the currency is the one of the listing."""
        self._services(fake_network, ms_id="0P0000A5ZK")

        metrics = await MorningStarProvider().get_financials("0P0000A5ZK")

        assert (metrics.name, metrics.currency, metrics.price) == (
            "SCOR SE Act. Prov. Regpt.",
            "CHF",
            31.200001,
        )
        assert metrics.dividend_yield == pytest.approx(0.055754)
        assert metrics.investment_style == "Mid Value"
        assert metrics.country == "France"
        # No short interest is published for this listing: nothing is invented
        assert metrics.beta == pytest.approx(-0.024912)
        assert metrics.shares_outstanding == pytest.approx(178_663_400)
        assert getattr(metrics, "float_shares", None) is None
        assert getattr(metrics, "short_percent_float", None) is None

    @pytest.mark.parametrize(
        "identifier",
        [
            "0P0000OQN8",
            "https://global.morningstar.com/fr/investissements/actions/0P0000OQN8/cours",
        ],
        ids=["identifier", "page-url"],
    )
    async def test_identifier_or_page_skips_the_search(self, fake_network, identifier):
        self._services(fake_network)

        metrics = await MorningStarProvider().get_financials(identifier)

        assert metrics.morningstar_id == "0P0000OQN8"
        assert metrics.pe_ratio == pytest.approx(447.758242)
        assert fake_network.calls(self.SCREENER) == 0

    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("NL0000235190", "0P00009WFE"),
            ("AIR.PA", "0P00009WFE"),
            ("AIR", "0P0000AAR0"),
            ("AIR.DE", None),
            ("US0378331005", None),
        ],
        ids=["isin", "ticker-on-its-place", "bare-ticker-in-the-us", "other-place", "other-isin"],
    )
    def test_the_row_of_the_identifier_is_chosen(self, identifier, expected):
        rows = json.loads((MORNINGSTAR_FIXTURES / "screener_NL0000235190.json").read_text())
        rows = [
            # A fund comes first in the screener for short tickers: never taken
            {"SecId": "F00000ZZZZ", "ISIN": "NL0000235190", "Ticker": "AIR"},
            {"SecId": "0P000000ZZ", "ISIN": "FR0000000000", "Ticker": "ZZ"},
            # AAR Corp, "AIR" in New York
            {"SecId": "0P0000AAR0", "Ticker": "AIR", "ExchangeId": "EX$$$$XNYS"},
            *rows["rows"],
        ]

        assert MorningStarProvider.select_id(rows, identifier) == expected

    @pytest.mark.parametrize("failing", ["overview", "profile"])
    async def test_one_failing_service_keeps_the_other(self, fake_network, failing):
        fake_network.get(self.SCREENER, morningstar("screener_US88160R1014.json"))
        broken = status(500)
        self._services(
            fake_network,
            overview=broken if failing == "overview" else None,
            profile=broken if failing == "profile" else None,
        )

        metrics = dump(await MorningStarProvider().get_financials(self.TESLA))

        assert metrics["morningstar_id"] == "0P0000OQN8"
        assert ("pe_ratio" in metrics) == (failing == "profile")
        assert ("employees" in metrics) == (failing == "overview")

    @pytest.mark.parametrize(
        "service_response",
        [status(401), status(403), httpx.ConnectError("down"), ok("not json")],
        ids=["http-401", "http-403", "network-error", "not-json"],
    )
    async def test_unreadable_services_keep_the_identifiers(self, fake_network, service_response):
        fake_network.get(self.SCREENER, morningstar("screener_US88160R1014.json"))
        fake_network.get("sal-service", service_response)

        metrics = dump(await MorningStarProvider().get_financials(self.TESLA))

        assert metrics == {
            "ticker": self.TESLA,
            "isin": self.TESLA,
            "morningstar_id": "0P0000OQN8",
            "provider_url": self.TESLA_PAGE,
        }

    @pytest.mark.parametrize(
        "search_response",
        [status(500), ok_json({"total": 0, "rows": []}), ok("not json"), httpx.ReadTimeout("t")],
        ids=["http-500", "no-row", "not-json", "timeout"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
        fake_network.get(self.SCREENER, search_response)

        assert await MorningStarProvider().get_financials(self.TESLA) is None
        assert fake_network.calls("sal-service") == 0

    async def test_missing_identifier_makes_no_request(self, fake_network):
        assert await MorningStarProvider().get_financials("") is None
        assert fake_network.requests == []


# ── Yahoo Finance (yfinance library) ─────────────────────────────────────────


class FakeTicker:
    """Stands in for ``yfinance.Ticker``."""

    info = {
        "symbol": "AIR.PA",
        "totalRevenue": 70_000_000_000,
        "ebitda": 9_000_000_000,
        "netIncomeToCommon": 4_500_000_000,
        "trailingEps": 5.7,
        "payoutRatio": 0.45,
        "debtToEquity": 55.2,
        "beta": 1.2,
    }
    isin = "NL0000235190"
    institutional_holders = None
    mutualfund_holders = None
    requested: list[str] = []

    def __init__(self, ticker: str):
        type(self).requested.append(ticker)


class TestYahooFinance:
    @pytest.fixture(autouse=True)
    def _fake_library(self, monkeypatch):
        from financials.providers import yfinance_provider

        FakeTicker.requested = []
        monkeypatch.setattr(yfinance_provider.yf, "Ticker", FakeTicker)
        self.provider = yfinance_provider.YFinanceProvider()

    async def test_library_fields_are_mapped(self):
        metrics = await self.provider.get_financials("AIR.PA")

        assert metrics.revenue == 70_000_000_000
        assert metrics.net_income == 4_500_000_000
        assert metrics.eps == pytest.approx(5.7)
        assert metrics.payout_ratio == pytest.approx(0.45)
        assert metrics.isin == AIRBUS_ISIN
        assert metrics.ticker == "AIR.PA"
        assert metrics.provider_url == "https://finance.yahoo.com/quote/AIR.PA/"
        # Raw library fields stay available to the formatter.
        assert metrics.beta == pytest.approx(1.2)

    @pytest.mark.parametrize(
        "url",
        [
            "https://finance.yahoo.com/quote/AIR.PA",
            "https://finance.yahoo.com/quote/AIR.PA/",
            "https://finance.yahoo.com/quote/AIR.PA?p=AIR.PA",
        ],
    )
    async def test_quote_url_is_reduced_to_its_ticker(self, url):
        await self.provider.get_financials(url)

        assert FakeTicker.requested == ["AIR.PA"]

    async def test_holders_are_kept_for_the_rendered_document(self, monkeypatch):
        import pandas as pd

        institutions = pd.DataFrame(
            [{"Holder": "Vanguard Group", "Shares": 1_200_000_000, "% Out": 0.08}]
        )
        funds = pd.DataFrame([{"Holder": "Vanguard Total Stock Market", "Shares": 400_000_000}])
        monkeypatch.setattr(FakeTicker, "institutional_holders", institutions)
        monkeypatch.setattr(FakeTicker, "mutualfund_holders", funds)

        metrics = await self.provider.get_financials("AIR.PA")

        assert metrics.holders["institutions"] == [
            {"Holder": "Vanguard Group", "Shares": 1_200_000_000, "% Out": 0.08}
        ]
        assert metrics.holders["funds"][0]["Holder"] == "Vanguard Total Stock Market"

    async def test_placeholder_isin_is_not_kept(self, monkeypatch):
        monkeypatch.setattr(FakeTicker, "isin", "-")

        assert (await self.provider.get_financials("AIR.PA")).isin is None

    async def test_answer_is_not_printed(self, capsys):
        """The whole Yahoo payload used to be written to the output at each request."""
        await self.provider.get_financials("AIR.PA")

        assert capsys.readouterr().out == ""

    async def test_empty_answer_gives_none(self, monkeypatch):
        monkeypatch.setattr(FakeTicker, "info", {})

        assert await self.provider.get_financials("ZZZZ") is None

    async def test_library_error_gives_none(self, monkeypatch):
        from financials.providers import yfinance_provider

        def broken(ticker):
            raise RuntimeError("Too Many Requests")

        monkeypatch.setattr(yfinance_provider.yf, "Ticker", broken)

        assert await self.provider.get_financials("AIR.PA") is None


# ── Shared HTTP policy, seen from the scrapers ───────────────────────────────


@pytest.mark.parametrize(
    ("provider_class", "search_url", "page_url", "page_fixture"), DOW_JONES_CASES
)
class TestDowJonesSharedPolicy:
    """Barron's and MarketWatch used to retry three times without pause, even on a refusal."""

    async def test_refused_page_is_requested_once(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, status(404))

        metrics = dump(await provider_class().get_financials("AAPL"))

        assert metrics["name"] == "Apple Inc."
        assert "pe_ratio" not in metrics
        assert fake_network.calls(page_url) == 1
        assert fake_network.backoffs == []

    async def test_server_error_is_retried_after_a_pause(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, status(503), status(503), ok(fixture(page_fixture)))

        metrics = await provider_class().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert fake_network.calls(page_url) == 3
        assert fake_network.backoffs == [1.0, 2.0]

    async def test_network_failure_on_the_page_keeps_the_search_data(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, httpx.ConnectError("unreachable"))

        metrics = dump(await provider_class(max_retries=2).get_financials("AAPL"))

        assert metrics["name"] == "Apple Inc."
        assert "pe_ratio" not in metrics
        assert fake_network.calls(page_url) == 2

    async def test_page_user_agent_changes_between_attempts_are_browser_like(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS))
        fake_network.get(page_url, status(503), ok(fixture(page_fixture)))

        await provider_class().get_financials("AAPL")

        page_requests = [r for r in fake_network.requests if page_url in str(r.url)]
        assert len(page_requests) == 2
        assert all(r.headers["User-Agent"].startswith("Mozilla/5.0") for r in page_requests)

    async def test_search_symbol_helper(
        self, fake_network, provider_class, search_url, page_url, page_fixture
    ):
        fake_network.get(search_url, ok_json(DOW_JONES_SYMBOLS), ok_json({"symbols": []}))
        provider = provider_class()

        async with provider._session() as client:
            found = await provider._search_symbol(client, "AAPL")
            fallback = await provider._search_symbol(client, "ZZZZ")

        assert found[0] == "AAPL"
        assert fallback[0] == "ZZZZ"
        assert fallback[1].upper() == "US"


class TestBoursoramaFailures:
    SEARCH = TestBoursorama.SEARCH
    PAGE = TestBoursorama.PAGE

    async def test_unexpected_error_while_parsing_gives_none(self, fake_network, monkeypatch):
        fake_network.get(self.SEARCH, ok(fixture("search/boursorama_isin.html")))
        fake_network.get(self.PAGE, ok(fixture("boursorama_air.html")))
        provider = BoursoramaProvider()

        def broken_parser(html, ticker):
            raise ValueError("unexpected page layout")

        monkeypatch.setattr(provider, "_parse_page", broken_parser)

        assert await provider.get_financials(AIRBUS_ISIN) is None

    async def test_unexpected_error_during_the_search_gives_none(self, fake_network, monkeypatch):
        provider = BoursoramaProvider()

        async def broken_fetch(client, url):
            raise RuntimeError("boom")

        monkeypatch.setattr(provider, "_fetch_with_retry", broken_fetch)

        assert await provider.get_financials(AIRBUS_ISIN) is None
        assert fake_network.requests == []

    async def test_refused_search_is_requested_once(self, fake_network):
        fake_network.get(self.SEARCH, status(403))

        assert await BoursoramaProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls(self.SEARCH) == 1
        assert fake_network.backoffs == []

    async def test_server_error_on_the_page_is_retried_after_a_pause(self, fake_network):
        fake_network.get(self.SEARCH, ok(fixture("search/boursorama_isin.html")))
        fake_network.get(self.PAGE, status(502), ok(fixture("boursorama_air.html")))

        metrics = await BoursoramaProvider().get_financials(AIRBUS_ISIN)

        assert metrics is not None
        assert fake_network.calls(self.PAGE) == 2
        assert fake_network.backoffs == [1.0]


class TestInvestirLesEchosHelpers:
    async def test_search_url_helper(self, fake_network):
        fake_network.get(
            TestInvestirLesEchos.TOKEN_PAGE, ok(f'<script>{{"token":"{LES_ECHOS_TOKEN}"}}</script>')
        )
        fake_network.get(TestInvestirLesEchos.SEARCH, ok_json(LES_ECHOS_HITS), ok_json({}))
        provider = InvestirLesEchosProvider()

        async with provider._session() as client:
            found = await provider._search_url(client, "AIR")
            missing = await provider._search_url(client, "ZZZZ")

        assert found == TestInvestirLesEchos.PAGE
        assert missing is None


class TestMorningStarJoinsTheCommonBase:
    async def test_server_error_on_the_search_is_retried(self, fake_network):
        fake_network.get(
            TestMorningStar.SCREENER, status(503), morningstar("screener_US88160R1014.json")
        )
        fake_network.get(TestMorningStar.OVERVIEW, morningstar("overview_0P0000OQN8.json"))
        fake_network.get(TestMorningStar.PROFILE, morningstar("profile_0P0000OQN8.json"))
        fake_network.get(TestMorningStar.TRADING, morningstar("trading_0P0000OQN8.json"))

        metrics = await MorningStarProvider().get_financials(TestMorningStar.TESLA)

        assert metrics.morningstar_id == "0P0000OQN8"
        assert fake_network.calls(TestMorningStar.SCREENER) == 2
        assert fake_network.backoffs == [1.0]
