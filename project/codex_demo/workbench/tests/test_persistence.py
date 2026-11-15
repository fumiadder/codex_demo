"""Persistence contract checks; no external accounts or provider calls."""
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import sys
import tempfile
import threading
import traceback
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persistence import Database, Row, _postgres_query, _schema_statements


class PersistenceContract:
    """The same transaction behavior must hold for local and cloud storage."""

    def setUp(self):
        self.prefix = "persistence_" + uuid.uuid4().hex
        self.parent_table, self.child_table = self.prefix + "_parents", self.prefix + "_children"
        with self.database.connect_context() as conn:
            conn.executescript(f"""
                CREATE TABLE {self.parent_table} (id TEXT PRIMARY KEY, value TEXT UNIQUE NOT NULL);
                CREATE TABLE {self.child_table} (
                    id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES {self.parent_table}(id) ON DELETE CASCADE,
                    value INTEGER NOT NULL CHECK(value >= 0), fraction REAL NOT NULL
                );
            """)

    def tearDown(self):
        with self.database.connect_context() as conn:
            conn.execute(f"DROP TABLE {self.child_table}")
            conn.execute(f"DROP TABLE {self.parent_table}")

    def test_committed_records_survive_new_connection_with_row_access_and_upsert(self):
        with self.database.connect_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("parent", "可持久化"))
            conn.execute(f"INSERT INTO {self.child_table} VALUES (?,?,?,?)", ("child", "parent", 2**40, 0.123456789123))
        with self.database.connect_context() as conn:
            row = conn.execute(f"SELECT id,value FROM {self.parent_table} WHERE id=?", ("parent",)).fetchone()
            self.assertEqual(row[0], row["id"])
            self.assertEqual(dict(row), {"id": "parent", "value": "可持久化"})
            conn.execute(
                f"INSERT INTO {self.child_table} VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                ("child", "parent", 2**41, 0.5),
            )
            updated = conn.execute(f"SELECT value,fraction FROM {self.child_table}").fetchone()
            self.assertEqual(updated["value"], 2**41)
            self.assertAlmostEqual(updated["fraction"], 0.123456789123, places=11)
            self.assertEqual(conn.execute(f"DELETE FROM {self.parent_table} WHERE id=?", ("parent",)).rowcount, 1)
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.child_table}").fetchone()[0], 0)

    def test_multi_statement_permission_and_record_changes_roll_back_together(self):
        with self.assertRaisesRegex(ValueError, "cancel"):
            with self.database.connect_context() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("parent", "temporary"))
                conn.execute(f"INSERT INTO {self.child_table} VALUES (?,?,?,?)", ("child", "parent", 1, 0.2))
                raise ValueError("cancel")
        with self.database.connect_context() as conn:
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.parent_table}").fetchone()[0], 0)
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.child_table}").fetchone()[0], 0)

    def test_unique_and_foreign_key_errors_preserve_integrity_error_contract(self):
        with self.database.connect_context() as conn:
            conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("first", "registered"))
        for operation, values in (
            (f"INSERT INTO {self.parent_table} VALUES (?,?)", ("second", "registered")),
            (f"INSERT INTO {self.child_table} VALUES (?,?,?,?)", ("orphan", "unknown", 1, 0.0)),
            (f"INSERT INTO {self.child_table} VALUES (?,?,?,?)", ("negative", "first", -1, 0.0)),
        ):
            with self.subTest(values=values):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.database.connect_context() as conn:
                        conn.execute(operation, values)
        with self.database.connect_context() as conn:
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.parent_table}").fetchone()[0], 1)
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {self.child_table}").fetchone()[0], 0)

    def test_writer_lock_serializes_membership_recheck_after_revocation(self):
        with self.database.connect_context() as conn:
            conn.execute(f"INSERT INTO {self.parent_table} VALUES (?,?)", ("membership", "editor"))
        first_locked, second_read, release_first, second_locked = (threading.Event() for _ in range(4))

        def revoke():
            with self.database.connect_context() as conn:
                conn.execute("BEGIN IMMEDIATE")
                first_locked.set()
                if not release_first.wait(5):
                    raise AssertionError("revocation test did not release first writer")
                conn.execute(f"DELETE FROM {self.parent_table} WHERE id=?", ("membership",))

        def write_after_read():
            if not first_locked.wait(5):
                raise AssertionError("first writer did not acquire lock")
            with self.database.connect_context() as conn:
                before = conn.execute(f"SELECT value FROM {self.parent_table} WHERE id=?", ("membership",)).fetchone()
                self.assertEqual(before[0], "editor")
                second_read.set()
                conn.execute("BEGIN IMMEDIATE")
                second_locked.set()
                # This is the API's second permission check after reading the
                # request body, not a write based on the earlier authorization.
                return conn.execute(f"SELECT value FROM {self.parent_table} WHERE id=?", ("membership",)).fetchone()

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(revoke)
            second = workers.submit(write_after_read)
            try:
                self.assertTrue(second_read.wait(5), "second writer did not complete preliminary read")
                self.assertFalse(second_locked.is_set(), "second writer bypassed the lock")
            finally:
                release_first.set()
            first.result(timeout=8)
            self.assertIsNone(second.result(timeout=8))


class SQLitePersistenceTests(PersistenceContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.temp.name) / "folio.sqlite3")
        # WAL permits the same preliminary read during an active writer as the
        # production SQLite server; transaction serialization still holds.
        with cls.database.connect_context() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()


class SqlAdaptationTests(unittest.TestCase):
    def test_parameters_bind_only_question_marks_outside_literals_and_comments(self):
        source = "SELECT ?, '50% ?; ''text''', \"why?\", $$?; 100%$$ /* ?; /* ? */ */ -- ?;\n, ?"
        query, values = _postgres_query(source, ("first", "second"))
        self.assertEqual(values, ("first", "second"))
        self.assertEqual(query.count("%s"), 2)
        self.assertIn("'50%% ?; ''text'''", query)
        self.assertIn("$$?; 100%%$$", query)
        with self.assertRaises(sqlite3.ProgrammingError):
            _postgres_query("SELECT ?", ())

    def test_schema_split_preserves_default_literals_comments_and_dollar_blocks(self):
        script = "CREATE TABLE sample (value INTEGER, note TEXT DEFAULT 'REAL; INTEGER', fraction REAL); /* ; */ SELECT $$;$$;"
        statements = list(_schema_statements(script))
        self.assertEqual(len(statements), 2)
        self.assertIn("value BIGINT", statements[0])
        self.assertIn("fraction DOUBLE PRECISION", statements[0])
        self.assertIn("'REAL; INTEGER'", statements[0])
        self.assertIn("SELECT $$;$$", statements[1])

    def test_postgres_configuration_rejects_remote_plaintext_and_keeps_secret_out_of_repr(self):
        for url in ("https://example.com/database", "postgresql://user:password@example.com/app?sslmode=disable", "postgresql://user:password@example.com/app?sslmode=prefer"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as error:
                    Database("unused", url)
                self.assertNotIn("password", str(error.exception))
        database = Database("unused", "postgresql://user:secret@example.com/app?sslmode=require")
        self.assertEqual(database.dialect, "postgresql")
        self.assertNotIn("secret", repr(database))


class PostgresConnectionDiagnosticTests(unittest.TestCase):
    class DriverError(Exception):
        def __init__(self, message, sqlstate=None):
            super().__init__(message)
            self.sqlstate = sqlstate

    def assert_safe_failure(self, driver_error, reason, sqlstate=None, transport="native", detail=None):
        secret = "do-not-expose-password"
        url = f"postgresql://owner:{secret}@private-db.example/app?sslmode=require"
        driver = SimpleNamespace(
            Error=self.DriverError,
            connect=Mock(side_effect=driver_error),
            IsolationLevel=SimpleNamespace(READ_COMMITTED="read committed"),
        )
        module = "neon_transport" if transport == "neon-ws" else "psycopg"
        with patch.dict(os.environ, {"DATABASE_TRANSPORT": transport}), patch.dict(sys.modules, {module: driver}), patch(
            "persistence.ssl.get_default_verify_paths",
            return_value=SimpleNamespace(cafile=__file__),
        ):
            with self.assertRaises(sqlite3.OperationalError) as raised:
                with Database("unused", url).connect_context():
                    self.fail("A failed cloud connection must never fall back to SQLite")
        expected = f"Cloud database connection failed (reason={reason}"
        if sqlstate:
            expected += f", sqlstate={sqlstate}"
        if detail:
            expected += f", detail={detail}"
        self.assertEqual(str(raised.exception), expected + ")")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        rendered = "".join(traceback.format_exception(raised.exception))
        for private in (secret, url, "private-db.example", "hostile-driver-detail"):
            self.assertNotIn(private, rendered)
            self.assertNotIn(private, repr(raised.exception))
        driver.connect.assert_called_once()

    def test_websocket_transport_connection_failure_preserves_safe_labels(self):
        error = self.DriverError("hostile-driver-detail do-not-expose-password", "08001")
        error.reason = "timeout"
        self.assert_safe_failure(error, "timeout", "08001", transport="neon-ws")
        error = self.DriverError("hostile-driver-detail do-not-expose-password", "28P01")
        error.reason = "unknown"
        self.assert_safe_failure(error, "authentication", "28P01", transport="neon-ws")

    def test_transport_reason_attributes_are_a_finite_enum(self):
        for reason in ("configuration", "connection_failure", "authentication", "tls_certificate", "channel_binding", "timeout", "dns", "unknown"):
            with self.subTest(reason=reason):
                error = self.DriverError("hostile-driver-detail do-not-expose-password")
                error.reason = reason
                self.assert_safe_failure(error, reason, transport="neon-ws")
        for reason in ("hostile-driver-detail do-not-expose-password", "timeout\nhostile-driver-detail", "TIMEOUT", 42, {"timeout": True}):
            with self.subTest(reason=reason):
                error = self.DriverError("hostile-driver-detail do-not-expose-password")
                error.reason = reason
                self.assert_safe_failure(error, "unknown", transport="neon-ws")

    def test_broken_transport_reason_attribute_cannot_replace_safe_diagnostics(self):
        class BrokenReasonError(self.DriverError):
            @property
            def reason(self):
                raise ValueError("hostile-driver-detail do-not-expose-password")

        self.assert_safe_failure(BrokenReasonError("connection timed out: hostile-driver-detail"), "timeout", transport="neon-ws")

    def test_protocol_details_are_a_finite_enum_for_protocol_errors_only(self):
        for detail in ("endpoint_missing", "endpoint_unknown", "endpoint_unavailable", "startup_parameters", "protocol_version", "protocol_message", "unknown"):
            with self.subTest(detail=detail):
                error = self.DriverError("hostile-driver-detail do-not-expose-password", "08P01")
                error.reason = "connection_failure"
                error.detail = detail
                self.assert_safe_failure(error, "connection_failure", "08P01", transport="neon-ws", detail=detail)
        for state, reason in (("08006", "connection_failure"), ("28P01", "authentication"), ("SECRT", "unknown"), (None, "unknown")):
            with self.subTest(state=state):
                error = self.DriverError("hostile-driver-detail do-not-expose-password", state)
                error.detail = "endpoint_missing"
                expected_state = state if state in ("08006", "28P01") else None
                self.assert_safe_failure(error, reason, expected_state, transport="neon-ws")

    def test_hostile_and_broken_protocol_detail_attributes_are_not_exposed(self):
        class StringSubclass(str):
            def __str__(self):
                raise ValueError("hostile-driver-detail do-not-expose-password")

        for detail in ("hostile-driver-detail do-not-expose-password", "endpoint_missing\nhostile-driver-detail", "ENDPOINT_MISSING", 42, {"endpoint_missing": True}, StringSubclass("endpoint_missing")):
            with self.subTest(detail_type=type(detail).__name__):
                error = self.DriverError("hostile-driver-detail do-not-expose-password", "08P01")
                error.detail = detail
                self.assert_safe_failure(error, "connection_failure", "08P01", transport="neon-ws")

        class BrokenDetailError(self.DriverError):
            @property
            def detail(self):
                raise ValueError("hostile-driver-detail do-not-expose-password")

        self.assert_safe_failure(BrokenDetailError("hostile-driver-detail", "08P01"), "connection_failure", "08P01", transport="neon-ws")

    def test_libpq_network_tls_and_channel_binding_failures_have_finite_labels(self):
        private = "hostile-driver-detail postgresql://owner:do-not-expose-password@private-db.example/app"
        cases = (
            ("connection timeout expired", "timeout"),
            ("could not translate host name", "dns"),
            ("SSL error: certificate verify failed", "tls_certificate"),
            ('server certificate for "internal" does not match host name "other"', "tls_certificate"),
            ("channel binding required, but server did not offer it", "channel_binding"),
            ("password authentication failed", "authentication"),
            ("connection refused", "connection_failure"),
            ("unsupported startup parameter: options", "configuration"),
            ("an unrecognized failure", "unknown"),
        )
        for detail, reason in cases:
            with self.subTest(reason=reason):
                self.assert_safe_failure(self.DriverError(f"{detail}: {private}"), reason)

    def test_only_known_connection_sqlstates_are_emitted(self):
        for state, reason in (("28P01", "authentication"), ("28000", "authentication"), ("08006", "connection_failure"), ("0A000", "configuration"), ("3D000", "configuration")):
            with self.subTest(state=state):
                self.assert_safe_failure(self.DriverError("hostile-driver-detail", state), reason, state)
        for state in ("SECRT", "23505", "28p01", "28P01\nhostile-driver-detail", 28000, {"sqlstate": "28P01"}):
            with self.subTest(state=state):
                self.assert_safe_failure(self.DriverError("hostile-driver-detail", state), "unknown")

    def test_timeout_can_preserve_an_allowlisted_connection_sqlstate(self):
        self.assert_safe_failure(self.DriverError("connection timed out: hostile-driver-detail", "08001"), "timeout", "08001")

    def test_invalid_connection_parameters_have_safe_configuration_label(self):
        self.assert_safe_failure(ValueError("invalid parameter: hostile-driver-detail do-not-expose-password"), "configuration")

    def test_broken_exception_attributes_cannot_replace_safe_diagnostics(self):
        class BrokenDiagnosticError(self.DriverError):
            def __init__(self):
                Exception.__init__(self)

            @property
            def sqlstate(self):
                raise ValueError("hostile-driver-detail do-not-expose-password")

            def __str__(self):
                raise ValueError("hostile-driver-detail do-not-expose-password")

        self.assert_safe_failure(BrokenDiagnosticError(), "unknown")


class PostgresTransportSelectionTests(unittest.TestCase):
    url = "postgresql://owner:do-not-expose-password@ep-example.ap-southeast-1.aws.neon.tech/app?sslmode=require"

    class DriverError(Exception):
        pass

    class DriverIntegrityError(DriverError):
        pass

    def driver(self):
        cursor = Mock(description=[SimpleNamespace(name="answer")], rowcount=1)
        cursor.fetchone.return_value = (42,)
        raw = Mock()
        raw.execute.return_value = cursor
        driver = SimpleNamespace(
            Error=self.DriverError,
            IntegrityError=self.DriverIntegrityError,
            IsolationLevel=SimpleNamespace(READ_COMMITTED="read committed"),
            connect=Mock(return_value=raw),
        )
        return driver, raw

    def test_native_is_the_default_and_websocket_is_explicitly_selected(self):
        for transport, module, other_module in ((None, "psycopg", "neon_transport"), ("native", "psycopg", "neon_transport"), ("neon-ws", "neon_transport", "psycopg")):
            with self.subTest(transport=transport), patch.dict(os.environ, {}, clear=True):
                if transport is not None:
                    os.environ["DATABASE_TRANSPORT"] = transport
                driver, raw = self.driver()
                # The unselected dependency is unavailable: selection must be
                # lazy and cannot silently fall back to another driver.
                with patch.dict(sys.modules, {module: driver, other_module: None}), patch(
                    "persistence.ssl.get_default_verify_paths", return_value=SimpleNamespace(cafile=__file__),
                ):
                    with Database("unused", self.url).connect_context() as conn:
                        self.assertEqual(conn.execute("SELECT ? AS answer", (42,)).fetchone()["answer"], 42)
                driver.connect.assert_called_once_with(
                    self.url, connect_timeout=10,
                    options="-c statement_timeout=15000 -c lock_timeout=15000 -c idle_in_transaction_session_timeout=30000",
                    autocommit=False, sslmode="verify-full", sslrootcert=__file__,
                )
                self.assertEqual(raw.isolation_level, "read committed")
                raw.execute.assert_called_once_with("SELECT %s AS answer", (42,))
                raw.commit.assert_called_once_with()
                raw.rollback.assert_not_called()
                raw.close.assert_called_once_with()

    def test_missing_selected_dependency_fails_without_a_different_driver_or_sqlite(self):
        for transport, module, other_module, message in (
            ("native", "psycopg", "neon_transport", "psycopg dependency"),
            ("neon-ws", "neon_transport", "psycopg", "transport dependencies"),
        ):
            with self.subTest(transport=transport):
                other_driver, _ = self.driver()
                with patch.dict(os.environ, {"DATABASE_TRANSPORT": transport}), patch.dict(sys.modules, {module: None, other_module: other_driver}), patch("persistence.sqlite3.connect") as sqlite_connect:
                    with self.assertRaisesRegex(RuntimeError, message) as raised:
                        with Database("unused", self.url).connect_context():
                            self.fail("Missing cloud dependency must fail closed")
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertNotIn(self.url, str(raised.exception))
                other_driver.connect.assert_not_called()
                sqlite_connect.assert_not_called()

    def test_invalid_postgres_transport_fails_before_connection(self):
        for transport in ("", "http", "NEON-WS", "native ", "hostile-driver-detail"):
            with self.subTest(transport=transport), patch.dict(os.environ, {"DATABASE_TRANSPORT": transport}), patch("persistence.sqlite3.connect") as sqlite_connect:
                with self.assertRaisesRegex(ValueError, "DATABASE_TRANSPORT must be native or neon-ws") as raised:
                    Database("unused", self.url)
                self.assertNotIn("hostile-driver-detail", str(raised.exception))
                sqlite_connect.assert_not_called()

    def test_sqlite_ignores_transport_and_does_not_load_cloud_dependencies(self):
        for transport in ("native", "neon-ws", "not-a-cloud-transport"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"DATABASE_TRANSPORT": transport}), patch.dict(sys.modules, {"psycopg": None, "neon_transport": None}):
                database = Database(Path(root) / "local.sqlite3")
                self.assertEqual(database.dialect, "sqlite")
                with database.connect_context() as conn:
                    conn.execute("CREATE TABLE local (id INTEGER PRIMARY KEY)")
                    conn.execute("INSERT INTO local VALUES (?)", (7,))
                with database.connect_context() as conn:
                    self.assertEqual(conn.execute("SELECT id FROM local").fetchone()[0], 7)

    def test_websocket_mode_preserves_tls_guard_even_before_driver_is_loaded(self):
        with patch.dict(os.environ, {"DATABASE_TRANSPORT": "neon-ws"}), patch.dict(sys.modules, {"neon_transport": None}):
            with self.assertRaisesRegex(ValueError, "TLS for remote hosts"):
                Database("unused", self.url.replace("sslmode=require", "sslmode=disable"))

    def test_websocket_operation_integrity_errors_roll_back_with_sanitized_contract(self):
        driver, raw = self.driver()
        raw.execute.side_effect = self.DriverIntegrityError("private row and do-not-expose-password")
        with patch.dict(os.environ, {"DATABASE_TRANSPORT": "neon-ws"}), patch.dict(sys.modules, {"neon_transport": driver}), patch(
            "persistence.ssl.get_default_verify_paths", return_value=SimpleNamespace(cafile=__file__),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Database integrity constraint failed") as raised:
                with Database("unused", self.url).connect_context() as conn:
                    conn.execute("INSERT INTO sample VALUES (?)", ("private row",))
        self.assertNotIn("private row", "".join(traceback.format_exception(raised.exception)))
        self.assertTrue(raised.exception.__suppress_context__)
        raw.commit.assert_not_called()
        raw.rollback.assert_called_once_with()
        raw.close.assert_called_once_with()

    def test_isolation_setup_failure_closes_session_and_preserves_safe_error(self):
        class FailedSetup:
            def __init__(self, error, close_error):
                self.error = error
                self.close = Mock(side_effect=close_error)

            @property
            def isolation_level(self):
                return None

            @isolation_level.setter
            def isolation_level(self, value):
                raise self.error

        for transport, module in (("native", "psycopg"), ("neon-ws", "neon_transport")):
            for close_error in (None, RuntimeError("private-close-data do-not-expose-password")):
                with self.subTest(transport=transport, close_failure=close_error is not None):
                    driver, _ = self.driver()
                    raw = FailedSetup(self.DriverError("connection timed out with private-setup-data do-not-expose-password"), close_error)
                    driver.connect.return_value = raw
                    with patch.dict(os.environ, {"DATABASE_TRANSPORT": transport}), patch.dict(sys.modules, {module: driver}), patch(
                        "persistence.ssl.get_default_verify_paths", return_value=SimpleNamespace(cafile=__file__),
                    ):
                        with self.assertRaises(sqlite3.OperationalError) as raised:
                            with Database("unused", self.url).connect_context():
                                self.fail("Session setup did not complete")
                    self.assertEqual(str(raised.exception), "Cloud database connection failed (reason=timeout)")
                    rendered = "".join(traceback.format_exception(raised.exception))
                    for value in ("private-setup-data", "private-close-data", "do-not-expose-password"):
                        self.assertNotIn(value, rendered)
                    self.assertTrue(raised.exception.__suppress_context__)
                    raw.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
