"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("duplicati")

_session_token: str | None = None


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
