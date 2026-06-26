"""Entry point for the Rohlik MCP server.

Runs the server using the Streamable HTTP transport, suitable for running as a
long-lived service (e.g. in a Docker container). Host, port and path are read
from the environment (see :mod:`rohlik_mcp.config`).
"""

from __future__ import annotations

import logging

from .config import Config
from .server import mcp


def main() -> None:
    """Run the MCP server over the Streamable HTTP transport."""
    logging.basicConfig(level=logging.INFO)

    # Validate credentials up front so misconfiguration fails fast on startup,
    # and read the HTTP bind settings.
    config = Config.from_env()

    mcp.run(
        transport="http",
        host=config.host,
        port=config.port,
        path=config.path,
    )


if __name__ == "__main__":
    main()
