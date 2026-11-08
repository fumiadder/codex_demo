"""Per-account workspace preferences: homepage shortcuts.

Only additive schema (CREATE TABLE IF NOT EXISTS). The row is keyed by user so a
single account can never read or write another account's shortcuts. Values are
stored as JSON because the shortcut list is an ordered array of view ids.
"""
from __future__ import annotations

import json
import sys
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 shortcuts TEXT NOT NULL DEFAULT '[]',
 updated_at TEXT NOT NULL
);
"""

# Whitelisted view ids a shortcut may point at. Keeping this server-side means a
# crafted request cannot inject an arbitrary string that later becomes a route.
SHORTCUT_VIEWS = (
    "overview", "daily", "tasks", "notes", "media", "vault",
    "spaces", "modules", "search", "calendar", "bedtime",
)
MAX_SHORTCUTS = 8


def fail(handler, status, message):
    error = getattr(handler.server, "settings_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def initialize(server):
    """No on-disk state; keep the entry point for symmetry with other modules.

    Resolve APIError from the module that actually runs the server. Under
    ``python server.py`` the running module is ``__main__``, so importing the
    ``server`` module by name would yield a *different* class object and the
    handler's ``except APIError`` would not match it (turning 400s into 500s).
    """
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:  # test subclasses may live in another module
        from server import APIError as error
    server.settings_error = error


def _normalize_shortcuts(raw):
    if not isinstance(raw, list):
        raise ValueError("快捷方式必须是数组")
    seen = []
    for value in raw:
        if not isinstance(value, str) or value not in SHORTCUT_VIEWS:
            raise ValueError("快捷方式包含不支持的页面")
        if value in seen:
            continue
        seen.append(value)
    if len(seen) > MAX_SHORTCUTS:
        raise ValueError(f"最多可设置 {MAX_SHORTCUTS} 个快捷方式")
    return seen


def _row_json(row):
    try:
        shortcuts = json.loads(row["shortcuts"])
    except (ValueError, TypeError):
        shortcuts = []
    if not isinstance(shortcuts, list):
        shortcuts = []
    shortcuts = [value for value in shortcuts if isinstance(value, str) and value in SHORTCUT_VIEWS]
    return {"shortcuts": shortcuts, "updatedAt": row["updated_at"]}


def _load(conn, uid):
    return conn.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()


def _read(handler, conn):
    uid = handler.current_user(conn)["id"]
    row = _load(conn, uid)
    if row is None:
        return {"shortcuts": [], "updatedAt": None}
    return _row_json(row)


def _write(handler, conn, data):
    from server import now_iso

    uid = handler.current_user(conn)["id"]
    if not isinstance(data, dict) or "shortcuts" not in data:
        fail(handler, 400, "缺少快捷方式设置")
    try:
        shortcuts = _normalize_shortcuts(data.get("shortcuts"))
    except ValueError as exc:
        fail(handler, 400, str(exc))
    stamp = now_iso()
    payload = json.dumps(shortcuts, ensure_ascii=False, separators=(",", ":"))
    conn.execute("BEGIN IMMEDIATE")
    if _load(conn, uid) is None:
        conn.execute(
            "INSERT INTO user_settings (user_id,shortcuts,updated_at) VALUES (?,?,?)",
            (uid, payload, stamp),
        )
    else:
        conn.execute(
            "UPDATE user_settings SET shortcuts=?,updated_at=? WHERE user_id=?",
            (payload, stamp, uid),
        )
    return {"shortcuts": shortcuts, "updatedAt": stamp}


def dispatch(handler, path, query):
    """Own /api/settings; return False for every other path."""
    if path != "/api/settings":
        return False
    method = handler.command
    server = handler.server
    if method == "GET":
        with server.db() as conn:
            result = _read(handler, conn)
        handler.json_response(200, result)
        return True
    if method == "PATCH":
        data = handler.read_json()
        with server.db() as conn:
            result = _write(handler, conn, data)
        handler.json_response(200, result)
        return True
    fail(handler, 405, "不支持此请求方法")
    return True