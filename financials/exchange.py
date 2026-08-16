"""
GuruFocus Exchange mapping.
Provides a structured, queryable dictionary of all stock exchanges supported by GuruFocus.
"""

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ExchangeInfo:
    code: str
    country: str
    region: str


GURUFOCUS_EXCHANGES: Dict[str, ExchangeInfo] = {
    "AMEX": ExchangeInfo(code="AMEX", country="USA", region="USA"),
    "ARCA": ExchangeInfo(code="ARCA", country="USA", region="USA"),
    "BATS": ExchangeInfo(code="BATS", country="USA", region="USA"),
    "GREY": ExchangeInfo(code="GREY", country="USA", region="USA"),
    "IEXG": ExchangeInfo(code="IEXG", country="USA", region="USA"),
    "NAS": ExchangeInfo(code="NAS", country="USA", region="USA"),
    "NYSE": ExchangeInfo(code="NYSE", country="USA", region="USA"),
    "OTCBB": ExchangeInfo(code="OTCBB", country="USA", region="USA"),
    "OTCPK": ExchangeInfo(code="OTCPK", country="USA", region="USA"),
    "BAH": ExchangeInfo(code="BAH", country="Bahrain", region="Asia"),
    "DHA": ExchangeInfo(code="DHA", country="Bangladesh", region="Asia"),
    "BJSE": ExchangeInfo(code="BJSE", country="China", region="Asia"),
    "SHSE": ExchangeInfo(code="SHSE", country="China", region="Asia"),
    "SZSE": ExchangeInfo(code="SZSE", country="China", region="Asia"),
    "HKSE": ExchangeInfo(code="HKSE", country="Hong Kong", region="Asia"),
    "ISX": ExchangeInfo(code="ISX", country="Indonesia", region="Asia"),
    "XTEH": ExchangeInfo(code="XTEH", country="Iran", region="Asia"),
    "IQS": ExchangeInfo(code="IQS", country="Iraq", region="Asia"),
    "XTAE": ExchangeInfo(code="XTAE", country="Israel", region="Asia"),
    "FSE": ExchangeInfo(code="FSE", country="Japan", region="Asia"),
    "JAS": ExchangeInfo(code="JAS", country="Japan", region="Asia"),
    "NGO": ExchangeInfo(code="NGO", country="Japan", region="Asia"),
    "OSE": ExchangeInfo(code="OSE", country="Japan", region="Asia"),
    "SSE": ExchangeInfo(code="SSE", country="Japan", region="Asia"),
    "TSE": ExchangeInfo(code="TSE", country="Japan", region="Asia"),
    "AMM": ExchangeInfo(code="AMM", country="Jordan", region="Asia"),
    "XKAZ": ExchangeInfo(code="XKAZ", country="Kazakhstan", region="Asia"),
    "XKRX": ExchangeInfo(code="XKRX", country="Korea", region="Asia"),
    "KUW": ExchangeInfo(code="KUW", country="Kuwait", region="Asia"),
    "BEY": ExchangeInfo(code="BEY", country="Lebanon", region="Asia"),
    "XKLS": ExchangeInfo(code="XKLS", country="Malaysia", region="Asia"),
    "XNEP": ExchangeInfo(code="XNEP", country="Nepal", region="Asia"),
    "MUS": ExchangeInfo(code="MUS", country="Oman", region="Asia"),
    "XPAE": ExchangeInfo(code="XPAE", country="Palestine", region="Asia"),
    "PHS": ExchangeInfo(code="PHS", country="Philippines", region="Asia"),
    "DSMD": ExchangeInfo(code="DSMD", country="Qatar", region="Asia"),
    "SAU": ExchangeInfo(code="SAU", country="Saudi", region="Asia"),
    "SGX": ExchangeInfo(code="SGX", country="Singapore", region="Asia"),
    "COL": ExchangeInfo(code="COL", country="Sri Lanka", region="Asia"),
    "ROCO": ExchangeInfo(code="ROCO", country="Taiwan", region="Asia"),
    "TPE": ExchangeInfo(code="TPE", country="Taiwan", region="Asia"),
    "BKK": ExchangeInfo(code="BKK", country="Thailand", region="Asia"),
    "ADX": ExchangeInfo(code="ADX", country="United Arab Emirates", region="Asia"),
    "DFM": ExchangeInfo(code="DFM", country="United Arab Emirates", region="Asia"),
    "DIFX": ExchangeInfo(code="DIFX", country="United Arab Emirates", region="Asia"),
    "HSTC": ExchangeInfo(code="HSTC", country="Vietnam", region="Asia"),
    "STC": ExchangeInfo(code="STC", country="Vietnam", region="Asia"),
    "WBO": ExchangeInfo(code="WBO", country="Austria", region="Europe"),
    "XBRU": ExchangeInfo(code="XBRU", country="Belgium", region="Europe"),
    "XBLB": ExchangeInfo(code="XBLB", country="Bosnia and Herzegovina", region="Europe"),
    "XBUL": ExchangeInfo(code="XBUL", country="Bulgaria", region="Europe"),
    "ZAG": ExchangeInfo(code="ZAG", country="Croatia", region="Europe"),
    "CYS": ExchangeInfo(code="CYS", country="Cyprus", region="Europe"),
    "XPRA": ExchangeInfo(code="XPRA", country="Czech Republic", region="Europe"),
    "OCSE": ExchangeInfo(code="OCSE", country="Denmark", region="Europe"),
    "OTSE": ExchangeInfo(code="OTSE", country="Estonia", region="Europe"),
    "OHEL": ExchangeInfo(code="OHEL", country="Finland", region="Europe"),
    "XPAR": ExchangeInfo(code="XPAR", country="France", region="Europe"),
    "FRA": ExchangeInfo(code="FRA", country="Germany", region="Europe"),
    "HAM": ExchangeInfo(code="HAM", country="Germany", region="Europe"),
    "STU": ExchangeInfo(code="STU", country="Germany", region="Europe"),
    "XTER": ExchangeInfo(code="XTER", country="Germany", region="Europe"),
    "ATH": ExchangeInfo(code="ATH", country="Greece", region="Europe"),
    "BUD": ExchangeInfo(code="BUD", country="Hungary", region="Europe"),
    "OISE": ExchangeInfo(code="OISE", country="Iceland", region="Europe"),
    "MIL": ExchangeInfo(code="MIL", country="Italy", region="Europe"),
    "ORSE": ExchangeInfo(code="ORSE", country="Latvia", region="Europe"),
    "OVSE": ExchangeInfo(code="OVSE", country="Lithuania", region="Europe"),
    "LUX": ExchangeInfo(code="LUX", country="Luxembourg", region="Europe"),
    "XMAE": ExchangeInfo(code="XMAE", country="Macedonia", region="Europe"),
    "MAL": ExchangeInfo(code="MAL", country="Malta", region="Europe"),
    "XAMS": ExchangeInfo(code="XAMS", country="Netherlands", region="Europe"),
    "OSL": ExchangeInfo(code="OSL", country="Norway", region="Europe"),
    "WAR": ExchangeInfo(code="WAR", country="Poland", region="Europe"),
    "XLIS": ExchangeInfo(code="XLIS", country="Portugal", region="Europe"),
    "BSE": ExchangeInfo(code="BSE", country="Romania", region="Europe"),
    "MIC": ExchangeInfo(code="MIC", country="Russia", region="Europe"),
    "XBEL": ExchangeInfo(code="XBEL", country="Serbia", region="Europe"),
    "XBRA": ExchangeInfo(code="XBRA", country="Slovakia", region="Europe"),
    "XLJU": ExchangeInfo(code="XLJU", country="Slovenia", region="Europe"),
    "XMAD": ExchangeInfo(code="XMAD", country="Spain", region="Europe"),
    "NGM": ExchangeInfo(code="NGM", country="Sweden", region="Europe"),
    "OSTO": ExchangeInfo(code="OSTO", country="Sweden", region="Europe"),
    "XSAT": ExchangeInfo(code="XSAT", country="Sweden", region="Europe"),
    "XSWX": ExchangeInfo(code="XSWX", country="Switzerland", region="Europe"),
    "IST": ExchangeInfo(code="IST", country="Turkey", region="Europe"),
    "PFTS": ExchangeInfo(code="PFTS", country="Ukraine", region="Europe"),
    "UKEX": ExchangeInfo(code="UKEX", country="Ukraine", region="Europe"),
    "NEOE": ExchangeInfo(code="NEOE", country="Canada", region="Canada"),
    "TSX": ExchangeInfo(code="TSX", country="Canada", region="Canada"),
    "TSXV": ExchangeInfo(code="TSXV", country="Canada", region="Canada"),
    "XCNQ": ExchangeInfo(code="XCNQ", country="Canada", region="Canada"),
    "DUB": ExchangeInfo(code="DUB", country="Ireland", region="UK/Ireland"),
    "AQSE": ExchangeInfo(code="AQSE", country="UK", region="UK/Ireland"),
    "CHIX": ExchangeInfo(code="CHIX", country="UK", region="UK/Ireland"),
    "IPSX": ExchangeInfo(code="IPSX", country="UK", region="UK/Ireland"),
    "LSE": ExchangeInfo(code="LSE", country="UK", region="UK/Ireland"),
    "LTS": ExchangeInfo(code="LTS", country="UK", region="UK/Ireland"),
    "NEXX": ExchangeInfo(code="NEXX", country="UK", region="UK/Ireland"),
    "XPLU": ExchangeInfo(code="XPLU", country="UK", region="UK/Ireland"),
    "ASX": ExchangeInfo(code="ASX", country="Australia", region="Oceania"),
    "XNEC": ExchangeInfo(code="XNEC", country="Australia", region="Oceania"),
    "NZSE": ExchangeInfo(code="NZSE", country="New Zealand", region="Oceania"),
    "BUE": ExchangeInfo(code="BUE", country="Argentina", region="Latin America"),
    "BDA": ExchangeInfo(code="BDA", country="Bermuda", region="Latin America"),
    "BSP": ExchangeInfo(code="BSP", country="Brazil", region="Latin America"),
    "XSGO": ExchangeInfo(code="XSGO", country="Chile", region="Latin America"),
    "BOG": ExchangeInfo(code="BOG", country="Colombia", region="Latin America"),
    "QUI": ExchangeInfo(code="QUI", country="Ecuador", region="Latin America"),
    "XGUA": ExchangeInfo(code="XGUA", country="Ecuador", region="Latin America"),
    "XJAM": ExchangeInfo(code="XJAM", country="Jamaica", region="Latin America"),
    "MEX": ExchangeInfo(code="MEX", country="Mexico", region="Latin America"),
    "XPTY": ExchangeInfo(code="XPTY", country="Panama", region="Latin America"),
    "LIM": ExchangeInfo(code="LIM", country="Peru", region="Latin America"),
    "TRN": ExchangeInfo(code="TRN", country="Trinidad and Tobago", region="Latin America"),
    "MNT": ExchangeInfo(code="MNT", country="Uruguay", region="Latin America"),
    "CAR": ExchangeInfo(code="CAR", country="Venezuela", region="Latin America"),
    "BOT": ExchangeInfo(code="BOT", country="Botswana", region="Africa"),
    "XBRV": ExchangeInfo(code="XBRV", country="Cote d'Ivoire", region="Africa"),
    "CAI": ExchangeInfo(code="CAI", country="Egypt", region="Africa"),
    "SWA": ExchangeInfo(code="SWA", country="Eswatini", region="Africa"),
    "XGHA": ExchangeInfo(code="XGHA", country="Ghana", region="Africa"),
    "NAI": ExchangeInfo(code="NAI", country="Kenya", region="Africa"),
    "MSW": ExchangeInfo(code="MSW", country="Malawi", region="Africa"),
    "XMAU": ExchangeInfo(code="XMAU", country="Mauritius", region="Africa"),
    "CAS": ExchangeInfo(code="CAS", country="Morocco", region="Africa"),
    "NAM": ExchangeInfo(code="NAM", country="Namibia", region="Africa"),
    "NSA": ExchangeInfo(code="NSA", country="Nigeria", region="Africa"),
    "JSE": ExchangeInfo(code="JSE", country="South Africa", region="Africa"),
    "XTUN": ExchangeInfo(code="XTUN", country="Tunisia", region="Africa"),
    "LUS": ExchangeInfo(code="LUS", country="Zambia", region="Africa"),
    "XZIM": ExchangeInfo(code="XZIM", country="Zimbabwe", region="Africa"),
    "BOM": ExchangeInfo(code="BOM", country="India", region="India/Pakistan"),
    "NSE": ExchangeInfo(code="NSE", country="India", region="India/Pakistan"),
    "KAR": ExchangeInfo(code="KAR", country="Pakistan", region="India/Pakistan"),
}

# --- Yahoo Finance Exchanges ---


@dataclass
class YahooExchangeInfo:
    name: str
    country: str
    suffix: str


YAHOO_EXCHANGES: List[YahooExchangeInfo] = [
    YahooExchangeInfo(name="Johannesburg Stock Exchange", country="Afrique du Sud", suffix=".JO"),
    YahooExchangeInfo(name="Berlin Stock Exchange", country="Allemagne", suffix=".BE"),
    YahooExchangeInfo(name="Bremen Stock Exchange", country="Allemagne", suffix=".BM"),
    YahooExchangeInfo(name="Dusseldorf Stock Exchange", country="Allemagne", suffix=".DU"),
    YahooExchangeInfo(name="Frankfurt Stock Exchange", country="Allemagne", suffix=".F"),
    YahooExchangeInfo(name="Hamburg Stock Exchange", country="Allemagne", suffix=".HM"),
    YahooExchangeInfo(name="Hanover Stock Exchange", country="Allemagne", suffix=".HA"),
    YahooExchangeInfo(name="Munich Stock Exchange", country="Allemagne", suffix=".MU"),
    YahooExchangeInfo(name="Stuttgart Stock Exchange", country="Allemagne", suffix=".SG"),
    YahooExchangeInfo(name="Deutsche Boerse XETRA", country="Allemagne", suffix=".DE"),
    YahooExchangeInfo(
        name="Saudi Stock Exchange / Tadawul", country="Arabie Saoudite", suffix=".SAU"
    ),
    YahooExchangeInfo(name="Buenos Aires Stock Exchange", country="Argentine", suffix=".BA"),
    YahooExchangeInfo(name="Australian Stock Exchange", country="Australie", suffix=".AX"),
    YahooExchangeInfo(name="Cboe Australia", country="Australie", suffix=".XA"),
    YahooExchangeInfo(name="Vienna Stock Exchange", country="Autriche", suffix=".VI"),
    YahooExchangeInfo(name="Euronext Brussels", country="Belgique", suffix=".BR"),
    YahooExchangeInfo(name="Sao Paulo Stock Exchange / BOVESPA", country="Brésil", suffix=".SA"),
    YahooExchangeInfo(name="Canadian Securities Exchange", country="Canada", suffix=".CN"),
    YahooExchangeInfo(name="Cboe Canada", country="Canada", suffix=".NE"),
    YahooExchangeInfo(name="Toronto Stock Exchange", country="Canada", suffix=".TO"),
    YahooExchangeInfo(name="TSX Venture Exchange", country="Canada", suffix=".V"),
    YahooExchangeInfo(name="Santiago Stock Exchange", country="Chili", suffix=".SN"),
    YahooExchangeInfo(name="Shanghai Stock Exchange", country="Chine", suffix=".SS"),
    YahooExchangeInfo(name="Shenzhen Stock Exchange", country="Chine", suffix=".SZ"),
    YahooExchangeInfo(name="Colombia Stock Exchange", country="Colombie", suffix=".CL"),
    YahooExchangeInfo(name="Korea Stock Exchange", country="Corée du Sud", suffix=".KS"),
    YahooExchangeInfo(name="KOSDAQ", country="Corée du Sud", suffix=".KQ"),
    YahooExchangeInfo(name="Nasdaq OMX Copenhagen", country="Danemark", suffix=".CO"),
    YahooExchangeInfo(name="Egyptian Exchange Index", country="Égypte", suffix=".CA"),
    YahooExchangeInfo(name="Dubai Financial Market", country="Émirats Arabes Unis", suffix=".AE"),
    YahooExchangeInfo(name="Madrid Stock Exchange / BME", country="Espagne", suffix=".MC"),
    YahooExchangeInfo(name="Nasdaq OMX Tallinn", country="Estonie", suffix=".TL"),
    YahooExchangeInfo(name="Nasdaq Stock Exchange", country="États-Unis", suffix=""),
    YahooExchangeInfo(name="Dow Jones Indexes", country="États-Unis", suffix=""),
    YahooExchangeInfo(name="S&P Indices", country="États-Unis", suffix=""),
    YahooExchangeInfo(name="Cboe Indices", country="États-Unis", suffix=""),
    YahooExchangeInfo(name="OTC Markets Group", country="États-Unis", suffix=""),
    YahooExchangeInfo(
        name="Options Price Reporting Authority / OPRA", country="États-Unis", suffix=""
    ),
    YahooExchangeInfo(name="Chicago Board of Trade / CBOT", country="États-Unis", suffix=".CBT"),
    YahooExchangeInfo(
        name="Chicago Mercantile Exchange / CME", country="États-Unis", suffix=".CME"
    ),
    YahooExchangeInfo(name="ICE Futures US", country="États-Unis", suffix=".NYB"),
    YahooExchangeInfo(
        name="New York Commodities Exchange / COMEX", country="États-Unis", suffix=".CMX"
    ),
    YahooExchangeInfo(
        name="New York Mercantile Exchange / NYMEX", country="États-Unis", suffix=".NYM"
    ),
    YahooExchangeInfo(name="Cboe Europe", country="Europe (Pan-Européen)", suffix=".XD"),
    YahooExchangeInfo(name="Euronext", country="Europe (Pan-Européen)", suffix=".NX"),
    YahooExchangeInfo(name="Nasdaq OMX Helsinki", country="Finlande", suffix=".HE"),
    YahooExchangeInfo(name="Euronext Paris", country="France", suffix=".PA"),
    YahooExchangeInfo(name="Athens Stock Exchange", country="Grèce", suffix=".AT"),
    YahooExchangeInfo(name="Hang Seng Indices", country="Hong Kong", suffix=""),
    YahooExchangeInfo(name="Hong Kong Stock Exchange", country="Hong Kong", suffix=".HK"),
    YahooExchangeInfo(name="Budapest Stock Exchange", country="Hongrie", suffix=".BD"),
    YahooExchangeInfo(name="Bombay Stock Exchange", country="Inde", suffix=".BO"),
    YahooExchangeInfo(name="National Stock Exchange of India", country="Inde", suffix=".NS"),
    YahooExchangeInfo(name="Indonesia Stock Exchange", country="Indonésie", suffix=".JK"),
    YahooExchangeInfo(name="Euronext Dublin", country="Irlande", suffix=".IR"),
    YahooExchangeInfo(name="Nasdaq OMX Iceland", country="Islande", suffix=".IC"),
    YahooExchangeInfo(name="Tel Aviv Stock Exchange", country="Israël", suffix=".TA"),
    YahooExchangeInfo(name="EuroTLX", country="Italie", suffix=".TI"),
    YahooExchangeInfo(
        name="Italian Stock Exchange / Borsa Italiana", country="Italie", suffix=".MI"
    ),
    YahooExchangeInfo(name="Tokyo Stock Exchange", country="Japon", suffix=".T"),
    YahooExchangeInfo(name="Nikkei Indices", country="Japon", suffix=""),
    YahooExchangeInfo(name="Boursa Kuwait", country="Koweït", suffix=".KW"),
    YahooExchangeInfo(name="Nasdaq OMX Riga", country="Lettonie", suffix=".RG"),
    YahooExchangeInfo(name="Nasdaq OMX Vilnius", country="Lituanie", suffix=".VS"),
    YahooExchangeInfo(name="Malaysian Stock Exchange", country="Malaisie", suffix=".KL"),
    YahooExchangeInfo(name="Mexico Stock Exchange / BMV", country="Mexique", suffix=".MX"),
    YahooExchangeInfo(name="Oslo Stock Exchange", country="Norvège", suffix=".OL"),
    YahooExchangeInfo(name="New Zealand Stock Exchange", country="Nouvelle-Zélande", suffix=".NZ"),
    YahooExchangeInfo(name="Euronext Amsterdam", country="Pays-Bas", suffix=".AS"),
    YahooExchangeInfo(
        name="Philippine Stock Exchange Indices", country="Philippines", suffix=".PS"
    ),
    YahooExchangeInfo(name="Warsaw Stock Exchange", country="Pologne", suffix=".WA"),
    YahooExchangeInfo(name="Euronext Lisbon", country="Portugal", suffix=".LS"),
    YahooExchangeInfo(name="Qatar Stock Exchange", country="Qatar", suffix=".QA"),
    YahooExchangeInfo(
        name="Prague Stock Exchange Index", country="République Tchèque", suffix=".PR"
    ),
    YahooExchangeInfo(name="Bucharest Stock Exchange", country="Roumanie", suffix=".RO"),
    YahooExchangeInfo(name="London Stock Exchange", country="Royaume-Uni", suffix=".L"),
    YahooExchangeInfo(name="London Stock Exchange", country="Royaume-Uni", suffix=".IL"),
    YahooExchangeInfo(name="Aquis Exchange AQSE", country="Royaume-Uni", suffix=".AQ"),
    YahooExchangeInfo(name="Cboe UK", country="Royaume-Uni", suffix=".XC"),
    YahooExchangeInfo(name="FTSE Indices", country="Royaume-Uni", suffix=""),
    YahooExchangeInfo(name="Singapore Stock Exchange", country="Singapour", suffix=".SI"),
    YahooExchangeInfo(name="Nasdaq OMX Stockholm", country="Suède", suffix=".ST"),
    YahooExchangeInfo(name="Swiss Exchange / SIX", country="Suisse", suffix=".SW"),
    YahooExchangeInfo(name="Taiwan Stock Exchange", country="Taïwan", suffix=".TW"),
    YahooExchangeInfo(name="Taiwan OTC Exchange", country="Taïwan", suffix=".TWO"),
    YahooExchangeInfo(name="Stock Exchange of Thailand", country="Thaïlande", suffix=".BK"),
    YahooExchangeInfo(name="Borsa İstanbul", country="Turquie", suffix=".IS"),
    YahooExchangeInfo(name="Caracas Stock Exchange", country="Venezuela", suffix=".CR"),
    YahooExchangeInfo(name="Ho Chi Minh City Stock Exchange", country="Vietnam", suffix=".VN"),
    YahooExchangeInfo(name="Devises Globales / Forex", country="Marchés Globaux", suffix="=X"),
    YahooExchangeInfo(name="Cryptomonnaies", country="Marchés Globaux", suffix=""),
    YahooExchangeInfo(name="Collectable Indices", country="Marchés Globaux", suffix=".REGA"),
    YahooExchangeInfo(name="MSCI Indices", country="Marchés Globaux", suffix=""),
]

# --- Google Finance Exchanges ---


@dataclass
class GoogleExchangeInfo:
    code: str
    name: str
    country: str
    region: str


GOOGLE_EXCHANGES: Dict[str, GoogleExchangeInfo] = {
    "BCBA": GoogleExchangeInfo(
        code="BCBA", name="Buenos Aires Stock Exchange", country="Argentine", region="Amériques"
    ),
    "BMV": GoogleExchangeInfo(
        code="BMV", name="Mexican Stock Exchange", country="Mexique", region="Amériques"
    ),
    "BVMF": GoogleExchangeInfo(
        code="BVMF", name="B3 - Brazil Stock Exchange", country="Brésil", region="Amériques"
    ),
    "CNSX": GoogleExchangeInfo(
        code="CNSX", name="Canadian Securities Exchange", country="Canada", region="Amériques"
    ),
    "NASDAQ": GoogleExchangeInfo(
        code="NASDAQ", name="NASDAQ", country="États-Unis", region="Amériques"
    ),
    "NYSE": GoogleExchangeInfo(
        code="NYSE", name="New York Stock Exchange", country="États-Unis", region="Amériques"
    ),
    "NYSEARCA": GoogleExchangeInfo(
        code="NYSEARCA", name="NYSE ARCA", country="États-Unis", region="Amériques"
    ),
    "NYSEAMERICAN": GoogleExchangeInfo(
        code="NYSEAMERICAN", name="NYSE American", country="États-Unis", region="Amériques"
    ),
    "OPRA": GoogleExchangeInfo(
        code="OPRA",
        name="Options Price Reporting Authority",
        country="États-Unis",
        region="Amériques",
    ),
    "OTCMKTS": GoogleExchangeInfo(
        code="OTCMKTS", name="FINRA Other OTC Issues", country="États-Unis", region="Amériques"
    ),
    "TSE": GoogleExchangeInfo(
        code="TSE", name="Toronto Stock Exchange", country="Canada", region="Amériques"
    ),
    "TSX": GoogleExchangeInfo(
        code="TSX", name="Toronto Stock Exchange", country="Canada", region="Amériques"
    ),
    "TSXV": GoogleExchangeInfo(
        code="TSXV", name="Toronto TSX Ventures Exchange", country="Canada", region="Amériques"
    ),
    "AMS": GoogleExchangeInfo(
        code="AMS", name="Euronext Amsterdam", country="Pays-Bas", region="Europe"
    ),
    "BIT": GoogleExchangeInfo(
        code="BIT", name="Borsa Italiana Milan Stock Exchange", country="Italie", region="Europe"
    ),
    "BME": GoogleExchangeInfo(
        code="BME", name="Bolsas y Mercados Españoles", country="Espagne", region="Europe"
    ),
    "CPH": GoogleExchangeInfo(
        code="CPH", name="NASDAQ OMX Copenhagen", country="Danemark", region="Europe"
    ),
    "EBR": GoogleExchangeInfo(
        code="EBR", name="Euronext Brussels", country="Belgique", region="Europe"
    ),
    "ELI": GoogleExchangeInfo(
        code="ELI", name="Euronext Lisbon", country="Portugal", region="Europe"
    ),
    "EPA": GoogleExchangeInfo(code="EPA", name="Euronext Paris", country="France", region="Europe"),
    "ETR": GoogleExchangeInfo(
        code="ETR", name="Deutsche Börse XETRA", country="Allemagne", region="Europe"
    ),
    "FRA": GoogleExchangeInfo(
        code="FRA",
        name="Deutsche Börse Frankfurt Stock Exchange",
        country="Allemagne",
        region="Europe",
    ),
    "HEL": GoogleExchangeInfo(
        code="HEL", name="NASDAQ OMX Helsinki", country="Finlande", region="Europe"
    ),
    "ICE": GoogleExchangeInfo(
        code="ICE", name="NASDAQ OMX Iceland", country="Islande", region="Europe"
    ),
    "IST": GoogleExchangeInfo(
        code="IST", name="Borsa Istanbul", country="Turquie", region="Europe"
    ),
    "LON": GoogleExchangeInfo(
        code="LON", name="London Stock Exchange", country="Royaume-Uni", region="Europe"
    ),
    "RSE": GoogleExchangeInfo(
        code="RSE", name="NASDAQ OMX Riga", country="Lettonie", region="Europe"
    ),
    "STO": GoogleExchangeInfo(
        code="STO", name="NASDAQ OMX Stockholm", country="Suède", region="Europe"
    ),
    "SWX": GoogleExchangeInfo(
        code="SWX", name="SIX Swiss Exchange", country="Suisse", region="Europe"
    ),
    "VTX": GoogleExchangeInfo(
        code="VTX", name="SIX Swiss Exchange", country="Suisse", region="Europe"
    ),
    "TAL": GoogleExchangeInfo(
        code="TAL", name="NASDAQ OMX Tallinn", country="Estonie", region="Europe"
    ),
    "VIE": GoogleExchangeInfo(
        code="VIE", name="Vienna Stock Exchange", country="Autriche", region="Europe"
    ),
    "VSE": GoogleExchangeInfo(
        code="VSE", name="NASDAQ OMX Vilnius", country="Lituanie", region="Europe"
    ),
    "WSE": GoogleExchangeInfo(
        code="WSE", name="Warsaw Stock Exchange", country="Pologne", region="Europe"
    ),
    "ASX": GoogleExchangeInfo(
        code="ASX",
        name="Australian Securities Exchange",
        country="Australie",
        region="Asie et Pacifique Sud",
    ),
    "BKK": GoogleExchangeInfo(
        code="BKK",
        name="Thailand Stock Exchange",
        country="Thaïlande",
        region="Asie et Pacifique Sud",
    ),
    "BOM": GoogleExchangeInfo(
        code="BOM",
        name="Bombay Stock Exchange Limited",
        country="Inde",
        region="Asie et Pacifique Sud",
    ),
    "HKG": GoogleExchangeInfo(
        code="HKG",
        name="Hong Kong Stock Exchange",
        country="Hong Kong",
        region="Asie et Pacifique Sud",
    ),
    "IDX": GoogleExchangeInfo(
        code="IDX",
        name="Indonesia Stock Exchange",
        country="Indonésie",
        region="Asie et Pacifique Sud",
    ),
    "KLSE": GoogleExchangeInfo(
        code="KLSE", name="Bursa Malaysia", country="Malaisie", region="Asie et Pacifique Sud"
    ),
    "KOSDAQ": GoogleExchangeInfo(
        code="KOSDAQ", name="KOSDAQ", country="Corée du Sud", region="Asie et Pacifique Sud"
    ),
    "KRX": GoogleExchangeInfo(
        code="KRX",
        name="Korea Stock Exchange",
        country="Corée du Sud",
        region="Asie et Pacifique Sud",
    ),
    "NSE": GoogleExchangeInfo(
        code="NSE",
        name="National Stock Exchange of India",
        country="Inde",
        region="Asie et Pacifique Sud",
    ),
    "NZE": GoogleExchangeInfo(
        code="NZE",
        name="New Zealand Stock Exchange",
        country="Nouvelle-Zélande",
        region="Asie et Pacifique Sud",
    ),
    "SGX": GoogleExchangeInfo(
        code="SGX", name="Singapore Exchange", country="Singapour", region="Asie et Pacifique Sud"
    ),
    "SHA": GoogleExchangeInfo(
        code="SHA", name="Shanghai Stock Exchange", country="Chine", region="Asie et Pacifique Sud"
    ),
    "SHE": GoogleExchangeInfo(
        code="SHE", name="Shenzhen Stock Exchange", country="Chine", region="Asie et Pacifique Sud"
    ),
    "TPE": GoogleExchangeInfo(
        code="TPE", name="Taiwan Stock Exchange", country="Taïwan", region="Asie et Pacifique Sud"
    ),
    "TYO": GoogleExchangeInfo(
        code="TYO", name="Tokyo Stock Exchange", country="Japon", region="Asie et Pacifique Sud"
    ),
    "JSE": GoogleExchangeInfo(
        code="JSE",
        name="Johannesburg Stock Exchange",
        country="Afrique du Sud",
        region="Afrique et Moyen-Orient",
    ),
    "TADAWUL": GoogleExchangeInfo(
        code="TADAWUL",
        name="Saudi Stock Exchange",
        country="Arabie Saoudite",
        region="Afrique et Moyen-Orient",
    ),
    "TLV": GoogleExchangeInfo(
        code="TLV",
        name="Tel Aviv Stock Exchange",
        country="Israël",
        region="Afrique et Moyen-Orient",
    ),
    "MUTF": GoogleExchangeInfo(
        code="MUTF",
        name="Mutual Funds (USA)",
        country="États-Unis",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "MUTF_IN": GoogleExchangeInfo(
        code="MUTF_IN",
        name="Mutual Funds (Inde)",
        country="Inde",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "KALSHI": GoogleExchangeInfo(
        code="KALSHI",
        name="Prediction Markets (Kalshi)",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "POLY": GoogleExchangeInfo(
        code="POLY",
        name="Prediction Markets (Polymarket)",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "CURRENCY": GoogleExchangeInfo(
        code="CURRENCY",
        name="Devises / Forex",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "INDEXDJX": GoogleExchangeInfo(
        code="INDEXDJX",
        name="Indices Dow Jones (ex: Dow Jones Industrial Average)",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "INDEXSP": GoogleExchangeInfo(
        code="INDEXSP",
        name="Indices S&P (ex: S&P 500)",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "INDEXNASDAQ": GoogleExchangeInfo(
        code="INDEXNASDAQ",
        name="Indices Nasdaq",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "INDEXEURO": GoogleExchangeInfo(
        code="INDEXEURO",
        name="Indices Euronext (ex: CAC 40)",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
    "INDEXFTSE": GoogleExchangeInfo(
        code="INDEXFTSE",
        name="Indices FTSE",
        country="Global",
        region="Autres marchés et préfixes spéciaux Google Finance",
    ),
}

# --- Mapping for Automated Ticker Resolution ---

DB_EXCHANGE_TO_YAHOO_SUFFIX = {
    "PAR": ".PA",
    "AMS": ".AS",
    "BRU": ".BR",
    "LIS": ".LS",
    "LON": ".L",
    "FRA": ".F",
    "GER": ".DE",
    "XETRA": ".DE",
    "MAD": ".MC",
    "MIL": ".MI",
    "VX": ".SW",
    "STU": ".SG",
    "BER": ".BE",
    "MUN": ".MU",
    "DUS": ".DU",
    "HAN": ".HA",
    "BSE": ".BO",
    "NSE": ".NS",
    "TSE": ".T",
    "HKG": ".HK",
    "SHG": ".SS",
    "SZE": ".SZ",
    "ASX": ".AX",
    "TSX": ".TO",
    "TSXV": ".V",
    "NYQ": "",
    "NMS": "",
    "NGM": "",
    "NCM": "",
    "US": "",
}

# Mapping spécifique GuruFocus (ISO MIC ou codes propriétaires) vers Yahoo Suffix
GURUFOCUS_TO_YAHOO = {
    "XPAR": ".PA",
    "XAMS": ".AS",
    "XBRU": ".BR",
    "XLIS": ".LS",
    "XMAD": ".MC",
    "MIL": ".MI",
    "XSWX": ".SW",
    "FRA": ".F",
    "XTER": ".DE",
    "LSE": ".L",
    "TSE": ".T",
    "HKSE": ".HK",
    "SHSE": ".SS",
    "SZSE": ".SZ",
    "ASX": ".AX",
    "TSX": ".TO",
    "TSXV": ".V",
    "XCNQ": ".CN",
    "NEOE": ".NE",
    "BSP": ".SA",
    "MEX": ".MX",
    "BUE": ".BA",
    "XSGO": ".SN",
    "BOG": ".CL",
    "JSE": ".JO",
    "BOM": ".BO",
    "NSE": ".NS",
    "SGX": ".SI",
    "TPE": ".TW",
    "ROCO": ".TWO",
    "XKRX": ".KS",
    "BKK": ".BK",
    "XKLS": ".KL",
    "ISX": ".JK",
    "HSTC": ".VN",
    "XTAE": ".TA",
    "SAU": ".SAU",
    "OSTO": ".ST",
    "OSL": ".OL",
    "OHEL": ".HE",
    "OCSE": ".CO",
    "WBO": ".VI",
    "DUB": ".IR",
    "ATH": ".AT",
    "IST": ".IS",
    "WAR": ".WA",
    "XPRA": ".PR",
    "BUD": ".BD",
    "CAI": ".CA",
    "NAS": "",
    "NYSE": "",
    "AMEX": "",
    "ARCA": "",
    "GREY": "",
    "OTCBB": "",
    "OTCPK": "",
}


def get_yahoo_ticker(base_ticker: str, db_exchange: str) -> str:
    """Construit le ticker Yahoo Finance en ajoutant le suffixe approprié selon l'exchange."""
    if not db_exchange:
        return base_ticker

    db_exchange_upper = db_exchange.upper()

    # 1. Tentative par mapping GuruFocus (ISO MIC ou spécifique)
    suffix = GURUFOCUS_TO_YAHOO.get(db_exchange_upper)
    if suffix is not None:
        return f"{base_ticker}{suffix}"

    # 2. Tentative par code court (MIC tronqué ou legacy)
    suffix = DB_EXCHANGE_TO_YAHOO_SUFFIX.get(db_exchange_upper)
    if suffix is not None:
        return f"{base_ticker}{suffix}"

    # 3. Tentative par recherche dans la liste YAHOO_EXCHANGES (nom ou pays)
    for ex in YAHOO_EXCHANGES:
        if db_exchange_upper in ex.name.upper() or db_exchange_upper in ex.country.upper():
            return f"{base_ticker}{ex.suffix}"

    return base_ticker


# --- Google Finance Mapping ---

DB_EXCHANGE_TO_GOOGLE_CODE = {
    "PAR": "EPA",
    "AMS": "AMS",
    "BRU": "EBR",
    "LIS": "ELI",
    "LON": "LON",
    "FRA": "FRA",
    "GER": "ETR",
    "XETRA": "ETR",
    "MAD": "BME",
    "MIL": "BIT",
    "VX": "VTX",
    "STU": "FRA",
    "BER": "FRA",
    "MUN": "FRA",
    "DUS": "FRA",
    "HAN": "FRA",
    "TSE": "TYO",
    "HKG": "HKG",
    "ASX": "ASX",
    "TSX": "TSE",
    "TSXV": "TSXV",
    "NYQ": "NYSE",
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
}


def get_google_ticker(base_ticker: str, db_exchange: str) -> str:
    """Construit le ticker Google Finance au format EXCHANGE:SYMBOL."""
    if not db_exchange:
        return base_ticker

    db_exchange_upper = db_exchange.upper()

    # 1. Tentative par code court (MIC)
    google_code = DB_EXCHANGE_TO_GOOGLE_CODE.get(db_exchange_upper)
    if google_code:
        return f"{google_code}:{base_ticker}"

    # 2. Tentative par recherche dans la liste GOOGLE_EXCHANGES (nom ou pays)
    for code, ex in GOOGLE_EXCHANGES.items():
        if db_exchange_upper in ex.name.upper() or db_exchange_upper in ex.country.upper():
            return f"{ex.code}:{base_ticker}"

    return base_ticker
