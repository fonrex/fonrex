import logging
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from monitoring.units import percent_fields

logger = logging.getLogger(__name__)

# Name given, in ``Sources``, to a value read from the figures stored in the database.
STORED = "database"


@dataclass(frozen=True)
class Field:
    """Where a value of the rendered document may come from.

    ``yahoo`` is a key of the Yahoo payload fetched for this request, ``stored``
    a column of the figures kept in the database (an earlier Yahoo answer),
    ``scraped`` a field of the scraped providers that means the same thing (see
    ``LIKE_FOR_LIKE_PROVIDERS``). They are tried in that order: the freshest first.
    """

    stored: Optional[str] = None
    yahoo: Optional[str] = None
    scraped: Optional[str] = None
    # Yahoo publishes this key in percent (0.32 for 0.32 %): the document holds a ratio.
    yahoo_percent: bool = False


# Scraped providers whose price/earnings ratio, earnings per share and dividend
# yield are the trailing figures the document asks for. Boursorama and ZoneBourse
# publish estimates for the current fiscal year, GoogleFinance publishes revenue
# and margins for the last quarter: those are another quantity, not a fallback.
# They stay available as they are with ``fmt=raw``.
LIKE_FOR_LIKE_PROVIDERS = ("GoogleFinance", "Barrons", "Marketwatch", "wallStreetJournal", "Investing")

HIGHLIGHTS = {
    "MarketCapitalization": Field("market_cap", "marketCap"),
    "EBITDA": Field("ebitda_ttm", "ebitda"),
    "PERatio": Field("pe_ratio", "trailingPE", "pe_ratio"),
    "PEGRatio": Field("peg_ratio", "pegRatio"),
    "WallStreetTargetPrice": Field(yahoo="targetMeanPrice"),
    "BookValue": Field("book_value_per_share", "bookValue"),
    "DividendShare": Field("dividend_rate", "dividendRate"),
    "DividendYield": Field("dividend_yield", "dividendYield", "dividend_yield", yahoo_percent=True),
    "EarningsShare": Field("eps_trailing", "trailingEps", "eps"),
    "EPSEstimateCurrentYear": Field(yahoo="forwardEps"),
    "EPSEstimateNextYear": Field(),
    "EPSEstimateNextQuarter": Field(),
    "EPSEstimateCurrentQuarter": Field(),
    "MostRecentQuarter": Field(),
    "ProfitMargin": Field("net_margin", "profitMargins"),
    "OperatingMarginTTM": Field("operating_margin", "operatingMargins"),
    "ReturnOnAssetsTTM": Field("roa", "returnOnAssets"),
    "ReturnOnEquityTTM": Field("roe", "returnOnEquity"),
    "RevenueTTM": Field("revenue_ttm", "totalRevenue"),
    "RevenuePerShareTTM": Field("revenue_per_share", "revenuePerShare"),
    "QuarterlyRevenueGrowthYOY": Field("quarterly_revenue_growth_yoy", "revenueGrowth"),
    "QuarterlyEarningsGrowthYOY": Field("quarterly_earnings_growth_yoy", "earningsGrowth"),
    "GrossProfitTTM": Field("gross_profit_ttm", "grossProfits"),
    "DilutedEpsTTM": Field("diluted_eps_ttm", "trailingEps"),
    "QuarterlyEarningsShareGrowthYOY": Field(),
}

VALUATION = {
    "TrailingPE": Field("pe_ratio", "trailingPE", "pe_ratio"),
    "ForwardPE": Field("pe_forward", "forwardPE"),
    "PriceSalesTTM": Field("ps_ratio", "priceToSalesTrailing12Months"),
    "PriceBookMRQ": Field("pb_ratio", "priceToBook"),
    "EnterpriseValue": Field("enterprise_value", "enterpriseValue"),
    "EnterpriseValueRevenue": Field("ev_revenue", "enterpriseToRevenue"),
    "EnterpriseValueEbitda": Field("ev_ebitda", "enterpriseToEbitda"),
}

SHARES_STATS = {
    "SharesOutstanding": Field("shares_outstanding", "sharesOutstanding"),
    "SharesFloat": Field("float_shares", "floatShares"),
    "PercentInsiders": Field("pct_insiders", "heldPercentInsiders"),
    "PercentInstitutions": Field("pct_institutions", "heldPercentInstitutions"),
    "SharesShort": Field("shares_short", "sharesShort"),
    "SharesShortPriorMonth": Field("shares_short_prior", "sharesShortPriorMonth"),
    "ShortRatio": Field("short_ratio", "shortRatio"),
    "ShortPercentFloat": Field("short_percent_float", "shortPercentOfFloat"),
    "ShortPercentOutstanding": Field("short_percent_outstanding", "sharesPercentSharesOut"),
}

TECHNICALS = {
    "Beta": Field("beta", "beta"),
    "52WeekHigh": Field("week_52_high", "fiftyTwoWeekHigh"),
    "52WeekLow": Field("week_52_low", "fiftyTwoWeekLow"),
    "50DayMA": Field("ma_50", "fiftyDayAverage"),
    "200DayMA": Field("ma_200", "twoHundredDayAverage"),
    "SharesShort": Field("shares_short", "sharesShort"),
    "SharesShortPriorMonth": Field("shares_short_prior", "sharesShortPriorMonth"),
    "ShortRatio": Field("short_ratio", "shortRatio"),
    "ShortPercent": Field("short_percent_float", "shortPercentOfFloat"),
}

SPLITS_DIVIDENDS = {
    "ForwardAnnualDividendRate": Field("dividend_rate", "dividendRate"),
    "ForwardAnnualDividendYield": Field(
        "dividend_yield", "dividendYield", "dividend_yield", yahoo_percent=True
    ),
    "PayoutRatio": Field("payout_ratio", "payoutRatio"),
    "LastSplitFactor": Field(yahoo="lastSplitFactor"),
    "LastSplitDate": Field(yahoo="lastSplitDate"),
}

ANALYST_RATINGS = {
    "Rating": Field("consensus", "recommendationKey"),
    "TargetPrice": Field("target_mean", "targetMeanPrice"),
    "StrongBuy": Field("strong_buy"),
    "Buy": Field("buy"),
    "Hold": Field("hold"),
    "Sell": Field("sell"),
    "StrongSell": Field("strong_sell"),
}


def _known(value: Any) -> bool:
    """A value that says something: zero does; ``None``, NaN and infinity do not."""
    if value is None or value == "":
        return False
    if isinstance(value, Decimal):
        return value.is_finite()
    return not (isinstance(value, float) and not math.isfinite(value))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and _known(value)


def _percent_to_ratio(value: Any) -> float:
    """1.45 (percent) -> 0.0145, without the noise of a binary division."""
    return float(Decimal(str(value)) / 100)


def _as_dict(payload: Any) -> Dict[str, Any]:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    elif hasattr(payload, "dict"):
        payload = payload.dict()
    return payload if isinstance(payload, dict) else {}


class _Chooser:
    """Chooses each value among the sources and remembers which one gave it."""

    def __init__(self, results: Dict[str, Any], yahoo: Dict[str, Any], yahoo_name: str) -> None:
        self._results = results
        self._yahoo = yahoo
        self._yahoo_name = yahoo_name
        self.sources: Dict[str, Dict[str, str]] = {}

    def section(self, name: str, fields: Dict[str, Field], stored: Dict[str, Any]) -> Dict:
        # The stored figures say when they were fetched: "database (2026-10-01)".
        fetched_at = str(stored.get("fetched_at") or "")[:10]
        stored_name = f"{STORED} ({fetched_at})" if fetched_at else STORED
        rendered = {}
        for key, field in fields.items():
            value, source = self._choose(field, stored, stored_name)
            rendered[key] = value
            if source:
                self.sources.setdefault(name, {})[key] = source
        return rendered

    def _choose(
        self, field: Field, stored: Dict[str, Any], stored_name: str
    ) -> tuple[Any, Optional[str]]:
        if field.yahoo:
            value = self._yahoo.get(field.yahoo)
            if field.yahoo_percent:
                if _is_number(value):
                    return _percent_to_ratio(value), self._yahoo_name
            elif _known(value):
                return value, self._yahoo_name
        if field.stored and _known(stored.get(field.stored)):
            return stored[field.stored], stored_name
        if field.scraped:
            for provider in LIKE_FOR_LIKE_PROVIDERS:
                value = self._scraped(provider, field.scraped)
                if value is not None:
                    return value, provider
        return None, None

    def _scraped(self, provider: str, field: str) -> Optional[float]:
        payload = _as_dict(self._results.get(provider))
        value = payload.get(field)
        if payload.get("error") or not _is_number(value):
            return None
        # Scraped pages display some fields in percent; the document holds ratios.
        return _percent_to_ratio(value) if field in percent_fields(provider) else value


class FinancialsFormatter:
    """
    Formateur pour transformer les résultats enrichis (DB + YFinance)
    en une structure strictement identique à l'API Premium EODHD Fundamental Data.
    """

    @staticmethod
    def to_eodhd(results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforme le dictionnaire agrégé en format EODHD.
        """
        asset_profile = results.get("asset_profile") or {}
        # The Yahoo payload of this request, then the figures stored in the
        # database, then the scraped providers for the fields that mean the same thing.
        highlights = _as_dict(results.get("highlights"))
        yahoo_name = "YahooFinance"
        yf_info = results.get("YahooFinance", {})
        if not yf_info or (isinstance(yf_info, dict) and yf_info.get("error")):
            yahoo_name = "YFinanceProvider"
            yf_info = results.get("YFinanceProvider", {})
        yf_info = _as_dict(yf_info)
        if yf_info.get("error"):
            yf_info = {}

        choose = _Chooser(results, yf_info, yahoo_name)
        section_highlights = choose.section("Highlights", HIGHLIGHTS, highlights)
        market_cap = section_highlights["MarketCapitalization"]
        section_highlights = FinancialsFormatter._insert_after(
            section_highlights,
            "MarketCapitalization",
            "MarketCapitalizationMln",
            float(market_cap) / 1e6 if _is_number(market_cap) else None,
        )
        splits_dividends = choose.section("SplitsDividends", SPLITS_DIVIDENDS, highlights)
        splits_dividends = FinancialsFormatter._insert_after(
            splits_dividends,
            "PayoutRatio",
            "DividendDate",
            str(highlights["dividend_pay_date"]) if highlights.get("dividend_pay_date") else None,
        )
        splits_dividends = FinancialsFormatter._insert_after(
            splits_dividends,
            "DividendDate",
            "ExDividendDate",
            str(highlights["dividend_ex_date"]) if highlights.get("dividend_ex_date") else None,
        )
        splits_dividends["NumberDividendsByYear"] = 0

        # Construction du rendu Premium
        rendered = {
            "General": FinancialsFormatter._build_general(asset_profile, yf_info),
            "Highlights": section_highlights,
            "Valuation": choose.section("Valuation", VALUATION, highlights),
            "SharesStats": choose.section("SharesStats", SHARES_STATS, highlights),
            "Technicals": choose.section("Technicals", TECHNICALS, highlights),
            "SplitsDividends": splits_dividends,
            "AnalystRatings": choose.section(
                "AnalystRatings", ANALYST_RATINGS, _as_dict(results.get("analyst_ratings"))
            ),
            "Holders": FinancialsFormatter._build_holders(yf_info),
            "InsiderTransactions": FinancialsFormatter._build_insider_transactions(results),
            "ESGScores": FinancialsFormatter._build_esg_scores(results.get("esg_scores"), results),
            "Earnings": FinancialsFormatter._build_earnings(
                results.get("earnings_history"), results.get("earnings_trend")
            ),
            "Financials": FinancialsFormatter._format_financials(
                results.get("financial_statements")
            ),
            "Providers": FinancialsFormatter._build_providers(results),
            # Which source gave each figure above ("database": stored figures).
            "Sources": choose.sources,
        }

        # Ajout ETF_Data si c'est un ETF
        if (asset_profile.get("quote_type") or "").upper() == "ETF":
            rendered["ETF_Data"] = FinancialsFormatter._build_etf_data(results)

        return rendered

    @staticmethod
    def _build_providers(results: Dict) -> Dict:
        """Construit la section Providers : pour chaque provider appelé, expose
        le ticker utilisé, l'URL interrogée, l'ISIN, le nom et le statut."""
        raw_providers = results.get("raw_providers") or {}
        providers_info = {}

        for provider_name, url_used in raw_providers.items():
            payload = results.get(provider_name)
            entry: Dict[str, Any] = {}

            if isinstance(payload, dict):
                if payload.get("error"):
                    entry["status"] = "error"
                    entry["error"] = payload["error"]
                else:
                    entry["status"] = "ok"
                    for field in ("ticker", "isin", "name", "provider_url", "country"):
                        val = payload.get(field)
                        if val is not None:
                            entry[field] = val
            else:
                entry["status"] = "no_data"

            providers_info[provider_name] = entry

        return providers_info

    @staticmethod
    def _safe_str(val) -> Optional[str]:
        if val is None:
            return None
        if isinstance(val, (int, float, Decimal)):
            return f"{float(val):.4f}".rstrip("0").rstrip(".")
        return str(val)

    @staticmethod
    def _build_general(profile: Dict, yf: Dict) -> Dict:
        # Logo URL logic
        ticker = profile.get("ticker") or yf.get("symbol")
        logo_url = profile.get("logo_path")
        if not logo_url and ticker:
            exchange = profile.get("exchange") or "UNKNOWN"
            clean_ticker = ticker.split(".")[0].upper()
            logo_url = f"/static/logos/{exchange}/{clean_ticker}.webp"

        return {
            "Code": ticker,
            "Type": profile.get("quote_type") or "Common Stock",
            "Name": profile.get("name") or yf.get("longName"),
            "Exchange": profile.get("exchange"),
            "CurrencyCode": profile.get("currency") or yf.get("currency"),
            "CurrencySymbol": None,  # Non-essentiel
            "CountryName": profile.get("country"),
            "CountryISO": profile.get("country_code"),
            "OpenFigi": None,
            "ISIN": profile.get("isin") or yf.get("isin"),
            "LEI": None,
            "PrimaryTicker": profile.get("ticker"),
            "CIK": yf.get("cik"),
            "EmployerIdNumber": None,
            "FiscalYearEnd": yf.get("fiscalYearEnd"),
            "IPODate": FinancialsFormatter._safe_str(yf.get("firstTradeDateEpochUtc")),
            "Sector": profile.get("sector") or yf.get("sector"),
            "Industry": profile.get("industry") or yf.get("industry"),
            "GicSector": profile.get("gic_sector"),
            "GicGroup": profile.get("gic_group"),
            "GicIndustry": profile.get("gic_industry"),
            "GicSubIndustry": profile.get("gic_sub_industry"),
            "Description": profile.get("long_business_summary"),
            "Address": yf.get("address1"),
            "Phone": yf.get("phone"),
            "WebURL": yf.get("website"),
            "LogoURL": logo_url,
            "FullTimeEmployees": yf.get("fullTimeEmployees"),
        }

    @staticmethod
    def _insert_after(section: Dict, after: str, key: str, value: Any) -> Dict:
        """Return ``section`` with ``key`` placed right after ``after`` (EODHD order)."""
        rendered = {}
        for name, existing in section.items():
            rendered[name] = existing
            if name == after:
                rendered[key] = value
        return rendered

    @staticmethod
    def _build_holders(yf: Dict) -> Dict:
        # Comme précédemment, indexé par "0", "1", ...
        holders_raw = yf.get("holders", {})
        res = {"Institutions": {}, "Funds": {}}
        for cat in ["institutions", "funds"]:
            raw_list = holders_raw.get(cat, [])
            if not isinstance(raw_list, list):
                continue
            for i, item in enumerate(raw_list):
                res[cat.capitalize()][str(i)] = {
                    "name": item.get("Holder"),
                    "date": str(item.get("Date Reported")),
                    "totalShares": item.get("% Out"),
                    "currentShares": item.get("Shares"),
                    "change": item.get("Change"),
                    "value": item.get("Value"),
                }
        return res

    @staticmethod
    def _build_insider_transactions(results: Dict) -> Dict:
        txns = {}

        # 1. SEC Edgar (US): the answer of the provider, as it returns it
        sec_data = results.get("SECEdgar")
        filed = _as_dict(sec_data).get("transactions")
        if filed:
            for i, item in enumerate(filed):
                item = _as_dict(item)
                txns[str(i)] = {
                    "date": FinancialsFormatter._safe_str(
                        item.get("transaction_date") or item.get("filing_date")
                    ),
                    "ownerName": item.get("insider_name"),
                    "ownerTitle": item.get("insider_title"),
                    "shares": item.get("shares"),
                    "transactionCode": item.get("transaction_code"),
                    "transactionAmount": item.get("total_value"),
                    "transactionPrice": item.get("price_per_share"),
                    "postTransactionAmount": item.get("shares_owned_after"),
                    "description": item.get("transaction_type"),
                    "secLink": item.get("sec_filing_url"),
                }
            return txns
        if sec_data and isinstance(sec_data, list):
            for i, item in enumerate(sec_data):
                txns[str(i)] = {
                    "date": item.get("date"),
                    "ownerName": item.get("ownerName"),
                    "shares": item.get("shares"),
                    "transactionCode": item.get("transactionCode"),
                    "transactionAmount": item.get("transactionAmount"),
                    "transactionPrice": item.get("transactionPrice"),
                    "description": item.get("description"),
                }
            return txns

        # 2. Fallback sur WallStreetJournal (International)
        # "wallStreetJournal" is the name the provider is registered under.
        wsj = (
            results.get("wallStreetJournal")
            or results.get("wallstreetjournal")
            or results.get("WallStreetJournal")
        )

        # Handle Pydantic objects
        if hasattr(wsj, "model_dump"):
            wsj = wsj.model_dump()
        elif hasattr(wsj, "dict"):
            wsj = wsj.dict()

        if isinstance(wsj, dict) and "insider_transactions" in wsj:
            wsj_insiders = wsj["insider_transactions"].get("Transactions", [])
            for i, item in enumerate(wsj_insiders):
                txns[str(i)] = {
                    "date": item.get("date"),
                    "ownerName": item.get("ownerName"),
                    "shares": item.get("shares"),
                    "transactionCode": None,
                    "transactionAmount": None,
                    "transactionPrice": None,
                    "description": item.get("description"),
                }

        return txns

    @staticmethod
    def _build_esg_scores(esg: Optional[Dict], results: Dict[str, Any]) -> Dict:
        if not esg:
            esg = {}

        # Fallback sur les providers (Boursorama, etc.)
        # On cherche dans tous les résultats de providers si on a des infos ESG
        brs = results.get("boursorama")
        if not brs:
            # Parfois le nom est en majuscule ou autre
            brs = results.get("Boursorama")

        if hasattr(brs, "model_dump"):
            brs = brs.model_dump()
        elif hasattr(brs, "dict"):
            brs = brs.dict()

        if not isinstance(brs, dict):
            brs = {}

        return {
            "RatingDate": str(esg.get("rating_date")) if esg.get("rating_date") else None,
            "TotalEsg": esg.get("total_esg") or brs.get("esg_score"),
            "EnvironmentScore": esg.get("environment_score"),
            "SocialScore": esg.get("social_score"),
            "GovernanceScore": esg.get("governance_score"),
            "ControversyLevel": esg.get("controversy_level") or brs.get("esg_controversy"),
            "ActivitiesInvolved": {
                "Adult": esg.get("adult"),
                "Alcoholic": esg.get("alcoholic"),
                "AnimalTesting": esg.get("animal_testing"),
                "Gambling": esg.get("gambling"),
                "Tobacco": esg.get("tobacco"),
            },
            # Extensions non-standard EODHD mais utiles si présentes
            "CO2_Emissions": brs.get("esg_co2"),
            "Positive_Impact": brs.get("esg_positive_impact"),
            "Negative_Impact": brs.get("esg_negative_impact"),
        }

    @staticmethod
    def _build_earnings(history: List[Dict], trend: List[Dict]) -> Dict:
        res = {"History": {}, "Trend": {}, "Annual": {}}
        for i, h in enumerate(history or []):
            res["History"][str(i)] = {
                "reportDate": str(h.get("period_end")),
                "date": str(h.get("period")),
                "epsActual": h.get("eps_actual"),
                "epsEstimate": h.get("eps_estimate"),
                "epsDifference": h.get("surprise"),
                "surprisePercent": h.get("surprise_pct"),
            }

        # Trend mapping (matching EODHD keys)
        for i, t in enumerate(trend or []):
            period = t.get("period")
            res["Trend"][str(i)] = {
                "period": period,
                "earningsEstimateAvg": t.get("eps_avg"),
                "earningsEstimateLow": t.get("eps_low"),
                "earningsEstimateHigh": t.get("eps_high"),
                "earningsEstimateYearAgoEps": None,
                "earningsEstimateNumberOfAnalysts": t.get("eps_nb_analysts"),
                "earningsEstimateGrowth": t.get("eps_growth"),
                "revenueEstimateAvg": t.get("revenue_avg"),
                "revenueEstimateLow": t.get("revenue_low"),
                "revenueEstimateHigh": t.get("revenue_high"),
                "revenueEstimateYearAgoRevenue": None,
                "revenueEstimateNumberOfAnalysts": t.get("revenue_nb_analysts"),
                "revenueEstimateGrowth": t.get("revenue_growth"),
                "epsTrendCurrent": t.get("eps_avg"),
                "epsTrend7daysAgo": None,
                "epsTrend30daysAgo": None,
                "epsTrend60daysAgo": None,
                "epsTrend90daysAgo": None,
            }
        return res

    @staticmethod
    def _format_financials(statements: Optional[List[Dict]]) -> Dict:
        if not statements:
            return {}
        res = {
            "Balance_Sheet": {"yearly": {}, "quarterly": {}},
            "Cash_Flow": {"yearly": {}, "quarterly": {}},
            "Income_Statement": {"yearly": {}, "quarterly": {}},
        }

        type_map = {
            "income": "Income_Statement",
            "balance": "Balance_Sheet",
            "cashflow": "Cash_Flow",
        }

        for s in statements:
            t = type_map.get(s.get("statement_type"))
            p = "yearly" if s.get("period_type") == "annual" else "quarterly"
            if not t:
                continue

            date_str = str(s.get("period_end"))
            # Conversion de toutes les valeurs numériques en strings pour matching strict EODHD
            entry = {}
            for k, v in s.items():
                if k not in [
                    "asset_id",
                    "statement_type",
                    "period_type",
                    "period_end",
                    "fetched_at",
                ]:
                    entry[k] = FinancialsFormatter._safe_str(v)

            res[t][p][date_str] = entry

        return res

    @staticmethod
    def _build_etf_data(results: Dict) -> Dict:
        details = results.get("etf_details", {})
        holdings = results.get("etf_holdings", [])

        res = {
            "ISIN": None,
            "Inception_Date": str(details.get("inception_date")),
            "Net_Expense_Ratio": details.get("net_expense_ratio"),
            "Total_Assets": details.get("total_net_assets"),
            "Asset_Allocation": {
                "Cash": details.get("alloc_cash"),
                "Stock": details.get("alloc_stock_us"),
                "Bond": details.get("alloc_bond"),
            },
            "Top_10_Holdings": {},
        }

        for i, h in enumerate(holdings):
            res["Top_10_Holdings"][str(i)] = {
                "Symbol": h.get("holding_ticker"),
                "Name": h.get("holding_name"),
                "Sector": h.get("sector"),
                "Country": h.get("country"),
                "Assets_%": h.get("weight"),
            }
        return res
