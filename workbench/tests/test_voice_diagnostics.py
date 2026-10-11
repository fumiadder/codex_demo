"""Mock-only voice failure diagnostics: useful categories without private data."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bedtime
import test_qwen_audio as route_fixture
from media_storage import StorageError


PRIVATE_ORIGIN = "https://diagnostic-private-space.cn-beijing.maas.aliyuncs.com"
PRIVATE_KEY = "FAKE_DIAGNOSTIC_PRIVATE_KEY"
PRIVATE_SAMPLE = "https://private-storage.invalid/sample.wav?X-Amz-Signature=FAKE_PRIVATE_SIGNATURE"
PRIVATE_VOICE = "FAKE_PRIVATE_VENDOR_VOICE_ID"
PRIVATE_TEXT = "FAKE_PRIVATE_STORY_TEXT"
PRIVATE_AUDIO = "FAKE_PRIVATE_AUDIO_BASE64"
PRIVATE_ERROR = "FAKE_PRIVATE_ERROR_DETAIL"
PRIVATE_REQUEST = "FAKE_PRIVATE_REQUEST_ID"
PRIVATE_CODE = "FAKE_PRIVATE_UNKNOWN_CODE"
PRIVATE_MARKERS = (PRIVATE_ORIGIN, "diagnostic-private-space", PRIVATE_KEY,
                   PRIVATE_SAMPLE, "private-storage.invalid", "X-Amz-Signature",
                   PRIVATE_VOICE, PRIVATE_TEXT, PRIVATE_AUDIO, PRIVATE_ERROR,
                   PRIVATE_REQUEST, PRIVATE_CODE)
PRIVATE_DETAIL = " ".join(PRIVATE_MARKERS)

OPERATIONS = ("enroll_create", "enroll_query", "enroll_delete", "synthesize",
              "sample_put", "sample_sign")
REASONS = ("auth_failed", "free_quota", "model_denied", "workspace_denied",
           "input_download", "input_format", "input_inspection", "invalid_parameter",
           "model_unavailable", "rate_limited", "provider_unavailable",
           "network_or_timeout", "invalid_response", "timeout", "storage_failure", "unknown")


class MemoryResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def new_provider():
    provider = bedtime.HTTPProviders({
        "DASHSCOPE_API_KEY": PRIVATE_KEY,
        "CLOUD_VOICE_MODEL": bedtime.QWEN_AUDIO_MODEL,
        "CLOUD_VOICE_API_HOST": PRIVATE_ORIGIN,
    })
    provider.opener = Mock()
    return provider


def voice_body(action="create_voice"):
    return {"model": "voice-enrollment", "input": {
        "action": action, "target_model": bedtime.QWEN_AUDIO_MODEL,
        "prefix": "bt12345678", "url": PRIVATE_SAMPLE,
        "voice_id": PRIVATE_VOICE, "audio": PRIVATE_AUDIO,
    }}


class VoiceDiagnosticAssertions:
    def assert_safe_record(self, captured, operation, reason, status):
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        message = record.getMessage()
        # Enforce the entire log payload, so unexpected fields cannot pass
        # merely because the known private markers are absent.
        self.assertEqual(message, f"voice_failure operation={operation} http_status={status} reason={reason}")
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, message)
            self.assertNotIn(marker, "\n".join(captured.output))
        self.assertNotIn("Traceback", "\n".join(captured.output))

    def assert_safe_exception(self, error):
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, str(error))


class VoiceDiagnosticHelperTests(VoiceDiagnosticAssertions, unittest.TestCase):
    def test_all_permitted_operations_and_reasons_are_fixed_fields(self):
        for operation in OPERATIONS:
            for reason in REASONS:
                with self.subTest(operation=operation, reason=reason):
                    with self.assertLogs(level="WARNING") as captured:
                        bedtime.log_voice_failure(operation, reason, 403)
                    self.assert_safe_record(captured, operation, reason, 403)

    def test_default_status_and_unknown_reasons_do_not_log_untrusted_values(self):
        for reason in (PRIVATE_DETAIL, None, {}, [], 1, True):
            with self.subTest(reason_type=type(reason).__name__):
                with self.assertLogs(level="WARNING") as captured:
                    bedtime.log_voice_failure("enroll_create", reason)
                self.assert_safe_record(captured, "enroll_create", "unknown", 0)

    def test_unknown_or_malformed_operations_produce_no_log(self):
        for operation in (PRIVATE_DETAIL, "enroll_create\n" + PRIVATE_KEY,
                          "story_generate", "search", "", None, {}, [], 1, True):
            with self.subTest(operation_type=type(operation).__name__):
                with self.assertNoLogs(level="WARNING"):
                    bedtime.log_voice_failure(operation, PRIVATE_DETAIL, PRIVATE_DETAIL)

    def test_status_accepts_only_plain_integers_in_http_range(self):
        for status, expected in ((100, 100), (599, 599), (0, 0), (99, 0), (600, 0),
                                 (-1, 0), (403.0, 0), ("403", 0), (True, 0),
                                 (False, 0), (None, 0), ({}, 0), ([], 0), (PRIVATE_DETAIL, 0)):
            with self.subTest(status_type=type(status).__name__, expected=expected):
                with self.assertLogs(level="WARNING") as captured:
                    bedtime.log_voice_failure("sample_sign", "storage_failure", status)
                self.assert_safe_record(captured, "sample_sign", "storage_failure", expected)


class VoiceProviderDiagnosticTests(VoiceDiagnosticAssertions, unittest.TestCase):
    def call_json(self, provider, body=None):
        return provider._json(PRIVATE_ORIGIN + "/api/v1/services/audio/tts/customization",
                              PRIVATE_KEY, voice_body() if body is None else body)

    def test_http_error_categories_never_log_provider_body_or_exception(self):
        cases = (
            (401, PRIVATE_CODE, "auth_failed"),
            (403, "AllocationQuota.FreeTierOnly", "free_quota"),
            (403, "Model.AccessDenied", "model_denied"),
            (403, "Workspace.AccessDenied", "workspace_denied"),
            (403, "App.AccessDenied", "workspace_denied"),
            (400, "BadRequest.InputDownloadFailed", "input_download"),
            (400, "InvalidURL.Timeout", "input_download"),
            (400, "InvalidURL.ConnectionRefused", "input_download"),
            (400, "BadRequest.UnsupportedFileFormat", "input_format"),
            (400, "InvalidParameter.DataInspection", "input_inspection"),
            (400, "InvalidParameter", "invalid_parameter"),
            (404, "model_not_found", "model_unavailable"),
            (404, "ModelNotFound", "model_unavailable"),
            (429, PRIVATE_CODE, "rate_limited"),
            (500, PRIVATE_CODE, "provider_unavailable"),
            (502, PRIVATE_CODE, "provider_unavailable"),
            (503, "ServiceUnavailable", "provider_unavailable"),
            (504, "GatewayTimeout.InputDownload", "input_download"),
            (400, PRIVATE_CODE, "unknown"),
            (403, PRIVATE_CODE, "unknown"),
            (400, {"nested_private_code": PRIVATE_DETAIL}, "unknown"),
            (400, [PRIVATE_DETAIL], "unknown"),
        )
        for status, code, expected in cases:
            with self.subTest(status=status, code_type=type(code).__name__, expected=expected):
                provider = new_provider()
                payload = json.dumps({"code": code, "message": PRIVATE_DETAIL,
                                      "request_id": PRIVATE_REQUEST,
                                      "output": {"voice_id": PRIVATE_VOICE, "url": PRIVATE_SAMPLE}}).encode()
                error = HTTPError(PRIVATE_ORIGIN + "/" + PRIVATE_KEY, status,
                                  PRIVATE_DETAIL, {"Private-Header": PRIVATE_KEY}, io.BytesIO(payload))
                provider.opener.open.side_effect = error
                with self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(bedtime.ProviderFailure) as caught:
                        self.call_json(provider)
                self.assert_safe_record(captured, "enroll_create", expected, status)
                self.assert_safe_exception(caught.exception)
                self.assertTrue(error.fp.closed)
                self.assertEqual(provider.opener.open.call_count, 1)

    def test_query_delete_and_synthesis_are_identified_without_logging_private_input(self):
        bodies = [(voice_body("query_voice"), "enroll_query"),
                  (voice_body("delete_voice"), "enroll_delete"),
                  ({"model": bedtime.QWEN_AUDIO_MODEL, "input": {
                      "text": PRIVATE_TEXT, "voice": PRIVATE_VOICE, "format": "wav", "sample_rate": 24000}},
                   "synthesize")]
        for body, operation in bodies:
            with self.subTest(operation=operation):
                provider = new_provider()
                provider.opener.open.side_effect = HTTPError(PRIVATE_SAMPLE, 403, PRIVATE_DETAIL, {},
                    io.BytesIO(json.dumps({"code": "Model.AccessDenied", "message": PRIVATE_DETAIL}).encode()))
                with self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(bedtime.ProviderFailure):
                        self.call_json(provider, body)
                self.assert_safe_record(captured, operation, "model_denied", 403)

    def test_nonvoice_requests_and_unknown_actions_do_not_create_voice_logs(self):
        bodies = [{"model": "qwen3.8-flash", "input": {"text": PRIVATE_TEXT}},
                  {"model": "voice-enrollment", "input": {"action": PRIVATE_DETAIL}},
                  {"model": "voice-enrollment", "input": {"action": {"private": PRIVATE_KEY}}},
                  {"model": "voice-enrollment", "input": [PRIVATE_DETAIL]}, {}]
        for body in bodies:
            with self.subTest(model=body.get("model", "none")):
                provider = new_provider()
                provider.opener.open.side_effect = HTTPError(PRIVATE_SAMPLE, 400, PRIVATE_DETAIL, {},
                    io.BytesIO(json.dumps({"code": PRIVATE_CODE, "message": PRIVATE_DETAIL}).encode()))
                with self.assertNoLogs(level="WARNING"):
                    with self.assertRaises(bedtime.ProviderFailure) as caught:
                        self.call_json(provider, body)
                self.assert_safe_exception(caught.exception)

    def test_network_and_timeout_exceptions_log_only_fixed_category(self):
        for error in (URLError(PRIVATE_DETAIL), TimeoutError(PRIVATE_DETAIL), OSError(PRIVATE_DETAIL)):
            with self.subTest(error_type=type(error).__name__):
                provider = new_provider()
                provider.opener.open.side_effect = error
                with self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(bedtime.ProviderFailure) as caught:
                        self.call_json(provider)
                self.assert_safe_record(captured, "enroll_create", "network_or_timeout", 0)
                self.assert_safe_exception(caught.exception)

    def test_nonjson_nondict_and_oversized_success_responses_are_invalid_response(self):
        for payload in (PRIVATE_DETAIL.encode(), json.dumps([PRIVATE_DETAIL]).encode(),
                        b"{" + b"x" * (2 * 1024 * 1024)):
            with self.subTest(payload_size=len(payload)):
                provider = new_provider()
                provider.opener.open.return_value = MemoryResponse(payload)
                with self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(bedtime.ProviderFailure) as caught:
                        self.call_json(provider)
                self.assert_safe_record(captured, "enroll_create", "invalid_response", 0)
                self.assert_safe_exception(caught.exception)

    def test_nonjson_and_oversized_http_error_body_stay_bounded_and_unknown(self):
        for payload in (PRIVATE_DETAIL.encode(), PRIVATE_DETAIL.encode() * 1000):
            with self.subTest(payload_size=len(payload)):
                provider = new_provider()
                error = HTTPError(PRIVATE_SAMPLE, 400, PRIVATE_DETAIL, {}, io.BytesIO(payload))
                provider.opener.open.side_effect = error
                with self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(bedtime.ProviderFailure) as caught:
                        self.call_json(provider)
                self.assert_safe_record(captured, "enroll_create", "unknown", 400)
                self.assert_safe_exception(caught.exception)
                self.assertTrue(error.fp.closed)

    def test_successful_voice_response_creates_no_failure_log(self):
        provider = new_provider()
        output = {"output": {"voice_id": PRIVATE_VOICE}, "request_id": PRIVATE_REQUEST}
        provider.opener.open.return_value = MemoryResponse(json.dumps(output).encode())
        with self.assertNoLogs(level="WARNING"):
            self.assertEqual(self.call_json(provider), output)


class VoiceSampleDiagnosticTests(VoiceDiagnosticAssertions, unittest.TestCase):
    def test_put_and_sign_failures_preserve_cleanup_and_hide_sdk_exception(self):
        for method, operation in (("put", "sample_put"), ("signed_voice_sample_url", "sample_sign")):
            with self.subTest(operation=operation):
                fixture = route_fixture.QwenAudioSampleRouteTests(
                    "test_sample_reserved_signed_then_deleted_without_app_publication")
                fixture.setUp()
                try:
                    with patch.object(fixture.storage, method, side_effect=StorageError(PRIVATE_DETAIL)):
                        with self.assertLogs(level="WARNING") as captured:
                            with self.assertRaises(StorageError):
                                fixture.invoke("POST", "voices", fixture.clone_body())
                    self.assert_safe_record(captured, operation, "storage_failure", 0)
                    self.assertEqual(fixture.remote_calls, [])
                    self.assertEqual(fixture.storage.objects, {})
                    with fixture.server.db() as conn:
                        self.assertEqual(conn.execute("SELECT state FROM bedtime_voices").fetchone()[0], "failed")
                        self.assertEqual(conn.execute("SELECT count(*) FROM bedtime_audio").fetchone()[0], 0)
                        self.assertEqual(conn.execute("SELECT count(*) FROM storage_jobs").fetchone()[0], 0)
                finally:
                    fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
