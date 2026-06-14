from __future__ import annotations

import pytest

import mssql_mcp_server as server


class DummyCursor:
    def __init__(self, description=None, rows=None):
        """Capture queries and return canned rows"""
        self.description = description or []
        self._rows = rows or []
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        """Support context manager usage like a real DB cursor"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """Let exceptions propagate out of the cursor context"""
        return False

    def execute(self, query: str, params=()) -> None:
        """Record SQL and parameters from server code"""
        self.executed.append((query, tuple(params or ())))

    def fetchmany(self, max_rows: int):
        """Return up to the requested number of canned rows"""
        return self._rows[:max_rows]

    def fetchone(self):
        """Return the first canned row or None"""
        return self._rows[0] if self._rows else None


class DummyConnection:
    def __init__(self, cursor: DummyCursor):
        """Wrap a dummy cursor as a connection"""
        self._cursor = cursor

    def __enter__(self):
        """Support context manager usage like a real DB connection"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """Let exceptions propagate out of the connection context"""
        return False

    def cursor(self) -> DummyCursor:
        """Return the dummy cursor for query execution"""
        return self._cursor


class IndexableRow:
    def __init__(self, *values):
        """Store positional row values for driver tests"""
        self._values = values

    def __getitem__(self, index):
        """Return values by numeric index like DB row objects"""
        return self._values[index]


def test_validate_identifier_rejects_statement_shaped_input() -> None:
    """Reject identifiers that look like SQL statements"""
    with pytest.raises(ValueError, match="simple identifier"):
        server._validate_identifier("dbo; DROP TABLE x", "schema")


def test_validate_table_name_requires_two_or_three_parts() -> None:
    """Require schema table or database schema table names"""
    with pytest.raises(ValueError, match="schema.table or database.schema.table"):
        server._validate_table_name("dbo")


def test_run_query_readonly_blocks_write_keyword() -> None:
    """Block read-shaped queries that use write keywords"""
    with pytest.raises(ValueError, match="non-read-only keyword"):
        server.run_query_readonly("SELECT * INTO dbo.copy_of_table FROM dbo.source")


@pytest.mark.parametrize(
    ("sql_text", "message"),
    [
        ("UPDATE dbo.people SET first_name = first_name WHERE id = -1", "only SELECT, WITH"),
        ("INSERT INTO dbo.people (id) VALUES (-1)", "only SELECT, WITH"),
        ("DELETE FROM dbo.people WHERE id = -1", "only SELECT, WITH"),
        ("EXEC dbo.SomeProcedure", "only SELECT, WITH"),
        ("WITH target_rows AS (SELECT id FROM dbo.people) DELETE FROM target_rows", "non-read-only"),
    ],
)
def test_run_query_readonly_blocks_write_and_admin_shapes(
    monkeypatch: pytest.MonkeyPatch,
    sql_text: str,
    message: str,
) -> None:
    """Block write and admin shaped queries before connecting"""
    def unexpected_connect():
        """Fail if guardrails allow connection setup to run"""
        raise AssertionError("_connect() should not be called for blocked SQL")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match=message):
        server.run_query_readonly(sql_text)


def test_run_query_readonly_blocks_multiple_statements() -> None:
    """Block multiple SQL statements in one request"""
    with pytest.raises(ValueError, match="multiple SQL statements"):
        server.run_query_readonly("SELECT 1; SELECT 2")


def test_connect_uses_configured_query_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass configured timeout to the SQL driver"""
    captured: dict[str, object] = {}

    def fake_connect(connection_string: str, timeout: int):
        """Capture connect arguments"""
        captured["connection_string"] = connection_string
        captured["timeout"] = timeout
        return object()

    monkeypatch.setenv("SQL_CONNECTION_STRING", "Server=localhost;")
    monkeypatch.setattr(server, "QUERY_TIMEOUT_SECONDS", 17)
    monkeypatch.setattr(server.mssql_python, "connect", fake_connect)

    server._connect()

    assert captured == {
        "connection_string": "Server=localhost;",
        "timeout": 17,
    }


def test_risk_hints_detect_sensitive_column_names() -> None:
    """Detect sensitive looking column names"""
    result = server._fetch(
        DummyCursor(
            description=[("user_name",), ("password_hash",), ("access_token",), ("api_key",)],
            rows=[],
        ),
        max_rows=10,
    )

    assert result["column_risk_hints"] == {
        "password_hash": ["credential", "credential-material"],
        "access_token": ["token"],
        "api_key": ["key"],
    }


def test_list_tables_rejects_invalid_schema_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject invalid table schema before connecting"""
    def unexpected_connect():
        """Fail if validation allows connection setup to run"""
        raise AssertionError("_connect() should not be called for invalid identifiers")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="simple identifier"):
        server.list_tables("dbo --")


def test_list_tables_executes_expected_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build base-table metadata SQL for the target database"""
    cursor = DummyCursor(
        description=[("database_name",), ("schema_name",), ("table_name",), ("table_type",)],
        rows=[],
    )

    def fake_connect():
        """Return a dummy connection for query capture"""
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    server.list_tables("dbo", database="Inventory")

    query, params = cursor.executed[0]
    assert "FROM [Inventory].INFORMATION_SCHEMA.TABLES" in query
    assert "TABLE_TYPE = 'BASE TABLE'" in query
    assert params == ("dbo",)


def test_list_views_rejects_invalid_schema_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject invalid view schema before connecting"""
    def unexpected_connect():
        """Fail if validation allows connection setup to run"""
        raise AssertionError("_connect() should not be called for invalid identifiers")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="simple identifier"):
        server.list_views("dbo --")


def test_list_views_executes_expected_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build view metadata SQL for the target database"""
    cursor = DummyCursor(
        description=[("database_name",), ("schema_name",), ("view_name",), ("table_type",)],
        rows=[],
    )

    def fake_connect():
        """Return a dummy connection for query capture"""
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    server.list_views("dbo", database="Inventory")

    query, params = cursor.executed[0]
    assert "FROM [Inventory].INFORMATION_SCHEMA.TABLES" in query
    assert "TABLE_TYPE = 'VIEW'" in query
    assert params == ("dbo",)


def test_describe_table_rejects_comment_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject table names with comment suffixes before connect"""
    def unexpected_connect():
        """Fail if validation allows connection setup to run"""
        raise AssertionError("_connect() should not be called for invalid identifiers")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="simple identifier"):
        server.describe_table("dbo.events --")


def test_describe_table_allows_three_part_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow database schema table metadata lookup"""
    cursor = DummyCursor(
        description=[
            ("database_name",),
            ("schema_name",),
            ("table_name",),
            ("object_type",),
            ("column_name",),
            ("ordinal_position",),
            ("data_type",),
            ("character_maximum_length",),
            ("max_length_bytes",),
            ("numeric_precision",),
            ("numeric_scale",),
            ("is_primary_key",),
            ("is_identity",),
            ("is_computed",),
            ("computed_definition",),
            ("default_definition",),
            ("foreign_key_name",),
            ("referenced_schema_name",),
            ("referenced_table_name",),
            ("referenced_column_name",),
            ("row_count_estimate",),
        ],
        rows=[],
    )

    def fake_connect():
        """Return a dummy connection for column query capture"""
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    server.describe_table("OtherDb.dbo.events")

    query, params = cursor.executed[0]
    assert "FROM [OtherDb].sys.objects" in query
    assert "is_primary_key" in query
    assert "foreign_key_name" in query
    assert params == ("dbo", "events", "OtherDb")


def test_describe_table_summary_tracks_keys_and_sensitive_columns() -> None:
    """Summarize rich describe_table metadata"""
    result = {
        "columns": [
            "database_name",
            "schema_name",
            "table_name",
            "object_type",
            "column_name",
            "is_primary_key",
            "is_identity",
            "is_computed",
            "computed_definition",
            "foreign_key_name",
            "referenced_schema_name",
            "referenced_table_name",
            "referenced_column_name",
            "row_count_estimate",
        ],
        "rows": [
            [
                "TestDb",
                "dbo",
                "users",
                "USER_TABLE",
                "id",
                True,
                True,
                False,
                None,
                None,
                None,
                None,
                None,
                25,
            ],
            [
                "TestDb",
                "dbo",
                "users",
                "USER_TABLE",
                "password_hash",
                False,
                False,
                False,
                None,
                None,
                None,
                None,
                None,
                25,
            ],
            [
                "TestDb",
                "dbo",
                "users",
                "USER_TABLE",
                "role_id",
                False,
                False,
                False,
                None,
                "FK_users_roles",
                "dbo",
                "roles",
                "id",
                25,
            ],
        ],
        "row_count": 3,
    }

    server._add_describe_table_risk_hints(result)
    server._add_describe_table_summary(result)

    assert result["object"]["row_count_estimate"] == 25
    assert result["primary_keys"] == ["id"]
    assert result["identity_columns"] == ["id"]
    assert result["foreign_keys"] == [
        {
            "foreign_key_name": "FK_users_roles",
            "column_name": "role_id",
            "referenced_schema_name": "dbo",
            "referenced_table_name": "roles",
            "referenced_column_name": "id",
        }
    ]
    assert result["sensitive_columns"] == [
        {
            "column_name": "password_hash",
            "risk_hints": ["credential", "credential-material"],
        }
    ]


def test_cap_rows_uses_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clamp requested rows to the hard limit"""
    monkeypatch.setattr(server, "DEFAULT_MAX_ROWS", 100)
    monkeypatch.setattr(server, "HARD_MAX_ROWS", 250)

    assert server._cap_rows(9999) == 250


def test_healthcheck_reports_config_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Include config warnings in healthcheck output"""
    monkeypatch.setattr(server, "CONFIG_WARNINGS", ["Invalid integer for MSSQL_DEFAULT_MAX_ROWS"])
    monkeypatch.setattr(server, "_WARMUP_INFLIGHT", False)
    monkeypatch.setattr(server, "_WARMUP_LAST_OK", None)
    monkeypatch.setattr(server, "_WARMUP_LAST_ERROR", None)

    result = server.healthcheck(probe=False)

    assert result["config_warnings"] == ["Invalid integer for MSSQL_DEFAULT_MAX_ROWS"]


def test_discover_context_handles_indexable_row_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map indexable driver rows into context output"""
    cursor = DummyCursor(
        description=[
            ("current_database",),
            ("login_name",),
            ("server_name",),
            ("edition",),
            ("engine_edition",),
        ],
        rows=[IndexableRow("master", "sa", "localhost", "Express Edition", 4)],
    )

    def fake_connect():
        """Return a dummy connection for context capture"""
        return DummyConnection(cursor)

    monkeypatch.setattr(server, "_connect", fake_connect)

    result = server.discover_context()

    assert result["current_database"] == "master"
    assert result["deployment_hint"] == "express"


def test_verify_environment_reports_dotenv_and_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report dotenv packages and env readiness"""
    class FakeEnvPath:
        def exists(self):
            """Pretend the env file exists"""
            return True

        def __str__(self):
            """Return a stable fake env path"""
            return "fake-project/.env"

    class FakeProjectDir:
        def __truediv__(self, name: str):
            """Return fake env path for joins"""
            assert name == ".env"
            return FakeEnvPath()

        def __str__(self):
            """Return a stable fake project path"""
            return "fake-project"

    monkeypatch.setattr(server, "PROJECT_DIR", FakeProjectDir())
    monkeypatch.setattr(
        server,
        "dotenv_values",
        lambda env_path: {"SQL_CONNECTION_STRING": "Server=localhost;Database=test;"},
    )
    monkeypatch.setattr(server, "_package_version", lambda package_name: "test-version")
    monkeypatch.setattr(server, "CONFIG_WARNINGS", [])
    monkeypatch.setenv("SQL_CONNECTION_STRING", "Server=localhost;Database=test;")

    result = server.verify_environment(probe=False)

    assert result["packages"]["mssql-python"] == "test-version"
    assert result["dotenv"]["exists"] is True
    assert result["env"]["SQL_CONNECTION_STRING"] is True
    assert result["limits"]["default_max_rows"] == server.DEFAULT_MAX_ROWS
    assert result["limits"]["hard_max_rows"] == server.HARD_MAX_ROWS
    assert result["limits"]["query_timeout_seconds"] == server.QUERY_TIMEOUT_SECONDS
    assert "guardrails" not in result
    assert "healthcheck" not in result
