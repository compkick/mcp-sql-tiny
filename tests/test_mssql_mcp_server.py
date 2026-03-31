from __future__ import annotations

import pytest

import mssql_mcp_server as server


class DummyCursor:
    def __init__(self, description=None, rows=None):
        self.description = description or []
        self._rows = rows or []
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query: str, params=()) -> None:
        self.executed.append((query, tuple(params or ())))

    def fetchmany(self, max_rows: int):
        return self._rows[:max_rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class DummyConnection:
    def __init__(self, cursor: DummyCursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self) -> DummyCursor:
        return self._cursor


def test_validate_identifier_rejects_statement_shaped_input() -> None:
    with pytest.raises(ValueError, match="simple identifier"):
        server._validate_identifier("dbo; DROP TABLE x", "schema")


def test_validate_table_name_requires_two_or_three_parts() -> None:
    with pytest.raises(ValueError, match="schema.table or database.schema.table"):
        server._validate_table_name("dbo")


def test_run_query_readonly_blocks_write_keyword() -> None:
    with pytest.raises(ValueError, match="non-read-only keyword"):
        server.run_query_readonly("SELECT * INTO dbo.copy_of_table FROM dbo.source")


def test_run_query_readonly_blocks_multiple_statements() -> None:
    with pytest.raises(ValueError, match="multiple SQL statements"):
        server.run_query_readonly("SELECT 1; SELECT 2")


def test_list_tables_rejects_invalid_schema_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connect():
        raise AssertionError("_connect() should not be called for invalid identifiers")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="simple identifier"):
        server.list_tables("dbo --")


def test_list_tables_executes_expected_query(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = DummyCursor(
        description=[("database_name",), ("schema_name",), ("table_name",), ("table_type",)],
        rows=[],
    )

    def fake_connect():
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    server.list_tables("dbo")

    assert cursor.executed == [
        (
            """
        SELECT
            TABLE_CATALOG AS database_name,
            TABLE_SCHEMA AS schema_name,
            TABLE_NAME AS table_name,
            TABLE_TYPE AS table_type
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = ?
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """,
            ("dbo",),
        )
    ]


def test_describe_table_rejects_comment_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connect():
        raise AssertionError("_connect() should not be called for invalid identifiers")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="simple identifier"):
        server.describe_table("dbo.events --")


def test_describe_table_blocks_cross_database_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = DummyCursor(rows=[("CurrentDb",)])

    def fake_connect():
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    with pytest.raises(ValueError, match="cross-database metadata lookup"):
        server.describe_table("OtherDb.dbo.events")


def test_cap_rows_uses_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "DEFAULT_MAX_ROWS", 100)
    monkeypatch.setattr(server, "HARD_MAX_ROWS", 250)

    assert server._cap_rows(9999) == 250


def test_healthcheck_reports_config_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "CONFIG_WARNINGS", ["Invalid integer for MSSQL_DEFAULT_MAX_ROWS"])
    monkeypatch.setattr(server, "_WARMUP_INFLIGHT", False)
    monkeypatch.setattr(server, "_WARMUP_LAST_OK", None)
    monkeypatch.setattr(server, "_WARMUP_LAST_ERROR", None)

    result = server.healthcheck(probe=False)

    assert result["config_warnings"] == ["Invalid integer for MSSQL_DEFAULT_MAX_ROWS"]
