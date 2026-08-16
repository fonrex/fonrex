import asyncio
import unittest
from types import SimpleNamespace

from financials.models import StandardFinancials
from financials.provider_runner import FinancialProviderRunner


class FastProvider:
    async def get_financials(self, ticker):
        await asyncio.sleep(0.05)
        return StandardFinancials(
            isin="US0378331005", provider_url=f"https://example.test/{ticker}"
        )


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


if __name__ == "__main__":
    unittest.main()
