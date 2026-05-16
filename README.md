# MSSQL Tiny MCP Server

MCP MSSQL Tiny is a small, local-first MCP server that helps Codex and other MCP-aware tools connect to Microsoft SQL Server and Azure SQL with a practical setup and explicit read-only guardrails. The tool uses Microsoft's official `mssql-python` driver, loads a repo-local `.env`, and keeps the surface area small: health checks, connection warmup, schema and table inspection, and guarded read-only query execution.

This project is intentionally narrow. It is for local development, private internal tooling, and safe data inspection workflows. It is not an admin console, migration runner, or write-capable SQL automation layer.

## Features

* Local-first MCP server over **stdio**
* Microsoft SQL Server and Azure SQL connectivity via **mssql-python**
* Authentication handled through a standard **SQL connection string** in repo-local `.env`
* Safe, **read-only SQL execution** with allowed-prefix checks, blocked keywords, and multi-statement rejection
* Schema and table discovery helpers for the connected database
* Configurable row caps and optional schema/database allowlists
* `healthcheck()` and `warmup()` helpers for startup diagnostics
* Lightweight smoke test and unit tests

## Exclusions

* Not a full SQL Server admin tool
* Not a replacement for database permissions
* Does not run writes, DDL, or stored procedures
* Does not manage login provisioning or Azure setup for you

## Requirements

* **Python 3.10+**
* Windows, macOS, or Linux
* Access to Microsoft SQL Server, Azure SQL Database, or Azure SQL Managed Instance
* Codex or another MCP-aware client

## Quickstart

From the repo root in PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python test_server.py --no-probe
```

If you want the test suite too:

```powershell
pip install -r requirements-dev.txt
pytest
```

## Environment variables

The server loads a repo-local `.env` file automatically via `python-dotenv`.

Required:

| Variable | Description |
| --- | --- |
| `SQL_CONNECTION_STRING` | Standard SQL Server / Azure SQL connection string for `mssql-python` |

Optional guardrails:

| Variable | Purpose |
| --- | --- |
| `MSSQL_ALLOWED_DATABASES` | Comma-separated allowlist for metadata requests |
| `MSSQL_ALLOWED_SCHEMAS` | Comma-separated allowlist for metadata requests |
| `MSSQL_DEFAULT_MAX_ROWS` | Default row cap (default: `1000`) |
| `MSSQL_HARD_MAX_ROWS` | Absolute row cap (default: `5000`) |

## Authentication examples

Local SQL Server with Windows-integrated auth:

```env
SQL_CONNECTION_STRING=Server=localhost\SQLEXPRESS;Database=master;Trusted_Connection=Yes;Encrypt=yes;TrustServerCertificate=yes;
```

Local SQL Server with SQL login:

```env
SQL_CONNECTION_STRING=Server=localhost\SQLEXPRESS;Database=master;UID=sa;PWD=your-password;Encrypt=yes;TrustServerCertificate=yes;
```

Azure SQL from a developer machine already signed in with Azure CLI:

```env
SQL_CONNECTION_STRING=Server=your-server.database.windows.net;Database=your-db;Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;
```

Azure SQL with managed identity:

```env
SQL_CONNECTION_STRING=Server=your-server.database.windows.net;Database=your-db;Authentication=ActiveDirectoryMSI;Encrypt=yes;TrustServerCertificate=no;
```

## MCP tools exposed

| Tool | Description |
| --- | --- |
| `ping()` | Sanity check |
| `healthcheck(probe?)` | Env readiness and optional live probe |
| `warmup()` | Trigger a background connection warmup |
| `discover_context()` | Return server/database/login context |
| `list_schemas()` | List schemas in the current database |
| `list_tables(schema?)` | List base tables and views in the current database |
| `describe_table(full_name)` | Describe a table or view with `schema.table` or `database.schema.table` |
| `run_query_preview(sql_text, max_rows?)` | Read-only query preview |
| `run_query_readonly(sql_text, max_rows?)` | Read-only SQL with guardrails |

## Read-only enforcement

The arbitrary SQL tools are intentionally strict:

* Only allow statements that begin with `SELECT` or `WITH`
* Reject multiple SQL statements in one request
* Block obvious write/admin keywords such as `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `EXEC`, `DBCC`, `BACKUP`, `RESTORE`, and `INTO`

This is a safety layer, not a substitute for least-privilege database credentials.

## Smoke test

Run a quick Python-level smoke test from the repo root:

```powershell
python test_server.py
python test_server.py --no-probe
python test_server.py --discover-context
```

`--no-probe` checks dotenv/config loading without opening a live SQL connection.

## Pytest

Run the unit tests from the repo root:

```powershell
pytest
pytest -q
pytest tests\test_mssql_mcp_server.py
```

These tests cover local guardrails and SQL construction without requiring a live database.

## Running the server manually

```powershell
python mssql_mcp_server.py
```

You should see no output because MCP uses stdio.

## Codex MCP configuration

Add this to your Codex config file:

```toml
[mcp_servers.mssql_tiny]
command = 'PROJECT_DIR\mcp-sql-tiny\.venv\Scripts\python.exe'
args = ['PROJECT_DIR\mcp-sql-tiny\mssql_mcp_server.py']
```

Replace `PROJECT_DIR` with the full path to the parent folder where this repo lives.

The database connection values stay in this repo's local `.env`, not in Codex `config.toml`.

## Notes

* The server only exposes metadata for the currently connected database.
* `describe_table()` accepts `database.schema.table`, but cross-database metadata lookup is intentionally blocked unless the requested database matches the connected database.
* For local development, `TrustServerCertificate=yes` is often necessary with self-signed certs.
* For Azure SQL, prefer Microsoft Entra authentication where possible.

## Security model

This server is designed to reduce risk, not eliminate it completely.

* `run_query_readonly()` and `run_query_preview()` only accept statements that begin with read-only keywords and reject a blocklist of write and admin keywords.
* The arbitrary SQL tools reject multiple statements in one request.
* `list_tables()` and `describe_table()` only accept simple identifier inputs, not arbitrary SQL fragments.
* Row caps are enforced on fetch size, with configurable default and hard limits.
* Optional database and schema allowlists can narrow what the tools are allowed to inspect.
* This is still a direct SQL Server or Azure SQL client using your credentials. It does not sandbox the database itself, replace SQL Server permissions, or provide complete SQL parsing guarantees.

Use least-privilege credentials and keep the allowed databases and schemas narrow if you plan to share this setup.

## Why this exists (design rationale)

* Safer than generic SQL MCP servers
* Smaller blast radius
* Predictable outputs
* Easy to reason about
* Matches execution-focused database and DevOps workflows
* Can be extended later with export helpers and workflow-specific Codex skills

## Changelog

This project uses semantic versioning. `0.1.0` is the current project version.

### 0.1.0

First project release of the MSSQL Tiny MCP Server.

Included in this release:

* Core MCP tools for `ping`, `discover_context`, `list_schemas`, `list_tables`, `describe_table`, `run_query_preview`, and `run_query_readonly`
* Read-only SQL enforcement with row caps, multi-statement rejection, and optional database and schema allowlists
* `warmup()` and `healthcheck()` helpers for startup diagnostics and connection testing
* Local `.env` loading via `python-dotenv` and a matching `.env.example`
* Smoke test script via `python test_server.py`
* Pytest coverage for local safety and query-construction logic
* MIT licensing and versioned dependency ranges in `requirements.txt`
* README setup guidance for local SQL Server and Azure SQL connection-string-based authentication

## Future roadmap

* `export_query_jsonl(sql, path)`
* `export_query_parquet(sql, path)`
* General `verify_venv` helper for Python environment sanity checks across projects
* Optional cross-database metadata support with explicit allowlists
* Stored procedure and view introspection helpers that remain read-only
* Python package artifacts (`pyproject.toml`, wheel, source distribution)
* Structured logging
* Containerization (Docker)
* Optional connection profiles for local SQL Server, Azure SQL Database, and managed identity scenarios
* Codex skills for common MSSQL workflows such as schema exploration, query shaping, performance triage, and safe troubleshooting playbooks

## License / usage

Released under the MIT License. See [LICENSE](/LICENSE).
