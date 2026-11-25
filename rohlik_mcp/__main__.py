"""Entry point for the Rohlik MCP server.

Runs the server using the Streamable HTTP transport, suitable for running as a
long-lived service (e.g. in a Docker container). Bind settings are read from the
environment (see :mod:`rohlik_mcp.config`); shop credentials are not — each
client supplies its own with every request.
"""

from __future__ import annotations

import logging

from .server import get_config, mcp


def main() -> None:
    """Run the MCP server over the Streamable HTTP transport."""
    logging.basicConfig(level=logging.INFO)

    config = get_config()

    mcp.run(
        transport="http",
        host=config.host,
        port=config.port,
        path=config.path,
    )


if __name__ == "__main__":
    main()
