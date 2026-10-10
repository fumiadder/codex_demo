"""Real HTTP isolation checks; fake providers never call paid external APIs."""
import base64
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, urlsplit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bedtime
from server import FolioServer


def wav(seconds=20, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(b"\x20\x00" * int(seconds * rate))
    return buffer.getvalue()


class FakeProviders:
    voice_enabled = False
    generation_enabled = False
    search_enabled = False
    search_provider = "local"

    def __init__(self):
        self.enroll_calls = []
        self.synth_calls = []
        self.deleted = []
        self.on_enroll = None
        self.on_synthesize = None
        self.on_generate = None
        self.generate_calls = []
        self.generated_output = None
        self.failure = False

    def enroll(self, payload, preferred_name):
        self.enroll_calls.append((payload, preferred_name))
        if self.failure:
            raise bedtime.ProviderFailure("供应商暂时不可用")
        if self.on_enroll:
            self.on_enroll()
        return "provider-private-voice-" + str(len(self.enroll_calls)), bedtime.VC_MODEL

    def synthesize(self, text, voice, model):
        self.synth_calls.append((text, voice, model))
        if self.on_synthesize:
            self.on_synthesize()
        return wav(0.1)

    def delete_voice(self, voice):
        self.deleted.append(voice)

    def generate(self, parameters):
        # Explicit test fixture, never a real AI or external network request.
        self.generate_calls.append(dict(parameters))
        if self.failure:
            raise bedtime.ProviderFailure("供应商暂时不可用")
        if self.on_generate:
            self.on_generate()
        if self.generated_output is not None:
            return self.generated_output
        short = ["小兔子在窗边看着柔和的月光。它把今天捡来的小叶子放在床头，摸了摸柔软的被角，听见妈妈在隔壁轻轻整理书本。",
                 "它想起朋友留给自己的座位，觉得心里暖暖的。窗外的树叶慢慢摇动，小兔子合上书，安心地靠在枕头上，让今天的小事轻轻留在明天。"]
        paragraphs = short if parameters["durationMinutes"] == 1 else [
            "小兔子和妈妈慢慢坐在窗边，听远处小溪轻轻流动。" + "它把一片小叶子夹进书里，想起朋友温柔的笑容，觉得今晚的月色很柔和。" * 5
            for _ in range(6)]
        questions = [{"afterParagraph": 0, "question": "也许可以想一想，你喜欢哪种柔和的云朵颜色？不用回答。"}] if parameters["interactive"] else []
        return {"title": "QA 测试故事，不是实际 AI 生成", "paragraphs": paragraphs, "questions": questions}


class BedtimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
            # The existing space owner FK intentionally does not cascade.
            # Remove children in the same order as the main HTTP fixtures.
            for table in ("storage_jobs", "vaults", "files", "items", "members", "spaces", "sessions", "users"):
                conn.execute("DELETE FROM " + table)
        self.server.rate_events.clear()
        self.server.bedtime_provider = self.provider = FakeProviders()
        self.server.max_space_storage = 1024 ** 3
        self.server.max_total_storage = 10 * 1024 ** 3
        for path in self.server.bedtime_audio_dir.iterdir():
            path.unlink()
        self.owner, self.owner_user, self.space = self.register("owner@example.com")
        self.editor, self.editor_user, _ = self.register("editor@example.com")
        self.viewer, self.viewer_user, _ = self.register("viewer@example.com")
        self.sid = self.space["id"]
        self.prefix = "/api/spaces/" + self.sid + "/bedtime/"
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "editor@example.com", "role": "editor"}, self.owner)
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "viewer@example.com", "role": "viewer"}, self.owner)

    def request(self, method, path, data=None, cookie=None, headers=None):
        values = {"X-Requested-With": "Workspace", **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        raw = None
        if data is not None:
            values["Content-Type"] = "application/json"
            raw = json.dumps(data).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        payload, response_headers, status = response.read(), dict(response.getheaders()), response.status
        connection.close()
        if response_headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return status, payload, response_headers

    def register(self, email):
        status, result, headers = self.request("POST", "/api/auth/register", {"email": email, "name": "测试用户", "password": "bedtime-password-123"})
        self.assertEqual(status, 201, result)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, spaces, _ = self.request("GET", "/api/spaces", cookie=cookie)
        self.assertEqual(status, 200)
        return cookie, result["user"], spaces["spaces"][0]

    def clone_body(self, seconds=20, **fields):
        return {"name": "我的晚安音色", "mimeType": "audio/wav", "audioBase64": base64.b64encode(wav(seconds)).decode(), "consent": True, **fields}

    def clone(self, cookie=None):
        self.provider.voice_enabled = True
        status, body, _ = self.request("POST", self.prefix + "voices", self.clone_body(), cookie or self.owner)
        self.assertEqual(status, 201, body)
        return body["voice"]

    def test_authenticated_catalog_contains_real_text_independent_of_voices(self):
        self.assertEqual(self.request("GET", self.prefix + "search")[0], 401)
        status, before, _ = self.request("GET", self.prefix + "search?q=" + quote("月亮"), cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertTrue(before["stories"])
        story = before["stories"][0]
        self.assertGreater(len(story["text"]), 200)
        self.assertEqual(story["source"]["kind"], "original")
        self.assertFalse(story["isExcerpt"])
        self.clone()
        after = self.request("GET", self.prefix + "search?q=" + quote("月亮"), cookie=self.owner)[1]
        self.assertEqual(before, after)
        self.assertEqual(self.request("GET", self.prefix + "stories/" + story["id"], cookie=self.viewer)[1]["story"], story)

    def test_shared_space_favorites_are_private_and_viewer_writes_denied(self):
        route = self.prefix + "favorites/moon-post-office"
        self.assertEqual(self.request("PUT", route, {}, self.owner)[0], 200)
        self.assertEqual(len(self.request("GET", self.prefix + "favorites", cookie=self.owner)[1]["favorites"]), 1)
        self.assertEqual(self.request("GET", self.prefix + "favorites", cookie=self.editor)[1]["favorites"], [])
        self.assertEqual(self.request("PUT", route, {}, self.viewer)[0], 403)
        self.assertEqual(self.request("DELETE", route, cookie=self.editor)[0], 200)
        self.assertEqual(len(self.request("GET", self.prefix + "favorites", cookie=self.owner)[1]["favorites"]), 1)
        status, favorite, _ = self.request("PUT", self.prefix + "favorites/imported-1", {"story": {
            "title": "自己的小故事", "text": "这是用户主动保存的故事文本。", "source": {"kind": "original", "url": "https://example.com/story"},
        }}, self.editor)
        self.assertEqual(status, 200)
        self.assertEqual(favorite["story"]["source"]["kind"], "imported")
        self.assertEqual(self.request("GET", self.prefix + "stories/imported-1", cookie=self.owner)[0], 404)

    def test_history_private_snapshot_and_validation(self):
        payload = {"storyId": "moon-post-office", "voiceId": "system:local", "positionSeconds": 12.5}
        self.assertEqual(self.request("POST", self.prefix + "history", payload, self.owner)[0], 200)
        self.assertEqual(self.request("POST", self.prefix + "history", payload, self.viewer)[0], 403)
        self.assertEqual(self.request("GET", self.prefix + "history", cookie=self.editor)[1]["history"], [])
        self.assertEqual(self.request("POST", self.prefix + "history", {**payload, "positionSeconds": True}, self.owner)[0], 400)
        history = self.request("GET", self.prefix + "history", cookie=self.owner)[1]["history"]
        self.assertEqual(history[0]["positionSeconds"], 12.5)
        self.assertIn("text", history[0]["story"])
        self.assertEqual(self.request("DELETE", self.prefix + "history", cookie=self.owner)[0], 200)
        self.assertEqual(self.request("GET", self.prefix + "history", cookie=self.owner)[1]["history"], [])

    def test_config_no_secrets_disabled_cloud_no_provider_calls(self):
        status, config, headers = self.request("GET", self.prefix + "config", cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertFalse(config["webSearch"]["enabled"])
        self.assertFalse(config["voiceClone"]["enabled"])
        self.assertFalse(config["storyGeneration"]["enabled"])
        self.assertEqual(config["storyGeneration"]["model"], bedtime.GENERATION_MODEL)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("key", json.dumps(config).lower())
        self.assertEqual(self.request("GET", self.prefix + "search?q=" + quote("童话") + "&scope=web", cookie=self.owner)[0], 503)
        self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.owner)[0], 503)
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": "cloud:Seren"}, self.owner)[0], 503)
        self.assertEqual(self.provider.enroll_calls, [])
        self.assertEqual(self.provider.synth_calls, [])
        self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.owner)[0], 503)
        self.assertEqual(self.provider.generate_calls, [])

    def test_personalized_generation_no_automatic_storage_and_private_explicit_snapshot(self):
        self.provider.generation_enabled = True
        parameters = {"ageGroup": "7-10", "category": "成长勇气", "durationMinutes": 1,
                      "style": "healing", "moral": True, "blockScary": True,
                      "protagonist": "rabbit", "interactive": True}
        status, payload, headers = self.request("POST", self.prefix + "generate", parameters, self.owner)
        self.assertEqual(status, 200, payload)
        self.assertEqual(headers["Cache-Control"], "no-store")
        story = payload["story"]
        self.assertEqual(story["source"]["kind"], "generated")
        self.assertEqual(story["generationParameters"], parameters)
        self.assertEqual(story["readMinutes"], 1)
        self.assertEqual(story["ending"], bedtime.SLEEP_ENDING)
        self.assertEqual(story["text"], "\n\n".join([*story["paragraphs"], story["ending"]]))
        self.assertEqual(story["questions"][0]["afterParagraph"], 0)
        self.assertNotIn(story["id"], self.server.bedtime_stories)
        self.assertEqual(self.request("GET", self.prefix + "stories/" + story["id"], cookie=self.owner)[0], 404)
        self.assertEqual(self.request("GET", self.prefix + "favorites", cookie=self.owner)[1]["favorites"], [])
        self.assertEqual(self.request("GET", self.prefix + "history", cookie=self.owner)[1]["history"], [])
        saved = self.request("PUT", self.prefix + "favorites/" + story["id"], {"story": story}, self.owner)
        self.assertEqual(saved[0], 200, saved[1])
        self.assertEqual(saved[1]["story"]["source"]["kind"], "imported")
        self.assertEqual(saved[1]["story"]["questions"], story["questions"])
        self.assertEqual(saved[1]["story"]["paragraphs"], story["paragraphs"])
        self.assertEqual(saved[1]["story"]["generationParameters"], parameters)
        self.assertEqual(self.request("GET", self.prefix + "stories/" + story["id"], cookie=self.editor)[0], 404)
        recorded = self.request("POST", self.prefix + "history", {"storyId": story["id"], "story": story, "voiceId": "system:local"}, self.editor)
        self.assertEqual(recorded[0], 200, recorded[1])
        self.assertEqual(self.request("GET", self.prefix + "history", cookie=self.owner)[1]["history"], [])

    def test_generation_parameters_roles_csrf_expected_account_and_long_duration(self):
        self.provider.generation_enabled = True
        for cookie, headers, status in ((None, {}, 401), (self.viewer, {}, 403),
                                        (self.owner, {"X-Requested-With": ""}, 403),
                                        (self.owner, {"Origin": "https://other.example.com"}, 403),
                                        (self.owner, {"X-Expected-User": self.editor_user["id"]}, 401)):
            with self.subTest(status=status, headers=headers):
                self.assertEqual(self.request("POST", self.prefix + "generate", {}, cookie, headers)[0], status)
        for parameters in ({"ageGroup": "adult"}, {"category": "高刺激竞技"}, {"durationMinutes": True},
                           {"durationMinutes": "5"}, {"style": ["healing"]}, {"protagonist": "evil"},
                           {"moral": 1}, {"blockScary": "false"}, {"interactive": None}, {"prompt": "注入任意文本"}):
            self.assertEqual(self.request("POST", self.prefix + "generate", parameters, self.owner)[0], 400, parameters)
        self.assertEqual(self.provider.generate_calls, [])
        status, payload, _ = self.request("POST", self.prefix + "generate", {"durationMinutes": 5, "category": "亲情暖心"}, self.editor)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["story"]["readMinutes"], 5)
        self.assertTrue(payload["story"]["generationParameters"]["blockScary"])
        self.assertFalse(payload["story"]["generationParameters"]["interactive"])

    def test_generation_rejects_untrusted_external_format_content_and_questions(self):
        self.provider.generation_enabled = True
        good = self.provider.generate(dict(bedtime.GENERATION_DEFAULTS))
        self.provider.generate_calls.clear()
        invalid = [None, {"title": "标题", "paragraphs": "wrong", "questions": []},
                   {**good, "paragraphs": ["超长内容" * 3000, good["paragraphs"][1]]},
                   {**good, "title": "<script>private-output</script>"},
                   {**good, "paragraphs": ["恐怖怪物" + good["paragraphs"][0], good["paragraphs"][1]]},
                   {**good, "paragraphs": [good["paragraphs"][0], bedtime.SLEEP_ENDING]},
                   {**good, "questions": [{"afterParagraph": 1, "question": "这是最后一段的互动问题吗？"}]},
                   {**good, "questions": [{"afterParagraph": True, "question": "云朵会是什么样的颜色呢？"}]},
                   {**good, "questions": [{"afterParagraph": 0, "question": "必须回答！赶快抢答！"}]}]
        for output in invalid:
            # None is sent through a patched method because FakeProviders uses
            # None to mean its valid default fixture.
            with patch.object(self.provider, "generate", return_value=output):
                status, result, _ = self.request("POST", self.prefix + "generate", {"interactive": True}, self.owner)
            self.assertEqual(status, 502, output)
            self.assertNotIn("private-output", json.dumps(result))
        self.assertEqual(self.request("GET", self.prefix + "favorites", cookie=self.owner)[1]["favorites"], [])

    def test_generation_rechecks_logout_revocation_and_role_after_provider_without_db_lock(self):
        self.provider.generation_enabled = True
        def revoke():
            with self.server.db() as conn:
                conn.execute("DELETE FROM members WHERE space_id=? AND user_id=?", (self.sid, self.editor_user["id"]))
        self.provider.on_generate = revoke
        self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.editor)[0], 404)
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "editor@example.com", "role": "editor"}, self.owner)
        def demote():
            with self.server.db() as conn:
                conn.execute("UPDATE members SET role='viewer' WHERE space_id=? AND user_id=?", (self.sid, self.editor_user["id"]))
        self.provider.on_generate = demote
        self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.editor)[0], 403)
        self.request("POST", f"/api/spaces/{self.sid}/members", {"email": "editor@example.com", "role": "editor"}, self.owner)
        def logout():
            with self.server.db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (self.editor_user["id"],))
        self.provider.on_generate = logout
        self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.editor)[0], 401)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_favorites").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_history").fetchone()[0], 0)

    def test_generation_account_rate_and_concurrency_limits_do_not_call_provider(self):
        self.provider.generation_enabled = True
        uid = self.owner_user["id"]
        for key, limit in (("bedtime-generate-hour", 10), ("bedtime-generate-day", 20)):
            self.server.rate_events.clear()
            self.server.rate_events[(key, uid)] = [time.time()] * limit
            self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.owner)[0], 429)
        self.server.rate_events.clear()
        self.server.bedtime_provider_slots.acquire()
        self.server.bedtime_provider_slots.acquire()
        try:
            self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.owner)[0], 429)
        finally:
            self.server.bedtime_provider_slots.release()
            self.server.bedtime_provider_slots.release()
        self.assertEqual(self.provider.generate_calls, [])
        self.provider.failure = True
        self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.owner)[0], 502)

    def test_official_generation_compatible_request_and_incomplete_output_rejection(self):
        provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "test-secret-not-real"})
        self.assertTrue(provider.generation_enabled)
        self.assertTrue(provider.voice_enabled)
        raw = self.provider.generate(dict(bedtime.GENERATION_DEFAULTS))
        completed = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]}
        for duration, limit in ((1, 900), (5, 2600)):
            with self.subTest(duration=duration), patch.object(provider, "_json", return_value=completed) as fake:
                self.assertEqual(provider.generate({**bedtime.GENERATION_DEFAULTS, "durationMinutes": duration}), raw)
                url, token, body = fake.call_args.args
                self.assertEqual(url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
                self.assertEqual(token, "test-secret-not-real")
                self.assertEqual(body["model"], bedtime.GENERATION_MODEL)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
                self.assertEqual(body["max_tokens"], limit)
                self.assertNotIn("max_completion_tokens", body)
                self.assertNotIn("enable_thinking", body)
        for response in ({"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                         {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}):
            with patch.object(provider, "_json", return_value=response), self.assertRaises(bedtime.ProviderFailure):
                provider.generate(dict(bedtime.GENERATION_DEFAULTS))

    def test_flash_generation_profile_short_long_and_no_model_fallback(self):
        provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "test-secret-not-real",
                                          "STORY_GENERATION_MODEL": "qwen3.8-flash"})
        self.assertTrue(provider.generation_enabled)
        for duration, limit in ((1, 900), (5, 2600)):
            raw = self.provider.generate({**bedtime.GENERATION_DEFAULTS, "durationMinutes": duration})
            completed = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]}
            with self.subTest(duration=duration), patch.object(provider, "_json", return_value=completed) as fake:
                self.assertEqual(provider.generate({**bedtime.GENERATION_DEFAULTS, "durationMinutes": duration}), raw)
                url, token, body = fake.call_args.args
                self.assertEqual(url, bedtime.GENERATION_URL)
                self.assertEqual(token, "test-secret-not-real")
                self.assertEqual(body["model"], "qwen3.8-flash")
                self.assertIs(body["enable_thinking"], False)
                self.assertEqual(body["max_completion_tokens"], limit)
                self.assertNotIn("max_tokens", body)
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        with patch.object(provider, "_json", side_effect=bedtime.ProviderFailure("供应商暂时不可用")) as fake:
            with self.assertRaises(bedtime.ProviderFailure):
                provider.generate(dict(bedtime.GENERATION_DEFAULTS))
            self.assertEqual(fake.call_count, 1)
            self.assertEqual(fake.call_args.args[2]["model"], "qwen3.8-flash")

    def test_invalid_generation_model_is_disabled_and_not_exposed(self):
        private_setting = "unsupported-private-model-setting"
        provider = self.server.bedtime_provider = bedtime.HTTPProviders({
            "DASHSCOPE_API_KEY": "test-secret-not-real", "STORY_GENERATION_MODEL": private_setting,
        })
        self.assertFalse(provider.generation_enabled)
        self.assertTrue(provider.voice_enabled)
        with patch.object(provider, "_json") as fake:
            with self.assertRaises(bedtime.ProviderFailure):
                provider.generate(dict(bedtime.GENERATION_DEFAULTS))
            status, config, _ = self.request("GET", self.prefix + "config", cookie=self.owner)
            self.assertEqual(status, 200)
            self.assertFalse(config["storyGeneration"]["enabled"])
            self.assertEqual(config["storyGeneration"]["model"], "")
            self.assertNotIn(private_setting, json.dumps(config))
            self.assertEqual(self.request("POST", self.prefix + "generate", {}, self.owner)[0], 503)
            fake.assert_not_called()

    def test_provider_feature_flags_require_exact_one_and_are_independent(self):
        for flag in ("0", "", "true", "yes", " 1", "1 ", 1, True):
            with self.subTest(flag=flag):
                provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "test-secret-not-real",
                                                  "STORY_GENERATION_ENABLED": flag})
                self.assertFalse(provider.generation_enabled)
                self.assertTrue(provider.voice_enabled)
                with patch.object(provider, "_json") as fake:
                    with self.assertRaises(bedtime.ProviderFailure):
                        provider.generate(dict(bedtime.GENERATION_DEFAULTS))
                    fake.assert_not_called()
                provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "test-secret-not-real",
                                                  "CLOUD_VOICE_ENABLED": flag})
                self.assertTrue(provider.generation_enabled)
                self.assertFalse(provider.voice_enabled)
                with patch.object(provider, "_json") as fake:
                    with self.assertRaises(bedtime.ProviderFailure):
                        provider.enroll(wav(10), "test-voice")
                    with self.assertRaises(bedtime.ProviderFailure):
                        provider.synthesize("晚安", "Seren", bedtime.SYSTEM_MODEL)
                    fake.assert_not_called()
        missing_key = bedtime.HTTPProviders({"STORY_GENERATION_ENABLED": "1", "CLOUD_VOICE_ENABLED": "1",
                                              "STORY_GENERATION_MODEL": "qwen3.8-flash"})
        self.assertFalse(missing_key.generation_enabled)
        self.assertFalse(missing_key.voice_enabled)

    def test_story_only_key_does_not_enable_cloud_voice_routes(self):
        provider = self.server.bedtime_provider = bedtime.HTTPProviders({
            "DASHSCOPE_API_KEY": "test-secret-not-real", "STORY_GENERATION_MODEL": "qwen3.8-flash",
            "STORY_GENERATION_ENABLED": "1", "CLOUD_VOICE_ENABLED": "0",
        })
        raw = self.provider.generate(dict(bedtime.GENERATION_DEFAULTS))
        completed = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]}
        with patch.object(provider, "_json", return_value=completed) as fake:
            status, config, _ = self.request("GET", self.prefix + "config", cookie=self.owner)
            self.assertEqual(status, 200)
            self.assertTrue(config["storyGeneration"]["enabled"])
            self.assertEqual(config["storyGeneration"]["model"], "qwen3.8-flash")
            self.assertFalse(config["voiceClone"]["enabled"])
            self.assertFalse(config["synthesis"]["enabled"])
            self.assertEqual(config["synthesis"]["systemVoices"], [])
            self.assertFalse(config["webSearch"]["enabled"])
            self.assertTrue(config["systemSpeech"])
            self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.owner)[0], 503)
            self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": "cloud:Seren"}, self.owner)[0], 503)
            self.assertEqual(self.request("GET", self.prefix + "voices", cookie=self.owner)[1]["systemVoices"], [])
            fake.assert_not_called()
            status, result, _ = self.request("POST", self.prefix + "generate", {}, self.owner)
            self.assertEqual(status, 200, result)
            self.assertEqual(fake.call_count, 1)
            self.assertEqual(fake.call_args.args[2]["model"], "qwen3.8-flash")

    def test_original_categories_duration_and_structured_sleep_ending(self):
        for category in bedtime.STORY_CATEGORIES:
            status, result, _ = self.request("GET", self.prefix + "search?category=" + quote(category), cookie=self.owner)
            self.assertEqual(status, 200)
            self.assertTrue(result["stories"], category)
            for story in result["stories"]:
                self.assertEqual(story["source"]["kind"], "original")
                self.assertEqual(story["text"], "\n\n".join([*story["paragraphs"], story["ending"]]))
        for duration in (1, 5):
            result = self.request("GET", self.prefix + "search?durationMinutes=" + str(duration), cookie=self.owner)[1]
            self.assertTrue(result["stories"])
            self.assertTrue(all(story["readMinutes"] == duration for story in result["stories"]))
        for age in ("3-6", "7-10"):
            result = self.request("GET", self.prefix + "search?ageGroup=" + age, cookie=self.owner)[1]
            self.assertTrue(result["stories"])
            self.assertTrue(all(age in story.get("ageGroups", []) for story in result["stories"]))
            self.assertNotIn("seaside-lamp", [story["id"] for story in result["stories"]])
        for query in ("durationMinutes=2", "ageGroup=adult"):
            self.assertEqual(self.request("GET", self.prefix + "search?" + query, cookie=self.owner)[0], 400)

    def test_actual_wav_duration_and_consent_are_required(self):
        self.provider.voice_enabled = True
        for fields in (self.clone_body(consent=False), self.clone_body(seconds=3), self.clone_body(audioBase64="!!!!"), self.clone_body(mimeType="audio/webm")):
            with self.subTest(fields={k: v for k, v in fields.items() if k != "audioBase64"}):
                self.assertIn(self.request("POST", self.prefix + "voices", fields, self.owner)[0], (400, 415))
        self.assertEqual(self.provider.enroll_calls, [])
        truncated = wav(20)[:100]
        self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(audioBase64=base64.b64encode(truncated).decode()), self.owner)[0], 400)

    def test_large_base64_clone_owner_isolation_and_provider_id_not_exposed(self):
        voice = self.clone()
        self.assertEqual(voice["state"], "ready")
        self.assertEqual(len(self.provider.enroll_calls[0][0]), len(wav(20)))
        self.assertGreater(len(self.clone_body()["audioBase64"]), 1024 ** 2)
        listed = self.request("GET", self.prefix + "voices", cookie=self.owner)[1]
        self.assertNotIn("provider-private-voice", json.dumps(listed))
        self.assertEqual(self.request("GET", self.prefix + "voices", cookie=self.editor)[1]["voices"], [])
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": voice["id"]}, self.editor)[0], 404)
        self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.viewer)[0], 403)

    def test_synthesize_same_text_with_multiple_voices_private_audio_and_expiry(self):
        voice = self.clone()
        text = "同一篇故事文本，与音色选择互相独立。"
        audio_urls = []
        for choice in (voice["id"], "cloud:Seren"):
            status, data, _ = self.request("POST", self.prefix + "synthesize", {"text": text, "voiceId": choice, "speed": 0.8}, self.owner)
            self.assertEqual(status, 200, data)
            self.assertTrue(data["audioUrl"].startswith(self.prefix + "audio/"))
            audio_urls.append(data["audioUrl"])
        self.assertEqual([call[0] for call in self.provider.synth_calls], [text, text])
        self.assertEqual(self.provider.synth_calls[0][2], bedtime.VC_MODEL)
        self.assertEqual(self.provider.synth_calls[1][2], bedtime.SYSTEM_MODEL)
        status, content, headers = self.request("GET", audio_urls[0], cookie=self.owner)
        self.assertEqual(status, 200)
        self.assertEqual(content[:4], b"RIFF")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.request("GET", audio_urls[0], cookie=self.editor)[0], 404)
        self.assertEqual(self.request("GET", audio_urls[0])[0], 401)
        with self.server.db() as conn:
            conn.execute("UPDATE bedtime_audio SET created_at=0")
        self.assertEqual(self.request("GET", audio_urls[0], cookie=self.owner)[0], 404)
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "x" * 601, "voiceId": voice["id"]}, self.owner)[0], 400)

    def test_revoke_during_provider_call_does_not_save_or_expose_voice(self):
        self.provider.voice_enabled = True
        def revoke():
            with self.server.db() as conn:
                conn.execute("DELETE FROM members WHERE space_id=? AND user_id=?", (self.sid, self.editor_user["id"]))
        self.provider.on_enroll = revoke
        status, _, _ = self.request("POST", self.prefix + "voices", self.clone_body(), self.editor)
        self.assertEqual(status, 404)
        self.assertEqual(len(self.provider.deleted), 1)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_voices WHERE user_id=?", (self.editor_user["id"],)).fetchone()[0], 0)

    def test_revoke_during_synthesis_does_not_save_audio(self):
        self.provider.voice_enabled = True
        def revoke():
            with self.server.db() as conn:
                conn.execute("DELETE FROM members WHERE space_id=? AND user_id=?", (self.sid, self.editor_user["id"]))
        self.provider.on_synthesize = revoke
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": "cloud:Seren"}, self.editor)[0], 404)
        self.assertEqual(list(self.server.bedtime_audio_dir.iterdir()), [])

    def test_logout_during_enrollment_and_synthesis_does_not_save_result(self):
        self.provider.voice_enabled = True
        def logout():
            with self.server.db() as conn:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (self.editor_user["id"],))
        self.provider.on_enroll = logout
        self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.editor)[0], 401)
        self.assertEqual(len(self.provider.deleted), 1)
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_voices WHERE user_id=?", (self.editor_user["id"],)).fetchone()[0], 0)
        status, _, headers = self.request("POST", "/api/auth/login", {"email": "editor@example.com", "password": "bedtime-password-123"})
        self.assertEqual(status, 200)
        self.editor = headers["Set-Cookie"].split(";")[0]
        self.provider.on_synthesize = logout
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": "cloud:Seren"}, self.editor)[0], 401)
        self.assertEqual(list(self.server.bedtime_audio_dir.iterdir()), [])

    def test_thirty_second_wav_boundary_and_server_duration_validation(self):
        self.assertEqual(bedtime.pcm_sample(wav(10)), 10)
        self.assertEqual(bedtime.pcm_sample(wav(30)), 30)
        for sample in (wav(9.99), wav(30.01), wav(20, rate=16000), wav(20)[:44]):
            with self.assertRaises(ValueError):
                bedtime.pcm_sample(sample)
        self.provider.voice_enabled = True
        status, result, _ = self.request("POST", self.prefix + "voices", self.clone_body(30), self.owner)
        self.assertEqual(status, 201, result)
        self.assertEqual(len(self.provider.enroll_calls[0][0]), len(wav(30)))

    def test_official_inline_pcm_is_wrapped_as_wav_without_invented_api_parameter(self):
        provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "test-secret-not-real"})
        pcm = b"\x20\x00" * 2400
        with patch.object(provider, "_json", return_value={"output": {"audio": {"data": base64.b64encode(pcm).decode()}}}) as fake:
            generated = provider.synthesize("晚安", "Seren", bedtime.SYSTEM_MODEL)
        self.assertEqual(generated[:4], b"RIFF")
        with wave.open(io.BytesIO(generated), "rb") as recording:
            self.assertEqual(recording.getframerate(), 24000)
            self.assertEqual(recording.getnchannels(), 1)
            self.assertEqual(recording.getsampwidth(), 2)
            self.assertEqual(recording.readframes(recording.getnframes()), pcm)
        self.assertEqual(set(fake.call_args.args[2]["input"]), {"text", "voice", "language_type"})

    def test_synthesis_checks_real_disk_reserve(self):
        self.provider.voice_enabled = True
        with patch.object(bedtime.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * 1024 * 1024)):
            status, _, _ = self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": "cloud:Seren"}, self.owner)
        self.assertEqual(status, 507)
        self.assertEqual(list(self.server.bedtime_audio_dir.iterdir()), [])

    def test_delete_private_voice_deletes_provider_and_cannot_delete_other_user(self):
        voice = self.clone()
        self.assertEqual(self.request("DELETE", self.prefix + "voices/" + voice["id"], cookie=self.editor)[0], 404)
        self.assertEqual(self.request("DELETE", self.prefix + "voices/" + voice["id"], cookie=self.owner)[0], 200)
        self.assertEqual(len(self.provider.deleted), 1)
        self.assertEqual(self.request("GET", self.prefix + "voices", cookie=self.owner)[1]["voices"], [])

    def test_provider_failure_is_not_fake_ready_and_clone_retry_is_visible(self):
        self.provider.voice_enabled = True
        self.provider.failure = True
        self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.owner)[0], 502)
        profiles = self.request("GET", self.prefix + "voices", cookie=self.owner)[1]["voices"]
        self.assertEqual(profiles[0]["state"], "failed")
        self.assertEqual(self.request("POST", self.prefix + "synthesize", {"text": "晚安", "voiceId": profiles[0]["id"]}, self.owner)[0], 404)

    def test_configurable_search_cannot_target_private_network_and_audio_cannot_redirect(self):
        for endpoint in ("https://127.0.0.1", "https://localhost", "https://169.254.169.254", "https://example.com", "https://evil.com/a"):
            provider = bedtime.HTTPProviders({"STORY_SEARCH_PROVIDER": "opensearch", "OPENSEARCH_API_KEY": "secret", "OPENSEARCH_ENDPOINT": endpoint})
            self.assertFalse(provider.search_enabled, endpoint)
        for url in ("https://127.0.0.1/a.wav", "https://example.com/a.wav", "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.evil.com/a.wav", "https://user@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"):
            with self.assertRaises(bedtime.ProviderFailure):
                bedtime.result_audio_url(url)
        self.assertEqual(bedtime.result_audio_url("http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"), "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav")
        with self.assertRaises(bedtime.ProviderFailure):
            bedtime.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://127.0.0.1")

    def test_cloud_concurrency_busy_fails_without_fake_ready(self):
        self.provider.voice_enabled = True
        self.server.bedtime_provider_slots.acquire()
        self.server.bedtime_provider_slots.acquire()
        try:
            self.assertEqual(self.request("POST", self.prefix + "voices", self.clone_body(), self.owner)[0], 429)
            self.assertEqual(self.provider.enroll_calls, [])
        finally:
            self.server.bedtime_provider_slots.release()
            self.server.bedtime_provider_slots.release()


if __name__ == "__main__":
    unittest.main()
