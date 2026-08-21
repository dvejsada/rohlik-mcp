"""Configuration for the Rohlik MCP server.

Server-level settings come from the environment (:class:`ServerConfig`).
Rohlik.cz credentials do **not** — they are supplied per request by the calling
client (see :mod:`rohlik_mcp.server`), so one server can serve many accounts.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://www.rohlik.cz"
DEFAULT_TIMEOUT = 30.0
DEFAULT_HOST = "0.0.0.0"  # bind all interfaces for containerised use
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp/"
DEFAULT_CLIENT_TTL = 900.0  # 15 min, matching LibreChat's idle disconnect
DEFAULT_MAX_CLIENTS = 50

# An idle client must not be reaped while a request is still using it: a client
# is only touched when a tool acquires it, so a call still in flight looks idle.
# Keeping the TTL comfortably above the request timeout - which bounds how long
# any single call can run - makes that race unreachable.
MIN_CLIENT_TTL = 60.0

# Headers carrying the per-request credentials. ``get_http_headers`` lowercases
# header names, so these are compared in lowercase.
USERNAME_HEADER = "x-rohlik-username"
PASSWORD_HEADER = "x-rohlik-password"


@dataclass(frozen=True, slots=True)
class Credentials:
    """One Rohlik.cz account's credentials, as supplied by a client."""

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

    base_url: str = DEFAULT_BASE_URL
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

            ROHLIK_BASE_URL     (default https://www.rohlik.cz)
            ROHLIK_TIMEOUT      (seconds, default 30)
            ROHLIK_MCP_HOST     (default 0.0.0.0)
            ROHLIK_MCP_PORT     (default 8000)
            ROHLIK_MCP_PATH     (default /mcp/)
            ROHLIK_CLIENT_TTL   (seconds an idle account client is kept, default 900;
                                 floored at 60s and at twice ROHLIK_TIMEOUT)
            ROHLIK_MAX_CLIENTS  (max cached account clients, default 50)

        Note there are no credential variables: credentials arrive per request.
        """
        timeout = float(os.environ.get("ROHLIK_TIMEOUT", str(DEFAULT_TIMEOUT)))
        ttl = float(os.environ.get("ROHLIK_CLIENT_TTL", str(DEFAULT_CLIENT_TTL)))
        return cls(
            base_url=os.environ.get("ROHLIK_BASE_URL", DEFAULT_BASE_URL),
            timeout=timeout,
            host=os.environ.get("ROHLIK_MCP_HOST", DEFAULT_HOST),
            port=int(os.environ.get("ROHLIK_MCP_PORT", str(DEFAULT_PORT))),
            path=os.environ.get("ROHLIK_MCP_PATH", DEFAULT_PATH),
            client_ttl=max(ttl, MIN_CLIENT_TTL, timeout * 2),
            max_clients=max(1, int(os.environ.get("ROHLIK_MAX_CLIENTS", str(DEFAULT_MAX_CLIENTS)))),
        )
