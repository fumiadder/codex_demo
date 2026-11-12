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
           "access_denied", "service_not_activated", "endpoint_denied", "account_not_ready",
           "input_download", "input_format", "input_inspection", "invalid_parameter",
           "model_unavailable", "rate_limited", "provider_unavailable",
           "network_or_timeout", "invalid_response", "nonjson_error", "invalid_error_response",
           "timeout", "storage_failure", "unknown")


class MemoryResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ErrorBody(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.read_limits = []
        self.bytes_read = 0

    def read(self, size=-1):
        self.read_limits.append(size)
        result = super().read(size)
        self.bytes_read += len(result)
        return result


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

    def assert_http_failure(self, payload, status, expected):
        provider = new_provider()
        stream = ErrorBody(payload)
        error = HTTPError(PRIVATE_ORIGIN + "/" + PRIVATE_KEY, status,
                          PRIVATE_DETAIL, {"Private-Header": PRIVATE_KEY}, stream)
        provider.opener.open.side_effect = error
        with self.assertLogs(level="WARNING") as captured:
            with self.assertRaises(bedtime.ProviderFailure) as caught:
                self.call_json(provider)
        self.assert_safe_record(captured, "enroll_create", expected, status)
        self.assert_safe_exception(caught.exception)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.read_limits, [16 * 1024 + 1])
        self.assertLessEqual(stream.bytes_read, 16 * 1024 + 1)
        self.assertEqual(provider.opener.open.call_count, 1)
        return str(caught.exception)

    def test_http_error_categories_never_log_provider_body_or_exception(self):
        cases = (
            (401, PRIVATE_CODE, "auth_failed"),
            (403, "AllocationQuota.FreeTierOnly", "free_quota"),
            (403, "Model.AccessDenied", "model_denied"),
            (403, "Workspace.AccessDenied", "workspace_denied"),
            (403, "App.AccessDenied", "workspace_denied"),
            (403, "AccessDenied", "access_denied"),
            (403, "access_denied", "access_denied"),
            (403, "AccessDenied.Unpurchased", "service_not_activated"),
            (403, "Endpoint.AccessDenied", "endpoint_denied"),
            (400, "Arrearage", "account_not_ready"),
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
            for nested in (False, True):
                with self.subTest(status=status, nested=nested, code_type=type(code).__name__, expected=expected):
                    error_fields = {"code": code, "message": PRIVATE_DETAIL, "request_id": PRIVATE_REQUEST}
                    envelope = {"error": error_fields} if nested else error_fields
                    envelope["output"] = {"voice_id": PRIVATE_VOICE, "url": PRIVATE_SAMPLE}
                    message = self.assert_http_failure(json.dumps(envelope).encode(), status, expected)
                    if status == 403:
                        self.assertEqual(message,
                            "免费额度限制阻止了本次调用，请在百炼控制台核对剩余额度；不会自动切换模型或付费调用"
                            if expected == "free_quota" else
                            "百炼拒绝了本次调用，请核对业务空间、模型权限与免费额度设置")

    def test_code_precedence_and_nested_error_shapes_are_conservative(self):
        cases = (
            ({"code": "Model.AccessDenied", "error": {"code": "AllocationQuota.FreeTierOnly"}}, "model_denied"),
            ({"code": PRIVATE_CODE, "error": {"code": "AllocationQuota.FreeTierOnly"}}, "unknown"),
            ({"code": " ", "error": {"code": "Model.AccessDenied"}}, "unknown"),
            ({"code": "", "error": {"code": "AllocationQuota.FreeTierOnly"}}, "free_quota"),
            ({"code": None, "error": {"code": "AccessDenied"}}, "access_denied"),
            ({"code": [PRIVATE_DETAIL], "error": {"code": "Endpoint.AccessDenied"}}, "endpoint_denied"),
            ({"code": {"private": PRIVATE_DETAIL}, "error": {"code": "Arrearage"}}, "account_not_ready"),
            ({"code": True, "error": {"code": "AccessDenied.Unpurchased"}}, "service_not_activated"),
            ({"error": [{"code": "Model.AccessDenied", "message": PRIVATE_DETAIL}]}, "unknown"),
            ({"error": PRIVATE_DETAIL}, "unknown"),
            ({"error": None}, "unknown"),
            ({"error": {"code": {"Model.AccessDenied": PRIVATE_DETAIL}}}, "unknown"),
            ({"error": {"code": ["Model.AccessDenied", PRIVATE_DETAIL]}}, "unknown"),
            ({"error": {"code": 403}}, "unknown"),
            ({"error": {"code": ""}}, "unknown"),
            ({"error": {"code": "Model.AccessDenied\n" + PRIVATE_KEY}}, "unknown"),
            ({"code": " Model.AccessDenied"}, "unknown"),
            ({"code": "Model.AccessDenied." + PRIVATE_CODE}, "unknown"),
            ({"error": {"message": "AllocationQuota.FreeTierOnly", "request_id": PRIVATE_REQUEST}}, "unknown"),
            ({"Code": "Model.AccessDenied", "error": {"Code": "AllocationQuota.FreeTierOnly"}}, "unknown"),
            ({"output": {"code": "Model.AccessDenied", "voice_id": PRIVATE_VOICE}}, "unknown"),
        )
        for envelope, expected in cases:
            with self.subTest(expected=expected, envelope_type=type(envelope.get("error")).__name__):
                envelope["message"] = PRIVATE_DETAIL
                envelope["request_id"] = PRIVATE_REQUEST
                message = self.assert_http_failure(json.dumps(envelope).encode(), 403, expected)
                self.assertEqual("免费额度限制" in message, expected == "free_quota")

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

    def test_unknown_403_error_shapes_are_distinguished_without_exposing_body(self):
        cases = (
            (PRIVATE_DETAIL.encode(), "nonjson_error"),
            (b'{"code":', "nonjson_error"),
            (b'\xff' + PRIVATE_DETAIL.encode(), "nonjson_error"),
            (json.dumps([PRIVATE_DETAIL]).encode(), "invalid_error_response"),
            (json.dumps(PRIVATE_DETAIL).encode(), "invalid_error_response"),
            (b"null", "invalid_error_response"),
            (b"403", "invalid_error_response"),
            (b"true", "invalid_error_response"),
            (PRIVATE_DETAIL.encode() * 1000, "invalid_error_response"),
            (b'{"code":"AllocationQuota.FreeTierOnly"}' + b" " * (16 * 1024), "invalid_error_response"),
            (b"[" * 2000 + b"0" + b"]" * 2000, "invalid_error_response"),
            (b"{}", "unknown"),
            (json.dumps({"code": PRIVATE_CODE, "message": PRIVATE_DETAIL}).encode(), "unknown"),
        )
        for payload, expected in cases:
            with self.subTest(payload_size=len(payload), expected=expected):
                message = self.assert_http_failure(payload, 403, expected)
                self.assertEqual(message, "百炼拒绝了本次调用，请核对业务空间、模型权限与免费额度设置")

    def test_error_shape_classification_preserves_existing_http_status_fallbacks(self):
        for status, expected in ((400, "unknown"), (401, "auth_failed"), (429, "rate_limited"),
                                 (500, "provider_unavailable"), (502, "provider_unavailable"),
                                 (503, "provider_unavailable"), (504, "provider_unavailable")):
            for payload in (PRIVATE_DETAIL.encode(), json.dumps([PRIVATE_DETAIL]).encode(),
                            PRIVATE_DETAIL.encode() * 1000):
                with self.subTest(status=status, payload_size=len(payload)):
                    self.assert_http_failure(payload, status, expected)
        for status, expected in ((401, "auth_failed"), (429, "rate_limited")):
            for code in ("AllocationQuota.FreeTierOnly", "AccessDenied", "Arrearage"):
                with self.subTest(status=status, nested_code=code):
                    self.assert_http_failure(json.dumps({"error": {"code": code, "message": PRIVATE_DETAIL}}).encode(),
                                             status, expected)

    def test_error_body_at_limit_is_read_and_closed_before_classification(self):
        payload = json.dumps({"error": {"code": "AllocationQuota.FreeTierOnly", "message": PRIVATE_DETAIL}}).encode()
        payload += b" " * (16 * 1024 - len(payload))
        message = self.assert_http_failure(payload, 403, "free_quota")
        self.assertIn("不会自动切换模型或付费调用", message)

    def test_error_body_read_failure_is_closed_and_does_not_invent_json_diagnosis(self):
        provider = new_provider()
        stream = ErrorBody(b"")
        stream.read = Mock(side_effect=OSError(PRIVATE_DETAIL))
        error = HTTPError(PRIVATE_SAMPLE, 403, PRIVATE_DETAIL, {}, stream)
        provider.opener.open.side_effect = error
        with self.assertLogs(level="WARNING") as captured:
            with self.assertRaises(bedtime.ProviderFailure) as caught:
                self.call_json(provider)
        self.assert_safe_record(captured, "enroll_create", "unknown", 403)
        self.assert_safe_exception(caught.exception)
        self.assertTrue(stream.closed)
        stream.read.assert_called_once_with(16 * 1024 + 1)
        self.assertEqual(provider.opener.open.call_count, 1)

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
