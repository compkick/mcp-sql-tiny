# MSSQL Tiny MCP Server

MSSQL Tiny MCP Server is a small, local-first development utility, not a packaged database product. The tool helps developers use Codex and other MCP-aware agents (i.e. Claude) connect to Microsoft SQL Server and Azure SQL with a practical setup and read-only guardrails.

This tool uses Microsoft's official `mssql-python` driver, loads a repo-local `.env`, and keeps the surface area small. It offers health checks, connection warmup, schema and table inspection, and guarded read-only query execution.

This project is intentionally narrow. It is for local development, private internal tooling, database analysis and planning, and safe data inspection workflows. It is not an admin console, migration runner, or write-capable SQL automation layer.

## Features

* Local-first MCP server over **stdio**
* Microsoft SQL Server and Azure SQL connectivity via **mssql-python**
* Authentication handled through a standard **SQL connection string** in repo-local `.env`
* Safe, **read-only SQL execution** with read-only prefix checks, blocked keywords, and multi-statement rejection
* Database, schema, table, view, key, identity, computed-column, default, and foreign-key discovery helpers
* Sensitive column name hints for names like `password`, `token`, `secret`, `key`, `salt`, and `hash`
* Configurable query timeout and row caps
* `healthcheck()` and `warmup()` helpers for startup diagnostics
* Lightweight smoke test and unit tests
* Safer than generic SQL MCP servers
* Easy analysis and inspection of SQL databases
* Matches execution-focused database and DevOps workflows
* Can be extended later with export helpers and workflow-specific Codex skills

## Exclusions

* Not a full SQL Server admin tool
* Not a replacement for database permissions
* Does not run writes, DDL, or stored procedures
* Does not manage login provisioning or Azure setup

## Requirements

* **Python 3.10+**
* Windows, macOS, or Linux
* Access to Microsoft SQL Server, Azure SQL Database, or Azure SQL Managed Instance
* Codex or another MCP-aware coding agent

## Quickstart

Clone repo from Github, then from the repo root in PowerShell:

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

Create your .env from the .env.example, then add the required values and follow the authentication examples below. The server will load your repo-local `.env` file automatically via `python-dotenv`.

Required:

| Variable                | Description                                                          |
| ----------------------- | -------------------------------------------------------------------- |
| `SQL_CONNECTION_STRING` | Standard SQL Server / Azure SQL connection string for `mssql-python` |

Optional limits:

| Variable                      | Purpose                                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------------------- |
| `MSSQL_QUERY_TIMEOUT_SECONDS` | Query timeout passed to `mssql-python` (default: `30`; `0` means driver default/no timeout) |
| `MSSQL_DEFAULT_MAX_ROWS`      | Default row cap (default: `1000`)                                                           |
| `MSSQL_HARD_MAX_ROWS`         | Absolute row cap (default: `5000`)                                                          |

## Codex MCP configuration

Add this to your Codex config file:

```toml
[mcp_servers.mssql_tiny]
command = 'PROJECT_DIR\mcp-sql-tiny\.venv\Scripts\python.exe'
args = ['PROJECT_DIR\mcp-sql-tiny\mssql_mcp_server.py']
```

Replace `PROJECT_DIR` with the full path to the parent folder where you cloned the repo.

The database connection values stay in this repo's local `.env`, not in Codex `config.toml`.

## Running the server manually

```powershell
python mssql_mcp_server.py
```

You should see no output because MCP uses stdio.

## Authentication examples

For Codex MCP, prefer SQL authentication or another non-interactive credential path. Windows-integrated auth depends on the Windows identity of the process that launches the MCP server, so it can work in your own terminal while failing when Codex starts the server under a sandboxed or service account.

Local SQL Server with SQL login and no initial catalog (recommended for Codex MCP):

```env
SQL_CONNECTION_STRING=Server=localhost\SQLEXPRESS;UID=codex_mcp;PWD=your-password;Encrypt=yes;TrustServerCertificate=yes;
```

Local SQL Server with Windows-integrated auth and no initial catalog (interactive local terminals only):

```env
SQL_CONNECTION_STRING=Server=localhost\SQLEXPRESS;Trusted_Connection=Yes;Encrypt=yes;TrustServerCertificate=yes;
```

Azure SQL from a developer machine already signed in with Azure CLI:

```env
SQL_CONNECTION_STRING=Server=your-server.database.windows.net;Database=your-db;Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;
```

Azure SQL with managed identity:

```env
SQL_CONNECTION_STRING=Server=your-server.database.windows.net;Database=your-db;Authentication=ActiveDirectoryMSI;Encrypt=yes;TrustServerCertificate=no;
```

Add `Connection Timeout=15;` or a similar driver-supported connection timeout to the connection string if startup/login should fail fast. Query execution timeout is controlled separately by `MSSQL_QUERY_TIMEOUT_SECONDS`.

## MCP tools exposed

| Tool                                      | Description                                                                                                                                                                      |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ping()`                                  | Sanity check                                                                                                                                                                     |
| `healthcheck(probe?)`                     | Env readiness and optional live probe                                                                                                                                            |
| `verify_environment(probe?)`              | Python, package, dotenv, row-limit, and optional SQL connectivity diagnostics                                                                                                    |
| `warmup()`                                | Trigger a background connection warmup                                                                                                                                           |
| `discover_context()`                      | Return server/database/login context                                                                                                                                             |
| `list_databases()`                        | List databases visible to the current login                                                                                                                                      |
| `list_schemas(database?)`                 | List schemas in the current or specified database                                                                                                                                |
| `list_tables(schema?, database?)`         | List base tables in the current or specified database                                                                                                                            |
| `list_views(schema?, database?)`          | List views in the current or specified database                                                                                                                                  |
| `describe_table(full_name)`               | Describe a table or view with `schema.table` or `database.schema.table`, including keys, defaults, identity/computed flags, foreign keys, row estimate, and sensitive-name hints |
| `run_query_preview(sql_text, max_rows?)`  | Read-only query preview                                                                                                                                                          |
| `run_query_readonly(sql_text, max_rows?)` | Read-only SQL with guardrails                                                                                                                                                    |

## Read-only enforcement

The arbitrary SQL tools are intentionally strict:

* Only allow statements that begin with `SELECT` or `WITH`
* Reject multiple SQL statements in one request
* Block obvious write/admin keywords such as `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `EXEC`, `DBCC`, `BACKUP`, `RESTORE`, and `INTO`

This is a safety layer, not a substitute for least-privilege database credentials.

The SQL guardrail is intentionally simple and regex/token based. It catches obvious write/admin attempts, but it is not a complete T-SQL parser and should not be treated as a hard security boundary. Use a SQL login with only the permissions you are comfortable exposing through MCP.

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

Opt-in live integration tests are separate from normal unit tests. To run them,
set `RUN_LIVE_TESTS = True` at the top of `tests\test_live_integration.py`:

```powershell
pytest -m live
```

Live tests use the repo-local `.env` connection string and should only run against a safe development SQL Server.

## Notes

* Local SQL Server and Azure SQL Managed Instance connections do not need a `Database=` value if the login has a valid default database.
* If `Database=` is omitted, SQL Server connects to the login's default database, similar to connecting in SSMS without choosing a database.
* If `Database=` is provided, it is only the startup/default database, not the access boundary.
* Metadata helpers can inspect any database the login can access by using `list_databases()`, `database` arguments, or `database.schema.table` names.
* Azure SQL Database is more database-scoped than SQL Server or Azure SQL Managed Instance; if cross-database three-part names are not supported by the endpoint, connect directly to the target database.
* For local development, `TrustServerCertificate=yes` is often necessary with self-signed certs.
* For Azure SQL, prefer Microsoft Entra authentication where possible.

## Security model

This server is designed to reduce risk, not eliminate it completely.

* `run_query_readonly()` and `run_query_preview()` only accept statements that begin with read-only keywords and reject a blocklist of write and admin keywords.
* The arbitrary SQL tools reject multiple statements in one request.
* Metadata tools only accept simple identifier inputs, not arbitrary SQL fragments.
* `describe_table()` and query results include name-based risk hints for sensitive-looking columns, but these are warnings only and do not redact data.
* Row caps are enforced on fetch size, with configurable default and hard limits.
* Query timeout is enforced through `mssql-python`, while connection/login timeout should be set in the connection string.
* This is still a direct SQL Server or Azure SQL client using your credentials. It does not sandbox the database itself, replace SQL Server permissions, redact sensitive data, or provide complete SQL parsing guarantees.

Use least-privilege credentials if you plan to share this setup.

Example local SQL Server read-only login:

```sql
USE [master];
CREATE LOGIN [mcp_reader] WITH PASSWORD = 'replace-with-a-strong-password';

USE [YourDatabase];
CREATE USER [mcp_reader] FOR LOGIN [mcp_reader];
ALTER ROLE [db_datareader] ADD MEMBER [mcp_reader];
```

Repeat the database-level `CREATE USER` and `ALTER ROLE` statements for each database this MCP login should inspect.

## Changelog

This project uses semantic versioning. `0.1.1` is the current project version.

### 0.1.1

Verification and release-readiness cleanup.

Included in this release:

* General `tools\verify_venv.py` helper for Python virtual-environment sanity checks across projects
* `.env` verification through `python-dotenv`, including import, load, and key-count checks
* Added `list_databases()` for server-level database discovery
* Added `list_views()` and expanded `list_schemas()` / `list_tables()` with optional database targeting
* Expanded `describe_table()` to support `database.schema.table` names plus keys, identity columns, computed columns, defaults, foreign keys, row estimates, and sensitive-name hints
* Added configurable query timeout support through `MSSQL_QUERY_TIMEOUT_SECONDS`
* Added sensitive-column risk hints to query and metadata results
* README release-readiness status and updated live-test instructions

### 0.1.0

First project release of the MSSQL Tiny MCP Server.

Included in this release:

* Core MCP tools for `ping`, `healthcheck`, `verify_environment`, `discover_context`, `list_databases`, `list_schemas`, `list_tables`, `list_views`, `describe_table`, `run_query_preview`, and `run_query_readonly`
* Read-only SQL enforcement with query timeout, row caps, and multi-statement rejection
* Rich `describe_table()` metadata for keys, identity columns, computed columns, defaults, foreign keys, row estimates, and sensitive-name hints
* Opt-in live integration tests through the `RUN_LIVE_TESTS` switch and `live` pytest marker
* `warmup()` and `healthcheck()` helpers for startup diagnostics and connection testing
* Local `.env` loading via `python-dotenv` and a matching `.env.example`
* Smoke test script via `python test_server.py`
* Pytest coverage for local safety and query-construction logic
* MIT licensing and versioned dependency ranges in `requirements.txt`
* README setup guidance for local SQL Server and Azure SQL connection-string-based authentication

## Future roadmap

* `export_query_jsonl(sql, path)`
* `export_query_parquet(sql, path)`
* Stored procedure introspection helpers that remain read-only
* Deeper view definition and dependency introspection
* Python package artifacts (`pyproject.toml`, wheel, source distribution)
* Structured logging
* Containerization (Docker)
* Optional connection profiles for local SQL Server, Azure SQL Database, and managed identity scenarios
* Codex skills for common MSSQL workflows such as schema exploration, query shaping, performance triage, and safe troubleshooting playbooks
* Add a driver or config switch, so the user can set up different database connections and db types, such as MSSQL, MySQL, MariaDB, etc
* Turn into published and "installable" skill or MCP server?

## License / usage

Released under the MIT License. See [LICENSE](/LICENSE).

## Support / Questions

View project page here - https://computerkick.com/mcp-server-for-mssql
