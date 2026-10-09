"""Persistence contract checks; no external accounts or provider calls."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
