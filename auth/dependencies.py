"""Authentication dependencies for Fonrex API.

Supports dual credential extraction:
  1. Authorization: Bearer frx_live_...   (standard Fonrex Relay format)
  2. X-API-KEY: frx_live_...              (OpenBB Workspace custom header)

Both formats resolve to the same underlying key validation logic —
single point of truth.
"""

import os
import re
import secrets
from typing import Optional

from fastapi import HTTPException, Request

# Regular expression for valid Fonrex key format (frx_live_... or frx_test_...)
# Requires a live or test prefix followed by at least 6 alphanumeric/dash/underscore characters.
API_KEY_PATTERN = re.compile(r"^frx_(?:live|test)_[a-zA-Z0-9_-]{6,}$")


def get_api_key_from_request(request: Request) -> Optional[str]:
    """Extract the API key from the request, checking two header formats.

    Priority order:
      1. ``Authorization: Bearer frx_live_...`` (standard Fonrex Relay format)
      2. ``X-API-KEY: frx_live_...`` (OpenBB Workspace custom header format)

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


def is_auth_enforced() -> bool:
    """Return True if authentication is explicitly required by configuration."""
    return bool(
        os.environ.get("FONREX_API_KEY")
        or os.environ.get("FONREX_RELAY_KEY")
        or os.environ.get("FONREX_API_KEYS")
        or os.environ.get("FONREX_AUTH_REQUIRED", "").lower() in ("true", "1", "yes")
    )


def validate_api_key(key: str) -> bool:
    """Validate an API key against configured environment keys or key format.

    - If ``FONREX_API_KEY``, ``FONREX_RELAY_KEY`` or ``FONREX_API_KEYS`` is configured,
      the key must match one of the configured keys (using constant-time comparison).
    - If no configured keys are present, the key must conform to the valid
      Fonrex key format (``frx_live_...`` or ``frx_test_...``).
    """
    configured_key = os.environ.get("FONREX_API_KEY") or os.environ.get("FONREX_RELAY_KEY")
    configured_keys_str = os.environ.get("FONREX_API_KEYS")

    allowed_keys: list[str] = []
    if configured_key:
        allowed_keys.extend([k.strip() for k in configured_key.split(",") if k.strip()])
    if configured_keys_str:
        allowed_keys.extend([k.strip() for k in configured_keys_str.split(",") if k.strip()])

    if allowed_keys:
        return any(secrets.compare_digest(key, k) for k in allowed_keys)

    # When no explicit key is configured in the environment,
    # validate that the key matches the structured Fonrex API key format.
    return bool(API_KEY_PATTERN.match(key))


def require_api_key(request: Request) -> str:
    """FastAPI dependency requiring a valid API key.

    Extracts key via ``get_api_key_from_request`` and validates it via
    ``validate_api_key``.
    Raises HTTP 401 Unauthorized if missing, or HTTP 403 Forbidden if invalid.
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

    return key
