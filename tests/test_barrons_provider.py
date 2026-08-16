import unittest

from financials.providers.Barrons_provider import BarronsProvider


class BarronsProviderTest(unittest.TestCase):
    def test_select_symbol_prefers_us_exact_stock_match(self):
        data = {
            "symbols": [
                {
                    "score": 0.62,
                    "ticker": "MX:TSLA",
                    "country": "MX",
                    "type": "Stock",
                    "isin": "US88160R1014",
                    "company": "Tesla Inc.",
                },
                {
                    "score": 1,
                    "ticker": "TSLA",
                    "country": "US",
                    "type": "Stock",
                    "isin": "US88160R1014",
                    "company": "Tesla Inc.",
                },
            ]
        }

        symbol = BarronsProvider._select_symbol(data, "TSLA")
        result = BarronsProvider._search_result_from_symbol(symbol, "TSLA")

        self.assertEqual(result["ticker"], "TSLA")
        self.assertEqual(result["url_ticker"], "TSLA")
        self.assertEqual(result["isin"], "US88160R1014")
        self.assertEqual(result["provider_url"], "https://www.barrons.com/market-data/stocks/tsla")

    def test_metrics_can_be_returned_from_search_result_without_page_parse(self):
        metrics = BarronsProvider._metrics_from_search_result(
            {
                "ticker": "TSLA",
                "url_ticker": "TSLA",
                "isin": "US88160R1014",
                "name": "Tesla Inc.",
                "provider_url": "https://www.barrons.com/market-data/stocks/tsla",
            },
            "TSLA",
        )

        self.assertEqual(metrics.name, "Tesla Inc.")
        self.assertEqual(metrics.ticker, "TSLA")
        self.assertEqual(metrics.isin, "US88160R1014")
        self.assertEqual(metrics.provider_url, "https://www.barrons.com/market-data/stocks/tsla")


if __name__ == "__main__":
    unittest.main()
