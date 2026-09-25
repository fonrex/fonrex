from fastapi import HTTPException, Request

from auth.dependencies import get_api_key_from_request, require_api_key

__all__ = [
    "get_api_key_from_request",
    "require_api_key",
    "get_database_service",
    "get_query_service",
    "get_ingestion_service",
    "get_technical_service",
    "get_cache_service",
    "get_redis_client",
]


def _required_service(request: Request, name: str, label: str):
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(status_code=503, detail=f"{label} indisponible")
    return service


def get_database_service(request: Request):
    if getattr(request.app.state, "db_available", True) is False:
        raise HTTPException(
            status_code=503, detail="Schéma de base de données indisponible ou obsolète"
        )
    return _required_service(request, "db_service", "Base de données")


def get_query_service(request: Request):
    return _required_service(request, "query_service", "QueryService")


def get_ingestion_service(request: Request):
    return _required_service(request, "ingestion_service", "IngestionService")


def get_technical_service(request: Request):
    return _required_service(request, "technical_service", "TechnicalIndicatorService")


def get_cache_service(request: Request):
    return getattr(request.app.state, "cache_service", None)


def get_redis_client(request: Request):
    return getattr(request.app.state, "redis_client", None)
