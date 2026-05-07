"""Tests for mcp-duplicati tools. All HTTP calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Auth layer tests
# ---------------------------------------------------------------------------

async def test_login_caches_session_token(monkeypatch):
    """_login() POSTs credentials and caches the returned session-auth cookie."""
    monkeypatch.setenv("DUPLICATI_PASSWORD", "testpass")
    monkeypatch.setenv("DUPLICATI_HOST", "http://localhost:8200")

    mock_resp = MagicMock()
    mock_resp.cookies = {"session-auth": "tok123"}
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
    assert srv._session_token == "tok123"
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
    """_request() sends session-auth cookie from module-level cache."""
    monkeypatch.setenv("DUPLICATI_HOST", "http://localhost:8200")

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
        cookies={"session-auth": "cached_token"},
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
    assert second_call_kwargs[1]["cookies"] == {"session-auth": "fresh_token"}
