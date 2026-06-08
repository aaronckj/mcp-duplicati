"""Tests for mcp-duplicati tools. All HTTP calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Auth layer tests
# ---------------------------------------------------------------------------

async def test_login_caches_session_token(monkeypatch):
    """_login() POSTs credentials and returns the JWT AccessToken."""
    monkeypatch.setenv("DUPLICATI_PASSWORD", "testpass")
    monkeypatch.setenv("DUPLICATI_HOST", "http://localhost:8200")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"AccessToken": "tok123"})
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("mcp_duplicati.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_duplicati.server as srv
        srv._session_token = None
        token = await srv._login()

    assert token == "tok123"
    mock_client.post.assert_called_once_with(
        "http://localhost:8200/api/v1/auth/login",
        json={"Password": "testpass"},
    )


async def test_login_missing_password_raises(monkeypatch):
    """_login() raises ValueError when DUPLICATI_PASSWORD is not set."""
    monkeypatch.delenv("DUPLICATI_PASSWORD", raising=False)

    import mcp_duplicati.server as srv
    with pytest.raises(ValueError, match="DUPLICATI_PASSWORD"):
        await srv._login()


async def test_request_uses_cached_token(monkeypatch):
    """_request() sends the cached JWT as a Bearer Authorization header."""
    monkeypatch.setenv("DUPLICATI_HOST", "http://localhost:8200")
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("mcp_duplicati.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_duplicati.server as srv
        srv._session_token = "cached_token"
        resp = await srv._request("GET", "/api/v1/serverstate")

    assert resp.status_code == 200
    mock_client.request.assert_called_once_with(
        "GET",
        "http://localhost:8200/api/v1/serverstate",
        headers={"Authorization": "Bearer cached_token"},
    )


async def test_request_refreshes_token_on_401(monkeypatch):
    """_request() calls _login() once on 401, then retries with new token."""
    monkeypatch.setenv("DUPLICATI_HOST", "http://localhost:8200")

    resp_401 = MagicMock()
    resp_401.status_code = 401

    resp_200 = MagicMock()
    resp_200.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(side_effect=[resp_401, resp_200])

    import mcp_duplicati.server as srv
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    srv._session_token = "stale_token"

    async def fake_login():
        srv._session_token = "fresh_token"
        return "fresh_token"

    with patch("mcp_duplicati.server.httpx.AsyncClient", return_value=mock_client):
        with patch.object(srv, "_login", side_effect=fake_login):
            resp = await srv._request("GET", "/api/v1/serverstate")

    assert resp.status_code == 200
    assert mock_client.request.call_count == 2
    # Second call used the fresh token
    second_call_kwargs = mock_client.request.call_args_list[1]
    assert second_call_kwargs[1]["headers"] == {"Authorization": "Bearer fresh_token"}


# ---------------------------------------------------------------------------
# Tool tests — helpers
# ---------------------------------------------------------------------------

def make_response(status: int, data) -> httpx.Response:
    """Build a real httpx.Response with JSON body (no live HTTP needed)."""
    import json
    # Create a mock request to avoid "request instance has not been set" error
    from unittest.mock import MagicMock
    mock_req = MagicMock()
    resp = httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json"})
    resp._request = mock_req
    return resp


# ---------------------------------------------------------------------------
# server_info
# ---------------------------------------------------------------------------

async def test_server_info_success(monkeypatch):
    payload = {"ServerVersion": "2.0.8.1", "ProgramState": "Running", "Started": "2024-01-01T00:00:00"}

    async def fake_request(method, path, **kw):
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.server_info()

    assert result["result"]["ServerVersion"] == "2.0.8.1"
    assert result["result"]["ProgramState"] == "Running"


async def test_server_info_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.server_info()

    assert "error" in result
    assert result["tool"] == "server_info"


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------

async def test_list_backups_success(monkeypatch):
    payload = [
        {
            "Backup": {"ID": "1", "Name": "Home Documents", "TargetURL": "file:///backup/home"},
            "Schedule": {"NextTime": "2024-01-02T02:00:00Z"},
            "DisplayNames": {},
        }
    ]

    async def fake_request(method, path, **kw):
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_backups()

    assert isinstance(result["result"], list)
    assert result["result"][0]["Backup"]["Name"] == "Home Documents"


async def test_list_backups_empty(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(200, [])

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_backups()

    assert result["result"] == []


async def test_list_backups_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.TimeoutException("Timeout")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_backups()

    assert "error" in result
    assert result["tool"] == "list_backups"


# ---------------------------------------------------------------------------
# backup_status
# ---------------------------------------------------------------------------

async def test_backup_status_success(monkeypatch):
    payload = {
        "Backup": {
            "ID": "1",
            "Name": "Home Documents",
            "Metadata": {
                "LastBackupDate": "2024-01-01T02:00:00Z",
                "LastBackupResult": "Success",
                "LastBackupDuration": "00:05:30",
                "SourceFilesCount": "1500",
                "SourceSizeString": "5.00 GB",
                "BackupSizeString": "2.10 GB",
            },
        },
        "Schedule": {"Repeat": "1D", "Time": "2024-01-02T02:00:00Z"},
    }

    async def fake_request(method, path, **kw):
        assert path == "/api/v1/backup/1"
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.backup_status("1")

    assert result["result"]["backup_id"] == "1"
    assert result["result"]["name"] == "Home Documents"
    assert result["result"]["last_result"] == "Success"
    assert result["result"]["repeat"] == "1D"
    assert result["result"]["next_run"] == "2024-01-02T02:00:00Z"


async def test_backup_status_not_found(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(404, {"Error": "Not found"})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.backup_status("999")

    assert "error" in result
    assert result["tool"] == "backup_status"


async def test_backup_status_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.backup_status("1")

    assert "error" in result
    assert result["tool"] == "backup_status"


# ---------------------------------------------------------------------------
# run_backup
# ---------------------------------------------------------------------------

async def test_run_backup_success(monkeypatch):
    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/api/v1/backup/1/run"
        return make_response(200, {})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.run_backup("1")

    assert result["result"]["triggered"] is True
    assert result["result"]["backup_id"] == "1"


async def test_run_backup_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.TimeoutException("Timeout")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.run_backup("1")

    assert "error" in result
    assert result["tool"] == "run_backup"


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------

async def test_progress_active(monkeypatch):
    payload = {
        "Phase": "Backup_ProcessingFiles",
        "OverallProgress": 0.45,
        "ProcessedFileCount": 1200,
        "TotalFileCount": 2680,
    }

    async def fake_request(method, path, **kw):
        assert path == "/api/v1/progressstate"
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.progress()

    assert result["result"]["Phase"] == "Backup_ProcessingFiles"
    assert result["result"]["OverallProgress"] == 0.45


async def test_progress_idle(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(200, {"Phase": "Idle"})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.progress()

    assert result["result"]["Phase"] == "Idle"


async def test_progress_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.progress()

    assert "error" in result
    assert result["tool"] == "progress"


# ---------------------------------------------------------------------------
# list_versions
# ---------------------------------------------------------------------------

async def test_list_versions_success(monkeypatch):
    payload = [
        {"Version": 0, "Time": "2024-01-01T02:00:00Z", "FileCount": 1500},
        {"Version": 1, "Time": "2024-01-02T02:00:00Z", "FileCount": 1502},
    ]

    async def fake_request(method, path, **kw):
        assert path == "/api/v1/backup/1/filesets"
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_versions("1")

    assert isinstance(result["result"], list)
    assert len(result["result"]) == 2
    assert result["result"][0]["Version"] == 0


async def test_list_versions_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_versions("1")

    assert "error" in result
    assert result["tool"] == "list_versions"


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------

async def test_pause_indefinite(monkeypatch):
    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/api/v1/serverstate/pause"
        assert kw.get("params", {}) == {}
        return make_response(200, {})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.pause()

    assert result["result"]["paused"] is True
    assert result["result"]["duration"] is None


async def test_pause_with_duration(monkeypatch):
    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/api/v1/serverstate/pause/5m"
        return make_response(200, {})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.pause(duration=300)

    assert result["result"]["paused"] is True
    assert result["result"]["duration"] == 300


async def test_pause_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.pause()

    assert "error" in result
    assert result["tool"] == "pause"


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

async def test_resume_success(monkeypatch):
    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/api/v1/serverstate/resume"
        return make_response(200, {})

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.resume()

    assert result["result"]["resumed"] is True


async def test_resume_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.resume()

    assert "error" in result
    assert result["tool"] == "resume"


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------

async def test_get_logs_global(monkeypatch):
    """Without backup_id, uses /api/v1/logdata/log."""
    payload = [
        {"When": "2024-01-01T02:05:00Z", "Type": "Information", "Message": "Backup completed"},
        {"When": "2024-01-01T02:00:00Z", "Type": "Information", "Message": "Backup started"},
    ]

    async def fake_request(method, path, **kw):
        assert path == "/api/v1/logdata/log"
        assert kw.get("params", {}).get("pagesize") == 20
        return make_response(200, payload)

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_logs()

    assert isinstance(result["result"], list)
    assert len(result["result"]) == 2
    assert result["result"][0]["Type"] == "Information"


async def test_get_logs_for_backup(monkeypatch):
    """With backup_id, uses /api/v1/backup/{id}/log."""
    async def fake_request(method, path, **kw):
        assert path == "/api/v1/backup/1/log"
        return make_response(200, [{"When": "2024-01-01T02:05:00Z", "Message": "Done"}])

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_logs(backup_id="1")

    assert isinstance(result["result"], list)


async def test_get_logs_custom_page_size(monkeypatch):
    async def fake_request(method, path, **kw):
        assert kw.get("params", {}).get("pagesize") == 5
        return make_response(200, [])

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_logs(page_size=5)

    assert result["result"] == []


async def test_get_logs_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_duplicati.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_logs()

    assert "error" in result
    assert result["tool"] == "get_logs"
