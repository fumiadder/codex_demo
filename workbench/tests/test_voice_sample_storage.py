"""Mock-only tests for the narrowly scoped server-to-provider sample signer."""
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_storage import LocalStorage, S3Storage, StorageError


OBJECT_ID = "a" * 32
OTHER_ID = "b" * 32
ENDPOINT = "https://br-test.storage.c-1.ap-southeast-1.aws.neon.tech"


def sample_url(ttl=300, *, endpoint=ENDPOINT, path=None, query=None):
    values = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": "TEST_PRIVATE_CREDENTIAL",
              "X-Amz-Date": "20261010T000000Z", "X-Amz-Expires": str(ttl),
              "X-Amz-SignedHeaders": "host", "X-Amz-Signature": "c" * 64}
    if query is not None:
        values.update(query)
    return endpoint + (path if path is not None else "/bedtime-private/bedtime-audio/" + OBJECT_ID) + "?" + urlencode(values)


class VoiceSampleStorageTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.meta = SimpleNamespace(endpoint_url=ENDPOINT)
        self.client.head_object.return_value = {"ContentLength": 17, "ContentType": "audio/wav"}
        self.client.generate_presigned_url.return_value = sample_url()
        self.storage = S3Storage(client=self.client, buckets={"uploads": "uploads-private", "bedtime-audio": "bedtime-private"})

    def assert_sanitized_failure(self, operation):
        with self.assertRaises(StorageError) as caught:
            operation()
        self.assertEqual(str(caught.exception), "Temporary voice sample URL preparation failed")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("TEST_PRIVATE_CREDENTIAL", str(caught.exception))
        self.assertNotIn("X-Amz", str(caught.exception))

    def test_existing_bedtime_sample_uses_private_path_style_get_and_maximum_ttl(self):
        result = self.storage.signed_voice_sample_url(OBJECT_ID)
        # URL parameters never appear in assertion diagnostics or test logs.
        self.assertTrue(result == self.client.generate_presigned_url.return_value)
        self.client.head_object.assert_called_once_with(Bucket="bedtime-private", Key="bedtime-audio/" + OBJECT_ID)
        self.client.generate_presigned_url.assert_called_once_with(
            "get_object", Params={"Bucket": "bedtime-private", "Key": "bedtime-audio/" + OBJECT_ID},
            ExpiresIn=300, HttpMethod="GET")
        self.assertEqual([call[0] for call in self.client.mock_calls], ["head_object", "generate_presigned_url"])

    def test_minimum_ttl_is_signed_and_checked(self):
        self.client.generate_presigned_url.return_value = sample_url(1)
        self.assertTrue(self.storage.signed_voice_sample_url(OBJECT_ID, 1) == self.client.generate_presigned_url.return_value)
        self.assertEqual(self.client.generate_presigned_url.call_args.kwargs["ExpiresIn"], 1)

    def test_invalid_ttl_never_calls_sdk(self):
        for ttl in (0, -1, 301, 86400, True, False, 1.0, "300", None):
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID, ttl))
        self.assertEqual(self.client.mock_calls, [])

    def test_invalid_id_never_calls_sdk_and_namespace_is_not_caller_selectable(self):
        for identifier in (None, 1, "A"*32, "a"*31, "a"*33, "../"+OBJECT_ID, "uploads/"+OBJECT_ID, OBJECT_ID+"\n"):
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(identifier))
        self.assertEqual(self.client.mock_calls, [])
        with self.assertRaises(TypeError):
            self.storage.signed_voice_sample_url(OBJECT_ID, namespace="uploads")

    def test_missing_object_is_not_signed_and_error_is_sanitized(self):
        error = RuntimeError("TEST_PRIVATE_CREDENTIAL X-Amz-Signature=TEST_SECRET")
        error.response = {"Error": {"Code": "NoSuchKey"}}
        self.client.head_object.side_effect = error
        self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))
        self.client.generate_presigned_url.assert_not_called()

    def test_head_permission_and_network_errors_are_sanitized(self):
        for code in ("AccessDenied", "ServiceUnavailable"):
            error = RuntimeError("TEST_PRIVATE_CREDENTIAL " + sample_url())
            error.response = {"Error": {"Code": code}}
            self.client.head_object.side_effect = error
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))
        self.client.generate_presigned_url.assert_not_called()

    def test_presigner_error_is_sanitized(self):
        self.client.generate_presigned_url.side_effect = RuntimeError(sample_url())
        self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))

    def test_untrusted_client_endpoint_is_rejected_before_any_sdk_operation(self):
        endpoints = ("http://br-test.storage.c-1.ap-southeast-1.aws.neon.tech", "https://example.com",
                     "https://169.254.169.254", "https://user:password@" + ENDPOINT[8:],
                     ENDPOINT + ":444", ENDPOINT + "/other", ENDPOINT + "?credential=secret", ENDPOINT + "#fragment",
                     ENDPOINT + "\n", ENDPOINT + "\\", None)
        for endpoint in endpoints:
            self.client.meta.endpoint_url = endpoint
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))
        self.assertEqual(self.client.mock_calls, [])

    def test_url_host_protocol_userinfo_fragment_and_path_are_exactly_scoped(self):
        urls = (sample_url(endpoint="http://"+ENDPOINT[8:]), sample_url(endpoint="https://example.com"),
                sample_url(endpoint=ENDPOINT.replace("br-test", "br-other")),
                sample_url(endpoint="https://user:password@" + ENDPOINT[8:]),
                sample_url(endpoint=ENDPOINT+":444"), sample_url()+"#fragment",
                sample_url(path="/uploads-private/uploads/"+OBJECT_ID),
                sample_url(path="/bedtime-private/bedtime-audio/"+OTHER_ID),
                sample_url(path="/bedtime-private/bedtime-audio/%2e%2e/"+OBJECT_ID),
                sample_url(path="/bedtime-private/bedtime-audio%2f"+OBJECT_ID),
                sample_url()+"\n", sample_url()+"\\", None, 123)
        for url in urls:
            self.client.generate_presigned_url.return_value = url
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))

    def test_returned_sigv4_expiry_duplicates_and_signature_fail_closed(self):
        urls = (sample_url(301), sample_url(299), sample_url()+"&X-Amz-Expires=300",
                sample_url(query={"X-Amz-Algorithm": "invalid"}), sample_url(query={"X-Amz-Expires": "0300"}),
                sample_url(query={"X-Amz-SignedHeaders": "host;other"}), sample_url(query={"X-Amz-Signature": ""}),
                sample_url(query={"X-Amz-Signature": "z"*64}), ENDPOINT+"/bedtime-private/bedtime-audio/"+OBJECT_ID)
        for url in urls:
            self.client.generate_presigned_url.return_value = url
            self.assert_sanitized_failure(lambda: self.storage.signed_voice_sample_url(OBJECT_ID))

    def test_shared_bucket_and_default_https_port_keep_namespace_isolation(self):
        storage = S3Storage("shared-private", client=self.client)
        self.client.meta.endpoint_url = ENDPOINT+":443/"
        self.client.generate_presigned_url.return_value = sample_url(endpoint=ENDPOINT+":443", path="/shared-private/bedtime-audio/"+OBJECT_ID)
        self.assertTrue(storage.signed_voice_sample_url(OBJECT_ID) == self.client.generate_presigned_url.return_value)
        self.client.generate_presigned_url.assert_called_once_with(
            "get_object", Params={"Bucket": "shared-private", "Key": "bedtime-audio/"+OBJECT_ID}, ExpiresIn=300, HttpMethod="GET")

    def test_normal_private_upload_read_and_delete_do_not_use_signer(self):
        self.storage.put("uploads", OBJECT_ID, b"private", "text/plain")
        self.client.put_object.assert_called_once_with(Bucket="uploads-private", Key="uploads/"+OBJECT_ID,
            Body=b"private", ContentLength=7, ContentType="text/plain", CacheControl="private, no-store")
        self.client.head_object.return_value = {"ContentLength": 7, "ContentType": "text/plain"}
        self.client.get_object.return_value = {"Body": BytesIO(b"private"), "ContentLength": 7}
        with self.storage.open("uploads", OBJECT_ID) as stream:
            self.assertEqual(stream.read(), b"private")
        self.assertTrue(self.storage.delete("uploads", OBJECT_ID))
        self.client.generate_presigned_url.assert_not_called()
        self.assertEqual(self.client.get_object.call_args.kwargs, {"Bucket": "uploads-private", "Key": "uploads/"+OBJECT_ID})

    def test_local_storage_has_no_signing_capability(self):
        with tempfile.TemporaryDirectory() as data:
            storage = LocalStorage(data)
            self.assertFalse(callable(getattr(storage, "signed_voice_sample_url", None)))
            storage.put("bedtime-audio", OBJECT_ID, b"sample", "audio/wav")
            with storage.open("bedtime-audio", OBJECT_ID) as stream:
                self.assertEqual(stream.read(), b"sample")
            self.assertTrue(storage.delete("bedtime-audio", OBJECT_ID))


if __name__ == "__main__":
    unittest.main()
