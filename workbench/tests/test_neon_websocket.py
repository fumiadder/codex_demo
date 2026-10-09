"""Optional real PostgreSQL transaction tests through a local WebSocket proxy."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neon_ws_proxy import PostgresWebSocketProxy, local_postgres_target
from persistence import Database
from test_persistence import PersistenceContract


class ProxyTargetTests(unittest.TestCase):
    def test_proxy_refuses_remote_or_tls_targets_before_opening_any_socket(self):
        for url in (
            "postgresql://owner:secret@example.com/zhixu_tests?sslmode=disable",
            "postgresql://owner:secret@127.0.0.1/zhixu_tests?sslmode=require",
            "postgresql://owner:secret@localhost/zhixu_tests",
            "postgresql://owner:secret@127.0.0.1/zhixu_tests?sslmode=disable&sslmode=require",
            "https://localhost/zhixu_tests?sslmode=disable",
        ):
            with self.subTest(url_kind=url.split(":", 1)[0]):
                with self.assertRaises(ValueError) as raised:
                    local_postgres_target(url)
                self.assertNotIn("secret", str(raised.exception))


class _SchemaDatabase:
    """Keep every connection in one uniquely named, disposable test schema."""

    def __init__(self, database, schema):
        self.database, self.schema = database, schema

    @contextmanager
    def connect_context(self):
        with self.database.connect_context() as conn:
            conn.execute(f'SET LOCAL search_path TO "{self.schema}"')
            yield conn


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_URL"), "TEST_POSTGRES_URL is not configured")
class NeonWebSocketPersistenceTests(PersistenceContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["TEST_POSTGRES_URL"]
        # Validation occurs before importing optional websocket test libraries.
        cls.proxy = PostgresWebSocketProxy(url)
        cls.proxy.__enter__()
        cls.transport_env = patch.dict(os.environ, {
            "DATABASE_TRANSPORT": "neon-ws", "NEON_WS_TEST_URL": cls.proxy.url,
        })
        cls.transport_env.start()
        cls.base_database = Database("unused", url)
        cls.schema = "neon_ws_" + uuid.uuid4().hex
        try:
            with cls.base_database.connect_context() as conn:
                conn.execute(f'CREATE SCHEMA "{cls.schema}"')
            cls.database = _SchemaDatabase(cls.base_database, cls.schema)
        except BaseException:
            cls.transport_env.stop()
            cls.proxy.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            # This generated schema contains only this class's probe tables.
            with cls.base_database.connect_context() as conn:
                conn.execute(f'DROP SCHEMA "{cls.schema}" CASCADE')
            if cls.proxy.session_count < 2 or min(cls.proxy.byte_counts) <= 0:
                raise AssertionError("PostgreSQL checks did not use actual WebSocket sessions")
        finally:
            cls.transport_env.stop()
            cls.proxy.__exit__(None, None, None)

    def test_parameter_literals_percent_and_unicode_cross_actual_wire_unchanged(self):
        value = "月光🌙，50% 安心睡；' ? %s"
        with self.database.connect_context() as conn:
            row = conn.execute(
                "SELECT ? AS value, '50% ?; ''text''' AS literal, $$?; 100%$$ AS dollar, "
                "$bedtime$?; 100% %s$bedtime$ AS tagged_dollar "
                "/* ?; %s /* ? */ */ -- ?; %s\n, CAST(? AS BIGINT) AS large_integer",
                (value, 2**42),
            ).fetchone()
        self.assertEqual(dict(row), {
            "value": value, "literal": "50% ?; 'text'", "dollar": "?; 100%",
            "tagged_dollar": "?; 100% %s", "large_integer": 2**42,
        })
        self.assertEqual(row["VALUE"], value)
        self.assertIsInstance(row["large_integer"], int)

    def test_large_unicode_value_crosses_fragmented_messages_and_new_connections(self):
        value = "睡前月光 ? 50% 🌙；" * 7000
        initial_sessions = self.proxy.session_count
        with self.database.connect_context() as conn:
            conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("large", value))
        with self.database.connect_context() as conn:
            row = conn.execute(f"SELECT value FROM {self.parent_table} WHERE id=?", ("large",)).fetchone()
        self.assertEqual(row[0], value)
        self.assertGreaterEqual(self.proxy.session_count - initial_sessions, 2)

    def test_schema_script_failure_rolls_back_all_statements(self):
        table = self.prefix + "_atomic"
        with self.assertRaises(sqlite3.OperationalError):
            with self.database.connect_context() as conn:
                conn.executescript(f"CREATE TABLE {table} (id TEXT); INVALID SQL;")
        with self.database.connect_context() as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass(?)", (table,)).fetchone()[0])

    def test_real_session_uses_read_committed_and_configured_timeouts(self):
        with self.database.connect_context() as conn:
            self.assertEqual(conn.execute("SHOW transaction_isolation").fetchone()[0], "read committed")
            self.assertEqual(conn.execute("SHOW statement_timeout").fetchone()[0], "15s")
            self.assertEqual(conn.execute("SHOW lock_timeout").fetchone()[0], "15s")

    def test_native_constraint_details_do_not_escape_the_persistence_facade(self):
        private_value = "private-probe-value-" + uuid.uuid4().hex
        with self.database.connect_context() as conn:
            conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("first", private_value))
        with self.assertRaises(sqlite3.IntegrityError) as raised:
            with self.database.connect_context() as conn:
                conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("second", private_value))
        self.assertEqual(str(raised.exception), "Database integrity constraint failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(private_value, repr(raised.exception))
        with self.database.connect_context() as conn:
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.parent_table}").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
