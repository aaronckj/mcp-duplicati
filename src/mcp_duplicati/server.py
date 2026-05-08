"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import json
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
    if kwargs.get("params"):
        body["query"] = {k: str(v) for k, v in kwargs["params"].items()}
    return body


def _err(e: Exception, tool: str) -> dict:
    out: dict = {"error": str(e), "tool": tool, "detail": type(e).__name__}
    if isinstance(e, httpx.HTTPStatusError):
        out["status"] = e.response.status_code
        try:
            out["body"] = e.response.json()
        except Exception:
            out["body"] = e.response.text[:500]
    return out


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
        caller_id = os.environ.get("VAULT_PROXY_CALLER_ID", "mcp-duplicati")
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(
                f"{proxy_url}/proxy",
                json=_build_proxy_body(method, path, **kwargs),
                headers={"X-Caller-Id": caller_id},
            )

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
        return _err(e, "server_info")


@mcp.tool()
async def list_backups() -> dict:
    """List all configured backup jobs with ID, name, last run, and next run."""
    try:
        resp = await _request("GET", "/api/v1/backups")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_backups")


@mcp.tool()
async def backup_status(backup_id: str) -> dict:
    """Get detailed status of a specific backup job including destination, settings, and schedule."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "backup_status"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "backup_status")


@mcp.tool()
async def create_backup(name: str, source_paths: str, destination_url: str, passphrase: str = "") -> dict:
    """Create a new Duplicati backup job. source_paths: comma-separated local paths to back up. destination_url: Duplicati backend URL (e.g., 'file:///mnt/backup', 's3://bucket/path'). passphrase: optional AES-256 encryption key."""
    if not name or not name.strip():
        return {"error": "name must not be empty", "tool": "create_backup"}
    if not source_paths or not source_paths.strip():
        return {"error": "source_paths must not be empty", "tool": "create_backup"}
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "create_backup"}
    sources = [p.strip() for p in source_paths.split(",") if p.strip()]
    settings: list[dict] = []
    if passphrase:
        settings.append({"Name": "passphrase", "Value": passphrase})
    config = {
        "Backup": {
            "Name": name,
            "Sources": sources,
            "Settings": settings,
            "Filters": [],
        },
        "Schedule": None,
        "Destinations": [destination_url],
    }
    try:
        resp = await _request("POST", "/api/v1/backups", json=config)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "create_backup")


@mcp.tool()
async def update_backup(
    backup_id: str,
    name: str = "",
    source_paths: str = "",
    destination_url: str = "",
    passphrase: str = "",
) -> dict:
    """Update an existing backup job. Only non-empty fields are changed. Fetches current config, applies changes, then PUTs the updated config."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "update_backup"}
    if not any([name, source_paths, destination_url, passphrase]):
        return {"error": "At least one field to update must be specified", "tool": "update_backup"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()

        backup = current.get("Backup", current)
        if name:
            backup["Name"] = name
        if source_paths:
            backup["Sources"] = [p.strip() for p in source_paths.split(",") if p.strip()]
        if destination_url:
            current["Destinations"] = [destination_url]
        if passphrase:
            settings = backup.get("Settings", [])
            settings = [s for s in settings if s.get("Name") != "passphrase"]
            settings.append({"Name": "passphrase", "Value": passphrase})
            backup["Settings"] = settings

        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "updated": True}}
    except Exception as e:
        return _err(e, "update_backup")


@mcp.tool()
async def delete_backup(backup_id: str) -> dict:
    """Delete a backup job configuration. Does NOT delete backup data on the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_backup"}
    try:
        resp = await _request("DELETE", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "deleted": True}}
    except Exception as e:
        return _err(e, "delete_backup")


@mcp.tool()
async def import_backup_config(config_json: str) -> dict:
    """Import a backup job from a JSON config string. Use export_backup_config to get the correct format. The imported job will be created as a new backup job."""
    if not config_json or not config_json.strip():
        return {"error": "config_json must not be empty", "tool": "import_backup_config"}
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "import_backup_config"}
    try:
        resp = await _request("POST", "/api/v1/backups/import", json=config)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "import_backup_config")


@mcp.tool()
async def export_backup_config(backup_id: str) -> dict:
    """Export a backup job's full configuration as JSON. Use this to save/restore job definitions or migrate to another Duplicati instance."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "export_backup_config"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/export")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "export_backup_config")




@mcp.tool()
async def get_backup_commandline(backup_id: str) -> dict:
    """Get the equivalent command-line invocation for a backup job. Useful for understanding settings, debugging, or running the backup outside Duplicati."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_commandline"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/commandline")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_backup_commandline")


@mcp.tool()
async def run_backup(backup_id: str) -> dict:
    """Trigger a backup job to run immediately."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "run_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/run")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "triggered": True}}
    except Exception as e:
        return _err(e, "run_backup")


@mcp.tool()
async def abort_backup(backup_id: str) -> dict:
    """Abort a currently running backup job."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "abort_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/abort")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "aborted": True}}
    except Exception as e:
        return _err(e, "abort_backup")


@mcp.tool()
async def repair_backup(backup_id: str) -> dict:
    """Repair the local database for a backup job. Rebuilds index from destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "repair_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/repair")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "repair_started": True}}
    except Exception as e:
        return _err(e, "repair_backup")


@mcp.tool()
async def compact_backup(backup_id: str) -> dict:
    """Compact the backup destination: removes unused data blocks to reclaim storage space."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "compact_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/compact")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "compact_started": True}}
    except Exception as e:
        return _err(e, "compact_backup")


@mcp.tool()
async def verify_backup(backup_id: str) -> dict:
    """Verify backup integrity by comparing local database with actual data at the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "verify_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/verify")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "verify_started": True}}
    except Exception as e:
        return _err(e, "verify_backup")


@mcp.tool()
async def progress() -> dict:
    """Get current progress of any active backup or restore operation."""
    try:
        resp = await _request("GET", "/api/v1/progressstate")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "progress")


@mcp.tool()
async def list_versions(backup_id: str) -> dict:
    """List available restore points (filesets) for a backup job."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "list_versions"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/filesets")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_versions")


@mcp.tool()
async def search_backup_files(backup_id: str, path_filter: str = "*", restore_time: str = "latest") -> dict:
    """Search files within a backup version. path_filter: glob pattern (e.g., '*.pdf'). restore_time: 'latest' or ISO timestamp."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "search_backup_files"}
    try:
        resp = await _request(
            "GET",
            f"/api/v1/backup/{backup_id}/files",
            params={"filter": path_filter, "time": restore_time},
        )
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "search_backup_files")


@mcp.tool()
async def restore_files(backup_id: str, restore_path: str, source_path: str = "", restore_time: str = "latest") -> dict:
    """Restore files from a backup to a local directory. restore_path: destination directory on this machine. source_path: optional path filter within backup (empty = all files). restore_time: 'latest' or ISO timestamp."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "restore_files"}
    if not restore_path or not restore_path.strip():
        return {"error": "restore_path must not be empty", "tool": "restore_files"}
    try:
        payload: dict = {
            "restore-path": restore_path,
            "time": restore_time,
        }
        if source_path and source_path.strip():
            payload["paths"] = [source_path]
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/restore", json=payload)
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "restore_path": restore_path, "restore_started": True}}
    except Exception as e:
        return _err(e, "restore_files")


@mcp.tool()
async def pause(duration: int | None = None) -> dict:
    """Pause the Duplicati scheduler. duration: optional seconds (converted to HH:MM:SS for Duplicati API)."""
    if duration is not None and duration <= 0:
        return {"error": "duration must be a positive number of seconds", "tool": "pause"}
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
        return _err(e, "pause")


@mcp.tool()
async def resume() -> dict:
    """Resume the Duplicati scheduler after a pause."""
    try:
        resp = await _request("POST", "/api/v1/serverstate/resume")
        resp.raise_for_status()
        return {"result": {"resumed": True}}
    except Exception as e:
        return _err(e, "resume")


@mcp.tool()
async def get_logs(backup_id: str | None = None, page_size: int = 20, page: int = 0) -> dict:
    """Retrieve recent log entries. backup_id: optional, filters to a specific job. page_size: 1-500. page: 0-indexed page number."""
    page_size = min(max(1, page_size), 500)
    page = max(0, page)
    try:
        if backup_id is not None:
            path = f"/api/v1/backup/{backup_id}/log"
        else:
            path = "/api/v1/logdata/log"
        resp = await _request("GET", path, params={"pagesize": page_size, "page": page})
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_logs")


@mcp.tool()
async def get_server_settings() -> dict:
    """Get Duplicati server-level settings (schedule, concurrency, update channel, etc.)."""
    try:
        resp = await _request("GET", "/api/v1/serversettings")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_server_settings")




@mcp.tool()
async def update_server_settings(settings: str) -> dict:
    """Update Duplicati server-level settings. settings: JSON object with key-value pairs (e.g., \'{"startup-delay": "0", "max-upload-speed": "0"}\'). Get current keys via get_server_settings."""
    if not settings or not settings.strip():
        return {"error": "settings must not be empty", "tool": "update_server_settings"}
    try:
        parsed = json.loads(settings)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "update_server_settings"}
    if not isinstance(parsed, dict):
        return {"error": "settings must be a JSON object (key-value pairs)", "tool": "update_server_settings"}
    try:
        resp = await _request("PUT", "/api/v1/serversettings", json=parsed)
        resp.raise_for_status()
        return {"result": {"updated": True, "keys": list(parsed.keys())}}
    except Exception as e:
        return _err(e, "update_server_settings")


@mcp.tool()
async def list_notifications() -> dict:
    """List all pending Duplicati notifications and alerts."""
    try:
        resp = await _request("GET", "/api/v1/notifications")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_notifications")


@mcp.tool()
async def dismiss_notification(notification_id: str) -> dict:
    """Dismiss a Duplicati notification by ID."""
    if not notification_id or not notification_id.strip():
        return {"error": "notification_id must not be empty", "tool": "dismiss_notification"}
    try:
        resp = await _request("DELETE", f"/api/v1/notification/{notification_id}")
        resp.raise_for_status()
        return {"result": {"notification_id": notification_id, "dismissed": True}}
    except Exception as e:
        return _err(e, "dismiss_notification")




@mcp.tool()
async def dismiss_all_notifications() -> dict:
    """Dismiss all pending Duplicati notifications and alerts at once."""
    try:
        resp = await _request("DELETE", "/api/v1/notifications")
        resp.raise_for_status()
        return {"result": {"all_dismissed": True}}
    except Exception as e:
        return _err(e, "dismiss_all_notifications")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
