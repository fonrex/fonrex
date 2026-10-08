"""Authentication dependencies for Fonrex API.

The keys are the ones of this self-hosted instance: its owner chooses them and
sets them in the environment. Two ways to send a key:
  1. Authorization: Bearer <key>
  2. X-API-KEY: <key>              (OpenBB Workspace custom header)

Both formats resolve to the same underlying key validation logic —
single point of truth.

Authentication is secure by default: unless ``FONREX_AUTH_REQUIRED`` is
explicitly set to a false value, every protected route requires a key listed in
``FONREX_API_KEY``, ``FONREX_RELAY_KEY`` or ``FONREX_API_KEYS`` (full access),
or in ``FONREX_READ_ONLY_API_KEYS`` (read access only).

A read-only key is meant for clients that hold the key outside the machine
running Fonrex (a spreadsheet, a dashboard reached through a tunnel): it can
query data but cannot clear the cache, clean the database, trigger ingestion or
change subscriptions.
"""

import hashlib
import os
import re
import secrets
from typing import Optional

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection

# Regular expression for valid Fonrex key format (frx_live_... or frx_test_...)
# Requires a live or test prefix followed by at least 6 alphanumeric/dash/underscore characters.
API_KEY_PATTERN = re.compile(r"^frx_(?:live|test)_[a-zA-Z0-9_-]{6,}$")

# Environment variables holding full-access keys (each may be a comma-separated list).
API_KEY_ENV_VARS = ("FONREX_API_KEY", "FONREX_RELAY_KEY", "FONREX_API_KEYS")

# Environment variable holding read-only keys (comma-separated list).
READ_ONLY_API_KEY_ENV_VAR = "FONREX_READ_ONLY_API_KEYS"

# What a read-only key may call: reads, plus the POST routes that only compute a
# result from their request body and change nothing.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_READ_ONLY_POST_PATHS = (
    re.compile(r"^/technical/batch/?$"),
    re.compile(r"^/dcf/[^/]+/?$"),
)

# Values of FONREX_AUTH_REQUIRED that explicitly opt out of authentication.
_AUTH_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})


def anonymize_api_key(key: Optional[str]) -> Optional[str]:
    """Return a non-reversible cryptographic fingerprint for an API key.

    Hashes the secret using SHA-256 and preserves the prefix (e.g., 'frx_live_'
    or 'frx_test_') for telemetry / billing attribution while ensuring raw
    credentials are never persisted to database tables or logs.
    """
    if not key:
        return None
    prefix = ""
    if key.startswith("frx_live_"):
        prefix = "frx_live_"
    elif key.startswith("frx_test_"):
        prefix = "frx_test_"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}sha256_{digest}" if prefix else f"sha256_{digest}"


def get_api_key_from_connection(connection: HTTPConnection) -> Optional[str]:
    """Extract the API key from an HTTP request or WebSocket connection.

    Priority order:
      1. ``Authorization: Bearer <key>`` header
      2. ``X-API-KEY: <key>`` header
      3. Query parameter: ``api_key``, ``token``, or ``key`` (primarily for WebSockets)

    Returns the raw key string, or ``None`` if neither header nor query param is present.
    """
    # 1. Authorization: Bearer …
    auth_header = connection.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    # 2. X-API-KEY: …
    x_api_key = connection.headers.get("X-API-KEY")
    if x_api_key:
        return x_api_key.strip()

    # 3. Query parameters (e.g. for WebSocket clients that cannot set headers)
    for qparam in ("api_key", "token", "key"):
        val = connection.query_params.get(qparam)
        if val:
            return val.strip()

    return None


def get_api_key_from_request(request: Request) -> Optional[str]:
    """Extract the API key from the request, checking two header formats.

    Priority order:
      1. ``Authorization: Bearer <key>``
      2. ``X-API-KEY: <key>`` (OpenBB Workspace custom header format)

    Returns the raw key string, or ``None`` if neither header is present.
    Both formats resolve to the same downstream validation logic — this is
    the **single point of truth** for key extraction.
    """
    # 1. Authorization: Bearer …
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    # 2. X-API-KEY: …
    x_api_key = request.headers.get("X-API-KEY")
    if x_api_key:
        return x_api_key.strip()

    return None


def _keys_from_env(env_var: str) -> list[str]:
    val = os.environ.get(env_var)
    if not val:
        return []
    return [k.strip() for k in val.split(",") if k.strip()]


def get_full_access_api_keys() -> list[str]:
    """Return the keys allowed to call every route, administration included."""
    keys: list[str] = []
    for env_var in API_KEY_ENV_VARS:
        keys.extend(_keys_from_env(env_var))
    return keys


def get_read_only_api_keys() -> list[str]:
    """Return the keys restricted to read access (``FONREX_READ_ONLY_API_KEYS``)."""
    return _keys_from_env(READ_ONLY_API_KEY_ENV_VAR)


def get_configured_api_keys() -> list[str]:
    """Return the API keys accepted by this instance, as configured in the environment."""
    return get_full_access_api_keys() + get_read_only_api_keys()


def is_read_only_key(key: str) -> bool:
    """Tell whether ``key`` only grants read access.

    A key listed both as full-access and read-only keeps full access: the
    restriction must come from the configuration, never from a duplicate entry.
    """
    if any(secrets.compare_digest(key, k) for k in get_full_access_api_keys()):
        return False
    return any(secrets.compare_digest(key, k) for k in get_read_only_api_keys())


def is_read_request(method: str, path: str) -> bool:
    """Tell whether a request only reads data, and is open to a read-only key."""
    method = method.upper()
    if method in _READ_METHODS:
        return True
    return method == "POST" and any(pattern.match(path) for pattern in _READ_ONLY_POST_PATHS)


def is_auth_explicitly_disabled() -> bool:
    """Return True only when ``FONREX_AUTH_REQUIRED`` is set to a false value."""
    return os.environ.get("FONREX_AUTH_REQUIRED", "").strip().lower() in _AUTH_DISABLED_VALUES


def is_auth_enforced() -> bool:
    """Return True unless authentication has been explicitly disabled.

    Authentication is enforced by default. The only way to run an open instance
    is to set ``FONREX_AUTH_REQUIRED=false`` *and* configure no API key; a
    configured key always enforces authentication.
    """
    if get_configured_api_keys():
        return True
    return not is_auth_explicitly_disabled()


def validate_api_key(key: str) -> bool:
    """Validate an API key against the keys configured in the environment.

    - If a key is configured (full-access or read-only), the key must match one
      of the configured keys (using constant-time comparison). Whether a
      read-only key may perform a given request is decided by ``require_api_key``.
    - If no key is configured, validation fails closed: a key matching the Fonrex
      format is not a credential.
    - Only when authentication is explicitly disabled (``FONREX_AUTH_REQUIRED=false``)
      does a key merely need to conform to the Fonrex key format
      (``frx_live_...`` or ``frx_test_...``).
    """
    allowed_keys = get_configured_api_keys()
    if allowed_keys:
        return any(secrets.compare_digest(key, k) for k in allowed_keys)

    if not is_auth_explicitly_disabled():
        return False

    return bool(API_KEY_PATTERN.match(key))


def require_api_key(request: Request) -> str:
    """FastAPI dependency requiring a valid API key.

    Extracts key via ``get_api_key_from_request`` and validates it via
    ``validate_api_key``.
    Raises HTTP 401 Unauthorized if missing, or HTTP 403 Forbidden if invalid or
    if a read-only key is used on a route that changes something.
    """
    key = get_api_key_from_request(request)
    if not key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-KEY: <key>'",
        )

    if not validate_api_key(key):
        raise HTTPException(
            status_code=403,
            detail="Invalid API key format or credentials",
        )

    if is_read_only_key(key) and not is_read_request(request.method, request.url.path):
        raise HTTPException(
            status_code=403,
            detail="This API key is read-only and cannot perform this operation",
        )

    request.state.api_key_id = anonymize_api_key(key)
    return key
