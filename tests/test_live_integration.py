from __future__ import annotations

import pytest

import mssql_mcp_server as server


RUN_LIVE_TESTS = False

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_LIVE_TESTS,
        reason="set RUN_LIVE_TESTS = True to run live SQL integration tests",
    ),
]


def test_live_healthcheck_probe() -> None:
    """Probe the configured SQL connection"""
    result = server.healthcheck(probe=True)

    assert result["env"]["SQL_CONNECTION_STRING"] is True
    assert result["probe"]["status"] == "ok"


def test_live_list_databases() -> None:
    """List at least one visible database"""
    result = server.list_databases()

    assert "database_name" in result["columns"]
    assert result["row_count"] >= 1


def test_live_readonly_guardrail_blocks_write_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block write SQL before opening a live connection"""
    def unexpected_connect():
        """Fail if guardrails allow connection setup to run"""
        raise AssertionError("_connect() should not be called for blocked SQL")

    monkeypatch.setattr(server, "_connect", unexpected_connect)

    with pytest.raises(ValueError, match="only SELECT, WITH"):
        server.run_query_readonly("UPDATE dbo.people SET id = id")
