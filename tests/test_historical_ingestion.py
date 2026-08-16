import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import asyncio
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_cache_service import FakeRedis

from historical.ingestion_service import HistoricalIngestionService
from models import Asset, Base


class IngestionFakeRedis(FakeRedis):
    async def scan(self, cursor=0, match=None, count=100):
        prefix = match.rstrip("*")
        keys = [key for key in self.store if key.startswith(prefix)]
        return 0, keys

    async def delete(self, *keys):
        return super().delete(*keys)


class TestHistoricalIngestion(unittest.TestCase):
    def setUp(self):
        # 1. In-memory SQLite test database
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine))

        # 2. Mock DatabaseService
        self.db_service = MagicMock()
        self.db_service.get_session.side_effect = lambda: self.Session()
        self.db_service.async_session = MagicMock()

        # 3. Create a test asset
        session = self.Session()
        self.asset = Asset(
            id=1, ticker="AAPL", name="Apple Inc", isin="US0378331005", is_active=True
        )
        session.add(self.asset)
        session.commit()
        session.close()

        # 4. Mock Redis
        self.redis_client = IngestionFakeRedis()

        # 5. QueryService mock
        self.query_service = MagicMock()
        self.query_service.get_asset_id.return_value = 1

        # 6. Service under test
        self.ingestion_service = HistoricalIngestionService(
            db_service=self.db_service,
            query_service=self.query_service,
            redis_client=self.redis_client,
        )

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_normalize_bars_swaps_high_low(self):
        """Checks that normalization swaps high and low if necessary and corrects negative volumes."""
        bars = [
            {
                "timestamp": datetime(2026, 1, 1),
                "open": 100.0,
                "high": 90.0,  # Invalid: high < open / low
                "low": 110.0,  # Invalid: low > high / open
                "close": 105.0,
                "volume": -500,  # Invalid: negative volume
                "resolution": "1D",
            }
        ]

        normalized = self.ingestion_service._normalize_bars(
            bars, asset_id=1, listing_id=None, resolution="1D"
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["high"], 110.0)
        self.assertEqual(normalized[0]["low"], 90.0)
        self.assertEqual(normalized[0]["volume"], 0)

    def test_normalize_bars_handles_nan_and_none(self):
        """Checks that None and NaN values are correctly ignored or filtered."""
        bars = [
            {
                "timestamp": datetime(2026, 1, 1),
                "open": None,
                "high": 100.0,
                "low": 90.0,
                "close": 95.0,
                "volume": 1000,
                "resolution": "1D",
            },
            {
                "timestamp": datetime(2026, 1, 2),
                "open": 100.0,
                "high": float("nan"),
                "low": 90.0,
                "close": 95.0,
                "volume": 1000,
                "resolution": "1D",
            },
        ]

        normalized = self.ingestion_service._normalize_bars(
            bars, asset_id=1, listing_id=None, resolution="1D"
        )
        # Both bars have None/NaN values, they must be excluded
        self.assertEqual(len(normalized), 0)

    @patch("historical.providers.yf.Ticker")
    def test_ingest_yfinance_success(self, mock_yf_ticker):
        """Checks the nominal behavior of ingest_yfinance."""
        # Configure the yfinance mock
        mock_instance = MagicMock()
        mock_yf_ticker.return_value = mock_instance

        # Simulate a pandas DataFrame returned by history()
        history_df = pd.DataFrame(
            {
                "Open": [150.0, 152.0],
                "High": [153.0, 155.0],
                "Low": [149.0, 151.0],
                "Close": [152.0, 154.0],
                "Adj Close": [152.0, 154.0],
                "Volume": [100000, 200000],
            },
            index=pd.DatetimeIndex([datetime(2026, 5, 18), datetime(2026, 5, 19)]),
        )

        mock_instance.history.return_value = history_df

        # Run yfinance ingestion while mocking the upsert
        with patch.object(self.ingestion_service, "_upsert_prices_eod", return_value=2):
            result = asyncio.run(
                self.ingestion_service._fetch_yfinance(
                    ticker="AAPL", resolution="1D", start=date(2026, 5, 1), end=date(2026, 5, 20)
                )
            )

            self.assertEqual(result["source_used"], "yfinance")
            self.assertEqual(len(result["bars"]), 2)

    def test_clear_cache_deletes_redis_keys(self):
        """Checks that clear_cache deletes the corresponding Redis keys."""
        # Pre-populate the cache
        self.redis_client.store["history:AAPL:1D:None:None"] = "data"
        self.redis_client.store["history:AAPL:1W:2026-01-01:2026-05-20"] = "data"
        self.redis_client.store["history:TSLA:1D:None:None"] = "data"

        # Call cache invalidation for AAPL
        asyncio.run(self.ingestion_service._invalidate_cache("AAPL"))

        # Check that only AAPL keys were deleted
        self.assertNotIn("history:AAPL:1D:None:None", self.redis_client.store)
        self.assertNotIn("history:AAPL:1W:2026-01-01:2026-05-20", self.redis_client.store)
        self.assertIn("history:TSLA:1D:None:None", self.redis_client.store)


if __name__ == "__main__":
    unittest.main()
