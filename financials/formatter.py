import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
        asset_profile = results.get("asset_profile", {})
        highlights = results.get("highlights", {})
        # yf_info est utilisé comme fallback si les highlights DB sont pauvres
        yf_info = results.get("YahooFinance", {})
        if not yf_info or (isinstance(yf_info, dict) and yf_info.get("error")):
            yf_info = results.get("YFinanceProvider", {})

        # Si c'est un objet Pydantic, on le convertit en dict
        def _to_dict(obj):
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            if hasattr(obj, "dict"):
                return obj.dict()
            return obj

        highlights = _to_dict(highlights)
        yf_info = _to_dict(yf_info)

        # Construction du rendu Premium
        rendered = {
            "General": FinancialsFormatter._build_general(asset_profile, yf_info),
            "Highlights": FinancialsFormatter._build_highlights(highlights, yf_info),
            "Valuation": FinancialsFormatter._build_valuation(highlights, yf_info),
            "SharesStats": FinancialsFormatter._build_shares_stats(highlights, yf_info),
            "Technicals": FinancialsFormatter._build_technicals(highlights, yf_info),
            "SplitsDividends": FinancialsFormatter._build_splits_dividends(highlights, yf_info),
            "AnalystRatings": FinancialsFormatter._build_analyst_ratings(
                results.get("analyst_ratings"), yf_info
            ),
            "Holders": FinancialsFormatter._build_holders(yf_info, results),
            "InsiderTransactions": FinancialsFormatter._build_insider_transactions(results),
            "ESGScores": FinancialsFormatter._build_esg_scores(results.get("esg_scores"), results),
            "Earnings": FinancialsFormatter._build_earnings(
                results.get("earnings_history"), results.get("earnings_trend")
            ),
            "Financials": FinancialsFormatter._format_financials(
                results.get("financial_statements")
            ),
            "Providers": FinancialsFormatter._build_providers(results),
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
    def _build_highlights(h: Dict, yf: Dict) -> Dict:
        return {
            "MarketCapitalization": h.get("market_cap") or yf.get("marketCap"),
            "MarketCapitalizationMln": float(h.get("market_cap") or yf.get("marketCap") or 0) / 1e6
            if (h.get("market_cap") or yf.get("marketCap"))
            else None,
            "EBITDA": h.get("ebitda_ttm") or yf.get("ebitda"),
            "PERatio": h.get("pe_ratio") or yf.get("trailingPE"),
            "PEGRatio": h.get("peg_ratio") or yf.get("pegRatio"),
            "WallStreetTargetPrice": yf.get("targetMeanPrice"),
            "BookValue": h.get("book_value_per_share") or yf.get("bookValue"),
            "DividendShare": h.get("dividend_rate") or yf.get("dividendRate"),
            "DividendYield": h.get("dividend_yield") or yf.get("dividendYield"),
            "EarningsShare": h.get("eps_trailing") or yf.get("trailingEps"),
            "EPSEstimateCurrentYear": yf.get("forwardEps"),
            "EPSEstimateNextYear": None,
            "EPSEstimateNextQuarter": None,
            "EPSEstimateCurrentQuarter": None,
            "MostRecentQuarter": None,
            "ProfitMargin": h.get("net_margin") or yf.get("profitMargins"),
            "OperatingMarginTTM": h.get("operating_margin") or yf.get("operatingMargins"),
            "ReturnOnAssetsTTM": h.get("roa") or yf.get("returnOnAssets"),
            "ReturnOnEquityTTM": h.get("roe") or yf.get("returnOnEquity"),
            "RevenueTTM": h.get("revenue_ttm") or yf.get("totalRevenue"),
            "RevenuePerShareTTM": h.get("revenue_per_share") or yf.get("revenuePerShare"),
            "QuarterlyRevenueGrowthYOY": h.get("quarterly_revenue_growth_yoy")
            or yf.get("revenueGrowth"),
            "QuarterlyEarningsGrowthYOY": h.get("quarterly_earnings_growth_yoy")
            or yf.get("earningsGrowth"),
            "GrossProfitTTM": h.get("gross_profit_ttm") or yf.get("grossProfits"),
            "DilutedEpsTTM": h.get("diluted_eps_ttm") or yf.get("trailingEps"),
            "QuarterlyEarningsShareGrowthYOY": None,
        }

    @staticmethod
    def _build_valuation(h: Dict, yf: Dict) -> Dict:
        return {
            "TrailingPE": h.get("pe_ratio") or yf.get("trailingPE"),
            "ForwardPE": h.get("pe_forward") or yf.get("forwardPE"),
            "PriceSalesTTM": h.get("ps_ratio") or yf.get("priceToSalesTrailing12Months"),
            "PriceBookMRQ": h.get("pb_ratio") or yf.get("priceToBook"),
            "EnterpriseValue": h.get("enterprise_value") or yf.get("enterpriseValue"),
            "EnterpriseValueRevenue": h.get("ev_revenue") or yf.get("enterpriseToRevenue"),
            "EnterpriseValueEbitda": h.get("ev_ebitda") or yf.get("enterpriseToEbitda"),
        }

    @staticmethod
    def _build_shares_stats(h: Dict, yf: Dict) -> Dict:
        return {
            "SharesOutstanding": h.get("shares_outstanding") or yf.get("sharesOutstanding"),
            "SharesFloat": h.get("float_shares") or yf.get("floatShares"),
            "PercentInsiders": h.get("pct_insiders") or yf.get("heldPercentInsiders"),
            "PercentInstitutions": h.get("pct_institutions") or yf.get("heldPercentInstitutions"),
            "SharesShort": h.get("shares_short") or yf.get("sharesShort"),
            "SharesShortPriorMonth": h.get("shares_short_prior") or yf.get("sharesShortPriorMonth"),
            "ShortRatio": h.get("short_ratio") or yf.get("shortRatio"),
            "ShortPercentFloat": h.get("short_percent_float") or yf.get("shortPercentOfFloat"),
            "ShortPercentOutstanding": h.get("short_percent_outstanding")
            or yf.get("sharesPercentSharesOut"),
        }

    @staticmethod
    def _build_technicals(h: Dict, yf: Dict) -> Dict:
        return {
            "Beta": h.get("beta") or yf.get("beta"),
            "52WeekHigh": h.get("week_52_high") or yf.get("fiftyTwoWeekHigh"),
            "52WeekLow": h.get("week_52_low") or yf.get("fiftyTwoWeekLow"),
            "50DayMA": h.get("ma_50") or yf.get("fiftyDayAverage"),
            "200DayMA": h.get("ma_200") or yf.get("twoHundredDayAverage"),
            "SharesShort": h.get("shares_short") or yf.get("sharesShort"),
            "SharesShortPriorMonth": h.get("shares_short_prior") or yf.get("sharesShortPriorMonth"),
            "ShortRatio": h.get("short_ratio") or yf.get("shortRatio"),
            "ShortPercent": h.get("short_percent_float") or yf.get("shortPercentOfFloat"),
        }

    @staticmethod
    def _build_splits_dividends(h: Dict, yf: Dict) -> Dict:
        return {
            "ForwardAnnualDividendRate": h.get("dividend_rate") or yf.get("dividendRate"),
            "ForwardAnnualDividendYield": h.get("dividend_yield") or yf.get("dividendYield"),
            "PayoutRatio": h.get("payout_ratio") or yf.get("payoutRatio"),
            "DividendDate": str(h.get("dividend_pay_date")) if h.get("dividend_pay_date") else None,
            "ExDividendDate": str(h.get("dividend_ex_date")) if h.get("dividend_ex_date") else None,
            "LastSplitFactor": yf.get("lastSplitFactor"),
            "LastSplitDate": yf.get("lastSplitDate"),
            "NumberDividendsByYear": 0,
        }

    @staticmethod
    def _build_analyst_ratings(r: Optional[Dict], yf: Dict) -> Dict:
        if not r:
            r = {}
        return {
            "Rating": r.get("consensus") or yf.get("recommendationKey"),
            "TargetPrice": r.get("target_mean") or yf.get("targetMeanPrice"),
            "StrongBuy": r.get("strong_buy"),
            "Buy": r.get("buy"),
            "Hold": r.get("hold"),
            "Sell": r.get("sell"),
            "StrongSell": r.get("strong_sell"),
        }

    @staticmethod
    def _build_holders(yf: Dict, results: Dict) -> Dict:
        # Comme précédemment, indexé par "0", "1", ...
        holders_raw = yf.get("holders", {})
        
        # Fallback sur WallStreetJournal
        if not holders_raw or (not holders_raw.get("institutions") and not holders_raw.get("funds")):
            wsj = results.get("wallstreetjournal") or results.get("WallStreetJournal") or results.get("wallStreetJournal")
            if hasattr(wsj, "model_dump"):
                wsj = wsj.model_dump()
            elif hasattr(wsj, "dict"):
                wsj = wsj.dict()
            if isinstance(wsj, dict) and "holders" in wsj and isinstance(wsj["holders"], dict):
                holders_raw = wsj["holders"]

        res = {"Institutions": {}, "Funds": {}}
        for cat in ["institutions", "funds"]:
            raw_list = holders_raw.get(cat, [])
            if not isinstance(raw_list, list):
                continue
            for i, item in enumerate(raw_list):
                res[cat.capitalize()][str(i)] = {
                    "name": item.get("Holder") or item.get("name"),
                    "date": str(item.get("Date Reported") or item.get("date") or ""),
                    "totalShares": item.get("% Out") or item.get("totalShares"),
                    "currentShares": item.get("Shares") or item.get("currentShares"),
                    "change": item.get("Change") or item.get("change"),
                    "value": item.get("Value") or item.get("value"),
                }
        return res

    @staticmethod
    def _build_insider_transactions(results: Dict) -> Dict:
        txns = {}

        # 1. Fallback sur SEC Edgar (US)
        sec_data = results.get("SECEdgar")
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
        wsj = results.get("wallstreetjournal") or results.get("WallStreetJournal") or results.get("wallStreetJournal")

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
