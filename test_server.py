from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import mssql_mcp_server as server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test the MSSQL tiny MCP server configuration."
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the live SQL connection probe.",
    )
    parser.add_argument(
        "--discover-context",
        action="store_true",
        help="Also run discover_context() after healthcheck.",
    )
    return parser.parse_args()


def print_result(label: str, payload: Any) -> None:
    print(f"=== {label} ===")
    print(json.dumps(payload, indent=2))


def main() -> int:
    args = parse_args()

    ping_result = server.ping()
    print_result("ping", ping_result)

    healthcheck_result = server.healthcheck(probe=not args.no_probe)
    print_result("healthcheck", healthcheck_result)

    if args.discover_context:
        context_result = server.discover_context()
        print_result("discover_context", context_result)

    probe_result = healthcheck_result.get("probe", {})
    if probe_result.get("status") == "error":
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
