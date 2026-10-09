"""Optional real PostgreSQL contract checks on an explicitly supplied test DB.

Run with TEST_POSTGRES_URL pointing to a disposable local PostgreSQL database.
These checks create only uniquely named probe tables and drop them afterwards.
The connection URL and credentials are never printed.
"""
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_persistence import PersistenceContract
from persistence import Database


@unittest.skipUnless(os.environ.get("TEST_POSTGRES_URL"), "TEST_POSTGRES_URL is not configured")
class PostgresPersistenceTests(PersistenceContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = Database("unused", os.environ["TEST_POSTGRES_URL"])

    def test_schema_script_is_one_native_transaction(self):
        new_table = self.prefix + "_atomic"
        with self.assertRaises(sqlite3.OperationalError):
            with self.database.connect_context() as conn:
                conn.executescript(f"CREATE TABLE {new_table} (id TEXT); INVALID SQL;")
        with self.database.connect_context() as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass(?)", (new_table,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
