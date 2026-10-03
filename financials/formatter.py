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
            "General": FinancialsFormatter._build_general(asset_profile, yf_info, results),
            "Highlights": FinancialsFormatter._build_highlights(highlights, yf_info),
            "Valuation": FinancialsFormatter._build_valuation(highlights, yf_info),
            "SharesStats": FinancialsFormatter._build_shares_stats(highlights, yf_info),
            "Technicals": FinancialsFormatter._build_technicals(highlights, yf_info),
            "SplitsDividends": FinancialsFormatter._build_splits_dividends(highlights, yf_info),
            "AnalystRatings": FinancialsFormatter._build_analyst_ratings(
                results.get("analyst_ratings"), yf_info, results
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
            "News": FinancialsFormatter._build_news(results),
            "Competitors": FinancialsFormatter._build_competitors(results),
            "Providers": FinancialsFormatter._build_providers(results),
        }

        # Ajout ETF_Data si c'est un ETF
        if (asset_profile.get("quote_type") or "").upper() == "ETF":
            rendered["ETF_Data"] = FinancialsFormatter._build_etf_data(results)

        return rendered

    @staticmethod
    def _build_news(results: Dict) -> Dict:
        news_dict = {}
        idx = 0
        for provider_name, payload in results.items():
            if not isinstance(payload, dict) and not hasattr(payload, "model_dump") and not hasattr(payload, "dict"):
                continue
            
            p_dict = payload
            if hasattr(payload, "model_dump"):
                p_dict = payload.model_dump()
            elif hasattr(payload, "dict"):
                p_dict = payload.dict()
            
            if isinstance(p_dict, dict) and p_dict.get("news"):
                for article in p_dict["news"]:
                    # exclude null fields if needed, but dict is fine
                    news_dict[str(idx)] = article
                    idx += 1
        return news_dict

    @staticmethod
    def _build_competitors(results: Dict) -> Dict:
        competitors_dict = {}
        idx = 0
        for provider_name, payload in results.items():
            if not isinstance(payload, dict) and not hasattr(payload, "model_dump") and not hasattr(payload, "dict"):
                continue
            
            p_dict = payload
            if hasattr(payload, "model_dump"):
                p_dict = payload.model_dump()
            elif hasattr(payload, "dict"):
                p_dict = payload.dict()
            
            if isinstance(p_dict, dict) and p_dict.get("competitors"):
                for comp in p_dict["competitors"]:
                    competitors_dict[str(idx)] = comp
                    idx += 1
        return competitors_dict

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
    def _build_general(profile: Dict, yf: Dict, results: Dict = None) -> Dict:
        import datetime
        if results is None:
            results = {}
            
        isin = profile.get("isin") or yf.get("isin")
        country = profile.get("country") or yf.get("country")
        
        if not isin or not country:
            for p, p_data in results.items():
                if isinstance(p_data, dict):
                    if not isin and p_data.get("isin"):
                        isin_candidate = p_data.get("isin")
                        if isin_candidate:
                            isin = isin_candidate.split(" ")[0]
                    if not country and p_data.get("country"):
                        country = p_data.get("country")

        # Logo URL logic
        ticker = profile.get("ticker") or yf.get("symbol")
        logo_url = profile.get("logo_path")
        if not logo_url and ticker:
            exchange = profile.get("exchange") or yf.get("exchange") or "UNKNOWN"
            clean_ticker = ticker.split(".")[0].upper()
            logo_url = f"/static/logos/{exchange}/{clean_ticker}.webp"

        # Currency formatting
        currency = profile.get("currency") or yf.get("currency")
        currency_symbol_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "C$", "AUD": "A$", "CHF": "CHF"}
        currency_symbol = currency_symbol_map.get(currency)

        # Country ISO
        country_iso_map = {"United States": "US", "France": "FR", "Germany": "DE", "United Kingdom": "GB", "Canada": "CA", "Japan": "JP", "China": "CN", "Switzerland": "CH", "Netherlands": "NL", "Ireland": "IE"}
        country_iso = profile.get("country_code") or country_iso_map.get(country)

        # IPO Date formatting
        ipo_date = FinancialsFormatter._safe_str(yf.get("firstTradeDateEpochUtc"))
        if not ipo_date and yf.get("firstTradeDateMilliseconds"):
            try:
                ipo_date = datetime.datetime.fromtimestamp(yf.get("firstTradeDateMilliseconds") / 1000, tz=datetime.timezone.utc).strftime('%Y-%m-%d')
            except Exception:
                pass

        # Fiscal Year End
        fiscal_year_end = yf.get("fiscalYearEnd")
        if not fiscal_year_end and yf.get("lastFiscalYearEnd"):
            try:
                fiscal_year_end = datetime.datetime.fromtimestamp(yf.get("lastFiscalYearEnd"), tz=datetime.timezone.utc).strftime('%B')
            except Exception:
                pass

        # CIK
        cik = yf.get("cik")
        if not cik:
            for p, p_data in results.items():
                if isinstance(p_data, dict) and p_data.get("cik"):
                    cik = p_data.get("cik")
                    break

        return {
            "Code": ticker,
            "Type": profile.get("quote_type") or yf.get("quoteType") or "Common Stock",
            "Name": profile.get("name") or yf.get("longName"),
            "Exchange": profile.get("exchange") or yf.get("exchange") or yf.get("fullExchangeName"),
            "CurrencyCode": currency,
            "CurrencySymbol": currency_symbol,
            "CountryName": country,
            "CountryISO": country_iso,
            "OpenFigi": None,
            "ISIN": isin,
            "LEI": None,
            "PrimaryTicker": profile.get("ticker") or ticker,
            "CIK": cik,
            "EmployerIdNumber": None,
            "FiscalYearEnd": fiscal_year_end,
            "IPODate": ipo_date,
            "Sector": profile.get("sector") or yf.get("sector"),
            "Industry": profile.get("industry") or yf.get("industry"),
            "GicSector": profile.get("gic_sector"),
            "GicGroup": profile.get("gic_group"),
            "GicIndustry": profile.get("gic_industry"),
            "GicSubIndustry": profile.get("gic_sub_industry"),
            "Description": profile.get("long_business_summary") or yf.get("longBusinessSummary"),
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
    def _build_analyst_ratings(r: Optional[Dict], yf: Dict, results: Dict = None) -> Dict:
        if not r:
            r = {}
        if not results:
            results = {}
            
        provider_rating = None
        provider_sentiment = None
        
        for provider_name, payload in results.items():
            if not isinstance(payload, dict) and not hasattr(payload, "model_dump") and not hasattr(payload, "dict"):
                continue
            
            p_dict = payload
            if hasattr(payload, "model_dump"):
                p_dict = payload.model_dump()
            elif hasattr(payload, "dict"):
                p_dict = payload.dict()
            
            if isinstance(p_dict, dict):
                if p_dict.get("rating") and not provider_rating:
                    provider_rating = p_dict.get("rating")
                if p_dict.get("sentiment") and not provider_sentiment:
                    provider_sentiment = p_dict.get("sentiment")

        return {
            "Rating": provider_rating or r.get("consensus") or yf.get("recommendationKey"),
            "Sentiment": provider_sentiment,
            "TargetPrice": r.get("target_mean") or yf.get("targetMeanPrice"),
            "StrongBuy": r.get("strong_buy"),
            "Buy": r.get("buy"),
            "Hold": r.get("hold"),
            "Sell": r.get("sell"),
            "StrongSell": r.get("strong_sell"),
        }

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
        wsj = results.get("wallstreetjournal") or results.get("WallStreetJournal")

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
