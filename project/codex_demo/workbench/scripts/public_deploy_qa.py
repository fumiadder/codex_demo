#!/usr/bin/env python3
"""Exercise the free test deployment with two disposable, fake accounts.

Uses only Python's standard library. No passwords, cookies, email addresses,
IDs, response bodies, or existing user records are printed. The opaque vault
fixtures are random bytes: this verifies API persistence and user isolation,
not browser encryption. Current APIs cannot delete accounts, spaces, or the
owner's vault fixture; created items/uploads are deleted on a best-effort basis.

Public: python scripts/public_deploy_qa.py
Local:  python scripts/public_deploy_qa.py --self-test
Only use this on a deployment authorized for temporary test data.
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TARGET = "https://zhixu-free-test.onrender.com"
COOKIE_NAME = "folio_session"
MAX_RESPONSE = 4 * 1024 * 1024
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP9sAAAAASUVORK5CYII=")


class QAError(Exception):
    """A sanitized diagnostic safe to print in a public CI log."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: object

    def json(self):
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            raise QAError("response was not valid JSON") from None
        if not isinstance(value, dict):
            raise QAError("response was not a JSON object")
        return value


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if f"{target.scheme}://{target.netloc}" != self.origin or req.method not in ("GET", "HEAD"):
            raise QAError("unexpected redirect was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, origin, deadline, proxy_headers=None):
        self.origin = origin
        self.deadline = deadline
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            SameOriginRedirects(origin), urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.proxy_headers = proxy_headers or {}

    def request(self, method, path, payload=None, raw=None, headers=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise QAError("overall verification deadline exceeded")
        values = {"Accept": "application/json", "Accept-Encoding": "identity", **self.proxy_headers}
        if method not in ("GET", "HEAD"):
            values.update({"X-Requested-With": "Workspace", "Origin": self.origin})
        if payload is not None:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        req = urllib.request.Request(self.origin + path, data=raw, headers=values, method=method)
        try:
            response = self.opener.open(req, timeout=min(15, remaining))
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise QAError("network request failed or timed out") from None
        with response:
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise QAError("response exceeded verification size limit")
            return Response(response.status, body, response.headers)


def require(condition, message):
    if not condition:
        raise QAError(message)


def expect(response, status):
    require(response.status == status, f"expected HTTP {status}; received {response.status}")
    return response


def safety_headers(response, secure, api=False, file=False):
    headers = response.headers
    require(headers.get("X-Content-Type-Options") == "nosniff", "missing nosniff header")
    require(headers.get("Referrer-Policy") == "same-origin", "unexpected Referrer-Policy")
    require(headers.get("X-Frame-Options") == ("SAMEORIGIN" if file else "DENY"), "unexpected frame policy")
    csp = headers.get("Content-Security-Policy", "")
    if file:
        require("sandbox" in csp and "default-src 'none'" in csp, "missing file sandbox policy")
    else:
        require("script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp,
                "missing application CSP directives")
    if api or file:
        require("no-store" in headers.get("Cache-Control", ""), "sensitive response permits caching")
    if secure:
        require("max-age=" in headers.get("Strict-Transport-Security", ""), "missing HTTPS HSTS header")


def session_cookie(response, client, secure):
    cookies = SimpleCookie()
    for value in response.headers.get_all("Set-Cookie", []):
        cookies.load(value)
    require(COOKIE_NAME in cookies, "login response did not set a session cookie")
    morsel = cookies[COOKIE_NAME]
    require(bool(morsel["httponly"]), "session cookie is missing HttpOnly")
    require(morsel["samesite"].lower() == "strict", "session cookie is missing SameSite=Strict")
    require(morsel["path"] == "/", "session cookie has unexpected scope")
    if secure:
        require(bool(morsel["secure"]), "HTTPS session cookie is missing Secure")
    saved = [cookie for cookie in client.jar if cookie.name == COOKIE_NAME]
    require(len(saved) == 1, "cookie jar did not retain exactly one session cookie")
    if secure:
        require(saved[0].secure, "cookie jar did not retain the secure session")


def opaque_fixture():
    # Deliberately not an encryption implementation or a browser-crypto test.
    return {"version": 1, "salt": base64.b64encode(secrets.token_bytes(16)).decode(),
            "iv": base64.b64encode(secrets.token_bytes(12)).decode(),
            "ciphertext": base64.b64encode(secrets.token_bytes(48)).decode(), "kdfIterations": 310000}


class Verification:
    def __init__(self, target, max_runtime=300, startup_timeout=120, proxy_headers=None):
        self.origin = target
        self.secure = urlsplit(target).scheme == "https"
        self.deadline = time.monotonic() + max_runtime
        self.startup_timeout = startup_timeout
        self.clients = [Client(target, self.deadline, proxy_headers) for _ in range(3)]
        self.owner, self.member, self.anon = self.clients
        self.stage = "startup"
        self.passes = 0
        self.items = []
        self.accounts = []
        self.shared = None
        self.member_id = None
        self.member_added = False
        self.failed = False

    def pass_stage(self, label):
        self.passes += 1
        print("PASS " + label, flush=True)

    def startup(self):
        self.stage = "health and cold-start readiness"
        end = min(self.deadline, time.monotonic() + self.startup_timeout)
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self.anon.request("GET", "/api/health")
                if response.status == 200:
                    require(response.json().get("ok") is True, "health response did not report ready")
                    safety_headers(response, self.secure, api=True)
                    break
                require(response.status in (429, 502, 503, 504), "health endpoint returned an unexpected status")
            except QAError as error:
                if str(error) not in ("network request failed or timed out",):
                    raise
            remaining = end-time.monotonic()
            if remaining <= 0 or attempts >= 12:
                raise QAError("service did not become ready within cold-start retry budget")
            time.sleep(min(2**min(attempts-1, 3), remaining))
        self.pass_stage(self.stage)

        self.stage = "free test configuration and static security headers"
        response = expect(self.anon.request("GET", "/api/config"), 200)
        safety_headers(response, self.secure, api=True)
        config = response.json()
        require(config.get("testMode") is True, "target did not enable testMode; mutation tests were not started")
        require(config.get("storagePersistence") == "ephemeral", "target did not declare ephemeral test storage")
        for path, marker in (("/", b"test-environment-banner"), ("/app.js", b"testMode"), ("/styles.css", b"")):
            response = expect(self.anon.request("GET", path), 200)
            safety_headers(response, self.secure)
            require(bool(response.body) and marker in response.body, "expected application asset or test banner was missing")
        self.pass_stage(self.stage)

    def create_item(self, client, **fields):
        response = expect(client.request("POST", f"/api/spaces/{self.shared}/items", payload=fields), 201)
        item = response.json().get("item")
        require(isinstance(item, dict) and isinstance(item.get("id"), str), "created item response was invalid")
        self.items.append(item["id"])
        return item

    def run_checks(self):
        prefix = "TEST_" + secrets.token_hex(10)
        credentials = [{"email": f"{prefix.lower()}_{suffix}@example.invalid", "password": secrets.token_urlsafe(24),
                        "name": f"{prefix}_{suffix}"} for suffix in ("owner", "member")]
        self.stage = "two-account registration, login, and session protections"
        personal_spaces = []
        for client, credential in zip((self.owner, self.member), credentials):
            response = expect(client.request("POST", "/api/auth/register", payload=credential), 201)
            safety_headers(response, self.secure, api=True)
            session_cookie(response, client, self.secure)
            user = response.json().get("user", {})
            require(isinstance(user.get("id"), str) and user.get("email") == credential["email"], "registration user response was invalid")
            self.accounts.append(user["id"])
            expect(client.request("POST", "/api/auth/logout"), 200)
            expect(client.request("GET", "/api/me"), 401)
            response = expect(client.request("POST", "/api/auth/login", payload={"email": credential["email"], "password": credential["password"]}), 200)
            safety_headers(response, self.secure, api=True)
            session_cookie(response, client, self.secure)
            require(expect(client.request("GET", "/api/me"), 200).json()["user"]["id"] == user["id"], "session identity did not match account")
            spaces = expect(client.request("GET", "/api/spaces"), 200).json().get("spaces", [])
            require(len(spaces) == 1 and spaces[0].get("role") == "owner", "new account did not receive one private owner space")
            personal_spaces.append(spaces[0]["id"])
        self.member_id = self.accounts[1]
        self.pass_stage(self.stage)

        self.stage = "private spaces and uninvited shared-space isolation"
        expect(self.member.request("GET", f"/api/spaces/{personal_spaces[0]}/items"), 404)
        expect(self.owner.request("GET", f"/api/spaces/{personal_spaces[1]}/items"), 404)
        response = expect(self.owner.request("POST", "/api/spaces", payload={"name": prefix + "_shared"}), 201)
        space = response.json().get("space", {})
        require(isinstance(space.get("id"), str) and space.get("role") == "owner", "shared-space response was invalid")
        self.shared = space["id"]
        expect(self.member.request("GET", f"/api/spaces/{self.shared}/items"), 404)
        expect(self.anon.request("GET", f"/api/spaces/{self.shared}/items"), 401)
        self.pass_stage(self.stage)

        self.stage = "work and life records remain in their selected areas"
        work = self.create_item(self.owner, kind="note", area="work", title=prefix + "_work_note", body="Fake deployment QA work note.")
        life = self.create_item(self.owner, kind="task", area="life", title=prefix + "_life_task", body="Fake deployment QA life task.", status="todo")
        for area, expected_id in (("work", work["id"]), ("life", life["id"])):
            items = expect(self.owner.request("GET", f"/api/spaces/{self.shared}/items?area={area}"), 200).json().get("items", [])
            require([item.get("id") for item in items] == [expected_id] and all(item.get("area") == area for item in items), "area filtering returned unexpected records")
        self.pass_stage(self.stage)

        self.stage = "tiny PNG upload, private file authorization, and byte ranges"
        boundary = "qa-" + secrets.token_hex(12)
        filename = prefix + ".png"
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="area"\r\n\r\nwork\r\n'
                     f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n').encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
        response = expect(self.owner.request("POST", f"/api/spaces/{self.shared}/uploads", raw=multipart, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), 201)
        media = response.json().get("item", {})
        require(isinstance(media.get("id"), str), "upload response was invalid")
        self.items.append(media["id"])
        meta = media.get("meta", {})
        file_url = meta.get("url", "")
        require(file_url == "/api/files/" + meta.get("fileId", "") and meta.get("mime") == "image/png" and meta.get("size") == len(PNG), "upload metadata was invalid")
        expect(self.anon.request("GET", file_url), 401)
        expect(self.member.request("GET", file_url), 404)
        response = expect(self.owner.request("GET", file_url), 200)
        safety_headers(response, self.secure, file=True)
        require(response.body == PNG, "uploaded PNG bytes did not round-trip")
        response = expect(self.owner.request("GET", file_url, headers={"Range": "bytes=0-7"}), 206)
        require(response.body == PNG[:8] and response.headers.get("Content-Range") == f"bytes 0-7/{len(PNG)}", "byte-range response was invalid")
        expect(self.owner.request("GET", file_url, headers={"Range": "bytes=99999-"}), 416)
        self.pass_stage(self.stage)

        self.stage = "owner opaque vault payload round-trips without plaintext fields"
        owner_vault = opaque_fixture()
        expect(self.owner.request("PUT", f"/api/spaces/{self.shared}/vault", payload=owner_vault), 200)
        require(expect(self.owner.request("GET", f"/api/spaces/{self.shared}/vault"), 200).json().get("vault") == owner_vault, "owner opaque vault fixture did not round-trip")
        expect(self.owner.request("PUT", f"/api/spaces/{self.shared}/vault", payload={**owner_vault, "password": "TEST_fake_plaintext"}), 400)
        self.pass_stage(self.stage)

        self.stage = "viewer can read shared records and files but cannot write or escalate"
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members", payload={"email": credentials[1]["email"], "role": "viewer"}), 201)
        self.member_added = True
        shared_items = expect(self.member.request("GET", f"/api/spaces/{self.shared}/items"), 200).json().get("items", [])
        require({item.get("id") for item in shared_items} == set(self.items), "viewer could not read exactly the test-space records")
        expect(self.member.request("POST", f"/api/spaces/{self.shared}/items", payload={"kind": "note", "area": "work", "title": prefix + "_forbidden"}), 403)
        expect(self.member.request("PATCH", f'/api/items/{work["id"]}', payload={"title": prefix + "_forbidden"}), 403)
        expect(self.member.request("DELETE", f'/api/items/{media["id"]}'), 403)
        expect(self.member.request("POST", f"/api/spaces/{self.shared}/members", payload={"email": credentials[1]["email"], "role": "editor"}), 403)
        require(expect(self.member.request("GET", file_url), 200).body == PNG, "viewer could not read authorized PNG")
        require(expect(self.member.request("GET", f"/api/spaces/{self.shared}/vault"), 200).json().get("vault") is None, "owner vault fixture leaked to viewer")
        expect(self.member.request("PUT", f"/api/spaces/{self.shared}/vault", payload=opaque_fixture()), 403)
        self.pass_stage(self.stage)

        self.stage = "editor collaboration and per-account opaque vault isolation"
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/members", payload={"email": credentials[1]["email"], "role": "editor"}), 201)
        editor_note = self.create_item(self.member, kind="note", area="work", title=prefix + "_editor_note", body="Fake deployment QA editor note.")
        owner_items = expect(self.owner.request("GET", f"/api/spaces/{self.shared}/items"), 200).json().get("items", [])
        require(editor_note["id"] in {item.get("id") for item in owner_items}, "owner did not observe editor record")
        member_vault = opaque_fixture()
        expect(self.member.request("PUT", f"/api/spaces/{self.shared}/vault", payload=member_vault), 200)
        require(expect(self.member.request("GET", f"/api/spaces/{self.shared}/vault"), 200).json().get("vault") == member_vault, "member opaque vault fixture did not round-trip")
        require(expect(self.owner.request("GET", f"/api/spaces/{self.shared}/vault"), 200).json().get("vault") == owner_vault, "member write changed owner vault fixture")
        self.pass_stage(self.stage)

        self.stage = "member removal immediately revokes records, writes, files, and vault access"
        expect(self.owner.request("DELETE", f"/api/spaces/{self.shared}/members/{self.member_id}"), 200)
        self.member_added = False
        for path in (f"/api/spaces/{self.shared}/items", file_url, f"/api/spaces/{self.shared}/vault"):
            expect(self.member.request("GET", path), 404)
        expect(self.member.request("POST", f"/api/spaces/{self.shared}/items", payload={"title": prefix + "_revoked"}), 404)
        spaces = expect(self.member.request("GET", "/api/spaces"), 200).json().get("spaces", [])
        require(self.shared not in {space.get("id") for space in spaces}, "removed space remained in member listing")
        self.pass_stage(self.stage)

        self.stage = "cross-site mutation is rejected"
        expect(self.owner.request("POST", f"/api/spaces/{self.shared}/items", payload={"title": prefix + "_csrf"}, headers={"Origin": "https://test-attacker.example.invalid"}), 403)
        self.pass_stage(self.stage)

    def cleanup(self):
        failures = 0
        if self.member_added and self.shared and self.member_id:
            try:
                expect(self.owner.request("DELETE", f"/api/spaces/{self.shared}/members/{self.member_id}"), 200)
            except QAError:
                failures += 1
        for item_id in reversed(self.items):
            try:
                expect(self.owner.request("DELETE", f"/api/items/{item_id}"), 200)
            except QAError:
                failures += 1
        if self.shared:
            try:
                remaining = expect(self.owner.request("GET", f"/api/spaces/{self.shared}/items"), 200).json().get("items")
                require(remaining == [], "test-space item cleanup left records")
            except QAError:
                failures += 1
        for client in (self.owner, self.member):
            if any(cookie.name == COOKIE_NAME for cookie in client.jar):
                try:
                    expect(client.request("POST", "/api/auth/logout"), 200)
                    expect(client.request("GET", "/api/me"), 401)
                except QAError:
                    failures += 1
        if failures:
            print(f"FAIL best-effort cleanup: {failures} operation(s) did not complete", flush=True)
        else:
            self.pass_stage("created items/uploads removed and sessions logged out")
        if self.accounts:
            print("NOTE Temporary TEST_ accounts/spaces and any owner opaque vault fixture remain: current APIs have no delete operation for them.", flush=True)
        return failures == 0

    def run(self):
        success = False
        try:
            self.startup()
            self.run_checks()
            success = True
        except QAError as error:
            print(f"FAIL {self.stage}: {error}", flush=True)
        except Exception as error:
            # Avoid tracebacks or arbitrary response details in public CI logs.
            print(f"FAIL {self.stage}: unexpected {type(error).__name__}", flush=True)
        finally:
            cleanup_ok = self.cleanup()
        success = success and cleanup_ok
        label = "HTTPS public" if self.secure else "local HTTP"
        print(f"{'PASS' if success else 'FAIL'} SUMMARY: {self.passes} groups passed ({label}); opaque vault storage/isolation tested, browser encryption not tested.", flush=True)
        if not self.secure:
            print("NOTE Local HTTP deliberately skips Secure-cookie/HSTS requirements; those checks run on HTTPS targets.", flush=True)
        return 0 if success else 1


def validate_target(value, allow_local_http=False):
    parsed = urlsplit(value)
    require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "target must be an origin without credentials, query, or fragment")
    require(parsed.hostname is not None and parsed.path in ("", "/"), "target must be a site origin")
    if parsed.scheme != "https":
        require(allow_local_http and parsed.scheme == "http" and parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1"),
                "HTTPS is required; --allow-local-http only permits an explicit loopback origin")
    try:
        parsed.port
    except ValueError:
        raise QAError("target port was invalid") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def self_test(args):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from server import FolioServer
    with tempfile.TemporaryDirectory(prefix="zhixu-public-qa-") as data:
        server = FolioServer(("127.0.0.1", 0), data_dir=data, public_dir=root/"public", trust_proxy=True)
        server.test_mode = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            # Exercise the trusted-proxy path locally without impersonating
            # HTTPS: Secure-cookie/HSTS are verified only on an HTTPS target.
            proxy = {"X-Forwarded-Proto": "http", "X-Forwarded-Host": urlsplit(origin).netloc,
                     "X-Forwarded-For": "203.0.113.77"}
            return Verification(origin, args.max_runtime, args.startup_timeout, proxy).run()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default=os.environ.get("TARGET_URL", DEFAULT_TARGET))
    parser.add_argument("--allow-local-http", action="store_true", help="Allow HTTP only for localhost/loopback.")
    parser.add_argument("--self-test", action="store_true", help="Use an isolated temporary local server; no public requests.")
    parser.add_argument("--startup-timeout", type=int, default=120, help="Cold-start retry budget in seconds (30–180).")
    parser.add_argument("--max-runtime", type=int, default=300, help="Overall request budget in seconds (60–600).")
    args = parser.parse_args()
    try:
        require(30 <= args.startup_timeout <= 180 and 60 <= args.max_runtime <= 600,
                "timeout options are outside their permitted bounds")
        if args.self_test:
            return self_test(args)
        target = validate_target(args.target, args.allow_local_http)
        return Verification(target, args.max_runtime, args.startup_timeout).run()
    except QAError as error:
        print("FAIL setup: " + str(error), flush=True)
        return 1
    except Exception as error:
        print("FAIL setup: " + type(error).__name__, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
