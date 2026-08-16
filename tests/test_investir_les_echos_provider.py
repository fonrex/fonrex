import unittest

from financials.providers.InvestirLesEchos_provider import InvestirLesEchosProvider


class InvestirLesEchosProviderTest(unittest.TestCase):
    def test_select_hit_skips_rows_without_mic_and_prefers_xnas(self):
        data = {
            "categories": [
                {
                    "hits": [
                        {
                            "fields": {
                                "DISPLAY_NAME": {"v": "Tesla"},
                                "ISIN": {"v": "US88160R1014"},
                                "M_SYMB": {"v": "TSLA"},
                            }
                        },
                        {
                            "fields": {
                                "DISPLAY_NAME": {"v": "Tesla"},
                                "ISIN": {"v": "US88160R1014"},
                                "MIC": {"v": "XNAS"},
                                "M_SYMB": {"v": "TSLA"},
                            }
                        },
                    ]
                }
            ]
        }

        hit = InvestirLesEchosProvider._select_hit(data, "TSLA")
        result = InvestirLesEchosProvider._search_result_from_hit(hit)

        self.assertEqual(result["ticker"], "TSLA")
        self.assertEqual(result["isin"], "US88160R1014")
        self.assertEqual(result["mic"], "XNAS")
        self.assertEqual(
            result["url"],
            "https://investir.lesechos.fr/cours/actions/tesla-tsla-us88160r1014-xnas",
        )

    def test_metrics_can_be_returned_from_search_result_without_page_parse(self):
        metrics = InvestirLesEchosProvider._metrics_from_search_result(
            {
                "url": "https://investir.lesechos.fr/cours/actions/tesla-tsla-us88160r1014-xnas",
                "name": "Tesla",
                "ticker": "TSLA",
                "isin": "US88160R1014",
            },
            "TSLA",
        )

        self.assertEqual(metrics.name, "Tesla")
        self.assertEqual(metrics.ticker, "TSLA")
        self.assertEqual(metrics.isin, "US88160R1014")
        self.assertEqual(
            metrics.provider_url,
            "https://investir.lesechos.fr/cours/actions/tesla-tsla-us88160r1014-xnas",
        )


if __name__ == "__main__":
    unittest.main()
