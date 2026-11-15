"""templates.py 的路由契约、鉴权隔离、容量限制与外键级联测试。

全部走真实 HTTP 请求与一次性 SQLite 数据库，无外网、无第三方依赖，
夹具风格与 tests/test_notify.py 保持一致。
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import templates
from server import FolioServer, FolioHandler

VALID_SUBTASK_ID = "0123456789abcdef0123456789abcdef"


class TemplateServerTests(unittest.TestCase):
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
            for table in ("item_templates", "webhook_deliveries", "notifications",
                          "user_webhooks", "storage_jobs", "vaults", "files", "items",
                          "members", "spaces", "sessions", "users", "registration_invites"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()

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
        raw = json.dumps({"email": email, "name": "阿舟", "password": "test-password-123"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=8)
        conn.request("POST", "/api/auth/register", body=raw,
                     headers={"X-Requested-With": "Workspace", "Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        result_headers = dict(response.getheaders())
        conn.close()
        self.assertEqual(response.status, 201, body)
        return result_headers["Set-Cookie"].split(";")[0], body["user"]

    def table_count(self, table, where="", params=()):
        with self.server.db() as conn:
            return conn.execute(
                "SELECT count(*) FROM " + table + (" WHERE " + where if where else ""), params
            ).fetchone()[0]

    def create_template(self, cookie, **overrides):
        payload = {
            "name": "我的模板",
            "kind": "task",
            "area": "work",
            "title": "示例标题",
            "body": "示例正文",
            "meta": {},
        }
        payload.update(overrides)
        return self.request("POST", "/api/templates", payload, cookie=cookie)

    # ----- 内置常量自检 ---------------------------------------------------- #
    def test_builtin_templates_are_valid_and_complete(self):
        self.assertEqual(len(templates.BUILTIN_TEMPLATES), 10)
        slugs = [item["id"] for item in templates.BUILTIN_TEMPLATES]
        self.assertEqual(slugs, [
            "builtin-daily-standup", "builtin-weekly-report", "builtin-meeting-minutes",
            "builtin-project-retro", "builtin-reading-list", "builtin-shopping-list",
            "builtin-fitness-plan", "builtin-bedtime-review", "builtin-travel-prep",
            "builtin-idea-inbox",
        ])
        self.assertEqual(len(set(slugs)), 10)
        areas = {item["area"] for item in templates.BUILTIN_TEMPLATES}
        self.assertEqual(areas, {"work", "life"})
        for item in templates.BUILTIN_TEMPLATES:
            self.assertTrue(item["id"].startswith("builtin-"))
            self.assertTrue(item["builtin"])
            self.assertIn(item["kind"], ("task", "note", "media"))
            # 内置 meta 必须能通过与正式接口同一个静态校验入口。
            FolioHandler.validate_meta_extensions(item["meta"])
        # 内置模板只活在常量里，数据库中永远没有 builtin- 行。
        self.assertEqual(self.table_count("item_templates"), 0)

    # ----- 鉴权 ------------------------------------------------------------ #
    def test_anonymous_requests_rejected_with_401(self):
        self.assertEqual(self.request("GET", "/api/templates")[0], 401)
        self.assertEqual(self.request("POST", "/api/templates", {"name": "x"})[0], 401)
        self.assertEqual(self.request("PATCH", "/api/templates/" + "f" * 32)[0], 401)
        self.assertEqual(self.request("DELETE", "/api/templates/" + "f" * 32)[0], 401)

    # ----- GET 列表契约 ---------------------------------------------------- #
    def test_get_returns_builtins_first_then_own_custom(self):
        cookie, _ = self.register()
        status, body = self.request("GET", "/api/templates", cookie=cookie)
        self.assertEqual(status, 200, body)
        names = [item["id"] for item in body["templates"]]
        builtin_ids = [item["id"] for item in templates.BUILTIN_TEMPLATES]
        self.assertEqual(names[:len(builtin_ids)], builtin_ids)
        self.assertTrue(all(item["builtin"] for item in body["templates"][:10]))

        status, created = self.create_template(cookie, name="自定义一", title="T1")
        self.assertEqual(status, 201, created)
        custom_id = created["template"]["id"]
        status, created2 = self.create_template(cookie, name="自定义二", title="T2")
        self.assertEqual(status, 201, created2)
        # 自定义模板按 updated_at DESC：把第一条改成更旧的时间戳。
        with self.server.db() as conn:
            conn.execute("UPDATE item_templates SET updated_at=? WHERE id=?",
                         ("2000-01-01T00:00:00Z", custom_id))
        status, body = self.request("GET", "/api/templates", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["templates"]), 12)
        self.assertEqual([item["id"] for item in body["templates"][:10]], builtin_ids)
        custom = body["templates"][10:]
        self.assertEqual([item["id"] for item in custom], [created2["template"]["id"], custom_id])
        first = custom[0]
        self.assertEqual(set(first), {
            "id", "name", "kind", "area", "title", "body", "meta",
            "createdAt", "updatedAt", "builtin",
        })
        self.assertFalse(first["builtin"])
        self.assertEqual(first["meta"], {})

    def test_builtin_templates_shared_but_custom_rows_are_private(self):
        cookie_a, _ = self.register("a@example.com")
        cookie_b, _ = self.register("b@example.com")
        status, created = self.create_template(cookie_a, name="A 的私房模板")
        self.assertEqual(status, 201, created)
        hidden_id = created["template"]["id"]

        status, body_a = self.request("GET", "/api/templates", cookie=cookie_a)
        self.assertEqual(status, 200)
        self.assertIn(hidden_id, [item["id"] for item in body_a["templates"]])
        status, body_b = self.request("GET", "/api/templates", cookie=cookie_b)
        self.assertEqual(status, 200)
        # B 能看到同样的内置预设，但看不到 A 的自定义行。
        self.assertEqual(
            [item["id"] for item in body_b["templates"]],
            [item["id"] for item in templates.BUILTIN_TEMPLATES],
        )
        self.assertNotIn(hidden_id, [item["id"] for item in body_b["templates"]])

    # ----- POST 校验 ------------------------------------------------------- #
    def test_create_template_with_rich_meta_succeeds(self):
        cookie, _ = self.register()
        meta = {
            "tags": ["工作", "重点"],
            "subtasks": [
                {"id": VALID_SUBTASK_ID, "title": "子任务一", "done": False},
                {"id": "f" * 32, "title": "子任务二", "done": True},
            ],
            "recurrence": {"freq": "weekly", "byday": ["MO", "WE", "FR"]},
        }
        status, body = self.create_template(
            cookie, name="带扩展 meta 的模板", kind="note", area="life", meta=meta
        )
        self.assertEqual(status, 201, body)
        template = body["template"]
        self.assertFalse(template["builtin"])
        self.assertEqual(template["meta"], meta)
        self.assertEqual(template["createdAt"], template["updatedAt"])
        # 落库文本就是 JSON 序列化后的 meta。
        with self.server.db() as conn:
            row = conn.execute("SELECT meta,user_id FROM item_templates WHERE id=?",
                               (template["id"],)).fetchone()
        self.assertEqual(json.loads(row["meta"]), meta)

    def test_create_accepts_media_kind_and_defaults(self):
        cookie, _ = self.register()
        status, body = self.request("POST", "/api/templates",
                                    {"name": "媒体模板", "kind": "media", "area": "life"},
                                    cookie=cookie)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["template"]["title"], "")
        self.assertEqual(body["template"]["body"], "")
        self.assertEqual(body["template"]["meta"], {})

    def test_create_validation_errors(self):
        cookie, _ = self.register()
        # 非法 kind / area。
        self.assertEqual(self.create_template(cookie, kind="bug")[0], 400)
        self.assertEqual(self.create_template(cookie, area="home")[0], 400)
        # name 缺失、空白、超长（>60）。
        self.assertEqual(self.create_template(cookie, name="")[0], 400)
        self.assertEqual(self.create_template(cookie, name="   ")[0], 400)
        self.assertEqual(self.create_template(cookie, name="甲" * 61)[0], 400)
        # title 超 200、body 超 20000。
        self.assertEqual(self.create_template(cookie, title="x" * 201)[0], 400)
        self.assertEqual(self.create_template(cookie, body="x" * 20001)[0], 400)
        # meta 非对象。
        self.assertEqual(self.create_template(cookie, meta=[])[0], 400)
        self.assertEqual(self.create_template(cookie, meta="nope")[0], 400)
        # 非法 subtask id（不是 32 位小写十六进制）。
        bad_meta = {"subtasks": [{"id": "not-hex", "title": "坏子任务", "done": False}]}
        self.assertEqual(self.create_template(cookie, meta=bad_meta)[0], 400)
        # 非法 recurrence。
        self.assertEqual(self.create_template(cookie, meta={"recurrence": {"freq": "hourly"}})[0], 400)
        # 非法 tags。
        self.assertEqual(self.create_template(cookie, meta={"tags": ["x" * 21]})[0], 400)
        # 超 16KB meta。
        self.assertEqual(self.create_template(cookie, meta={"pad": "x" * 16384})[0], 400)
        # 全部失败，没有产生任何行。
        self.assertEqual(self.table_count("item_templates"), 0)

    def test_per_user_capacity_limit_is_50(self):
        cookie, _ = self.register()
        for index in range(50):
            status, body = self.create_template(cookie, name=f"模板{index:02d}")
            self.assertEqual(status, 201, body)
        self.assertEqual(self.table_count("item_templates"), 50)
        status, body = self.create_template(cookie, name="第五十一个")
        self.assertEqual(status, 400)
        self.assertIn("50", body["error"])
        self.assertEqual(self.table_count("item_templates"), 50)

    # ----- PATCH / DELETE -------------------------------------------------- #
    def test_patch_own_template_merges_fields_and_refreshes_timestamp(self):
        cookie, _ = self.register()
        status, created = self.create_template(cookie, name="原名", title="原标题", body="原正文")
        self.assertEqual(status, 201, created)
        template_id = created["template"]["id"]
        with self.server.db() as conn:
            conn.execute("UPDATE item_templates SET updated_at=? WHERE id=?",
                         ("2000-01-01T00:00:00Z", template_id))

        status, body = self.request("PATCH", f"/api/templates/{template_id}",
                                    {"name": "新名字", "meta": {"tags": ["更新"]}}, cookie=cookie)
        self.assertEqual(status, 200, body)
        template = body["template"]
        self.assertEqual(template["name"], "新名字")
        # 未提供的字段保持原值。
        self.assertEqual(template["title"], "原标题")
        self.assertEqual(template["body"], "原正文")
        self.assertEqual(template["meta"], {"tags": ["更新"]})
        self.assertNotEqual(template["updatedAt"], "2000-01-01T00:00:00Z")

        # 空 PATCH 与非法字段同样 400。
        self.assertEqual(self.request("PATCH", f"/api/templates/{template_id}", {},
                                      cookie=cookie)[0], 400)
        self.assertEqual(self.request("PATCH", f"/api/templates/{template_id}",
                                      {"kind": "bug"}, cookie=cookie)[0], 400)

    def test_delete_own_template(self):
        cookie, _ = self.register()
        status, created = self.create_template(cookie)
        self.assertEqual(status, 201, created)
        template_id = created["template"]["id"]
        status, body = self.request("DELETE", f"/api/templates/{template_id}", cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(self.table_count("item_templates"), 0)
        # 再删一次统一 404。
        self.assertEqual(self.request("DELETE", f"/api/templates/{template_id}",
                                      cookie=cookie)[0], 404)

    def test_builtin_and_other_user_resources_return_404(self):
        cookie_a, _ = self.register("a@example.com")
        cookie_b, _ = self.register("b@example.com")
        status, created = self.create_template(cookie_a, name="A 的模板")
        self.assertEqual(status, 201, created)
        owned_id = created["template"]["id"]
        builtin_id = templates.BUILTIN_TEMPLATES[0]["id"]

        # 内置模板只读：PATCH/DELETE 一律 404。
        self.assertEqual(self.request("PATCH", f"/api/templates/{builtin_id}",
                                      {"name": "篡改"}, cookie=cookie_a)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/templates/{builtin_id}",
                                      cookie=cookie_a)[0], 404)
        # B 对 A 的资源做 PATCH/DELETE 统一 404，不暴露存在性。
        self.assertEqual(self.request("PATCH", f"/api/templates/{owned_id}",
                                      {"name": "B 的篡改"}, cookie=cookie_b)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/templates/{owned_id}",
                                      cookie=cookie_b)[0], 404)
        # 不存在的 id 同样 404。
        self.assertEqual(self.request("DELETE", f"/api/templates/{'9' * 32}",
                                      cookie=cookie_a)[0], 404)
        # A 的行原样保留。
        self.assertEqual(self.table_count("item_templates"), 1)

    # ----- 405 / 404 路由契约 ---------------------------------------------- #
    def test_method_mismatch_returns_405_and_bad_subpath_404(self):
        cookie, _ = self.register()
        self.assertEqual(self.request("PUT", "/api/templates", cookie=cookie)[0], 405)
        self.assertEqual(self.request("PATCH", "/api/templates", {}, cookie=cookie)[0], 405)
        self.assertEqual(self.request("DELETE", "/api/templates", cookie=cookie)[0], 405)
        template_id = "a" * 32
        self.assertEqual(self.request("POST", f"/api/templates/{template_id}",
                                      {"name": "x"}, cookie=cookie)[0], 405)
        self.assertEqual(self.request("GET", f"/api/templates/{template_id}/extra",
                                      cookie=cookie)[0], 404)

    # ----- 脏数据兜底 ------------------------------------------------------ #
    def test_dirty_meta_text_falls_back_to_empty_object(self):
        cookie, user = self.register()
        dirty_id = uuid.uuid4().hex
        stamp = "2026-01-01T00:00:00Z"
        with self.server.db() as conn:
            conn.execute(
                "INSERT INTO item_templates (id,user_id,name,kind,area,title,body,meta,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (dirty_id, user["id"], "脏数据", "note", "work", "", "", "{not-json", stamp, stamp),
            )
        status, body = self.request("GET", "/api/templates", cookie=cookie)
        self.assertEqual(status, 200)
        custom = [item for item in body["templates"] if item["id"] == dirty_id]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["meta"], {})

    # ----- 账号注销级联 ---------------------------------------------------- #
    def test_close_account_cascades_item_templates(self):
        cookie, user = self.register()
        status, _ = self.create_template(cookie, name="随账号一起删除")
        self.assertEqual(status, 201)
        self.assertEqual(self.table_count("item_templates"), 1)
        status, body = self.request("DELETE", "/api/me/account",
                                    {"password": "test-password-123"}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.table_count("item_templates"), 0)
        self.assertEqual(self.table_count("users"), 0)


if __name__ == "__main__":
    unittest.main()
