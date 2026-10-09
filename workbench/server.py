#!/usr/bin/env python3
"""Folio's single-node server with optional durable cloud storage.

Run: python server.py. Put it behind an HTTPS reverse proxy for production.
All records are checked against workspace membership; vault payloads are opaque
and additionally scoped to the current account. This server never receives a
vault password or a plaintext vault entry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import bedtime
import storage_jobs
from media_storage import create_storage, MissingMedia, StorageError
from persistence import Database

PASSWORD_ITERATIONS = 310_000
SESSION_SECONDS = 14 * 24 * 60 * 60
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
ID_RE = r"[a-f0-9]{32}"
EMAIL_RE = re.compile(r"^[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+$")
COOKIE_NAME = "folio_session"
INVITATION_ERROR = "邀请码无效或已过期，请联系工作台管理员获取新的邀请码"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id():
    return uuid.uuid4().hex


def hash_password(password: str, salt: bytes | None = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str):
    try:
        method, iterations, salt, expected = encoded.split("$")
        if method != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 password_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
 space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
 PRIMARY KEY(space_id,user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS registration_invites (
 token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at INTEGER NOT NULL,
 consumed_at INTEGER, bootstrap INTEGER NOT NULL CHECK(bootstrap IN (0,1)),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS registration_invites_email ON registration_invites(email);
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('task','note','media')),
 area TEXT NOT NULL CHECK(area IN ('work','life')),
 title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, meta TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_space ON items(space_id,area,created_at);
CREATE TABLE IF NOT EXISTS files (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vaults (
 space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(space_id,user_id)
);
"""


class FolioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir=None, public_dir=None, trust_proxy=None, allowed_origins=None,
                 registration_mode=None, storage_persistence=None, database_url=None, media_storage=None):
        self.registration_mode = os.environ.get("REGISTRATION_MODE", "open") if registration_mode is None else registration_mode
        if self.registration_mode not in ("open", "invite"):
            raise ValueError("REGISTRATION_MODE must be open or invite")
        self.storage_persistence = os.environ.get("STORAGE_PERSISTENCE", "unknown") if storage_persistence is None else storage_persistence
        if self.storage_persistence not in ("persistent", "ephemeral", "unknown"):
            raise ValueError("STORAGE_PERSISTENCE must be persistent, ephemeral, or unknown")
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", Path(__file__).parent / "data")).resolve()
        self.public_dir = Path(public_dir or Path(__file__).parent / "public").resolve()
        self.test_mode = os.environ.get("WORKSPACE_TEST_MODE", "false").lower() in ("true", "1")
        self.upload_dir = self.data_dir / "uploads"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / "folio.sqlite3"
        self.database = Database(self.db_path, os.environ.get("DATABASE_URL") if database_url is None else database_url)
        self.media_storage = media_storage or create_storage(self.data_dir)
        if self.media_storage.backend == "s3":
            self.media_storage.verify()
        self.storage_cleanup_lock = threading.Lock()
        self.storage_maintenance_lock = threading.Lock()
        self.storage_schedule_lock = threading.Lock()
        self.storage_worker = None
        self.last_storage_cleanup = 0
        if self.database.dialect == "postgresql" and self.media_storage.backend == "local" and self.storage_persistence == "persistent":
            raise ValueError("Persistent cloud storage requires private remote media storage")
        self.trust_proxy = (os.environ.get("TRUST_PROXY", "false").lower() in ("true", "1")) if trust_proxy is None else trust_proxy
        origins = os.environ.get("ALLOWED_ORIGINS", "") if allowed_origins is None else allowed_origins
        self.allowed_origins = set(x.strip().rstrip("/") for x in origins.split(",") if x.strip())
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("ALLOWED_ORIGINS must contain comma-separated origins, e.g. https://folio.example.com")
        self.rate_lock = threading.Lock()
        self.rate_events = {}
        self.upload_slots = threading.BoundedSemaphore(4)
        self.max_space_storage = int(os.environ.get("MAX_SPACE_STORAGE_BYTES", str(1024**3)))
        default_capacity = 4 * 1024**3 if self.media_storage.backend == "s3" else 10 * 1024**3
        self.max_total_storage = int(os.environ.get("MAX_TOTAL_STORAGE_BYTES", str(default_capacity)))
        if self.max_space_storage < 1 or self.max_total_storage < 1:
            raise ValueError("Storage limits must be positive byte counts")
        self.dummy_password = hash_password(secrets.token_urlsafe(24))
        with self.db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(SCHEMA)
            conn.executescript(storage_jobs.SCHEMA)
        bedtime.initialize(self)
        self.cleanup_media()
        super().__init__(address, FolioHandler)

    def db(self):
        return self.database.connect_context()

    def cleanup_media(self):
        if self.media_storage.backend == "local":
            storage_jobs.collect(self)
            return
        # A slow private bucket must not delay an authenticated request. One
        # bounded worker is triggered by activity and exits after this batch.
        with self.storage_schedule_lock:
            if self.storage_worker is not None and self.storage_worker.is_alive():
                return
            def run():
                try:
                    storage_jobs.collect(self)
                except Exception:
                    logging.warning("Media housekeeping deferred")
            self.storage_worker = threading.Thread(target=run, daemon=True)
            self.storage_worker.start()

    def maybe_cleanup(self):
        # Only active authenticated traffic drives housekeeping. An idle free
        # database is allowed to sleep, without a polling background worker.
        if not self.storage_maintenance_lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() - self.last_storage_cleanup >= 60:
                self.last_storage_cleanup = time.monotonic()
                self.cleanup_media()
        finally:
            self.storage_maintenance_lock.release()

    def throttle(self, key, limit, window=900):
        current = time.time()
        with self.rate_lock:
            # Drop expired keys as well as individual events to bound memory use.
            if len(self.rate_events) > 5000:
                self.rate_events = {k: v for k, v in self.rate_events.items() if v and v[-1] > current-window}
                if len(self.rate_events) > 5000:
                    raise APIError(429, "请求过于频繁，请稍后重试")
            events = [value for value in self.rate_events.get(key, ()) if value > current-window]
            if len(events) >= limit:
                raise APIError(429, "请求过于频繁，请 15 分钟后重试")
            events.append(current)
            self.rate_events[key] = events


class FolioHandler(BaseHTTPRequestHandler):
    server: FolioServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Do not log auth bodies, cookies, query strings, or user-provided names.
        logging.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def secure_request(self):
        return self.server.trust_proxy and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def security_headers(self, file_response=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if file_response else "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'" if file_response else
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; frame-src 'self' blob:")
        if self.secure_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def json_response(self, status, value, cookie=None):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if status == 429:
            self.send_header("Retry-After", "900")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def session_cookie(self, token=None):
        value = f"{COOKIE_NAME}={token or ''}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if token else 0}"
        if self.secure_request():
            value += "; Secure"
        return value

    def check_mutation(self):
        if self.headers.get("X-Requested-With") != "Workspace":
            raise APIError(403, "请求校验失败")
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            if self.server.trust_proxy:
                host = self.headers.get("X-Forwarded-Host", host)
            scheme = "https" if self.secure_request() else "http"
            same = f"{scheme}://{host}"
            if origin.rstrip("/") != same and origin.rstrip("/") not in self.server.allowed_origins:
                raise APIError(403, "禁止跨站请求")
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and not origin:
            raise APIError(403, "禁止跨站请求")

    def read_body(self, max_bytes):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "不支持分块上传")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "请求长度无效")
        if length < 0:
            raise APIError(400, "请求长度无效")
        if length > max_bytes:
            self.close_connection = True
            raise APIError(413, "请求内容超过大小限制")
        # Bound total elapsed time, not only socket inactivity: a client
        # cannot keep a worker forever by sending occasional bytes.
        deadline = time.monotonic() + (180 if max_bytes > MAX_JSON_BYTES else 30)
        chunks, remaining = [], length
        while remaining:
            available = deadline - time.monotonic()
            if available <= 0:
                raise APIError(408, "上传超时，请重试")
            self.connection.settimeout(min(30, available))
            chunk = self.rfile.read1(min(65_536, remaining))
            if not chunk:
                raise APIError(400, "请求内容不完整")
            chunks.append(chunk)
            remaining -= len(chunk)
        self.connection.settimeout(30)
        return b"".join(chunks)

    def read_json(self):
        if hasattr(self, "_json_body"):
            return self._json_body
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise APIError(415, "请使用 JSON 请求")
        try:
            value = json.loads(self.read_body(MAX_JSON_BYTES))
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, "JSON 格式无效")
        if not isinstance(value, dict):
            raise APIError(400, "请求内容必须为对象")
        self._json_body = value
        return value

    @staticmethod
    def string(value, field, max_len, required=False):
        if not isinstance(value, str) or len(value) > max_len or "\x00" in value:
            raise APIError(400, f"{field}格式或长度无效")
        value = value.strip()
        if required and not value:
            raise APIError(400, f"请输入{field}")
        return value

    def client_ip(self):
        # A trusted reverse proxy must append/replace X-Forwarded-For; take
        # its last hop so a client's prepended value cannot spoof the bucket.
        if self.server.trust_proxy:
            candidate = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return self.client_address[0]

    def account_input(self, data, register=False):
        email = self.string(data.get("email", ""), "邮箱", 254, True).lower()
        password = data.get("password", "")
        if not EMAIL_RE.match(email):
            raise APIError(400, "邮箱格式无效")
        if not isinstance(password, str) or len(password) > 1024 or (register and len(password) < 10):
            raise APIError(400, "密码至少需要 10 个字符，且不得超过 1024 个字符")
        return email, password

    def current_user(self, conn):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[COOKIE_NAME].value
        except (KeyError, ValueError, CookieError):
            raise APIError(401, "请先登录")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise APIError(401, "请先登录")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = conn.execute("SELECT u.id,u.email,u.name,u.created_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (token_hash, int(time.time()))).fetchone()
        if not row:
            raise APIError(401, "登录已过期，请重新登录")
        expected_user = self.headers.get("X-Expected-User")
        if expected_user is not None and expected_user != row["id"]:
            # Cookies are shared by tabs. A page opened as A must fail closed
            # when another tab switches the browser session to B.
            raise APIError(401, "账号已切换，请重新打开工作台")
        self.token_hash = token_hash
        return dict(row)

    def membership(self, conn, space_id, user_id, write=False, owner=False):
        row = conn.execute("SELECT role FROM members WHERE space_id=? AND user_id=?", (space_id, user_id)).fetchone()
        if not row:
            raise APIError(404, "空间不存在或无权访问")
        if (write and row["role"] == "viewer") or (owner and row["role"] != "owner"):
            raise APIError(403, "没有执行此操作的权限")
        return row["role"]

    @staticmethod
    def check_item_capacity(conn, space_id):
        # The first version returns at most 2,000 items in one request.
        # Enforce that capacity so saved records never silently disappear.
        count = conn.execute("SELECT count(*) FROM items WHERE space_id=?", (space_id,)).fetchone()[0]
        if count >= 2000:
            raise APIError(409, "每个空间最多保存 2,000 个条目，请先整理已有内容")

    @staticmethod
    def item_json(row):
        result = dict(row)
        result["meta"] = json.loads(result["meta"])
        return result

    def item_input(self, data, current=None):
        current = current or {}
        kind = data.get("kind", current.get("kind", "note"))
        if kind not in ("task", "note", "media"):
            raise APIError(400, "条目类型无效")
        if kind == "media" and not current:
            raise APIError(400, "媒体条目须通过文件上传创建")
        if current and kind != current["kind"]:
            raise APIError(400, "无法更改条目类型")
        area = data.get("area", current.get("area", "work"))
        if area not in ("work", "life"):
            raise APIError(400, "请选择工作或生活分类")
        title = self.string(data.get("title", current.get("title", "")), "标题", 200, True)
        body = self.string(data.get("body", current.get("body", "")), "内容", 100_000)
        status = data.get("status", current.get("status", "todo" if kind == "task" else "active"))
        if status not in ("todo", "doing", "done", "open", "active", "archived"):
            raise APIError(400, "状态无效")
        meta = data.get("meta", current.get("meta", {}))
        if not isinstance(meta, dict):
            raise APIError(400, "meta 必须为对象")
        meta_str = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(meta_str.encode()) > 16_384:
            raise APIError(400, "附加信息超过大小限制")
        if current and kind == "media":
            # File access never relies on caller-provided metadata.
            meta_str = json.dumps(current["meta"], ensure_ascii=False, separators=(",", ":"))
        return kind, area, title, body, status, meta_str

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.json_response(405, {"error": "不支持此请求方法"})

    def dispatch(self):
        # Handlers can process multiple requests on one persistent connection.
        # Never reuse a cached body (or an upload permit) across requests.
        self.__dict__.pop("_json_body", None)
        self.__dict__.pop("_prepared_upload", None)
        self._upload_slot = False
        self._stream_response_started = False
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if "\x00" in path:
                raise APIError(400, "路径无效")
            if path.startswith("/api/"):
                if self.command not in ("GET", "HEAD"):
                    self.check_mutation()
                if self.command == "HEAD":
                    raise APIError(405, "不支持此请求方法")
                self.api(path, parse_qs(parsed.query))
            elif self.command in ("GET", "HEAD"):
                self.static(path)
            else:
                raise APIError(405, "不支持此请求方法")
        except APIError as exc:
            # A rejected request may have an unread body. Close the connection
            # so those bytes cannot become a second HTTP request.
            if self.command not in ("GET", "HEAD"):
                self.close_connection = True
            self.json_response(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except StorageError:
            self.close_connection = True
            if not self._stream_response_started:
                self.json_response(503, {"error": "媒体存储暂时不可用，请稍后重试"})
        except (ValueError, TypeError, OverflowError):
            self.close_connection = True
            self.json_response(400, {"error": "请求内容无效"})
        except Exception:
            logging.exception("Request failed")
            self.close_connection = True
            self.json_response(500, {"error": "服务暂时不可用，请稍后重试"})
        finally:
            if self._upload_slot:
                self.server.upload_slots.release()
                self._upload_slot = False

    def api(self, path, query):
        method = self.command
        if path == "/api/config" and method == "GET":
            self.json_response(200, {
                "testMode": self.server.test_mode,
                "storagePersistence": "ephemeral" if self.server.test_mode else self.server.storage_persistence,
                "registrationMode": self.server.registration_mode,
            })
            return
        if path == "/api/health" and method == "GET":
            with self.server.db() as conn:
                conn.execute("SELECT 1").fetchone()
            self.json_response(200, {"ok": True})
            return
        if path in ("/api/auth/register", "/api/auth/login") and method == "POST":
            data = self.read_json()
            register = path.endswith("register")
            email, password = self.account_input(data, register)
            ip = self.client_ip()
            # Both IP and account buckets protect shared / public networks.
            self.server.throttle(("register" if register else "login", ip), 8 if register else 30)
            if not register:
                self.server.throttle(("login-email", email), 12)
            invitation_hash = None
            if register:
                name = self.string(data.get("name", ""), "姓名", 80, True)
                if self.server.registration_mode == "invite":
                    code = data.get("invitationCode", "")
                    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
                        raise APIError(403, INVITATION_ERROR)
                    invitation_hash = hashlib.sha256(code.encode()).hexdigest()
                # Derive passwords before acquiring the database writer lock.
                encoded_password = hash_password(password)
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with self.server.db() as conn:
                if register:
                    # Creating the account, personal space, session, and consuming
                    # its single-use invitation is one serialized transaction.
                    conn.execute("BEGIN IMMEDIATE")
                    consumed_at = int(time.time())
                    if invitation_hash:
                        invite = conn.execute("SELECT email,expires_at,consumed_at,bootstrap FROM registration_invites WHERE token_hash=?", (invitation_hash,)).fetchone()
                        if not invite or invite["email"] != email or invite["expires_at"] <= consumed_at or invite["consumed_at"] is not None:
                            raise APIError(403, INVITATION_ERROR)
                        # A bootstrap code is valid only for an empty install,
                        # including when another account was created after it
                        # was issued. Keep this guard inside the same lock.
                        if invite["bootstrap"] and conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                            raise APIError(403, INVITATION_ERROR)
                    user_id, space_id, stamp = new_id(), new_id(), now_iso()
                    try:
                        conn.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user_id, email, name, encoded_password, stamp))
                    except sqlite3.IntegrityError:
                        raise APIError(409, "该邮箱已注册，请登录或更换邮箱")
                    conn.execute("INSERT INTO spaces VALUES (?,?,?,?)", (space_id, "我的空间", user_id, stamp))
                    conn.execute("INSERT INTO members VALUES (?,?,?)", (space_id, user_id, "owner"))
                    if invitation_hash:
                        changed = conn.execute("UPDATE registration_invites SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL AND expires_at>? AND email=?", (consumed_at, invitation_hash, consumed_at, email)).rowcount
                        if changed != 1:
                            raise APIError(403, INVITATION_ERROR)
                    user = {"id": user_id, "email": email, "name": name, "created_at": stamp}
                else:
                    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    valid = verify_password(password, row["password_hash"] if row else self.server.dummy_password)
                    if not row or not valid:
                        raise APIError(401, "邮箱或密码错误")
                    user = {key: row[key] for key in ("id", "email", "name", "created_at")}
                conn.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
                conn.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash, user["id"], int(time.time()) + SESSION_SECONDS))
            self.json_response(201 if register else 200, {"user": user}, self.session_cookie(token))
            return
        if bedtime.dispatch(self, path, query):
            return
        with self.server.db() as conn:
            user = self.current_user(conn)
            conn.commit()
            self.server.maybe_cleanup()
            if method not in ("GET", "HEAD"):
                conn.commit()
                upload_route = re.fullmatch(rf"/api/spaces/({ID_RE})/uploads", path)
                if method == "POST" and upload_route:
                    # Reject unauthorized uploads before reading large bodies.
                    self.membership(conn, upload_route[1], user["id"], write=True)
                    conn.commit()
                    self._prepared_upload = self.prepare_upload()
                elif method in ("POST", "PATCH", "PUT") and path != "/api/auth/logout":
                    self.read_json()
                else:
                    self.read_body(MAX_JSON_BYTES)
                # Acquire the writer lock only AFTER parsing the entire body.
                # Recheck auth and membership in this transaction so a revoke
                # during an upload cannot race the subsequent write.
                conn.execute("BEGIN IMMEDIATE")
                user = self.current_user(conn)
            uid = user["id"]
            if path == "/api/me" and method == "GET":
                result = {"user": user}
            elif path == "/api/auth/logout" and method == "POST":
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash,))
                conn.commit()
                self.json_response(200, {"ok": True}, self.session_cookie())
                return
            elif path == "/api/spaces" and method == "GET":
                rows = conn.execute("SELECT s.*,m.role,(SELECT count(*) FROM members x WHERE x.space_id=s.id) AS member_count FROM spaces s JOIN members m ON m.space_id=s.id WHERE m.user_id=? ORDER BY s.created_at,s.id", (uid,)).fetchall()
                result = {"spaces": [dict(row) for row in rows]}
            elif path == "/api/spaces" and method == "POST":
                data = self.read_json()
                name = self.string(data.get("name", ""), "空间名称", 80, True)
                count = conn.execute("SELECT count(*) FROM spaces WHERE owner_id=?", (uid,)).fetchone()[0]
                if count >= 50:
                    raise APIError(400, "每个账号最多可创建 50 个空间")
                space_id, stamp = new_id(), now_iso()
                conn.execute("INSERT INTO spaces VALUES (?,?,?,?)", (space_id, name, uid, stamp))
                conn.execute("INSERT INTO members VALUES (?,?,?)", (space_id, uid, "owner"))
                result = {"space": {"id": space_id, "name": name, "owner_id": uid, "created_at": stamp, "role": "owner", "member_count": 1}}
            elif match := re.fullmatch(rf"/api/spaces/({ID_RE})/items", path):
                sid = match[1]
                self.membership(conn, sid, uid, write=method == "POST")
                if method == "GET":
                    area = query.get("area", [None])[0]
                    if area is not None and area not in ("work", "life"):
                        raise APIError(400, "分类无效")
                    rows = conn.execute("SELECT * FROM items WHERE space_id=?" + (" AND area=?" if area else "") + " ORDER BY created_at DESC,id DESC LIMIT 2000", (sid, area) if area else (sid,)).fetchall()
                    result = {"items": [self.item_json(row) for row in rows]}
                elif method == "POST":
                    values = self.item_input(self.read_json())
                    self.check_item_capacity(conn, sid)
                    iid, stamp = new_id(), now_iso()
                    conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", (iid, sid, *values, uid, stamp, stamp))
                    result = {"item": self.item_json(conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())}
                else:
                    raise APIError(405, "不支持此请求方法")
            elif match := re.fullmatch(rf"/api/items/({ID_RE})", path):
                row = conn.execute("SELECT * FROM items WHERE id=?", (match[1],)).fetchone()
                if not row:
                    raise APIError(404, "条目不存在或无权访问")
                self.membership(conn, row["space_id"], uid, write=True)
                if method == "PATCH":
                    current = self.item_json(row)
                    values = self.item_input(self.read_json(), current)
                    conn.execute("UPDATE items SET kind=?,area=?,title=?,body=?,status=?,meta=?,updated_at=? WHERE id=?", (*values, now_iso(), row["id"]))
                    result = {"item": self.item_json(conn.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone())}
                elif method == "DELETE":
                    for file in conn.execute("SELECT * FROM files WHERE item_id=?", (row["id"],)).fetchall():
                        storage_jobs.queue_delete(conn, "uploads", file["id"], file["space_id"], file["size"])
                    conn.execute("DELETE FROM items WHERE id=?", (row["id"],))
                    conn.commit()
                    self.server.cleanup_media()
                    result = {"ok": True}
                else:
                    raise APIError(405, "不支持此请求方法")
            elif match := re.fullmatch(rf"/api/spaces/({ID_RE})/members(?:/({ID_RE}))?", path):
                sid, member_id = match[1], match[2]
                self.membership(conn, sid, uid, owner=method != "GET")
                if method == "GET" and member_id is None:
                    rows = conn.execute("SELECT u.id,u.email,u.name,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE m.space_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,u.name", (sid,)).fetchall()
                    result = {"members": [dict(row) for row in rows]}
                elif method == "POST" and member_id is None:
                    data = self.read_json()
                    email = self.string(data.get("email", ""), "邮箱", 254, True).lower()
                    role = data.get("role", "editor")
                    if role not in ("editor", "viewer"):
                        raise APIError(400, "成员角色只能是编辑者或查看者")
                    target = conn.execute("SELECT id,email,name FROM users WHERE email=?", (email,)).fetchone()
                    if not target:
                        raise APIError(404, "此邮箱尚未注册，请对方先创建账号")
                    if target["id"] == uid:
                        raise APIError(400, "无法修改空间所有者的角色")
                    conn.execute("INSERT INTO members VALUES (?,?,?) ON CONFLICT(space_id,user_id) DO UPDATE SET role=excluded.role", (sid, target["id"], role))
                    result = {"member": {**dict(target), "role": role}}
                elif method == "DELETE" and member_id is not None:
                    target = conn.execute("SELECT role FROM members WHERE space_id=? AND user_id=?", (sid, member_id)).fetchone()
                    if not target:
                        raise APIError(404, "成员不存在")
                    if target["role"] == "owner":
                        raise APIError(400, "无法移除空间所有者")
                    conn.execute("DELETE FROM members WHERE space_id=? AND user_id=?", (sid, member_id))
                    # Revoking membership removes their personal vault copy too.
                    conn.execute("DELETE FROM vaults WHERE space_id=? AND user_id=?", (sid, member_id))
                    result = {"ok": True}
                else:
                    raise APIError(405, "不支持此请求方法")
            elif match := re.fullmatch(rf"/api/spaces/({ID_RE})/vault", path):
                sid = match[1]
                self.membership(conn, sid, uid, write=method == "PUT")
                if method == "GET":
                    row = conn.execute("SELECT payload FROM vaults WHERE space_id=? AND user_id=?", (sid, uid)).fetchone()
                    result = {"vault": json.loads(row["payload"]) if row else None}
                elif method == "PUT":
                    payload = self.validate_vault(self.read_json())
                    conn.execute("INSERT INTO vaults VALUES (?,?,?,?) ON CONFLICT(space_id,user_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (sid, uid, json.dumps(payload), now_iso()))
                    result = {"ok": True}
                else:
                    raise APIError(405, "不支持此请求方法")
            elif match := re.fullmatch(rf"/api/spaces/({ID_RE})/uploads", path):
                if method != "POST":
                    raise APIError(405, "不支持此请求方法")
                sid = match[1]
                self.membership(conn, sid, uid, write=True)
                result = self.upload(conn, sid, uid)
            elif match := re.fullmatch(rf"/api/files/({ID_RE})", path):
                if method != "GET":
                    raise APIError(405, "不支持此请求方法")
                row = conn.execute("SELECT * FROM files WHERE id=?", (match[1],)).fetchone()
                if not row:
                    raise APIError(404, "文件不存在或无权访问")
                self.membership(conn, row["space_id"], uid)
                conn.commit()
                self.serve_file(row)
                return
            else:
                raise APIError(404, "接口不存在")
        self.json_response(201 if method == "POST" else 200, result)

    @staticmethod
    def validate_vault(data):
        keys = {"version", "salt", "iv", "ciphertext", "kdfIterations"}
        if set(data) != keys or type(data["version"]) is not int or data["version"] != 1:
            raise APIError(400, "密码箱密文格式无效")
        iterations = data["kdfIterations"]
        if type(iterations) is not int or not 310_000 <= iterations <= 2_000_000:
            raise APIError(400, "密码箱密钥参数无效")
        decoded = {}
        for key in ("salt", "iv", "ciphertext"):
            if not isinstance(data[key], str):
                raise APIError(400, "密码箱密文格式无效")
            try:
                decoded[key] = base64.b64decode(data[key], validate=True)
            except (ValueError, TypeError):
                raise APIError(400, "密码箱密文格式无效")
        if len(decoded["salt"]) != 16 or len(decoded["iv"]) != 12 or not 16 <= len(decoded["ciphertext"]) <= 600_000:
            raise APIError(400, "密码箱密文长度无效")
        return data

    @staticmethod
    def sniff_mime(data, filename, declared):
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return "audio/wav"
        if data.startswith(b"%PDF-"):
            return "application/pdf"
        if data.startswith(b"OggS"):
            return "audio/ogg"
        if data.startswith(b"fLaC"):
            return "audio/flac"
        if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
            return "audio/mpeg"
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "audio/mp4" if declared == "audio/mp4" else "video/mp4"
        if data.startswith(b"\x1a\x45\xdf\xa3"):
            return "audio/webm" if declared == "audio/webm" else "video/webm"
        if Path(filename).suffix.lower() in (".txt", ".md", ".csv", ".json"):
            try:
                data.decode("utf-8")
                if b"\x00" not in data:
                    return "text/plain"
            except UnicodeDecodeError:
                pass
        # Active formats including HTML and SVG remain downloadable only.
        return "application/octet-stream"

    def prepare_upload(self):
        if not self.server.upload_slots.acquire(blocking=False):
            raise APIError(429, "上传任务较多，请稍后重试")
        self._upload_slot = True
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;") or "\r" in content_type or "\n" in content_type:
            raise APIError(415, "请选择文件上传")
        body = self.read_body(MAX_UPLOAD_BYTES + 65_536)
        message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        if not message.is_multipart() or message.defects:
            raise APIError(400, "上传格式无效")
        files, fields = [], {}
        parts = list(message.iter_parts())
        if len(parts) > 10:
            raise APIError(400, "上传字段过多")
        for part in parts:
            if part.is_multipart() or part.defects or part.get_content_disposition() != "form-data":
                raise APIError(400, "上传格式无效")
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if payload is None:
                raise APIError(400, "上传内容无效")
            filename = part.get_filename()
            if filename is not None:
                if name != "file":
                    raise APIError(400, "文件字段必须为 file")
                files.append((filename, part.get_content_type(), payload))
            elif name in ("area", "title"):
                if len(payload) > 1024:
                    raise APIError(400, "上传字段过长")
                fields[name] = payload.decode("utf-8")
        if len(files) != 1:
            raise APIError(400, "每次请选择一个文件")
        filename, declared, payload = files[0]
        if not payload or len(payload) > MAX_UPLOAD_BYTES:
            raise APIError(413, "文件须为 1 字节至 25 MB")
        filename = filename.replace("\\", "/").split("/")[-1]
        filename = self.string(filename, "文件名称", 255, True)
        if any(ord(char) < 32 or ord(char) == 127 for char in filename):
            raise APIError(400, "文件名称无效")
        area = fields.get("area", "work")
        if area not in ("work", "life"):
            raise APIError(400, "分类无效")
        title = self.string(fields.get("title", filename), "标题", 200, True)
        mime = self.sniff_mime(payload, filename, declared)
        return filename, mime, payload, area, title

    def upload(self, conn, sid, uid):
        filename, mime, payload, area, title = self._prepared_upload
        # Release the request transaction before retrying private object cleanup.
        conn.commit()
        self.server.cleanup_media()
        conn.execute("BEGIN IMMEDIATE")
        self.current_user(conn)
        self.membership(conn, sid, uid, write=True)
        self.check_item_capacity(conn, sid)
        space_bytes, total_bytes = storage_jobs.usage(conn, sid)
        if space_bytes + len(payload) > self.server.max_space_storage:
            raise APIError(413, "空间存储容量已满，请先删除不需要的文件")
        if total_bytes + len(payload) > self.server.max_total_storage:
            raise APIError(507, "服务器可用存储不足，请联系管理员")
        if self.server.media_storage.backend == "local" and shutil.disk_usage(self.server.data_dir).free < len(payload) + 100*1024*1024:
            raise APIError(507, "服务器可用存储不足，请联系管理员")
        file_id, item_id, stamp = new_id(), new_id(), now_iso()
        storage_jobs.reserve(conn, "uploads", file_id, sid, len(payload))
        # The durable reservation enforces quota while S3 runs without a DB lock.
        conn.commit()
        try:
            self.server.media_storage.put("uploads", file_id, payload, mime)
            conn.execute("BEGIN IMMEDIATE")
            self.current_user(conn)
            self.membership(conn, sid, uid, write=True)
            self.check_item_capacity(conn, sid)
            storage_jobs.assert_pending(conn, "uploads", file_id)
            meta = {"fileId": file_id, "url": "/api/files/" + file_id, "mime": mime, "name": filename, "size": len(payload)}
            conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)", (item_id, sid, "media", area, title, "", "active", json.dumps(meta, ensure_ascii=False), uid, stamp, stamp))
            conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (file_id, sid, item_id, filename, mime, len(payload), stamp))
            storage_jobs.publish(conn, "uploads", file_id)
            conn.commit()
            return {"item": self.item_json(conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())}
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            storage_jobs.abandon(self.server, "uploads", file_id, sid, len(payload))
            raise

    def serve_file(self, row):
        return self.serve_media("uploads", row, row["mime"], row["name"])

    def serve_media(self, namespace, row, mime, filename):
        size = row["size"]
        start, end, partial = 0, size - 1, False
        byte_range = self.headers.get("Range")
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
            if not match or not any(match.groups()):
                self.range_error(size)
                return
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), size-1) if match[2] else size-1
            else:
                suffix = int(match[2])
                if suffix <= 0:
                    self.range_error(size)
                    return
                start, end = max(0, size-suffix), size-1
            if start >= size or start > end:
                self.range_error(size)
                return
            partial = True
        length = end-start+1
        try:
            stream = self.server.media_storage.open(namespace, row["id"], byte_range=(start, end) if partial else None)
        except MissingMedia:
            raise APIError(404, "文件不存在或已过期")
        with stream:
            if stream.total_size != size or stream.size != length:
                raise StorageError("Private media size mismatch")
            self._stream_response_started = True
            self.send_response(206 if partial else 200)
            self.security_headers(file_response=True)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime == "text/plain" else ""))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            from urllib.parse import quote
            disposition = "attachment" if mime == "application/octet-stream" else "inline"
            self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            remaining = length
            try:
                while remaining > 0:
                    chunk = stream.body.read(min(65_536, remaining))
                    if not chunk:
                        raise StorageError("Private media stream truncated")
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except StorageError:
                # Headers are already sent; terminate instead of appending JSON.
                self.close_connection = True
                raise BrokenPipeError("Private media stream interrupted") from None

    def range_error(self, size):
        self.send_response(416)
        self.security_headers(file_response=True)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def static(self, request_path):
        if request_path == "/":
            request_path = "/index.html"
        candidate = (self.server.public_dir / request_path.lstrip("/")).resolve()
        if not candidate.is_relative_to(self.server.public_dir) or not candidate.is_file():
            raise APIError(404, "页面不存在")
        if candidate.name.startswith("."):
            raise APIError(404, "页面不存在")
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    address = (os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000")))
    server = FolioServer(address)
    logging.info("Folio listening on %s:%d", *server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
