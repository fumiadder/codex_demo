"""Real SQLite snapshots and local-media migration failure/permission checks."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_storage import LocalStorage
from persistence import Database
from scripts.migrate_storage import (MigrationError, RuntimeMedia, TABLE_COLUMNS,
    app_schemas, create_snapshot, main, migrate_snapshot, verify_snapshot)


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = sqlite3.connect(self.source / "folio.sqlite3")
        self.database.execute("PRAGMA foreign_keys=ON")
        self.database.execute("PRAGMA journal_mode=WAL")
        self.database.executescript(app_schemas())
        self.uid, self.sid, self.iid, self.fid, self.aid = (character * 32 for character in "abcde")
        rows = {
            "users": (self.uid, "private@example.com", "私人姓名", "unchanged-password-hash", "2026-10-09"),
            "spaces": (self.sid, "私密空间", self.uid, "2026-10-09"),
            "members": (self.sid, self.uid, "owner"),
            "sessions": ("f" * 64, self.uid, 123),
            "registration_invites": ("a" * 64, "invited@example.com", 123, None, 0, "2026-10-09"),
            "items": (self.iid, self.sid, "media", "life", "私密标题", "", "active", "{}", self.uid, "2026-10-09", "2026-10-09"),
            "files": (self.fid, self.sid, self.iid, "私人录音.wav", "audio/wav", 5, "2026-10-09"),
            "vaults": (self.sid, self.uid, '{"ciphertext":"never-decrypt-me"}', "2026-10-09"),
            "bedtime_favorites": (self.sid, self.uid, "story-1", '{"text":"私人故事"}', "2026-10-09"),
            "bedtime_history": ("0" * 32, self.sid, self.uid, "story-1", '{"text":"听过的故事"}', "voice-local", 3.25, "2026-10-09"),
            "bedtime_voices": ("1" * 32, self.sid, self.uid, "私人人声", "ready", "provider-private-id", "target", "2026-10-09"),
            "bedtime_audio": (self.aid, self.sid, self.uid, 7, 123),
        }
        for table, values in rows.items():
            self.database.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in values) + ")", values)
        self.database.commit()
        for directory, identifier, payload in (("uploads", self.fid, b"media"), ("bedtime-audio", self.aid, b"private")):
            (self.source / directory).mkdir()
            (self.source / directory / identifier).write_bytes(payload)
        self.target = Database(self.root / "target.sqlite3")
        with self.target.connect_context() as conn:
            conn.executescript(app_schemas())
        self.storage = LocalStorage(self.root / "target-media")
        self.media = RuntimeMedia(self.storage)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def snapshot(self):
        return create_snapshot(self.source, self.root / "snapshot")

    def row_count(self):
        with self.target.connect_context() as conn:
            return sum(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in TABLE_COLUMNS)

    def test_backup_includes_wal_and_does_not_mutate_source(self):
        # SQLite readers update shared-memory lock metadata, including mode=ro.
        # The database and WAL contain user data; SHM is a transient lock index.
        before = {path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file() and not path.name.endswith('-shm')}
        snapshot = self.snapshot()
        self.assertEqual(snapshot.counts, {table: 1 for table in TABLE_COLUMNS})
        self.assertTrue(verify_snapshot(snapshot))
        after = {path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file() and not path.name.endswith('-shm')}
        self.assertEqual(before, after)

    def test_snapshot_files_are_private(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(snapshot.database.stat().st_mode & 0o777, 0o600)
        self.assertEqual((snapshot.root / "manifest.json").stat().st_mode & 0o777, 0o600)
        for item in snapshot.objects:
            self.assertEqual((snapshot.root / "data" / item.directory / item.identifier).stat().st_mode & 0o777, 0o600)

    def test_default_cli_is_source_only_and_output_has_no_personal_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"DATABASE_URL": "secret-credential", "MEDIA_STORAGE_BACKEND": "s3"}), redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--source-data", str(self.source), "--snapshot-dir", str(self.root / "snapshot")])
        self.assertEqual(result, 0, stderr.getvalue())
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["status"], "source-audit")
        self.assertEqual(value["objects"], 2)
        for private in (self.uid, self.sid, self.fid, "private@example.com", "ciphertext", "私人", str(self.root), "secret-credential"):
            self.assertNotIn(private, stdout.getvalue() + stderr.getvalue())

    def test_cli_apply_requires_maintenance_before_snapshot_or_remote_calls(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--source-data", str(self.source), "--snapshot-dir", str(self.root / "snapshot"), "--apply"]), 2)
        self.assertFalse((self.root / "snapshot").exists())

    def test_dry_run_never_writes_target_database_or_media(self):
        snapshot = self.snapshot()
        result = migrate_snapshot(snapshot, self.target.connect_context, self.media)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(self.row_count(), 0)
        self.assertFalse(self.media.exists(snapshot.objects[0]))
        self.assertFalse((snapshot.root / "migration-journal.json").exists())

    def test_apply_preserves_all_twelve_tables_and_media(self):
        snapshot = self.snapshot()
        result = migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.row_count(), 12)
        with self.target.connect_context() as conn:
            self.assertEqual(conn.execute("SELECT password_hash FROM users").fetchone()[0], "unchanged-password-hash")
            self.assertEqual(conn.execute("SELECT payload FROM vaults").fetchone()[0], '{"ciphertext":"never-decrypt-me"}')
            self.assertEqual(conn.execute("SELECT provider_voice FROM bedtime_voices").fetchone()[0], "provider-private-id")
        for item in snapshot.objects:
            self.assertTrue(self.media.verify(item))
        journal = json.loads((snapshot.root / "migration-journal.json").read_text())
        self.assertEqual(journal["status"], "complete")

    def test_nonempty_target_is_refused_without_overwrite(self):
        snapshot = self.snapshot()
        with self.target.connect_context() as conn:
            conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", ("2" * 32, "existing@example.com", "Existing", "hash", "date"))
        with self.assertRaisesRegex(MigrationError, "not empty"):
            migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(self.row_count(), 1)
        self.assertFalse(self.media.exists(snapshot.objects[0]))

    def test_existing_target_object_is_never_overwritten_or_deleted(self):
        snapshot = self.snapshot()
        item = snapshot.objects[0]
        self.storage.put(item.directory, item.identifier, b"existing")
        with self.assertRaisesRegex(MigrationError, "overwrite"):
            migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        with self.storage.open(item.directory, item.identifier) as stream:
            self.assertEqual(stream.body.read(), b"existing")
        self.assertEqual(self.row_count(), 0)

    def test_upload_failure_rolls_back_only_attempt_objects(self):
        snapshot = self.snapshot()
        actual_create = self.media.create
        calls = []
        def fail_second(item, path):
            calls.append(item)
            if len(calls) == 2:
                raise RuntimeError("credential/private data should never appear")
            return actual_create(item, path)
        with mock.patch.object(self.media, "create", side_effect=fail_second):
            with self.assertRaisesRegex(MigrationError, "inspect the private journal"):
                migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(self.row_count(), 0)
        self.assertFalse(self.media.exists(snapshot.objects[0]))
        self.assertEqual(json.loads((snapshot.root / "migration-journal.json").read_text())["status"], "rolled-back")

    def test_ambiguous_database_commit_keeps_uploaded_objects_for_reconciliation(self):
        snapshot = self.snapshot()
        calls = []
        @contextmanager
        def ambiguous_transaction():
            calls.append(True)
            with self.target.connect_context() as conn:
                yield conn
            if len(calls) == 2:
                raise RuntimeError("Commit acknowledgement lost")
        with self.assertRaisesRegex(MigrationError, "inspect the private journal"):
            migrate_snapshot(snapshot, ambiguous_transaction, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(self.row_count(), 12)
        self.assertTrue(all(self.media.exists(item) for item in snapshot.objects))
        self.assertEqual(json.loads((snapshot.root / "migration-journal.json").read_text())["status"], "database-outcome-needs-verification")

    def test_database_insertion_error_is_atomic_and_retains_media_until_verified(self):
        snapshot = self.snapshot()
        with self.target.connect_context() as conn:
            conn.execute("CREATE TRIGGER reject_rows BEFORE INSERT ON items BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(MigrationError):
            migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(self.row_count(), 0)
        self.assertTrue(all(self.media.exists(item) for item in snapshot.objects))

    def test_missing_source_object_is_refused(self):
        (self.source / "uploads" / self.fid).unlink()
        with self.assertRaisesRegex(MigrationError, "missing or unsafe"):
            self.snapshot()

    def test_symlink_source_object_is_refused(self):
        original = self.source / "uploads" / self.fid
        original.unlink()
        original.symlink_to(self.source / "bedtime-audio" / self.aid)
        with self.assertRaisesRegex(MigrationError, "missing or unsafe"):
            self.snapshot()

    def test_unknown_table_is_refused_instead_of_silently_lost(self):
        self.database.execute("CREATE TABLE future_private_data (payload TEXT)")
        self.database.commit()
        with self.assertRaisesRegex(MigrationError, "unsupported"):
            self.snapshot()

    def test_invalid_foreign_keys_are_refused(self):
        self.database.execute("PRAGMA foreign_keys=OFF")
        self.database.execute("UPDATE members SET user_id=?", ("9" * 32,))
        self.database.commit()
        with self.assertRaisesRegex(MigrationError, "foreign-key"):
            self.snapshot()

    def test_corrupted_snapshot_object_is_refused_before_target_changes(self):
        snapshot = self.snapshot()
        item = snapshot.objects[0]
        (snapshot.root / "data" / item.directory / item.identifier).write_bytes(b"wrong")
        with self.assertRaisesRegex(MigrationError, "checksum"):
            migrate_snapshot(snapshot, self.target.connect_context, self.media, apply=True, maintenance_confirmed=True)
        self.assertEqual(self.row_count(), 0)


if __name__ == "__main__":
    unittest.main()
