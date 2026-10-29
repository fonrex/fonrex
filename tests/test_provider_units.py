"""Unit normalisation between provider payloads and monitoring ranges.

Scraped providers return percentages (``3.45`` for 3.45 %) while the
monitoring ranges are ratios (``0.0345``). Each provider is exercised through
its real HTML parser so that a change of unit on either side is caught here.
"""

from decimal import Decimal

import pytest
from selectolax.parser import HTMLParser

from financials.models import FinancialMetrics
from financials.providers.Barrons_provider import BarronsProvider
from financials.providers.boursorama_provider import BoursoramaProvider
from financials.providers.GoogleFinance_provider import GoogleFinanceProvider
from financials.providers.Investing_provider import InvestingProvider
from financials.providers.Marketwatch_provider import MarketwatchProvider
from financials.providers.MorningStar_provider import MorningStarProvider
from financials.providers.wallStreetJournal_provider import WallStreetJournalProvider
from financials.providers.ZoneBourse_provider import ZoneBourseProvider
from monitoring import canary_monitor
from monitoring.canary_catalog import CANARY_ASSETS, MONITORED_PROVIDERS
from monitoring.canary_monitor import CanaryMonitor
from monitoring.units import PROVIDER_PERCENT_FIELDS, percent_fields, to_ratio
from monitoring.validation_layer import FIELD_RANGES, ValidationLayer
from schemas.monitoring import HealthStatus

FRENCH_TABLE_HTML = """
<html><body><h1>Airbus</h1><table>
  <tr><th>PER</th><td>24,10</td></tr>
  <tr><th>Rendement</th><td>3,45 %</td></tr>
  <tr><th>Marge d'exploitation</th><td>9,8 %</td></tr>
</table></body></html>
"""

GOOGLE_HTML = """
<html><body><h1>Airbus SE</h1>
  <div><div>P/E ratio</div><div>24.10</div></div>
  <div><div>Dividend yield</div><div>3.45%</div></div>
  <table><tr><td>Operating margin</td><td>9.8%</td></tr></table>
</body></html>
"""

US_LABEL_HTML = """
<html><body><h1>Apple</h1><ul>
  <li>P/E Ratio: 30.5</li>
  <li>Dividend Yield: 3.45%</li>
</ul></body></html>
"""

INVESTING_HTML = """
<html><body><h1>Apple</h1><dl>
  <dt>P/E Ratio</dt><dd>30.5</dd>
  <dt>Dividend Yield</dt><dd>3.45%</dd>
</dl></body></html>
"""


def _parse(provider_name: str):
    """Run a provider's real HTML parser on a page showing a 3.45 % yield."""
    if provider_name == "Boursorama":
        return BoursoramaProvider()._parse_page(HTMLParser(FRENCH_TABLE_HTML), "AIR")
    if provider_name == "ZoneBourse":
        return ZoneBourseProvider()._parse_page(
            HTMLParser(FRENCH_TABLE_HTML), "AIR", "https://example.test/airbus"
        )
    if provider_name == "GoogleFinance":
        return GoogleFinanceProvider()._parse_page(HTMLParser(GOOGLE_HTML), "AIR:EPA")
    if provider_name == "Barrons":
        return BarronsProvider()._parse_page(HTMLParser(US_LABEL_HTML), "AAPL")
    if provider_name == "Marketwatch":
        return MarketwatchProvider()._parse_page(HTMLParser(US_LABEL_HTML), "AAPL")
    if provider_name == "wallStreetJournal":
        return WallStreetJournalProvider()._parse_page(HTMLParser(US_LABEL_HTML), "AAPL")
    if provider_name == "Investing":
        return InvestingProvider()._parse_page(HTMLParser(INVESTING_HTML), "AAPL")
    raise AssertionError(f"No HTML fixture for provider {provider_name}")


def _as_payload(parsed) -> dict:
    """Mirror what FinancialProviderRunner hands to the ValidationLayer."""
    if isinstance(parsed, dict):
        return dict(parsed)
    return parsed.model_dump(exclude_none=True)


class RecordingRepository:
    """Validation log port that keeps the entries in memory."""

    def __init__(self):
        self.entries = []

    async def save_validation_logs(self, entries):
        self.entries.extend(entries)


KNOWN_YIELD_PROVIDERS = {
    "Barrons",
    "Boursorama",
    "GoogleFinance",
    "Investing",
    "Marketwatch",
    "ZoneBourse",
    "wallStreetJournal",
}

PERCENT_YIELD_PROVIDERS = sorted(
    KNOWN_YIELD_PROVIDERS
    | {name for name, fields in PROVIDER_PERCENT_FIELDS.items() if "dividend_yield" in fields}
)


class TestProviderUnitDeclarations:
    def test_every_monitored_provider_declares_its_units(self):
        missing = set(MONITORED_PROVIDERS) - set(PROVIDER_PERCENT_FIELDS)
        assert not missing, f"Declare the units of {sorted(missing)} in monitoring/units.py"

    def test_percent_fields_are_validated_fields(self):
        for provider, fields in PROVIDER_PERCENT_FIELDS.items():
            unknown = fields - set(FIELD_RANGES)
            assert not unknown, f"{provider} declares fields without a range: {sorted(unknown)}"

    def test_every_percent_yield_provider_has_a_parser_fixture(self):
        for provider in PERCENT_YIELD_PROVIDERS:
            assert _parse(provider) is not None

    def test_provider_name_lookup_is_case_insensitive(self):
        assert percent_fields("boursorama") == percent_fields("Boursorama")
        assert "dividend_yield" in percent_fields("WALLSTREETJOURNAL")

    def test_unknown_provider_is_assumed_to_return_ratios(self):
        assert percent_fields("SomeNewProvider") == frozenset()
        assert to_ratio("SomeNewProvider", "dividend_yield", Decimal("0.03")) == Decimal("0.03")

    def test_to_ratio_keeps_none(self):
        assert to_ratio("Boursorama", "dividend_yield", None) is None

    def test_only_declared_fields_are_converted(self):
        assert to_ratio("Boursorama", "dividend_yield", Decimal("3.45")) == Decimal("0.0345")
        assert to_ratio("Boursorama", "pe_ratio", Decimal("24.1")) == Decimal("24.1")
        assert to_ratio("YahooFinance", "payout_ratio", Decimal("0.15")) == Decimal("0.15")


class TestPercentProvidersPassValidation:
    """One test per provider: its parsed yield must survive the range check."""

    @pytest.mark.parametrize("provider_name", PERCENT_YIELD_PROVIDERS)
    async def test_parsed_yield_is_kept_and_logged_as_ratio(self, provider_name):
        payload = _as_payload(_parse(provider_name))
        assert payload["dividend_yield"] == pytest.approx(3.45), (
            f"{provider_name} no longer returns its yield as a percentage: "
            "update PROVIDER_PERCENT_FIELDS in monitoring/units.py"
        )

        repository = RecordingRepository()
        results = await ValidationLayer(repository).validate_results(
            ticker="TEST", results={provider_name: payload}
        )

        # The provider payload keeps its native unit…
        assert results[provider_name]["dividend_yield"] == pytest.approx(3.45)
        # …while the check and the log use the normalised ratio.
        entry = next(e for e in repository.entries if e["field"] == "dividend_yield")
        assert entry["status"] == "ok"
        assert entry["value_received"] == Decimal("0.0345")

    async def test_zonebourse_operating_margin_is_kept(self):
        payload = _as_payload(_parse("ZoneBourse"))
        assert payload["operating_margin"] == pytest.approx(9.8)

        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={"ZoneBourse": payload}
        )

        assert results["ZoneBourse"]["operating_margin"] == pytest.approx(9.8)

    async def test_gurufocus_real_page_passes_the_validation(self):
        """The Tesla summary page: percentages and scores are kept, in their own unit."""
        from pathlib import Path

        from financials.providers.Gurufocus_provider import GurufocusProvider

        html = (Path(__file__).parent / "fixtures/providers/gurufocus_tsla.html").read_text()
        payload = GurufocusProvider()._parse_page(HTMLParser(html), "NAS:TSLA").model_dump()

        results = await ValidationLayer().validate_results(
            ticker="TSLA", results={"Gurufocus": payload}
        )

        kept = results["Gurufocus"]
        assert kept["roic"] == pytest.approx(3.11)
        assert kept["operating_margin"] == pytest.approx(3.5)
        assert kept["piotroski_score"] == 4
        assert kept["beneish_m_score"] == pytest.approx(-2.95)
        assert kept["altman_z_score"] == pytest.approx(14.4)
        # The PER the page displays is within the canary range of TSLA
        low, high = CANARY_ASSETS["TSLA"]["pe_ratio"]
        assert low <= kept["pe_ratio"] <= high
        assert kept["gf_score"] == 88
        assert to_ratio("Gurufocus", "roic", Decimal("3.11")) == Decimal("0.0311")

    @pytest.mark.parametrize("provider_name", ["ZoneBourse", "GoogleFinance", "Gurufocus"])
    async def test_net_margin_in_percent_is_kept(self, provider_name):
        """A 12.5 % net margin is checked as 0.125, not rejected as 12.5."""
        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={provider_name: {"profit_margin": 12.5}}
        )

        assert results[provider_name]["profit_margin"] == pytest.approx(12.5)

    async def test_zonebourse_real_page_passes_the_validation(self):
        from pathlib import Path

        html = (Path(__file__).parent / "fixtures/providers/zonebourse_tsla.html").read_text()
        payload = ZoneBourseProvider()._parse_page(HTMLParser(html), "TSLA", "u").model_dump()

        results = await ValidationLayer().validate_results(
            ticker="TSLA", results={"ZoneBourse": payload}
        )

        kept = results["ZoneBourse"]
        assert kept["price"] == pytest.approx(391.62)
        assert kept["target_price"] == pytest.approx(409.27)
        assert kept["pe_ratio"] == pytest.approx(367)
        low, high = CANARY_ASSETS["TSLA"]["pe_ratio"]
        assert low <= kept["pe_ratio"] <= high

    async def test_investing_real_page_passes_the_validation(self):
        """The French page of Tesla: percentages are checked as ratios and kept."""
        from pathlib import Path

        html = (Path(__file__).parent / "fixtures/providers/investing_tsla.html").read_text()
        payload = InvestingProvider()._parse_page(HTMLParser(html), "TSLA").model_dump()

        results = await ValidationLayer().validate_results(
            ticker="TSLA", results={"Investing": payload}
        )

        kept = results["Investing"]
        assert kept["roe"] == pytest.approx(3.8)
        assert kept["roa"] == pytest.approx(2.3)
        assert kept["gross_margin"] == pytest.approx(19.3)
        assert kept["pb_ratio"] == pytest.approx(18.0)
        assert kept["beta"] == pytest.approx(1.92)
        low, high = CANARY_ASSETS["TSLA"]["pe_ratio"]
        assert low <= kept["pe_ratio"] <= high

    async def test_morningstar_ratios_pass_the_validation_as_given(self):
        """The JSON services give ratios: a 5.58 % yield is 0.055754, not divided again."""
        import json
        from pathlib import Path

        from financials.models import FinancialMetrics

        folder = Path(__file__).parent / "fixtures/providers/morningstar"
        metrics = FinancialMetrics(ticker="0P0000A5ZK")
        MorningStarProvider.read_overview(
            metrics, json.loads((folder / "overview_0P0000A5ZK.json").read_text())
        )

        results = await ValidationLayer().validate_results(
            ticker="SCR", results={"MorningStar": metrics.model_dump()}
        )

        kept = results["MorningStar"]
        assert kept["dividend_yield"] == pytest.approx(0.055754)
        assert kept["pb_ratio"] == pytest.approx(1.389758)
        assert kept["pe_ratio"] == pytest.approx(7.456088)
        assert to_ratio("MorningStar", "dividend_yield", Decimal("0.055754")) == Decimal("0.055754")

    async def test_morningstar_short_float_is_a_percentage(self):
        import json
        from pathlib import Path

        from financials.models import FinancialMetrics

        folder = Path(__file__).parent / "fixtures/providers/morningstar"
        metrics = FinancialMetrics(ticker="0P0000OQN8")
        MorningStarProvider.read_trading(
            metrics, json.loads((folder / "trading_0P0000OQN8.json").read_text())
        )

        results = await ValidationLayer().validate_results(
            ticker="TSLA", results={"MorningStar": metrics.model_dump()}
        )

        kept = results["MorningStar"]
        # 1.9611 % is checked as 0.019611, inside (0, 1), and kept as given
        assert kept["short_percent_float"] == pytest.approx(1.9611)
        assert kept["beta"] == pytest.approx(1.942994)

    async def test_impossible_scores_are_rejected(self):
        results = await ValidationLayer().validate_results(
            ticker="TSLA",
            results={"Gurufocus": {"piotroski_score": 12, "gf_score": 140, "roic": 3.11}},
        )

        assert results["Gurufocus"]["piotroski_score"] is None
        assert results["Gurufocus"]["gf_score"] is None
        assert results["Gurufocus"]["roic"] == pytest.approx(3.11)

    async def test_googlefinance_operating_margin_is_kept(self):
        # Through the real parser: the page shows "9.8%", the parser keeps 9.8.
        payload = _as_payload(_parse("GoogleFinance"))
        assert payload["operating_margin"] == pytest.approx(9.8)

        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={"GoogleFinance": payload}
        )

        assert results["GoogleFinance"]["operating_margin"] == pytest.approx(9.8)

    async def test_pydantic_payload_is_kept(self):
        metrics = FinancialMetrics(dividend_yield=3.45, pe_ratio=24.1)

        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={"Boursorama": metrics}
        )

        assert results["Boursorama"].dividend_yield == pytest.approx(3.45)


class TestNormalisationKeepsRejectingBadValues:
    async def test_implausible_percent_yield_is_still_rejected(self):
        # 75 % is out of the (0, 0.50) ratio range even once converted.
        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={"Boursorama": {"dividend_yield": 75.0}}
        )

        assert results["Boursorama"]["dividend_yield"] is None

    async def test_ratio_provider_is_not_divided(self):
        # A ratio provider returning 3.45 is a real anomaly and must be rejected.
        results = await ValidationLayer().validate_results(
            ticker="AIR.PA", results={"YahooFinance": {"dividend_yield": 3.45}}
        )

        assert results["YahooFinance"]["dividend_yield"] is None

    async def test_consensus_compares_providers_in_the_same_unit(self):
        results = await ValidationLayer().validate_results(
            ticker="AIR.PA",
            results={
                "Boursorama": {"dividend_yield": 3.45},
                "ZoneBourse": {"dividend_yield": 3.50},
                "YahooFinance": {"dividend_yield": 0.0345},
            },
        )

        assert results["Boursorama"]["dividend_yield"] == pytest.approx(3.45)
        assert results["ZoneBourse"]["dividend_yield"] == pytest.approx(3.50)
        assert results["YahooFinance"]["dividend_yield"] == pytest.approx(0.0345)

    async def test_percent_outlier_is_detected_against_consensus(self):
        results = await ValidationLayer().validate_results(
            ticker="AIR.PA",
            results={
                "Boursorama": {"dividend_yield": 3.45},
                "ZoneBourse": {"dividend_yield": 3.50},
                "Marketwatch": {"dividend_yield": 12.0},
            },
        )

        assert results["Marketwatch"]["dividend_yield"] is None
        assert results["Boursorama"]["dividend_yield"] == pytest.approx(3.45)


class TestCanaryUsesNormalisedUnits:
    async def _run(self, monkeypatch, provider_name, dividend_yield):
        class FakeProvider:
            async def get_financials(self, ticker):
                return FinancialMetrics(dividend_yield=dividend_yield)

        monkeypatch.setattr(canary_monitor, "_get_provider_class", lambda name: FakeProvider)
        monkeypatch.setattr(
            canary_monitor,
            "CANARY_ASSETS",
            {"AIR.PA": {"dividend_yield": CANARY_ASSETS["AIR.PA"]["dividend_yield"]}},
        )
        results = await CanaryMonitor().run_provider(provider_name)
        assert len(results) == 1
        return results[0]

    async def test_percent_provider_matches_ratio_canary_range(self, monkeypatch):
        result = await self._run(monkeypatch, "Boursorama", 1.8)

        assert result.status == HealthStatus.ok
        assert result.value_received == Decimal("0.018")

    async def test_percent_provider_out_of_canary_range(self, monkeypatch):
        result = await self._run(monkeypatch, "Boursorama", 18.0)

        assert result.status == HealthStatus.out_of_range

    async def test_ratio_provider_is_compared_as_is(self, monkeypatch):
        result = await self._run(monkeypatch, "YahooFinance", 0.018)

        assert result.status == HealthStatus.ok
        assert result.value_received == Decimal("0.018")
