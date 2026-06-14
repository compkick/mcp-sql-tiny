"""
mssql_mcp_server.py
Tiny MCP server for Microsoft SQL Server and Azure SQL over stdio.

Primary configuration:
  SQL_CONNECTION_STRING   Standard SQL Server connection string for mssql-python

Optional guardrails:
  MSSQL_QUERY_TIMEOUT_SECONDS query timeout for SQL execution (int)
  MSSQL_DEFAULT_MAX_ROWS  default row cap (int)
  MSSQL_HARD_MAX_ROWS     absolute row cap (int)
"""

from __future__ import annotations

import sys
import os
import re
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import mssql_python
from dotenv import dotenv_values, load_dotenv
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

READONLY_PREFIXES = (
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

SENSITIVE_NAME_MARKERS = (
    ("password", "credential"),
    ("passwd", "credential"),
    ("pwd", "credential"),
    ("token", "token"),
    ("secret", "secret"),
    ("api_key", "key"),
    ("apikey", "key"),
    ("access_key", "key"),
    ("private_key", "key"),
    ("key", "key"),
    ("salt", "credential-material"),
    ("hash", "credential-material"),
    ("credential", "credential"),
    ("connection_string", "connection-string"),
)

# ----------------------------
# SQL text helpers
# ----------------------------


def _strip_comments(sql_text: str) -> str:
    """Remove SQL comments before guardrail checks"""
    s = BLOCK_COMMENT.sub(" ", sql_text)
    s = LINE_COMMENT.sub(" ", s)
    return s


def _first_token(sql_text: str) -> str:
    """Return the first SQL token after comments and whitespace"""
    s = _strip_comments(sql_text).strip()
    s = re.sub(r"^[\s(]+", "", s)
    match = re.match(r"([A-Za-z]+)", s)
    return (match.group(1) if match else "").upper()


def _contains_multiple_statements(sql_text: str) -> bool:
    """Detect semicolon-separated SQL statements"""
    stripped = _strip_comments(sql_text).strip()
    if not stripped:
        return False
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return ";" in stripped


def _validate_identifier(value: str, label: str) -> str:
    """Validate a simple SQL Server identifier"""
    s = value.strip()
    if not s:
        raise ValueError(f"Blocked: {label} cannot be empty.")
    if not IDENTIFIER_RE.fullmatch(s):
        raise ValueError(
            f"Blocked: {label} must be a simple identifier containing only "
            "letters, numbers, and underscores, and cannot start with a number."
        )
    return s


def _quote_identifier(value: str) -> str:
    """Quote a previously validated SQL Server identifier"""
    return f"[{value.replace(']', ']]')}]"


def _validate_table_name(full_name: str) -> tuple[str | None, str, str]:
    """Validate schema table or database schema table input"""
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


def _load_int_env(name: str, default: int, minimum: int = 1) -> int:
    """Load an integer environment variable with warnings"""
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


def _package_version(package_name: str) -> str | None:
    """Return an installed package version when available"""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


DEFAULT_MAX_ROWS = _load_int_env("MSSQL_DEFAULT_MAX_ROWS", 1000)
HARD_MAX_ROWS = _load_int_env("MSSQL_HARD_MAX_ROWS", 5000)
QUERY_TIMEOUT_SECONDS = _load_int_env("MSSQL_QUERY_TIMEOUT_SECONDS", 30, minimum=0)
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
    """Read a required environment variable"""
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f"Missing env var: {name}. Set it in your shell or in {PROJECT_DIR / '.env'}."
        )
    return value


def _env_present(name: str) -> bool:
    """Check whether an environment variable has a value"""
    value = os.getenv(name)
    return bool(value and value.strip())


def _connect():
    """Open a SQL Server connection from the configured string"""
    connection_string = _env("SQL_CONNECTION_STRING")
    return mssql_python.connect(connection_string, timeout=QUERY_TIMEOUT_SECONDS)


def _enforce_readonly(sql_text: str) -> None:
    """Reject SQL that falls outside the read-only guardrails"""
    if not sql_text or not sql_text.strip():
        raise ValueError("Empty SQL text.")

    token = _first_token(sql_text)
    if token not in READONLY_PREFIXES:
        raise ValueError(
            f"Blocked: only {', '.join(READONLY_PREFIXES)} statements are allowed. "
            f"Detected leading token: {token or '(none)'}"
        )

    if _contains_multiple_statements(sql_text):
        raise ValueError("Blocked: multiple SQL statements are not allowed.")

    if BLOCKED_COMMANDS.search(sql_text):
        raise ValueError("Blocked: query contains a non-read-only keyword.")


def _cap_rows(max_rows: int) -> int:
    """Clamp requested rows to configured bounds"""
    try:
        value = int(max_rows)
    except Exception:
        value = DEFAULT_MAX_ROWS
    if value < 1:
        value = 1
    if value > HARD_MAX_ROWS:
        value = HARD_MAX_ROWS
    return value


def _risk_hints_for_name(name: str) -> list[str]:
    """Return risk hints for sensitive-looking names"""
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower())
    hints: list[str] = []
    for marker, hint in SENSITIVE_NAME_MARKERS:
        if marker in normalized and hint not in hints:
            hints.append(hint)
    return hints


def _result_column_risk_hints(columns: list[str]) -> dict[str, list[str]]:
    """Return risk hints keyed by result column name"""
    return {column: hints for column in columns if (hints := _risk_hints_for_name(column))}


def _add_describe_table_risk_hints(result: dict[str, Any]) -> None:
    """Add per-table-column risk hints to describe_table rows"""
    try:
        column_index = result["columns"].index("column_name")
    except ValueError:
        return

    result["columns"].append("risk_hints")
    for row in result["rows"]:
        row.append(_risk_hints_for_name(str(row[column_index])))


def _add_describe_table_summary(result: dict[str, Any]) -> None:
    """Add table-level summary fields to describe_table output"""
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not rows:
        result["object"] = None
        result["primary_keys"] = []
        result["identity_columns"] = []
        result["computed_columns"] = []
        result["foreign_keys"] = []
        result["sensitive_columns"] = []
        return

    index = {column: position for position, column in enumerate(columns)}

    def value(row: list[Any], column: str) -> Any:
        return row[index[column]]

    first_row = rows[0]
    result["object"] = {
        "database_name": value(first_row, "database_name"),
        "schema_name": value(first_row, "schema_name"),
        "table_name": value(first_row, "table_name"),
        "object_type": value(first_row, "object_type"),
        "row_count_estimate": value(first_row, "row_count_estimate"),
    }
    result["primary_keys"] = [
        value(row, "column_name") for row in rows if bool(value(row, "is_primary_key"))
    ]
    result["identity_columns"] = [
        value(row, "column_name") for row in rows if bool(value(row, "is_identity"))
    ]
    result["computed_columns"] = [
        {
            "column_name": value(row, "column_name"),
            "definition": value(row, "computed_definition"),
        }
        for row in rows
        if bool(value(row, "is_computed"))
    ]

    foreign_keys = []
    seen_foreign_keys = set()
    for row in rows:
        foreign_key_name = value(row, "foreign_key_name")
        if not foreign_key_name:
            continue
        key = (
            foreign_key_name,
            value(row, "column_name"),
            value(row, "referenced_schema_name"),
            value(row, "referenced_table_name"),
            value(row, "referenced_column_name"),
        )
        if key in seen_foreign_keys:
            continue
        seen_foreign_keys.add(key)
        foreign_keys.append(
            {
                "foreign_key_name": foreign_key_name,
                "column_name": value(row, "column_name"),
                "referenced_schema_name": value(row, "referenced_schema_name"),
                "referenced_table_name": value(row, "referenced_table_name"),
                "referenced_column_name": value(row, "referenced_column_name"),
            }
        )
    result["foreign_keys"] = foreign_keys

    risk_index = index.get("risk_hints")
    sensitive_columns = []
    if risk_index is not None:
        for row in rows:
            hints = row[risk_index]
            if hints:
                sensitive_columns.append(
                    {
                        "column_name": value(row, "column_name"),
                        "risk_hints": hints,
                    }
                )
    result["sensitive_columns"] = sensitive_columns


def _fetch(cur, max_rows: int, include_column_risk_hints: bool = True) -> dict[str, Any]:
    """Fetch rows from a cursor into a JSON-friendly payload"""
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchmany(max_rows) if cols else []
    result = {
        "columns": cols,
        "rows": [list(row) for row in rows],
        "row_count": len(rows),
    }
    if include_column_risk_hints:
        result["column_risk_hints"] = _result_column_risk_hints(cols)
    return result


def _description_columns(cur) -> list[str]:
    """Read column names from a cursor description"""
    return [d[0] for d in (cur.description or [])]


def _row_to_mapping(row: Any, columns: list[str]) -> dict[str, Any]:
    """Map driver-specific row objects to dictionaries"""
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
    """Return the active database for a SQL connection"""
    with conn.cursor() as cur:
        cur.execute("SELECT CAST(DB_NAME() AS nvarchar(128)) AS current_database")
        columns = _description_columns(cur)
        row = cur.fetchone()
    payload = _row_to_mapping(row, columns)
    if "current_database" not in payload:
        raise RuntimeError("Unable to determine current database from the SQL connection.")
    return str(payload["current_database"])


def _resolve_metadata_database(database: str | None, conn) -> str:
    """Resolve the metadata target database"""
    if database is None:
        database = _current_database(conn)
    else:
        database = _validate_identifier(database, "database")
    return database


def _engine_edition_name(value: Any) -> str:
    """Convert SQL Server engine edition codes to readable names"""
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
    """Run a background connection probe"""
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


def _connection_string_auth_hint() -> str:
    """Infer the configured authentication style without returning secrets"""
    connection_string = os.getenv("SQL_CONNECTION_STRING", "")
    lowered = connection_string.lower()
    if "trusted_connection=yes" in lowered or "integrated security=sspi" in lowered:
        return "windows_integrated"
    if "authentication=activedirectory" in lowered:
        return "microsoft_entra"
    if "uid=" in lowered or "user id=" in lowered:
        return "sql_login"
    if connection_string.strip():
        return "unknown"
    return "missing"

# endregion

# region MCP tools

@mcp.tool()
def ping() -> dict[str, Any]:
    """Sanity check that the MCP server is running"""
    return {"status": "ok", "server": "mssql-tiny", "time": int(time.time())}

@mcp.tool()
def healthcheck(probe: bool = False) -> dict[str, Any]:
    """
    Check environment readiness and optional SQL connectivity

    Set probe=True to attempt a lightweight SELECT 1
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
def verify_environment(probe: bool = False) -> dict[str, Any]:
    """
    Verify Python packages dotenv and optional SQL connectivity
    Does not return secret values from the connection string
    """
    env_path = PROJECT_DIR / ".env"
    dotenv_values_map = dotenv_values(env_path) if env_path.exists() else {}
    result: dict[str, Any] = {
        "project_dir": str(PROJECT_DIR),
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "in_virtualenv": sys.prefix != sys.base_prefix,
        },
        "packages": {
            "mcp": _package_version("mcp"),
            "mssql-python": _package_version("mssql-python"),
            "python-dotenv": _package_version("python-dotenv"),
        },
        "dotenv": {
            "path": str(env_path),
            "exists": env_path.exists(),
            "key_count": len(dotenv_values_map),
            "loaded": bool(dotenv_values_map),
        },
        "env": {
            "SQL_CONNECTION_STRING": _env_present("SQL_CONNECTION_STRING"),
            "auth_hint": _connection_string_auth_hint(),
        },
        "limits": {
            "default_max_rows": DEFAULT_MAX_ROWS,
            "hard_max_rows": HARD_MAX_ROWS,
            "query_timeout_seconds": QUERY_TIMEOUT_SECONDS,
        },
        "config_warnings": CONFIG_WARNINGS,
    }

    if probe:
        result["healthcheck"] = healthcheck(probe=True)

    return result


@mcp.tool()
def warmup() -> dict[str, Any]:
    """
    Trigger a background connection warmup
    Returns immediately so clients do not block on login or cold starts
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
    """Return connection and platform context for the SQL endpoint"""
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
def list_databases() -> dict[str, Any]:
    """List databases visible to the current login"""
    query = """
    SELECT
        name AS database_name,
        database_id,
        state_desc,
        user_access_desc,
        is_read_only
    FROM sys.databases
    WHERE HAS_DBACCESS(name) = 1
    ORDER BY name
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            result = _fetch(cur, 2000)

    return result


@mcp.tool()
def list_schemas(database: str | None = None) -> dict[str, Any]:
    """List schemas in the current or specified database"""
    if database is not None:
        database = _validate_identifier(database, "database")

    with _connect() as conn:
        database = _resolve_metadata_database(database, conn)
        query = f"""
        SELECT
            DB_NAME() AS connection_database,
            CAST(? AS nvarchar(128)) AS database_name,
            s.name AS schema_name,
            s.schema_id
        FROM {_quote_identifier(database)}.sys.schemas AS s
        ORDER BY s.name
        """
        with conn.cursor() as cur:
            cur.execute(query, (database,))
            return _fetch(cur, 2000)


@mcp.tool()
def list_tables(schema: str | None = None, database: str | None = None) -> dict[str, Any]:
    """List base tables in the current or specified database"""
    if schema is not None:
        schema = _validate_identifier(schema, "schema")
    if database is not None:
        database = _validate_identifier(database, "database")

    with _connect() as conn:
        database = _resolve_metadata_database(database, conn)
        if schema is not None:
            query = f"""
            SELECT
                TABLE_CATALOG AS database_name,
                TABLE_SCHEMA AS schema_name,
                TABLE_NAME AS table_name,
                TABLE_TYPE AS table_type
            FROM {_quote_identifier(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
            params: tuple[Any, ...] = (schema,)
        else:
            query = f"""
            SELECT
                TABLE_CATALOG AS database_name,
                TABLE_SCHEMA AS schema_name,
                TABLE_NAME AS table_name,
                TABLE_TYPE AS table_type
            FROM {_quote_identifier(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
            params = ()

        with conn.cursor() as cur:
            cur.execute(query, params)
            return _fetch(cur, 5000)


@mcp.tool()
def list_views(schema: str | None = None, database: str | None = None) -> dict[str, Any]:
    """List views in the current or specified database"""
    if schema is not None:
        schema = _validate_identifier(schema, "schema")
    if database is not None:
        database = _validate_identifier(database, "database")

    with _connect() as conn:
        database = _resolve_metadata_database(database, conn)
        if schema is not None:
            query = f"""
            SELECT
                TABLE_CATALOG AS database_name,
                TABLE_SCHEMA AS schema_name,
                TABLE_NAME AS view_name,
                TABLE_TYPE AS table_type
            FROM {_quote_identifier(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
              AND TABLE_TYPE = 'VIEW'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
            params: tuple[Any, ...] = (schema,)
        else:
            query = f"""
            SELECT
                TABLE_CATALOG AS database_name,
                TABLE_SCHEMA AS schema_name,
                TABLE_NAME AS view_name,
                TABLE_TYPE AS table_type
            FROM {_quote_identifier(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'VIEW'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
            params = ()

        with conn.cursor() as cur:
            cur.execute(query, params)
            return _fetch(cur, 5000)


@mcp.tool()
def describe_table(full_name: str) -> dict[str, Any]:
    """Describe a table or view by schema table or database schema table"""
    database, schema, table = _validate_table_name(full_name)

    with _connect() as conn:
        database = _resolve_metadata_database(database, conn)
        query = f"""
        WITH target_object AS (
            SELECT
                o.object_id,
                o.type_desc
            FROM {_quote_identifier(database)}.sys.objects AS o
            INNER JOIN {_quote_identifier(database)}.sys.schemas AS s
                ON s.schema_id = o.schema_id
            WHERE s.name = ?
              AND o.name = ?
              AND o.type IN ('U', 'V')
        ),
        primary_key_columns AS (
            SELECT
                ic.object_id,
                ic.column_id
            FROM {_quote_identifier(database)}.sys.indexes AS i
            INNER JOIN {_quote_identifier(database)}.sys.index_columns AS ic
                ON ic.object_id = i.object_id
               AND ic.index_id = i.index_id
            WHERE i.is_primary_key = 1
        ),
        foreign_key_columns AS (
            SELECT
                fkc.parent_object_id AS object_id,
                fkc.parent_column_id AS column_id,
                fk.name AS foreign_key_name,
                rs.name AS referenced_schema_name,
                ro.name AS referenced_table_name,
                rc.name AS referenced_column_name
            FROM {_quote_identifier(database)}.sys.foreign_key_columns AS fkc
            INNER JOIN {_quote_identifier(database)}.sys.foreign_keys AS fk
                ON fk.object_id = fkc.constraint_object_id
            INNER JOIN {_quote_identifier(database)}.sys.objects AS ro
                ON ro.object_id = fkc.referenced_object_id
            INNER JOIN {_quote_identifier(database)}.sys.schemas AS rs
                ON rs.schema_id = ro.schema_id
            INNER JOIN {_quote_identifier(database)}.sys.columns AS rc
                ON rc.object_id = fkc.referenced_object_id
               AND rc.column_id = fkc.referenced_column_id
        ),
        row_counts AS (
            SELECT
                p.object_id,
                SUM(p.rows) AS row_count_estimate
            FROM {_quote_identifier(database)}.sys.partitions AS p
            WHERE p.index_id IN (0, 1)
            GROUP BY p.object_id
        )
        SELECT
            CAST(? AS nvarchar(128)) AS database_name,
            s.name AS schema_name,
            o.name AS table_name,
            target_object.type_desc AS object_type,
            c.name AS column_name,
            c.column_id AS ordinal_position,
            ty.name AS data_type,
            c.is_nullable,
            CASE
                WHEN ty.name IN ('nchar', 'nvarchar') AND c.max_length > 0 THEN c.max_length / 2
                WHEN c.max_length = -1 THEN -1
                ELSE c.max_length
            END AS character_maximum_length,
            c.max_length AS max_length_bytes,
            c.precision AS numeric_precision,
            c.scale AS numeric_scale,
            CAST(CASE WHEN pk.column_id IS NOT NULL THEN 1 ELSE 0 END AS bit) AS is_primary_key,
            c.is_identity,
            c.is_computed,
            cc.definition AS computed_definition,
            dc.definition AS default_definition,
            fk.foreign_key_name,
            fk.referenced_schema_name,
            fk.referenced_table_name,
            fk.referenced_column_name,
            row_counts.row_count_estimate
        FROM target_object
        INNER JOIN {_quote_identifier(database)}.sys.objects AS o
            ON o.object_id = target_object.object_id
        INNER JOIN {_quote_identifier(database)}.sys.schemas AS s
            ON s.schema_id = o.schema_id
        INNER JOIN {_quote_identifier(database)}.sys.columns AS c
            ON c.object_id = o.object_id
        INNER JOIN {_quote_identifier(database)}.sys.types AS ty
            ON ty.user_type_id = c.user_type_id
        LEFT JOIN primary_key_columns AS pk
            ON pk.object_id = c.object_id
           AND pk.column_id = c.column_id
        LEFT JOIN {_quote_identifier(database)}.sys.computed_columns AS cc
            ON cc.object_id = c.object_id
           AND cc.column_id = c.column_id
        LEFT JOIN {_quote_identifier(database)}.sys.default_constraints AS dc
            ON dc.parent_object_id = c.object_id
           AND dc.parent_column_id = c.column_id
        LEFT JOIN foreign_key_columns AS fk
            ON fk.object_id = c.object_id
           AND fk.column_id = c.column_id
        LEFT JOIN row_counts
            ON row_counts.object_id = c.object_id
        ORDER BY c.column_id
        """
        with conn.cursor() as cur:
            cur.execute(query, (schema, table, database))
            result = _fetch(cur, 5000, include_column_risk_hints=False)

    _add_describe_table_risk_hints(result)
    _add_describe_table_summary(result)
    return result


@mcp.tool()
def run_query_readonly(sql_text: str, max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """
    Run a read-only query with prefix command and row-cap guardrails

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
    """Run a read-only query with a small default row cap"""
    return run_query_readonly(sql_text, max_rows=max_rows)


# endregion


# entrypoint (stdio transport)

if __name__ == "__main__":
    mcp.run()
