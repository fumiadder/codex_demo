"""notify.py 的 SSRF、发送泵、通知生成与路由契约测试。

全部使用 fake opener 与 monkeypatch 的 getaddrinfo，无任何真实外网依赖。
"""
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify
from server import FolioServer

SHANGHAI_TZ = timezone(timedelta(hours=8))
PUBLIC_IP = "93.184.216.34"
SAFE_URL = "https://hooks.example.com/zhixu"


def public_getaddrinfo(host, port, type=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port or 443))]


def ip_getaddrinfo(ip, family=socket.AF_INET):
    def _resolve(host, port, type=0):
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 443))]
    return _resolve


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, size=-1):
        return b"{}"

    def close(self):
        pass

    def getcode(self):
        return self.status


class FakeOpener:
    """记录全部请求并按脚本返回状态码或抛出给定异常。"""

    def __init__(self):
        self.requests = []
        self.behavior = 200

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        behavior = self.behavior
        if isinstance(behavior, BaseException):
            raise behavior
        if isinstance(behavior, list):
            code = behavior.pop(0)
        else:
            code = behavior
        if isinstance(code, BaseException):
            raise code
        if code >= 400:
            raise HTTPError(request.full_url, code, "error", {}, io.BytesIO(b"{}"))
        return FakeResponse(code)


class UrlValidationTests(unittest.TestCase):
    def setUp(self):
        self.resolver = mock.patch.object(notify, "_resolve", side_effect=public_getaddrinfo)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def test_public_https_passes(self):
        self.assertEqual(notify.validate_webhook_url(SAFE_URL), SAFE_URL)
        self.assertEqual(notify.validate_webhook_url("https://hooks.example.com:8443/path?x=1"),
                         "https://hooks.example.com:8443/path?x=1")

    def test_non_https_schemes_rejected(self):
        for raw in (
            "http://hooks.example.com/hook",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "gopher://hooks.example.com/_x",
            "ftp://hooks.example.com/hook",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    notify.validate_webhook_url(raw)

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError):
            notify.validate_webhook_url("https://user:secret@hooks.example.com/hook")
        with self.assertRaises(ValueError):
            notify.validate_webhook_url("https://user@hooks.example.com/hook")

    def test_missing_hostname_rejected(self):
        for raw in ("https:///path", "", "not a url"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    notify.validate_webhook_url(raw)

    def test_blocked_ipv4_targets(self):
        blocked = ("127.0.0.1", "127.1.2.3", "10.0.0.5", "172.16.0.1", "172.31.255.255",
                   "192.168.1.10", "169.254.169.254", "0.0.0.0")
        for ip in blocked:
            with self.subTest(ip=ip):
                with mock.patch.object(notify, "_resolve", side_effect=ip_getaddrinfo(ip)):
                    with self.assertRaises(ValueError):
                        notify.validate_webhook_url(f"https://{ip}/hook")

    def test_blocked_ipv6_targets(self):
        blocked = (("::1", socket.AF_INET6), ("fc00::1", socket.AF_INET6),
                   ("fe80::1", socket.AF_INET6), ("ff02::1", socket.AF_INET6))
        for ip, family in blocked:
            with self.subTest(ip=ip):
                with mock.patch.object(notify, "_resolve",
                                       side_effect=ip_getaddrinfo(ip, family)):
                    with self.assertRaises(ValueError):
                        notify.validate_webhook_url(f"https://[{ip}]/hook")

    def test_dns_failure_rejected(self):
        with mock.patch.object(notify, "_resolve", side_effect=socket.gaierror("no name")):
            with self.assertRaises(ValueError):
                notify.validate_webhook_url("https://missing.example.com/hook")

    def test_redirect_handler_raises(self):
        handler = notify._NoRedirect()
        with self.assertRaises(notify.RedirectBlocked):
            handler.redirect_request(None, None, 302, "Found", {}, "https://169.254.169.254/")


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.resolver = mock.patch.object(notify, "_resolve", side_effect=public_getaddrinfo)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.opener = FakeOpener()
        self.opener_patch = mock.patch.object(notify, "build_safe_opener", return_value=self.opener)
        self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)

    def test_signature_headers_and_success(self):
        payload = {"event": "webhook.test", "test": True}
        result = notify.send_webhook(SAFE_URL, "topsecret", "webhook.test", "deliv-1", payload)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(self.opener.requests), 1)
        request, timeout = self.opener.requests[0]
        self.assertEqual(timeout, notify.REQUEST_TIMEOUT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Content-type"], "application/json")
        self.assertEqual(request.headers["X-zhixu-event"], "webhook.test")
        self.assertEqual(request.headers["X-zhixu-delivery"], "deliv-1")
        body = request.data
        expected = notify.hmac.new(b"topsecret", body, notify.hashlib.sha256).hexdigest()
        self.assertEqual(request.headers["X-zhixu-signature"], expected)
        self.assertEqual(json.loads(body), payload)

    def test_ssrf_block_is_permanent_failure(self):
        with mock.patch.object(notify, "_resolve",
                               side_effect=ip_getaddrinfo("169.254.169.254")):
            result = notify.send_webhook("https://metadata.example/hook", "s", "task.due", "d", {})
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(self.opener.requests, [])

    def test_429_and_5xx_retryable_other_4xx_permanent(self):
        for code, retryable in ((429, True), (500, True), (503, True), (504, True),
                                (400, False), (401, False), (403, False), (404, False)):
            with self.subTest(code=code):
                self.opener.behavior = code
                result = notify.send_webhook(SAFE_URL, "s", "task.due", "d", {})
                self.assertEqual(result.ok, False)
                self.assertEqual(result.retryable, retryable)

    def test_timeout_and_url_error_retryable(self):
        for exc in (socket.timeout("timed out"), TimeoutError(), OSError("unreachable")):
            with self.subTest(exc=type(exc).__name__):
                self.opener.behavior = exc
                result = notify.send_webhook(SAFE_URL, "s", "task.due", "d", {})
                self.assertFalse(result.ok)
                self.assertTrue(result.retryable)

    def test_never_raises_on_unexpected_error(self):
        class WeirdError(Exception):
            pass
        self.opener.behavior = WeirdError("boom")
        result = notify.send_webhook(SAFE_URL, "s", "task.due", "d", {})
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)


class NotifyServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.public = Path(cls.temp.name) / "public"
        cls.public.mkdir()
        (cls.public / "index.html").write_text("<!doctype html><title>Folio</title>")
        cls.server = FolioServer(("127.0.0.1", 0), data_dir=Path(cls.temp.name) / "data",
                                 public_dir=cls.public, registration_mode="open",
                                 storage_persistence="unknown")
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
            for table in ("webhook_deliveries", "notifications", "user_webhooks",
                          "storage_jobs", "vaults", "files", "items", "members",
                          "spaces", "sessions", "users", "registration_invites"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        notify._last_maintenance = 0.0
        self.resolver = mock.patch.object(notify, "_resolve", side_effect=public_getaddrinfo)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.opener = FakeOpener()
        self.opener_patch = mock.patch.object(notify, "build_safe_opener", return_value=self.opener)
        self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        # 维护测试默认不启动后台线程，需要发送时显式调用同步 drain。
        self.pump_patch = mock.patch.object(notify, "pump", lambda server: None)
        self.pump_patch.start()
        self.addCleanup(self.pump_patch.stop)

    # ----- 夹具辅助 -------------------------------------------------------- #
    def request(self, method, path, data=None, cookie=None):
        headers = {"X-Requested-With": "Workspace"}
        raw = None
        if cookie:
            headers["Cookie"] = cookie
        if data is not None:
            raw = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=8)
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        if result_headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(body)
        return status, body

    def register(self, email="owner@example.com"):
        status, body, headers = self.request_raw_register(email)
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";")[0]
        _, spaces = self.request("GET", "/api/spaces", cookie=cookie)
        return cookie, body["user"], spaces["spaces"][0]

    def request_raw_register(self, email):
        headers = {"X-Requested-With": "Workspace", "Content-Type": "application/json"}
        raw = json.dumps({"email": email, "name": "阿舟", "password": "test-password-123"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=8)
        conn.request("POST", "/api/auth/register", body=raw, headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        result_headers = dict(response.getheaders())
        conn.close()
        return response.status, body, result_headers

    def create_task(self, cookie, sid, title="整理周报", due=None, status="todo", remind_at=None):
        meta = {}
        if due is not None:
            meta["due"] = due
        if remind_at is not None:
            meta["remindAt"] = remind_at
        return self.request("POST", f"/api/spaces/{sid}/items",
                            {"kind": "task", "area": "work", "title": title,
                             "status": status, "meta": meta}, cookie=cookie)

    def create_webhook(self, cookie, **overrides):
        payload = {"name": "我的通知通道", "url": SAFE_URL, "events": ["task.due"]}
        payload.update(overrides)
        return self.request("POST", "/api/webhooks", payload, cookie=cookie)

    def table_count(self, table, where="", params=()):
        with self.server.db() as conn:
            return conn.execute(
                "SELECT count(*) FROM " + table + (" WHERE " + where if where else ""), params
            ).fetchone()[0]

    @staticmethod
    def day(offset):
        return (datetime.now(SHANGHAI_TZ).date() + timedelta(days=offset)).isoformat()

    def seed_delivery(self, uid, hook_id, state="pending", next_attempt_at=None, attempts=0):
        delivery_id = uuid.uuid4().hex
        now = int(time.time())
        with self.server.db() as conn:
            conn.execute(
                "INSERT INTO webhook_deliveries "
                "(id,webhook_id,user_id,event,payload,state,attempts,next_attempt_at,last_error,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (delivery_id, hook_id, uid, "task.due",
                 json.dumps({"event": "task.due"}), state, attempts,
                 now if next_attempt_at is None else next_attempt_at, "", now),
            )
        return delivery_id

    # ----- 通知生成与去重 -------------------------------------------------- #
    def test_due_task_creates_notification_once_with_dedup(self):
        cookie, user, space = self.register()
        yesterday = self.day(-1)
        status, body = self.create_task(cookie, space["id"], due=yesterday)
        self.assertEqual(status, 201, body)
        item_id = body["item"]["id"]

        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 1)
        with self.server.db() as conn:
            note = conn.execute("SELECT * FROM notifications").fetchone()
        self.assertEqual(note["user_id"], user["id"])
        self.assertEqual(note["type"], "task.due")
        self.assertEqual(note["dedup_key"], f"task-due:{item_id}:{yesterday}")
        self.assertEqual(note["read"], 0)
        self.assertEqual(json.loads(note["data"]),
                         {"title": "整理周报", "body": f"任务「整理周报」已于 {yesterday} 到期，请及时处理。",
                          "itemId": item_id, "spaceId": space["id"], "due": yesterday})
        self.assertIn(yesterday, note["body"])
        self.assertIn("整理周报", note["title"])

        # 第二次维护不重复通知。
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 1)

        status, body = self.request("GET", "/api/notifications", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["unreadCount"], 1)
        self.assertEqual(len(body["notifications"]), 1)
        self.assertEqual(body["notifications"][0]["id"], note["id"])

    def test_scheduled_remind_creates_notification_with_remind_at(self):
        cookie, user, space = self.register()
        # 1 分钟前到达提醒时刻，仍在 15 分钟补发窗口内。
        remind_at = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() - 60))
        status, body = self.create_task(cookie, space["id"], remind_at=remind_at)
        self.assertEqual(status, 201, body)
        item_id = body["item"]["id"]

        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 1)
        with self.server.db() as conn:
            note = conn.execute("SELECT * FROM notifications").fetchone()
        self.assertEqual(note["user_id"], user["id"])
        self.assertEqual(note["type"], "task.due")
        self.assertEqual(note["dedup_key"], f"task-remind:{item_id}:{remind_at}")
        self.assertEqual(note["read"], 0)
        data = json.loads(note["data"])
        self.assertEqual(data["itemId"], item_id)
        self.assertEqual(data["spaceId"], space["id"])
        self.assertEqual(data["remindAt"], remind_at)
        self.assertIn("提醒时间到了", note["body"])

        # 第二次维护不重复通知。
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 1)

    def test_future_remind_at_suppresses_due_notification(self):
        cookie, _, space = self.register()
        # 设了未来提醒时间的任务即使今天到期，也只等提醒时间，不走「按日期到期」路径。
        remind_at = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 3600))
        status, body = self.create_task(cookie, space["id"], due=self.day(0), remind_at=remind_at)
        self.assertEqual(status, 201, body)
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 0)

    def test_recurring_task_new_due_creates_new_notification(self):
        cookie, _, space = self.register()
        first_due, second_due = self.day(-2), self.day(-1)
        status, body = self.create_task(cookie, space["id"], due=first_due)
        item_id = body["item"]["id"]
        notify.run_maintenance(self.server, force=True)
        # 重复任务滚动到新 due：直接更新 meta（等价 recur 滚动后的落库形态）。
        with self.server.db() as conn:
            conn.execute("UPDATE items SET meta=?, updated_at=? WHERE id=?",
                         (json.dumps({"due": second_due}), "2026-01-02T00:00:00Z", item_id))
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 2)
        keys = {row["dedup_key"] for row in self._all_notifications()}
        self.assertEqual(keys, {f"task-due:{item_id}:{first_due}", f"task-due:{item_id}:{second_due}"})

    def test_done_and_future_tasks_generate_nothing(self):
        cookie, _, space = self.register()
        self.create_task(cookie, space["id"], title="已完成", due=self.day(-3), status="done")
        self.create_task(cookie, space["id"], title="未来任务", due=self.day(3))
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 0)

    def test_maintenance_throttle_and_bad_meta_skipped(self):
        cookie, _, space = self.register()
        self.create_task(cookie, space["id"], due=self.day(-1))
        # 认证请求触发的 maybe_cleanup 可能已推进模块级节流时间戳；此处显式归零，
        # 让断言只验证「非强制调用可立即执行」本身，不受前面请求时序影响。
        notify._last_maintenance = 0.0
        notify.run_maintenance(self.server)
        self.assertEqual(self.table_count("notifications"), 1)
        # 60s 节流：不带 force 的第二次调用直接跳过。
        notify.run_maintenance(self.server)
        self.assertEqual(self.table_count("notifications"), 1)
        # 脏 meta 不影响整轮维护。
        with self.server.db() as conn:
            conn.execute("INSERT INTO items (id,space_id,kind,area,title,body,status,meta,created_by,created_at,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("a" * 32, space["id"], "task", "work", "脏数据", "", "todo", "{broken",
                          self._uid(cookie), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        notify.run_maintenance(self.server, force=True)
        self.assertEqual(self.table_count("notifications"), 1)

    def _uid(self, cookie):
        _, body = self.request("GET", "/api/me", cookie=cookie)
        return body["user"]["id"]

    def _all_notifications(self):
        with self.server.db() as conn:
            return conn.execute("SELECT * FROM notifications").fetchall()

    def test_delivery_enqueued_only_for_subscribed_webhooks(self):
        cookie, user, space = self.register()
        _, matching = self.create_webhook(cookie, name="匹配", events=["task.due"])
        _, wildcard = self.create_webhook(cookie, name="通配", events=["*"])
        # 空事件数组在创建时即被拒绝（400），无法落库。
        self.assertEqual(self.create_webhook(cookie, name="空订阅", events=[])[0], 400)
        # 停用的 webhook 不入队。
        with self.server.db() as conn:
            conn.execute("UPDATE user_webhooks SET enabled=0 WHERE id=?",
                         (wildcard["webhook"]["id"],))
        self.create_task(cookie, space["id"], due=self.day(-1))
        notify.run_maintenance(self.server, force=True)
        with self.server.db() as conn:
            rows = conn.execute("SELECT webhook_id FROM webhook_deliveries").fetchall()
            payload = json.loads(conn.execute(
                "SELECT payload FROM webhook_deliveries").fetchone()["payload"])
        self.assertEqual([row["webhook_id"] for row in rows], [matching["webhook"]["id"]])
        self.assertEqual(payload["event"], "task.due")
        self.assertEqual(payload["notification"]["type"], "task.due")

    # ----- 发送泵 ---------------------------------------------------------- #
    def test_drain_marks_sent_on_2xx(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        delivery_id = self.seed_delivery(user["id"], hook_id)
        notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(len(self.opener.requests), 1)

    def test_drain_backoff_retry_then_failed_after_five(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        delivery_id = self.seed_delivery(user["id"], hook_id)
        self.opener.behavior = 503
        before = int(time.time())
        notify._drain_deliveries(self.server)
        after = int(time.time())
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(before + 60 <= row["next_attempt_at"] <= after + 60)
        # 第 2..5 次：每次把到期时间拨回，退避 300/900/3600，第 5 次置 failed。
        for expected_attempt in (2, 3, 4, 5):
            with self.server.db() as conn:
                conn.execute("UPDATE webhook_deliveries SET next_attempt_at=0 WHERE id=?", (delivery_id,))
            notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 5)
        self.assertIn("503", row["last_error"])
        self.assertEqual(len(self.opener.requests), 5)

    def test_drain_429_retryable_and_4xx_permanent(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        self.opener.behavior = 429
        delivery_id = self.seed_delivery(user["id"], hook_id)
        notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT state FROM webhook_deliveries WHERE id=?",
                                          (delivery_id,)).fetchone()["state"], "pending")
        # 永久失败：404 一次即 failed。
        self.opener.behavior = 404
        with self.server.db() as conn:
            conn.execute("UPDATE webhook_deliveries SET next_attempt_at=0 WHERE id=?", (delivery_id,))
        notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["attempts"], 2)

    def test_drain_timeout_retryable_and_disabled_hook_fails(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        delivery_id = self.seed_delivery(user["id"], hook_id)
        self.opener.behavior = socket.timeout("timed out")
        notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 1)
        # 停用 webhook：存量 delivery 直接 failed，不再发起请求。
        with self.server.db() as conn:
            conn.execute("UPDATE user_webhooks SET enabled=0 WHERE id=?", (hook_id,))
            conn.execute("UPDATE webhook_deliveries SET next_attempt_at=0 WHERE id=?", (delivery_id,))
        notify._drain_deliveries(self.server)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertIn("停用", row["last_error"])

    def test_pump_runs_single_daemon_thread(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        self.seed_delivery(user["id"], body["webhook"]["id"])
        self.pump_patch.stop()
        try:
            notify.pump(self.server)
            first = notify._pump_thread
            self.assertTrue(first.daemon)
            notify.pump(self.server)
            self.assertIs(notify._pump_thread, first)
            first.join(timeout=5)
            self.assertFalse(first.is_alive())
        finally:
            self.pump_patch.start()
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT state FROM webhook_deliveries").fetchone()["state"], "sent")

    # ----- 路由契约：鉴权/隔离 --------------------------------------------- #
    def test_auth_required_for_every_route(self):
        hook_id = "f" * 32
        routes = [
            ("GET", "/api/notifications", None),
            ("POST", "/api/notifications/read", {"all": True}),
            ("GET", "/api/webhooks", None),
            ("POST", "/api/webhooks", {"name": "x", "url": SAFE_URL, "events": ["task.due"]}),
            ("PATCH", f"/api/webhooks/{hook_id}", {"name": "y"}),
            ("DELETE", f"/api/webhooks/{hook_id}", None),
            ("POST", f"/api/webhooks/{hook_id}/rotate-secret", {}),
            ("POST", f"/api/webhooks/{hook_id}/test", {}),
        ]
        for method, path, data in routes:
            with self.subTest(path=path):
                status, body = self.request(method, path, data)
                self.assertEqual(status, 401, (path, body))

    def test_account_isolation_returns_404_and_hides_rows(self):
        cookie_a, _, _ = self.register("a@example.com")
        cookie_b, _, _ = self.register("b@example.com")
        _, body = self.create_webhook(cookie_a)
        hook_id = body["webhook"]["id"]
        # B 的列表看不到 A 的 webhook。
        status, body_b = self.request("GET", "/api/webhooks", cookie=cookie_b)
        self.assertEqual(status, 200)
        self.assertEqual(body_b["webhooks"], [])
        # B 对 A 的资源任何操作都 404，不暴露存在性。
        self.assertEqual(self.request("PATCH", f"/api/webhooks/{hook_id}",
                                      {"name": "劫持"}, cookie=cookie_b)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/webhooks/{hook_id}",
                                      cookie=cookie_b)[0], 404)
        self.assertEqual(self.request("POST", f"/api/webhooks/{hook_id}/rotate-secret",
                                      {}, cookie=cookie_b)[0], 404)
        self.assertEqual(self.request("POST", f"/api/webhooks/{hook_id}/test",
                                      {}, cookie=cookie_b)[0], 404)
        # A 自己的通知 B 也看不到。
        self.create_task(cookie_a, self._space_id(cookie_a), due=self.day(-1))
        notify.run_maintenance(self.server, force=True)
        status, body_b = self.request("GET", "/api/notifications", cookie=cookie_b)
        self.assertEqual(status, 200)
        self.assertEqual(body_b["notifications"], [])
        self.assertEqual(body_b["unreadCount"], 0)

    def _space_id(self, cookie):
        _, spaces = self.request("GET", "/api/spaces", cookie=cookie)
        return spaces["spaces"][0]["id"]

    # ----- Webhook CRUD ---------------------------------------------------- #
    def test_create_list_hides_secret_and_rotate(self):
        cookie, _, _ = self.register()
        status, body = self.create_webhook(cookie)
        self.assertEqual(status, 201, body)
        hook = body["webhook"]
        self.assertIn("secret", hook)
        self.assertGreaterEqual(len(hook["secret"]), 40)
        self.assertTrue(hook["secretConfigured"])
        old_secret = hook["secret"]
        # 列表绝不返回 secret。
        status, body = self.request("GET", "/api/webhooks", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["webhooks"]), 1)
        listed = body["webhooks"][0]
        self.assertNotIn("secret", listed)
        self.assertTrue(listed["secretConfigured"])
        self.assertEqual(listed["events"], ["task.due"])
        # 轮转：新 secret 仅此一次返回，且与旧值不同。
        status, body = self.request("POST", f"/api/webhooks/{listed['id']}/rotate-secret",
                                    {}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertIn("secret", body["webhook"])
        self.assertNotEqual(body["webhook"]["secret"], old_secret)
        status, body = self.request("GET", "/api/webhooks", cookie=cookie)
        self.assertNotIn("secret", body["webhooks"][0])

    def test_create_validation_errors(self):
        cookie, _, _ = self.register()
        self.assertEqual(self.create_webhook(cookie, name="  ")[0], 400)
        self.assertEqual(self.create_webhook(cookie, url="http://insecure.example/hook")[0], 400)
        with mock.patch.object(notify, "_resolve",
                               side_effect=ip_getaddrinfo("10.1.2.3")):
            self.assertEqual(self.create_webhook(cookie, url="https://intranet.example/hook")[0], 400)
        self.assertEqual(self.create_webhook(cookie, events=["unknown.event"])[0], 400)
        self.assertEqual(self.create_webhook(cookie, events=[])[0], 400)
        self.assertEqual(self.create_webhook(cookie, enabled="yes")[0], 400)

    def test_patch_webhook_and_url_revalidation(self):
        cookie, _, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        status, body = self.request("PATCH", f"/api/webhooks/{hook_id}",
                                    {"name": "新名字", "enabled": False, "events": ["*"]},
                                    cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["webhook"]["name"], "新名字")
        self.assertFalse(body["webhook"]["enabled"])
        self.assertEqual(body["webhook"]["events"], ["*"])
        self.assertNotIn("secret", body["webhook"])
        # 改 url 必须重过 SSRF。
        with mock.patch.object(notify, "_resolve",
                               side_effect=ip_getaddrinfo("192.168.0.9")):
            status, body = self.request("PATCH", f"/api/webhooks/{hook_id}",
                                        {"url": "https://lan.example/hook"}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(self.request("PATCH", f"/api/webhooks/{'e' * 32}",
                                      {"name": "x"}, cookie=cookie)[0], 404)
        self.assertEqual(self.request("PATCH", f"/api/webhooks/{hook_id}",
                                      {}, cookie=cookie)[0], 400)

    def test_webhook_cap_returns_429(self):
        cookie, _, _ = self.register()
        for index in range(notify.MAX_WEBHOOKS):
            status, body = self.create_webhook(cookie, name=f"通道{index}")
            self.assertEqual(status, 201, body)
        status, body = self.create_webhook(cookie, name="超额通道")
        self.assertEqual(status, 429)
        self.assertIn("10", body["error"])

    def test_write_frequency_throttle_returns_429(self):
        cookie, _, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        # 创建已占用 1 次写额度，再打满剩余额度后下一次写必须 429。
        for index in range(notify.WEBHOOK_WRITE_LIMIT - 1):
            status, body = self.request("PATCH", f"/api/webhooks/{hook_id}",
                                        {"name": f"改名{index}"}, cookie=cookie)
            self.assertEqual(status, 200, body)
        status, body = self.request("PATCH", f"/api/webhooks/{hook_id}",
                                    {"name": "触发限流"}, cookie=cookie)
        self.assertEqual(status, 429)

    def test_test_endpoint_synchronous_result_and_throttle(self):
        cookie, _, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        self.opener.behavior = 204
        status, body = self.request("POST", f"/api/webhooks/{hook_id}/test", {}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "status": 204})
        self.assertEqual(self.opener.requests[-1][1], notify.TEST_TIMEOUT)
        self.opener.behavior = 500
        status, body = self.request("POST", f"/api/webhooks/{hook_id}/test", {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("500", body["error"])
        # 严格限流：5 次/小时（已用 2 次，再 3 次后第 6 次 429）。
        for _ in range(3):
            self.assertEqual(self.request("POST", f"/api/webhooks/{hook_id}/test",
                                           {}, cookie=cookie)[0], 200)
        status, body = self.request("POST", f"/api/webhooks/{hook_id}/test", {}, cookie=cookie)
        self.assertEqual(status, 429)

    def test_delete_webhook_cascades_deliveries(self):
        cookie, user, _ = self.register()
        _, body = self.create_webhook(cookie)
        hook_id = body["webhook"]["id"]
        first = self.seed_delivery(user["id"], hook_id)
        second = self.seed_delivery(user["id"], hook_id)
        self.assertEqual(self.table_count("webhook_deliveries"), 2)
        status, body = self.request("DELETE", f"/api/webhooks/{hook_id}", cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.table_count("webhook_deliveries"), 0)
        self.assertEqual(self.table_count("user_webhooks"), 0)
        self.assertTrue(first and second)

    # ----- 通知已读 -------------------------------------------------------- #
    def test_notifications_read_single_and_all(self):
        cookie, _, space = self.register()
        self.create_task(cookie, space["id"], due=self.day(-1))
        notify.run_maintenance(self.server, force=True)
        _, body = self.request("GET", "/api/notifications", cookie=cookie)
        note_id = body["notifications"][0]["id"]
        status, body = self.request("POST", "/api/notifications/read", {"id": note_id}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["updated"], 1)
        # 重复已读/他人通知统一 404。
        self.assertEqual(self.request("POST", "/api/notifications/read",
                                      {"id": note_id}, cookie=cookie)[0], 404)
        self.assertEqual(self.request("POST", "/api/notifications/read",
                                      {"id": "z" * 32}, cookie=cookie)[0], 400)
        # 再造一条未读，all:true 一次清掉。
        with self.server.db() as conn:
            conn.execute("INSERT INTO notifications "
                         "(id,user_id,space_id,type,title,body,data,dedup_key,read,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,0,?)",
                         (uuid.uuid4().hex, self._uid(cookie), space["id"], "task.due", "t", "b",
                          "{}", None, "2026-01-03T00:00:00Z"))
        status, body = self.request("POST", "/api/notifications/read", {"all": True}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["updated"], 1)
        _, body = self.request("GET", "/api/notifications", cookie=cookie)
        self.assertEqual(body["unreadCount"], 0)

    def test_notification_limit_validation(self):
        cookie, _, _ = self.register()
        self.assertEqual(self.request("GET", "/api/notifications?limit=51",
                                      cookie=cookie)[0], 400)
        self.assertEqual(self.request("GET", "/api/notifications?limit=abc",
                                      cookie=cookie)[0], 400)
        status, body = self.request("GET", "/api/notifications?limit=1", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["notifications"]), 1)


if __name__ == "__main__":
    unittest.main()
