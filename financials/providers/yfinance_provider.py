import logging
from typing import Optional

import yfinance as yf

from concurrency import run_sync
from financials.models import FinancialMetrics
from financials.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class YFinanceProvider(BaseProvider):
    """
    Implémentation du provider utilisant yfinance.
    """

    async def get_financials(self, ticker: str) -> Optional[FinancialMetrics]:
        try:
            # Handle URL input
            if ticker.startswith("http"):
                # Extract ticker from URL: .../quote/AAPL -> AAPL
                # .../quote/AAPL?p=AAPL -> AAPL
                if "/quote/" in ticker:
                    ticker = ticker.split("/quote/", 1)[1].split("?", 1)[0].rstrip("/")

            # Exécution dans un thread séparé pour ne pas bloquer la boucle d'événements
            return await run_sync(self._fetch_data, ticker)
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des données yfinance pour {ticker}: {e}")
            return None

    def _fetch_data(self, ticker: str) -> Optional[FinancialMetrics]:
        stock = yf.Ticker(ticker)
        info = stock.info

        print(info)
        if not info:
            return None

        # Mapping des champs yfinance vers FinancialMetrics
        data = info.copy()

        # Ajout explicite de l'ISIN
        try:
            val = stock.isin
            if val and val != "-":
                data["isin"] = val
        except Exception:
            pass

        # Ajout des détenteurs (Holders)
        try:
            holders = {}
            if hasattr(stock, "institutional_holders") and stock.institutional_holders is not None:
                holders["institutions"] = stock.institutional_holders.to_dict("records")
            if hasattr(stock, "mutualfund_holders") and stock.mutualfund_holders is not None:
                holders["funds"] = stock.mutualfund_holders.to_dict("records")
            data["holders"] = holders
        except Exception as e:
            logger.warning(f"Erreur extraction holders pour {ticker}: {e}")

        data.update(
            {
                "revenue": info.get("totalRevenue"),
                "ebitda": info.get("ebitda"),
                "net_income": info.get("netIncomeToCommon"),
                "eps": info.get("trailingEps"),
                "payout_ratio": info.get("payoutRatio"),
                "debt_to_equity": info.get("debtToEquity"),
                "provider_url": f"https://finance.yahoo.com/quote/{ticker}/",
                "ticker": info.get("symbol") or ticker,
            }
        )

        return FinancialMetrics(**data)
