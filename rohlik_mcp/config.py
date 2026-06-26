"""Configuration for the Rohlik MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://www.rohlik.cz"
DEFAULT_TIMEOUT = 30.0
DEFAULT_HOST = "0.0.0.0"  # bind all interfaces for containerised use
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp/"


@dataclass(slots=True)
class Config:
    """Runtime configuration, sourced from environment variables."""

    username: str
    password: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH

    @classmethod
    def from_env(cls) -> Config:
        """Build a :class:`Config` from environment variables.

        Required:
            ROHLIK_USERNAME, ROHLIK_PASSWORD

        Optional:
            ROHLIK_BASE_URL   (default https://www.rohlik.cz)
            ROHLIK_TIMEOUT    (seconds, default 30)
            ROHLIK_MCP_HOST   (default 0.0.0.0)
            ROHLIK_MCP_PORT   (default 8000)
            ROHLIK_MCP_PATH   (default /mcp/)

        Raises:
            RuntimeError: If the required credentials are missing.
        """
        username = os.environ.get("ROHLIK_USERNAME")
        password = os.environ.get("ROHLIK_PASSWORD")
        if not username or not password:
            raise RuntimeError(
                "ROHLIK_USERNAME and ROHLIK_PASSWORD environment variables are required"
            )

        return cls(
            username=username,
            password=password,
            base_url=os.environ.get("ROHLIK_BASE_URL", DEFAULT_BASE_URL),
            timeout=float(os.environ.get("ROHLIK_TIMEOUT", str(DEFAULT_TIMEOUT))),
            host=os.environ.get("ROHLIK_MCP_HOST", DEFAULT_HOST),
            port=int(os.environ.get("ROHLIK_MCP_PORT", str(DEFAULT_PORT))),
            path=os.environ.get("ROHLIK_MCP_PATH", DEFAULT_PATH),
        )
