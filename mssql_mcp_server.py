"""
mssql_mcp_server.py
Tiny MCP server for Microsoft SQL Server and Azure SQL over stdio.

Primary configuration:
  SQL_CONNECTION_STRING   Standard SQL Server connection string for mssql-python

Optional guardrails:
  MSSQL_ALLOWED_DATABASES comma-separated database allowlist
  MSSQL_ALLOWED_SCHEMAS   comma-separated schema allowlist
  MSSQL_DEFAULT_MAX_ROWS  default row cap (int)
  MSSQL_HARD_MAX_ROWS     absolute row cap (int)
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import mssql_python
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ----------------------------
# Bootstrap
# ----------------------------

mcp = FastMCP("mssql-tiny")
PROJECT_DIR = Path(__file__).resolve().parent

load_dotenv(PROJECT_DIR / ".env")

# ----------------------------
# SQL guardrails
# ----------------------------

ALLOWED_PREFIXES = (
    "SELECT",
    "WITH",
)

BLOCKED_COMMANDS = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|"
    r"GRANT|REVOKE|DENY|"
    r"EXEC|EXECUTE|CALL|"
    r"BACKUP|RESTORE|DBCC|BULK|"
    r"USE|SET|"
    r"ATTACH|DETACH|KILL|"
    r"OPENROWSET|OPENDATASOURCE|OPENQUERY|"
    r"sp_configure|xp_|INTO"
    r")\b",
    re.IGNORECASE,
)

LINE_COMMENT = re.compile(r"--.*?$", re.MULTILINE)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONFIG_WARNINGS: list[str] = []

# ----------------------------
# SQL text helpers
# ----------------------------


def _strip_comments(sql_text: str) -> str:
    s = BLOCK_COMMENT.sub(" ", sql_text)
    s = LINE_COMMENT.sub(" ", s)
    return s


def _first_token(sql_text: str) -> str:
    s = _strip_comments(sql_text).strip()
    s = re.sub(r"^[\s(]+", "", s)
    match = re.match(r"([A-Za-z]+)", s)
    return (match.group(1) if match else "").upper()


def _contains_multiple_statements(sql_text: str) -> bool:
    stripped = _strip_comments(sql_text).strip()
    if not stripped:
        return False
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return ";" in stripped


def _validate_identifier(value: str, label: str) -> str:
    s = value.strip()
    if not s:
        raise ValueError(f"Blocked: {label} cannot be empty.")
    if not IDENTIFIER_RE.fullmatch(s):
        raise ValueError(
            f"Blocked: {label} must be a simple identifier containing only "
            "letters, numbers, and underscores, and cannot start with a number."
        )
    return s


def _validate_table_name(full_name: str) -> tuple[str | None, str, str]:
    parts = [p.strip() for p in full_name.split(".")]
    if len(parts) == 2 and all(parts):
        schema, table = parts
        return None, _validate_identifier(schema, "schema"), _validate_identifier(table, "table")
    if len(parts) == 3 and all(parts):
        database, schema, table = parts
        return (
            _validate_identifier(database, "database"),
            _validate_identifier(schema, "schema"),
            _validate_identifier(table, "table"),
        )
    raise ValueError("Blocked: full_name must be schema.table or database.schema.table.")


# ----------------------------
# Environment and configuration
# ----------------------------


def _load_allowlist_env() -> tuple[set[str], set[str]]:
    databases = set(filter(None, (os.getenv("MSSQL_ALLOWED_DATABASES", "")).split(",")))
    schemas = set(filter(None, (os.getenv("MSSQL_ALLOWED_SCHEMAS", "")).split(",")))
    databases = {d.strip() for d in databases if d.strip()}
    schemas = {s.strip() for s in schemas if s.strip()}
    return databases, schemas


def _load_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        CONFIG_WARNINGS.append(
            f"Invalid integer for {name}: {raw!r}. Falling back to default {default}."
        )
        return default
    if value < minimum:
        CONFIG_WARNINGS.append(
            f"{name} must be >= {minimum}. Falling back to default {default}."
        )
        return default
    return value


ALLOWED_DATABASES, ALLOWED_SCHEMAS = _load_allowlist_env()
DEFAULT_MAX_ROWS = _load_int_env("MSSQL_DEFAULT_MAX_ROWS", 1000)
HARD_MAX_ROWS = _load_int_env("MSSQL_HARD_MAX_ROWS", 5000)
if DEFAULT_MAX_ROWS > HARD_MAX_ROWS:
    CONFIG_WARNINGS.append(
        "MSSQL_DEFAULT_MAX_ROWS is greater than MSSQL_HARD_MAX_ROWS. "
        f"Clamping default row cap to {HARD_MAX_ROWS}."
    )
    DEFAULT_MAX_ROWS = HARD_MAX_ROWS

_WARMUP_LOCK = threading.Lock()
_WARMUP_INFLIGHT = False
_WARMUP_LAST_OK: int | None = None
_WARMUP_LAST_ERROR: str | None = None


# region connection and query helpers

def _env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"Missing env var: {name}. Set it in your shell or in {PROJECT_DIR / '.env'}."
        )
    return value


def _env_present(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


def _connect():
    connection_string = _env("SQL_CONNECTION_STRING")
    return mssql_python.connect(connection_string)


def _enforce_readonly(sql_text: str) -> None:
    if not sql_text or not sql_text.strip():
        raise ValueError("Empty SQL text.")

    token = _first_token(sql_text)
    if token not in ALLOWED_PREFIXES:
        raise ValueError(
            f"Blocked: only {', '.join(ALLOWED_PREFIXES)} statements are allowed. "
            f"Detected leading token: {token or '(none)'}"
        )

    if _contains_multiple_statements(sql_text):
        raise ValueError("Blocked: multiple SQL statements are not allowed.")

    if BLOCKED_COMMANDS.search(sql_text):
        raise ValueError("Blocked: query contains a non-read-only keyword.")


def _cap_rows(max_rows: int) -> int:
    try:
        value = int(max_rows)
    except Exception:
        value = DEFAULT_MAX_ROWS
    if value < 1:
        value = 1
    if value > HARD_MAX_ROWS:
        value = HARD_MAX_ROWS
    return value


def _fetch(cur, max_rows: int) -> dict[str, Any]:
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(max_rows) if cols else []
    return {
        "columns": cols,
        "rows": [list(row) for row in rows],
        "row_count": len(rows),
    }


def _description_columns(cur) -> list[str]:
    return [d[0] for d in (cur.description or [])]


def _row_to_mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    if row is None:
        raise RuntimeError("Query returned no rows.")
    if isinstance(row, dict):
        return row
    if hasattr(row, "_asdict"):
        return row._asdict()

    try:
        return {column: row[index] for index, column in enumerate(columns)}
    except Exception:
        pass

    try:
        values = list(row)
    except Exception as exc:
        raise RuntimeError(f"Unexpected row shape returned by query: {type(row)!r}") from exc

    return {column: values[index] for index, column in enumerate(columns)}


def _current_database(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT CAST(DB_NAME() AS nvarchar(128)) AS current_database")
        columns = _description_columns(cur)
        row = cur.fetchone()
    payload = _row_to_mapping(row, columns)
    if "current_database" not in payload:
        raise RuntimeError("Unable to determine current database from the SQL connection.")
    return str(payload["current_database"])


def _enforce_allowlists(schema: str, database: str | None = None) -> None:
    if database and ALLOWED_DATABASES and database not in ALLOWED_DATABASES:
        raise ValueError(f"Blocked: database '{database}' is not in MSSQL_ALLOWED_DATABASES.")
    if ALLOWED_SCHEMAS and schema not in ALLOWED_SCHEMAS:
        raise ValueError(f"Blocked: schema '{schema}' is not in MSSQL_ALLOWED_SCHEMAS.")


def _resolve_validated_table_name(
    database: str | None,
    schema: str,
    table: str,
    conn,
) -> tuple[str, str, str]:
    current_db = _current_database(conn)
    if database is None:
        database = current_db
    elif database != current_db:
        raise ValueError(
            "Blocked: cross-database metadata lookup is not enabled in this tiny server. "
            f"Connected database is '{current_db}', requested '{database}'."
        )
    _enforce_allowlists(schema=schema, database=database)
    return database, schema, table


def _engine_edition_name(value: Any) -> str:
    mapping = {
        2: "standard",
        3: "enterprise",
        4: "express",
        5: "azure_sql_database",
        6: "azure_synapse_analytics",
        8: "azure_sql_managed_instance",
        9: "azure_sql_edge",
        11: "azure_synapse_serverless",
    }
    try:
        key = int(value)
    except Exception:
        return "unknown"
    return mapping.get(key, "unknown")


def _warmup_worker() -> None:
    global _WARMUP_INFLIGHT, _WARMUP_LAST_OK, _WARMUP_LAST_ERROR
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS warmup_ok")
                cur.fetchone()
        _WARMUP_LAST_OK = int(time.time())
        _WARMUP_LAST_ERROR = None
    except Exception as exc:
        _WARMUP_LAST_ERROR = str(exc)
    finally:
        with _WARMUP_LOCK:
            _WARMUP_INFLIGHT = False

#end region

# region MCP tools

@mcp.tool()
def ping() -> dict[str, Any]:
    """Sanity check that the MCP server is running."""
    return {"status": "ok", "server": "mssql-tiny", "time": int(time.time())}

@mcp.tool()
def healthcheck(probe: bool = False) -> dict[str, Any]:
    """
    Check environment readiness and optional connection probe.
    Set probe=True to attempt a lightweight SELECT 1.
    """
    result: dict[str, Any] = {
        "env": {
            "SQL_CONNECTION_STRING": _env_present("SQL_CONNECTION_STRING"),
        },
        "config_warnings": CONFIG_WARNINGS,
        "warmup": {
            "in_flight": _WARMUP_INFLIGHT,
            "last_ok": _WARMUP_LAST_OK,
            "last_error": _WARMUP_LAST_ERROR,
        },
    }
    if probe:
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 AS probe_ok")
                    cur.fetchone()
            result["probe"] = {"status": "ok"}
        except Exception as exc:
            result["probe"] = {"status": "error", "error": str(exc)}
    return result


@mcp.tool()
def warmup() -> dict[str, Any]:
    """
    Trigger a background connection warmup.
    Returns immediately so clients do not block on login or cold starts.
    """
    global _WARMUP_INFLIGHT
    with _WARMUP_LOCK:
        if _WARMUP_INFLIGHT:
            return {
                "status": "in_progress",
                "last_ok": _WARMUP_LAST_OK,
                "last_error": _WARMUP_LAST_ERROR,
            }
        _WARMUP_INFLIGHT = True

    thread = threading.Thread(target=_warmup_worker, daemon=True)
    thread.start()
    return {
        "status": "started",
        "last_ok": _WARMUP_LAST_OK,
        "last_error": _WARMUP_LAST_ERROR,
    }


@mcp.tool()
def discover_context() -> dict[str, Any]:
    """Return basic connection and platform context for the current SQL endpoint."""
    query = """
    SELECT
        CAST(DB_NAME() AS nvarchar(128)) AS current_database,
        CAST(SUSER_SNAME() AS nvarchar(256)) AS login_name,
        CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)) AS server_name,
        CAST(SERVERPROPERTY('Edition') AS nvarchar(256)) AS edition,
        CAST(SERVERPROPERTY('EngineEdition') AS int) AS engine_edition
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            columns = _description_columns(cur)
            row = cur.fetchone()

    payload = _row_to_mapping(row, columns)
    payload["deployment_hint"] = _engine_edition_name(payload.get("engine_edition"))
    return payload


@mcp.tool()
def list_schemas() -> dict[str, Any]:
    """List schemas in the current database."""
    query = """
    SELECT
        s.name AS schema_name,
        s.schema_id
    FROM sys.schemas AS s
    ORDER BY s.name
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return _fetch(cur, 2000)


@mcp.tool()
def list_tables(schema: str | None = None) -> dict[str, Any]:
    """List base tables and views in the current database, optionally filtered by schema."""
    if schema is not None:
        schema = _validate_identifier(schema, "schema")
        _enforce_allowlists(schema=schema)
        query = """
        SELECT
            TABLE_CATALOG AS database_name,
            TABLE_SCHEMA AS schema_name,
            TABLE_NAME AS table_name,
            TABLE_TYPE AS table_type
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
        params: tuple[Any, ...] = (schema,)
    else:
        query = """
        SELECT
            TABLE_CATALOG AS database_name,
            TABLE_SCHEMA AS schema_name,
            TABLE_NAME AS table_name,
            TABLE_TYPE AS table_type
        FROM INFORMATION_SCHEMA.TABLES
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
        params = ()

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return _fetch(cur, 5000)


@mcp.tool()
def describe_table(full_name: str) -> dict[str, Any]:
    """Describe a table or view using schema.table or database.schema.table."""
    database, schema, table = _validate_table_name(full_name)
    _enforce_allowlists(schema=schema, database=database)

    with _connect() as conn:
        database, schema, table = _resolve_validated_table_name(database, schema, table, conn)
        query = """
        SELECT
            TABLE_CATALOG AS database_name,
            TABLE_SCHEMA AS schema_name,
            TABLE_NAME AS table_name,
            COLUMN_NAME AS column_name,
            ORDINAL_POSITION AS ordinal_position,
            DATA_TYPE AS data_type,
            IS_NULLABLE AS is_nullable,
            CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
            NUMERIC_PRECISION AS numeric_precision,
            NUMERIC_SCALE AS numeric_scale
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_CATALOG = ?
            AND TABLE_SCHEMA = ?
            AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """
        with conn.cursor() as cur:
            cur.execute(query, (database, schema, table))
            return _fetch(cur, 5000)


@mcp.tool()
def run_query_readonly(sql_text: str, max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """
    Run a read-only query. Enforces allowed prefixes + commands + row caps.
    Returns columns, rows, and row_count
    """
    _enforce_readonly(sql_text)
    max_rows = _cap_rows(max_rows)

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            result = _fetch(cur, max_rows)

    return {
        "max_rows": max_rows,
        **result,
    }


@mcp.tool()
def run_query_preview(sql_text: str, max_rows: int = 50) -> dict[str, Any]:
    """Run a read-only query with a small default row cap for quick previews."""
    return run_query_readonly(sql_text, max_rows=max_rows)


# endregoin


# entrypoint (stdio transport)

if __name__ == "__main__":
    mcp.run()
