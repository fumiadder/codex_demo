"""Real HTTP tests for space-scoped exports and the SQLite snapshot maintenance.

Production server.py does not mount export.py yet, so a thin server subclass
mounts export.dispatch while keeping the full Folio HTTP stack (sessions,
membership, throttling, private media storage) exercised over real sockets.
"""
import base64
import http.client
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export
from scripts.manage_access import issue_invitation
# APIError must live in this module's namespace: bedtime/daily.initialize look
# it up through sys.modules[type(server).__module__].
from server import APIError, FolioHandler, FolioServer

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a pinned runtime dependency
    Image = None


def png_bytes(color=(120, 170, 130), size=(96, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class ExportHandler(FolioHandler):
    def api(self, path, query):
        if export.dispatch(self, path, query):
            return
        super().api(path, query)


class ExportServer(FolioServer):
    """Test-only mounting point; production wiring is handled in server.py."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # FolioServer.__init__ finishes by binding FolioHandler, so override after.
        self.RequestHandlerClass = ExportHandler
        export.initialize(self)


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Image is None:
            raise unittest.SkipTest("Pillow unavailable")
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = ExportServer(
            ("127.0.0.1", 0),
            data_dir=Path(cls.temp.name) / "data",
            registration_mode="open",
            storage_persistence="persistent",
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        with self.server.db() as conn:
            for table in ("daily_digests", "daily_images", "daily_uploads", "daily_entries",
                          "storage_jobs", "vaults", "files", "items", "members", "spaces",
                          "sessions", "users", "registration_invites"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        self.owner, self.owner_user, self.space = self.register("owner@example.com")
        self.editor, _, _ = self.register("editor@example.com")
        self.viewer, _, _ = self.register("viewer@example.com")
        self.outsider, self.outsider_user, self.outsider_space = self.register("outsider@example.com")
        self.sid = self.space["id"]
        self.request("POST", f"/api/spaces/{self.sid}/members",
                     {"email": "editor@example.com", "role": "editor"}, self.owner)
        self.request("POST", f"/api/spaces/{self.sid}/members",
                     {"email": "viewer@example.com", "role": "viewer"}, self.owner)
        # Authenticated traffic may already have triggered run_maintenance via
        # maybe_cleanup; every test starts with a deterministic snapshot slate.
        self.clear_snapshots()

    def clear_snapshots(self):
        directory = Path(self.server.data_dir) / "snapshots"
        if not directory.exists():
            return
        for child in directory.iterdir():
            if child.is_dir():
                for nested in child.iterdir():
                    nested.unlink()
                child.rmdir()
            else:
                child.unlink()

    def request(self, method, path, data=None, cookie=None, headers=None, raw=None, content_type=None):
        values = {"X-Requested-With": "Workspace", **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        if data is not None:
            raw = json.dumps(data).encode()
            values["Content-Type"] = "application/json"
        if content_type:
            values["Content-Type"] = content_type
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=15)
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        payload, response_headers, status = response.read(), dict(response.getheaders()), response.status
        connection.close()
        if response_headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return status, payload, response_headers

    def register(self, email):
        status, body, headers = self.request(
            "POST", "/api/auth/register",
            {"email": email, "name": "阿" + email.split("@")[0], "password": "test-password-123"})
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";")[0]
        _, spaces, _ = self.request("GET", "/api/spaces", cookie=cookie)
        return cookie, body["user"], spaces["spaces"][0]

    @staticmethod
    def vault():
        return {"version": 1,
                "salt": base64.b64encode(b"s" * 16).decode(),
                "iv": base64.b64encode(b"i" * 12).decode(),
                "ciphertext": base64.b64encode(b"encrypted authenticated payload" * 3).decode(),
                "kdfIterations": 310000}

    def upload_file(self, cookie, sid, content=b"generic file bytes 0001", name="notes.txt",
                    mime="text/plain", area="work", title="附件条目"):
        boundary = "export-test-boundary"
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"area\"\r\n\r\n{area}\r\n"
               f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n{title}\r\n"
               f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
               f"Content-Type: {mime}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", f"/api/spaces/{sid}/uploads", cookie=cookie, raw=raw,
                            content_type=f"multipart/form-data; boundary={boundary}")

    def make_daily_entry(self, image_content=None, body="白板记录"):
        image_content = png_bytes() if image_content is None else image_content
        boundary = "export-daily-boundary"
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               f"filename=\"shot.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + image_content + \
              f"\r\n--{boundary}--\r\n".encode()
        status, uploaded, _ = self.request(
            "POST", f"/api/spaces/{self.sid}/daily/uploads", cookie=self.owner, raw=raw,
            content_type=f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 201, uploaded)
        status, created, _ = self.request(
            "POST", f"/api/spaces/{self.sid}/daily/entries",
            {"imageIds": [uploaded["upload"]["id"]], "body": body}, cookie=self.owner)
        self.assertEqual(status, 201, created)
        return uploaded["upload"]["id"], created["entry"]

    def seed_space(self):
        """Create one of every exported record type in the owner's space."""
        file_bytes = b"generic file bytes 0001"
        status, uploaded, _ = self.upload_file(self.owner, self.sid, content=file_bytes)
        self.assertEqual(status, 201, uploaded)
        status, task, _ = self.request(
            "POST", f"/api/spaces/{self.sid}/items",
            {"kind": "task", "area": "work", "title": "整理周报", "body": "周五前完成",
             "status": "todo", "meta": {"source": "manual", "n": 1}}, cookie=self.owner)
        self.assertEqual(status, 201, task)
        image_id, entry = self.make_daily_entry()
        status, _, _ = self.request("PUT", f"/api/spaces/{self.sid}/vault", self.vault(), self.owner)
        self.assertEqual(status, 200)
        with self.server.db() as conn:
            conn.execute(
                "INSERT INTO daily_digests VALUES(?,?,?,?,?,?)",
                (self.sid, "2026-01-02", "今天完成了周报与白板整理。", self.owner_user["id"],
                 "2026-01-02T22:00:00Z", "2026-01-02T22:00:00Z"))
        return {"fileBytes": file_bytes, "imageId": image_id, "entry": entry,
                "fileId": uploaded["item"]["meta"]["fileId"], "taskId": task["item"]["id"]}

    def do_export(self, fmt=None, cookie=None, sid=None):
        path = f"/api/spaces/{sid or self.sid}/export"
        if fmt is not None:
            path += "?format=" + fmt
        return self.request("GET", path, cookie=self.owner if cookie is None else cookie)

    @staticmethod
    def open_zip(raw):
        archive = zipfile.ZipFile(io.BytesIO(raw))
        bad_name = archive.testzip()
        assert bad_name is None, f"zip 校验失败: {bad_name}"
        return archive

    def test_owner_export_three_formats_with_identical_media_bytes(self):
        seeded = self.seed_space()
        expected_headers = {
            "Content-Type": "application/zip",
            "Cache-Control": "private, no-store",
        }
        for fmt in ("json", "markdown", "html"):
            status, raw, headers = self.do_export(fmt)
            self.assertEqual(status, 200, raw[:200] if isinstance(raw, bytes) else raw)
            for key, value in expected_headers.items():
                self.assertEqual(headers.get(key), value, f"{fmt} 缺少响应头 {key}")
            disposition = headers.get("Content-Disposition", "")
            self.assertTrue(disposition.startswith("attachment;"), disposition)
            self.assertIn("filename*=UTF-8''", disposition)
            archive = self.open_zip(raw)
            names = archive.namelist()

            # Media bytes must survive the archive byte-for-byte in every format.
            file_asset = next(name for name in names if name.startswith("assets/files/"))
            image_asset = next(name for name in names if name.startswith("assets/daily/"))
            self.assertEqual(archive.read(file_asset), seeded["fileBytes"])
            self.assertEqual(archive.read(image_asset), png_bytes())

            if fmt == "json":
                self.assertIn("export.json", names)
                doc = json.loads(archive.read("export.json"))
                self.assertEqual(doc["spaceId"], self.sid)
                self.assertEqual(doc["space"]["id"], self.sid)
                self.assertEqual([m["role"] for m in doc["members"]].count("owner"), 1)
                self.assertEqual(len(doc["items"]), 2)
                task_item = next(item for item in doc["items"] if item["id"] == seeded["taskId"])
                self.assertIsInstance(task_item["meta"], dict)
                self.assertEqual(task_item["meta"]["source"], "manual")
                self.assertEqual(len(doc["files"]), 1)
                self.assertEqual(doc["files"][0]["assetPath"], file_asset)
                self.assertEqual(len(doc["daily"]["entries"]), 1)
                self.assertIsInstance(doc["daily"]["entries"][0]["meta"], dict)
                self.assertEqual(len(doc["daily"]["images"]), 1)
                self.assertEqual(len(doc["daily"]["digests"]), 1)
                self.assertTrue(doc["vault"]["locked"])
                self.assertIn("原密码", doc["vault"]["notice"])
                self.assertEqual(doc["vault"]["cipher"]["version"], 1)
                self.assertEqual(doc["missingMedia"], [])
            elif fmt == "markdown":
                for required in ("INDEX.md", "space.md", "vault.md",
                                 f"items/{seeded['taskId']}.md",
                                 f"daily/entries/{seeded['entry']['id']}.md",
                                 "daily/digests/2026-01-02.md", file_asset, image_asset):
                    self.assertIn(required, names, f"{fmt} 缺少 {required}")
                index_text = archive.read("INDEX.md").decode("utf-8")
                self.assertIn("assets/files/", index_text)
            else:
                self.assertIn("index.html", names)
                self.assertTrue(archive.read("index.html").startswith(b"<!doctype html>"))
                self.assertEqual([n for n in names if not n.startswith("assets/")], ["index.html"])

        # Streaming through a temp file must leave no archive behind on disk.
        leftovers = list(Path(self.server.data_dir).glob("export-*.zip"))
        self.assertEqual(leftovers, [])

    def test_owner_export_defaults_to_json(self):
        self.seed_space()
        status, raw, _ = self.do_export()
        self.assertEqual(status, 200)
        self.assertIn("export.json", self.open_zip(raw).namelist())

    def test_export_requires_owner_and_login(self):
        self.seed_space()
        status, body, _ = self.do_export("json", cookie=self.editor)
        self.assertEqual(status, 403)
        self.assertIn("权限", body["error"])
        status, body, _ = self.do_export("json", cookie=self.viewer)
        self.assertEqual(status, 403)
        # Authenticated non-members get the same generic 404 as the rest of the API.
        status, body, _ = self.do_export("json", cookie=self.outsider)
        self.assertEqual(status, 404)
        status, body, _ = self.do_export("json", cookie="")
        self.assertEqual(status, 401)
        self.assertIn("登录", body["error"])

    def test_format_and_method_validation_and_route_passthrough(self):
        self.seed_space()
        status, body, _ = self.do_export("pdf")
        self.assertEqual(status, 400)
        self.assertIn("格式", body["error"])
        status, _, _ = self.request("POST", f"/api/spaces/{self.sid}/export", {}, self.owner)
        self.assertEqual(status, 405)
        status, _, _ = self.do_export("json", sid="a" * 32)
        self.assertEqual(status, 404)
        status, body, _ = self.request("GET", f"/api/spaces/{self.sid}/export/child", cookie=self.owner)
        self.assertEqual(status, 404)
        # Unrelated routes must still pass through the shared handler untouched.
        status, body, _ = self.request("GET", "/api/me", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["id"], self.owner_user["id"])

    def test_export_never_leaks_other_spaces_sessions_invites_or_hashes(self):
        seeded = self.seed_space()
        # A second account owns an entirely separate space with its own records.
        outsider_sid = self.outsider_space["id"]
        status, _, _ = self.upload_file(
            self.outsider, outsider_sid, content=b"OUTSIDER-FILE-BYTES-9999", name="secret.txt")
        self.assertEqual(status, 201)
        status, _, _ = self.request(
            "POST", f"/api/spaces/{outsider_sid}/items",
            {"kind": "note", "area": "life", "title": "外人记录", "body": "OUTSIDER-ITEM-MARKER",
             "status": "active", "meta": {}}, cookie=self.outsider)
        self.assertEqual(status, 201)
        outsider_vault = self.vault()
        outsider_vault["ciphertext"] = base64.b64encode(b"OUTSIDER-VAULT-SECRET-MARKER-0001").decode()
        status, _, _ = self.request("PUT", f"/api/spaces/{outsider_sid}/vault", outsider_vault, self.outsider)
        self.assertEqual(status, 200)
        invitation = issue_invitation(self.server.data_dir, "invitee@example.com")
        with self.server.db() as conn:
            owner_hash = conn.execute(
                "SELECT password_hash FROM users WHERE id=?", (self.owner_user["id"],)).fetchone()[0]
            invite_hash = conn.execute("SELECT token_hash FROM registration_invites").fetchone()[0]
            outsider_item = conn.execute(
                "SELECT id FROM items WHERE space_id=?", (outsider_sid,)).fetchone()[0]
        self.assertEqual(invitation["email"], "invitee@example.com")

        for fmt in ("json", "markdown", "html"):
            status, raw, _ = self.do_export(fmt)
            self.assertEqual(status, 200, fmt)
            archive = self.open_zip(raw)
            blob = b"".join(archive.read(name) for name in archive.namelist())
            for secret in (outsider_sid.encode(), outsider_item.encode(),
                           b"OUTSIDER-FILE-BYTES-9999", b"OUTSIDER-ITEM-MARKER",
                           b"OUTSIDER-VAULT-SECRET-MARKER", b"outsider@example.com",
                           owner_hash.encode(), invite_hash.encode(),
                           b"registration_invites", b"sessions"):
                self.assertNotIn(secret, blob, f"{fmt} 导出泄露了 {secret!r}")
            doc = json.loads(archive.read("export.json")) if fmt == "json" else None
            if doc:
                self.assertTrue(all(item["space_id"] == self.sid for item in doc["items"]))
                self.assertTrue(all(row["space_id"] == self.sid for row in doc["files"]))
                self.assertTrue(all(row["space_id"] == self.sid for row in doc["daily"]["entries"]))
                self.assertEqual(doc["vault"]["userId"], self.owner_user["id"])
        # The invitation row remains untouched by the export (still unconsumed).
        with self.server.db() as conn:
            self.assertEqual(
                conn.execute("SELECT consumed_at FROM registration_invites").fetchone()[0], None)

    def test_html_and_markdown_escape_injected_markup(self):
        malicious = {"kind": "note", "area": "work",
                     "title": "<script>alert(1)</script>",
                     "body": "<img src=x onerror=alert(1)>",
                     "status": "active",
                     "meta": {"html": "<script>confirm(2)</script>"}}
        status, body, _ = self.request(
            "POST", f"/api/spaces/{self.sid}/items", malicious, cookie=self.owner)
        self.assertEqual(status, 201, body)
        self.make_daily_entry(body="<script>alert(3)</script>")

        status, raw, _ = self.do_export("html")
        self.assertEqual(status, 200)
        page = self.open_zip(raw).read("index.html")
        # No live tag or event handler may survive; the words themselves are
        # inert inside escaped text.
        self.assertNotIn(b"<script", page)
        self.assertNotIn(b"<img", page)
        self.assertNotIn(b"<img src=x onerror", page)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn(b"&lt;img src=x onerror=alert(1)&gt;", page)

        status, raw, _ = self.do_export("markdown")
        self.assertEqual(status, 200)
        archive = self.open_zip(raw)
        for name in archive.namelist():
            if name.endswith(".md"):
                text = archive.read(name)
                self.assertNotIn(b"<script", text, name)
                self.assertNotIn(b"<img", text, name)
        joined = b"".join(archive.read(n) for n in archive.namelist() if n.endswith(".md"))
        self.assertIn(b"&lt;script&gt;", joined)

    def test_export_throttled_at_seventh_request(self):
        self.seed_space()
        self.server.rate_events.clear()
        statuses = [self.do_export("json")[0] for _ in range(7)]
        self.assertEqual(statuses[:6], [200] * 6)
        self.assertEqual(statuses[6], 429)
        status, body, headers = self.do_export("json")
        self.assertEqual(status, 429)
        self.assertIn("频繁", body["error"])
        self.assertEqual(headers.get("Retry-After"), "900")

    def test_missing_media_is_recorded_without_failing_export(self):
        seeded = self.seed_space()
        object_path = Path(self.server.data_dir) / "uploads" / seeded["fileId"]
        object_path.unlink()
        status, raw, _ = self.do_export("json")
        self.assertEqual(status, 200)
        doc = json.loads(self.open_zip(raw).read("export.json"))
        self.assertEqual([item["id"] for item in doc["missingMedia"]], [seeded["fileId"]])

    def test_maintenance_creates_one_daily_snapshot_and_keeps_seven(self):
        self.seed_space()
        snapshots = Path(self.server.data_dir) / "snapshots"
        self.assertTrue(snapshots.is_dir())
        self.assertEqual(stat.S_IMODE(snapshots.stat().st_mode), 0o700)
        # seed_space traffic may have produced an empty same-day snapshot via
        # maybe_cleanup; reset so the explicit pass backs up the seeded rows.
        self.clear_snapshots()

        export.run_maintenance(self.server)
        files = sorted(p for p in snapshots.iterdir() if export.SNAPSHOT_RE.fullmatch(p.name))
        self.assertEqual(len(files), 1)
        first = files[0]
        self.assertTrue(first.name.startswith("folio-"))
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        # The backup is a real, intact SQLite database containing the live rows.
        with sqlite3.connect(first) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertGreaterEqual(backup.execute("SELECT count(*) FROM items").fetchone()[0], 2)
            self.assertTrue(backup.execute("SELECT 1 FROM spaces WHERE id=?", (self.sid,)).fetchone())

        # Idempotent within the same calendar day.
        export.run_maintenance(self.server)
        self.assertEqual(len([p for p in snapshots.iterdir() if export.SNAPSHOT_RE.fullmatch(p.name)]), 1)

        # A concurrent maintenance pass skips immediately on the non-blocking lock.
        acquired = export._snapshot_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            export.run_maintenance(self.server)
            self.assertEqual(len([p for p in snapshots.iterdir() if export.SNAPSHOT_RE.fullmatch(p.name)]), 1)
        finally:
            export._snapshot_lock.release()

        # Simulate eight maintenance days; retention must leave the newest seven.
        self.clear_snapshots()
        base = datetime(2026, 1, 1, 9, 0, 0)
        for index in range(8):
            day = base + timedelta(days=index)
            day_text = day.strftime("%Y%m%d")
            with mock.patch.object(export, "_today", return_value=day_text), \
                 mock.patch.object(export, "_stamp", return_value=day_text + "-090000"):
                export.run_maintenance(self.server)
                # A second pass on the same day never creates another snapshot.
                export.run_maintenance(self.server)
        remaining = sorted(p.name for p in snapshots.iterdir() if export.SNAPSHOT_RE.fullmatch(p.name))
        self.assertEqual(len(remaining), 7)
        self.assertNotIn("folio-20260101-090000.db", remaining)
        self.assertEqual(remaining[0], "folio-20260102-090000.db")
        self.assertEqual(remaining[-1], "folio-20260108-090000.db")

    def test_maintenance_skips_postgres_dialect_silently(self):
        self.clear_snapshots()
        snapshots = Path(self.server.data_dir) / "snapshots"
        self.assertEqual(list(snapshots.iterdir()), [])
        with mock.patch.object(self.server.database, "dialect", "postgresql"):
            export.run_maintenance(self.server)
        self.assertEqual(list(snapshots.iterdir()), [])

    def test_maintenance_swallows_errors(self):
        # A broken database path must surface as a warning, never an exception
        # that could break the request thread calling the maintenance hook.
        self.clear_snapshots()
        original_path = self.server.db_path
        try:
            self.server.db_path = Path(self.temp.name) / "does-not-exist" / "folio.sqlite3"
            export.run_maintenance(self.server)
        finally:
            self.server.db_path = original_path
        # No partial backup may survive a failed maintenance pass.
        self.assertEqual(
            list((Path(self.server.data_dir) / "snapshots").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
