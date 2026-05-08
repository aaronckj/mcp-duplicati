"""mcp-duplicati: Duplicati backup management MCP server."""

from __future__ import annotations

import json
import os
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
    """Get Duplicati server version, OS type, and server time. For full runtime state (scheduler status, active tasks, pause state), use get_server_state instead."""
    try:
        resp = await _request("GET", "/api/v1/serverstate")
        resp.raise_for_status()
        data = resp.json()
        return {"result": {
            "version": data.get("Version"),
            "package_build_date": data.get("PackageBuildDate"),
            "server_version": data.get("ServerVersion"),
            "os": data.get("OSType"),
            "server_time": data.get("ServerTime"),
        }}
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
    """Get operational status of a backup job: last run time, last result, next scheduled run, and source size metrics. For the full job configuration (source paths, filters, settings), use get_backup instead."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "backup_status"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        data = resp.json()
        backup = data.get("Backup", data)
        schedule = data.get("Schedule") or {}
        metadata = backup.get("Metadata") or {}
        return {"result": {
            "backup_id": backup_id.strip(),
            "name": backup.get("Name"),
            "last_run": metadata.get("LastBackupDate"),
            "last_result": metadata.get("LastBackupResult"),
            "source_files_count": metadata.get("SourceFilesCount"),
            "source_size_bytes": metadata.get("SourceFilesSize"),
            "next_run": schedule.get("Time") if schedule else None,
            "schedule_repeat": schedule.get("Repeat") if schedule else None,
        }}
    except Exception as e:
        return _err(e, "backup_status")


@mcp.tool()
async def get_backup(backup_id: str) -> dict:
    """Get full configuration of a backup job including source paths, destination, schedule, and settings. Different from backup_status which only returns the last run statistics."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_backup")


@mcp.tool()
async def create_backup(name: str, source_paths: str, destination_url: str, passphrase: str = "", exclude_filters: str = "") -> dict:
    """Create a new Duplicati backup job. source_paths: comma-separated local paths to back up. destination_url: Duplicati backend URL (e.g., 'file:///mnt/backup', 's3://bucket/path'). passphrase: optional AES-256 encryption key. exclude_filters: comma-separated glob patterns to exclude (e.g., '*.tmp,*.log,/proc/*')."""
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
    filters: list[dict] = []
    if exclude_filters:
        for pattern in [p.strip() for p in exclude_filters.split(",") if p.strip()]:
            filters.append({"Order": len(filters), "Include": False, "Expression": pattern})
    config = {
        "Backup": {
            "Name": name.strip(),
            "TargetURL": destination_url.strip(),
            "Sources": sources,
            "Settings": settings,
            "Filters": filters,
        },
        "Schedule": None,
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
) -> dict:
    """Update an existing backup job. Only non-empty fields are changed. Fetches current config, applies changes, then PUTs the updated config. exclude_filters: comma-separated glob patterns to exclude (replaces existing filters; omit to keep current filters)."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "update_backup"}
    if not any([name, source_paths, destination_url, passphrase, exclude_filters]):
        return {"error": "At least one field to update must be specified", "tool": "update_backup"}
    try:
        bid = backup_id.strip()
        resp = await _request("GET", f"/api/v1/backup/{bid}")
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

        put_resp = await _request("PUT", f"/api/v1/backup/{bid}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": bid, "updated": True}}
    except Exception as e:
        return _err(e, "update_backup")


@mcp.tool()
async def delete_backup(backup_id: str) -> dict:
    """Delete a backup job configuration. Does NOT delete backup data on the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_backup"}
    try:
        resp = await _request("DELETE", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "deleted": True}}
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
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}/export")
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
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}/commandline")
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
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/run")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "triggered": True}}
    except Exception as e:
        return _err(e, "run_backup")


@mcp.tool()
async def abort_backup(backup_id: str) -> dict:
    """Abort a currently running backup job."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "abort_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/abort")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "aborted": True}}
    except Exception as e:
        return _err(e, "abort_backup")


@mcp.tool()
async def repair_backup(backup_id: str) -> dict:
    """Repair the local database for a backup job. Rebuilds index from destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "repair_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/repair")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "repair_started": True}}
    except Exception as e:
        return _err(e, "repair_backup")


@mcp.tool()
async def compact_backup(backup_id: str) -> dict:
    """Compact the backup destination: removes unused data blocks to reclaim storage space."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "compact_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/compact")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "compact_started": True}}
    except Exception as e:
        return _err(e, "compact_backup")


@mcp.tool()
async def verify_backup(backup_id: str) -> dict:
    """Verify backup integrity by comparing local database with actual data at the destination."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "verify_backup"}
    try:
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/verify")
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "verify_started": True}}
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
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}/filesets")
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
            f"/api/v1/backup/{backup_id.strip()}/files",
            params={"filter": path_filter.strip(), "time": restore_time.strip()},
        )
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "search_backup_files")


@mcp.tool()
async def restore_files(backup_id: str, restore_path: str, source_path: str = "", restore_time: str = "latest") -> dict:
    """Restore files from a backup to a local directory. restore_path: destination directory on this machine. source_path: optional comma-separated list of path filters within the backup (empty = restore all files). restore_time: 'latest' or ISO timestamp."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "restore_files"}
    if not restore_path or not restore_path.strip():
        return {"error": "restore_path must not be empty", "tool": "restore_files"}
    try:
        payload: dict = {
            "restore-path": restore_path.strip(),
            "time": restore_time.strip(),
        }
        if source_path and source_path.strip():
            paths = [p.strip() for p in source_path.split(",") if p.strip()]
            if paths:
                payload["paths"] = paths
        resp = await _request("POST", f"/api/v1/backup/{backup_id.strip()}/restore", json=payload)
        resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "restore_path": restore_path.strip(), "restore_started": True}}
    except Exception as e:
        return _err(e, "restore_files")


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
async def get_task(task_id: str) -> dict:
    """Get details of a specific Duplicati task by its task ID. Returns task type, backup ID, and status. Use list_tasks to discover task IDs."""
    if not task_id or not task_id.strip():
        return {"error": "task_id must not be empty", "tool": "get_task"}
    try:
        resp = await _request("GET", f"/api/v1/task/{task_id.strip()}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_task")


@mcp.tool()
async def stop_task(task_id: str) -> dict:
    """Stop a queued or running Duplicati task by its task ID. Use list_tasks to find task IDs. Running backup tasks are cancelled; queued tasks are dequeued."""
    if not task_id or not task_id.strip():
        return {"error": "task_id must not be empty", "tool": "stop_task"}
    try:
        resp = await _request("DELETE", f"/api/v1/task/{task_id.strip()}")
        resp.raise_for_status()
        return {"result": {"task_id": task_id.strip(), "stopped": True}}
    except Exception as e:
        return _err(e, "stop_task")


@mcp.tool()
async def pause(duration: int = 0) -> dict:
    """Pause the Duplicati scheduler. duration: optional number of seconds to pause (0 = indefinite, converted to HH:MM:SS for Duplicati API)."""
    if duration < 0:
        return {"error": "duration must be a non-negative number of seconds", "tool": "pause"}
    try:
        params: dict = {}
        if duration > 0:
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
async def get_logs(backup_id: str = "", page_size: int = 20, page: int = 0, level: str = "") -> dict:
    """Retrieve recent log entries. backup_id: optional backup job ID — leave empty for server-wide logs. page_size: 1-500. page: 0-indexed page number. level: optional filter — one of General, Warning, Error, Retry, Upload, Download (server-wide logs only)."""
    page_size = min(max(1, page_size), 500)
    page = max(0, page)
    _valid_levels = {"General", "Warning", "Error", "Retry", "Upload", "Download"}
    if level:
        level = level.strip().title()
        if level not in _valid_levels:
            return {"error": f"Invalid level '{level}'. Valid: {', '.join(sorted(_valid_levels))}", "tool": "get_logs"}
    try:
        params: dict = {"pagesize": page_size, "page": page}
        if backup_id and backup_id.strip():
            path = f"/api/v1/backup/{backup_id.strip()}/log"
            if level:
                params["level"] = level
        else:
            path = "/api/v1/logdata/log"
            if level:
                params["level"] = level
        resp = await _request("GET", path, params=params)
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
async def update_server_settings(key: str, value: str) -> dict:
    """Update a single Duplicati server-level setting. key: setting name (e.g., 'startup-delay', 'max-upload-speed', 'max-download-speed'). value: new setting value as a string. Use get_server_settings to discover available keys."""
    if not key or not key.strip():
        return {"error": "key must not be empty", "tool": "update_server_settings"}
    try:
        resp = await _request("PUT", "/api/v1/serversettings", json={key.strip(): value.strip()})
        resp.raise_for_status()
        return {"result": {"updated": True, "key": key.strip(), "value": value.strip()}}
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
        resp = await _request("DELETE", f"/api/v1/notification/{notification_id.strip()}")
        resp.raise_for_status()
        return {"result": {"notification_id": notification_id.strip(), "dismissed": True}}
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



@mcp.tool()
async def get_backup_schedule(backup_id: str) -> dict:
    """Get the schedule for a backup job: next run time, repeat interval, and allowed days. Returns null schedule if no schedule is configured."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "get_backup_schedule"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        data = resp.json()
        schedule = data.get("Schedule") or data.get("schedule")
        return {"result": {"backup_id": backup_id.strip(), "schedule": schedule}}
    except Exception as e:
        return _err(e, "get_backup_schedule")


@mcp.tool()
async def set_backup_schedule(backup_id: str, repeat: str, time: str = "", allowed_days: str = "") -> dict:
    """Set or update the automatic schedule for a backup job. repeat: interval string ('1D' = daily, '1W' = weekly, '12H' = every 12 hours, '30M' = every 30 minutes). time: ISO 8601 datetime for next run (empty = now). allowed_days: comma-separated days to run on ('mon,tue,wed,thu,fri,sat,sun'). Fetches current config and PUTs updated version."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "set_backup_schedule"}
    if not repeat or not repeat.strip():
        return {"error": "repeat must not be empty (e.g. '1D', '1W', '12H')", "tool": "set_backup_schedule"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        current = resp.json()

        schedule = current.get("Schedule") or {}
        schedule["Repeat"] = repeat.strip()
        if time and time.strip():
            schedule["Time"] = time.strip()
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
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id.strip()}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "schedule": schedule}}
    except Exception as e:
        return _err(e, "set_backup_schedule")



@mcp.tool()
async def is_backup_active(backup_id: str) -> dict:
    """Check whether a backup job is currently running or queued. Queries the task list and filters by backup ID. Returns active boolean, task ID if running, and task type."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "is_backup_active"}
    bid = backup_id.strip()
    try:
        resp = await _request("GET", "/api/v1/tasks")
        resp.raise_for_status()
        tasks_raw = resp.json()
        tasks = tasks_raw if isinstance(tasks_raw, list) else []
        matching = [t for t in tasks if str(t.get("BackupID", "")) == bid or str(t.get("Backup", {}).get("ID", "")) == bid]
        if matching:
            task = matching[0]
            return {"result": {"backup_id": bid, "active": True, "task_id": task.get("ID"), "task_type": task.get("Operation", task.get("TaskType", ""))}}
        prog_resp = await _request("GET", "/api/v1/progressstate")
        if prog_resp.status_code == 200:
            prog = prog_resp.json()
            if str(prog.get("BackupID", "")) == bid and prog.get("Phase", "") not in {"", "Backup_Complete", "Error"}:
                return {"result": {"backup_id": bid, "active": True, "phase": prog.get("Phase")}}
        return {"result": {"backup_id": bid, "active": False}}
    except Exception as e:
        return _err(e, "is_backup_active")



@mcp.tool()
async def delete_backup_schedule(backup_id: str) -> dict:
    """Remove the automatic schedule from a backup job so it only runs on-demand. Fetches current config, sets Schedule to null, and PUTs updated version. Pairs with set_backup_schedule."""
    if not backup_id or not backup_id.strip():
        return {"error": "backup_id must not be empty", "tool": "delete_backup_schedule"}
    try:
        resp = await _request("GET", f"/api/v1/backup/{backup_id.strip()}")
        resp.raise_for_status()
        current = resp.json()
        current["Schedule"] = None
        put_resp = await _request("PUT", f"/api/v1/backup/{backup_id.strip()}", json=current)
        put_resp.raise_for_status()
        return {"result": {"backup_id": backup_id.strip(), "schedule_removed": True}}
    except Exception as e:
        return _err(e, "delete_backup_schedule")


@mcp.tool()
async def get_server_setting(key: str) -> dict:
    """Get a single Duplicati server-level setting by key. Returns the value for the requested key. Use get_server_settings to discover all available keys and their current values."""
    if not key or not key.strip():
        return {"error": "key must not be empty", "tool": "get_server_setting"}
    try:
        resp = await _request("GET", "/api/v1/serversettings")
        resp.raise_for_status()
        settings = resp.json()
        k = key.strip()
        if k not in settings:
            return {"error": f"Setting '{k}' not found. Use get_server_settings to list valid keys.", "tool": "get_server_setting"}
        return {"result": {"key": k, "value": settings[k]}}
    except Exception as e:
        return _err(e, "get_server_setting")


@mcp.tool()
async def test_connection(destination_url: str) -> dict:
    """Test connectivity to a Duplicati backup destination URL without running a backup. Verifies credentials, permissions, and reachability. destination_url: Duplicati backend URL (e.g., 's3://bucket/path', 'file:///mnt/backup', 'ftp://host/path'). Returns success/failure and any error details."""
    if not destination_url or not destination_url.strip():
        return {"error": "destination_url must not be empty", "tool": "test_connection"}
    try:
        resp = await _request(
            "POST",
            "/api/v1/remoteoperation/test",
            json={"uri": destination_url.strip()},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"result": {"destination_url": destination_url.strip(), "success": True, "response": data}}
    except Exception as e:
        return _err(e, "test_connection")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
