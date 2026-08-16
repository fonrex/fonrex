import unittest

from financials.providers.Fortuneo_provider import FortuneoProvider


class FortuneoProviderTest(unittest.TestCase):
    def test_search_result_from_official_item_builds_provider_url(self):
        item = {
            "codeRef": "FTN000023FR0000045072",
            "codeIsin": "FR0000045072",
            "mnemo": "ACA",
            "libelle": "CREDIT AGRICOLE",
            "type": "Action",
            "place": "Euronext Paris",
            "codePlace": 23,
            "cours": 17.03,
            "devise": "EUR",
            "variation": -0.0073,
        }

        result = FortuneoProvider._search_result_from_item(item, "FR0000045072")

        self.assertEqual(result["ticker"], "ACA")
        self.assertEqual(result["isin"], "FR0000045072")
        self.assertEqual(
            result["provider_url"],
            "https://bourse.fortuneo.fr/actions/cours-credit-agricole-ACA-FR0000045072-23",
        )

    def test_select_item_reads_fortuneo_market_and_prefers_priced_entry(self):
        data = {
            "market": {
                "fortuneo": {
                    "total": 2,
                    "items": [
                        {
                            "codeIsin": "FR0000045072",
                            "mnemo": "ACA",
                            "libelle": "CREDIT AGRICOLE",
                            "type": "Action",
                            "place": "Euronext Paris",
                            "codePlace": 23,
                            "cours": None,
                            "devise": None,
                            "variation": None,
                        },
                        {
                            "codeIsin": "FR0000045072",
                            "mnemo": "ACA",
                            "libelle": "CREDIT AGRICOLE",
                            "type": "Action",
                            "place": "Euronext Paris",
                            "codePlace": 23,
                            "cours": 17.03,
                            "devise": "EUR",
                            "variation": -0.0073,
                        },
                    ],
                }
            }
        }

        item = FortuneoProvider._select_item(data, "FR0000045072")

        self.assertEqual(item["cours"], 17.03)

    def test_metrics_can_fall_back_to_search_result_when_page_is_blocked(self):
        metrics = FortuneoProvider._metrics_from_search_result(
            {
                "provider_url": "https://bourse.fortuneo.fr/actions/cours-credit-agricole-ACA-FR0000045072-23",
                "name": "CREDIT AGRICOLE",
                "ticker": "ACA",
                "isin": "FR0000045072",
                "exchange": "Euronext Paris",
                "instrument_type": "Action",
                "currency": "EUR",
                "price": 17.03,
                "change_percent": -0.0073,
            },
            "FR0000045072",
        )

        self.assertEqual(metrics.ticker, "ACA")
        self.assertEqual(metrics.isin, "FR0000045072")
        self.assertEqual(metrics.name, "CREDIT AGRICOLE")
        self.assertEqual(
            metrics.provider_url,
            "https://bourse.fortuneo.fr/actions/cours-credit-agricole-ACA-FR0000045072-23",
        )


if __name__ == "__main__":
    unittest.main()
