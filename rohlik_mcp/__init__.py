"""Rohlik MCP server.

A Model Context Protocol server that exposes the Rohlik.cz API (via the
``rohlik-api`` package) as MCP tools.
"""

from .server import mcp

__version__ = "0.3.0"
__all__ = ["mcp"]
