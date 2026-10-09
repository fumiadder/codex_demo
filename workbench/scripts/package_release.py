#!/usr/bin/env python3
"""Create a code-only deployment archive; data and secrets are never included."""
import argparse
import hashlib
import io
import re
import tarfile
from pathlib import Path

ALLOW_FILES = ('server.py', 'bedtime.py', 'persistence.py', 'neon_transport.py', 'media_storage.py', 'storage_jobs.py', 'requirements.txt', 'stories_data.json', 'Dockerfile', 'compose.yaml', 'Caddyfile', '.dockerignore')
ALLOW_TREES = ('public', 'scripts')


def package(root, output, sha):
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('A 40-character source SHA is required')
    root = root.resolve()
    selected = []
    for name in ALLOW_FILES:
        path = root / name
        if path.is_file():
            selected.append(path)
        else:
            raise ValueError('Required source file missing: ' + name)
    for name in ALLOW_TREES:
        for path in (root / name).rglob('*'):
            if path.is_file() and path.suffix not in ('.pyc', '.sqlite3', '.db') and '__pycache__' not in path.parts:
                selected.append(path)
    for path in selected:
        if path.is_symlink() or any(part.startswith('.env') for part in path.relative_to(root).parts):
            raise ValueError('Symlink or environment file in release source')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(selected):
            name = path.relative_to(root).as_posix()
            archive.add(path, arcname=name, recursive=False)
        data = (sha + '\n').encode()
        info = tarfile.TarInfo('SOURCE_SHA')
        info.size, info.mode = len(data), 0o600
        archive.addfile(info, io.BytesIO(data))
    print('PASS code-only release archive created')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sha', required=True)
    args = parser.parse_args()
    package(args.root, args.output, args.sha)
