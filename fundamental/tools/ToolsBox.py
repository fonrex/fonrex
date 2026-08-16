import re
import unicodedata
from datetime import datetime

from dateutil.parser import ParserError, parse

REGEX_MORNINGSTART_PI = r"(?:\"pi\":\")(.*?)(?:\",\"n\")"
REGEX_JUSTEFT_CANONICAL = r"(?:<link href=\")(.*?)(?:\" rel=\"canonical\"><)"
REGEX_BOURSORAMA_AVALAIBLE = r"(?:aria-labelledby=\"title-)(.*?)(?:\"    ><div class=\"c-modal)"
REGEX_PIOTROSKI_F_SCORE = r"(?: )(.*?)(?: \(As of Today\))"
REGEX_BENEISH_M_SCORE = r"(?: )(.*?)(?: \(As of Today\))"
REGEX_ROIC = r"(?:: )(.*?)(?: \(As of )"
REGEX_BOURSORAMA_GAUGE_CURRENT_STEP = r"(?:data-gauge-current-step=\")(.*?)(?:\" )"
REGEX_BOURSORAMA_GAUGE_MAX_STEP = r"(?:data-gauge-steps=\")(.*?)(?:\" )"
REGEX_CLEAN_YAHOO_TICKER = r"(?:\-)(.*?)(?:\.)"
REGEX_JUSTETF_LOGO = r"(?:\<span class=\"provider-logo__image\" style=\"background-image:url\(&#039;)(.*?)(?:&#039;\)\"><\/span>  <\/a> <h1 id=\"etf-title\")"
REGEX_JUSTETF_LOGO_LOCAL = r"(?:\<span class=\"provider-logo__image\" style=\"background-image:url\(\')(.*?)(?:\'\)\"><\/span>)"
REGEX_GURUFOCUS_SYMBOL = r"(?:'symbol': ')(.*?)(?:', 'exchange')"
REGEX_GURUFOCUS_EXCHANGE = r"(?:, 'exchange': ')(.*?)(?:', 'company')"
URL_REGEX = r"""(?i)\b((?:https?:(?:/{1,3}|[a-z0-9%])|[a-z0-9.\-]+[.](?:com|net|org|edu|gov|mil|aero|asia|biz|cat|coop|info|int|jobs|mobi|museum|name|post|pro|tel|travel|xxx|ac|ad|ae|af|ag|ai|al|am|an|ao|aq|ar|as|at|au|aw|ax|az|ba|bb|bd|be|bf|bg|bh|bi|bj|bm|bn|bo|br|bs|bt|bv|bw|by|bz|ca|cc|cd|cf|cg|ch|ci|ck|cl|cm|cn|co|cr|cs|cu|cv|cx|cy|cz|dd|de|dj|dk|dm|do|dz|ec|ee|eg|eh|er|es|et|eu|fi|fj|fk|fm|fo|fr|ga|gb|gd|ge|gf|gg|gh|gi|gl|gm|gn|gp|gq|gr|gs|gt|gu|gw|gy|hk|hm|hn|hr|ht|hu|id|ie|il|im|in|io|iq|ir|is|it|je|jm|jo|jp|ke|kg|kh|ki|km|kn|kp|kr|kw|ky|kz|la|lb|lc|li|lk|lr|ls|lt|lu|lv|ly|ma|mc|md|me|mg|mh|mk|ml|mm|mn|mo|mp|mq|mr|ms|mt|mu|mv|mw|mx|my|mz|na|nc|ne|nf|ng|ni|nl|no|np|nr|nu|nz|om|pa|pe|pf|pg|ph|pk|pl|pm|pn|pr|ps|pt|pw|py|qa|re|ro|rs|ru|rw|sa|sb|sc|sd|se|sg|sh|si|sj|Ja|sk|sl|sm|sn|so|sr|ss|st|su|sv|sx|sy|sz|tc|td|tf|tg|th|tj|tk|tl|tm|tn|to|tp|tr|tt|tv|tw|tz|ua|ug|uk|us|uy|uz|va|vc|ve|vg|vi|vn|vu|wf|ws|ye|yt|yu|za|zm|zw)/)(?:[^\s()<>{}\[\]]+|\([^\s()]*?\([^\s()]+\)[^\s()]*?\)|\([^\s]+?\))+(?:\([^\s()]*?\([^\s()]+\)[^\s()]*?\)|\([^\s]+?\)|[^\s`!()\[\]{};:'".,<>?«»“”‘’])|(?:(?<!@)[a-z0-9]+(?:[.\-][a-z0-9]+)*[.](?:com|net|org|edu|gov|mil|aero|asia|biz|cat|coop|info|int|jobs|mobi|museum|name|post|pro|tel|travel|xxx|ac|ad|ae|af|ag|ai|al|am|an|ao|aq|ar|as|at|au|aw|ax|az|ba|bb|bd|be|bf|bg|bh|bi|bj|bm|bn|bo|br|bs|bt|bv|bw|by|bz|ca|cc|cd|cf|cg|ch|ci|ck|cl|cm|cn|co|cr|cs|cu|cv|cx|cy|cz|dd|de|dj|dk|dm|do|dz|ec|ee|eg|eh|er|es|et|eu|fi|fj|fk|fm|fo|fr|ga|gb|gd|ge|gf|gg|gh|gi|gl|gm|gn|gp|gq|gr|gs|gt|gu|gw|gy|hk|hm|hn|hr|ht|hu|id|ie|il|im|in|io|iq|ir|is|it|je|jm|jo|jp|ke|kg|kh|ki|km|kn|kp|kr|kw|ky|kz|la|lb|lc|li|lk|lr|ls|lt|lu|lv|ly|ma|mc|md|me|mg|mh|mk|ml|mm|mn|mo|mp|mq|mr|ms|mt|mu|mv|mw|mx|my|mz|na|nc|ne|nf|ng|ni|nl|no|np|nr|nu|nz|om|pa|pe|pf|pg|ph|pk|pl|pm|pn|pr|ps|pt|pw|py|qa|re|ro|rs|ru|rw|sa|sb|sc|sd|se|sg|sh|si|sj|Ja|sk|sl|sm|sn|so|sr|ss|st|su|sv|sx|sy|sz|tc|td|tf|tg|th|tj|tk|tl|tm|tn|to|tp|tr|tt|tv|tw|tz|ua|ug|uk|us|uy|uz|va|vc|ve|vg|vi|vn|vu|wf|ws|ye|yt|yu|za|zm|zw)\b/?(?!@)))"""

GOOGLE_EXCHANGES = {
    "NYSE": "New York Stock Exchange",
    "NASDAQ": "The NASDAQ Stock Market, Inc. – NASDAQ Last Sale",
    "NYSE_CHANGE_IT": "NYSE AMEX",
    "NYSEARCA": "NYSE ARCA",
    "OTC": "FINRA OTC Bulletin Board",
    "PINK": "FINRA OTC Bulletin Board",
    "TSE": "Toronto Stock Exchange",
    "CVE": "Toronto TSX Ventures Exchange",
    "OPRA": "Option Chains",
    "LON": "London Stock Exchange",
    "FRA": "Deutsche Börse Frankfurt Stock Exchange",
    "ETR": "Deutsche Börse XETRA",
    "BIT": "Borsa Italiana Milan Stock Exchange",
    "EPA": "NYSE Euronext Paris",
    "EBR": "NYSE Euronext Brussels",
    "ELI": "NYSE Euronext Lisbon",
    "AMS": "NYSE Euronext Amsterdam",
    "BOM": "Bombay Stock Exchange Limited",
    "NSE": "National Stock Exchange of India",
    "SHA": "Shanghai Stock Exchange",
    "SHE": "Shenzhen Stock Exchange",
    "TPE": "Taiwan Stock Exchange",
    "HKG": "Hong Kong Stock Exchange",
    "TYO": "Tokyo Stock Exchange",
    "ASX": "Australian Securities Exchange",
    "NZE": "New Zealand Stock Exchange",
    "WSE": "Warsaw Stock Exchange",
    "OTCMKTS": "OTCMKTS",
}


class ToolsBox:
    def __init__(self):
        pass

    def extractYahooTickerFromMoning(self, test_str):
        tmpFound = test_str.split("• ")
        if tmpFound[1] is None:
            return None
        else:
            return tmpFound[1].strip()

    def extractYahooNameFromMoning(self, test_str):
        tmpFound = test_str.split("• ")
        if tmpFound[0] is None:
            return None
        else:
            cleaner = tmpFound[0].split(".")
            return cleaner[0].replace("Ltd", "").strip()

    def cleanYahooTicker(self, yahooticker):
        if "." in yahooticker:
            tmp = yahooticker.split(".")
            if tmp[0].isnumeric():
                if len(tmp[0]) <= 5:
                    calcul = 5 - len(tmp[0])
                    for i in range(calcul):
                        tmp[0] = "0" + tmp[0]
                    return tmp[0]
            return tmp[0]
        else:
            return yahooticker

    def cleanGoogleTicker(self, gooleticker):
        if ":" in gooleticker:
            tmp = gooleticker.split(":")
            if tmp[0].isnumeric():
                if len(tmp[0]) <= 5:
                    calcul = 5 - len(tmp[0])
                    for i in range(calcul):
                        tmp[0] = "0" + tmp[0]
                    return tmp[0]
            return tmp[0]
        else:
            return gooleticker

    def cleanTitleForMorningStar(self, title):
        if title is None:
            return ""

        # For tickers with a suffix (.PA, .L, etc.), only keep the main part
        if "." in title:
            tmp = title.split(".")
            return tmp[0]
        else:
            return title

    def cleanLongNameFromYahoo(self, longName):
        if ", " in longName:
            tmp = longName.split(", ")
            return tmp[0]
        else:
            return longName

    def strip_accents(self, text):
        """
        Strip accents from input String.

        :param text: The input string.
        :type text: String.

        :returns: The processed String.
        :rtype: String.
        """
        try:
            text = unicode(text, "utf-8")
        except (TypeError, NameError):  # unicode is a default on python 3
            pass
        text = unicodedata.normalize("NFD", text)
        text = text.encode("ascii", "ignore")
        text = text.decode("utf-8")
        return str(text)

    def cleanLongNameForMorningStar(self, longName):
        if longName is not None:
            return self.strip_accents(
                str(longName.replace("Limited", "Ltd").replace(",", "").replace(".", ""))
            )

    def replaceBlankSpaceByPlus(self, string):
        return str(string.replace(" ", "+")).lower()

    def extractValueFromInvestingResponse(self, test_str, symbol, exchange):
        REGEX_INVESTING_RESPONSE = (
            r"(?:{\"pairId\":)(.*?)(?:\"symbol\":\""
            + symbol
            + '")(.*?)(?:"exchange":"'
            + exchange
            + '")'
        )
        matches = re.finditer(REGEX_INVESTING_RESPONSE, str(test_str), re.MULTILINE)
        for matchNum, match in enumerate(matches, start=1):
            for groupNum in range(0, len(match.groups())):
                groupNum = groupNum + 1
                return match.group(groupNum)

    def extractMorningStartId(self, test_str, yahooTicker):
        # If yahooTicker is empty or None, use the generic method
        if not yahooTicker:
            return self.extractMorningStartIdForETF(test_str)

        # Regular expression to find the Morningstar ID with the Yahoo ticker
        REGEX_MORNINGSTART = r"(?:\"pi\":\")(.*?)(?:\",\"s\":\"" + yahooTicker + '")'
        tmpFound = ""
        matches = re.finditer(REGEX_MORNINGSTART, test_str, re.MULTILINE)

        for matchNum, match in enumerate(matches, start=1):
            # Extract the Morningstar ID
            matchesToFoundPi = re.finditer(REGEX_MORNINGSTART_PI, match.group(), re.MULTILINE)
            for matchNum, match in enumerate(matchesToFoundPi, start=1):
                for groupNum in range(0, len(match.groups())):
                    groupNum = groupNum + 1
                    tmpFound = match.group(groupNum)
                    if tmpFound == "" or tmpFound is None:
                        # If no result, try the alternative method
                        return self.extractMorningStartId2(test_str, yahooTicker)
                    else:
                        return tmpFound

        # If no result with the first method, try to extract the first available ID
        if not tmpFound or tmpFound == "":
            # Try to simply extract the first Morningstar ID found (useful for French stocks)
            return self.extractMorningStartIdForETF(test_str)

        return tmpFound

    def extractMorningStartIWithStockName(self, test_str, name):
        REGEX_MORNINGSTART = r"(?:\"pi\":\")(.*?)(?:\",\"n\":\"" + name + '")'
        tmpFound = ""
        matches = re.finditer(REGEX_MORNINGSTART, test_str, re.MULTILINE)
        for matchNum, match in enumerate(matches, start=1):
            # extract pid id
            matchesToFoundPi = re.finditer(REGEX_MORNINGSTART_PI, match.group(), re.MULTILINE)
            for matchNum, match in enumerate(matchesToFoundPi, start=1):
                for groupNum in range(0, len(match.groups())):
                    groupNum = groupNum + 1
                    tmpFound = match.group(groupNum)
                    return tmpFound

    def extractMorningStartIdForETF(self, test_str):
        matches = re.finditer(REGEX_MORNINGSTART_PI, test_str, re.MULTILINE)
        for matchNum, match in enumerate(matches, start=1):
            for groupNum in range(0, len(match.groups())):
                groupNum = groupNum + 1
                return match.group(groupNum)

    def extractMorningStartId2(self, test_str, yahooTicker):
        lastChanceToFoundId = ""
        if yahooTicker in test_str:
            REGEX_MORNINGSTART = r"(?:\"pi\":\")(.*?)(?:\",\"s\":\"" + yahooTicker + "*?)"
            matches = re.finditer(REGEX_MORNINGSTART, test_str, re.MULTILINE)
            for matchNum, match in enumerate(matches, start=1):
                # print ("Match {matchNum} was found at {start}-{end}: {match}".format(matchNum = matchNum, start = match.start(), end = match.end(), match = match.group()))
                # extract pid id
                matchesToFoundPi = re.finditer(REGEX_MORNINGSTART_PI, match.group(), re.MULTILINE)
                for matchNum, match in enumerate(matchesToFoundPi, start=1):
                    for groupNum in range(0, len(match.groups())):
                        groupNum = groupNum + 1
                        lastChanceToFoundId = match.group(groupNum)
        return lastChanceToFoundId

    def extractAnySetence(self, test_str, regex):
        tmpFound = ""
        matches = re.finditer(regex, str(test_str), re.MULTILINE)
        for matchNum, match in enumerate(matches, start=1):
            for groupNum in range(0, len(match.groups())):
                groupNum = groupNum + 1
                tmpFound = match.group(groupNum)
        return tmpFound

    def yahooExchangeToGoogle(self, yahooMarketName):
        for x in GOOGLE_EXCHANGES:
            if (
                str(self.cleanMarketName(yahooMarketName)).lower()
                in str(GOOGLE_EXCHANGES[x]).lower()
            ):
                return x
        return None

    def yahooExhangeToGoogleExchange(self, yahooMarketName):
        for x in GOOGLE_EXCHANGES:
            if self.cleanMarketName(yahooMarketName) in GOOGLE_EXCHANGES[x]:
                return GOOGLE_EXCHANGES[x]
        return None

    def stripTrailingSlash(self, url):
        if url[-1] == "/":
            url = url[:-1]
        if len(url.rsplit("/", 1)[-1]) > 0:
            return url.rsplit("/", 1)[-1]
        else:
            return url

    def regexExtractMultiData(self, body, regex):
        listFound = []
        matches = re.finditer(regex, body, re.MULTILINE)

        for matchNum, match in enumerate(matches, start=1):
            for groupNum in range(0, len(match.groups())):
                groupNum = groupNum + 1
                listFound.append(match.group(groupNum))
        return listFound

    def cleanMarketName(self, name):
        if "HKSE" == name:
            name = "Hong Kong Stock Exchange"
        if "JPX" == name:
            name = "Tokyo Stock Exchange"
        if "VAN" == name:
            name = "TSX"
        if "TSXV" == name:
            name = "TSX"
        if "ASX" == name:
            name = "Australian"
        if "Other OTC" == name:
            name = "OTCMKTS"
        if "EURONEXT PARIS" == name:
            name = "Paris"
        if "NASDAQ" in name:
            name = "NASDAQ"
        if "LSE" in name:
            name = "London"
        if "Stockholm" in name:
            name = "Frankfurt"
        return name

    def swapString(self, string, keytoswap):
        if keytoswap in string:
            tmp = string.split(keytoswap)
            return tmp[1] + keytoswap + tmp[0]

    def googleToGurufocusCode(self, googleTicker):
        getNewCode = googleTicker.split(":")
        if len(getNewCode) > 1:
            if getNewCode[0] == "EPA":
                return "XPAR:" + getNewCode[1]
            if getNewCode[0] == "NASDAQ":
                return getNewCode[1]
            else:
                return googleTicker

    def cleanLongNameForGurufocus(self, name):
        if "Limited" in name:
            name = name.replace("Limited", "")
        if "," in name:
            name = name.replace(",", "")
        if name[len(name) - 1] == ".":
            name = name[0 : len(name) - 1]
        return name.strip()

    def cleanGoogleTickerForGurufocus(self, googleTicker):
        getNewCode = googleTicker.split(":")
        if len(getNewCode) > 1:
            if getNewCode[1].isnumeric():
                if len(getNewCode[1]) <= 5:
                    calcul = 5 - len(getNewCode[1])
                    for i in range(calcul):
                        getNewCode[1] = "0" + getNewCode[1]
                    return getNewCode[1]
            return getNewCode[1]

    def is_float(self, string):
        try:
            # float() is a built-in function
            float(string)
            return True
        except ValueError:
            return False

    def is_int(self, string):
        try:
            int(string)
            return True
        except ValueError:
            return False

    def convertUSADateTime(self, dateStr):
        new_date = str(datetime.strptime(dateStr, "%m/%d/%Y"))
        if "00:00:00" in new_date:
            return new_date.replace(" 00:00:00", "")
        return new_date

    def is_valid_date(self, date):
        if date:
            try:
                parse(date)
                return True
            except (ParserError, TypeError, OverflowError):
                return False
        return False

    def isUSADate(self, date):
        r = re.compile(r"\d{2}/\d{2}/\d{4}")
        if r.match(date) is not None:
            return True
        else:
            return False

    def createValideUSADate(self, date):
        tmp = date.split("/")
        if len(tmp) == 3:
            if len(tmp[0]) == 1:
                date = "0" + date
        return date

    def extract_information(input, sub1, sub2):
        """
        Function to extract the id from the input
        Args:
            input (str): input string
        Returns:
            passenger id
        """
        idx1 = input.index(sub1) + len(sub1)
        idx2 = input.index(sub2)
        id = input[idx1:idx2]
        return id

    def YahooFinanceTickerToGoogleFinance(self, yahooTicker):
        print("yahooTicker: ", yahooTicker)
        if "." in yahooTicker:
            tmp = yahooTicker.split(".")
            print("tmp: ", tmp)
            if tmp[1] is not None:
                if tmp[1] == "PA":
                    return "EPA:" + tmp[0]
                elif tmp[1] == "DE":
                    return "ETR:" + tmp[0]
                elif tmp[1] == "AS":
                    return "AMS:" + tmp[0]
                elif tmp[1] == "BR":
                    return "EBR:" + tmp[0]
                elif tmp[1] == "MC":
                    return "BME:" + tmp[0]
                elif tmp[1] == "L":
                    return "LON:" + tmp[0]
                elif tmp[1] == "VI":
                    return "VIE:" + tmp[0]
                elif tmp[1] == "SW":
                    return "STO:" + tmp[0]
                elif tmp[1] == "HE":
                    return "HEL:" + tmp[0]
                elif tmp[1] == "CO":
                    return "CPH:" + tmp[0]
                elif tmp[1] == "OL":
                    return "OSL:" + tmp[0]
        else:
            return yahooTicker

    def googleFinanceToYahooFinance(self, googleTicker):
        """
        Converts a Google Finance ticker (e.g., EPA:AMUN) to Yahoo Finance (e.g., AMUN.PA)
        """
        if ":" not in googleTicker:
            return googleTicker

        exchange, symbol = googleTicker.split(":", 1)

        # Mapping Google Finance exchanges to Yahoo Finance suffixes
        google_to_yahoo_suffix = {
            "EPA": ".PA",  # NYSE Euronext Paris
            "EBR": ".BR",  # NYSE Euronext Brussels
            "AMS": ".AS",  # NYSE Euronext Amsterdam
            "ETR": ".DE",  # Deutsche Börse XETRA
            "FRA": ".F",  # Frankfurt Stock Exchange
            "LON": ".L",  # London Stock Exchange
            "BIT": ".MI",  # Borsa Italiana Milan
            "TYO": ".T",  # Tokyo Stock Exchange
            "HKG": ".HK",  # Hong Kong Stock Exchange
            "TSE": ".TO",  # Toronto Stock Exchange
            "ASX": ".AX",  # Australian Securities Exchange
            "WSE": ".WA",  # Warsaw Stock Exchange
            "NASDAQ": "",  # NASDAQ (no suffix)
            "NYSE": "",  # NYSE (no suffix)
        }

        suffix = google_to_yahoo_suffix.get(exchange, "")
        return symbol + suffix


if __name__ == "__main__":
    test_str = (
        "Actions|||"
        + 'Delta Plus Group|{"i":"0P00009WEQ","pi":"0P00009WEQ","n":"Delta Plus Group","p":"","s":"ALDLT","ar":"","sr":"","e":"EURONEXT","e1":"","t":3}|STOCK|ALDLT|EURONEXT|Actions'
        + 'Delta Plus Group|{"i":"0P0001M5T5","pi":"0P0001M5T5","n":"Delta Plus Group","p":"","s":"7E1","ar":"","sr":"","e":"XFRA","e1":"","t":3}|STOCK|7E1|XFRA|Actions'
        + 'Delta Plus Group|{"i":"0P0001M5QS","pi":"0P0001M5QS","n":"Delta Plus Group","p":"","s":"7E1","ar":"","sr":"","e":"XMUN","e1":"","t":3}|STOCK|7E1|XMUN|Actions'
        + "2 autre(s) Actions...||STOCK|"
        + '           <iframe style="margin:-8px" id="iframepage" scrolling="no" height="30px" width="100%" frameborder="0" src="https://msmedia.morningstar.com/mstar/hserver/site=ms.fr/size=searchtextlink/area=search/pos=toplink/usrt=v/random=1396249701/viewid=2021290430/language=fr-FR"></iframe>|{"t":0}||'
    )

    tools = ToolsBox()
    print(tools.extractMorningStartId(test_str, tools.cleanYahooTicker("ALDLT.PA")))
    print(tools.cleanYahooTicker("ALDLT.PA"))
