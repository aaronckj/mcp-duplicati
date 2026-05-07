# mcp-duplicati

MCP server for [Duplicati](https://www.duplicati.com/) backup management. Exposes 9 tools for checking status, running backups, viewing logs, and controlling the scheduler.

## Quick Start

**With uvx (recommended):**
```bash
DUPLICATI_PASSWORD=yourpassword uvx mcp-duplicati
```

**With Docker:**
```bash
docker run -i \
  -e DUPLICATI_PASSWORD=yourpassword \
  -e DUPLICATI_HOST=http://10.0.0.30:8200 \
  ghcr.io/aaronckj/mcp-duplicati:latest
```

**Add to Claude Code:**
```bash
claude mcp add duplicati -- uvx mcp-duplicati
```

Then set env vars in Claude Code MCP settings (`DUPLICATI_PASSWORD`, `DUPLICATI_HOST`).

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DUPLICATI_PASSWORD` | Yes | — | Duplicati web UI password |
| `DUPLICATI_HOST` | No | `http://localhost:8200` | Duplicati host URL |
| `DUPLICATI_TIMEOUT` | No | `30` | HTTP timeout in seconds |

## Tools

| Tool | Description |
|------|-------------|
| `server_info` | Server version and current state |
| `list_backups` | All configured backup jobs |
| `backup_status` | Detailed status of a specific job |
| `run_backup` | Trigger a job to run immediately |
| `progress` | Active backup/restore progress |
| `list_versions` | Available restore points for a job |
| `pause` | Pause the scheduler (optional duration in seconds) |
| `resume` | Resume the scheduler |
| `get_logs` | Recent log entries (optionally filtered by job) |

## Development

```bash
git clone https://github.com/aaronckj/mcp-duplicati
cd mcp-duplicati
uv sync --extra dev
uv run pytest -v
```
