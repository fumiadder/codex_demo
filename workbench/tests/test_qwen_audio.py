"""New Qwen Audio protocol tests. All cloud and object calls are explicit fakes."""
import base64
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bedtime
import storage_jobs
from media_storage import StorageError
from server import APIError, FolioHandler, SCHEMA

TEST_ORIGIN = "https://test-space.cn-beijing.maas.aliyuncs.com"


def new_provider(**values):
    return bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "fake-key-not-a-real-credential",
                                 "CLOUD_VOICE_MODEL": bedtime.QWEN_AUDIO_MODEL,
                                 "CLOUD_VOICE_API_HOST": TEST_ORIGIN, **values})


def recording(seconds=10):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x20\x00" * (24000 * seconds))
    return buffer.getvalue()


class MemoryResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class QwenAudioProtocolTests(unittest.TestCase):
    def test_new_model_requires_exact_configured_origin_without_fallback(self):
        for host in ("", "https://127.0.0.1", "https://localhost", "https://evil.com", "http://test-space.cn-beijing.maas.aliyuncs.com",
                     TEST_ORIGIN + "/", TEST_ORIGIN + "/path", TEST_ORIGIN + "?key=secret",
                     "https://user@test-space.cn-beijing.maas.aliyuncs.com", TEST_ORIGIN + ":443",
                     "https://test-space.cn-beijing.maas.aliyuncs.com.evil.com"):
            with self.subTest(host=host):
                provider = new_provider(CLOUD_VOICE_API_HOST=host)
                self.assertFalse(provider.voice_enabled)
                self.assertTrue(provider.generation_enabled)
                with patch.object(provider, "_json") as call:
                    with self.assertRaises(bedtime.ProviderFailure):
                        provider.enroll("https://example.com/signed.wav", "bt12345678")
                    call.assert_not_called()
        self.assertTrue(new_provider().voice_enabled)
        for model in ("not-a-model", "qwen-audio-3.0-tts-flash"):
            self.assertFalse(new_provider(CLOUD_VOICE_MODEL=model).voice_enabled)
        self.assertFalse(new_provider(CLOUD_VOICE_ENABLED="0").voice_enabled)

    def test_official_create_query_and_delete_use_new_protocol_only(self):
        provider = new_provider()
        outputs = [{"output": {"voice_id": "opaque-provider-voice"}},
                   {"output": {"status": "DEPLOYING", "target_model": bedtime.QWEN_AUDIO_MODEL}},
                   {"output": {"status": "OK", "target_model": bedtime.QWEN_AUDIO_MODEL}}, {"output": {}}]
        with patch.object(provider, "_json", side_effect=outputs) as call, patch.object(bedtime.time, "sleep"):
            voice, model = provider.enroll("https://example.com/signed.wav?token=private", "bt12345678")
            provider.delete_voice_for_model(voice, model)
        self.assertEqual((voice, model), ("opaque-provider-voice", bedtime.QWEN_AUDIO_MODEL))
        bodies = [invocation.args[2] for invocation in call.call_args_list]
        self.assertEqual(bodies[0], {"model": "voice-enrollment", "input": {
            "action": "create_voice", "target_model": bedtime.QWEN_AUDIO_MODEL, "prefix": "bt12345678",
            "url": "https://example.com/signed.wav?token=private", "language_hints": ["zh"]}})
        self.assertEqual([body["input"]["action"] for body in bodies], ["create_voice", "query_voice", "query_voice", "delete_voice"])
        self.assertEqual(bodies[-1]["input"]["voice_id"], voice)
        self.assertTrue(all(invocation.args[0] == TEST_ORIGIN + "/api/v1/services/audio/tts/customization" for invocation in call.call_args_list))
        self.assertNotIn("audio", bodies[0]["input"])
        self.assertNotIn("qwen-voice-enrollment", json.dumps(bodies))

    def test_only_ok_is_ready_and_failed_remote_voice_is_deleted(self):
        for status in ("UNDEPLOYED", "UNKNOWN", None):
            provider = new_provider()
            with patch.object(provider, "_json", side_effect=[{"output": {"voice_id": "vendor-only-id"}},
                                                              {"output": {"status": status}}, {"output": {}}]) as call:
                with self.assertRaises(bedtime.ProviderFailure):
                    provider.enroll("https://example.com/signed.wav", "bt12345678")
            self.assertEqual(call.call_args_list[-1].args[2]["input"]["action"], "delete_voice")

    def test_wrong_query_target_model_fails_and_cleans_up(self):
        provider = new_provider()
        with patch.object(provider, "_json", side_effect=[{"output": {"voice_id": "vendor-only-id"}},
                                                          {"output": {"status": "OK", "target_model": bedtime.VC_MODEL}}, {"output": {}}]):
            with self.assertRaises(bedtime.ProviderFailure):
                provider.enroll("https://example.com/signed.wav", "bt12345678")

    def test_polling_and_request_timeouts_share_one_45_second_deadline(self):
        provider = new_provider()
        clock = [0.0]
        calls = []
        def response(url, key, body, *, timeout):
            calls.append((body, timeout))
            self.assertLessEqual(timeout, 45 - clock[0])
            if body["input"]["action"] == "create_voice":
                clock[0] += 10
                return {"output": {"voice_id": "vendor-only-id"}}
            clock[0] += min(6, timeout)
            return {"output": {"status": "DEPLOYING"}}
        with patch.object(bedtime.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(bedtime.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(provider, "_json", side_effect=response):
            with self.assertRaises(bedtime.EnrollmentFailure) as caught:
                provider.enroll("https://example.com/signed.wav", "bt12345678")
        self.assertEqual(clock[0], 45)
        self.assertEqual(caught.exception.remote_voice, "vendor-only-id")
        self.assertNotIn("vendor-only-id", str(caught.exception))

    def test_late_create_retains_server_only_identifier_without_more_calls(self):
        provider = new_provider()
        clock = [0.0]
        def late(url, key, body, **kwargs):
            clock[0] = 46
            return {"output": {"voice_id": "late-vendor-only-id"}}
        with patch.object(bedtime.time, "monotonic", side_effect=lambda: clock[0]), patch.object(provider, "_json", side_effect=late) as call:
            with self.assertRaises(bedtime.EnrollmentFailure) as caught:
                provider.enroll("https://example.com/signed.wav", "bt12345678")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(caught.exception.remote_voice, "late-vendor-only-id")
        self.assertNotIn("late-vendor-only-id", str(caught.exception))

    def test_new_synthesis_protocol_and_public_voice_ids_are_separate_from_legacy(self):
        provider = new_provider()
        self.assertEqual({voice["id"] for voice in provider.system_voices}, {
            "cloud:wenhuaiqing_v3.1", "cloud:longyuan_v3.1", "cloud:longsanshu_v3.1"})
        self.assertFalse(any(voice["id"] == "cloud:Serena" for voice in provider.system_voices))
        result_url = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/sample.wav?signature=private"
        opener = Mock()
        opener.open.return_value = MemoryResponse(recording(1))
        provider.opener = opener
        with patch.object(provider, "_json", return_value={"output": {"finish_reason": "stop", "audio": {"url": result_url}}}) as call:
            audio = provider.synthesize("故事正文不随音色改变。", "wenhuaiqing_v3.1", bedtime.QWEN_AUDIO_MODEL)
        self.assertEqual(audio[:4], b"RIFF")
        self.assertEqual(call.call_args.args[0], TEST_ORIGIN + "/api/v1/services/audio/tts/SpeechSynthesizer")
        self.assertEqual(call.call_args.args[2], {"model": bedtime.QWEN_AUDIO_MODEL, "input": {
            "text": "故事正文不随音色改变。", "voice": "wenhuaiqing_v3.1", "format": "wav", "sample_rate": 24000}})
        downloaded_request = opener.open.call_args.args[0]
        self.assertTrue(downloaded_request.full_url.startswith("https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/"))
        self.assertFalse(downloaded_request.has_header("Authorization"))

    def test_no_cross_profile_synthesis_delete_or_incomplete_output_fallback(self):
        provider = new_provider()
        with patch.object(provider, "_json") as call:
            for function, args in ((provider.synthesize, ("晚安", "legacy-id", bedtime.VC_MODEL)),
                                   (provider.delete_voice_for_model, ("legacy-id", bedtime.VC_MODEL))):
                with self.assertRaises(bedtime.ProviderFailure):
                    function(*args)
            call.assert_not_called()
        with patch.object(provider, "_json", return_value={"output": {"finish_reason": "length", "audio": {"url": "https://example.com/audio.wav"}}}):
            with self.assertRaises(bedtime.ProviderFailure):
                provider.synthesize("晚安", "wenhuaiqing_v3.1", bedtime.QWEN_AUDIO_MODEL)

    def test_fixed_auth_free_quota_and_unknown_forbidden_messages_hide_provider_secrets(self):
        cases = [(401, {"code": "bad-secret-code", "message": "private-key-and-host"}, "认证失败"),
                 (403, {"code": "AllocationQuota.FreeTierOnly", "message": "private-key-and-host"}, "免费额度限制"),
                 (403, {"code": "UnknownPrivateVendorCode", "message": "private-key-and-host"}, "核对业务空间")]
        for status, body, expected in cases:
            provider = new_provider()
            error = HTTPError("https://secret-business-host.invalid/key-secret", status, "private message", {}, io.BytesIO(json.dumps(body).encode()))
            provider.opener = Mock()
            provider.opener.open.side_effect = error
            with self.assertRaises(bedtime.ProviderFailure) as caught:
                provider._json(TEST_ORIGIN + "/api", "fake-secret", {})
            self.assertIn(expected, str(caught.exception))
            for secret in ("private-key-and-host", "UnknownPrivateVendorCode", "secret-business-host", "fake-secret"):
                self.assertNotIn(secret, str(caught.exception))
            self.assertEqual(provider.opener.open.call_count, 1)

    def test_legacy_default_enrollment_stays_compatible_and_generation_uses_original_url(self):
        provider = bedtime.HTTPProviders({"DASHSCOPE_API_KEY": "fake-key", "STORY_GENERATION_MODEL": "qwen3.8-flash"})
        self.assertFalse(provider.requires_sample_url)
        with patch.object(provider, "_json", return_value={"output": {"voice": "legacy-only-id"}}) as call:
            self.assertEqual(provider.enroll(recording(), "bedtime123"), ("legacy-only-id", bedtime.VC_MODEL))
        self.assertEqual(call.call_args.args[0], bedtime.ENROLLMENT_URL)
        self.assertEqual(call.call_args.args[2]["model"], "qwen-voice-enrollment")
        result = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        with patch.object(provider, "_json", return_value=result) as call:
            self.assertEqual(provider.generate(dict(bedtime.GENERATION_DEFAULTS)), {})
        self.assertEqual(call.call_args.args[0], bedtime.GENERATION_URL)
        self.assertEqual(call.call_args.args[2]["model"], "qwen3.8-flash")
        self.assertFalse(call.call_args.args[2]["enable_thinking"])


class FakeS3:
    backend = "s3"

    def __init__(self, server):
        self.server, self.objects, self.signed, self.put_checks = server, {}, [], []
        self.delete_fails = False

    def put(self, namespace, identifier, payload, content_type):
        # A separate writer can enter: the route committed its reservation
        # before this slow object operation.
        with self.server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM storage_jobs WHERE namespace=? AND id=?", (namespace, identifier)).fetchone()
            self.put_checks.append((row["state"], row["size"]))
        self.objects[(namespace, identifier)] = payload

    def signed_voice_sample_url(self, identifier, *, expires_seconds):
        self.signed.append((identifier, expires_seconds))
        return "https://example.com/bedtime-audio/" + identifier + "?temporary-private-signature=secret"

    def delete(self, namespace, identifier):
        if self.delete_fails:
            raise StorageError("Deletion temporarily unavailable")
        return self.objects.pop((namespace, identifier), None) is not None


class FakeHandler:
    string = staticmethod(FolioHandler.string)

    def __init__(self, server, method, body=None):
        self.server, self.command, self.body = server, method, body or {}
        self.headers = {"Content-Type": "application/json"}
        self.active = True
        self._upload_slot = False
        self.response = None

    def current_user(self, conn):
        if not self.active:
            raise APIError(401, "登录已过期")
        return {"id": "b" * 32}

    def membership(self, conn, sid, uid, write=False):
        return FolioHandler.membership(self, conn, sid, uid, write=write)

    def read_body(self, limit):
        payload = json.dumps(self.body).encode()
        if len(payload) > limit:
            raise APIError(413, "请求内容超过大小限制")
        return payload

    def read_json(self):
        return self.body

    def json_response(self, status, body):
        self.response = (status, body)


class QwenAudioSampleRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        @contextmanager
        def db():
            conn = sqlite3.connect(self.path, timeout=0.2)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        self.server = SimpleNamespace(db=db, bedtime_error=APIError, bedtime_provider=new_provider(),
                                      bedtime_provider_slots=threading.BoundedSemaphore(2), upload_slots=threading.BoundedSemaphore(4),
                                      max_space_storage=2 ** 30, max_total_storage=2 ** 30,
                                      storage_cleanup_lock=threading.Lock(), maybe_cleanup=lambda: None,
                                      throttle=lambda *args: None)
        self.server.cleanup_media = lambda: storage_jobs.collect(self.server)
        self.storage = self.server.media_storage = FakeS3(self.server)
        self.sid = "a" * 32
        self.prefix = "/api/spaces/" + self.sid + "/bedtime/"
        with db() as conn:
            conn.executescript(SCHEMA + bedtime.SCHEMA + storage_jobs.SCHEMA)
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?)", ("b" * 32, "test@example.com", "测试", "not-used", "now"))
            conn.execute("INSERT INTO spaces VALUES(?,?,?,?)", (self.sid, "测试", "b" * 32, "now"))
            conn.execute("INSERT INTO members VALUES(?,?,?)", (self.sid, "b" * 32, "owner"))
        self.provider = self.server.bedtime_provider
        self.remote_calls = []
        self.provider._json = self.provider_response

    def tearDown(self):
        self.temp.cleanup()

    def provider_response(self, url, key, body, **kwargs):
        self.remote_calls.append(body)
        action = body["input"]["action"]
        if action == "create_voice":
            return {"output": {"voice_id": "provider-id-never-public"}}
        if action == "query_voice":
            return {"output": {"status": "OK", "target_model": bedtime.QWEN_AUDIO_MODEL}}
        return {"output": {}}

    def invoke(self, method, route, body=None, handler=None):
        handler = handler or FakeHandler(self.server, method, body)
        try:
            self.assertTrue(bedtime.dispatch(handler, self.prefix + route, {}))
            return handler.response
        finally:
            if handler._upload_slot:
                self.server.upload_slots.release()
                handler._upload_slot = False

    def clone_body(self):
        return {"name": "自己的声音", "mimeType": "audio/wav", "consent": True,
                "audioBase64": base64.b64encode(recording()).decode()}

    def test_sample_reserved_signed_then_deleted_without_app_publication(self):
        status, result = self.invoke("POST", "voices", self.clone_body())
        self.assertEqual(status, 201)
        self.assertEqual(result["voice"]["state"], "ready")
        self.assertEqual(self.storage.put_checks, [("pending", len(recording()))])
        sample_id, lifetime = self.storage.signed[0]
        self.assertEqual(lifetime, 300)
        self.assertNotEqual(sample_id, result["voice"]["id"])
        self.assertEqual(self.storage.objects, {})
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_audio").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
            saved = conn.execute("SELECT * FROM bedtime_voices").fetchone()
            self.assertEqual(saved["target_model"], bedtime.QWEN_AUDIO_MODEL)
            self.assertEqual(saved["provider_voice"], "provider-id-never-public")
        self.assertNotIn("provider-id-never-public", json.dumps(result))
        self.assertNotIn("temporary-private-signature", json.dumps(result))
        with self.assertRaises(APIError) as caught:
            self.invoke("GET", "audio/" + sample_id)
        self.assertEqual(caught.exception.status, 404)

    def test_local_storage_does_not_offer_or_call_new_clone(self):
        self.server.media_storage = SimpleNamespace(backend="local")
        _, config = self.invoke("GET", "config")
        self.assertFalse(config["voiceClone"]["enabled"])
        self.assertTrue(config["synthesis"]["enabled"])
        self.assertNotIn("test-space", json.dumps(config))
        with self.assertRaises(APIError) as caught:
            self.invoke("POST", "voices", self.clone_body())
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.remote_calls, [])

    def test_sample_reservation_obeys_existing_quota_before_object_or_cloud_call(self):
        self.server.max_space_storage = len(recording()) - 1
        with self.assertRaises(APIError) as caught:
            self.invoke("POST", "voices", self.clone_body())
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.remote_calls, [])
        self.assertEqual(self.storage.objects, {})

    def test_failed_sample_cleanup_is_durable_and_still_counts_toward_quota(self):
        self.storage.delete_fails = True
        status, _ = self.invoke("POST", "voices", self.clone_body())
        self.assertEqual(status, 201)
        with self.server.db() as conn:
            row = conn.execute("SELECT * FROM storage_jobs").fetchone()
            self.assertEqual(row["state"], "delete")
            self.assertEqual(storage_jobs.usage(conn, self.sid), (len(recording()), len(recording())))
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_audio").fetchone()[0], 0)

    def test_logout_during_new_registration_deletes_remote_voice_and_sample(self):
        handler = FakeHandler(self.server, "POST", self.clone_body())
        original = self.provider._json
        def revoke(url, key, body, **kwargs):
            response = original(url, key, body, **kwargs)
            if body["input"]["action"] == "query_voice":
                handler.active = False
            return response
        self.provider._json = revoke
        with self.assertRaises(APIError) as caught:
            self.invoke("POST", "voices", handler=handler)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.remote_calls[-1]["input"]["action"], "delete_voice")
        self.assertEqual(self.storage.objects, {})
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_voices").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)

    def test_failed_remote_cleanup_after_logout_keeps_private_failed_tracking(self):
        handler = FakeHandler(self.server, "POST", self.clone_body())
        original = self.provider._json
        def revoke(url, key, body, **kwargs):
            if body["input"]["action"] == "delete_voice":
                raise bedtime.ProviderFailure("供应商删除暂时不可用")
            response = original(url, key, body, **kwargs)
            if body["input"]["action"] == "query_voice":
                handler.active = False
            return response
        self.provider._json = revoke
        with self.assertRaises(APIError) as caught:
            self.invoke("POST", "voices", handler=handler)
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("provider-id-never-public", caught.exception.message)
        self.assertEqual(self.storage.objects, {})
        with self.server.db() as conn:
            saved = conn.execute("SELECT * FROM bedtime_voices").fetchone()
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["provider_voice"], "provider-id-never-public")
            self.assertEqual(saved["target_model"], bedtime.QWEN_AUDIO_MODEL)
        self.assertNotIn("provider-id-never-public", json.dumps(self.invoke("GET", "voices")[1]))

    def test_ready_persistence_failure_cleans_remote_and_preserves_original_error(self):
        handler = FakeHandler(self.server, "POST", self.clone_body())
        original = handler.current_user
        calls = [0]
        def auth(conn):
            calls[0] += 1
            if calls[0] == 4:
                raise RuntimeError("simulated persistence failure without a real cloud call")
            return original(conn)
        handler.current_user = auth
        with self.assertRaisesRegex(RuntimeError, "simulated persistence failure"):
            self.invoke("POST", "voices", handler=handler)
        self.assertEqual(self.remote_calls[-1]["input"]["action"], "delete_voice")
        self.assertEqual(self.storage.objects, {})
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_voices").fetchone()[0], 0)

    def test_uncertain_acknowledgement_preserves_confirmed_ready_profile(self):
        voice_id = "c" * 32
        with self.server.db() as conn:
            conn.execute("INSERT INTO bedtime_voices VALUES(?,?,?,?,?,?,?,?)", (
                voice_id, self.sid, "b" * 32, "自己的声音", "ready", "already-published-id", bedtime.QWEN_AUDIO_MODEL, "now"))
        handler = FakeHandler(self.server, "POST")
        bedtime.recover_enrollment(handler, voice_id, self.sid, "b" * 32, "already-published-id", bedtime.QWEN_AUDIO_MODEL)
        self.assertEqual(self.remote_calls, [])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT state FROM bedtime_voices WHERE id=?", (voice_id,)).fetchone()[0], "ready")


if __name__ == "__main__":
    unittest.main()
