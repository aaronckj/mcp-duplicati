"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
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
    """Get Duplicati server state including version, OS type, program state, and server time. For normalized key access use fields: ServerVersion, OSType, ProgramState, ServerTime."""
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
    """Get full status of a backup job including Backup config, Schedule, and BackupStatistics. Returns the raw API response structure."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "backup_status"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "backup_status")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def get_backup(backup_id: str) -> dict:
    """Get full configuration of a backup job including source paths, destination, schedule, and settings. Different from backup_status which only returns the last run statistics."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def create_backup(name: str, source_paths: str, destination_url: str, passphrase: str = "", exclude_filters: str = "", repeat: str = "", allowed_days: str = "") -> dict:
    """Create a new Duplicati backup job. source_paths: comma-separated local paths to back up. destination_url: Duplicati backend URL (e.g., 'file:///mnt/backup', 's3://bucket/path'). passphrase: optional AES-256 encryption key. exclude_filters: comma-separated glob patterns to exclude (e.g., '*.tmp,*.log,/proc/*'). repeat: optional schedule interval ('1D'=daily, '1W'=weekly, '12H'=every 12 hours, '30M'=every 30 minutes). allowed_days: comma-separated days to restrict schedule to (e.g., 'mon,wed,fri'); requires repeat."""
    if not name or not name.strip():
        return {"error": "name must not be empty", "tool": "create_backup"}
    name = name.strip()
    if not source_paths or not source_paths.strip():
        return {"error": "source_paths must not be empty", "tool": "create_backup"}
    source_paths = source_paths.strip()
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "create_backup"}
    destination_url = destination_url.strip()
    sources = [p.strip() for p in source_paths.split(",") if p.strip()]
    settings: list[dict] = []
    if passphrase:
        settings.append({"Name": "passphrase", "Value": passphrase})
    filters: list[dict] = []
    if exclude_filters:
        for pattern in [p.strip() for p in exclude_filters.split(",") if p.strip()]:
            filters.append({"Order": len(filters), "Include": False, "Expression": pattern})
    schedule = None
    if allowed_days and allowed_days.strip() and not (repeat and repeat.strip()):
        return {"error": "allowed_days requires repeat to also be specified", "tool": "create_backup"}
    if repeat and repeat.strip():
        r = repeat.strip().upper()
        if not re.match(r'^\d+[SMHDW]$', r):
            return {"error": f"Invalid repeat format '{r}'. Expected number + unit: S (seconds), M (minutes), H (hours), D (days), W (weeks). E.g. '1D', '12H', '30M'.", "tool": "create_backup"}
        schedule = {
            "Repeat": r,
            "Time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if allowed_days and allowed_days.strip():
            _valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
            day_list = [d.strip().lower() for d in allowed_days.split(",") if d.strip()]
            invalid = [d for d in day_list if d not in _valid_days]
            if invalid:
                return {"error": f"Invalid allowed_days values: {invalid}. Use: mon,tue,wed,thu,fri,sat,sun", "tool": "create_backup"}
            schedule["AllowedDays"] = day_list
    config = {
        "Backup": {
            "Name": name,
            "TargetURL": destination_url,
            "Sources": sources,
            "Settings": settings,
            "Filters": filters,
        },
        "Schedule": schedule,
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
    exclude_filters: str = "",
    repeat: str = "",
    allowed_days: str = "",
) -> dict:
    """Update an existing backup job. Only non-empty fields are changed. Fetches current config, applies changes, then PUTs the updated config. exclude_filters: comma-separated glob patterns to exclude (replaces existing filters; omit to keep current filters). repeat: update schedule interval ('1D', '1W', '12H', etc.). allowed_days: comma-separated days to restrict schedule to ('mon,tue,...,sun')."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "update_backup"}
    backup_id = backup_id.strip()
    if not any([name, source_paths, destination_url, passphrase, exclude_filters, repeat, allowed_days]):
        return {"error": "At least one field to update must be specified", "tool": "update_backup"}
    if repeat and repeat.strip():
        r = repeat.strip().upper()
        if not re.match(r'^\d+[SMHDW]$', r):
            return {"error": f"Invalid repeat format '{r}'. Expected number + unit: S, M, H, D, W. E.g. '1D', '12H'.", "tool": "update_backup"}
    if allowed_days and allowed_days.strip():
        _valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        day_list = [d.strip().lower() for d in allowed_days.split(",") if d.strip()]
        invalid = [d for d in day_list if d not in _valid_days]
        if invalid:
            return {"error": f"Invalid allowed_days values: {invalid}. Use: mon,tue,wed,thu,fri,sat,sun", "tool": "update_backup"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()

        backup = current.get("Backup", current)
        if name:
            backup["Name"] = name.strip()
        if source_paths:
            backup["Sources"] = [p.strip() for p in source_paths.split(",") if p.strip()]
        if destination_url:
            backup["TargetURL"] = destination_url.strip()
        if passphrase:
            settings = backup.get("Settings", [])
            settings = [s for s in settings if s.get("Name") != "passphrase"]
            settings.append({"Name": "passphrase", "Value": passphrase.strip()})
            backup["Settings"] = settings
        if exclude_filters:
            patterns = [p.strip() for p in exclude_filters.split(",") if p.strip()]
            backup["Filters"] = [{"Order": i, "Include": False, "Expression": p} for i, p in enumerate(patterns)]
        if repeat and repeat.strip():
            schedule = current.get("Schedule") or {}
            schedule["Repeat"] = repeat.strip().upper()
            if not schedule.get("Time"):
                schedule["Time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            current["Schedule"] = schedule
        if allowed_days and allowed_days.strip():
            schedule = current.get("Schedule") or {}
            if not schedule.get("Repeat") and not (repeat and repeat.strip()):
                return {"error": "allowed_days requires a repeat interval — provide repeat or set a schedule first via set_backup_schedule", "tool": "update_backup"}
            day_list = [d.strip().lower() for d in allowed_days.split(",") if d.strip()]
            schedule["AllowedDays"] = day_list
            current["Schedule"] = schedule

        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "updated": True}}
    except Exception as e:
        err = _err(e, "update_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def delete_backup(backup_id: str) -> dict:
    """Delete a backup job configuration. Does NOT delete backup data on the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("DELETE", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "deleted": True}}
    except Exception as e:
        err = _err(e, "delete_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def import_backup_config(config_json: str) -> dict:
    """Import a backup job from a JSON config string. Use export_backup_config to get the correct format. The imported job will be created as a new backup job."""
    if not config_json or not config_json.strip():
        return {"error": "config_json must not be empty", "tool": "import_backup_config"}
    config_json = config_json.strip()
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
async def export_backup_config(backup_id: str, include_all_options: bool = False) -> dict:
    """Export a backup job's full configuration as JSON. Use this to save/restore job definitions or migrate to another Duplicati instance. include_all_options: include all effective options with defaults (larger output but fully self-contained)."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "export_backup_config"}
    backup_id = backup_id.strip()
    try:
        params: dict = {}
        if include_all_options:
            params["all-options"] = "true"
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/export", params=params if params else None)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "export_backup_config")
        err["backup_id"] = backup_id
        return err




@mcp.tool()
async def get_backup_commandline(backup_id: str) -> dict:
    """Get the equivalent command-line invocation for a backup job. Useful for understanding settings, debugging, or running the backup outside Duplicati."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_commandline"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/commandline")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_backup_commandline")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def run_backup(backup_id: str) -> dict:
    """Trigger a backup job to run immediately."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "run_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/run")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "triggered": True}}
    except Exception as e:
        err = _err(e, "run_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def abort_backup(backup_id: str) -> dict:
    """Abort a currently running backup job."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "abort_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/abort")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "aborted": True}}
    except Exception as e:
        err = _err(e, "abort_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def repair_backup(backup_id: str) -> dict:
    """Repair the local database for a backup job. Rebuilds index from destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "repair_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/repair")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "repair_started": True}}
    except Exception as e:
        err = _err(e, "repair_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def compact_backup(backup_id: str) -> dict:
    """Compact the backup destination: removes unused data blocks to reclaim storage space."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "compact_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/compact")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "compact_started": True}}
    except Exception as e:
        err = _err(e, "compact_backup")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def verify_backup(backup_id: str) -> dict:
    """Verify backup integrity by comparing local database with actual data at the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "verify_backup"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/verify")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "verify_started": True}}
    except Exception as e:
        err = _err(e, "verify_backup")
        err["backup_id"] = backup_id
        return err


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
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/filesets")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "list_versions")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def search_backup_files(backup_id: str, path_filter: str = "*", restore_time: str = "latest") -> dict:
    """Search files within a backup version. path_filter: glob pattern (e.g., '*.pdf'). restore_time: 'latest' or ISO timestamp."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "search_backup_files"}
    backup_id = backup_id.strip()
    rt = restore_time.strip() if restore_time and restore_time.strip() else "latest"
    if rt != "latest" and not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?', rt):
        return {"error": f"Invalid restore_time '{rt}'. Use 'latest' or an ISO timestamp (e.g. '2024-01-15T10:30:00Z').", "tool": "search_backup_files", "backup_id": backup_id}
    try:
        resp = await _request(
            "GET",
            f"/api/v1/backup/{backup_id}/files",
            params={"filter": path_filter.strip(), "time": rt},
        )
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "search_backup_files")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def restore_files(backup_id: str, restore_path: str, source_path: str = "", restore_time: str = "latest") -> dict:
    """Restore files from a backup to a local directory. restore_path: destination directory on this machine. source_path: optional comma-separated list of path filters within the backup (empty = restore all files). restore_time: 'latest' or ISO timestamp."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "restore_files"}
    backup_id = backup_id.strip()
    if not restore_path or not restore_path.strip():
        return {"error": "restore_path must not be empty", "tool": "restore_files"}
    restore_path = restore_path.strip()
    rt = restore_time.strip() if restore_time and restore_time.strip() else "latest"
    if rt != "latest" and not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?', rt):
        return {"error": f"Invalid restore_time '{rt}'. Use 'latest' or an ISO timestamp (e.g. '2024-01-15T10:30:00Z').", "tool": "restore_files", "backup_id": backup_id}
    try:
        payload: dict = {
            "restore-path": restore_path,
            "time": rt,
        }
        if source_path and source_path.strip():
            paths = [p.strip() for p in source_path.split(",") if p.strip()]
            if paths:
                payload["paths"] = paths
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/restore", json=payload)
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "restore_path": restore_path, "restore_started": True}}
    except Exception as e:
        err = _err(e, "restore_files")
        err["backup_id"] = backup_id
        err["restore_path"] = restore_path
        return err


@mcp.tool()
async def get_server_state() -> dict:
    """Get current Duplicati server runtime state: whether the scheduler is paused, active task info, program version, and last update check. Different from server_info which returns installed version metadata."""
    try:
        resp = await _request("GET", "/api/v1/serverstate")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_server_state")


@mcp.tool()
async def list_tasks() -> dict:
    """List all queued and running backup tasks in Duplicati. Returns task ID, backup ID, task type (Backup/Restore/Verify), and status. Use abort_backup to cancel a running backup task."""
    try:
        resp = await _request("GET", "/api/v1/tasks")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_tasks")


@mcp.tool()
async def get_task(task_id: int) -> dict:
    """Get details of a specific Duplicati task by its task ID. Returns task type, backup ID, and status. Use list_tasks to discover task IDs."""
    if task_id <= 0:
        return {"error": "task_id must be a positive integer", "tool": "get_task"}
    try:
        resp = await _request("GET", f"/api/v1/task/{task_id}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_task")


@mcp.tool()
async def stop_task(task_id: int) -> dict:
    """Stop a queued or running Duplicati task by its task ID. Use list_tasks to find task IDs. Running backup tasks are cancelled; queued tasks are dequeued."""
    if task_id <= 0:
        return {"error": "task_id must be a positive integer", "tool": "stop_task"}
    try:
        resp = await _request("DELETE", f"/api/v1/task/{task_id}")
        resp.raise_for_status()
        return {"result": {"task_id": task_id, "stopped": True}}
    except Exception as e:
        return _err(e, "stop_task")


@mcp.tool()
async def pause(duration: int = 0) -> dict:
    """Pause the Duplicati scheduler. duration: optional number of seconds to pause (0 = indefinite, converted to HH:MM:SS for Duplicati API)."""
    if duration < 0:
        return {"error": "duration must be a non-negative number of seconds", "tool": "pause"}
    if duration > 604800:
        return {"error": "duration must be <= 604800 seconds (7 days)", "tool": "pause"}
    try:
        params: dict = {}
        if duration > 0:
            h, rem = divmod(int(duration), 3600)
            m, s = divmod(rem, 60)
            params["duration"] = f"{h:02d}:{m:02d}:{s:02d}"
        resp = await _request("POST", "/api/v1/serverstate/pause", params=params)
        resp.raise_for_status()
        return {"result": {"paused": True, "duration": duration if duration > 0 else None}}
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
async def get_logs(backup_id: str = "", page_size: int = 20, page: int = 0, level: str = "") -> dict:
    """Retrieve recent log entries. backup_id: optional backup job ID — leave empty for server-wide logs. page_size: 1-500. page: 0-indexed page number. level: optional filter — one of General, Warning, Error, Retry, Upload, Download (server-wide logs only)."""
    page_size = min(max(1, page_size), 500)
    page = max(0, page)
    _valid_levels = {"General", "Warning", "Error", "Retry", "Upload", "Download"}
    if level:
        level = level.strip().title()
        if level not in _valid_levels:
            return {"error": f"Invalid level '{level}'. Valid: {', '.join(sorted(_valid_levels))}", "tool": "get_logs"}
    if backup_id and backup_id.strip() and level:
        return {"error": "level filter only applies to server-wide logs — omit backup_id to filter by level", "tool": "get_logs"}
    try:
        params: dict = {"pagesize": page_size, "page": page}
        if backup_id and backup_id.strip():
            path = f"/api/v1/backup/{backup_id.strip()}/log"
        else:
            path = "/api/v1/logdata/log"
            if level:
                params["level"] = level
        resp = await _request("GET", path, params=params)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_logs")
        if backup_id and backup_id.strip():
            err["backup_id"] = backup_id.strip()
        return err


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
async def update_server_settings(key: str, value: str) -> dict:
    """Update a single Duplicati server-level setting. key: setting name (e.g., 'startup-delay', 'max-upload-speed', 'max-download-speed'). value: new setting value as a string. Use get_server_settings to discover available keys."""
    if not key or not key.strip():
        return {"error": "key must not be empty", "tool": "update_server_settings"}
    key = key.strip()
    try:
        resp = await _request("PUT", "/api/v1/serversettings", json={key: value.strip()})
        resp.raise_for_status()
        return {"result": {"updated": True, "key": key, "value": value.strip()}}
    except Exception as e:
        return _err(e, "update_server_settings")


@mcp.tool()
async def dismiss_all_notifications() -> dict:
    """Dismiss all pending Duplicati notifications and alerts at once."""
    try:
        resp = await _request("DELETE", "/api/v1/notifications")
        resp.raise_for_status()
        return {"result": {"all_dismissed": True}}
    except Exception as e:
        return _err(e, "dismiss_all_notifications")



@mcp.tool()
async def get_backup_schedule(backup_id: str) -> dict:
    """Get the schedule for a backup job: next run time, repeat interval, and allowed days. Returns null schedule if no schedule is configured."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_schedule"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        data = resp.json()
        schedule = data.get("Schedule") or data.get("schedule")
        return {"result": {"backup_id": backup_id, "schedule": schedule}}
    except Exception as e:
        err = _err(e, "get_backup_schedule")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def set_backup_schedule(backup_id: str, repeat: str, time: str = "", allowed_days: str = "") -> dict:
    """Set or update the automatic schedule for a backup job. repeat: interval string ('1D' = daily, '1W' = weekly, '12H' = every 12 hours, '30M' = every 30 minutes). time: ISO 8601 datetime for next run (empty = now). allowed_days: comma-separated days to run on ('mon,tue,wed,thu,fri,sat,sun'). Fetches current config and PUTs updated version."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "set_backup_schedule"}
    backup_id = backup_id.strip()
    if not repeat or not repeat.strip():
        return {"error": "repeat must not be empty (e.g. '1D', '1W', '12H')", "tool": "set_backup_schedule"}
    repeat = repeat.strip().upper()
    if not re.match(r'^\d+[SMHDW]$', repeat):
        return {"error": f"Invalid repeat format '{repeat}'. Expected number + unit: S (seconds), M (minutes), H (hours), D (days), W (weeks). E.g. '1D', '12H', '30M'.", "tool": "set_backup_schedule"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()

        schedule = current.get("Schedule") or {}
        schedule["Repeat"] = repeat
        if time and time.strip():
            t = time.strip()
            if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', t):
                return {"error": f"Invalid time '{t}'. Use ISO 8601 format (e.g. '2024-01-15T10:30:00Z').", "tool": "set_backup_schedule", "backup_id": backup_id}
            schedule["Time"] = t
        elif not schedule.get("Time"):
            schedule["Time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if allowed_days and allowed_days.strip():
            _valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
            day_list = [d.strip().lower() for d in allowed_days.split(",") if d.strip()]
            invalid = [d for d in day_list if d not in _valid_days]
            if invalid:
                return {"error": f"Invalid allowed_days values: {invalid}. Use: mon,tue,wed,thu,fri,sat,sun", "tool": "set_backup_schedule"}
            schedule["AllowedDays"] = day_list

        current["Schedule"] = schedule
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "schedule": schedule}}
    except Exception as e:
        err = _err(e, "set_backup_schedule")
        err["backup_id"] = backup_id
        return err



@mcp.tool()
async def is_backup_active(backup_id: str) -> dict:
    """Check whether a backup job is currently running or queued. Queries the task list and filters by backup ID. Returns active boolean, task ID if running, and task type."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "is_backup_active"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", "/api/v1/tasks")
        resp.raise_for_status()
        tasks_raw = resp.json()
        tasks = tasks_raw if isinstance(tasks_raw, list) else []
        matching = [t for t in tasks if str(t.get("BackupID", "")) == backup_id or str(t.get("Backup", {}).get("ID", "")) == backup_id]
        if matching:
            task = matching[0]
            return {"result": {"backup_id": backup_id, "active": True, "task_id": task.get("ID"), "task_type": task.get("Operation", task.get("TaskType", ""))}}
        prog_resp = await _request("GET", "/api/v1/progressstate")
        prog_resp.raise_for_status()
        prog = prog_resp.json()
        if str(prog.get("BackupID", "")) == backup_id and prog.get("Phase", "") not in {"", "Backup_Complete", "Error"}:
            return {"result": {"backup_id": backup_id, "active": True, "phase": prog.get("Phase")}}
        return {"result": {"backup_id": backup_id, "active": False}}
    except Exception as e:
        err = _err(e, "is_backup_active")
        err["backup_id"] = backup_id
        return err



@mcp.tool()
async def delete_backup_schedule(backup_id: str) -> dict:
    """Remove the automatic schedule from a backup job so it only runs on-demand. Fetches current config, sets Schedule to null, and PUTs updated version. Pairs with set_backup_schedule."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_backup_schedule"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()
        current["Schedule"] = None
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "schedule_removed": True}}
    except Exception as e:
        err = _err(e, "delete_backup_schedule")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def get_server_setting(key: str) -> dict:
    """Get a single Duplicati server-level setting by key. Returns the value for the requested key. Use get_server_settings to discover all available keys and their current values."""
    if not key or not key.strip():
        return {"error": "key must not be empty", "tool": "get_server_setting"}
    key = key.strip()
    try:
        resp = await _request("GET", "/api/v1/serversettings")
        resp.raise_for_status()
        settings = resp.json()
        if key not in settings:
            return {"error": f"Setting '{key}' not found. Use get_server_settings to list valid keys.", "tool": "get_server_setting"}
        return {"result": {"key": key, "value": settings[key]}}
    except Exception as e:
        return _err(e, "get_server_setting")


@mcp.tool()
async def test_connection(destination_url: str) -> dict:
    """Test connectivity to a Duplicati backup destination URL without running a backup. Verifies credentials, permissions, and reachability. destination_url: Duplicati backend URL (e.g., 's3://bucket/path', 'file:///mnt/backup', 'ftp://host/path'). Returns success/failure and any error details."""
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "test_connection"}
    destination_url = destination_url.strip()
    try:
        resp = await _request(
            "POST",
            "/api/v1/remoteoperation/test",
            json={"uri": destination_url},
        )
        if not resp.is_success:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
            return {"result": {"destination_url": destination_url, "success": False, "error": body, "status_code": resp.status_code}}
        data = resp.json()
        return {"result": {"destination_url": destination_url, "success": True, "response": data}}
    except Exception as e:
        err = _err(e, "test_connection")
        err["destination_url"] = destination_url
        return err


@mcp.tool()
async def clear_logs(backup_id: str = "") -> dict:
    """Clear Duplicati log entries. backup_id: optional — if provided, clears only that backup job's logs; leave empty to clear all server-wide logs."""
    backup_id = backup_id.strip() if backup_id else ""
    try:
        if backup_id:
            resp = await _request("DELETE", f"/api/v1/backup/{backup_id}/log")
        else:
            resp = await _request("DELETE", "/api/v1/logdata/log")
        resp.raise_for_status()
        return {"result": {"cleared": True, "backup_id": backup_id or None}}
    except Exception as e:
        err = _err(e, "clear_logs")
        if backup_id:
            err["backup_id"] = backup_id
        return err


@mcp.tool()
async def list_notifications() -> dict:
    """List all pending Duplicati notifications — backup failures, warnings, and informational messages. Returns notification ID, type, message, and timestamp."""
    try:
        resp = await _request("GET", "/api/v1/notifications")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_notifications")


@mcp.tool()
async def dismiss_notification(notification_id: str) -> dict:
    """Dismiss (acknowledge and delete) a Duplicati notification by ID. Use list_notifications to find notification IDs."""
    if not notification_id or not notification_id.strip():
        return {"error": "notification_id must not be empty", "tool": "dismiss_notification"}
    notification_id = notification_id.strip()
    try:
        resp = await _request("DELETE", f"/api/v1/notification/{notification_id}")
        resp.raise_for_status()
        return {"result": {"notification_id": notification_id, "dismissed": True}}
    except Exception as e:
        err = _err(e, "dismiss_notification")
        err["notification_id"] = notification_id
        return err


@mcp.tool()
async def get_system_info() -> dict:
    """Get Duplicati system information: machine name, configuration directory, log directory, temporary directory, database path, default settings, and installed backend/encryption modules. Useful for verifying the Duplicati installation and locating config files."""
    try:
        resp = await _request("GET", "/api/v1/systeminfo")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_system_info")


@mcp.tool()
async def list_installed_backends() -> dict:
    """List all storage backend modules installed in Duplicati. Returns each backend's key, display name, supported URL schemes, and configurable options. Useful when setting up new backup jobs to discover available destinations (S3, FTP, Backblaze B2, Google Drive, Azure Blob, etc.)."""
    try:
        resp = await _request("GET", "/api/v1/backendmodules")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_installed_backends")


@mcp.tool()
async def list_remote_volumes(backup_id: str) -> dict:
    """List the remote storage volumes (dblock/dindex/dlist files) stored at the backup destination for a job. Returns file names, sizes, and last modified timestamps. Useful for auditing destination storage and understanding backup storage layout. backup_id: backup job ID from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "list_remote_volumes"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/remotevolumes")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "list_remote_volumes")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def list_encryption_modules() -> dict:
    """List all encryption modules installed in Duplicati. Returns each module's key, display name, and configurable options. Useful when setting up new backup jobs to discover available encryption algorithms (AES-256, GPG, etc.)."""
    try:
        resp = await _request("GET", "/api/v1/encryptionmodules")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_encryption_modules")


@mcp.tool()
async def list_compression_modules() -> dict:
    """List all compression modules installed in Duplicati. Returns each module's key, display name, and configurable options. Useful when setting up new backup jobs to discover available compression algorithms (zip, lzma, etc.)."""
    try:
        resp = await _request("GET", "/api/v1/compressionmodules")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_compression_modules")


@mcp.tool()
async def list_filters() -> dict:
    """List built-in filter groups available in Duplicati. Filter groups are predefined sets of exclusion patterns for common use cases (e.g., exclude OS temp files, version control directories, browser caches). Returns each group's key name and patterns — use the key in backup job filter configuration."""
    try:
        resp = await _request("GET", "/api/v1/filters")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_filters")


@mcp.tool()
async def check_updates() -> dict:
    """Check if a newer version of Duplicati is available. Returns current version, latest available version, release type (stable/beta/experimental/canary), download URL, and release notes summary. Useful for keeping backup agents up to date."""
    try:
        resp = await _request("GET", "/api/v1/updates")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "check_updates")


@mcp.tool()
async def purge_deleted_files(backup_id: str) -> dict:
    """Purge backup set entries for files that have been deleted from the source paths. Scans backup history and removes file versions that no longer exist at source, reclaiming remote storage space over time. Different from repair_backup (which fixes the local DB) and compact_backup (which reclaims storage from expired retention). backup_id: backup job ID from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "purge_deleted_files"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/purge")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "purge_started": True}}
    except Exception as e:
        err = _err(e, "purge_deleted_files")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def get_changelog() -> dict:
    """Get the Duplicati release changelog — recent version history with release notes for the installed and available versions. Useful for understanding what changed between versions before upgrading."""
    try:
        resp = await _request("GET", "/api/v1/changelog")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_changelog")


@mcp.tool()
async def delete_local_database(backup_id: str) -> dict:
    """Delete the local SQLite database for a backup job and schedule a rebuild from remote storage. Use this when the local database is corrupt and repair_backup fails — Duplicati will recreate it by scanning the remote destination. The backup job is temporarily unavailable while the database is being rebuilt. backup_id: backup job ID from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_local_database"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("DELETE", f"/api/v1/backup/{backup_id}/database")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "database_deleted": True, "note": "Duplicati will rebuild the local database on next run"}}
    except Exception as e:
        err = _err(e, "delete_local_database")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def list_sources(destination_url: str, path: str = "/") -> dict:
    """Browse a remote backup destination to list files and folders. Useful for verifying the backup target is accessible and seeing what's stored there. destination_url: backend URL (e.g. s3://bucket/prefix, ftp://host/path, file:///local). path: folder to list within the destination (default: root)."""
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "list_sources"}
    try:
        params = {"url": destination_url.strip(), "path": path.strip() or "/"}
        resp = await _request("GET", "/api/v1/remoteoperation/list", params=params)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_sources")


@mcp.tool()
async def create_remote_folder(destination_url: str) -> dict:
    """Create the remote folder/bucket for a backup destination if it does not exist. destination_url: backend URL pointing to the folder to create (e.g. s3://bucket/prefix)."""
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "create_remote_folder"}
    try:
        resp = await _request("POST", "/api/v1/remoteoperation/create", json={"url": destination_url.strip()})
        resp.raise_for_status()
        return {"result": {"created": True, "destination_url": destination_url.strip()}}
    except Exception as e:
        return _err(e, "create_remote_folder")


@mcp.tool()
async def get_backup_statistics(backup_id: str) -> dict:
    """Get detailed statistics for a backup job: total file count, total size, last backup duration, added/modified/deleted file counts, and remote size. More detailed than backup_status. backup_id: backup job ID from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_statistics"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/filesets")
        resp.raise_for_status()
        filesets = resp.json() or []
        latest = filesets[0] if filesets else {}
        stats_resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        stats_resp.raise_for_status()
        backup_info = stats_resp.json() or {}
        settings = backup_info.get("Backup", {})
        return {"result": {
            "backup_id": backup_id,
            "name": settings.get("Name"),
            "latest_fileset": latest,
            "total_filesets": len(filesets),
            "source_size": settings.get("Metadata", {}).get("SourceSizeString"),
            "backup_size": settings.get("Metadata", {}).get("BackupSizeString"),
            "last_duration": settings.get("Metadata", {}).get("LastBackupDuration"),
            "last_backup": settings.get("Metadata", {}).get("LastBackupDate"),
            "file_count": settings.get("Metadata", {}).get("SourceFilesCount"),
        }}
    except Exception as e:
        err = _err(e, "get_backup_statistics")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def send_test_notification() -> dict:
    """Send a test notification using the currently configured notification module (email, webhook, etc.). Use this to verify notification settings are working before relying on them for backup alerts."""
    try:
        resp = await _request("POST", "/api/v1/notifications/test")
        resp.raise_for_status()
        return {"result": resp.json() if resp.text.strip() else {"sent": True}}
    except Exception as e:
        return _err(e, "send_test_notification")


@mcp.tool()
async def get_ui_settings() -> dict:
    """Get Duplicati web UI settings: language, startup wizard visibility, theme, and other interface preferences."""
    try:
        resp = await _request("GET", "/api/v1/uisettings")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_ui_settings")


@mcp.tool()
async def update_ui_settings(settings_json: str) -> dict:
    """Update Duplicati web UI settings. settings_json: JSON object with key-value pairs (e.g. '{\"language\": \"en-US\", \"startup-delay\": \"0\"}'. Use get_ui_settings to see current settings and available keys."""
    if not settings_json or not settings_json.strip():
        return {"error": "settings_json must not be empty", "tool": "update_ui_settings"}
    try:
        settings = json.loads(settings_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "update_ui_settings"}
    try:
        resp = await _request("POST", "/api/v1/uisettings", json=settings)
        resp.raise_for_status()
        return {"result": resp.json() if resp.text.strip() else {"saved": True}}
    except Exception as e:
        return _err(e, "update_ui_settings")


@mcp.tool()
async def get_backup_defaults() -> dict:
    """Get the default settings applied to all new Duplicati backup jobs: default compression, encryption, retention policy, and other global defaults."""
    try:
        resp = await _request("GET", "/api/v1/backupdefaults")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_backup_defaults")


@mcp.tool()
async def vacuum_database(backup_id: str) -> dict:
    """Run SQLite VACUUM on the local database for a backup job to reclaim disk space and optimize performance. Useful after large backup deletions or compactions. backup_id: backup job ID from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "vacuum_database"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id}/vacuum")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "vacuumed": True}}
    except Exception as e:
        err = _err(e, "vacuum_database")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def poll_operations(last_event_id: int = -1) -> dict:
    """Poll for Duplicati server events since a given event ID. Returns the current server state and any new events (task starts/completions, errors, notifications). last_event_id: start from this event ID (-1 = only return current state without waiting). Use the returned 'last_event_id' in subsequent calls to get only new events."""
    try:
        params: dict = {"lasteventid": last_event_id, "lonpolltime": 0}
        resp = await _request("GET", "/api/v1/serverstate", params=params)
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "poll_operations")


@mcp.tool()
async def abort_task(task_id: int) -> dict:
    """Abort a running Duplicati background task. task_id: task ID from poll_operations or backup operation responses. Returns immediately; use get_task to confirm termination."""
    if task_id <= 0:
        return {"error": "task_id must be a positive integer", "tool": "abort_task"}
    try:
        resp = await _request("POST", f"/api/v1/task/{task_id}/abort")
        resp.raise_for_status()
        return {"result": {"task_id": task_id, "aborted": True}}
    except Exception as e:
        return _err(e, "abort_task")


@mcp.tool()
async def add_backup_filter(backup_id: str, expression: str, include: bool = False) -> dict:
    """Add a single include or exclude filter to an existing backup without replacing all existing filters. expression: glob pattern (e.g. '*.tmp', '[*.log]', '/path/to/exclude/**'). include: True to include matching files, False (default) to exclude them. Use list_backups to find backup IDs, get_backup to see current filters."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "add_backup_filter"}
    if not expression or not expression.strip():
        return {"error": "expression must not be empty", "tool": "add_backup_filter"}
    backup_id = backup_id.strip()
    expression = expression.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()
        backup = current.get("Backup", current)
        filters = backup.get("Filters") or []
        order = max((f.get("Order", 0) for f in filters), default=-1) + 1
        filters.append({"Order": order, "Include": include, "Expression": expression})
        backup["Filters"] = filters
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "added": {"expression": expression, "include": include, "order": order}, "total_filters": len(filters)}}
    except Exception as e:
        err = _err(e, "add_backup_filter")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def remove_backup_filter(backup_id: str, expression: str) -> dict:
    """Remove a specific filter from a backup by its glob expression without affecting other filters. Use get_backup to list current filters. backup_id: from list_backups. expression: exact glob pattern to remove (e.g. '*.tmp')."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "remove_backup_filter"}
    if not expression or not expression.strip():
        return {"error": "expression must not be empty", "tool": "remove_backup_filter"}
    backup_id = backup_id.strip()
    expression = expression.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()
        backup = current.get("Backup", current)
        filters = backup.get("Filters") or []
        before = len(filters)
        filters = [f for f in filters if f.get("Expression") != expression]
        if len(filters) == before:
            return {"error": f"No filter with expression '{expression}' found", "tool": "remove_backup_filter"}
        for i, f in enumerate(filters):
            f["Order"] = i
        backup["Filters"] = filters
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "removed": expression, "remaining_filters": len(filters)}}
    except Exception as e:
        err = _err(e, "remove_backup_filter")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def list_backup_filters(backup_id: str) -> dict:
    """List all include and exclude filters configured for a backup job. Returns each filter's expression, include/exclude flag, and sort order. Use add_backup_filter and remove_backup_filter to manage individual filters."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "list_backup_filters"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        data = resp.json()
        backup = data.get("Backup", data)
        filters = sorted(backup.get("Filters") or [], key=lambda f: f.get("Order", 0))
        return {"result": {"backup_id": backup_id, "filters": filters, "total": len(filters)}}
    except Exception as e:
        err = _err(e, "list_backup_filters")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def get_backup_report(backup_id: str) -> dict:
    """Get the detailed report for the most recently completed backup run: files examined, added, deleted, modified, errors, warnings, duration, and start/end times. backup_id: from list_backups."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_report"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}/log?level=Information&pagesize=1")
        if resp.status_code == 404:
            resp = await _request("GET", f"/api/v1/backup/{backup_id}/log?pagesize=1")
        resp.raise_for_status()
        logs = resp.json()
        status_resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        status_resp.raise_for_status()
        status_data = status_resp.json()
        backup = status_data.get("Backup", status_data)
        metadata = backup.get("Metadata") or {}
        return {"result": {
            "backup_id": backup_id,
            "last_run": metadata.get("LastBackupDate"),
            "last_result": metadata.get("LastBackupResult"),
            "files_examined": metadata.get("LastBackupFilesExamined"),
            "files_added": metadata.get("LastBackupAddedFiles"),
            "files_deleted": metadata.get("LastBackupDeletedFiles"),
            "files_modified": metadata.get("LastBackupModifiedFiles"),
            "source_size_bytes": metadata.get("SourceFilesSize"),
            "backup_size_bytes": metadata.get("LastBackupSize"),
            "errors": metadata.get("LastBackupErrors"),
            "warnings": metadata.get("LastBackupWarnings"),
            "duration": metadata.get("LastBackupDuration"),
        }}
    except Exception as e:
        err = _err(e, "get_backup_report")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def move_backup_source(backup_id: str, old_path: str, new_path: str) -> dict:
    """Update a single source path in a backup job without replacing all sources. Useful when a directory is moved or renamed. backup_id: from list_backups. old_path: exact path to replace. new_path: replacement path. Errors if old_path is not found in current sources."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "move_backup_source"}
    if not old_path or not old_path.strip():
        return {"error": "old_path must not be empty", "tool": "move_backup_source"}
    if not new_path or not new_path.strip():
        return {"error": "new_path must not be empty", "tool": "move_backup_source"}
    backup_id = backup_id.strip()
    old_path = old_path.strip()
    new_path = new_path.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        current = resp.json()
        backup = current.get("Backup", current)
        sources = backup.get("Sources") or []
        if old_path not in sources:
            return {"error": f"Path '{old_path}' not found in backup sources", "tool": "move_backup_source"}
        backup["Sources"] = [new_path if s == old_path else s for s in sources]
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id, "replaced": old_path, "with": new_path, "total_sources": len(backup["Sources"])}}
    except Exception as e:
        err = _err(e, "move_backup_source")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def is_backup_overdue(backup_id: str, max_hours: float = 25.0) -> dict:
    """Check if a backup job has not run within the expected interval. Returns overdue=True if the last successful run was more than max_hours ago, or if the backup has never run. max_hours: expected maximum hours between backups (default 25.0, slightly over 24h to account for scheduling drift)."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "is_backup_overdue"}
    if max_hours <= 0:
        return {"error": "max_hours must be greater than 0", "tool": "is_backup_overdue"}
    backup_id = backup_id.strip()
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        data = resp.json()
        backup = data.get("Backup", data)
        metadata = backup.get("Metadata") or {}
        last_run_str = metadata.get("LastBackupDate")
        last_result = metadata.get("LastBackupResult")
        if not last_run_str:
            return {"result": {"backup_id": backup_id, "overdue": True, "reason": "never_run", "last_run": None, "max_hours": max_hours}}
        last_run = datetime.datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        hours_since = (now - last_run).total_seconds() / 3600
        overdue = hours_since > max_hours
        return {"result": {"backup_id": backup_id, "overdue": overdue, "hours_since_last_run": round(hours_since, 2), "last_run": last_run_str, "last_result": last_result, "max_hours": max_hours}}
    except Exception as e:
        err = _err(e, "is_backup_overdue")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def get_backup_retention(backup_id: str) -> dict:
    """Get the configured retention policy for a backup job: keep-versions count, keep-time duration, and retention-policy string. Returns all retention-related settings extracted from the backup configuration."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_retention"}
    backup_id = backup_id.strip()
    retention_keys = {"keep-versions", "keep-time", "retention-policy", "backup-retention", "no-auto-compact"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        data = resp.json()
        backup = data.get("Backup", data)
        settings = backup.get("Settings") or []
        retention = {s["Name"]: s.get("Value") for s in settings if s.get("Name") in retention_keys}
        return {"result": {"backup_id": backup_id, "retention": retention, "has_retention_policy": bool(retention)}}
    except Exception as e:
        err = _err(e, "get_backup_retention")
        err["backup_id"] = backup_id
        return err


@mcp.tool()
async def set_backup_retention(
    backup_id: str,
    keep_versions: int = 0,
    keep_time: str = "",
    retention_policy: str = "",
    no_auto_compact: bool = False,
) -> dict:
    """Set the retention policy for a backup job. keep_versions: number of backup versions to keep (0 = no limit by count). keep_time: time span to keep backups (e.g. '6M' for 6 months, '1Y' for 1 year, '30D' for 30 days). retention_policy: advanced tiered expression (e.g. '1W:1D,4W:1W,12M:1M' = keep daily for 1 week, weekly for 4 weeks, monthly for 12 months). no_auto_compact: disable automatic space reclamation. Existing retention settings are replaced."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "set_backup_retention"}
    if keep_versions < 0:
        return {"error": "keep_versions must be >= 0 (0 = unlimited)", "tool": "set_backup_retention"}
    if keep_time and keep_time.strip():
        import re as _re_rt
        if not _re_rt.match(r"^\d+[smhdwDWMY]$", keep_time.strip()):
            return {"error": "keep_time format must be a number followed by a unit: s(econds), m(inutes), h(ours), d/D(ays), W(eeks), M(onths), Y(ears) — e.g. '30D', '6M', '1Y'", "tool": "set_backup_retention"}
    backup_id = backup_id.strip()
    retention_map = {
        "keep-versions": str(keep_versions) if keep_versions > 0 else "",
        "keep-time": keep_time.strip() if keep_time else "",
        "retention-policy": retention_policy.strip() if retention_policy else "",
        "no-auto-compact": "true" if no_auto_compact else "",
    }
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id}")
        resp.raise_for_status()
        data = resp.json()
        backup = data.get("Backup", data)
        settings = backup.get("Settings") or []
        retention_keys = set(retention_map.keys())
        settings = [s for s in settings if s.get("Name") not in retention_keys]
        for name, value in retention_map.items():
            if value:
                settings.append({"Name": name, "Value": value})
        backup["Settings"] = settings
        data["Backup"] = backup
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id}", json=data)
        put_resp.raise_for_status()
        applied = {k: v for k, v in retention_map.items() if v}
        return {"result": {"backup_id": backup_id, "retention_applied": applied}}
    except Exception as e:
        err = _err(e, "set_backup_retention")
        err["backup_id"] = backup_id
        return err


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
