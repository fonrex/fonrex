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

    def test_select_symbol_prefers_matching_french_isin_over_us_otc(self):
        data = {
            "symbols": [
                {
                    "score": 0.74,
                    "chartingSymbol": "STOCK/US/OOTC/CRARF",
                    "company": "Credit Agricole S.A.",
                    "country": "US",
                    "exchangeIsoCode": "OOTC",
                    "isin": "FR0000045072",
                    "ticker": "CRARF",
                    "type": "Stock",
                },
                {
                    "score": 1,
                    "chartingSymbol": "STOCK/FR/XPAR/ACA",
                    "company": "Credit Agricole S.A.",
                    "country": "FR",
                    "exchange": "France: Euronext Paris",
                    "exchangeIsoCode": "XPAR",
                    "isin": "FR0000045072",
                    "ticker": "FR:ACA",
                    "type": "Stock",
                },
            ]
        }

        symbol = BarronsProvider._select_symbol(data, "FR0000045072")
        result = BarronsProvider._search_result_from_symbol(symbol, "FR0000045072")

        self.assertEqual(result["ticker"], "FR:ACA")
        self.assertEqual(result["url_ticker"], "ACA")
        self.assertEqual(result["country"], "FR")
        self.assertEqual(result["exchange"], "XPAR")
        self.assertEqual(result["charting_symbol"], "STOCK/FR/XPAR/ACA")
        self.assertEqual(
            result["provider_url"], "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"
        )

    def test_normalize_query_supports_urls_suffixes_and_prefixed_tickers(self):
        self.assertEqual(
            BarronsProvider._normalize_query(
                "https://www.barrons.com/market-data/stocks/aca?countrycode=fr"
            ),
            ("FR:ACA", "FR"),
        )
        self.assertEqual(
            BarronsProvider._normalize_query(
                "https://www.barrons.com/market-data/stocks/sap?countrycode=de"
            ),
            ("XE:SAP", "XE"),
        )
        self.assertEqual(
            BarronsProvider._normalize_query("https://www.barrons.com/market-data/stocks/aapl"),
            ("AAPL", "US"),
        )
        self.assertEqual(BarronsProvider._normalize_query("ACA.PA"), ("FR:ACA", "FR"))
        self.assertEqual(BarronsProvider._normalize_query("DE:SAP"), ("XE:SAP", "XE"))
        self.assertEqual(BarronsProvider._normalize_query("   "), ("", None))


if __name__ == "__main__":
    unittest.main()
