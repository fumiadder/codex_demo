"""Durable upload reservations and retryable private-object deletion.

Object I/O always happens outside database transactions. A reservation counts
towards quota until publication or confirmed deletion, including after a crash.
"""
from __future__ import annotations

import logging
import time
import uuid

from media_storage import StorageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_jobs (
 namespace TEXT NOT NULL CHECK(namespace IN ('uploads','bedtime-audio')),
 id TEXT NOT NULL, space_id TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
 state TEXT NOT NULL CHECK(state IN ('pending','delete')), created_at INTEGER NOT NULL,
 generation TEXT NOT NULL,
 PRIMARY KEY(namespace,id)
);
CREATE INDEX IF NOT EXISTS storage_jobs_state ON storage_jobs(state,created_at);
"""
UPLOAD_TTL = 3600
COLLECTION_BATCH = 4


def reserve(conn, namespace, identifier, sid, size):
    conn.execute("INSERT INTO storage_jobs VALUES(?,?,?,?,?,?,?)",
                 (namespace, identifier, sid, size, "pending", int(time.time()), uuid.uuid4().hex))


def assert_pending(conn, namespace, identifier):
    if not conn.execute("SELECT 1 FROM storage_jobs WHERE namespace=? AND id=? AND state='pending'",
                        (namespace, identifier)).fetchone():
        raise StorageError("Upload reservation expired")


def publish(conn, namespace, identifier):
    assert_pending(conn, namespace, identifier)
    conn.execute("DELETE FROM storage_jobs WHERE namespace=? AND id=?", (namespace, identifier))


def queue_delete(conn, namespace, identifier, sid, size):
    conn.execute("INSERT INTO storage_jobs VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,id) "
                 "DO UPDATE SET state='delete',created_at=excluded.created_at,generation=excluded.generation",
                 (namespace, identifier, sid, size, "delete", int(time.time()), uuid.uuid4().hex))


def linked(conn, namespace, identifier):
    table = "files" if namespace == "uploads" else "bedtime_audio"
    return conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (identifier,)).fetchone() is not None


def abandon(server, namespace, identifier, sid, size):
    """Resolve failed/uncertain commits before touching an uploaded object."""
    try:
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if linked(conn, namespace, identifier):
                # Commit might have succeeded before its acknowledgement failed.
                return
            # A stale-reservation sweep may have completed while PUT was slow.
            # Recreate its deletion record to account for a late object arrival.
            queue_delete(conn, namespace, identifier, sid, size)
        server.cleanup_media()
    except Exception:
        # A retained reservation is safer than deleting possibly committed data.
        logging.warning("Media cleanup deferred; reservation retained")


def usage(conn, sid):
    space, total = 0, 0
    for table in ("files", "bedtime_audio", "storage_jobs"):
        space += int(conn.execute(f"SELECT COALESCE(sum(size),0) FROM {table} WHERE space_id=?", (sid,)).fetchone()[0])
        total += int(conn.execute(f"SELECT COALESCE(sum(size),0) FROM {table}").fetchone()[0])
    return space, total


def expire_audio_rows(conn, ttl):
    cutoff = int(time.time()) - ttl
    for row in conn.execute("SELECT * FROM bedtime_audio WHERE created_at<?", (cutoff,)).fetchall():
        queue_delete(conn, "bedtime-audio", row["id"], row["space_id"], row["size"])
    conn.execute("DELETE FROM bedtime_audio WHERE created_at<?", (cutoff,))


def collect(server, *, expire_audio=True):
    # A single process owns object cleanup; DB state still serializes writers.
    if not server.storage_cleanup_lock.acquire(blocking=False):
        return
    try:
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expire_audio:
                from bedtime import AUDIO_TTL
                expire_audio_rows(conn, AUDIO_TTL)
            stale = conn.execute("SELECT * FROM storage_jobs WHERE state='pending' AND created_at<?",
                                 (int(time.time()) - UPLOAD_TTL,)).fetchall()
            for row in stale:
                if linked(conn, row["namespace"], row["id"]):
                    conn.execute("DELETE FROM storage_jobs WHERE namespace=? AND id=?", (row["namespace"], row["id"]))
                else:
                    conn.execute("UPDATE storage_jobs SET state='delete' WHERE namespace=? AND id=?", (row["namespace"], row["id"]))
            rows = conn.execute("SELECT * FROM storage_jobs WHERE state='delete' AND created_at<=? ORDER BY created_at LIMIT ?", (int(time.time()), COLLECTION_BATCH)).fetchall()
            # Detect an inconsistent queue before deleting any referenced object.
            rows = [row for row in rows if not linked(conn, row["namespace"], row["id"])]
        for row in rows:
            try:
                server.media_storage.delete(row["namespace"], row["id"])
            except StorageError:
                logging.warning("Private media deletion deferred")
                with server.db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE storage_jobs SET created_at=? WHERE namespace=? AND id=? AND state='delete' AND generation=?",
                                 (int(time.time()) + 60, row["namespace"], row["id"], row["generation"]))
                continue
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM storage_jobs WHERE namespace=? AND id=? AND state='delete' AND generation=?",
                             (row["namespace"], row["id"], row["generation"]))
    finally:
        server.storage_cleanup_lock.release()
