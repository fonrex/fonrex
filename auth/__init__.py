"""Authentication package for Fonrex."""

from auth.dependencies import (
    API_KEY_PATTERN,
    anonymize_api_key,
    get_api_key_from_connection,
    get_api_key_from_request,
    get_configured_api_keys,
    get_full_access_api_keys,
    get_read_only_api_keys,
    is_auth_enforced,
    is_read_only_key,
    is_read_request,
    require_api_key,
    validate_api_key,
)

__all__ = [
    "API_KEY_PATTERN",
    "anonymize_api_key",
    "get_api_key_from_connection",
    "get_api_key_from_request",
    "get_configured_api_keys",
    "get_full_access_api_keys",
    "get_read_only_api_keys",
    "is_auth_enforced",
    "is_read_only_key",
    "is_read_request",
    "require_api_key",
    "validate_api_key",
]
