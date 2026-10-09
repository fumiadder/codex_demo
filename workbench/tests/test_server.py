"""Integration tests use real HTTP requests and a disposable SQLite database."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import COOKIE_NAME, INVITATION_ERROR, MAX_UPLOAD_BYTES, FolioServer, verify_password
from scripts.manage_access import issue_invitation, main as manage_access_main, revoke_invitation


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Full HTTP checks may run on a disposable, local PostgreSQL database.
        url = os.environ.get("TEST_WORKBENCH_POSTGRES_URL", "")
        if url and (urlsplit(url).hostname not in ("localhost", "127.0.0.1", "::1") or not urlsplit(url).path.startswith("/zhixu_tests")):
            raise ValueError("HTTP PostgreSQL QA requires a local zhixu_tests database")
        cls.database_env = mock.patch.dict(os.environ, {"DATABASE_URL": url})
        cls.database_env.start()
        cls.temp = tempfile.TemporaryDirectory()
        cls.public = Path(cls.temp.name) / "public"
        cls.public.mkdir()
        (cls.public / "index.html").write_text("<!doctype html><title>Folio</title>")
        cls.server = FolioServer(("127.0.0.1", 0), data_dir=Path(cls.temp.name)/"data", public_dir=cls.public,
                                 registration_mode="open", storage_persistence="unknown")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()
        cls.database_env.stop()

    def setUp(self):
        with self.server.db() as conn:
            for table in ("storage_jobs", "vaults", "files", "items", "members", "spaces", "sessions", "users", "registration_invites"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        self.server.trust_proxy = False
        self.server.test_mode = False
        self.server.registration_mode = "open"
        self.server.storage_persistence = "unknown"
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
        self.assertEqual(body, {"testMode": False, "storagePersistence": "unknown", "registrationMode": "open"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.server.storage_persistence = "persistent"
        self.server.registration_mode = "invite"
        with mock.patch.dict("os.environ", {"MODEL_API_KEY": "private-provider-key", "DATA_DIR": "/private/runtime-data"}):
            self.assertEqual(self.request("GET", "/api/config")[1], {
                "testMode": False, "storagePersistence": "persistent", "registrationMode": "invite"})
        self.server.test_mode = True
        status, body, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"testMode": True, "storagePersistence": "ephemeral", "registrationMode": "invite"})

    def invitation_fields(self, email="owner@example.com", code=None):
        fields = {"email": email, "name": "阿舟", "password": "test-password-123"}
        if code is not None:
            fields["invitationCode"] = code
        return fields

    def test_invitation_email_binding_single_use_and_hashed_storage(self):
        self.server.registration_mode = "invite"
        issued = issue_invitation(self.server.data_dir, "OWNER@example.com", bootstrap=True)
        code = issued["invitationCode"]
        self.assertRegex(code, r"^[A-Za-z0-9_-]{43}$")
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM registration_invites").fetchone()
            self.assertEqual(row["token_hash"], hashlib.sha256(code.encode()).hexdigest())
            self.assertNotIn(code, dict(row).values())
        missing = self.request("POST", "/api/auth/register", self.invitation_fields())
        unknown = self.request("POST", "/api/auth/register", self.invitation_fields(code="x" * 43))
        wrong_email = self.request("POST", "/api/auth/register", self.invitation_fields("other@example.com", code))
        for rejected in (missing, unknown, wrong_email):
            self.assertEqual(rejected[:2], (403, {"error": INVITATION_ERROR}))
        accepted = self.request("POST", "/api/auth/register", self.invitation_fields(" Owner@Example.com ", code))
        self.assertEqual(accepted[0], 201, accepted[1])
        self.assertEqual(accepted[1]["user"]["email"], "owner@example.com")
        self.assertNotIn("invitationCode", accepted[1]["user"])
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=code))[:2],
                         (403, {"error": INVITATION_ERROR}))
        self.assertEqual(self.request("POST", "/api/auth/login", {
            "email": "owner@example.com", "password": "test-password-123"})[0], 200)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute("SELECT consumed_at FROM registration_invites").fetchone()[0])

    def test_expired_revoked_and_malformed_invitation_codes(self):
        self.server.registration_mode = "invite"
        first = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)["invitationCode"]
        with self.server.db() as conn:
            conn.execute("UPDATE registration_invites SET expires_at=?", (int(time.time()) - 1,))
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=first))[:2],
                         (403, {"error": INVITATION_ERROR}))
        second = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)["invitationCode"]
        self.assertEqual(revoke_invitation(self.server.data_dir, "OWNER@example.com")["revoked"], 2)
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=second))[:2],
                         (403, {"error": INVITATION_ERROR}))
        for code in (True, 43, [], "short", "!" * 43):
            with self.subTest(code=code):
                self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=code))[:2],
                                 (403, {"error": INVITATION_ERROR}))
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_concurrent_invitation_redemption_creates_one_complete_account(self):
        self.server.registration_mode = "invite"
        code = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)["invitationCode"]
        barrier = threading.Barrier(2)
        def redeem():
            barrier.wait(timeout=5)
            return self.request("POST", "/api/auth/register", self.invitation_fields(code=code))
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [future.result(timeout=10) for future in (executor.submit(redeem), executor.submit(redeem))]
        self.assertEqual(sorted(result[0] for result in responses), [201, 403])
        with self.server.db() as conn:
            for table in ("users", "spaces", "members", "sessions"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1, table)
            self.assertIsNotNone(conn.execute("SELECT consumed_at FROM registration_invites").fetchone()[0])

    def test_registration_transaction_rolls_back_invitation_on_session_failure(self):
        self.server.registration_mode = "invite"
        code = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)["invitationCode"]
        with self.server.db() as conn:
            if self.server.database.dialect == "postgresql":
                conn.execute("CREATE FUNCTION reject_test_session_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled test write failure'; END $$")
                conn.execute("CREATE TRIGGER reject_test_session BEFORE INSERT ON sessions FOR EACH ROW EXECUTE FUNCTION reject_test_session_fn()")
            else:
                conn.execute("CREATE TRIGGER reject_test_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'test session write failure'); END")
        try:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=code))[0], 500)
            with self.server.db() as conn:
                for table in ("users", "spaces", "members", "sessions"):
                    self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT consumed_at FROM registration_invites").fetchone()[0])
        finally:
            with self.server.db() as conn:
                conn.execute("DROP TRIGGER reject_test_session ON sessions" if self.server.database.dialect == "postgresql" else "DROP TRIGGER reject_test_session")
                if self.server.database.dialect == "postgresql":
                    conn.execute("DROP FUNCTION reject_test_session_fn()")
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=code))[0], 201)

    def test_bootstrap_redemption_requires_empty_database(self):
        code = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)["invitationCode"]
        self.register("other@example.com")
        self.server.registration_mode = "invite"
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=code))[:2],
                         (403, {"error": INVITATION_ERROR}))
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertIsNone(conn.execute("SELECT consumed_at FROM registration_invites").fetchone()[0])

    def test_registration_and_storage_enums_reject_invalid_startup_configuration(self):
        for mode in ("closed", "INVITE", "", True):
            with self.subTest(registrationMode=mode), self.assertRaises(ValueError):
                FolioServer(("127.0.0.1", 0), data_dir=self.server.data_dir, registration_mode=mode)
        for persistence in ("durable", "PERSISTENT", "", True):
            with self.subTest(storagePersistence=persistence), self.assertRaises(ValueError):
                FolioServer(("127.0.0.1", 0), data_dir=self.server.data_dir, storage_persistence=persistence)

    def test_cli_bootstrap_and_invite_guards_and_revoke(self):
        with self.assertRaises(ValueError):
            issue_invitation(self.server.data_dir, "colleague@example.com")
        initial = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)
        with self.assertRaises(ValueError):
            issue_invitation(self.server.data_dir, "other@example.com", bootstrap=True)
        self.assertEqual(revoke_invitation(self.server.data_dir, "owner@example.com")["revoked"], 1)
        replacement = issue_invitation(self.server.data_dir, "owner@example.com", bootstrap=True)
        self.assertNotEqual(initial["invitationCode"], replacement["invitationCode"])
        self.server.registration_mode = "invite"
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields(code=replacement["invitationCode"]))[0], 201)
        with self.assertRaises(ValueError):
            issue_invitation(self.server.data_dir, "other@example.com", bootstrap=True)
        with self.assertRaises(ValueError):
            issue_invitation(self.server.data_dir, "owner@example.com")
        standard = issue_invitation(self.server.data_dir, "colleague@example.com", ttl_hours=1)
        with self.assertRaises(ValueError):
            issue_invitation(self.server.data_dir, "colleague@example.com")
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields("colleague@example.com", standard["invitationCode"]))[0], 201)
        issued = issue_invitation(self.server.data_dir, "new@example.com", ttl_hours=168)
        self.assertEqual(revoke_invitation(self.server.data_dir, "new@example.com")["revoked"], 1)
        self.assertEqual(revoke_invitation(self.server.data_dir, "new@example.com")["revoked"], 0)
        self.assertEqual(self.request("POST", "/api/auth/register", self.invitation_fields("new@example.com", issued["invitationCode"]))[0], 403)

    def test_cli_input_validation_and_failed_bootstrap_has_no_code_output(self):
        for email in ("bad", "bad\x00@example.com", "long" * 70 + "@example.com", "a@exa\nmple.com"):
            with self.subTest(email=email), self.assertRaises(ValueError):
                issue_invitation(self.server.data_dir, email, bootstrap=True)
        for ttl in (0, -1, 169, True, 1.5):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                issue_invitation(self.server.data_dir, "owner@example.com", ttl_hours=ttl, bootstrap=True)
        self.register()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = manage_access_main(["--data-dir", str(self.server.data_dir), "bootstrap", "--email", "other@example.com", "--json"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("工作台已有账号", errors.getvalue())
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM registration_invites").fetchone()[0], 0)

    def test_existing_database_survives_additive_invitation_schema_upgrade(self):
        if self.server.database.dialect != "sqlite":
            self.skipTest("Legacy SQLite backup upgrade is checked in the SQLite run")
        cookie, user, space = self.register()
        payload = self.vault()
        self.assertEqual(self.task(cookie, space["id"])[0], 201)
        self.assertEqual(self.request("PUT", f'/api/spaces/{space["id"]}/vault', payload, cookie=cookie)[0], 200)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            legacy = sqlite3.connect(data_dir / "folio.sqlite3")
            try:
                with self.server.db() as conn:
                    conn.backup(legacy)
                legacy.execute("DROP TABLE registration_invites")
                legacy.commit()
            finally:
                legacy.close()
            upgraded = FolioServer(("127.0.0.1", 0), data_dir=data_dir, public_dir=self.public,
                                   registration_mode="invite", storage_persistence="persistent")
            try:
                with upgraded.db() as conn:
                    row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                    self.assertEqual(row["email"], "owner@example.com")
                    self.assertTrue(verify_password("test-password-123", row["password_hash"]))
                    self.assertEqual(conn.execute("SELECT count(*) FROM spaces").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT count(*) FROM items").fetchone()[0], 1)
                    self.assertEqual(json.loads(conn.execute("SELECT payload FROM vaults").fetchone()[0]), payload)
                    self.assertEqual(conn.execute("SELECT count(*) FROM registration_invites").fetchone()[0], 0)
            finally:
                upgraded.server_close()

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

    def test_media_storage_failure_retains_no_visible_record_or_untracked_object(self):
        from media_storage import StorageError
        cookie, _, space = self.register()
        original = self.server.media_storage.put
        def uncertain_put(*args, **kwargs):
            original(*args, **kwargs)
            raise StorageError("controlled lost upload acknowledgement")
        with mock.patch.object(self.server.media_storage, "put", side_effect=uncertain_put):
            self.assertEqual(self.upload(cookie, space["id"])[0], 503)
        with self.server.db() as conn:
            for table in ("items", "files", "storage_jobs"):
                self.assertEqual(conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(list(self.server.upload_dir.iterdir()), [])

    def test_revoke_during_media_storage_put_rechecks_and_removes_unpublished_object(self):
        owner, _, space = self.register()
        editor, user, _ = self.register("editor@example.com")
        self.member(owner, space["id"], user["email"])
        original = self.server.media_storage.put
        def revoke_then_put(*args, **kwargs):
            # This independent write would deadlock if S3 held the DB lock.
            with self.server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM members WHERE space_id=? AND user_id=?", (space["id"], user["id"]))
            original(*args, **kwargs)
        with mock.patch.object(self.server.media_storage, "put", side_effect=revoke_then_put):
            self.assertEqual(self.upload(editor, space["id"])[0], 404)
        with self.server.db() as conn:
            for table in ("items", "files", "storage_jobs"):
                self.assertEqual(conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(list(self.server.upload_dir.iterdir()), [])

    def test_failed_object_delete_hides_media_and_preserves_quota_until_retry(self):
        import storage_jobs
        from media_storage import StorageError
        cookie, _, space = self.register()
        _, body, _ = self.upload(cookie, space["id"])
        item = body["item"]
        with mock.patch.object(self.server.media_storage, "delete", side_effect=StorageError("controlled storage failure")):
            self.assertEqual(self.request("DELETE", "/api/items/" + item["id"], cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", item["meta"]["url"], cookie=cookie)[0], 404)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 1)
            self.assertEqual(storage_jobs.usage(conn, space["id"])[0], item["meta"]["size"])
            conn.execute("UPDATE storage_jobs SET created_at=0")
        storage_jobs.collect(self.server)
        self.assertEqual(list(self.server.upload_dir.iterdir()), [])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)

    def test_late_put_after_stale_reservation_cleanup_is_tracked_and_deleted(self):
        import storage_jobs
        cookie, _, space = self.register()
        original = self.server.media_storage.put
        def delayed_put(namespace, identifier, payload, mime):
            with self.server.db() as conn:
                conn.execute("UPDATE storage_jobs SET created_at=0 WHERE id=?", (identifier,))
            storage_jobs.collect(self.server)
            original(namespace, identifier, payload, mime)
        with mock.patch.object(self.server.media_storage, "put", side_effect=delayed_put):
            self.assertEqual(self.upload(cookie, space["id"])[0], 503)
        self.assertEqual(list(self.server.upload_dir.iterdir()), [])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM files").fetchone()[0], 0)

    def test_cleanup_acknowledgement_cannot_drop_new_late_upload_deletion(self):
        import storage_jobs
        _, _, space = self.register()
        identifier = "c" * 32
        with self.server.db() as conn:
            storage_jobs.queue_delete(conn, "uploads", identifier, space["id"], 4)
        original_delete = self.server.media_storage.delete
        def old_delete_then_late_put(namespace, identity):
            original_delete(namespace, identity)
            self.server.media_storage.put(namespace, identity, b"late", "text/plain")
            storage_jobs.abandon(self.server, namespace, identity, space["id"], 4)
            return False
        with mock.patch.object(self.server.media_storage, "delete", side_effect=old_delete_then_late_put):
            storage_jobs.collect(self.server)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 1)
            self.assertEqual(storage_jobs.usage(conn, space["id"])[0], 4)
        storage_jobs.collect(self.server)
        self.assertEqual(list(self.server.upload_dir.iterdir()), [])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)

    def test_failed_deletions_do_not_starve_newer_cleanup_jobs(self):
        import storage_jobs
        from media_storage import StorageError
        _, _, space = self.register()
        with self.server.db() as conn:
            for number in range(5):
                storage_jobs.queue_delete(conn, "uploads", f"{number:032x}", space["id"], 1)
            conn.execute("UPDATE storage_jobs SET created_at=1 WHERE id!=?", (f"{4:032x}",))
        def delete(namespace, identifier):
            if identifier != f"{4:032x}":
                raise StorageError("controlled inaccessible old object")
            return False
        with mock.patch.object(self.server.media_storage, "delete", side_effect=delete), self.assertLogs(level="WARNING"):
            storage_jobs.collect(self.server)
        with mock.patch.object(self.server.media_storage, "delete", side_effect=delete):
            storage_jobs.collect(self.server)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 4)
            self.assertIsNone(conn.execute("SELECT 1 FROM storage_jobs WHERE id=?", (f"{4:032x}",)).fetchone())

    def test_committed_records_and_media_survive_server_recreation(self):
        cookie, _, space = self.register()
        _, body, _ = self.upload(cookie, space["id"])
        payload = self.vault()
        self.request("PUT", f'/api/spaces/{space["id"]}/vault', payload, cookie=cookie)
        restarted = FolioServer(("127.0.0.1", 0), data_dir=self.server.data_dir, public_dir=self.public)
        try:
            with restarted.db() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM files").fetchone()[0], 1)
                self.assertEqual(json.loads(conn.execute("SELECT payload FROM vaults").fetchone()[0]), payload)
            with restarted.media_storage.open("uploads", body["item"]["meta"]["fileId"]) as stream:
                self.assertEqual(stream.body.read(), b"example upload")
        finally:
            restarted.server_close()

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

    def test_media_upload_shares_quota_with_private_story_audio(self):
        import io
        import wave

        cookie, user, space = self.register()
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24_000)
            audio.writeframes(b"\x01\x00" * 24)
        payload = stream.getvalue()
        audio_id = "a" * 32
        audio_path = self.server.bedtime_audio_dir / audio_id
        audio_path.write_bytes(payload)
        try:
            with self.server.db() as conn:
                conn.execute("INSERT INTO bedtime_audio VALUES (?,?,?,?,?)", (audio_id, space["id"], user["id"], len(payload), int(time.time())))
            self.server.max_space_storage = len(payload) + 1
            self.assertEqual(self.upload(cookie, space["id"])[0], 413)
            self.server.max_space_storage = 1024 ** 3
            self.server.max_total_storage = len(payload) + 1
            self.assertEqual(self.upload(cookie, space["id"])[0], 507)
            self.server.max_total_storage = 10 * 1024 ** 3
            self.assertEqual(self.upload(cookie, space["id"])[0], 201)
        finally:
            audio_path.unlink(missing_ok=True)

    def test_stale_tab_cannot_read_or_write_as_another_session_user(self):
        owner, owner_user, space = self.register()
        other, other_user, _ = self.register("second@example.com")
        sid = space["id"]
        self.member(owner, sid, "second@example.com", "editor")
        stale_headers = {"X-Expected-User": owner_user["id"]}
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/items", cookie=other, headers=stale_headers)[0], 401)
        self.assertEqual(self.request("POST", f"/api/spaces/{sid}/items", {"kind": "note", "area": "work", "title": "stale tab"}, cookie=other, headers=stale_headers)[0], 401)
        current_headers = {"X-Expected-User": other_user["id"]}
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/items", cookie=other, headers=current_headers)[0], 200)
        self.assertEqual(self.request("GET", f"/api/spaces/{sid}/items", cookie=other)[1]["items"], [])

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
