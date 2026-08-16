"""Translate application errors into HTTP responses."""

from fastapi import HTTPException

from use_cases.errors import (
    DependencyUnavailable,
    InvalidInput,
    ResourceNotFound,
    UpstreamFailure,
    UseCaseError,
)


def raise_http_error(error: UseCaseError):
    status_code = {
        InvalidInput: 400,
        ResourceNotFound: 404,
        DependencyUnavailable: 503,
        UpstreamFailure: 500,
    }.get(type(error), 500)
    raise HTTPException(status_code=status_code, detail=error.detail) from error
