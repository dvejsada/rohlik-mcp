"""Configuration for the Rohlik MCP server.

Server-level settings come from the environment (:class:`ServerConfig`).
Shop credentials do **not** — they are supplied per request by the calling
client (see :mod:`rohlik_mcp.server`), so one server can serve many accounts,
on any Rohlík Group shop (see :func:`resolve_shop`).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from rohlik_api import SITES, Site

DEFAULT_SITE = "cz"
DEFAULT_TIMEOUT = 30.0
DEFAULT_HOST = "0.0.0.0"  # bind all interfaces for containerised use
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp/"
DEFAULT_CLIENT_TTL = 900.0  # 15 min, matching LibreChat's idle disconnect
DEFAULT_MAX_CLIENTS = 50

# A client in use is never reaped regardless of this value - the registry tracks
# that exactly - so this floor is only here to stop a pathologically small TTL
# from logging every account out between one tool call and the next.
MIN_CLIENT_TTL = 60.0

# Headers carrying the per-request credentials. ``get_http_headers`` lowercases
# header names, so these are compared in lowercase.
USERNAME_HEADER = "x-rohlik-username"
PASSWORD_HEADER = "x-rohlik-password"
# Optional header choosing the shop for one request, e.g. ``de`` for Knuspr.de.
SITE_HEADER = "x-rohlik-site"

_SITE_CODES = ", ".join(SITES)


class InvalidSiteError(ValueError):
    """Raised for a shop code that is not in :data:`rohlik_api.SITES`."""


def _lookup_site(code: str, source: str) -> Site:
    """Return the shop for ``code`` (case-insensitive), naming ``source`` on failure."""
    site = SITES.get(code.strip().lower())
    if site is None:
        raise InvalidSiteError(f"Unknown shop {code!r} in {source}; use one of: {_SITE_CODES}.")
    return site


@dataclass(frozen=True, slots=True)
class Credentials:
    """One shop account's credentials, as supplied by a client."""

    username: str
    password: str

    def cache_key(self, base_url: str) -> str:
        """Return an opaque key identifying this account for client caching.

        The key covers the password as well as the username: keying on the
        username alone would hand an already authenticated session to anyone
        who supplied that username with *any* password. It is hashed so the
        plaintext never ends up in dict keys, reprs or logs.
        """
        digest = hashlib.sha256()
        for part in (self.username, self.password, base_url):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        """Redact the password so an accidental log line cannot leak it."""
        return f"Credentials(username={self.username!r}, password=<redacted>)"


@dataclass(slots=True)
class ServerConfig:
    """Runtime configuration, sourced from environment variables."""

    site: str = DEFAULT_SITE
    # Pins every request to this URL (e.g. a proxy) instead of the shop's own.
    base_url_override: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    client_ttl: float = DEFAULT_CLIENT_TTL
    max_clients: int = DEFAULT_MAX_CLIENTS

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Build a :class:`ServerConfig` from environment variables.

        All are optional; defaults are the module-level ``DEFAULT_*`` values.

            ROHLIK_SITE         (default shop: cz, de, at, hu or ro; default cz)
            ROHLIK_BASE_URL     (pins every request to this URL; unset by default)
            ROHLIK_TIMEOUT      (seconds, default 30)
            ROHLIK_MCP_HOST     (default 0.0.0.0)
            ROHLIK_MCP_PORT     (default 8000)
            ROHLIK_MCP_PATH     (default /mcp/)
            ROHLIK_CLIENT_TTL   (seconds an idle account client is kept, default 900,
                                 floored at 60)
            ROHLIK_MAX_CLIENTS  (max cached account clients, default 50)

        Note there are no credential variables: credentials arrive per request.

        Raises:
            InvalidSiteError: If ``ROHLIK_SITE`` is not a known shop code.
        """
        site = _lookup_site(os.environ.get("ROHLIK_SITE", DEFAULT_SITE), "ROHLIK_SITE")
        timeout = float(os.environ.get("ROHLIK_TIMEOUT", str(DEFAULT_TIMEOUT)))
        ttl = float(os.environ.get("ROHLIK_CLIENT_TTL", str(DEFAULT_CLIENT_TTL)))
        return cls(
            site=site.code,
            base_url_override=os.environ.get("ROHLIK_BASE_URL") or None,
            timeout=timeout,
            host=os.environ.get("ROHLIK_MCP_HOST", DEFAULT_HOST),
            port=int(os.environ.get("ROHLIK_MCP_PORT", str(DEFAULT_PORT))),
            path=os.environ.get("ROHLIK_MCP_PATH", DEFAULT_PATH),
            client_ttl=max(ttl, MIN_CLIENT_TTL),
            max_clients=max(1, int(os.environ.get("ROHLIK_MAX_CLIENTS", str(DEFAULT_MAX_CLIENTS)))),
        )


def resolve_shop(config: ServerConfig, requested: str | None) -> Site:
    """Return the shop one request talks to.

    Args:
        config: The server configuration (default shop, optional URL pin).
        requested: The ``X-Rohlik-Site`` header value, or None/empty if absent.

    Returns:
        The requested shop, else the server default. With ``ROHLIK_BASE_URL``
        set, the default shop with its URL replaced by the pinned one.

    Raises:
        InvalidSiteError: If ``requested`` is not a known shop code, or names a
            shop other than the default while the server is pinned to one URL
            (the request could not be honoured).
    """
    default = SITES[config.site]
    if not requested or not requested.strip():
        site = default
    else:
        site = _lookup_site(requested, f"the {SITE_HEADER} header")
    if config.base_url_override is None:
        return site
    if site != default:
        raise InvalidSiteError(
            f"This server is pinned to {config.base_url_override} by ROHLIK_BASE_URL, "
            f"so it cannot serve {site.name}; remove the {SITE_HEADER} header."
        )
    return Site(
        code=site.code,
        name=site.name,
        base_url=config.base_url_override,
        currency=site.currency,
        timezone=site.timezone,
    )
