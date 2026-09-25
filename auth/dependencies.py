"""Authentication dependencies for Fonrex API.

Supports dual credential extraction:
  1. Authorization: Bearer frx_live_...   (standard Fonrex Relay format)
  2. X-API-KEY: frx_live_...              (OpenBB Workspace custom header)

Both formats resolve to the same underlying key validation logic —
single point of truth.
"""

from typing import Optional

from fastapi import HTTPException, Request


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


def require_api_key(request: Request) -> str:
    """FastAPI dependency requiring a valid API key.

    Extracts key via ``get_api_key_from_request``.
    Raises HTTP 401 Unauthorized if neither Authorization: Bearer
    nor X-API-KEY header is provided.
    """
    key = get_api_key_from_request(request)
    if not key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-KEY: <key>'",
        )
    return key
