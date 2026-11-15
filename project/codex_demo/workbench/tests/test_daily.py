"""Real HTTP isolation checks for the daily image log; fake vision never calls paid APIs."""
import http.client
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily
import storage_jobs
from server import FolioServer

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is a pinned runtime dependency
    Image = None


def png_bytes(color=(120, 170, 130), size=(96, 64)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def bomb_png_bytes(width=50000, height=50000):
    """A tiny valid PNG whose IHDR declares an oversized frame (decompression bomb)."""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag, data):
        raw = tag + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # One filtered scanline row is enough for Pillow to read the IHDR and fail
    # on the declared pixel count before any large pixel buffer is allocated.
    idat = zlib.compress(b"\x00" + b"\x00\x00\x00")
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class FakeVision:
    enabled = True
    model = "qwen3.8-flash"

    def __init__(self):
        self.recognize_calls = []
        self.digest_calls = []
        self.failure = None
        self.recognized = {
            "title": "周会同步项目进展",
            "summary": "今天参加了项目周会，同步了本周进展，确认了上线时间和负责人。",
            "tags": ["会议", "项目"],
            "suggestions": ["整理会议纪要", "周五前确认上线清单"],
        }

    def recognize(self, data_uris, note):
        self.recognize_calls.append((list(data_uris), note))
        if not all(uri.startswith("data:image/jpeg;base64,") for uri in data_uris):
            raise daily.ProviderFailure("测试桩收到了非识别副本")
        if self.failure:
            raise daily.ProviderFailure("视觉模型暂时不可用")
        return dict(self.recognized)

    def digest(self, day_text):
        self.digest_calls.append(day_text)
        if self.failure:
            raise daily.ProviderFailure("视觉模型暂时不可用")
        return "今天主要推进了项目周会和上线准备，待跟进会议纪要与上线清单。"


class DailyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Image is None:
            raise unittest.SkipTest("Pillow unavailable")
        url = os.environ.get("TEST_WORKBENCH_POSTGRES_URL", "")
        if url and (urlsplit(url).hostname not in ("localhost", "127.0.0.1", "::1") or not urlsplit(url).path.startswith("/zhixu_tests")):
            raise ValueError("HTTP PostgreSQL QA requires a local zhixu_tests database")
        cls.database_env = patch.dict(os.environ, {"DATABASE_URL": url})
        cls.database_env.start()
        cls.temp = tempfile.TemporaryDirectory()
        cls.server = FolioServer(("127.0.0.1", 0), data_dir=Path(cls.temp.name) / "data",
                                 registration_mode="open", storage_persistence="unknown" if url else "persistent")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()
        cls.database_env.stop()

    def setUp(self):
        with self.server.db() as conn:
            for table in ("daily_digests", "daily_images", "daily_uploads", "daily_entries",
                          "storage_jobs", "vaults", "files", "items", "members", "spaces",
                          "sessions", "users", "registration_invites"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        self.server.daily_provider = self.vision = FakeVision()
        self.server.max_space_storage = 1024 ** 3
        self.server.max_total_storage = 10 * 1024 ** 3
        self.owner, self.owner_user, self.space = self.register("owner@example.com")
        self.editor, _, _ = self.register("editor@example.com")
        self.viewer, _, _ = self.register("viewer@example.com")
        self.outsider, _, outsider_space = self.register("outsider@example.com")
        self.sid = self.space["id"]
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "editor@example.com", "role": "editor"}, self.owner)
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "viewer@example.com", "role": "viewer"}, self.owner)
        self.prefix = f"/api/spaces/{self.sid}/daily"

    def request(self, method, path, data=None, cookie=None, headers=None, raw=None, content_type=None):
        values = {"X-Requested-With": "Workspace", **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        if data is not None:
            raw = json.dumps(data).encode()
            values["Content-Type"] = "application/json"
        if content_type:
            values["Content-Type"] = content_type
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
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

    def upload_png(self, cookie=None, sid=None, content=None, name="shot.png", mime="image/png"):
        content = content if content is not None else png_bytes()
        boundary = "daily-test-boundary"
        raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               f"filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n").encode() + content + \
              f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", f"/api/spaces/{sid or self.sid}/daily/uploads", cookie=cookie or self.owner,
                            raw=raw, content_type=f"multipart/form-data; boundary={boundary}")

    def make_entry(self, cookie=None, images=1, body="", area="work"):
        image_ids = []
        for _ in range(images):
            status, payload, _ = self.upload_png(cookie=cookie)
            self.assertEqual(status, 201, payload)
            image_ids.append(payload["upload"]["id"])
        return self.request("POST", f"{self.prefix}/entries",
                            {"imageIds": image_ids, "body": body, "area": area}, cookie=cookie or self.owner)

    def test_config_reflects_vision_availability(self):
        self.vision.enabled = False
        status, body, _ = self.request("GET", f"{self.prefix}/config", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertFalse(body["vision"]["enabled"])
        self.assertEqual(body["vision"]["model"], "")
        self.assertEqual(body["maxImages"], 9)
        self.vision.enabled = True
        status, body, _ = self.request("GET", f"{self.prefix}/config", cookie=self.owner)
        self.assertTrue(body["vision"]["enabled"])
        self.assertEqual(body["vision"]["model"], "qwen3.8-flash")

    def test_config_requires_authentication(self):
        status, _, _ = self.request("GET", f"{self.prefix}/config")
        self.assertEqual(status, 401)

    def test_upload_validates_real_image_bytes_and_returns_dimensions(self):
        status, body, _ = self.upload_png(content=b"not an image", name="fake.png")
        self.assertEqual(status, 400)
        status, body, _ = self.upload_png()
        self.assertEqual(status, 201, body)
        self.assertEqual(body["upload"]["mime"], "image/png")
        self.assertEqual(body["upload"]["width"], 96)
        self.assertEqual(body["upload"]["height"], 64)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM daily_uploads WHERE id=?", (body["upload"]["id"],)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["created_by"], self.owner_user["id"])

    def test_upload_requires_editor_permission(self):
        status, _, _ = self.upload_png(cookie=self.viewer)
        self.assertEqual(status, 403)
        status, _, _ = self.upload_png(cookie=self.outsider, sid=self.sid)
        self.assertEqual(status, 404)

    def test_staging_is_scoped_to_owner_and_space(self):
        status, mine, _ = self.upload_png()
        self.assertEqual(status, 201)
        # Another editor in the same space cannot spend the owner's staging id.
        status, body, _ = self.request("POST", f"{self.prefix}/entries",
                                       {"imageIds": [mine["upload"]["id"]], "body": "x"}, cookie=self.editor)
        self.assertEqual(status, 400, body)
        # The id means nothing in another account's own space.
        second_cookie, _, second_space = self.register("second@example.com")
        status, body, _ = self.request("POST", f"/api/spaces/{second_space['id']}/daily/entries",
                                       {"imageIds": [mine["upload"]["id"]], "body": "x"}, cookie=second_cookie)
        self.assertEqual(status, 400, body)

    def test_create_entry_with_images_and_list_by_date(self):
        status, body, _ = self.make_entry(images=2, body="白板上的周会内容")
        self.assertEqual(status, 201, body)
        entry = body["entry"]
        self.assertEqual(len(entry["images"]), 2)
        self.assertEqual(entry["entryDate"], daily.today_str())
        self.assertEqual(entry["meta"]["ai"]["status"], "pending")
        self.assertEqual(entry["title"], "白板上的周会内容")
        status, listing, _ = self.request("GET", f"{self.prefix}/entries?date={daily.today_str()}", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["entries"]), 1)
        self.assertEqual(listing["entries"][0]["id"], entry["id"])
        status, month_listing, _ = self.request("GET", f"{self.prefix}/entries?month={daily.today_str()[:7]}", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(len(month_listing["entries"]), 1)

    def test_entry_accepts_text_only_and_rejects_empty_payload(self):
        status, body, _ = self.request("POST", f"{self.prefix}/entries",
                                       {"body": "今天只写一段文字", "area": "life"}, cookie=self.owner)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["entry"]["meta"]["ai"]["status"], "disabled")
        status, body, _ = self.request("POST", f"{self.prefix}/entries", {"body": "  "}, cookie=self.owner)
        self.assertEqual(status, 400, body)
        status, body, _ = self.request("POST", f"{self.prefix}/entries",
                                       {"imageIds": ["x" * 32], "body": ""}, cookie=self.owner)
        self.assertEqual(status, 400, body)

    def test_entry_cannot_exceed_nine_images(self):
        ids = []
        for _ in range(10):
            _, payload, _ = self.upload_png()
            ids.append(payload["upload"]["id"])
        status, body, _ = self.request("POST", f"{self.prefix}/entries",
                                       {"imageIds": ids, "body": ""}, cookie=self.owner)
        self.assertEqual(status, 400, body)

    def test_recognize_fills_meta_and_presentation_fields(self):
        _, created, _ = self.make_entry(images=2, body="帮我记住重点")
        entry_id = created["entry"]["id"]
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        self.assertEqual(status, 200, body)
        entry = body["entry"]
        self.assertEqual(entry["meta"]["ai"]["status"], "done")
        self.assertEqual(entry["meta"]["ai"]["model"], "qwen3.8-flash")
        self.assertEqual(entry["meta"]["ai"]["tags"], ["会议", "项目"])
        self.assertEqual(entry["meta"]["ai"]["suggestions"], ["整理会议纪要", "周五前确认上线清单"])
        self.assertEqual(entry["title"], "周会同步项目进展")
        self.assertIn("周会", entry["summary"])
        self.assertEqual(len(self.vision.recognize_calls), 1)
        self.assertEqual(self.vision.recognize_calls[0][1], "帮我记住重点")
        self.assertEqual(len(self.vision.recognize_calls[0][0]), 2)

    def test_recognize_respects_disabled_provider_and_failures(self):
        _, created, _ = self.make_entry(images=1)
        entry_id = created["entry"]["id"]
        self.vision.enabled = False
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        self.assertEqual(status, 503)
        self.assertIn("密钥", body["error"])
        self.vision.enabled = True
        self.vision.failure = True
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        self.assertEqual(status, 502, body)
        self.vision.failure = False
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entry"]["meta"]["ai"]["status"], "done")

    def test_manual_edits_are_not_overwritten_by_later_recognition(self):
        _, created, _ = self.make_entry(images=1)
        entry_id = created["entry"]["id"]
        status, body, _ = self.request("PATCH", f"{self.prefix}/entries/{entry_id}",
                                       {"title": "我的自定义标题", "summary": "我的摘要"}, cookie=self.owner)
        self.assertEqual(status, 200, body)
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["title"], "我的自定义标题")
        self.assertEqual(body["entry"]["summary"], "我的摘要")
        self.assertEqual(body["entry"]["meta"]["ai"]["status"], "done")
        # Explicit apply still adopts the model title and summary.
        status, body, _ = self.request("PATCH", f"{self.prefix}/entries/{entry_id}",
                                       {"applyAi": True}, cookie=self.owner)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entry"]["title"], "周会同步项目进展")
        self.assertTrue(body["entry"]["meta"]["ai"]["applied"])

    def test_suggestion_can_become_a_real_task(self):
        _, created, _ = self.make_entry(images=1)
        entry_id = created["entry"]["id"]
        self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        suggestion = self.vision.recognized["suggestions"][0]
        status, task, _ = self.request("POST", f"/api/spaces/{self.sid}/items",
                                       {"kind": "task", "area": "work", "title": suggestion,
                                        "body": "", "status": "todo",
                                        "meta": {"source": "daily-suggestion", "entryId": entry_id}}, cookie=self.owner)
        self.assertEqual(status, 201, task)
        self.assertEqual(task["item"]["title"], suggestion)
        self.assertEqual(task["item"]["meta"]["entryId"], entry_id)

    def test_digest_generation_and_regeneration(self):
        self.make_entry(images=1)
        entry_id = self.request("GET", f"{self.prefix}/entries?date={daily.today_str()}", cookie=self.owner)[1]["entries"][0]["id"]
        self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
        status, body, _ = self.request("POST", f"{self.prefix}/digest", {}, cookie=self.owner)
        self.assertEqual(status, 200, body)
        self.assertIn("今天", body["digest"]["digest"])
        status, fetched, _ = self.request("GET", f"{self.prefix}/digest?date={daily.today_str()}", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(fetched["digest"]["digest"], body["digest"]["digest"])
        self.assertEqual(len(self.vision.digest_calls), 1)
        # Regeneration replaces the stored digest in place.
        status, _, _ = self.request("POST", f"{self.prefix}/digest", {}, cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.vision.digest_calls), 2)

    def test_digest_requires_entries_and_provider(self):
        status, body, _ = self.request("POST", f"{self.prefix}/digest", {}, cookie=self.owner)
        self.assertEqual(status, 400, body)
        self.request("POST", f"{self.prefix}/entries", {"body": "一条记录"}, cookie=self.owner)
        self.vision.enabled = False
        status, body, _ = self.request("POST", f"{self.prefix}/digest", {}, cookie=self.owner)
        self.assertEqual(status, 503, body)

    def test_entry_patch_delete_and_image_serving_permissions(self):
        _, created, _ = self.make_entry(images=1, area="life")
        entry = created["entry"]
        image = entry["images"][0]
        # Viewer can read entries and images but not change them.
        status, _, _ = self.request("GET", f"{self.prefix}/entries/{entry['id']}", cookie=self.viewer)
        self.assertEqual(status, 200)
        status, image_body, headers = self.request("GET", f"{self.prefix}/images/{image['id']}", cookie=self.viewer)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(image_body, png_bytes())
        status, _, _ = self.request("PATCH", f"{self.prefix}/entries/{entry['id']}",
                                    {"title": "x"}, cookie=self.viewer)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", f"/api/spaces/{self.sid}/daily/images/{image['id']}", cookie=self.outsider)
        self.assertEqual(status, 404)
        # Editor can update; owner can delete, which queues the private object removal.
        status, body, _ = self.request("PATCH", f"{self.prefix}/entries/{entry['id']}",
                                       {"title": "编辑后的标题", "area": "work"}, cookie=self.editor)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entry"]["title"], "编辑后的标题")
        status, body, _ = self.request("DELETE", f"{self.prefix}/entries/{entry['id']}", cookie=self.owner)
        self.assertEqual(status, 200, body)
        with self.server.db() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM daily_entries WHERE id=?", (entry["id"],)).fetchone())
        # Local housekeeping runs inline, so the private object is removed and
        # its completed deletion job does not linger.
        from media_storage import MissingMedia
        try:
            self.server.media_storage.stat("uploads", image["id"])
            object_gone = False
        except MissingMedia:
            object_gone = True
        self.assertTrue(object_gone)

    def test_quota_usage_includes_daily_images(self):
        self.make_entry(images=2)
        with self.server.db() as conn:
            space_bytes, total_bytes = __import__("storage_jobs").usage(conn, self.sid)
        self.assertGreater(space_bytes, 0)
        self.assertEqual(space_bytes, total_bytes)

    def test_staging_cleanup_queues_objects_after_ttl(self):
        _, uploaded, _ = self.upload_png()
        stale_id = uploaded["upload"]["id"]
        with self.server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE daily_uploads SET created_at=? WHERE id=?",
                         (int(time.time()) - daily.STAGING_TTL - 10, stale_id))
            conn.commit()
        daily.cleanup_staging(self.server)
        with self.server.db() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM daily_uploads WHERE id=?", (stale_id,)).fetchone())
        # Local housekeeping runs inline: the private object must be gone and
        # no pending reservation may survive a completed cleanup.
        from media_storage import MissingMedia
        try:
            self.server.media_storage.stat("uploads", stale_id)
            object_gone = False
        except MissingMedia:
            object_gone = True
        self.assertTrue(object_gone)
        with self.server.db() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM storage_jobs WHERE id=?", (stale_id,)).fetchone())

    def test_recognize_is_rate_limited_per_account(self):
        _, created, _ = self.make_entry(images=1)
        entry_id = created["entry"]["id"]
        statuses = []
        for _ in range(31):
            status, _, _ = self.request("POST", f"{self.prefix}/entries/{entry_id}/recognize", {}, cookie=self.owner)
            statuses.append(status)
        self.assertIn(429, statuses)
        self.assertEqual(statuses[-1], 429)

    def test_recognition_validation_rejects_malformed_model_output(self):
        with self.assertRaises(daily.ProviderFailure):
            daily.validate_recognition({"title": "", "summary": "有摘要无标题"})
        with self.assertRaises(daily.ProviderFailure):
            daily.validate_recognition({"title": "标题", "summary": ""})
        clean = daily.validate_recognition({
            "title": "正常标题", "summary": "这是一段足够长的正常摘要内容，用于通过校验。",
            "tags": ["会议", "会议", "项目", "多余标签", "又一个"],
            "suggestions": ["第一条行动项", "x" * 100, 12345],
        })
        self.assertEqual(clean["tags"], ["会议", "项目"])
        self.assertEqual(len(clean["suggestions"]), 2)
        self.assertLessEqual(len(clean["suggestions"][1]), 30)

    def test_error_responses_never_contain_provider_details(self):
        self.vision.failure = True
        _, created, _ = self.make_entry(images=1)
        status, body, _ = self.request("POST", f"{self.prefix}/entries/{created['entry']['id']}/recognize",
                                       {}, cookie=self.owner)
        self.assertEqual(status, 502)
        rendered = json.dumps(body, ensure_ascii=False)
        for secret in ("dashscope", "aliyuncs", "Bearer", "http://", "https://"):
            self.assertNotIn(secret, rendered)

    def test_staging_upload_rejected_when_quota_full_without_object_or_rows(self):
        existing_objects = set(self.server.upload_dir.iterdir())
        self.server.max_space_storage = 5
        status, body, _ = self.upload_png()
        self.assertEqual(status, 413, body)
        self.server.max_space_storage = 1024
        self.server.max_total_storage = 5
        status, body, _ = self.upload_png()
        self.assertEqual(status, 507, body)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_uploads").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
        self.assertEqual(set(self.server.upload_dir.iterdir()), existing_objects)

    def test_staged_and_published_uploads_count_once_in_usage(self):
        status, uploaded, _ = self.upload_png()
        self.assertEqual(status, 201, uploaded)
        size = uploaded["upload"]["size"]
        with self.server.db() as conn:
            # The reservation is deleted in the same transaction that inserts
            # the staging row: a committed state never holds both rows.
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_uploads").fetchone()[0], 1)
            space_bytes, total_bytes = storage_jobs.usage(conn, self.sid)
        self.assertEqual(space_bytes, size)
        self.assertEqual(total_bytes, size)
        status, created, _ = self.request(
            "POST", f"{self.prefix}/entries",
            {"imageIds": [uploaded["upload"]["id"]], "body": "x"}, cookie=self.owner)
        self.assertEqual(status, 201, created)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_uploads").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_images").fetchone()[0], 1)
            space_bytes, total_bytes = storage_jobs.usage(conn, self.sid)
        # The entry swap replaces the staging row with the image row: still once.
        self.assertEqual(space_bytes, size)
        self.assertEqual(total_bytes, size)

    def test_upload_throttle_returns_429_after_sixty_per_hour(self):
        statuses = [self.upload_png()[0] for _ in range(61)]
        self.assertEqual(statuses.count(201), 60)
        self.assertEqual(statuses[-1], 429)
        # The per-account bucket must not spill over to another account.
        other_cookie, _, other_space = self.register("other@example.com")
        status, body, _ = self.upload_png(cookie=other_cookie, sid=other_space["id"])
        self.assertEqual(status, 201, body)

    def test_decompression_bomb_png_rejected_with_400(self):
        existing_objects = set(self.server.upload_dir.iterdir())
        status, body, _ = self.upload_png(content=bomb_png_bytes(), name="bomb.png")
        self.assertEqual(status, 400, body)
        self.assertIn("像素", body["error"])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_uploads").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
        self.assertEqual(set(self.server.upload_dir.iterdir()), existing_objects)
        # A normal small image still passes the same inspection path.
        status, body, _ = self.upload_png()
        self.assertEqual(status, 201, body)

    def test_inspect_image_blocks_warning_level_pixel_bombs(self):
        # 9000x9000 = 81M pixels, over the 64M cap but under Pillow's hard
        # 2x limit: it only warns, and inspection must still reject it.
        with self.assertRaises(ValueError):
            daily.inspect_image(bomb_png_bytes(9000, 9000))
        with self.assertRaises(ValueError):
            daily.inspect_image(bomb_png_bytes(50000, 50000))
        self.assertEqual(daily.inspect_image(png_bytes()), (96, 64))


if __name__ == "__main__":
    unittest.main()
