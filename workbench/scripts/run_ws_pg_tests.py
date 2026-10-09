#!/usr/bin/env python3
"""Run the entire workbench suite through real PG-over-WebSocket sessions.

TEST_POSTGRES_URL must name a disposable loopback PostgreSQL zhixu_tests DB
with sslmode=disable. The bridge is test-only; no external service is contacted.
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from neon_ws_proxy import PostgresWebSocketProxy, local_postgres_target


def main():
    url = os.environ.get("TEST_POSTGRES_URL", "")
    try:
        local_postgres_target(url)
        if not unquote(urlsplit(url).path).startswith("/zhixu_tests"):
            raise ValueError
    except ValueError:
        print("FAIL WebSocket QA requires a disposable local zhixu_tests PostgreSQL database")
        return 2
    with PostgresWebSocketProxy(url) as proxy, patch.dict(os.environ, {
        "DATABASE_TRANSPORT": "neon-ws",
        "NEON_WS_TEST_URL": proxy.url,
        "TEST_WORKBENCH_POSTGRES_URL": url,
    }):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.wasSuccessful() and proxy.session_count and min(proxy.byte_counts) > 0:
            print(f"PASS real PostgreSQL over WebSocket: {proxy.session_count} sessions")
            return 0
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # No URL, password, SQL or arbitrary upstream exception is printed.
        print("FAIL WebSocket PostgreSQL QA: " + type(error).__name__)
        raise SystemExit(1) from None
