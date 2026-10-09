"""Local filesystem tests and fake S3 contract/failure tests, not cloud tests."""
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_storage import LocalStorage, MediaStream, MissingMedia, S3Storage, StorageError, create_storage


MEDIA_ID = "a" * 32
OTHER_ID = "b" * 32


class ProviderError(Exception):
    def __init__(self, code):
        super().__init__("provider error containing sensitive credential")
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.failure = None
        self.last_body = None

    def _before(self, operation, parameters):
        self.calls.append((operation, parameters))
        if self.failure and self.failure[0] == operation:
            raise ProviderError(self.failure[1])

    def put_object(self, **parameters):
        self._before("put_object", parameters)
        self.objects[(parameters["Bucket"], parameters["Key"])] = (parameters["Body"], parameters["ContentType"])
        return {}

    def head_object(self, **parameters):
        self._before("head_object", parameters)
        try:
            value, content_type = self.objects[(parameters["Bucket"], parameters["Key"])]
        except KeyError:
            raise ProviderError("NoSuchKey")
        return {"ContentLength": len(value), "ContentType": content_type}

    def get_object(self, **parameters):
        self._before("get_object", parameters)
        value, content_type = self.objects[(parameters["Bucket"], parameters["Key"])]
        total = len(value)
        result = {"ContentType": content_type}
        if "Range" in parameters:
            start, end = map(int, parameters["Range"].removeprefix("bytes=").split("-"))
            value = value[start:end + 1]
            result["ContentRange"] = f"bytes {start}-{end}/{total}"
        self.last_body = io.BytesIO(value)
        return {**result, "Body": self.last_body, "ContentLength": len(value)}

    def delete_object(self, **parameters):
        self._before("delete_object", parameters)
        self.objects.pop((parameters["Bucket"], parameters["Key"]), None)
        return {}


class LocalMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.store = create_storage(self.data_dir, {})

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_files_remain_readable_and_range_is_bounded(self):
        old_dir = self.data_dir / "uploads"
        old_dir.mkdir()
        (old_dir / MEDIA_ID).write_bytes(b"abcdefgh")
        self.assertEqual(self.store.stat("uploads", MEDIA_ID).size, 8)
        with self.store.open("uploads", MEDIA_ID, (2, 5)) as result:
            self.assertEqual((result.start, result.end, result.size, result.total_size), (2, 5, 4, 8))
            self.assertEqual(result.body.read(3), b"cde")
            self.assertEqual(result.body.read(10000), b"f")
            self.assertEqual(result.body.read(), b"")
        self.assertTrue(result._source.closed)

    def test_upload_is_private_exclusive_and_namespaces_are_separate(self):
        info = self.store.put("uploads", MEDIA_ID, b"photo", "image/png")
        self.assertEqual((info.size, info.content_type), (5, "image/png"))
        self.assertEqual((self.data_dir / "uploads" / MEDIA_ID).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.store.put("uploads", MEDIA_ID, b"overwrite")
        with self.store.open("uploads", MEDIA_ID) as result:
            self.assertEqual(result.read(), b"photo")
        self.store.put("bedtime-audio", MEDIA_ID, b"voice", "audio/wav")
        self.assertTrue(self.store.delete("uploads", MEDIA_ID))
        self.assertFalse(self.store.delete("uploads", MEDIA_ID))
        with self.store.open("bedtime-audio", MEDIA_ID) as result:
            self.assertEqual(result.read(), b"voice")
        with self.assertRaises(MissingMedia):
            self.store.open("uploads", MEDIA_ID)

    def test_rejects_traversal_and_symlink_reads(self):
        for invalid in ("../secret", "", "A" * 32, "a" * 31, MEDIA_ID + "/"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.put("uploads", invalid, b"x")
        with self.assertRaises(ValueError):
            self.store.open("../uploads", MEDIA_ID)
        target = Path(self.temp.name) / "secret"
        target.write_bytes(b"private")
        namespace = self.data_dir / "uploads"
        namespace.mkdir(exist_ok=True)
        (namespace / MEDIA_ID).symlink_to(target)
        with self.assertRaises(StorageError):
            self.store.open("uploads", MEDIA_ID)
        self.assertTrue(self.store.delete("uploads", MEDIA_ID))
        self.assertEqual(target.read_bytes(), b"private")

    def test_rejects_symlink_directory_and_nonregular_file(self):
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.data_dir / "uploads").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.store.put("uploads", MEDIA_ID, b"x")
        (self.data_dir / "uploads").unlink()
        (self.data_dir / "uploads").mkdir()
        (self.data_dir / "uploads" / MEDIA_ID).mkdir()
        with self.assertRaises(StorageError):
            self.store.stat("uploads", MEDIA_ID)

    def test_failed_local_write_removes_partial_object(self):
        with patch("media_storage.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(StorageError):
                self.store.put("uploads", MEDIA_ID, b"partial")
        self.assertFalse((self.data_dir / "uploads" / MEDIA_ID).exists())

    def test_validates_resolved_ranges_and_empty_objects(self):
        self.store.put("uploads", MEDIA_ID, b"abc")
        for invalid in ((0, 3), (-1, 2), (2, 1), (True, 2), "bytes=0-2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.open("uploads", MEDIA_ID, invalid)
        self.store.put("uploads", OTHER_ID, b"")
        with self.store.open("uploads", OTHER_ID) as result:
            self.assertEqual(result.size, 0)
            self.assertEqual(result.read(), b"")


class S3MediaTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3()
        self.store = S3Storage(client=self.client, buckets={"uploads": "uploads", "bedtime-audio": "bedtime-audio"})

    def test_startup_verification_checks_both_buckets_and_does_not_hide_errors(self):
        from unittest.mock import Mock
        client = Mock()
        storage = S3Storage(client=client, buckets={"uploads": "uploads", "bedtime-audio": "bedtime-audio"})
        storage.verify()
        self.assertEqual({call.kwargs["Bucket"] for call in client.head_bucket.call_args_list}, {"uploads", "bedtime-audio"})
        client.head_bucket.side_effect = RuntimeError("controlled credential error")
        with self.assertRaises(StorageError) as caught:
            storage.verify()
        self.assertNotIn("credential", str(caught.exception))

    def test_put_and_range_get_use_private_namespaced_buckets(self):
        self.store.put("uploads", MEDIA_ID, b"abcdefgh", "video/mp4")
        operation, parameters = self.client.calls[-1]
        self.assertEqual(operation, "put_object")
        self.assertEqual(parameters["Bucket"], "uploads")
        self.assertEqual(parameters["Key"], "uploads/" + MEDIA_ID)
        self.assertEqual(parameters["CacheControl"], "private, no-store")
        self.assertNotIn("ACL", parameters)
        self.assertNotIn("IfNoneMatch", parameters)
        with self.store.open("uploads", MEDIA_ID, (2, 4)) as result:
            self.assertEqual((result.total_size, result.size, result.content_type), (8, 3, "video/mp4"))
            self.assertEqual(result.read(2), b"cd")
            self.assertEqual(result.read(65536), b"e")
        self.assertTrue(self.client.last_body.closed)
        self.assertEqual(self.client.calls[-1][1]["Range"], "bytes=2-4")
        self.store.put("bedtime-audio", MEDIA_ID, b"wav", "audio/wav")
        self.assertEqual(self.client.calls[-1][1]["Bucket"], "bedtime-audio")

    def test_single_bucket_fallback_keeps_namespace_isolation(self):
        store = S3Storage("zhixu-private", client=self.client)
        store.put("uploads", MEDIA_ID, b"media")
        store.put("bedtime-audio", MEDIA_ID, b"audio")
        self.assertEqual(len(self.client.objects), 2)
        self.assertEqual({bucket for bucket, key in self.client.objects}, {"zhixu-private"})

    def test_missing_is_distinct_from_permission_and_provider_failures(self):
        with self.assertRaises(MissingMedia):
            self.store.stat("uploads", MEDIA_ID)
        self.assertFalse(self.store.delete("uploads", MEDIA_ID))
        for code in ("AccessDenied", "NoSuchBucket", "ServiceUnavailable", "SignatureDoesNotMatch"):
            self.client.failure = ("head_object", code)
            with self.subTest(code=code), self.assertRaises(StorageError) as raised:
                self.store.delete("uploads", MEDIA_ID)
            self.assertNotIn("credential", str(raised.exception))
            self.assertFalse(any(name == "delete_object" for name, parameters in self.client.calls))

    def test_upload_and_delete_errors_do_not_report_success(self):
        self.client.failure = ("put_object", "ServiceUnavailable")
        with self.assertRaises(StorageError):
            self.store.put("uploads", MEDIA_ID, b"data")
        self.assertEqual(self.client.objects, {})
        self.client.failure = None
        self.store.put("uploads", MEDIA_ID, b"data")
        self.client.failure = ("delete_object", "AccessDenied")
        with self.assertRaises(StorageError):
            self.store.delete("uploads", MEDIA_ID)
        self.assertEqual(len(self.client.objects), 1)

    def test_invalid_range_response_closes_stream_and_fails(self):
        self.store.put("uploads", MEDIA_ID, b"abcdef")
        original = self.client.get_object
        def missing_range(**parameters):
            response = original(**parameters)
            del response["ContentRange"]
            return response
        self.client.get_object = missing_range
        with self.assertRaises(StorageError):
            self.store.open("uploads", MEDIA_ID, (1, 2))
        self.assertTrue(self.client.last_body.closed)

    def test_provider_truncation_fails_during_stream_read(self):
        stream = MediaStream(io.BytesIO(b"ab"), self.store._info({"ContentLength": 4}), 0, 3)
        with stream:
            self.assertEqual(stream.read(4), b"ab")
            with self.assertRaises(StorageError):
                stream.read(4)

    def test_mime_header_injection_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put("uploads", MEDIA_ID, b"data", "text/plain\r\nX-Evil: yes")
        self.assertEqual(self.client.calls, [])

    def test_configuration_rejects_untrusted_endpoints_without_loading_sdk(self):
        base = {"AWS_REGION": "ap-southeast-1", "AWS_STORAGE_BUCKET": "zhixu-private",
                "AWS_ACCESS_KEY_ID": "test-key", "AWS_SECRET_ACCESS_KEY": "test-secret"}
        invalid = ("http://br-test.storage.c-1.ap-southeast-1.aws.neon.tech",
                   "https://localhost", "https://169.254.169.254", "https://example.com",
                   "https://br-test.storage.c-1.ap-southeast-1.aws.neon.tech.evil.example",
                   "https://user:password@br-test.storage.c-1.ap-southeast-1.aws.neon.tech",
                   "https://br-test.storage.c-1.ap-southeast-1.aws.neon.tech/?secret=foo")
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError) as raised:
                create_storage("unused", {**base, "MEDIA_STORAGE_BACKEND": "s3", "AWS_ENDPOINT_URL_S3": endpoint})
            self.assertNotIn("test-secret", str(raised.exception))

    def test_configuration_requires_both_buckets_or_shared_fallback(self):
        with self.assertRaisesRegex(ValueError, "AWS_STORAGE_BUCKET"):
            S3Storage.from_environment({"AWS_REGION": "ap-southeast-1", "AWS_ENDPOINT_URL_S3": "https://unused",
                                        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                                        "AWS_UPLOADS_BUCKET": "uploads"})

    def test_environment_factory_uses_explicit_credentials_and_neon_s3_settings(self):
        captured = {}
        def client(service, **parameters):
            captured["service"] = service
            captured.update(parameters)
            return self.client
        modules = {"boto3": SimpleNamespace(client=client),
                   "botocore": SimpleNamespace(),
                   "botocore.config": SimpleNamespace(Config=lambda **parameters: parameters)}
        environ = {"MEDIA_STORAGE_BACKEND": "s3", "AWS_REGION": "ap-southeast-1",
                   "AWS_ENDPOINT_URL_S3": "https://br-test.storage.c-1.ap-southeast-1.aws.neon.tech/",
                   "AWS_ACCESS_KEY_ID": "test-access", "AWS_SECRET_ACCESS_KEY": "test-secret",
                   "AWS_UPLOADS_BUCKET": "uploads", "AWS_BEDTIME_AUDIO_BUCKET": "bedtime-audio"}
        with patch.dict(sys.modules, modules):
            store = create_storage("unused", environ)
        self.assertEqual(captured["service"], "s3")
        self.assertEqual(captured["config"]["signature_version"], "s3v4")
        self.assertEqual(captured["config"]["s3"], {"addressing_style": "path"})
        self.assertEqual(captured["config"]["retries"]["total_max_attempts"], 2)
        self.assertEqual(captured["config"]["connect_timeout"], 5)
        self.assertEqual(captured["config"]["read_timeout"], 15)
        self.assertEqual(captured["config"]["request_checksum_calculation"], "when_required")
        self.assertEqual(captured["config"]["response_checksum_validation"], "when_required")
        self.assertEqual(captured["aws_access_key_id"], "test-access")
        self.assertEqual(captured["aws_secret_access_key"], "test-secret")
        self.assertFalse(captured["endpoint_url"].endswith("/"))
        self.assertEqual(store.buckets, {"uploads": "uploads", "bedtime-audio": "bedtime-audio"})
        self.assertEqual(self.client.calls, [])  # no bucket creation or network requests


if __name__ == "__main__":
    unittest.main()
