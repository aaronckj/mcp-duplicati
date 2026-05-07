"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("duplicati")

_session_token: str | None = None


async def _login() -> str:
    """Authenticate with Duplicati and cache the session token."""
    global _session_token
    password = os.environ.get("DUPLICATI_PASSWORD")
    if not password:
        raise ValueError("DUPLICATI_PASSWORD environment variable is required")
    host = os.environ.get("DUPLICATI_HOST", "http://localhost:8200")
    timeout = float(os.environ.get("DUPLICATI_TIMEOUT", "30"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{host}/api/v1/auth/login",
            json={"Password": password},
        )
        resp.raise_for_status()
        token = resp.cookies["session-auth"]
        _session_token = token
        return token


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Make an authenticated request; re-login once on 401."""
    global _session_token
    host = os.environ.get("DUPLICATI_HOST", "http://localhost:8200")
    timeout = float(os.environ.get("DUPLICATI_TIMEOUT", "30"))
    if _session_token is None:
        await _login()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method,
            f"{host}{path}",
            cookies={"session-auth": _session_token},
            **kwargs,
        )
        if resp.status_code == 401:
            await _login()
            resp = await client.request(
                method,
                f"{host}{path}",
                cookies={"session-auth": _session_token},
                **kwargs,
            )
        return resp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
