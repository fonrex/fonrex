import asyncio
import unittest
from types import SimpleNamespace

from financials.models import StandardFinancials
from financials.provider_runner import FinancialProviderRunner


class FastProvider:
    """Answers about whatever it is asked: its page shows no ISIN of its own."""

    async def get_financials(self, ticker):
        await asyncio.sleep(0.05)
        return StandardFinancials(revenue=1.0, provider_url=f"https://example.test/{ticker}")


class EchoProvider:
    async def get_financials(self, ticker):
        return StandardFinancials(isin=ticker)


class ParallelProbeProvider:
    active = 0
    max_active = 0

    @classmethod
    def reset(cls):
        cls.active = 0
        cls.max_active = 0

    async def get_financials(self, ticker):
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            await asyncio.sleep(0.05)
            return StandardFinancials(
                isin="US0378331005", provider_url=f"https://example.test/{ticker}"
            )
        finally:
            type(self).active -= 1


class SlowProvider:
    async def get_financials(self, ticker):
        await asyncio.sleep(0.2)
        return StandardFinancials(revenue=1.0)


class FinancialProviderRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_async_providers_in_parallel_and_filters_default_fields(self):
        ParallelProbeProvider.reset()
        runner = FinancialProviderRunner(
            {
                "Fast": {"type": "async", "class": ParallelProbeProvider},
                "Other": {"type": "async", "class": ParallelProbeProvider},
            }
        )

        results, raw_providers = await runner.run(
            ticker="AAPL", isin=None, provider_params=[], asset_mappings={}
        )

        self.assertEqual(ParallelProbeProvider.max_active, 2)
        self.assertEqual(raw_providers["Fast"], "https://example.test/AAPL")
        self.assertEqual(results["Fast"]["isin"], "US0378331005")
        self.assertNotIn("provider_url", results["Fast"])
        self.assertEqual(results["Other"]["isin"], "US0378331005")

    async def test_requested_provider_exposes_extra_fields_and_uses_active_mapping(self):
        runner = FinancialProviderRunner({"Fast": {"type": "async", "class": FastProvider}})
        mapping = SimpleNamespace(
            provider_url="https://mapped.example/aapl",
            provider_ticker=None,
            is_active=True,
        )

        results, raw_providers = await runner.run(
            ticker="AAPL", isin=None, provider_params=["fast"], asset_mappings={"fast": mapping}
        )

        self.assertEqual(raw_providers["Fast"], "https://example.test/https://mapped.example/aapl")
        self.assertEqual(
            results["Fast"]["provider_url"], "https://example.test/https://mapped.example/aapl"
        )

    async def test_provider_specific_default_ticker_is_used_before_generic_ticker(self):
        runner = FinancialProviderRunner({"Msn": {"type": "async", "class": FastProvider}})

        results, raw_providers = await runner.run(
            ticker="US88160R1014",
            isin="US88160R1014",
            provider_params=["msn"],
            asset_mappings={},
            provider_default_tickers={"msn": "TSLA"},
        )

        self.assertEqual(raw_providers["Msn"], "https://example.test/TSLA")
        self.assertEqual(results["Msn"]["provider_url"], "https://example.test/TSLA")

    async def test_active_mapping_wins_over_provider_specific_default_ticker(self):
        runner = FinancialProviderRunner({"Msn": {"type": "async", "class": FastProvider}})
        mapping = SimpleNamespace(
            provider_url=None,
            provider_ticker="MSFT",
            is_active=True,
        )

        results, raw_providers = await runner.run(
            ticker="US88160R1014",
            isin="US88160R1014",
            provider_params=["msn"],
            asset_mappings={"msn": mapping},
            provider_default_tickers={"msn": "TSLA"},
        )

        self.assertEqual(raw_providers["Msn"], "https://example.test/MSFT")
        self.assertEqual(results["Msn"]["provider_url"], "https://example.test/MSFT")

    async def test_investing_uses_isin_before_generic_ticker(self):
        runner = FinancialProviderRunner({"Investing": {"type": "async", "class": EchoProvider}})

        results, raw_providers = await runner.run(
            ticker="XCA", isin="FR0000045072", provider_params=[], asset_mappings={}
        )

        self.assertEqual(raw_providers["Investing"], "ISIN: FR0000045072")
        self.assertEqual(results["Investing"]["isin"], "FR0000045072")

    async def test_wsj_uses_isin_before_generic_ticker(self):
        runner = FinancialProviderRunner(
            {"wallStreetJournal": {"type": "async", "class": EchoProvider}}
        )

        results, raw_providers = await runner.run(
            ticker="XCA", isin="FR0000045072", provider_params=[], asset_mappings={}
        )

        self.assertEqual(raw_providers["wallStreetJournal"], "ISIN: FR0000045072")
        self.assertEqual(results["wallStreetJournal"]["isin"], "FR0000045072")

    async def test_marketwatch_uses_isin_before_generic_ticker(self):
        runner = FinancialProviderRunner({"Marketwatch": {"type": "async", "class": EchoProvider}})

        results, raw_providers = await runner.run(
            ticker="XCA", isin="FR0000045072", provider_params=[], asset_mappings={}
        )

        self.assertEqual(raw_providers["Marketwatch"], "ISIN: FR0000045072")
        self.assertEqual(results["Marketwatch"]["isin"], "FR0000045072")

    async def test_fortuneo_uses_isin_before_generic_ticker(self):
        runner = FinancialProviderRunner({"Fortuneo": {"type": "async", "class": EchoProvider}})

        results, raw_providers = await runner.run(
            ticker="XCA", isin="FR0000045072", provider_params=[], asset_mappings={}
        )

        self.assertEqual(raw_providers["Fortuneo"], "ISIN: FR0000045072")
        self.assertEqual(results["Fortuneo"]["isin"], "FR0000045072")

    async def test_timeout_is_reported_per_provider(self):
        runner = FinancialProviderRunner(
            {"Slow": {"type": "async", "class": SlowProvider}}, timeout_seconds=0.01
        )

        results, _ = await runner.run(
            ticker="AAPL", isin=None, provider_params=[], asset_mappings={}
        )

        self.assertEqual(results["Slow"], {"error": "Provider timeout"})

    async def test_answer_about_another_instrument_is_refused(self):
        """Searched by ticker, a site may answer with a homonym: "SPFF" is also a US fund.

        Its figures used to be returned under the ISIN of the instrument asked for.
        """

        class Homonym:
            async def get_financials(self, ticker):
                return StandardFinancials(isin="US37950E3339", eps=1.2)

        class SameInstrument:
            async def get_financials(self, ticker):
                return StandardFinancials(isin="ie000aq7a2x6", eps=0.4)

        class NoIsinOnThePage:
            async def get_financials(self, ticker):
                return StandardFinancials(eps=0.5)

        runner = FinancialProviderRunner(
            {
                "Msn": {"type": "async", "class": Homonym},
                "Barrons": {"type": "async", "class": SameInstrument},
                "GoogleFinance": {"type": "async", "class": NoIsinOnThePage},
                # The ISIN yfinance reports for a verified symbol is not checked.
                "YahooFinance": {"type": "async", "class": Homonym},
            }
        )

        results, _ = await runner.run(
            ticker="SPFF", isin="IE000AQ7A2X6", provider_params=[], asset_mappings={}
        )

        self.assertEqual(
            results["Msn"],
            {"error": "Another instrument was found (ISIN US37950E3339, expected IE000AQ7A2X6)"},
        )
        self.assertEqual(results["Barrons"], {"eps": 0.4, "isin": "IE000AQ7A2X6"})
        self.assertEqual(results["GoogleFinance"], {"eps": 0.5, "isin": "IE000AQ7A2X6"})
        self.assertEqual(results["YahooFinance"]["eps"], 1.2)

    async def test_answer_about_another_instrument_is_refused_with_profile_only_isin(self):
        class Homonym:
            async def get_financials(self, ticker):
                return StandardFinancials(isin="US37950E3339", eps=1.2)

        class SameInstrument:
            async def get_financials(self, ticker):
                return StandardFinancials(isin="ie000aq7a2x6", eps=0.4)

        runner = FinancialProviderRunner(
            {
                "Msn": {"type": "async", "class": Homonym},
                "Barrons": {"type": "async", "class": SameInstrument},
            }
        )

        results, _ = await runner.run(
            ticker="SPFF",
            isin=None,
            provider_params=[],
            asset_mappings={},
            asset_profile={"isin": "IE000AQ7A2X6"},
        )

        self.assertEqual(
            results["Msn"],
            {"error": "Another instrument was found (ISIN US37950E3339, expected IE000AQ7A2X6)"},
        )
        self.assertEqual(results["Barrons"], {"eps": 0.4, "isin": "IE000AQ7A2X6"})

    async def test_investir_les_echos_is_searched_by_isin(self):
        runner = FinancialProviderRunner(
            {"InvestirLesEchos": {"type": "async", "class": EchoProvider}}
        )

        _, raw_providers = await runner.run(
            ticker="SPFF", isin="IE000AQ7A2X6", provider_params=[], asset_mappings={}
        )

        self.assertEqual(raw_providers["InvestirLesEchos"], "ISIN: IE000AQ7A2X6")

    async def test_verified_symbol_is_used_instead_of_anything_guessed(self):
        """``EUCO`` on Xetra is ``SYBC.DE`` on Yahoo: no rule on the ticker gives it."""
        runner = FinancialProviderRunner({"YahooFinance": {"type": "async", "class": EchoProvider}})
        mapping = SimpleNamespace(
            provider_url="https://finance.yahoo.com/quote/EUCO", provider_ticker="EUCO"
        )

        results, raw_providers = await runner.run(
            ticker="EUCO",
            isin=None,
            provider_params=[],
            asset_mappings={"yahoofinance": mapping},
            asset_profile={"ticker": "EUCO", "exchange": "XETRA"},
            verified_symbols={"yahoofinance": "SYBC.DE"},
        )

        self.assertEqual(results["YahooFinance"]["isin"], "SYBC.DE")
        self.assertEqual(raw_providers["YahooFinance"], "Verified symbol: SYBC.DE")

    async def test_refused_provider_is_not_queried_and_says_why(self):
        queried = []

        class Recorder:
            async def get_financials(self, ticker):
                queried.append(ticker)
                return StandardFinancials(revenue=1.0)

        runner = FinancialProviderRunner(
            {
                "YahooFinance": {"type": "async", "class": Recorder},
                "Other": {"type": "async", "class": Recorder},
            }
        )
        reason = "No Yahoo symbol quoted in CHF for ISIN IE00B3S5XW04"

        results, raw_providers = await runner.run(
            ticker="GOVY",
            isin=None,
            provider_params=[],
            asset_mappings={},
            refused_providers={"yahoofinance": reason},
        )

        self.assertEqual(queried, ["GOVY"])  # the other provider only
        self.assertEqual(results["YahooFinance"], {"error": reason})
        self.assertEqual(raw_providers["YahooFinance"], f"Not queried: {reason}")


if __name__ == "__main__":
    unittest.main()
