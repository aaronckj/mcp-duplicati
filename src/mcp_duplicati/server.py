"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("duplicati")

_DEFAULT_HOST = "http://localhost:8200"
_DEFAULT_TIMEOUT = 30.0

_session_token: str | None = None


def _build_proxy_body(method: str, path: str, **kwargs: Any) -> dict:
    body: dict = {
        "service": os.environ.get("VAULT_PROXY_SERVICE", "duplicati"),
        "method": method,
        "path": path,
    }
    if "json" in kwargs:
        body["body"] = kwargs["json"]
    if "params" in kwargs:
        body["query"] = {k: str(v) for k, v in kwargs["params"].items()}
    return body


async def _login() -> str:
    """Authenticate with Duplicati and cache the session token."""
    global _session_token
    password = os.environ.get("DUPLICATI_PASSWORD")
    if not password:
        raise ValueError("DUPLICATI_PASSWORD environment variable is required")
    host = os.environ.get("DUPLICATI_HOST", _DEFAULT_HOST)
    timeout = float(os.environ.get("DUPLICATI_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{host}/api/v1/auth/login",
            json={"Password": password},
        )
        resp.raise_for_status()
        token = resp.cookies.get("session-auth")
        if not token:
            raise ValueError("Duplicati login response missing 'session-auth' cookie; check your password and Duplicati version")
        _session_token = token
        return token


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Route through vaultproxy if VAULT_PROXY_URL is set, else use direct session auth."""
    timeout = float(os.environ.get("DUPLICATI_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    proxy_url = os.environ.get("VAULT_PROXY_URL")

    if proxy_url:
        # vaultproxy handles session login + token refresh internally
        caller_id = os.environ.get("VAULT_PROXY_CALLER_ID", "mcp-duplicati")
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{proxy_url}/proxy",
                json=_build_proxy_body(method, path, **kwargs),
                headers={"X-Caller-Id": caller_id},
            )

    # Direct mode: manage session token ourselves
    global _session_token
    host = os.environ.get("DUPLICATI_HOST", _DEFAULT_HOST)
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


@mcp.tool()
async def server_info() -> dict:
    """Get Duplicati server version and current state."""
    try:
        resp = await _request("GET", "/api/v1/serverstate")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "server_info", "detail": type(e).__name__}


@mcp.tool()
async def list_backups() -> dict:
    """List all configured backup jobs with ID, name, last run, and next run."""
    try:
        resp = await _request("GET", "/api/v1/backups")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "list_backups", "detail": type(e).__name__}


@mcp.tool()
async def backup_status(backup_id: str) -> dict:
    """Get detailed status of a specific backup job."""
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "backup_status", "detail": type(e).__name__}


@mcp.tool()
async def run_backup(backup_id: str) -> dict:
    """Trigger a backup job to run immediately."""
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/run")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "triggered": True}}
    except Exception as e:
        return {"error": str(e), "tool": "run_backup", "detail": type(e).__name__}


@mcp.tool()
async def progress() -> dict:
    """Get current progress of any active backup or restore operation."""
    try:
        resp = await _request("GET", "/api/v1/progressstate")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "progress", "detail": type(e).__name__}


@mcp.tool()
async def list_versions(backup_id: str) -> dict:
    """List available restore points (filesets) for a backup job."""
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/filesets")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "list_versions", "detail": type(e).__name__}


@mcp.tool()
async def pause(duration: int | None = None) -> dict:
    """Pause the Duplicati scheduler. duration: optional seconds (converted to HH:MM:SS for Duplicati API)."""
    try:
        params: dict = {}
        if duration is not None:
            h, rem = divmod(int(duration), 3600)
            m, s = divmod(rem, 60)
            params["duration"] = f"{h:02d}:{m:02d}:{s:02d}"
        resp = await _request("POST", "/api/v1/serverstate/pause", params=params)
        resp.raise_for_status()
        return {"result": {"paused": True, "duration": duration}}
    except Exception as e:
        return {"error": str(e), "tool": "pause", "detail": type(e).__name__}


@mcp.tool()
async def resume() -> dict:
    """Resume the Duplicati scheduler after a pause."""
    try:
        resp = await _request("POST", "/api/v1/serverstate/resume")
        resp.raise_for_status()
        return {"result": {"resumed": True}}
    except Exception as e:
        return {"error": str(e), "tool": "resume", "detail": type(e).__name__}


@mcp.tool()
async def get_logs(backup_id: str | None = None, page_size: int = 20) -> dict:
    """Retrieve recent log entries. backup_id: optional, filters to a specific job. page_size: 1–500."""
    page_size = min(max(1, page_size), 500)
    try:
        if backup_id is not None:
            path = f"/api/v1/backup/{backup_id}/log"
        else:
            path = "/api/v1/logdata/log"
        resp = await _request("GET", path, params={"pagesize": page_size})
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "get_logs", "detail": type(e).__name__}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
