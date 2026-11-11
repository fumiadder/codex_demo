#!/usr/bin/env python3
"""Offline, explicit SQLite-to-Postgres migration with a private snapshot.

The source is always read-only. Cloud adapters are imported only when requested;
the default operation produces a checked snapshot and aggregate audit counts.
No personal rows, object keys, filenames or credentials are printed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# Explicit columns preserve IDs, password hashes and vault ciphertext verbatim.
# Parent tables precede every referencing table.
TABLE_COLUMNS = {
    "users": ("id", "email", "name", "password_hash", "created_at"),
    "spaces": ("id", "name", "owner_id", "created_at"),
    "members": ("space_id", "user_id", "role"),
    "sessions": ("token_hash", "user_id", "expires_at"),
    "registration_invites": ("token_hash", "email", "expires_at", "consumed_at", "bootstrap", "created_at"),
    "items": ("id", "space_id", "kind", "area", "title", "body", "status", "meta", "created_by", "created_at", "updated_at"),
    "files": ("id", "space_id", "item_id", "name", "mime", "size", "created_at"),
    "vaults": ("space_id", "user_id", "payload", "updated_at"),
    "bedtime_favorites": ("space_id", "user_id", "story_id", "story", "saved_at"),
    "bedtime_history": ("id", "space_id", "user_id", "story_id", "story", "voice_id", "position_seconds", "played_at"),
    "bedtime_voices": ("id", "space_id", "user_id", "name", "state", "provider_voice", "target_model", "created_at"),
    "bedtime_audio": ("id", "space_id", "user_id", "size", "created_at"),
}
MEDIA_TABLES = {"files": "uploads", "bedtime_audio": "bedtime-audio"}
FILE_ID = re.compile(r"[a-f0-9]{32}\Z")


class MigrationError(Exception):
    """A fixed, non-personal explanation safe for terminal output."""


@dataclass(frozen=True)
class ObjectRecord:
    directory: str
    identifier: str
    size: int
    sha256: str


@dataclass
class Snapshot:
    root: Path
    counts: dict[str, int]
    objects: list[ObjectRecord]

    @property
    def database(self):
        return self.root / "data" / "folio.sqlite3"

    def summary(self):
        return {"tables": self.counts, "objects": len(self.objects),
                "objectBytes": sum(item.size for item in self.objects)}


def readonly_database(path: Path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError("Source database is missing or unsafe")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_private_directory(path: Path, *, new=False):
    if path.is_symlink():
        raise MigrationError("Snapshot directory is unsafe")
    if new:
        try:
            path.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            raise MigrationError("Snapshot destination must be new") from None
    if not path.is_dir():
        raise MigrationError("Snapshot directory is missing")
    os.chmod(path, 0o700)


def private_json(path: Path, payload):
    # Restrictive mode is applied at creation, before private data is written.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def sha256_file(path: Path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError("Snapshot object is missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_database(conn):
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise MigrationError("SQLite integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MigrationError("Source has invalid foreign-key references")
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables not in (set(TABLE_COLUMNS), set(TABLE_COLUMNS) | {'storage_jobs'}):
        raise MigrationError("Source schema is incomplete or unsupported")
    if 'storage_jobs' in tables and conn.execute("SELECT COUNT(*) FROM storage_jobs").fetchone()[0]:
        raise MigrationError("Source has pending media operations; complete cleanup before snapshot")
    counts = {}
    for table, columns in TABLE_COLUMNS.items():
        found = tuple(row[1] for row in conn.execute("PRAGMA table_info(" + table + ")"))
        if found != columns:
            raise MigrationError("Source schema columns are unsupported")
        counts[table] = conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    return counts


def copy_object(source: Path, destination: Path, expected_size: int):
    if not isinstance(expected_size, int) or expected_size < 0:
        raise MigrationError("Source object has an invalid size")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError:
        raise MigrationError("Referenced source object is missing or unsafe") from None
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise MigrationError("Referenced source object has an invalid size")
        output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        output_fd = os.open(destination, output_flags, 0o600)
        digest, copied = hashlib.sha256(), 0
        with os.fdopen(output_fd, "wb") as output:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                copied += len(chunk)
                output.write(chunk)
        after = os.fstat(handle.fileno())
        if (copied != expected_size or before.st_mtime_ns != after.st_mtime_ns
                or before.st_size != after.st_size):
            raise MigrationError("Source objects changed; stop writes and retry")
        return digest.hexdigest()


def create_snapshot(source_data: Path, destination: Path):
    source_data, destination = Path(source_data), Path(destination)
    if source_data.is_symlink() or not source_data.is_dir():
        raise MigrationError("Source data directory is missing or unsafe")
    source_root, output_root = source_data.resolve(), destination.resolve()
    if output_root == source_root or source_root in output_root.parents:
        raise MigrationError("Snapshot cannot be created inside source data")
    ensure_private_directory(destination, new=True)
    data = destination / "data"
    data.mkdir(mode=0o700)
    database = data / "folio.sqlite3"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    # SQLite's backup API captures a consistent committed database, including
    # WAL data, without checkpointing, modifying, or deleting the source.
    source = readonly_database(source_data / "folio.sqlite3")
    target = sqlite3.connect(database)
    try:
        source.backup(target, pages=256)
    finally:
        target.close()
        source.close()
    snapshot_conn = readonly_database(database)
    objects = []
    try:
        counts = audit_database(snapshot_conn)
        for table, directory in MEDIA_TABLES.items():
            (data / directory).mkdir(mode=0o700)
            if (source_data / directory).is_symlink():
                raise MigrationError("Source media directory is unsafe")
            for identifier, size in snapshot_conn.execute("SELECT id,size FROM " + table):
                if not isinstance(identifier, str) or not FILE_ID.fullmatch(identifier):
                    raise MigrationError("Source object identifier is invalid")
                digest = copy_object(source_data / directory / identifier,
                                     data / directory / identifier, size)
                objects.append(ObjectRecord(directory, identifier, size, digest))
    finally:
        snapshot_conn.close()
    snapshot = Snapshot(destination, counts, objects)
    private_json(destination / "manifest.json", {
        "version": 1, "counts": counts, "databaseSha256": sha256_file(database),
        "objects": [vars(item) for item in objects],
    })
    return snapshot


def verify_snapshot(snapshot: Snapshot):
    ensure_private_directory(snapshot.root)
    try:
        manifest = json.loads((snapshot.root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MigrationError("Snapshot manifest is invalid") from None
    expected = {"version": 1, "counts": snapshot.counts,
                "databaseSha256": sha256_file(snapshot.database),
                "objects": [vars(item) for item in snapshot.objects]}
    if manifest != expected:
        raise MigrationError("Snapshot manifest verification failed")
    conn = readonly_database(snapshot.database)
    try:
        if audit_database(conn) != snapshot.counts:
            raise MigrationError("Snapshot table counts changed")
    finally:
        conn.close()
    for item in snapshot.objects:
        path = snapshot.root / "data" / item.directory / item.identifier
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item.size:
            raise MigrationError("Snapshot object size verification failed")
        if sha256_file(path) != item.sha256:
            raise MigrationError("Snapshot object checksum verification failed")
    return True


def require_empty_target(conn):
    for table in TABLE_COLUMNS:
        if conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]:
            raise MigrationError("Target database is not empty; overwrite is refused")
    tables = ({row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
              if getattr(conn, "dialect", None) != "postgresql" else
              {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")})
    if "storage_jobs" in tables and conn.execute("SELECT COUNT(*) FROM storage_jobs").fetchone()[0]:
        raise MigrationError("Target has pending media operations; overwrite is refused")


def rows_equal(snapshot: Snapshot, conn):
    """Verify all migrated values, including ciphertext, without printing them."""
    source = readonly_database(snapshot.database)
    try:
        for table, columns in TABLE_COLUMNS.items():
            order = ",".join(columns)
            query = "SELECT " + order + " FROM " + table + " ORDER BY " + order
            source_cursor, target_cursor = source.execute(query), conn.execute(query)
            while True:
                left, right = source_cursor.fetchone(), target_cursor.fetchone()
                if left is None and right is None:
                    break
                if left is None or right is None or tuple(left) != tuple(right):
                    raise MigrationError("Target row verification failed")
    finally:
        source.close()


def migrate_snapshot(snapshot: Snapshot, target_transaction: Callable,
                     media, *, apply=False, maintenance_confirmed=False):
    """Copy into a locked empty target; inject only reviewed backend adapters.

    target_transaction yields the app-compatible connection (``?`` parameters)
    and commits on success/rolls back on exceptions. Source and target apps must
    be stopped. Media must implement exists(record), create(record, path),
    verify(record), remove(record). A cloud backend without atomic create-only
    PUT additionally requires a dedicated new bucket and exclusive ownership.
    """
    verify_snapshot(snapshot)
    with target_transaction() as conn:
        require_empty_target(conn)
    for item in snapshot.objects:
        if media.exists(item):
            raise MigrationError("Target contains a migration object; overwrite is refused")
    if not apply:
        return {"status": "dry-run", **snapshot.summary()}
    if not maintenance_confirmed:
        raise MigrationError("Apply requires explicit maintenance confirmation")

    created = []
    journal = snapshot.root / "migration-journal.json"
    if journal.exists() or journal.is_symlink():
        raise MigrationError("Migration journal already exists; inspect before retrying")
    private_json(journal, {"version": 1, "status": "uploading", "createdObjects": []})
    # The journal is private. Only keys owned by THIS attempt are eligible for
    # rollback; existing target objects are never removed.
    def update_journal(status):
        replacement = snapshot.root / ".migration-journal.tmp"
        private_json(replacement, {"version": 1, "status": status,
                                  "createdObjects": [vars(item) for item in created]})
        os.replace(replacement, journal)

    committed, database_phase = False, False
    try:
        with target_transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if getattr(conn, "dialect", None) == "postgresql":
                # This is an offline maintenance operation, not an application
                # request. Keep the writer lock during media transfer. SET LOCAL
                # does not alter the normal runtime's timeout after this context.
                conn.execute("SET LOCAL idle_in_transaction_session_timeout=0")
            require_empty_target(conn)
            for item in snapshot.objects:
                # Recheck immediately before PUT. The S3 protocol available on
                # this platform does not promise If-None-Match support, so the
                # dedicated, stopped destination is a required operator guard.
                if media.exists(item):
                    raise MigrationError("Target object appeared; overwrite is refused")
                media.create(item, snapshot.root / "data" / item.directory / item.identifier)
                created.append(item)
                update_journal("uploading")
                if not media.verify(item):
                    raise MigrationError("Uploaded object verification failed")
            database_phase = True
            update_journal("writing-database")
            source = readonly_database(snapshot.database)
            try:
                for table, columns in TABLE_COLUMNS.items():
                    names = ",".join(columns)
                    sql = "INSERT INTO " + table + " (" + names + ") VALUES (" + ",".join("?" for _ in columns) + ")"
                    for row in source.execute("SELECT " + names + " FROM " + table):
                        conn.execute(sql, tuple(row))
            finally:
                source.close()
            rows_equal(snapshot, conn)
        committed = True
        update_journal("complete")
    except Exception:
        if database_phase and not committed:
            # A commit can have reached the database even if its reply was
            # lost. Keep media until an operator verifies the actual DB result.
            update_journal("database-outcome-needs-verification")
        elif not committed:
            failures = 0
            for item in reversed(created):
                try:
                    media.remove(item)
                except Exception:
                    failures += 1
            update_journal("rollback-needs-cleanup" if failures else "rolled-back")
        # Even a credential, SQL, or storage-library exception must not expose
        # a row, URL, key, credential, or filesystem name via terminal output.
        raise MigrationError("Migration failed; inspect the private journal before retrying") from None
    return {"status": "complete", **snapshot.summary()}


class RuntimeMedia:
    """Reviewed application's media API adapted to the migration engine."""

    def __init__(self, storage):
        self.storage = storage

    def exists(self, item):
        try:
            self.storage.stat(item.directory, item.identifier)
            return True
        except FileNotFoundError:
            return False

    def create(self, item, path):
        # Preserve stored MIME in SQL; the authorized response also derives its
        # content type there. Objects themselves use a conservative binary MIME.
        self.storage.put(item.directory, item.identifier, path.read_bytes(),
                         "application/octet-stream")

    def verify(self, item):
        info = self.storage.stat(item.directory, item.identifier)
        if info.size != item.size:
            return False
        digest = hashlib.sha256()
        with self.storage.open(item.directory, item.identifier) as stream:
            for chunk in iter(lambda: stream.body.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == item.sha256

    def remove(self, item):
        return self.storage.delete(item.directory, item.identifier)


def app_schemas():
    """Read schema literals without starting the server or external providers."""
    application = Path(__file__).resolve().parents[1]
    schemas = []
    for name in ("server.py", "bedtime.py", "storage_jobs.py"):
        module = ast.parse((application / name).read_text(encoding="utf-8"))
        for node in module.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "SCHEMA" for target in node.targets):
                schemas.append(ast.literal_eval(node.value))
                break
        else:
            raise MigrationError("Application schema is unavailable")
    return "\n".join(schemas)


def cloud_target(snapshot, *, apply, exclusive_bucket_confirmed):
    """Use the same cloud configuration as the application, never URL arguments."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from persistence import Database
    from media_storage import create_storage

    url = os.environ.get("DATABASE_URL")
    if not url or os.environ.get("MEDIA_STORAGE_BACKEND") != "s3":
        raise MigrationError("Cloud migration requires DATABASE_URL and MEDIA_STORAGE_BACKEND=s3")
    if apply and not exclusive_bucket_confirmed:
        raise MigrationError("Apply requires confirmation of dedicated new buckets with no other writers")
    database = Database(snapshot.root / "unused-local-database", database_url=url)
    media = create_storage(snapshot.root / "unused-local-media")
    with database.connect_context() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'")}
        expected_tables = set(TABLE_COLUMNS) | {"storage_jobs"}
        if tables - expected_tables:
            raise MigrationError("Target contains unsupported tables; choose a new empty database")
        # Existing tables are checked BEFORE any schema statement is executed.
        for table in tables:
            if conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]:
                raise MigrationError("Target database is not empty; overwrite is refused")
        if tables != expected_tables:
            if not apply:
                raise MigrationError("Target schema is not initialized; source snapshot audit passed")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(app_schemas())
    return database.connect_context, RuntimeMedia(media)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", required=True, type=Path)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="explicitly permit remote writes")
    parser.add_argument("--maintenance-confirmed", action="store_true",
                        help="confirm source writes and target application are stopped")
    parser.add_argument("--check-target", action="store_true",
                        help="also perform read-only cloud DB and object preflight")
    parser.add_argument("--exclusive-buckets-confirmed", action="store_true",
                        help="confirm dedicated new buckets and no other storage writers")
    arguments = parser.parse_args(argv)
    try:
        if arguments.apply and not arguments.maintenance_confirmed:
            raise MigrationError("Apply requires explicit maintenance confirmation")
        snapshot = create_snapshot(arguments.source_data, arguments.snapshot_dir)
        verify_snapshot(snapshot)
        result = {"status": "source-audit", **snapshot.summary()}
        if arguments.apply or arguments.check_target:
            transaction, media = cloud_target(snapshot, apply=arguments.apply,
                exclusive_bucket_confirmed=arguments.exclusive_buckets_confirmed)
            result = migrate_snapshot(snapshot, transaction, media, apply=arguments.apply,
                maintenance_confirmed=arguments.maintenance_confirmed)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, MigrationError) else "Operation failed; no source data was changed"
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
