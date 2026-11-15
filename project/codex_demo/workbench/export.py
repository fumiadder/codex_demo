"""Space-scoped data export and low-frequency database snapshots.

The module owns no tables and never changes the schema. ``dispatch`` streams a
single space's records as a ZIP archive in one of three formats. Every query is
bound to the requested space id; sessions, invitations, password hashes and
other accounts' vaults are never read. Media bytes are streamed through the
private media storage into a temporary file so large spaces cannot exhaust
memory, and the temporary file is always removed after the response.

``run_maintenance`` performs one online SQLite backup per day (PostgreSQL is
skipped, its backups belong to the external backup system) and retains the
newest seven snapshots. Snapshots contain the whole database and therefore
live in a 0700 directory with no HTTP download route.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from media_storage import MissingMedia, StorageError

ID = r"[a-f0-9]{32}"
ROUTE = re.compile(rf"^/api/spaces/({ID})/export(?:/(.*))?$")
SNAPSHOT_RE = re.compile(r"^folio-(\d{8})-(\d{6})\.(?:db|sqlite3)$")
SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
SAFE_ARCNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")

FORMATS = ("json", "markdown", "html")
EXPORT_LIMIT = 6
EXPORT_WINDOW = 3600
SNAPSHOT_KEEP = 7
STREAM_CHUNK = 64 * 1024
VAULT_NOTICE = "密码箱内容为端到端加密密文，导入后仍需原密码才能解密查看。"

# Maintenance may be triggered from request threads; a single non-blocking
# module lock keeps concurrent maintenance passes from racing each other.
_snapshot_lock = threading.Lock()


def _today():
    # Indirection so tests can simulate successive days without sleeping.
    return datetime.now().strftime("%Y%m%d")


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _export_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def initialize(server):
    """Create the private snapshot directory; no heavy work at startup."""
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:  # test subclasses may live in another module
        from server import APIError as error
    server.export_error = error
    snapshots = Path(server.data_dir) / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshots, 0o700)


def fail(handler, status, message):
    error = getattr(handler.server, "export_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def _safe_suffix(name):
    """Return only a short whitelisted suffix; never trust stored filenames."""
    if not isinstance(name, str):
        return ""
    suffix = Path(name.replace("\\", "/").split("/")[-1]).suffix
    return suffix if SAFE_SUFFIX_RE.fullmatch(suffix) else ""


def _arcname(path):
    if not SAFE_ARCNAME_RE.fullmatch(path) or ".." in path.split("/") or path.startswith("/"):
        raise ValueError("非法的归档路径")
    return path


def _json_field(raw):
    """Parse stored JSON columns; keep the original text if it is malformed."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _md_escape(value):
    # Escaping the HTML-significant characters makes sure rendered Markdown
    # cannot turn user text into executable markup such as <script> tags.
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _collect(conn, sid, uid):
    """Read only the rows belonging to this space and this account's vault."""
    space_row = conn.execute(
        "SELECT id,name,owner_id,created_at FROM spaces WHERE id=?", (sid,)
    ).fetchone()
    if not space_row:
        return None
    space = dict(space_row)
    members = [dict(row) for row in conn.execute(
        "SELECT m.space_id,m.user_id,m.role,u.email,u.name FROM members m "
        "JOIN users u ON u.id=m.user_id WHERE m.space_id=? "
        "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,u.name",
        (sid,),
    ).fetchall()]
    items = []
    for row in conn.execute("SELECT * FROM items WHERE space_id=? ORDER BY created_at,id", (sid,)).fetchall():
        item = dict(row)
        item["meta"] = _json_field(item["meta"])
        items.append(item)
    files = [dict(row) for row in conn.execute(
        "SELECT * FROM files WHERE space_id=? ORDER BY created_at,id", (sid,)
    ).fetchall()]
    entries = []
    for row in conn.execute(
        "SELECT * FROM daily_entries WHERE space_id=? ORDER BY entry_date,created_at,id", (sid,)
    ).fetchall():
        entry = dict(row)
        entry["meta"] = _json_field(entry["meta"])
        entries.append(entry)
    daily_images = [dict(row) for row in conn.execute(
        "SELECT * FROM daily_images WHERE space_id=? ORDER BY entry_id,sort_order,id", (sid,)
    ).fetchall()]
    digests = [dict(row) for row in conn.execute(
        "SELECT * FROM daily_digests WHERE space_id=? ORDER BY entry_date", (sid,)
    ).fetchall()]
    vault_row = conn.execute(
        "SELECT space_id,user_id,payload,updated_at FROM vaults WHERE space_id=? AND user_id=?",
        (sid, uid),
    ).fetchone()
    vault = None
    if vault_row:
        vault = {
            "spaceId": vault_row["space_id"],
            "userId": vault_row["user_id"],
            "updatedAt": vault_row["updated_at"],
            "cipher": _json_field(vault_row["payload"]),
            "locked": True,
            "notice": VAULT_NOTICE,
        }
    return {
        "space": space,
        "members": members,
        "items": items,
        "files": files,
        "dailyEntries": entries,
        "dailyImages": daily_images,
        "dailyDigests": digests,
        "vault": vault,
    }


def _media_plan(data):
    """Map every private object referenced by this space to a ZIP path."""
    plan = []
    for row in data["files"]:
        arcname = _arcname("assets/files/" + row["id"] + _safe_suffix(row.get("name")))
        row["assetPath"] = arcname
        plan.append((row["id"], arcname, row.get("mime", "application/octet-stream"), row.get("name", ""), "files"))
    for row in data["dailyImages"]:
        arcname = _arcname("assets/daily/" + row["id"] + _safe_suffix(row.get("name")))
        row["assetPath"] = arcname
        plan.append((row["id"], arcname, row.get("mime", "application/octet-stream"), row.get("name", ""), "daily"))
    return plan


def _stream_media(server, zf, media_id, arcname):
    """Copy a private object into the ZIP in bounded chunks, never whole in RAM."""
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    with server.media_storage.open("uploads", media_id) as stream:
        with zf.open(info, "w") as sink:
            while True:
                chunk = stream.body.read(STREAM_CHUNK)
                if not chunk:
                    break
                sink.write(chunk)


def _write_media(server, zf, plan, missing):
    for media_id, arcname, _mime, _name, _kind in plan:
        try:
            _stream_media(server, zf, media_id, arcname)
        except MissingMedia:
            # A missing object must not destroy the whole export; record it in
            # the manifest so the gap is visible instead of silent.
            logging.warning("Export media object missing: %s", media_id)
            missing.append({"id": media_id, "assetPath": arcname})
        # StorageError propagates: no response headers have been sent yet, so
        # dispatch can still answer with a sanitized JSON 503.


def _build_json_document(data, plan, missing, exported_at, sid):
    return {
        "exportVersion": 1,
        "exportedAt": exported_at,
        "spaceId": sid,
        "space": data["space"],
        "members": data["members"],
        "items": data["items"],
        "files": data["files"],
        "daily": {
            "entries": data["dailyEntries"],
            "images": data["dailyImages"],
            "digests": data["dailyDigests"],
        },
        "vault": data["vault"],
        "media": [
            {"id": media_id, "kind": kind, "assetPath": arcname, "mime": mime, "name": name}
            for media_id, arcname, mime, name, kind in plan
        ],
        "missingMedia": missing,
    }


def _write_json_export(server, zf, data, plan, exported_at, sid):
    missing = []
    _write_media(server, zf, plan, missing)
    document = _build_json_document(data, plan, missing, exported_at, sid)
    zf.writestr("export.json", json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))


def _write_markdown_export(server, zf, data, plan, exported_at):
    missing = []
    _write_media(server, zf, plan, missing)
    index_lines = [
        "# 知序工作台空间导出",
        "",
        "- 导出时间：" + _md_escape(exported_at),
        "- 空间：" + _md_escape(data["space"]["name"]) + "（" + data["space"]["id"] + "）",
        "- 说明：所有用户文本均已做 HTML 转义，媒体文件位于 assets/ 目录。",
        "",
        "## 目录",
        "",
        "- [空间与成员](space.md)",
        "- [密码箱](vault.md)",
    ]

    space_lines = [
        "# 空间信息",
        "",
        "- 空间名称：" + _md_escape(data["space"]["name"]),
        "- 空间 ID：" + data["space"]["id"],
        "- 创建时间：" + _md_escape(data["space"]["created_at"]),
        "",
        "## 成员",
        "",
        "| 姓名 | 邮箱 | 角色 |",
        "| --- | --- | --- |",
    ]
    for member in data["members"]:
        space_lines.append(
            "| " + _md_escape(member["name"]) + " | " + _md_escape(member["email"])
            + " | " + _md_escape(member["role"]) + " |"
        )
    space_lines.append("")
    zf.writestr("space.md", ("\n".join(space_lines)).encode("utf-8"))

    for item in data["items"]:
        arcname = _arcname("items/" + item["id"] + ".md")
        lines = [
            "# " + _md_escape(item["title"]),
            "",
            "- 条目 ID：" + item["id"],
            "- 类型：" + _md_escape(item["kind"]),
            "- 分区：" + _md_escape(item["area"]),
            "- 状态：" + _md_escape(item["status"]),
            "- 创建时间：" + _md_escape(item["created_at"]),
            "- 更新时间：" + _md_escape(item["updated_at"]),
            "",
            "## 内容",
            "",
            _md_escape(item["body"]) or "（无正文）",
            "",
            "## 附加信息（meta 原样 JSON，已转义）",
            "",
            "```json",
            _md_escape(json.dumps(item["meta"], ensure_ascii=False, sort_keys=True)),
            "```",
            "",
        ]
        linked = [row for row in data["files"] if row["item_id"] == item["id"]]
        if linked:
            lines.append("## 附件")
            lines.append("")
            for file_row in linked:
                lines.append("- [" + _md_escape(file_row["name"]) + "](" + "../" + file_row["assetPath"] + ")")
            lines.append("")
        zf.writestr(arcname, ("\n".join(lines)).encode("utf-8"))
        index_lines.append("- [条目：" + _md_escape(item["title"]) + "](" + arcname + ")")

    for entry in data["dailyEntries"]:
        arcname = _arcname("daily/entries/" + entry["id"] + ".md")
        lines = [
            "# " + _md_escape(entry["title"]),
            "",
            "- 记录 ID：" + entry["id"],
            "- 日期：" + _md_escape(entry["entry_date"]),
            "- 分区：" + _md_escape(entry["area"]),
            "- 状态：" + _md_escape(entry["status"]),
            "- 创建时间：" + _md_escape(entry["created_at"]),
            "",
            "## 摘要",
            "",
            _md_escape(entry["summary"]) or "（无摘要）",
            "",
            "## 附言",
            "",
            _md_escape(entry["body"]) or "（无附言）",
            "",
            "## 附加信息（meta 原样 JSON，已转义）",
            "",
            "```json",
            _md_escape(json.dumps(entry["meta"], ensure_ascii=False, sort_keys=True)),
            "```",
            "",
        ]
        images = [row for row in data["dailyImages"] if row["entry_id"] == entry["id"]]
        if images:
            lines.append("## 图片")
            lines.append("")
            for image in images:
                lines.append("- [" + _md_escape(image["name"]) + "](" + "../../" + image["assetPath"] + ")")
            lines.append("")
        zf.writestr(arcname, ("\n".join(lines)).encode("utf-8"))
        index_lines.append("- [今日记录：" + _md_escape(entry["title"]) + "](" + arcname + ")")

    for digest in data["dailyDigests"]:
        arcname = _arcname("daily/digests/" + digest["entry_date"] + ".md")
        content = "\n".join([
            "# 每日小结 " + digest["entry_date"],
            "",
            _md_escape(digest["digest"]),
            "",
            "- 更新时间：" + _md_escape(digest["updated_at"]),
            "",
        ])
        zf.writestr(arcname, content.encode("utf-8"))
        index_lines.append("- [每日小结：" + digest["entry_date"] + "](" + arcname + ")")

    vault_lines = ["# 密码箱", ""]
    if data["vault"]:
        vault_lines.extend([
            VAULT_NOTICE,
            "",
            "- 更新时间：" + _md_escape(data["vault"]["updatedAt"]),
            "- 密文（原样保留，已转义）：",
            "",
            "```json",
            _md_escape(json.dumps(data["vault"]["cipher"], ensure_ascii=False, sort_keys=True)),
            "```",
            "",
        ])
    else:
        vault_lines.append("该账号在此空间没有保存密码箱密文。")
        vault_lines.append("")
    zf.writestr("vault.md", ("\n".join(vault_lines)).encode("utf-8"))
    index_lines.append("")
    index_lines.append("## 媒体清单")
    index_lines.append("")
    for _media_id, arcname, mime, name, kind in plan:
        index_lines.append("- [" + _md_escape(name or arcname) + "](" + arcname + ")（" + _md_escape(kind) + "）")
    if missing:
        index_lines.append("")
        index_lines.append("## 缺失媒体")
        index_lines.append("")
        for item in missing:
            index_lines.append("- " + item["id"] + "（" + item["assetPath"] + "）")
    zf.writestr("INDEX.md", ("\n".join(index_lines) + "\n").encode("utf-8"))


def _write_html_export(server, zf, data, plan, exported_at):
    missing = []
    _write_media(server, zf, plan, missing)
    e = lambda value: html.escape("" if value is None else str(value), quote=True)
    parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>" + e(data["space"]["name"]) + " - 空间导出</title>",
        "</head>",
        "<body>",
        "<h1>知序工作台空间导出</h1>",
        "<p>导出时间：" + e(exported_at) + "</p>",
        "<h2>空间信息</h2>",
        "<ul>",
        "<li>空间名称：" + e(data["space"]["name"]) + "</li>",
        "<li>空间 ID：" + e(data["space"]["id"]) + "</li>",
        "<li>创建时间：" + e(data["space"]["created_at"]) + "</li>",
        "</ul>",
        "<h2>成员</h2>",
        '<table border="1"><thead><tr><th>姓名</th><th>邮箱</th><th>角色</th></tr></thead><tbody>',
    ]
    for member in data["members"]:
        parts.append("<tr><td>" + e(member["name"]) + "</td><td>" + e(member["email"])
                     + "</td><td>" + e(member["role"]) + "</td></tr>")
    parts.append("</tbody></table>")

    parts.append("<h2>条目</h2>")
    if data["items"]:
        parts.append("<ul>")
        for item in data["items"]:
            linked = [row for row in data["files"] if row["item_id"] == item["id"]]
            block = (
                "<strong>" + e(item["title"]) + "</strong>"
                " <em>(" + e(item["kind"]) + " / " + e(item["area"]) + " / " + e(item["status"]) + ")</em>"
                "<p>" + e(item["body"]) + "</p>"
                "<pre>" + e(json.dumps(item["meta"], ensure_ascii=False, sort_keys=True)) + "</pre>"
            )
            if linked:
                block += "<ul>" + "".join(
                    '<li><a href="' + e(file_row["assetPath"]) + '">' + e(file_row["name"]) + "</a></li>"
                    for file_row in linked
                ) + "</ul>"
            parts.append("<li>" + block + "</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>（无条目）</p>")

    parts.append("<h2>今日记录</h2>")
    if data["dailyEntries"]:
        parts.append("<ul>")
        for entry in data["dailyEntries"]:
            images = [row for row in data["dailyImages"] if row["entry_id"] == entry["id"]]
            block = (
                "<strong>" + e(entry["title"]) + "</strong> <em>(" + e(entry["entry_date"])
                + " / " + e(entry["area"]) + ")</em>"
                "<p>摘要：" + e(entry["summary"]) + "</p>"
                "<p>附言：" + e(entry["body"]) + "</p>"
                "<pre>" + e(json.dumps(entry["meta"], ensure_ascii=False, sort_keys=True)) + "</pre>"
            )
            if images:
                block += "<ul>" + "".join(
                    '<li><a href="' + e(image["assetPath"]) + '">' + e(image["name"]) + "</a></li>"
                    for image in images
                ) + "</ul>"
            parts.append("<li>" + block + "</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>（无今日记录）</p>")

    parts.append("<h2>每日小结</h2>")
    if data["dailyDigests"]:
        parts.append("<dl>")
        for digest in data["dailyDigests"]:
            parts.append("<dt>" + e(digest["entry_date"]) + "</dt><dd>" + e(digest["digest"]) + "</dd>")
        parts.append("</dl>")
    else:
        parts.append("<p>（无每日小结）</p>")

    parts.append("<h2>密码箱</h2>")
    if data["vault"]:
        parts.append("<p>" + e(VAULT_NOTICE) + "</p>")
        parts.append("<pre>" + e(json.dumps(data["vault"]["cipher"], ensure_ascii=False, sort_keys=True)) + "</pre>")
    else:
        parts.append("<p>该账号在此空间没有保存密码箱密文。</p>")

    parts.append("<h2>媒体清单</h2><ul>")
    for _media_id, arcname, mime, name, kind in plan:
        parts.append('<li><a href="' + e(arcname) + '">' + e(name or arcname) + "</a>（" + e(kind) + "）</li>")
    parts.append("</ul>")
    if missing:
        parts.append("<h2>缺失媒体</h2><ul>")
        for item in missing:
            parts.append("<li>" + e(item["id"]) + "（" + e(item["assetPath"]) + "）</li>")
        parts.append("</ul>")
    parts.append("</body></html>")
    zf.writestr("index.html", "".join(parts).encode("utf-8"))


def _download_name(space_name):
    # Control characters are stripped so they can never split the HTTP header.
    clean = re.sub(r"[\x00-\x1f\x7f]", "", space_name or "space")[:80]
    return f"知序空间导出-{clean}-{_export_stamp()}.zip"


def _send_zip(handler, tmp_path, size, download_name):
    handler._stream_response_started = True
    handler.send_response(200)
    handler.security_headers(file_response=True)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("Content-Length", str(size))
    handler.send_header(
        "Content-Disposition",
        "attachment; filename=\"export.zip\"; filename*=UTF-8''" + quote(download_name),
    )
    handler.end_headers()
    with open(tmp_path, "rb") as source:
        while True:
            chunk = source.read(STREAM_CHUNK)
            if not chunk:
                break
            handler.wfile.write(chunk)


def dispatch(handler, path, query):
    """Own the /api/spaces/:sid/export route; return False for every other path."""
    match = ROUTE.fullmatch(path)
    if not match:
        return False
    sid, sub_route = match[1], match[2] or ""
    server = handler.server
    if handler.command != "GET":
        fail(handler, 405, "不支持此请求方法")
    if sub_route:
        fail(handler, 404, "接口不存在")

    with server.db() as conn:
        user = handler.current_user(conn)
        uid = user["id"]
        handler.membership(conn, sid, uid, write=False, owner=True)

    # Per-account hourly quota, evaluated only after authentication passes.
    server.throttle(("export-hour", uid), EXPORT_LIMIT, EXPORT_WINDOW)

    fmt = (query.get("format", ["json"])[0] or "json").strip()
    if fmt not in FORMATS:
        fail(handler, 400, "导出格式无效，仅支持 json、markdown、html")

    with server.db() as conn:
        data = _collect(conn, sid, uid)
    if data is None:
        fail(handler, 404, "空间不存在或无权访问")
    plan = _media_plan(data)
    exported_at = _export_stamp()
    download_name = _download_name(data["space"].get("name"))

    tmp = tempfile.NamedTemporaryFile(
        delete=False, dir=server.data_dir, prefix="export-", suffix=".zip"
    )
    tmp.close()
    tmp_path = tmp.name
    try:
        os.chmod(tmp_path, 0o600)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if fmt == "json":
                    _write_json_export(server, zf, data, plan, exported_at, sid)
                elif fmt == "markdown":
                    _write_markdown_export(server, zf, data, plan, exported_at)
                else:
                    _write_html_export(server, zf, data, plan, exported_at)
        except StorageError:
            # Headers have not been sent yet, so a normal JSON error is safe.
            # Never surface provider/driver error text to the browser.
            fail(handler, 503, "媒体存储暂时不可用，请稍后重试")
        size = os.path.getsize(tmp_path)
        try:
            _send_zip(handler, tmp_path, size, download_name)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            # The status line is already on the wire; close instead of appending JSON.
            handler.close_connection = True
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.warning("Export temporary file removal deferred")
    return True


def _snapshot_dir(server):
    return Path(server.data_dir) / "snapshots"


def _prune_snapshots(directory):
    snapshots = sorted(
        (path for path in directory.iterdir() if SNAPSHOT_RE.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in snapshots[SNAPSHOT_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            logging.warning("Old snapshot removal deferred: %s", stale.name)


def run_maintenance(server):
    """Create at most one SQLite online backup per day; never raise."""
    try:
        if getattr(getattr(server, "database", None), "dialect", "") != "sqlite":
            # PostgreSQL backups are owned by the external backup system.
            return
        if not _snapshot_lock.acquire(blocking=False):
            # Another maintenance pass is running; the daily invariant is shared.
            return
        try:
            directory = _snapshot_dir(server)
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            today = _today()
            for path in directory.iterdir():
                match = SNAPSHOT_RE.fullmatch(path.name)
                if match and match.group(1) == today:
                    return
            stamp = _stamp()
            final_path = directory / f"folio-{stamp}.db"
            # The backup copies the source header, whose journal mode is WAL:
            # a target opened next to the snapshots would grow -wal/-shm
            # sidecars, and moving only the main file would strand the newest
            # pages. Keep the target in an isolated temp directory and switch
            # it back to the rollback journal so one self-contained file moves.
            tmp_dir = tempfile.mkdtemp(prefix=".snapshot-", dir=directory)
            tmp_path = Path(tmp_dir) / "folio.db"
            try:
                source = sqlite3.connect(server.db_path)
                try:
                    target = sqlite3.connect(tmp_path)
                    try:
                        source.backup(target)
                        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        target.execute("PRAGMA journal_mode=DELETE")
                    finally:
                        target.close()
                finally:
                    source.close()
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, final_path)
                _prune_snapshots(directory)
            except Exception:
                # Never publish or leave behind a partial backup.
                logging.warning("Snapshot backup failed", exc_info=False)
                raise
            finally:
                for leftover in Path(tmp_dir).glob("*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        logging.warning("Snapshot leftover removal deferred: %s", leftover.name)
                try:
                    os.rmdir(tmp_dir)
                except OSError:
                    logging.warning("Snapshot temporary directory removal deferred")
        finally:
            _snapshot_lock.release()
    except Exception:
        # Maintenance must never break the request thread that triggered it.
        logging.warning("Snapshot maintenance skipped", exc_info=False)
