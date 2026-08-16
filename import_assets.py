import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import aiohttp
import yfinance as yf

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm import Session

from database.service import DatabaseService
from models import Asset, AssetListing, AssetMapping

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Suppress yfinance internal HTTP error logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

STATIC_DIR = "static/logos"
DEFAULT_LOGO_PATH = os.path.join("static", "logos", "default.svg")
LOGO_TOKEN = os.getenv("LOGO_TOKEN")
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CONCURRENCY = 20  # Reduced concurrency to be safe with Yahoo rate limits if any
BATCH_SIZE = 100  # Smaller batch size to save progress more frequently
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ISIN_DATA_DIR = os.path.join(SCRIPT_DIR, "data", "isin_data")

# Reverse lookup map (Legacy suffix/code -> Google Exchange Code)
YAHOO_TO_GOOGLE_CODE = {
    "PA": "EPA",  # Paris
    "DE": "ETR",  # XETRA (Germany) - often defaults to ETR
    "AS": "AMS",  # Amsterdam
    "BR": "EBR",  # Brussels
    "MC": "BME",  # Madrid
    "L": "LON",  # London
    "VI": "VIE",  # Vienna
    "SW": "STO",  # Swiss
    "HE": "HEL",  # Helsinki
    "CO": "CPH",  # Copenhagen
    "OL": "OSL",  # Oslo
    "TO": "TSE",  # Toronto
    "HK": "HKG",  # Hong Kong
    "T": "TYO",  # Tokyo
    "MI": "BIT",  # Milan
}

# Country Name to ISO 2-letter Code Mapping
COUNTRY_NAME_TO_CODE = {
    "United States": "US",
    "France": "FR",
    "Germany": "DE",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Japan": "JP",
    "Australia": "AU",
    "Switzerland": "CH",
    "China": "CN",
    "India": "IN",
    "Brazil": "BR",
    "Netherlands": "NL",
    "Sweden": "SE",
    "Spain": "ES",
    "Italy": "IT",
    "South Korea": "KR",
    "Russia": "RU",
    "Singapore": "SG",
    "Hong Kong": "HK",
    "Mexico": "MX",
    "South Africa": "ZA",
    "Belgium": "BE",
    "Norway": "NO",
    "Denmark": "DK",
    "Finland": "FI",
    "Ireland": "IE",
    "Israel": "IL",
    "New Zealand": "NZ",
    "Austria": "AT",
    "Portugal": "PT",
    "Poland": "PL",
    "Turkey": "TR",
    "Taiwan": "TW",
    "Thailand": "TH",
    "Malaysia": "MY",
    "Indonesia": "ID",
    "Greece": "GR",
}

# ── Import Constants ────────────────────────────────────────────────────────

BATCH_SIZE_IMPORT = 200

# Primary currencies: listing in the native currency of the instrument
PRIMARY_CURRENCIES = {"USD", "GBP", "JPY", "CHF", "CAD", "AUD"}

# Known ticker suffixes -> exchange MIC code
EU_EXCHANGES_BY_SUFFIX = {
    ".PA": "XPAR",  # Euronext Paris
    ".AS": "XAMS",  # Euronext Amsterdam
    ".BR": "XBRU",  # Euronext Bruxelles
    ".DE": "XETR",  # XETRA Frankfurt
    ".F": "XFRA",  # Frankfurt
    ".L": "XLON",  # London
    ".MI": "XMIL",  # Milan
    ".MC": "XMAD",  # Madrid
    ".ST": "XSTO",  # Stockholm
    ".HE": "XHEL",  # Helsinki
    ".SW": "XSWX",  # Swiss Exchange
}

# MIC -> code Google Finance
GOOGLE_EXCHANGE_MAP = {
    "XPAR": "EPA",
    "XAMS": "AMS",
    "XBRU": "EBR",
    "XETR": "ETR",
    "XLON": "LON",
    "XMIL": "BIT",
    "XMAD": "BME",
    "XSTO": "STO",
    "XHEL": "HEL",
    "XSWX": "SWX",
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
}

# Validation CSV
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{10}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_VALID_PRODUCT_TYPES = {"STOCK", "ETF"}


# ── Data Structures ──────────────────────────────────────────────────────────


@dataclass
class CSVRow:
    """Represents a CSV row after parsing and validation."""

    name: str
    ticker: str
    isin: str
    product_type: str  # "STOCK" | "ETF"
    currency: str
    exchange: Optional[str] = None
    dedup_key: str = field(init=False)

    def __post_init__(self):
        self.ticker = self.ticker.strip().upper()
        self.isin = self.isin.strip().upper()
        self.currency = self.currency.strip().upper()
        self.product_type = self.product_type.strip().upper()
        self.name = self.name.strip()
        self.dedup_key = f"{self.isin}|{self.ticker}|{self.currency}"


@dataclass
class ImportStats:
    """Statistics for an import run."""

    total_csv_rows: int = 0
    duplicates_in_csv: int = 0
    invalid_rows: int = 0
    assets_created: int = 0
    assets_updated: int = 0
    listings_created: int = 0
    listings_skipped: int = 0
    mappings_created: int = 0
    errors: int = 0
    duration_seconds: float = 0.0

    def print_summary(self):
        print(f"""
╔══════════════════════════════════════════════╗
║                IMPORT SUMMARY                ║
╠══════════════════════════════════════════════╣
║  CSV rows read          : {self.total_csv_rows:>6}             ║
║  CSV duplicates filtered: {self.duplicates_in_csv:>6}             ║
║  Invalid rows           : {self.invalid_rows:>6}             ║
╠══════════════════════════════════════════════╣
║  Assets created         : {self.assets_created:>6}             ║
║  Assets updated         : {self.assets_updated:>6}             ║
║  Listings created       : {self.listings_created:>6}             ║
║  Listings skipped(exist): {self.listings_skipped:>6}             ║
║  Mappings created       : {self.mappings_created:>6}             ║
╠══════════════════════════════════════════════╣
║  Errors                 : {self.errors:>6}             ║
║  Duration               : {self.duration_seconds:>5.1f}s             ║
╚══════════════════════════════════════════════╝
""")


# ── Parsing and Validation ───────────────────────────────────────────────────


def parse_csv(file_path: Path) -> Tuple[List[CSVRow], int, int]:
    """
    Reads the CSV and returns (valid_rows, nb_duplicates, nb_invalid).

    Validation: name not empty, ticker ≤ 20 chars, ISIN regex,
    productType ∈ {STOCK,ETF}, 3-letter currency code.
    Deduplication on key (isin, ticker, currency).
    """
    valid_rows: List[CSVRow] = []
    seen_keys: set = set()
    nb_duplicates = 0
    nb_invalid = 0

    with open(file_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for line_num, raw in enumerate(reader, start=2):
            name = (raw.get("name") or "").strip()
            ticker_raw = (raw.get("ticker") or "").strip().upper()
            isin_raw = (raw.get("isin") or "").strip().upper()
            product_type = (raw.get("productType") or "").strip().upper()
            currency_raw = (raw.get("currency") or "").strip().upper()

            errors: List[str] = []
            if not name:
                errors.append("empty name")
            if not ticker_raw or len(ticker_raw) > 20:
                errors.append(f"invalid ticker: {ticker_raw!r}")
            if not _ISIN_RE.match(isin_raw):
                errors.append(f"invalid ISIN: {isin_raw!r}")
            if product_type not in _VALID_PRODUCT_TYPES:
                errors.append(f"invalid productType: {product_type!r}")
            if not _CURRENCY_RE.match(currency_raw):
                errors.append(f"invalid currency: {currency_raw!r}")

            if errors:
                logger.warning(
                    "Row %d ignored — ticker=%s isin=%s (%s)",
                    line_num,
                    ticker_raw,
                    isin_raw,
                    "; ".join(errors),
                )
                nb_invalid += 1
                continue

            row = CSVRow(
                name=name,
                ticker=ticker_raw,
                isin=isin_raw,
                product_type=product_type,
                currency=currency_raw,
            )

            if row.dedup_key in seen_keys:
                logger.debug("CSV duplicate ignored: %s", row.dedup_key)
                nb_duplicates += 1
                continue

            seen_keys.add(row.dedup_key)
            valid_rows.append(row)

    return valid_rows, nb_duplicates, nb_invalid


def determine_exchange(row: CSVRow) -> Optional[str]:
    """
    Deduces the MIC from the ticker suffix.
    Ex: AIR.PA -> XPAR, BMW.DE -> XETR, AAPL -> None.
    """
    for suffix, mic in EU_EXCHANGES_BY_SUFFIX.items():
        if row.ticker.endswith(suffix):
            return mic
    return None


def determine_is_primary(row: CSVRow, existing_listings_for_isin: list) -> bool:
    """
    Rules:
    1. First listing -> True
    2. Primary already exists -> False
    3. Currency in PRIMARY_CURRENCIES and no primary yet -> True
    4. Else -> False
    """
    if not existing_listings_for_isin:
        return True
    has_primary = any(getattr(lst, "is_primary", False) for lst in existing_listings_for_isin)
    if has_primary:
        return False
    return row.currency in PRIMARY_CURRENCIES


# ── Import Pipeline ───────────────────────────────────────────────────────────


class AssetImporter:
    """
    Orchestrates CSV import -> assets / asset_listings / asset_mappings.
    Groups rows by ISIN to guarantee intra-batch uniqueness.
    Commit by batch — an erroring batch does not rollback previous ones.
    """

    def __init__(
        self, db_session: Session, dry_run: bool = False, batch_size: int = BATCH_SIZE_IMPORT
    ):
        self.db = db_session
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.stats = ImportStats()

    def run(self, rows: List[CSVRow]) -> ImportStats:
        """Processes all rows by batch. Returns statistics."""
        start = time.time()
        batches = [rows[i : i + self.batch_size] for i in range(0, len(rows), self.batch_size)]

        for i, batch in enumerate(batches, 1):
            logger.info("Batch %d/%d — %d rows", i, len(batches), len(batch))
            try:
                self._process_batch(batch)
            except Exception as exc:
                logger.error("Batch %d error: %s", i, exc)
                self.stats.errors += len(batch)
                if not self.dry_run:
                    self.db.rollback()

        # Releasing a SAVEPOINT can make changes durable on SQLite when there
        # was no explicit outer transaction. A final rollback gives dry-run the
        # same no-write guarantee on every supported database.
        if self.dry_run:
            self.db.rollback()

        self.stats.duration_seconds = time.time() - start
        return self.stats

    def _process_batch(self, rows: List[CSVRow]) -> None:
        """Groups by ISIN then upserts asset + listings + mappings."""
        isin_groups: Dict[str, List[CSVRow]] = defaultdict(list)
        for row in rows:
            isin_groups[row.isin].append(row)

        for isin, isin_rows in isin_groups.items():
            sp = self.db.begin_nested()
            try:
                asset = self._upsert_asset(isin_rows)
                existing_listings: List[AssetListing] = []

                for row in isin_rows:
                    listing, created = self._upsert_listing(asset, row, existing_listings)
                    if listing is not None:
                        existing_listings.append(listing)
                        if created:
                            self.stats.listings_created += 1
                            self._create_default_mappings(asset, listing, row)
                        else:
                            self.stats.listings_skipped += 1

                if self.dry_run:
                    sp.rollback()
                else:
                    sp.commit()
            except Exception as exc:
                logger.error("ISIN %s error: %s", isin, exc)
                self.stats.errors += 1
                sp.rollback()

        if not self.dry_run:
            self.db.commit()

    def _upsert_asset(self, rows_for_isin: List[CSVRow]) -> Asset:
        """
        Upsert on ISIN: searches by ISIN, updates or creates.
        Returns the Asset with its id (after flush).
        """
        isin = rows_for_isin[0].isin
        first = rows_for_isin[0]

        asset = self.db.query(Asset).filter(Asset.isin == isin).first()
        if asset:
            if not asset.name and first.name:
                asset.name = first.name
            if not asset.currency and first.currency:
                asset.currency = first.currency
            self.stats.assets_updated += 1
        else:
            # Check for ticker collision before INSERT to avoid UniqueViolation
            ticker = first.ticker
            if ticker:
                collision = self.db.query(Asset).filter(Asset.ticker == ticker).first()
                if collision:
                    logger.warning(
                        "Ticker '%s' already taken by ISIN=%s — asset ISIN=%s will use ISIN as ticker",
                        ticker,
                        collision.isin,
                        isin,
                    )
                    ticker = isin

            asset = Asset(
                isin=isin,
                name=first.name,
                ticker=ticker,
                currency=first.currency,
                quote_type=first.product_type,
                is_active=True,
            )
            self.db.add(asset)
            self.db.flush()
            self.stats.assets_created += 1
            logger.debug("Asset created — ISIN=%s id=%s ticker=%s", isin, asset.id, ticker)

        return asset

    def _upsert_listing(
        self,
        asset: Asset,
        row: CSVRow,
        existing_listings: list,
    ) -> Tuple[Optional[AssetListing], bool]:
        """
        Upsert on (asset_id, ticker, exchange, currency).
        Returns (listing, created). In dry_run returns an unpersisted object.
        """
        exchange = determine_exchange(row) or ""

        listing = (
            self.db.query(AssetListing)
            .filter_by(
                asset_id=asset.id,
                ticker=row.ticker,
                exchange=exchange,
                currency=row.currency,
            )
            .first()
        )

        if listing:
            return listing, False

        is_primary = determine_is_primary(row, existing_listings)

        if self.dry_run:
            mock = AssetListing(
                asset_id=asset.id,
                ticker=row.ticker,
                exchange=exchange,
                currency=row.currency,
                is_primary=is_primary,
                is_active=True,
                source="csv_import",
            )
            logger.info(
                "[DRY-RUN] simulated listing — ticker=%s currency=%s primary=%s",
                row.ticker,
                row.currency,
                is_primary,
            )
            return mock, True

        listing = AssetListing(
            asset_id=asset.id,
            ticker=row.ticker,
            exchange=exchange,
            currency=row.currency,
            source="csv_import",
            is_primary=is_primary,
            is_active=True,
        )
        self.db.add(listing)
        self.db.flush()
        logger.debug(
            "Listing created — id=%s ticker=%s currency=%s primary=%s",
            listing.id,
            row.ticker,
            row.currency,
            is_primary,
        )
        return listing, True

    def _create_default_mappings(
        self,
        asset: Asset,
        listing: AssetListing,
        row: CSVRow,
    ) -> None:
        """Creates default YahooFinance and GoogleFinance mappings."""
        if self.dry_run:
            return

        exchange = listing.exchange or None

        candidates: List[Tuple[str, str, str]] = []
        yahoo_url = self._build_yahoo_url(row.ticker, row.currency)
        if yahoo_url:
            candidates.append(("YahooFinance", row.ticker, yahoo_url))

        google_url = self._build_google_url(row.ticker, exchange)
        if google_url:
            candidates.append(("GoogleFinance", row.ticker, google_url))

        for provider_name, provider_ticker, provider_url in candidates:
            existing = (
                self.db.query(AssetMapping)
                .filter_by(
                    asset_listing_id=listing.id,
                    provider_name=provider_name,
                )
                .first()
            )
            if existing:
                continue
            mapping = AssetMapping(
                asset_id=asset.id,
                asset_listing_id=listing.id,
                provider_name=provider_name,
                provider_ticker=provider_ticker,
                provider_url=provider_url,
                source="csv_import",
                confidence_score=1.0,
                is_active=True,
            )
            self.db.add(mapping)
            self.stats.mappings_created += 1

        self.db.flush()

    def _build_yahoo_url(self, ticker: str, currency: str) -> Optional[str]:
        """Yahoo Finance URL — None if ambiguity is too high (EUR without suffix)."""
        for suffix in EU_EXCHANGES_BY_SUFFIX:
            if ticker.endswith(suffix):
                return f"https://finance.yahoo.com/quote/{ticker}"
        if currency in PRIMARY_CURRENCIES:
            return f"https://finance.yahoo.com/quote/{ticker}"
        if "." not in ticker:
            return None
        return f"https://finance.yahoo.com/quote/{ticker}"

    def _build_google_url(self, ticker: str, exchange: Optional[str]) -> Optional[str]:
        """Google Finance URL — None if exchange is unknown."""

        if not exchange:
            return None
        google_code = GOOGLE_EXCHANGE_MAP.get(exchange)
        if not google_code:
            return None
        clean_ticker = ticker.split(".")[0] if "." in ticker else ticker
        return f"https://www.google.com/finance/quote/{clean_ticker}:{google_code}"


def get_google_ticker(symbol: str, exchange: Optional[str] = None) -> Optional[str]:
    """
    Resolves to Google Finance Ticker format (Symbol:Exchange) for URLs.
    Example: 'AIR.PA' -> 'AIR:EPA'
    """
    if not symbol:
        return None

    # 1. Check if already contains :
    if ":" in symbol:
        return symbol

    # 2. Handle Dots (Yahoo Style)
    if "." in symbol:
        parts = symbol.split(".")
        ticker_clean = parts[0]
        extension = parts[1]

        # Use mapping
        exchange_code = YAHOO_TO_GOOGLE_CODE.get(extension)

        if exchange_code:
            return f"{ticker_clean}:{exchange_code}"

        # Fallback for unknown extensions
        return f"{ticker_clean}:{extension}"

    # 3. Use an explicit exchange when known. Without reliable exchange, we don't
    # default to NASDAQ to avoid fake ETF mappings.
    if exchange:
        return f"{symbol}:{exchange.strip().upper()}"

    return None


def _select_yahoo_search_quote(
    quotes: List[Dict[str, Any]], query: str
) -> Optional[Dict[str, Any]]:
    if not quotes:
        return None

    normalized_query = (query or "").strip().upper()

    def rank(quote: Dict[str, Any]):
        symbol = normalize_ticker(quote.get("symbol")) or ""
        quote_type = normalize_ticker(quote.get("quoteType")) or ""
        return (
            not quote.get("isYahooFinance", True),
            symbol != normalized_query,
            quote_type not in ("EQUITY", "ETF", "MUTUALFUND", "INDEX"),
            -(quote.get("score") or 0),
        )

    return sorted(quotes, key=rank)[0]


def fetch_yahoo_search_quote(query: str) -> Optional[Dict[str, Any]]:
    """
    Resolves an ISIN or free query via Yahoo Finance search API.
    Returns the best quotes[] entry, or None if Yahoo does not respond.
    """

    query = normalize_ticker(query)
    if not query:
        return None

    url = f"{YAHOO_SEARCH_URL}?q={quote_plus(query)}"
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Yahoo search lookup failed for {query}: {e}")
        return None

    return _select_yahoo_search_quote(payload.get("quotes") or [], query)


def _quote_metadata(quote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not quote:
        return {}

    return {
        "name": quote.get("shortname") or quote.get("longname"),
        "display_name": quote.get("longname") or quote.get("shortname"),
        "official_symbol": quote.get("symbol"),
        "symbol": quote.get("symbol"),
        "exchange": quote.get("exchange"),
        "sector": quote.get("sector"),
        "industry": quote.get("industry"),
        "quote_type": quote.get("quoteType"),
    }


def _clean_yahoo_isin(value: Optional[str]) -> Optional[str]:
    normalized = normalize_ticker(value)
    if normalized in ("-", "N/A", "NA", "NONE", "NULL"):
        return None
    return normalized


def fetch_yfinance_data(ticker: str, isin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetches cold metadata from yfinance synchronously.
    When an ISIN is available, resolves the Yahoo Finance symbol through
    query1.finance.yahoo.com first because CSV tickers are not always Yahoo
    compatible.
    Returns a dictionary with mapped fields or None if error.
    """
    if not ticker:
        return None

    search_quote = fetch_yahoo_search_quote(isin) if isin else None
    search_metadata = _quote_metadata(search_quote)
    yahoo_symbol = search_metadata.get("symbol") or ticker

    try:
        # Use Ticker object
        dat = yf.Ticker(yahoo_symbol)

        # Access info to trigger the download
        # Note: .info can be blocking and slow
        info = dat.info

        if (
            not info or len(info) <= 1
        ):  # yfinance sometimes returns {"trailingPegRatio": None} for missing tickers
            return search_metadata or None

        # Build Profile Object
        profile = {
            "country": info.get("country"),
            "city": info.get("city"),
            "fullTimeEmployees": info.get("fullTimeEmployees"),
            "auditRisk": info.get("auditRisk"),
            "boardRisk": info.get("boardRisk"),
            "compensationRisk": info.get("compensationRisk"),
            "shareHolderRightsRisk": info.get("shareHolderRightsRisk"),
            "overallRisk": info.get("overallRisk"),
        }

        country = info.get("country")

        # Map fields
        return {
            "name": info.get("shortName") or info.get("longName") or search_metadata.get("name"),
            "display_name": info.get("longName")
            or info.get("shortName")
            or search_metadata.get("display_name"),
            "official_symbol": info.get("symbol") or search_metadata.get("official_symbol"),
            "symbol": info.get("symbol") or search_metadata.get("symbol") or yahoo_symbol,
            "exchange": info.get("exchange") or search_metadata.get("exchange"),
            "currency": info.get("currency"),
            "sector": info.get("sector") or search_metadata.get("sector"),
            "industry": info.get("industry") or search_metadata.get("industry"),
            "description": info.get("longBusinessSummary"),
            "long_business_summary": info.get("longBusinessSummary"),
            "website": info.get("website"),
            "ir_website": info.get("irWebsite") or info.get("website"),
            "country": country,
            "quote_type": info.get("quoteType") or search_metadata.get("quote_type"),
            "fund_family": info.get("fundFamily"),
            "isin": _clean_yahoo_isin(info.get("isin")) or _clean_yahoo_isin(isin),
            "profile": profile,
        }

    except Exception:
        return search_metadata or None


async def download_logo(
    session: aiohttp.ClientSession, ticker: str, exchange: str
) -> Optional[str]:
    """
    Downloads logo for a ticker and returns the relative path.
    Falls back to a local default logo when the provider has no match.
    """
    if not ticker:
        return DEFAULT_LOGO_PATH

    safe_exchange = "".join(
        c for c in (exchange or "UNKNOWN") if c.isalnum() or c in (" ", "_", "-")
    ).strip()
    if not safe_exchange:
        safe_exchange = "OTHERS"

    exchange_dir = os.path.join(STATIC_DIR, safe_exchange)
    os.makedirs(exchange_dir, exist_ok=True)

    filename = f"{ticker}.webp"
    file_path = os.path.join(exchange_dir, filename)
    relative_path = os.path.join("static", "logos", safe_exchange, filename)

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return relative_path

    if not LOGO_TOKEN:
        return DEFAULT_LOGO_PATH

    url = f"https://img.logo.dev/ticker/{ticker}"
    params = {"token": LOGO_TOKEN, "format": "webp", "retina": "true", "size": "128"}

    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200:
                content = await response.read()
                with open(file_path, "wb") as f:
                    f.write(content)
                return relative_path
            elif response.status == 404:
                logger.debug(f"Logo not found (404) for {ticker} at logo.dev")
            else:
                logger.warning(f"Failed to download logo for {ticker} (HTTP {response.status})")
            return DEFAULT_LOGO_PATH
    except Exception as e:
        logger.warning(f"Using default logo for {ticker}: {e}")
        return DEFAULT_LOGO_PATH


def normalize_ticker(value: Optional[str]) -> Optional[str]:
    return value.strip().upper() if value else None


def normalize_listing_part(value: Optional[str]) -> str:
    return value.strip().upper() if value else ""


def normalize_nullable(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_isin(value: Optional[str]) -> Optional[str]:
    value = normalize_ticker(value)
    return value or None


def get_row_ticker(row: Dict[str, Any]) -> Optional[str]:
    return normalize_ticker(row.get("symbol") or row.get("ticker"))


def get_row_exchange(row: Dict[str, Any]) -> str:
    return normalize_listing_part(row.get("exchange"))


def get_row_currency(row: Dict[str, Any]) -> str:
    return normalize_listing_part(row.get("currency"))


def get_row_isin(row: Dict[str, Any]) -> Optional[str]:
    return normalize_isin(row.get("isin"))


def listing_identity(row: Dict[str, Any]):
    ticker = get_row_ticker(row)
    isin = get_row_isin(row)
    exchange = get_row_exchange(row)
    currency = get_row_currency(row)
    return (isin or "", ticker or "", exchange, currency)


def resolve_input_file(file_arg: str, base_dir: Optional[str] = None) -> str:
    """
    Resolves --file portably between macOS host and Docker container.
    Accepts a simple filename, existing relative path, or absolute path.
    """
    if os.path.isabs(file_arg):
        return file_arg

    if os.path.exists(file_arg):
        return os.path.abspath(file_arg)

    base = base_dir or DEFAULT_ISIN_DATA_DIR
    rel_base = os.path.relpath(base, SCRIPT_DIR)
    if rel_base and rel_base != "." and (file_arg.startswith(rel_base + "/") or file_arg.startswith(rel_base + os.sep)):
        return os.path.abspath(file_arg)

    return os.path.join(base, file_arg)



def build_asset_payload(
    row: Dict[str, Any], primary_listing: Optional[AssetListing] = None
) -> Dict[str, Any]:
    ticker = get_row_ticker(row)
    exchange = get_row_exchange(row) or None
    currency = get_row_currency(row) or None
    quote_type = normalize_nullable(row.get("quote_type") or row.get("productType"))

    if primary_listing:
        ticker = primary_listing.ticker
        exchange = primary_listing.exchange or None
        currency = primary_listing.currency or None

    return {
        "ticker": ticker,
        "name": normalize_nullable(row.get("name")),
        "exchange": exchange,
        "currency": currency,
        "sector": normalize_nullable(row.get("sector")),
        "industry": normalize_nullable(row.get("industry")),
        "isin": get_row_isin(row),
        "quote_type": quote_type,
        "fund_family": normalize_nullable(row.get("fund_family")),
        "long_business_summary": normalize_nullable(row.get("long_business_summary")),
        "display_name": normalize_nullable(row.get("display_name")),
        "official_symbol": normalize_nullable(row.get("official_symbol")),
        "ir_website": normalize_nullable(row.get("ir_website")),
        "logo_path": normalize_nullable(row.get("logo_path")),
        "profile": row.get("profile"),
        "country": normalize_nullable(row.get("country")),
        "country_code": normalize_ticker(row.get("country_code")),
        "is_active": True,
    }


def apply_asset_payload(asset: Asset, payload: Dict[str, Any], only_missing: bool = False):
    for key, value in payload.items():
        if value is None:
            continue
        if only_missing and getattr(asset, key, None):
            continue
        setattr(asset, key, value)


def get_or_create_asset(db_session: Session, row: Dict[str, Any]):
    isin = get_row_isin(row)
    ticker = get_row_ticker(row)
    exchange = get_row_exchange(row)
    currency = get_row_currency(row)

    asset = None
    if isin:
        asset = db_session.query(Asset).filter(Asset.isin == isin).first()
    else:
        asset = (
            db_session.query(Asset)
            .join(AssetListing, AssetListing.asset_id == Asset.id)
            .filter(
                Asset.isin.is_(None),
                AssetListing.ticker == ticker,
                AssetListing.exchange == exchange,
                AssetListing.currency == currency,
            )
            .first()
        )

    payload = build_asset_payload(row)
    if asset:
        apply_asset_payload(asset, payload, only_missing=True)
        return asset, False

    asset = Asset(**payload)
    db_session.add(asset)
    db_session.flush()
    return asset, True


def get_or_create_listing(db_session: Session, asset: Asset, row: Dict[str, Any]):
    ticker = get_row_ticker(row)
    exchange = get_row_exchange(row)
    currency = get_row_currency(row)

    listing = (
        db_session.query(AssetListing)
        .filter_by(asset_id=asset.id, ticker=ticker, exchange=exchange, currency=currency)
        .first()
    )
    created_listing = False

    if not listing:
        has_primary = (
            db_session.query(AssetListing.id).filter_by(asset_id=asset.id, is_primary=True).first()
            is not None
        )
        listing = AssetListing(
            asset_id=asset.id,
            ticker=ticker,
            exchange=exchange,
            currency=currency,
            source="import_assets",
            is_primary=not has_primary,
            is_active=True,
        )
        db_session.add(listing)
        db_session.flush()
        created_listing = True
    else:
        listing.source = listing.source or "import_assets"
        listing.is_active = True

    if listing.is_primary:
        apply_asset_payload(asset, build_asset_payload(row, primary_listing=listing))

    return listing, created_listing


def upsert_mapping(
    db_session: Session,
    listing: AssetListing,
    provider_name: str,
    provider_ticker: str,
    provider_url: str,
    confidence_score: float,
    verified_at: datetime,
):
    mapping = (
        db_session.query(AssetMapping)
        .filter_by(asset_listing_id=listing.id, provider_name=provider_name)
        .first()
    )

    if not mapping:
        mapping = AssetMapping(
            asset_id=listing.asset_id, asset_listing_id=listing.id, provider_name=provider_name
        )
        db_session.add(mapping)

    mapping.provider_ticker = provider_ticker
    mapping.provider_url = provider_url
    mapping.source = "import_assets"
    mapping.confidence_score = confidence_score
    mapping.is_active = True
    mapping.failure_count = 0
    mapping.last_verified_at = verified_at
    mapping.updated_at = verified_at


async def enrich_after_import(asset_id: int, ticker: str, db_service_instance) -> None:
    """
    Triggers yfinance enrichment after importing an asset.
    Never blocks import if enrichment fails.
    """
    try:
        from financials.enrichment.yfinance_enricher import YFinanceEnricher

        enricher = YFinanceEnricher(db_service_instance)
        result = await enricher.enrich(asset_id, ticker)
        if result.get("errors"):
            logger.warning(
                "Partial enrichment for %s (asset_id=%s): %s",
                ticker,
                asset_id,
                result["errors"],
            )
        else:
            logger.info("Deep enrichment completed for %s (asset_id=%s)", ticker, asset_id)
    except Exception as e:
        logger.warning("Deep enrichment skipped for %s (asset_id=%s): %s", ticker, asset_id, e)


async def enrich_batch(db_service_instance, assets: list, batch_size: int = 5) -> None:
    """
    Batch parallel enrichment with rate limiting.

    Args:
        db_service_instance: Instance of DatabaseService
        assets: List of tuples (asset_id, ticker)
        batch_size: Number of parallel enrichments per batch
    """
    total = len(assets)
    logger.info("Starting deep enrichment for %d assets (batches of %d)", total, batch_size)

    for i in range(0, total, batch_size):
        batch = assets[i : i + batch_size]
        tasks = [
            enrich_after_import(asset_id, ticker, db_service_instance) for asset_id, ticker in batch
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        # Rate limiting: pause between batches
        if i + batch_size < total:
            await asyncio.sleep(0.5)

    logger.info("Deep enrichment finished for %d assets", total)


async def run_enrich_only(isin: str = None, file_path: str = None, limit: int = None) -> None:
    """
    Standalone enrichment mode (--enrich-only).
    Enriches existing database assets without re-importing.
    """
    db_svc = DatabaseService()
    if not db_svc.check_connection() or not db_svc.check_migrations():
        raise RuntimeError("Database schema unavailable or outdated; run `alembic upgrade head`.")
    session = db_svc.get_session()

    try:
        from models import Asset

        if isin:
            # Enrich single asset by ISIN
            asset = session.query(Asset).filter(Asset.isin == isin.strip().upper()).first()
            if not asset:
                logger.error("ISIN %s not found in database", isin)
                return
            logger.info("Standalone enrichment for %s (%s)", asset.ticker, isin)
            await enrich_after_import(asset.id, asset.ticker, db_svc)

        elif file_path:
            # Read ISINs from CSV and enrich
            resolved_path = resolve_input_file(file_path)
            if not os.path.exists(resolved_path):
                logger.error("File %s not found", resolved_path)
                return

            isins_to_enrich = []
            with open(resolved_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_isin = get_row_isin(row)
                    row_ticker = get_row_ticker(row)
                    if row_isin or row_ticker:
                        isins_to_enrich.append((row_isin, row_ticker))

            assets_to_enrich = []
            for row_isin, row_ticker in isins_to_enrich:
                if row_isin:
                    asset = session.query(Asset).filter(Asset.isin == row_isin).first()
                elif row_ticker:
                    asset = session.query(Asset).filter(Asset.ticker == row_ticker).first()
                else:
                    continue

                if asset:
                    assets_to_enrich.append((asset.id, asset.ticker))

                if limit and len(assets_to_enrich) >= limit:
                    break

            logger.info("Found %d assets to enrich", len(assets_to_enrich))
            await enrich_batch(db_svc, assets_to_enrich)

        else:
            logger.error("--enrich-only requires --isin or --file")

    finally:
        session.close()


# process_import removed — replaced by AssetImporter (synchronous).
# Use AssetImporter.run() directly or the main() function below.


def _legacy_process_import_stub(input_file: str):
    """Compatibility stub — redirects to the new synchronous pipeline."""
    rows, nb_dup, nb_inv = parse_csv(Path(input_file))
    db_service = DatabaseService()
    session = db_service.get_session()
    try:
        importer = AssetImporter(session)
        stats = importer.run(rows)
        stats.duplicates_in_csv = nb_dup
        stats.invalid_rows = nb_inv
        stats.print_summary()
    except Exception as exc:
        session.rollback()
        logger.exception("Fatal error: %s", exc)
    finally:
        session.close()


def _stub_csv_reader(input_file: str):
    """Do not call — empty compatibility stub. Use AssetImporter.run()."""
    pass  # removed



def main():
    parser = argparse.ArgumentParser(description="CSV import of financial assets into Fonrex")
    parser.add_argument(
        "--file",
        help="Path to a CSV file (default: etf.csv/stocks.csv if not specified)",
    )
    parser.add_argument(
        "--dir",
        help="Directory containing multiple CSV files to import (e.g. data/isin_data/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate import without database writes",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE_IMPORT,
        help=f"Batch size (default: {BATCH_SIZE_IMPORT})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="yfinance deep enrichment without re-import",
    )
    parser.add_argument(
        "--isin",
        help="ISIN to enrich (--enrich-only mode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of assets to enrich",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s — %(message)s")

    # Standalone enrichment mode
    if args.enrich_only:
        asyncio.run(
            run_enrich_only(
                isin=args.isin,
                file_path=args.file,
                limit=args.limit,
            )
        )
        return

    # Smart resolution of files to process
    files_to_process: List[Path] = []

    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_absolute() and not dir_path.exists():
            dir_path = Path(SCRIPT_DIR) / args.dir
        if not dir_path.exists() or not dir_path.is_dir():
            print(f"❌ Directory not found: {dir_path}")
            sys.exit(1)
        files_to_process = sorted([p for p in dir_path.glob("*.csv") if p.is_file()])
        if not files_to_process:
            print(f"⚠️  No .csv files found in {dir_path}")
            sys.exit(0)
    elif args.file:
        file_path = Path(args.file)
        if not file_path.is_absolute() and not file_path.exists():
            file_path = Path(DEFAULT_ISIN_DATA_DIR) / args.file
        if not file_path.exists():
            file_path = Path(SCRIPT_DIR) / args.file
        if not file_path.exists():
            print(f"❌ File not found: {args.file}")
            sys.exit(1)
        files_to_process = [file_path]
    else:
        # Automatic fallback: data/etf.csv / data/stocks.csv or data/isin_data/
        fallback_files = []
        for name in ["etf.csv", "stocks.csv"]:
            p = Path(SCRIPT_DIR) / "data" / name
            if p.exists():
                fallback_files.append(p)
        isin_dir = Path(DEFAULT_ISIN_DATA_DIR)
        if fallback_files:
            print(f"ℹ️  No file specified — using default seed datasets: {[p.name for p in fallback_files]}")
            files_to_process = fallback_files
        elif isin_dir.exists() and isin_dir.is_dir():
            print(f"ℹ️  No file specified — importing local directory: {isin_dir}")
            files_to_process = sorted([p for p in isin_dir.glob("*.csv") if p.is_file()])
        else:
            parser.error("No file specified and no default dataset available.")

    all_rows: List[CSVRow] = []
    total_duplicates = 0
    total_invalid = 0

    for f_path in files_to_process:
        print(f"📄 Parsing: {f_path}")
        rows, nb_dup, nb_inv = parse_csv(f_path)
        all_rows.extend(rows)
        total_duplicates += nb_dup
        total_invalid += nb_inv

    total_read = len(all_rows) + total_duplicates + total_invalid
    print(f"{total_read} total rows read ({len(files_to_process)} file(s))")
    print(f"   -> {total_duplicates} CSV duplicates filtered")
    print(f"   -> {total_invalid} invalid rows ignored")
    print(f"   -> {len(all_rows)} valid rows to process")

    if not all_rows:
        print("⚠️  No valid rows to import")
        sys.exit(0)

    db_service = DatabaseService()
    session = db_service.get_session()

    try:
        importer = AssetImporter(session, dry_run=args.dry_run, batch_size=args.batch_size)
        stats = importer.run(all_rows)
        stats.total_csv_rows = total_read
        stats.duplicates_in_csv = total_duplicates
        stats.invalid_rows = total_invalid
        stats.print_summary()

        if args.dry_run:
            session.rollback()
            print("🔍 Dry-run completed — no database modifications")
        else:
            print("Import completed successfully")

    except Exception as exc:
        session.rollback()
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()

