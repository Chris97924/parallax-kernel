"""Bearer-token auth for the Parallax HTTP API.

Two modes:

* **Single-token mode (default):** A shared secret ``PARALLAX_TOKEN``
  (env) gates every non-``/healthz`` route. If the env var is unset the
  API runs in *open mode* — intended only for localhost dev / hackathon
  demos. Production deployments must set the token; the app logs a loud
  warning when it boots without one. Routes derive ``user_id`` from the
  request (query param / body field) as today.

* **Multi-user mode:** Enabled by setting ``PARALLAX_MULTI_USER=1`` (or
  ``true``, case-insensitive). The bearer token is a per-user secret
  minted by ``parallax token create``: the server sha256-hashes the
  supplied token and looks it up in the ``api_tokens`` table (m0009).
  A matching, un-revoked row binds the request to its owning
  ``user_id`` — stored on ``request.state.user_id`` so downstream
  routes can scope reads/writes without trusting request-supplied
  identifiers.

The dependency returns the authenticated principal (``"open"``,
``"bearer"``, or ``"user:<uid>"``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from parallax.server.deps import get_conn

__all__ = [
    "require_auth",
    "auth_configured",
    "multi_user_mode",
    "current_user_id",
    "hash_token",
    "PARALLAX_TOKEN_ENV",
    "PARALLAX_MULTI_USER_ENV",
]

PARALLAX_TOKEN_ENV = "PARALLAX_TOKEN"
PARALLAX_MULTI_USER_ENV = "PARALLAX_MULTI_USER"

_log = logging.getLogger("parallax.server.auth")

# auto_error=False so we can distinguish "no Authorization header" (→ 401 we
# own) from a malformed header (→ FastAPI's own 403). The extra control also
# lets us short-circuit in open mode without the header ever being parsed.
_bearer = HTTPBearer(auto_error=False)

# Module-level Depends singletons — sidesteps ruff's B008 "function call in
# default argument" complaint for the auth dep wiring.
_BEARER_DEP = Depends(_bearer)
_CONN_DEP = Depends(get_conn)


def auth_configured() -> bool:
    """True when ``PARALLAX_TOKEN`` is set to a non-empty value."""
    return bool(os.environ.get(PARALLAX_TOKEN_ENV, "").strip())


def multi_user_mode() -> bool:
    """True when ``PARALLAX_MULTI_USER`` selects per-user token auth.

    Accepts ``"1"``, ``"true"`` (case-insensitive). Any other value —
    empty, ``"0"``, ``"false"`` — keeps the server in single-token mode.
    """
    raw = os.environ.get(PARALLAX_MULTI_USER_ENV, "").strip().lower()
    return raw in ("1", "true")


def _expected_token() -> str:
    return os.environ.get(PARALLAX_TOKEN_ENV, "").strip()


def hash_token(plaintext: str) -> str:
    """Return the sha256 hex digest of ``plaintext``.

    Used by both the CLI (``parallax token create``) and the auth
    dependency so the hash function stays in one place.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _resolve_multi_user_token(
    request: Request,
    supplied: str,
    conn: sqlite3.Connection,
) -> str:
    """Look up the bearer token's hash in ``api_tokens``.

    Returns the bound ``user_id`` on a match (and sets
    ``request.state.user_id``). Raises 401 on miss / revoked / missing
    table. The lookup hashes the supplied token with sha256 before the
    primary-key query, so a compare_digest on the row is redundant once
    the row is returned.
    """
    supplied_hash = hash_token(supplied)
    try:
        row = conn.execute(
            "SELECT token_hash, user_id, revoked_at FROM api_tokens "
            "WHERE token_hash = ?",
            (supplied_hash,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Table missing — multi-user mode was turned on without
        # running migrations. Treat like an auth miss rather than
        # leaking a 500 that hints at the schema state.
        _log.warning(
            "auth.multi_user.table_missing path=%s", request.url.path
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if row is None:
        _log.warning("auth.reject.unknown path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # sqlite3.Row supports both positional and name access; fall back to
    # index for plain tuple factories (tests sometimes swap).
    user_id = row[1] if not hasattr(row, "keys") else row["user_id"]
    revoked_at = row[2] if not hasattr(row, "keys") else row["revoked_at"]
    if revoked_at is not None:
        _log.warning("auth.reject.revoked path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user_id = str(user_id)
    return str(user_id)


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = _BEARER_DEP,
    conn: sqlite3.Connection = _CONN_DEP,
) -> str:
    """FastAPI dependency: validates the Authorization header.

    * Open mode (``PARALLAX_TOKEN`` unset AND multi-user off) → returns
      ``"open"`` without inspecting the request.
    * Single-token mode → ``Authorization: Bearer <PARALLAX_TOKEN>``
      with a constant-time compare.
    * Multi-user mode → bearer → sha256 → ``api_tokens`` lookup; sets
      ``request.state.user_id`` on success.
    """
    if multi_user_mode():
        if creds is None or (creds.scheme or "").lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = (creds.credentials or "").strip()
        if not supplied:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_id = _resolve_multi_user_token(request, supplied, conn)
        return f"user:{user_id}"

    if not auth_configured():
        return "open"
    if creds is None or (creds.scheme or "").lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = (creds.credentials or "").strip()
    if not hmac.compare_digest(supplied, _expected_token()):
        _log.warning("auth.reject path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "bearer"


def current_user_id(request: Request, fallback: str | None) -> str:
    """Return the authenticated ``user_id`` (multi-user) or ``fallback``.

    * When :func:`multi_user_mode` is on and ``require_auth`` stored a
      user_id on ``request.state``, that value wins — request-supplied
      identifiers are ignored (and a warning is logged if they disagree).
    * Otherwise returns ``fallback`` unchanged.
    * Raises 400 when neither is available.
    """
    authed = getattr(request.state, "user_id", None)
    if authed:
        if fallback and fallback != authed:
            _log.warning(
                "auth.user_id.override path=%s authed=%s requested=%s",
                request.url.path,
                authed,
                fallback,
            )
        return str(authed)
    if fallback:
        return fallback
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="user_id required",
    )
