"""HTTP adapters for specialized-provider use cases."""

from fastapi import APIRouter, Depends, Request

from routers.dependencies import get_cache_service
from routers.errors import raise_http_error
from use_cases.errors import UseCaseError
from use_cases.specialized import (
    GetEtfDetails,
    GetIndexConstituents,
    GetInsiderTransactions,
)

router = APIRouter(tags=["specialized providers"])


def _state_dependency(name: str):
    def dependency(request: Request):
        return getattr(request.app.state, name, None)

    return dependency


get_sec_edgar_provider = _state_dependency("sec_edgar_provider")
get_justetf_provider = _state_dependency("justetf_provider")
get_index_provider = _state_dependency("index_provider")
get_index_name_enum = _state_dependency("index_name_enum")
get_optional_database_service = _state_dependency("db_service")


@router.get("/insider-transactions/{ticker}")
async def get_insider_transactions(
    ticker: str,
    limit: int = 20,
    refresh: bool = False,
    provider=Depends(get_sec_edgar_provider),
    cache=Depends(get_cache_service),
):
    try:
        return await GetInsiderTransactions(provider, cache).execute(ticker, limit, refresh)
    except UseCaseError as error:
        raise_http_error(error)


@router.get("/etf/{isin}/details")
async def get_etf_details(
    isin: str,
    refresh: bool = False,
    provider=Depends(get_justetf_provider),
    database=Depends(get_optional_database_service),
    cache=Depends(get_cache_service),
):
    try:
        return await GetEtfDetails(provider, database, cache).execute(isin, refresh)
    except UseCaseError as error:
        raise_http_error(error)


@router.get("/index/{index_name}/constituents")
async def get_index_constituents(
    index_name: str,
    refresh: bool = False,
    provider=Depends(get_index_provider),
    index_name_enum=Depends(get_index_name_enum),
    cache=Depends(get_cache_service),
):
    try:
        return await GetIndexConstituents(provider, index_name_enum, cache).execute(
            index_name, refresh
        )
    except UseCaseError as error:
        raise_http_error(error)
