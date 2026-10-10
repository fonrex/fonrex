import asyncio
import logging
import re
from typing import Any, Dict, Iterable, Optional, Tuple

from concurrency import run_sync

logger = logging.getLogger(__name__)

_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")


class FinancialProviderRunner:
    """Runs the financial providers with a timeout and normalised answers."""

    def __init__(
        self,
        providers_available: Dict[str, Dict[str, Any]],
        timeout_seconds: float = 12.0,
        validation_layer=None,
    ):
        self.providers_available = providers_available
        self.timeout_seconds = timeout_seconds
        self.validation = validation_layer

    def resolve_providers(self, provider_params: Iterable[str]) -> Tuple[list[str], Dict[str, Any]]:
        """Resolve a list of requested providers, keeping the name errors."""
        requested = [
            provider.strip() for provider in provider_params if provider and provider.strip()
        ]
        if not requested:
            return list(self.providers_available.keys()), {}

        provider_lookup = {name.lower(): name for name in self.providers_available}
        providers_to_use = []
        errors = {}

        for provider_name in requested:
            resolved = provider_lookup.get(provider_name.lower())
            if resolved:
                providers_to_use.append(resolved)
            else:
                errors[provider_name] = {"error": "Provider not found"}

        return providers_to_use, errors

    @staticmethod
    def build_search_term(
        provider_name: str,
        ticker: str,
        isin: Optional[str],
        asset_mappings: Dict[str, Any],
        provider_default_tickers: Optional[Dict[str, str]] = None,
        asset_profile: Optional[Dict[str, Any]] = None,
        verified_symbols: Optional[Dict[str, str]] = None,
    ):
        """The most reliable identifier for a provider."""
        provider_key = provider_name.lower()
        # A symbol verified for the listing (ISIN and currency checked) is used as
        # it is: nothing guessed from the ticker or the exchange may replace it.
        verified = (verified_symbols or {}).get(provider_key)
        if verified:
            return verified, f"Verified symbol: {verified}"
        provider_default_tickers = provider_default_tickers or {}
        search_term = provider_default_tickers.get(provider_key) or ticker
        used_source = None

        mapping = asset_mappings.get(provider_key)
        if mapping and getattr(mapping, "is_active", True):
            if mapping.provider_url:
                search_term = mapping.provider_url
                used_source = mapping.provider_url
            elif mapping.provider_ticker:
                search_term = mapping.provider_ticker
                used_source = f"Ticker: {search_term}"

        # Yahoo Finance: ticker corrected with the exchange of the profile
        if provider_key == "yahoofinance" and asset_profile:
            db_exchange = asset_profile.get("exchange")
            # official_symbol first: it is often "cleaner" (e.g. ACA vs XCA)
            db_ticker = (
                asset_profile.get("official_symbol") or asset_profile.get("ticker") or ticker
            )

            if db_exchange and db_ticker and len(db_ticker) < 10:
                from financials.exchange import get_yahoo_ticker

                # Build the ticker with its suffix
                generated = get_yahoo_ticker(db_ticker, db_exchange)

                # The built ticker wins when it has a suffix (differs from the base ticker)
                if generated != db_ticker:
                    search_term = generated
                    used_source = f"Auto-generated (via {db_exchange}): {search_term}"
                elif not used_source:
                    search_term = generated
                    used_source = f"Auto-generated: {search_term}"

        # Google Finance: ticker corrected with the exchange of the profile
        if provider_key == "googlefinance" and asset_profile:
            db_exchange = asset_profile.get("exchange")
            # Same: official_symbol first, to avoid tickers of other exchanges (e.g. XCA)
            db_ticker = (
                asset_profile.get("official_symbol") or asset_profile.get("ticker") or ticker
            )

            if db_exchange and db_ticker and len(db_ticker) < 10:
                from financials.exchange import get_google_ticker

                generated = get_google_ticker(db_ticker, db_exchange)

                # The built ticker wins when it holds a separator:
                if ":" in generated and generated.split(":")[0] != generated:
                    search_term = generated
                    used_source = f"Auto-generated (via {db_exchange}): {search_term}"
                elif not used_source:
                    search_term = generated
                    used_source = f"Auto-generated: {search_term}"

        # GuruFocus: ticker corrected with the exchange of the profile, to help the resolution
        if provider_key == "gurufocus" and asset_profile:
            db_exchange = asset_profile.get("exchange")
            db_ticker = (
                asset_profile.get("official_symbol") or asset_profile.get("ticker") or ticker
            )

            if db_exchange and db_ticker and len(db_ticker) < 10:
                from financials.exchange import DB_EXCHANGE_TO_YAHOO_SUFFIX

                suffix = DB_EXCHANGE_TO_YAHOO_SUFFIX.get(db_exchange.upper())
                if suffix is not None:
                    search_term = f"{db_ticker}{suffix}"
                    used_source = f"Auto-generated (via {db_exchange}): {search_term}"

        # The ISIN of the profile, when known and better supported by the provider than the ticker
        profile_isin = asset_profile.get("isin") if asset_profile else None
        target_isin = isin or profile_isin

        if (
            not used_source
            and target_isin
            and provider_key
            in {
                "zonebourse",
                "investing",
                "wallstreetjournal",
                "marketwatch",
                "fortuneo",
                "boursedirect",
                "boursorama",
                "gurufocus",
                "investirlesechos",
            }
        ):
            search_term = target_isin
            used_source = f"ISIN: {target_isin}" if isin else f"ISIN from profile: {target_isin}"

        return search_term, used_source or f"Default: {search_term}"

    @staticmethod
    def _has_payload(data: Any) -> bool:
        if data is None:
            return False
        if hasattr(data, "model_dump"):
            return bool(data.model_dump(exclude_none=True))
        if isinstance(data, dict):
            return bool(data)
        return True

    @staticmethod
    def _dump_payload(data: Any, expose_extra_fields: bool):
        if hasattr(data, "model_dump"):
            if expose_extra_fields:
                return data.model_dump(exclude_none=True)
            return data.model_dump(
                include=set(data.__class__.model_fields.keys()), exclude_none=True
            )
        return data

    async def _execute_provider(
        self, provider_name: str, search_term: str, ticker: str, expose_extra_fields: bool
    ):
        provider_info = self.providers_available[provider_name]

        async def run_provider():
            if provider_info["type"] == "async":
                provider_instance = provider_info["class"]()
                data = await provider_instance.get_financials(search_term)
                # A provider returning the search term (e.g. the ISIN) as ticker gets the original ticker back
                if hasattr(data, "ticker") and data.ticker == search_term and search_term != ticker:
                    data.ticker = ticker
                return data

            if provider_info["type"] == "legacy":
                module = provider_info["module"]
                provider_class = getattr(module, provider_name)
                provider_instance = provider_class()
                if hasattr(provider_instance, "getAllData"):
                    return await run_sync(provider_instance.getAllData, ticker)
                return {"error": "Method getAllData not found on legacy provider"}

            return {"error": f"Unsupported provider type: {provider_info['type']}"}

        data = await asyncio.wait_for(run_provider(), timeout=self.timeout_seconds)
        if not self._has_payload(data):
            return None, None

        provider_url = None
        if hasattr(data, "provider_url"):
            provider_url = data.provider_url
        elif isinstance(data, dict):
            provider_url = data.get("provider_url")

        payload = self._dump_payload(data, expose_extra_fields)
        return payload, provider_url

    async def run(
        self,
        ticker: str,
        isin: Optional[str],
        provider_params: Iterable[str],
        asset_mappings: Dict[str, Any],
        provider_default_tickers: Optional[Dict[str, str]] = None,
        asset_profile: Optional[Dict[str, Any]] = None,
        verified_symbols: Optional[Dict[str, str]] = None,
        refused_providers: Optional[Dict[str, str]] = None,
    ):
        """Run the requested providers in parallel; returns the results and the sources used.

        ``verified_symbols`` gives, per provider (lower case), the symbol to query
        instead of anything derived from the ticker. ``refused_providers`` gives the
        providers that must not be queried, with the reason: asking them with the
        ticker would return another instrument.
        """
        providers_to_use, results = self.resolve_providers(provider_params)
        raw_providers = {}
        requested_set = {p.strip().lower() for p in provider_params if p and p.strip()}
        refused_providers = refused_providers or {}
        profile_isin = asset_profile.get("isin") if asset_profile else None
        target_isin = isin or profile_isin

        async def execute(provider_name: str):
            refusal = refused_providers.get(provider_name.lower())
            if refusal:
                raw_providers[provider_name] = f"Not queried: {refusal}"
                return provider_name, {"error": refusal}
            search_term, raw_provider = self.build_search_term(
                provider_name,
                ticker,
                isin,
                asset_mappings,
                provider_default_tickers=provider_default_tickers,
                asset_profile=asset_profile,
                verified_symbols=verified_symbols,
            )
            raw_providers[provider_name] = raw_provider
            try:
                # YahooFinance extra fields are always needed by the formatter
                expose_extra_fields = (
                    provider_name.lower() in requested_set
                    or provider_name.lower() == "yahoofinance"
                )
                payload, provider_url = await self._execute_provider(
                    provider_name, search_term, ticker, expose_extra_fields=expose_extra_fields
                )
                if provider_url:
                    raw_providers[provider_name] = provider_url
                if isinstance(payload, dict):
                    found = str(payload.get("isin") or "").strip().upper()
                    if (
                        target_isin
                        and _ISIN.fullmatch(found)
                        and found != target_isin.strip().upper()
                        # Yahoo is asked with a verified symbol; the ISIN yfinance
                        # gives for a symbol comes from another search and is not reliable.
                        and provider_name.lower() != "yahoofinance"
                    ):
                        # Searched by ticker, the site answered with a homonym
                        # ("SPFF" is another fund in the US): its figures are not
                        # those of the instrument, and must not carry its ISIN.
                        logger.warning(
                            "%s answered with ISIN %s for %s (%s)", provider_name, found, ticker, target_isin
                        )
                        return provider_name, {
                            "error": f"Another instrument was found (ISIN {found}, expected {target_isin})"
                        }
                    if target_isin:
                        payload["isin"] = target_isin
                return provider_name, payload
            except TimeoutError:
                logger.warning(f"Provider {provider_name} timed out after {self.timeout_seconds}s")
                return provider_name, {"error": "Provider timeout"}
            except Exception as e:
                logger.error(f"Provider {provider_name} error: {e}")
                return provider_name, {"error": str(e)}

        provider_results = await asyncio.gather(*(execute(name) for name in providers_to_use))
        for provider_name, payload in provider_results:
            results[provider_name] = payload

        # Validation layer — filters outliers and values out of range
        if self.validation and results:
            try:
                results = await self.validation.validate_results(
                    ticker=ticker,
                    results=results,
                )
            except Exception as exc:
                logger.warning("ValidationLayer error (non-blocking): %s", exc)

        return results, raw_providers
