#!/usr/bin/env python3
"""Non-secret dotenv reader; offline SQLite/upload snapshot manifest verifier."""
import ast
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


def env(path, key, default):
    found = default
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        name, sep, value = line.partition('=')
        if sep and name.strip() == key:
            value = value.strip()
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError('Malformed environment value')
                value = value[1:-1]
            if '\x00' in value or '\n' in value or '\r' in value or '$' in value or '`' in value:
                raise ValueError('Environment settings must use literal values')
            found = value
    print(found)


def schema(path):
    # Include every server-side schema, without importing provider/runtime code.
    source = Path(path)
    parts = []
    for module in (source, source.parent/'bedtime.py'):
        if not module.exists():
            continue
        tree = ast.parse(module.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCHEMA' for t in node.targets):
                parts.append(ast.literal_eval(node.value))
                break
        else:
            raise ValueError('SCHEMA constant is unavailable')
    if not parts:
        raise ValueError('SCHEMA constant is unavailable')
    print(hashlib.sha256('\n--module-schema--\n'.join(parts).encode()).hexdigest())


def validate_references(conn, data):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, directory in (('files', 'uploads'), ('bedtime_audio', 'bedtime-audio')):
        if table not in tables:
            if table == 'files':
                raise ValueError('Snapshot upload table missing')
            continue
        for file_id, size in conn.execute('SELECT id,size FROM '+table):
            if len(file_id) != 32 or any(c not in '0123456789abcdef' for c in file_id):
                raise ValueError('Unsafe file identifier')
            item = data/directory/file_id
            if item.is_symlink() or not item.is_file() or item.stat().st_size != size:
                raise ValueError('Referenced uploaded file missing or wrong size')


def validate_data(data):
    if data.is_symlink() or not data.is_dir():
        raise ValueError('Snapshot data directory missing or unsafe')
    database = data/'folio.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise ValueError('Snapshot database missing or unsafe')
    conn = sqlite3.connect(database)
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        validate_references(conn, data)
    finally:
        # sqlite3's context manager commits but does NOT close; close before
        # hashing so transient WAL/SHM cleanup cannot change the manifest.
        conn.close()


def hashes(data):
    result = {}
    for item in sorted(data.rglob('*')):
        if item.is_symlink():
            raise ValueError('Snapshot symlinks are not allowed')
        if item.is_file():
            digest = hashlib.sha256()
            with item.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024*1024), b''):
                    digest.update(chunk)
            result[item.relative_to(data).as_posix()] = digest.hexdigest()
    return result


def snapshot(path, image):
    root = Path(path)
    validate_data(root/'data')
    payload = {'version': 1, 'image': image, 'files': hashes(root/'data')}
    (root/'manifest.json').write_text(json.dumps(payload, sort_keys=True, indent=2)+'\n')
    os.chmod(root/'manifest.json', 0o600)


def verify(path):
    root = Path(path)
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('files') != hashes(root/'data'):
        raise ValueError('Snapshot checksum verification failed')
    # Read-only integrity check must not change the files whose hashes were
    # verified. Snapshot creation already checkpointed its SQLite WAL.
    conn = sqlite3.connect(f'file:{(root/"data/folio.sqlite3").resolve()}?mode=ro&immutable=1', uri=True)
    try:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        validate_references(conn, root/'data')
    finally:
        conn.close()
    print('PASS snapshot checksums, SQLite integrity, and uploaded file references')


if __name__ == '__main__':
    try:
        command, *args = sys.argv[1:]
        {'env': env, 'schema': schema, 'snapshot': snapshot, 'verify': verify}[command](*args)
    except Exception as error:
        reason = ': '+str(error) if isinstance(error, ValueError) else ''
        print(f'Operation refused: {type(error).__name__}'+reason, file=sys.stderr)
        raise SystemExit(2)
