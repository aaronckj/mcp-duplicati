# mcp-duplicati

MCP server for [Duplicati](https://www.duplicati.com/) backup management. Exposes
67 tools for inspecting, configuring, scheduling, running, restoring, and
maintaining Duplicati backup jobs over the Duplicati web API.

> **Heads up:** this server includes **destructive** tools that can delete
> backup jobs, wipe local databases, purge file history, overwrite files on
> restore, and reclaim storage. See [Destructive tools](#destructive-tools)
> before pointing an agent at a production Duplicati instance. Requires
> Duplicati **2.0.8+** (JWT auth).

## Quick Start

**With uvx (recommended):**
```bash
DUPLICATI_PASSWORD=yourpassword uvx mcp-duplicati
```

**Add to Claude Code:**
```bash
claude mcp add duplicati -- uvx mcp-duplicati
```

Then set the env vars (at minimum `DUPLICATI_PASSWORD`, and `DUPLICATI_HOST`
if Duplicati is not on `localhost:8200`) in your Claude Code MCP settings.

**mcp.json snippet** (e.g. `~/.config/claude/mcp.json` or any MCP client config):
```json
{
  "mcpServers": {
    "duplicati": {
      "command": "uvx",
      "args": ["mcp-duplicati"],
      "env": {
        "DUPLICATI_PASSWORD": "yourpassword",
        "DUPLICATI_HOST": "http://localhost:8200"
      }
    }
  }
}
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DUPLICATI_PASSWORD` | Yes | — | Duplicati web UI password |
| `DUPLICATI_HOST` | No | `http://localhost:8200` | Duplicati host URL |
| `DUPLICATI_TIMEOUT` | No | `30` | HTTP timeout in seconds |

## Docker

> **Note:** a prebuilt image is **not yet published publicly.** The
> `ghcr.io/aaronckj/mcp-duplicati` package is currently private, so the
> `docker run` below will fail with a 401 for anyone but the maintainer. Use
> the `uvx` install above, or build the image yourself from this repo:

```bash
docker build -t mcp-duplicati .
docker run -i \
  -e DUPLICATI_PASSWORD=yourpassword \
  -e DUPLICATI_HOST=http://duplicati.example.com:8200 \
  mcp-duplicati
```

This is a **stdio** MCP server — it speaks MCP over stdin/stdout and exposes no
HTTP port, so there is no network health endpoint. For a liveness probe, call
the `health_check` MCP tool.

## Tools

67 tools, grouped by area. Tools that modify or destroy data are flagged
**[DESTRUCTIVE]** — see [the section below](#destructive-tools).

### Server & status
| Tool | Description |
|------|-------------|
| `server_info` | Installed version, OS type, machine ID |
| `get_server_state` | Runtime scheduler/task state (paused, active task, version) |
| `get_server_settings` | All server-level settings |
| `get_server_setting` | A single server-level setting by key |
| `update_server_settings` | Change one server-level setting |
| `get_ui_settings` | Web UI preferences |
| `update_ui_settings` | Update web UI preferences |
| `get_backup_defaults` | Default settings applied to new jobs |
| `check_updates` | Check for a newer Duplicati release |
| `get_changelog` | Release changelog / version history |
| `health_check` | Liveness probe for container monitoring |

### Backup jobs
| Tool | Description |
|------|-------------|
| `list_backups` | All configured backup jobs |
| `backup_status` | Last-run statistics for a job |
| `get_backup` | Full job configuration |
| `get_backup_statistics` | Detailed file/size/duration statistics |
| `get_backup_report` | Detailed report of the most recent run |
| `get_backup_commandline` | Equivalent CLI invocation for a job |
| `create_backup` | Create a new backup job |
| `update_backup` | Update an existing job |
| `delete_backup` | **[DESTRUCTIVE]** Delete a job's configuration |
| `import_backup_config` | Create a job from exported JSON |
| `export_backup_config` | Export a job's config as JSON |
| `is_backup_overdue` | Check if a job missed its expected interval |

### Running, stopping & progress
| Tool | Description |
|------|-------------|
| `run_backup` | Run a job immediately |
| `stop_backup` | **[DESTRUCTIVE]** Gracefully stop a running job |
| `abort_backup` | **[DESTRUCTIVE]** Abort a running job immediately |
| `is_backup_active` | Check if a job is running/queued |
| `progress` | Progress of any active backup/restore |
| `poll_operations` | Long-poll server events since an event ID |

### Restore & verify
| Tool | Description |
|------|-------------|
| `list_versions` | Restore points (filesets) for a job |
| `search_backup_files` | Search files within a backup version |
| `restore_files` | **[DESTRUCTIVE]** Restore files to a local dir (overwrites) |
| `verify_backup` | Verify backup integrity against destination |

### Maintenance & repair
| Tool | Description |
|------|-------------|
| `repair_backup` | **[DESTRUCTIVE]** Rebuild local DB from destination |
| `compact_backup` | **[DESTRUCTIVE]** Remove unused data blocks at destination |
| `purge_deleted_files` | **[DESTRUCTIVE]** Purge history for deleted source files |
| `delete_local_database` | **[DESTRUCTIVE]** Delete & rebuild a job's local DB |
| `vacuum_database` | **[DESTRUCTIVE]** SQLite VACUUM on a job's local DB |

### Scheduling
| Tool | Description |
|------|-------------|
| `get_backup_schedule` | Get a job's schedule |
| `set_backup_schedule` | Set/update a job's schedule |
| `delete_backup_schedule` | **[DESTRUCTIVE]** Remove a job's schedule (on-demand only) |

### Filters & sources
| Tool | Description |
|------|-------------|
| `list_backup_filters` | List a job's include/exclude filters |
| `add_backup_filter` | Add one filter without replacing others |
| `remove_backup_filter` | **[DESTRUCTIVE]** Remove a specific filter |
| `list_filters` | Built-in filter groups |
| `move_backup_source` | Update a single source path in a job |
| `get_backup_retention` | Get a job's retention policy |
| `set_backup_retention` | **[DESTRUCTIVE]** Replace a job's retention policy |

### Tasks
| Tool | Description |
|------|-------------|
| `list_tasks` | Queued/running tasks |
| `get_task` | Details of a task by ID |
| `stop_task` | **[DESTRUCTIVE]** Stop/dequeue a task by ID |
| `abort_task` | **[DESTRUCTIVE]** Abort a running task by ID |

### Logs & notifications
| Tool | Description |
|------|-------------|
| `get_logs` | Recent log entries (optionally per job) |
| `clear_logs` | **[DESTRUCTIVE]** Clear log entries |
| `list_notifications` | Pending notifications/alerts |
| `dismiss_notification` | **[DESTRUCTIVE]** Dismiss a notification by ID |
| `dismiss_all_notifications` | **[DESTRUCTIVE]** Dismiss all notifications |
| `send_test_notification` | Send a test notification |

### Destinations & modules
| Tool | Description |
|------|-------------|
| `test_connection` | Test connectivity to a destination URL |
| `list_sources` | Browse files/folders at a destination |
| `create_remote_folder` | Create the remote folder/bucket |
| `list_remote_volumes` | List dblock/dindex/dlist files at the destination |
| `list_installed_backends` | Installed storage backend modules |
| `list_encryption_modules` | Installed encryption modules |
| `list_compression_modules` | Installed compression modules |

## Destructive tools

The following tools **delete, overwrite, or otherwise mutate data** and cannot
necessarily be undone. Review carefully before granting an agent unattended
access:

- `delete_backup` — deletes a job configuration.
- `delete_backup_schedule` — removes a job's automatic schedule.
- `delete_local_database` — deletes a job's local SQLite DB (forces a rebuild).
- `vacuum_database` — runs SQLite VACUUM on a job's local DB.
- `purge_deleted_files` — purges backup history for files removed at source.
- `compact_backup` — removes unused data blocks at the destination.
- `repair_backup` — rebuilds the local DB from the destination.
- `restore_files` — writes files to a local path, overwriting existing files.
- `stop_backup` / `abort_backup` / `stop_task` / `abort_task` — interrupt
  running operations.
- `remove_backup_filter` / `set_backup_retention` — change what data is kept.
- `clear_logs` / `dismiss_notification` / `dismiss_all_notifications` — discard
  log/notification records.

`delete_backup` and `delete_backup_schedule` change only Duplicati's
configuration; they do **not** delete backup data at the destination.

## Development

```bash
git clone https://github.com/aaronckj/mcp-duplicati
cd mcp-duplicati
uv sync --extra dev
uv run pytest -v
```
