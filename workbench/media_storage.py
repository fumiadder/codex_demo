"""Private application media, served through the existing authorized API.

The local backend preserves the existing ``uploads`` and ``bedtime-audio``
directories.  The S3 backend is for Neon Object Storage and never produces a
public or presigned URL.  Authorization, quota reservations and audio expiry
remain the responsibility of the caller, outside long database transactions.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat as stat_module
from typing import BinaryIO
from urllib.parse import urlsplit


NAMESPACES = frozenset(("uploads", "bedtime-audio"))
_ID = re.compile(r"[a-f0-9]{32}\Z")
_NEON_STORAGE_HOST = re.compile(
    r"br-[a-z0-9-]+\.storage\.[a-z0-9-]+\."
    r"(?:us-east-1|us-east-2|eu-central-1|ap-southeast-1)\.aws\.neon\.tech\Z"
)


class StorageError(OSError):
    """Storage failed; safe to log without exposing SDK credentials or URLs."""


class MissingMedia(FileNotFoundError):
    """The media object does not exist; unlike a provider or permission error."""


@dataclass(frozen=True)
class MediaInfo:
    size: int
    content_type: str = "application/octet-stream"


class MediaStream:
    """Closeable read result whose body never reads past the requested range."""

    def __init__(self, body: BinaryIO, info: MediaInfo, start: int, end: int):
        self._source = body
        self.total_size = info.size
        self.content_type = info.content_type
        self.start = start
        self.end = end
        self.size = max(0, end - start + 1)
        self._remaining = self.size
        # Existing HTTP handlers can copy .body.read(65536) without buffering.
        self.body = self

    def read(self, size=-1):
        if self._remaining <= 0 or size == 0:
            return b""
        if not isinstance(size, int) or size < -1:
            raise ValueError("Read size must be -1 or a nonnegative integer")
        amount = self._remaining if size == -1 else min(size, self._remaining)
        try:
            value = self._source.read(amount)
        except Exception:
            raise StorageError("Media read failed") from None
        if not isinstance(value, bytes) or not value or len(value) > amount:
            raise StorageError("Media read returned incomplete or invalid content")
        self._remaining -= len(value)
        return value

    def close(self):
        try:
            self._source.close()
        except Exception:
            raise StorageError("Media stream closure failed") from None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _key(namespace, object_id):
    if namespace not in NAMESPACES:
        raise ValueError("Unknown media namespace")
    if not isinstance(object_id, str) or not _ID.fullmatch(object_id):
        raise ValueError("Media ID must be 32 lowercase hexadecimal characters")
    return f"{namespace}/{object_id}"


def _content_type(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("Invalid media content type")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid media content type")
    return value


def _range(size, byte_range):
    if byte_range is None:
        return 0, size - 1
    if not isinstance(byte_range, tuple) or len(byte_range) != 2:
        raise ValueError("Byte range must be a resolved (start, end) tuple")
    start, end = byte_range
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < size:
        raise ValueError("Byte range is outside the media object")
    return start, end


class LocalStorage:
    backend = "local"

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _directory(self, namespace, object_id):
        _key(namespace, object_id)
        directory = self.data_dir / namespace
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            # Disallow symlink directories and files even when their names are
            # valid IDs. Relative operations use this fixed directory handle.
            return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise StorageError("Local media directory is unavailable") from None

    def put(self, namespace, object_id, data, content_type="application/octet-stream"):
        content_type = _content_type(content_type)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("Media data must be bytes")
        data = bytes(data)
        directory = self._directory(namespace, object_id)
        created = False
        try:
            descriptor = os.open(object_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise FileExistsError("Media object already exists") from None
        except Exception:
            if created:
                try:
                    os.unlink(object_id, dir_fd=directory)
                except OSError:
                    pass
            raise StorageError("Local media upload failed") from None
        finally:
            os.close(directory)
        return MediaInfo(len(data), content_type)

    def _handle(self, namespace, object_id):
        directory = self._directory(namespace, object_id)
        try:
            descriptor = os.open(object_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise StorageError("Local media is not a regular file")
            return os.fdopen(descriptor, "rb")
        except FileNotFoundError:
            raise MissingMedia("Media object does not exist") from None
        except OSError:
            raise StorageError("Local media read failed") from None
        finally:
            os.close(directory)

    def stat(self, namespace, object_id):
        with self._handle(namespace, object_id) as handle:
            return MediaInfo(os.fstat(handle.fileno()).st_size)

    def open(self, namespace, object_id, byte_range=None):
        handle = self._handle(namespace, object_id)
        try:
            info = MediaInfo(os.fstat(handle.fileno()).st_size)
            start, end = _range(info.size, byte_range)
            handle.seek(start)
            return MediaStream(handle, info, start, end)
        except Exception:
            handle.close()
            raise

    def delete(self, namespace, object_id):
        directory = self._directory(namespace, object_id)
        try:
            os.unlink(object_id, dir_fd=directory)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageError("Local media deletion failed") from None
        finally:
            os.close(directory)


class S3Storage:
    backend = "s3"

    def __init__(self, bucket=None, *, client, buckets=None):
        mapping = {namespace: (buckets or {}).get(namespace, bucket) for namespace in NAMESPACES}
        for name in mapping.values():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name):
                raise ValueError("S3 media bucket names must be valid bucket names")
        self.bucket = bucket
        self.buckets = mapping
        self._client = client

    @classmethod
    def from_environment(cls, environ):
        required = ("AWS_REGION", "AWS_ENDPOINT_URL_S3",
                    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        for key in required:
            if not environ.get(key):
                raise ValueError(f"{key} is required for S3 media storage")
        buckets = {
            "uploads": environ.get("AWS_UPLOADS_BUCKET") or environ.get("AWS_STORAGE_BUCKET"),
            "bedtime-audio": environ.get("AWS_BEDTIME_AUDIO_BUCKET") or environ.get("AWS_STORAGE_BUCKET"),
        }
        if not all(buckets.values()):
            raise ValueError("Configure AWS_STORAGE_BUCKET or both AWS_UPLOADS_BUCKET and AWS_BEDTIME_AUDIO_BUCKET")
        endpoint = environ["AWS_ENDPOINT_URL_S3"]
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname or
                    not _NEON_STORAGE_HOST.fullmatch(parsed.hostname) or
                    parsed.username is not None or parsed.password is not None or
                    parsed.port not in (None, 443) or parsed.path not in ("", "/") or
                    parsed.query or parsed.fragment):
                raise ValueError
        except ValueError:
            raise ValueError("AWS_ENDPOINT_URL_S3 must be the HTTPS Neon storage endpoint") from None
        region = environ["AWS_REGION"]
        if region not in ("us-east-1", "us-east-2", "eu-central-1", "ap-southeast-1"):
            raise ValueError("AWS_REGION must be a supported Neon object storage region")
        if f".{region}.aws.neon.tech" not in parsed.hostname:
            raise ValueError("AWS_REGION does not match the Neon storage endpoint")
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise StorageError("S3 media storage requires boto3") from None
        try:
            client = boto3.client(
                "s3", region_name=region, endpoint_url=endpoint.rstrip("/"),
                aws_access_key_id=environ["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=environ["AWS_SECRET_ACCESS_KEY"],
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                              request_checksum_calculation="when_required",
                              response_checksum_validation="when_required",
                              connect_timeout=5, read_timeout=15,
                              retries={"mode": "standard", "total_max_attempts": 2}),
            )
            return cls(environ.get("AWS_STORAGE_BUCKET"), client=client, buckets=buckets)
        except Exception:
            raise StorageError("S3 media client initialization failed") from None

    def _call(self, operation, namespace, **kwargs):
        try:
            return getattr(self._client, operation)(Bucket=self.buckets[namespace], **kwargs)
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
            # AccessDenied, network and provider failures must never become a
            # 404: otherwise the application may silently discard real data.
            if operation in ("head_object", "get_object") and code in ("NoSuchKey", "NotFound", "404"):
                raise MissingMedia("Media object does not exist") from None
            raise StorageError("S3 media operation failed") from None

    def verify(self):
        """Fail closed at startup if either configured private bucket is absent."""
        for namespace in NAMESPACES:
            self._call("head_bucket", namespace)

    def put(self, namespace, object_id, data, content_type="application/octet-stream"):
        key = _key(namespace, object_id)
        content_type = _content_type(content_type)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("Media data must be bytes")
        data = bytes(data)
        # Application-generated IDs are unique and immutable. Neon does not
        # document conditional PutObject, so don't rely on it for exclusivity.
        self._call("put_object", namespace, Key=key, Body=data, ContentLength=len(data),
                   ContentType=content_type, CacheControl="private, no-store")
        return MediaInfo(len(data), content_type)

    @staticmethod
    def _info(response):
        try:
            size = response["ContentLength"]
            if type(size) is not int or size < 0:
                raise ValueError
            return MediaInfo(size, _content_type(response.get("ContentType", "application/octet-stream")))
        except (KeyError, TypeError, ValueError):
            raise StorageError("S3 returned invalid media metadata") from None

    def stat(self, namespace, object_id):
        return self._info(self._call("head_object", namespace, Key=_key(namespace, object_id)))

    def open(self, namespace, object_id, byte_range=None):
        key = _key(namespace, object_id)
        info = self.stat(namespace, object_id)
        start, end = _range(info.size, byte_range)
        kwargs = {"Key": key}
        if byte_range is not None:
            kwargs["Range"] = f"bytes={start}-{end}"
        result = self._call("get_object", namespace, **kwargs)
        body = result.get("Body")
        try:
            expected = max(0, end - start + 1)
            if body is None or not callable(getattr(body, "read", None)) or not callable(getattr(body, "close", None)):
                raise StorageError("S3 returned no media stream")
            if result.get("ContentLength") != expected:
                raise StorageError("S3 returned an invalid media length")
            if byte_range is not None and result.get("ContentRange") != f"bytes {start}-{end}/{info.size}":
                raise StorageError("S3 returned an invalid media range")
            return MediaStream(body, info, start, end)
        except Exception:
            if body is not None and callable(getattr(body, "close", None)):
                body.close()
            raise

    def delete(self, namespace, object_id):
        key = _key(namespace, object_id)
        try:
            self._call("head_object", namespace, Key=key)
        except MissingMedia:
            return False
        self._call("delete_object", namespace, Key=key)
        return True


def create_storage(data_dir, environ=None):
    environ = os.environ if environ is None else environ
    backend = environ.get("MEDIA_STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(data_dir)
    if backend == "s3":
        return S3Storage.from_environment(environ)
    raise ValueError("MEDIA_STORAGE_BACKEND must be local or s3")
