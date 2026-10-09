#!/usr/bin/env python3
"""Issue email-bound, single-use registration codes from the server CLI.

Examples (use the application's DATA_DIR):
  python scripts/manage_access.py bootstrap --email owner@example.com
  python scripts/manage_access.py invite --email colleague@example.com --ttl-hours 24
  python scripts/manage_access.py revoke --email colleague@example.com

These operations require filesystem access to the application's database. There
is no corresponding public API. Codes are shown once; only their SHA-256 hashes
are persisted. They create accounts, not access to existing shared spaces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import EMAIL_RE, SCHEMA, now_iso
from persistence import Database


@contextmanager
def access_database(data_dir):
    directory = Path(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "folio.sqlite3"
    database = Database(path, os.environ.get("DATABASE_URL"))
    with database.connect_context() as conn:
        if database.dialect == "sqlite":
            os.chmod(path, 0o600)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(SCHEMA)
        # SQLite executescript ends its transaction; acquire the lock again.
        if database.dialect == "sqlite":
            conn.execute("BEGIN IMMEDIATE")
        yield conn


def normalize_email(email):
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValueError("请输入有效邮箱地址")
    return email


def issue_invitation(data_dir, email, ttl_hours=24, bootstrap=False):
    email = normalize_email(email)
    if type(ttl_hours) is not int or not 1 <= ttl_hours <= 168:
        raise ValueError("有效期需为 1 至 168 小时的整数")
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = now + ttl_hours * 3600
    with access_database(data_dir) as conn:
        has_users = conn.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0]
        if bootstrap and has_users:
            raise ValueError("工作台已有账号，不能再次执行首次引导；请使用 invite")
        if not bootstrap and not has_users:
            raise ValueError("工作台尚无账号，请先使用 bootstrap 为首次使用者生成邀请码")
        if bootstrap and conn.execute("SELECT 1 FROM registration_invites WHERE bootstrap=1 AND consumed_at IS NULL AND expires_at>? LIMIT 1", (now,)).fetchone():
            raise ValueError("已有未过期的首次引导邀请码；如已遗失，请先使用 revoke --email 撤销")
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise ValueError("此邮箱已注册，无需注册邀请码")
        if conn.execute("SELECT 1 FROM registration_invites WHERE email=? AND consumed_at IS NULL AND expires_at>? LIMIT 1", (email, now)).fetchone():
            raise ValueError("此邮箱已有有效邀请码；如需更换，请先使用 revoke --email 撤销")
        conn.execute("INSERT INTO registration_invites VALUES (?,?,?,?,?,?)", (token_hash, email, expires_at, None, int(bootstrap), now_iso()))
    return {
        "email": email,
        "invitationCode": token,
        "expiresAt": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def revoke_invitation(data_dir, email):
    email = normalize_email(email)
    with access_database(data_dir) as conn:
        count = conn.execute("UPDATE registration_invites SET consumed_at=? WHERE email=? AND consumed_at IS NULL", (int(time.time()), email)).rowcount
    return {"email": email, "revoked": count}


def main(argv=None):
    parser = argparse.ArgumentParser(description="从服务器本地生成或撤销一次性注册邀请码")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(ROOT / "data")), help="应用数据目录，默认使用 DATA_DIR")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("bootstrap", "invite", "revoke"):
        command = actions.add_parser(action)
        command.add_argument("--email", required=True, help="邀请码绑定的注册邮箱")
        command.add_argument("--json", action="store_true", help="以 JSON 输出当前操作结果")
        if action != "revoke":
            command.add_argument("--ttl-hours", type=int, default=24, help="有效小时数，1–168，默认 24")
    args = parser.parse_args(argv)
    try:
        result = revoke_invitation(args.data_dir, args.email) if args.action == "revoke" else issue_invitation(args.data_dir, args.email, args.ttl_hours, args.action == "bootstrap")
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"操作失败：{error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == "revoke":
        print(f"已撤销 {result['email']} 的 {result['revoked']} 个未使用邀请码。")
    else:
        print(f"注册邮箱：{result['email']}")
        print(f"一次性邀请码（仅本次显示）：{result['invitationCode']}")
        print(f"有效期至（UTC）：{result['expiresAt']}")
        print("请通过可信渠道交给上述邮箱的使用者，勿公开粘贴。注册后再按需授予共享空间权限。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
