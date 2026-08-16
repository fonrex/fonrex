"""HTTP adapters for fundamental-data use cases."""

from typing import Optional

from fastapi import APIRouter, Depends, Request

from cache.adapters import ResilientAsyncJsonCache
from financials.enrichment.adapters import (
    YFinanceAssetProfileEnricher,
    YFinanceDeepFundamentalsEnricher,
    normalize_google_finance_ticker,
)
from financials.formatter import FinancialsFormatter
from financials.provider_runner import FinancialProviderRunner
from routers.dependencies import get_cache_service, get_database_service, get_redis_client
from routers.errors import raise_http_error
from use_cases.errors import UseCaseError
from use_cases.fundamentals import GetDeepFundamentals, GetFundamentals

router = APIRouter(tags=["fundamentals"])


def _state_dependency(name: str, default=None):
    def dependency(request: Request):
        return getattr(request.app.state, name, default)

    return dependency


get_provider_registry = _state_dependency("providers_available", {})
get_validation_layer = _state_dependency("validation_layer")
get_sec_edgar_provider = _state_dependency("sec_edgar_provider")
get_optional_database_service = _state_dependency("db_service")


@router.get("/fundamental")
async def get_all_information(
    request: Request,
    ticker: Optional[str] = None,
    isin: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: Optional[str] = None,
    provider: Optional[str] = None,
    fmt: str = "eodhd",
    nocache: bool = False,
    database=Depends(get_optional_database_service),
    redis_client=Depends(get_redis_client),
    providers=Depends(get_provider_registry),
    validation_layer=Depends(get_validation_layer),
    sec_edgar_provider=Depends(get_sec_edgar_provider),
):
    use_case = GetFundamentals(
        database=database,
        redis=ResilientAsyncJsonCache(redis_client) if redis_client else None,
        provider_runner=FinancialProviderRunner(providers, validation_layer=validation_layer),
        formatter=FinancialsFormatter,
        profile_enricher=(YFinanceAssetProfileEnricher(database) if database else None),
        ticker_normalizer=normalize_google_finance_ticker,
        sec_edgar_provider=sec_edgar_provider,
    )
    try:
        result = await use_case.execute(
            ticker=ticker,
            isin=isin,
            exchange=exchange,
            currency=currency,
            provider=provider,
            fmt=fmt,
            nocache=nocache,
        )
    except UseCaseError as error:
        raise_http_error(error)

    request.state.provider_used = result.provider_used
    request.state.cache_hit = result.cache_hit
    return result.data


@router.get("/fundamental/deep")
async def get_fundamental_deep(
    ticker: Optional[str] = None,
    isin: Optional[str] = None,
    refresh: bool = False,
    sections: str = "all",
    database=Depends(get_database_service),
    cache=Depends(get_cache_service),
):
    try:
        return await GetDeepFundamentals(
            database,
            cache,
            enricher=YFinanceDeepFundamentalsEnricher(database),
        ).execute(
            ticker=ticker,
            isin=isin,
            refresh=refresh,
            sections=sections,
        )
    except UseCaseError as error:
        raise_http_error(error)
