"""Integration tests use real HTTP requests and a disposable SQLite database."""
import base64
import hashlib
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import COOKIE_NAME, MAX_UPLOAD_BYTES, FolioServer, verify_password


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.public = Path(cls.temp.name) / "public"
        cls.public.mkdir()
        (cls.public / "index.html").write_text("<!doctype html><title>Folio</title>")
        cls.server = FolioServer(("127.0.0.1", 0), data_dir=Path(cls.temp.name)/"data", public_dir=cls.public)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        with self.server.db() as conn:
            for table in ("vaults", "files", "items", "members", "spaces", "sessions", "users"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        self.server.trust_proxy = False
        self.server.test_mode = False
        self.server.max_space_storage = 1024**3
        self.server.max_total_storage = 10*1024**3
        for path in self.server.upload_dir.iterdir():
            path.unlink()

    def request(self, method, path, data=None, cookie=None, headers=None, raw=None):
        values = {"X-Requested-With": "Workspace"}
        if cookie:
            values["Cookie"] = cookie
        if data is not None:
            raw = json.dumps(data).encode()
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=8)
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        body = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        connection.close()
        if result_headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(body)
        return status, body, result_headers

    def register(self, email="owner@example.com", name="阿舟"):
        status, body, headers = self.request("POST", "/api/auth/register", {"email": email, "name": name, "password": "test-password-123"})
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, body2, _ = self.request("GET", "/api/spaces", cookie=cookie)
        self.assertEqual(status, 200)
        return cookie, body["user"], body2["spaces"][0]

    def task(self, cookie, sid, **fields):
        return self.request("POST", f"/api/spaces/{sid}/items", {"kind": "task", "area": "work", "title": "整理周报", **fields}, cookie=cookie)

    def member(self, cookie, sid, email, role="editor"):
        return self.request("POST", f"/api/spaces/{sid}/members", {"email": email, "role": role}, cookie=cookie)

    def upload(self, cookie, sid, content=b"example upload", name="notes.txt", mime="text/plain", area="work"):
        boundary = "folio-test-boundary"
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="area"\r\n\r\n{area}\r\n'
               f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", f"/api/spaces/{sid}/uploads", cookie=cookie, raw=raw, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    @staticmethod
    def vault():
        return {"version": 1, "salt": base64.b64encode(b"s"*16).decode(), "iv": base64.b64encode(b"i"*12).decode(), "ciphertext": base64.b64encode(b"encrypted authenticated payload"*3).decode(), "kdfIterations": 310000}

    def test_registration_private_spaces_and_area_filters(self):
        a, user_a, space_a = self.register()
        b, user_b, space_b = self.register("other@example.com")
        self.assertNotEqual(space_a["id"], space_b["id"])
        self.assertEqual(space_a["role"], "owner")
        self.assertNotIn("password_hash", user_a)
        self.assertEqual(self.task(a, space_a["id"])[0], 201)
        self.assertEqual(self.task(a, space_a["id"], area="life", title="跑步")[0], 201)
        status, body, _ = self.request("GET", f'/api/spaces/{space_a["id"]}/items?area=life', cookie=a)
        self.assertEqual(status, 200)
        self.assertEqual([i["title"] for i in body["items"]], ["跑步"])
        self.assertEqual(self.request("GET", f'/api/spaces/{space_a["id"]}/items', cookie=b)[0], 404)
        self.assertEqual(self.request("GET", f'/api/spaces/{space_a["id"]}/items?area=invalid', cookie=a)[0], 400)

    def test_anonymous_requests_denied(self):
        a, _, space = self.register()
        sid = space["id"]
        for method, path in (("GET", "/api/me"), ("GET", "/api/spaces"), ("GET", f"/api/spaces/{sid}/items"), ("GET", f"/api/spaces/{sid}/members"), ("GET", f"/api/spaces/{sid}/vault"), ("POST", "/api/spaces")):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, data={"name": "new"} if method == "POST" else None)[0], 401)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_public_config_exposes_only_storage_mode(self):
        status, body, headers = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"testMode": False, "storagePersistence": "persistent"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.server.test_mode = True
        status, body, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"testMode": True, "storagePersistence": "ephemeral"})

    def test_cross_account_item_write_delete_upload_denied(self):
        a, _, space = self.register()
        b, _, _ = self.register("other@example.com")
        sid = space["id"]
        _, body, _ = self.task(a, sid)
        item_id = body["item"]["id"]
        self.assertEqual(self.request("PATCH", f"/api/items/{item_id}", {"title": "stolen"}, cookie=b)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/items/{item_id}", cookie=b)[0], 404)
        self.assertEqual(self.task(b, sid)[0], 404)
        self.assertEqual(self.upload(b, sid)[0], 404)
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/members", cookie=b)[0], 404)

    def test_editor_collaboration_viewer_limits_and_revocation(self):
        owner, _, space = self.register()
        editor, editor_user, _ = self.register("editor@example.com")
        viewer, viewer_user, _ = self.register("viewer@example.com")
        sid = space["id"]
        self.assertEqual(self.member(owner, sid, "editor@example.com")[0], 201)
        self.assertEqual(self.member(owner, sid, "viewer@example.com", "viewer")[0], 201)
        _, body, _ = self.task(editor, sid)
        iid = body["item"]["id"]
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/items", cookie=viewer)[0], 200)
        self.assertEqual(self.task(viewer, sid)[0], 403)
        self.assertEqual(self.request("PATCH", f"/api/items/{iid}", {"status": "done"}, cookie=editor)[0], 200)
        self.assertEqual(self.request("PATCH", f"/api/items/{iid}", {"status": "done"}, cookie=viewer)[0], 403)
        self.assertEqual(self.upload(viewer, sid)[0], 403)
        self.assertEqual(self.member(editor, sid, "viewer@example.com", "editor")[0], 403)
        self.assertEqual(self.member(viewer, sid, "viewer@example.com", "owner")[0], 403)
        self.assertEqual(self.member(owner, sid, "editor@example.com", "owner")[0], 400)
        self.assertEqual(self.member(owner, sid, "owner@example.com", "viewer")[0], 400)
        self.assertEqual(self.request("DELETE", f'/api/spaces/{sid}/members/{editor_user["id"]}', cookie=viewer)[0], 403)
        self.assertEqual(self.request("DELETE", f'/api/spaces/{sid}/members/{editor_user["id"]}', cookie=owner)[0], 200)
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/items", cookie=editor)[0], 404)

    def test_vault_private_per_account_and_requires_editor(self):
        owner, owner_user, space = self.register()
        editor, _, _ = self.register("editor@example.com")
        viewer, _, _ = self.register("viewer@example.com")
        sid = space["id"]
        self.member(owner, sid, "editor@example.com")
        self.member(owner, sid, "viewer@example.com", "viewer")
        payload = self.vault()
        self.assertEqual(self.request("PUT", f"/api/spaces/{sid}/vault", payload, cookie=owner)[0], 200)
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/vault", cookie=owner)[1]["vault"], payload)
        self.assertIsNone(self.request("GET", f"/api/spaces/{sid}/vault", cookie=editor)[1]["vault"])
        self.assertEqual(self.request("PUT", f"/api/spaces/{sid}/vault", payload, cookie=editor)[0], 200)
        self.assertEqual(self.request("PUT", f"/api/spaces/{sid}/vault", payload, cookie=viewer)[0], 403)
        invalids = ({**payload, "password": "plaintext"}, {**payload, "kdfIterations": 1}, {**payload, "version": True}, {**payload, "iv": "bad"}, {**payload, "salt": "!!!!"})
        for invalid in invalids:
            self.assertEqual(self.request("PUT", f"/api/spaces/{sid}/vault", invalid, cookie=owner)[0], 400)
        with self.server.db() as conn:
            rows = conn.execute("SELECT payload FROM vaults").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("password" not in row[0] for row in rows))

    def test_password_and_session_storage_and_logout(self):
        cookie, user, _ = self.register()
        token = cookie.split("=", 1)[1]
        with self.server.db() as conn:
            stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
            self.assertTrue(stored.startswith("pbkdf2_sha256$310000$"))
            self.assertNotIn("test-password-123", stored)
            self.assertTrue(verify_password("test-password-123", stored))
            self.assertFalse(verify_password("wrong", stored))
            session = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(session["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(session["token_hash"], token)
        status, _, headers = self.request("POST", "/api/auth/logout", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 401)

    def test_login_generic_error_expiry_and_rate_limit(self):
        cookie, user, _ = self.register()
        a = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "wrong"})
        b = self.request("POST", "/api/auth/login", {"email": "unknown@example.com", "password": "wrong"})
        self.assertEqual(a[:2], b[:2])
        self.assertEqual(a[0], 401)
        valid = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "test-password-123"})
        self.assertEqual(valid[0], 200)
        with self.server.db() as conn:
            conn.execute("UPDATE sessions SET expires_at=?", (int(time.time())-1,))
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 401)
        self.server.rate_events[("login-email", "owner@example.com")] = [time.time()]*12
        denied = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "test-password-123"})
        self.assertEqual(denied[0], 429)
        self.assertIn("Retry-After", denied[2])

    def test_registration_validation(self):
        for fields in ({"email": "bad", "name": "me", "password": "long-password"}, {"email": "ok@example.com", "name": "me", "password": "short"}, {"email": "ok@example.com", "name": "", "password": "long-password"}):
            self.assertEqual(self.request("POST", "/api/auth/register", fields)[0], 400)
        self.register()
        self.assertEqual(self.request("POST", "/api/auth/register", {"email": "OWNER@example.com", "name": "me", "password": "long-password"})[0], 409)

    def test_csrf_and_forwarded_secure_cookie(self):
        cookie, _, space = self.register()
        path = f'/api/spaces/{space["id"]}/items'
        self.assertEqual(self.request("POST", path, {"title": "csrf"}, cookie=cookie, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", path, {"title": "csrf"}, cookie=cookie, headers={"X-Requested-With": ""})[0], 403)
        self.assertEqual(self.request("POST", path, {"title": "csrf"}, cookie=cookie, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("POST", path, {"title": "same"}, cookie=cookie, headers={"Origin": "http://" + self.host})[0], 201)
        self.server.trust_proxy = True
        status, _, headers = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "test-password-123"}, headers={"Origin": "https://folio.example.cn", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "folio.example.cn"})
        self.assertEqual(status, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])
        self.assertIn("Strict-Transport-Security", headers)
        self.server.trust_proxy = False
        status, _, headers = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "test-password-123"}, headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertNotIn("; Secure", headers["Set-Cookie"])

    def test_upload_authenticated_range_and_metadata(self):
        owner, _, space = self.register()
        other, _, _ = self.register("other@example.com")
        sid = space["id"]
        payload = b"0123456789"
        status, body, _ = self.upload(owner, sid, payload, area="life")
        self.assertEqual(status, 201, body)
        item = body["item"]
        self.assertEqual(item["area"], "life")
        self.assertEqual(item["meta"]["size"], 10)
        url = item["meta"]["url"]
        self.assertEqual(self.request("GET", url)[0], 401)
        self.assertEqual(self.request("GET", url, cookie=other)[0], 404)
        status, content, headers = self.request("GET", url, cookie=owner)
        self.assertEqual((status, content), (200, payload))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, content, headers = self.request("GET", url, cookie=owner, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, content), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(self.request("GET", url, cookie=owner, headers={"Range": "bytes=-3"})[:2], (206, b"789"))
        self.assertEqual(self.request("GET", url, cookie=owner, headers={"Range": "bytes=7-"})[:2], (206, b"789"))
        for byte_range in ("bytes=99-", "bytes=5-2", "bytes=-0", "bytes=0-1,3-4", "not-a-range"):
            invalid = self.request("GET", url, cookie=owner, headers={"Range": byte_range})
            self.assertEqual(invalid[0], 416)
            self.assertEqual(invalid[2]["Content-Range"], "bytes */10")
        self.assertEqual(self.request("DELETE", f'/api/items/{item["id"]}', cookie=owner)[0], 200)
        self.assertEqual(self.request("GET", url, cookie=owner)[0], 404)
        self.assertFalse((self.server.upload_dir / item["meta"]["fileId"]).exists())

    def test_active_upload_is_download_only_and_no_meta_forgery(self):
        cookie, _, space = self.register()
        sid = space["id"]
        status, body, _ = self.upload(cookie, sid, b"<html><script>alert(1)</script></html>", "../../attack.html", "image/png")
        self.assertEqual(status, 201)
        item = body["item"]
        self.assertEqual(item["meta"]["name"], "attack.html")
        self.assertEqual(item["meta"]["mime"], "application/octet-stream")
        status, content, headers = self.request("GET", item["meta"]["url"], cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(self.request("POST", f"/api/spaces/{sid}/items", {"kind": "media", "title": "fake", "meta": {"url": "https://evil.example"}}, cookie=cookie)[0], 400)
        status, body, _ = self.request("PATCH", f'/api/items/{item["id"]}', {"meta": {"url": "https://evil.example"}, "title": "rename"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["meta"], item["meta"])
        self.assertEqual(self.request("PATCH", f'/api/items/{item["id"]}', {"kind": "note"}, cookie=cookie)[0], 400)

    def test_upload_and_json_size_limits(self):
        cookie, _, space = self.register()
        path = f'/api/spaces/{space["id"]}/uploads'
        self.assertEqual(self.upload(cookie, space["id"], b"")[0], 413)
        self.assertEqual(self.request("POST", path, cookie=cookie, raw=b"", headers={"Content-Type": "multipart/form-data; boundary=test", "Content-Length": str(MAX_UPLOAD_BYTES + 65_537)})[0], 413)
        self.assertEqual(self.request("POST", path, cookie=cookie, data={"file": "x"})[0], 415)
        self.assertEqual(self.request("POST", "/api/spaces", cookie=cookie, raw=b"", headers={"Content-Type": "application/json", "Content-Length": "1048577"})[0], 413)

    def test_slow_upload_does_not_lock_database_and_revocation_rechecked(self):
        owner, _, space = self.register()
        editor, editor_user, _ = self.register("editor@example.com")
        sid = space["id"]
        self.member(owner, sid, "editor@example.com")
        boundary = "slow-upload"
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="slow.txt"\r\nContent-Type: text/plain\r\n\r\n').encode()
        suffix = b"slow file content" + f"\r\n--{boundary}--\r\n".encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.putrequest("POST", f"/api/spaces/{sid}/uploads")
        connection.putheader("Cookie", editor)
        connection.putheader("X-Requested-With", "Workspace")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", len(prefix) + len(suffix))
        connection.endheaders(prefix)
        try:
            deadline = time.monotonic() + 2
            while self.server.upload_slots._value == 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.server.upload_slots._value, 3)
            # This write must finish while another request waits for body bytes.
            self.assertEqual(self.task(owner, sid)[0], 201)
            self.assertEqual(self.request("DELETE", f'/api/spaces/{sid}/members/{editor_user["id"]}', cookie=owner)[0], 200)
            connection.send(suffix)
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            with self.server.db() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM files").fetchone()[0], 0)
        finally:
            connection.close()

    def test_trusted_proxy_ip_rate_buckets(self):
        self.register()
        self.server.trust_proxy = True
        for client_ip in ("203.0.113.5", "203.0.113.6"):
            status, _, _ = self.request("POST", "/api/auth/login", {"email": "owner@example.com", "password": "test-password-123"}, headers={"X-Forwarded-For": "198.51.100.66, " + client_ip})
            self.assertEqual(status, 200)
            self.assertIn(("login", client_ip), self.server.rate_events)
        self.assertNotIn(("login", "198.51.100.66"), self.server.rate_events)

    def test_upload_quotas_are_atomic(self):
        cookie, _, space = self.register()
        sid = space["id"]
        self.server.max_space_storage = 5
        self.assertEqual(self.upload(cookie, sid, b"1234")[0], 201)
        self.assertEqual(self.upload(cookie, sid, b"12")[0], 413)
        self.server.max_space_storage = 1024
        self.server.max_total_storage = 5
        self.assertEqual(self.upload(cookie, sid, b"12")[0], 507)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM files").fetchone()[0], 1)
        self.assertEqual(len(list(self.server.upload_dir.iterdir())), 1)

    def test_persistent_connection_reads_distinct_bodies(self):
        cookie, _, space = self.register()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            for title in ("first", "second"):
                connection.request("POST", f'/api/spaces/{space["id"]}/items', json.dumps({"title": title}), {"Cookie": cookie, "Content-Type": "application/json", "X-Requested-With": "Workspace"})
                response = connection.getresponse()
                self.assertEqual(response.status, 201)
                self.assertEqual(json.loads(response.read())["item"]["title"], title)
        finally:
            connection.close()

    def test_safe_static_paths_and_security_headers(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/../data/folio.sqlite3")[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/data/folio.sqlite3")[0], 404)
        self.assertEqual(self.request("GET", "/%00")[0], 400)
        self.assertEqual(self.request("GET", "/api/not-found")[0], 401)

    def test_mutation_rollback(self):
        cookie, user, space = self.register()
        # Failed validation must not leave a partial item or vault row.
        self.assertEqual(self.task(cookie, space["id"], title="")[0], 400)
        self.assertEqual(self.request("PUT", f'/api/spaces/{space["id"]}/vault', {"password": "bad"}, cookie=cookie)[0], 400)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM vaults").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
