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


# ── Google Finance ───────────────────────────────────────────────────────────


class TestGoogleFinance:
    QUOTE = "google.com/finance/quote/"

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
            **({} if provider_class is BarronsProvider else {"exchange": "XNAS"}),
            **({} if provider_class is BarronsProvider else {"exchange_name": "Nasdaq"}),
            **({} if provider_class is BarronsProvider else {"instrument_type": "stock"}),
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
    SEARCH = "api.investing.com/api/search/v2/search"
    PAGE = "https://www.investing.com/equities/apple-computer-inc"

    async def test_search_then_page(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert metrics.eps == pytest.approx(8.72)
        assert metrics.provider_url == self.PAGE
        assert metrics.exchange == "NASDAQ"
        assert fake_network.requests[1].headers["Referer"] == "https://www.investing.com/"

    async def test_cloudflare_challenge_keeps_the_search_data(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
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
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
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
            "  <dt>Next Earnings Date</dt>\n  <dd>Jan 29, 2027</dd>\n"
            "</dl></body></html>"
        )
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(page))

        metrics = await InvestingProvider().get_financials("AAPL")

        assert metrics.name == "Apple Inc"
        assert metrics.pe_ratio == pytest.approx(30.5)
        assert metrics.eps == pytest.approx(8.72)
        assert metrics.dividend_rate == pytest.approx(1.04)
        assert metrics.dividend_yield == pytest.approx(0.41)
        assert (metrics.week_52_low, metrics.week_52_high) == (164.08, 260.10)
        assert metrics.market_cap == pytest.approx(3.86e12)

    async def test_isin_query_is_recorded(self, fake_network):
        fake_network.get(self.SEARCH, ok_json(INVESTING_QUOTES))
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("US0378331005")

        assert metrics.isin == "US0378331005"

    async def test_page_path_skips_the_search(self, fake_network):
        fake_network.get(self.PAGE, ok(INVESTING_PAGE))

        metrics = await InvestingProvider().get_financials("/equities/apple-computer-inc")

        assert metrics.pe_ratio == pytest.approx(38.25)
        assert fake_network.calls(self.SEARCH) == 0

    @pytest.mark.parametrize(
        "search_response",
        [CLOUDFLARE_CHALLENGE, ok("not json"), ok_json({"quotes": []}), httpx.ConnectError("down")],
        ids=["http-403", "not-json", "no-quote", "network-error"],
    )
    async def test_unusable_search_gives_none(self, fake_network, search_response):
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


class TestMsn:
    SEARCH = "services.bingapis.com/contentservices-finance.csautosuggest/api/v1/Query"
    PAGE = "https://www.msn.com/fr-ca/finances/details-de-l-action/air-fr-stock/fi-ae7bur?id=ae7bur"

    async def test_search_then_page(self, fake_network):
        fake_network.get(self.SEARCH, ok(fixture("search/msn_ticker.json")))
        fake_network.get("msn.com/fr-ca/finances", ok(fixture("msn_air.html")))

        metrics = await MsnProvider().get_financials("AIR")

        assert metrics.name == "Airbus SE"
        assert metrics.provider_url == self.PAGE
        assert fake_network.urls("msn.com") == [self.PAGE]
        assert "query=AIR" in fake_network.urls(self.SEARCH)[0]

    async def test_stock_entry_already_decoded(self, fake_network):
        entry = json.loads(json.loads(fixture("search/msn_ticker.json"))["data"]["stocks"][0])
        fake_network.get(self.SEARCH, ok_json({"data": {"stocks": [entry]}}))
        fake_network.get("msn.com/fr-ca/finances", ok(fixture("msn_air.html")))

        metrics = await MsnProvider().get_financials("AIR")

        assert metrics.provider_url == self.PAGE

    async def test_isin_query_returns_no_stock(self, fake_network):
        # Real answer of the API to an ISIN query.
        fake_network.get(self.SEARCH, ok_json({"count": 1, "data": {"stocks": []}}))

        assert await MsnProvider().get_financials(AIRBUS_ISIN) is None
        assert fake_network.calls("msn.com") == 0

    @pytest.mark.parametrize(
        "page_response",
        [status(500), status(404), httpx.ReadTimeout("timed out")],
        ids=["http-500", "http-404", "timeout"],
    )
    async def test_page_failure_gives_none(self, fake_network, page_response):
        fake_network.get(self.SEARCH, ok(fixture("search/msn_ticker.json")))
        fake_network.get("msn.com/fr-ca/finances", page_response)

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
        ],
        ids=[
            "literal",
            "iife-parameter",
            "zero-not-applicable",
            "unknown-parameter",
            "not-an-iife",
            "unparseable-args",
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
