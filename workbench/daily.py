"""Private "today" image logs with server-side DashScope vision recognition.

The module owns its tables, routes and provider client, but reuses the central
account session, space membership checks, CSRF validation, quota accounting and
the private media pipeline. Provider keys stay on the server; without a key the
capture, upload and manual summary flows keep working and every AI surface is
explicitly marked as unavailable, never simulated.

Uses only the standard library besides Pillow, which downscales private
originals into in-memory recognition copies. Originals are never replaced or
deleted to produce a model request.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import ssl
import threading
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

import storage_jobs
from media_storage import MissingMedia, StorageError

ID = r"[a-f0-9]{32}"
ROUTE = re.compile(rf"^/api/spaces/({ID})/daily(?:/(.*))?$")
ENTRY_ID_RE = re.compile(rf"^({ID})$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SHANGHAI_TZ = timezone(timedelta(hours=8))

VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_VISION_MODEL = "qwen3.8-flash"
VISION_MODELS = frozenset(("qwen3.8-flash", "qwen3.7-plus", "qwen3.8-max"))
IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGES_PER_ENTRY = 9
ENTRY_LIST_LIMIT = 500
MAX_TITLE = 200
MAX_SUMMARY = 1000
MAX_BODY = 100_000
MAX_DIGEST_CHARS = 600
DIGEST_TARGET_CHARS = 300
STAGING_TTL = 3600
RECOGNIZE_LONG_EDGE = 1600
RECOGNIZE_FALLBACK_EDGE = 1280
RECOGNIZE_BUDGET = 6 * 1024 * 1024
MIN_SIDE = 16
# Reject decompression bombs at inspection time. Pillow is optional, mirroring
# the existing in-function import style; when present, cap decodable pixels.
MAX_IMAGE_PIXELS = 64_000_000
try:
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover - Pillow remains an optional dependency
    _PILImage = None
else:
    _PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_entries (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 area TEXT NOT NULL CHECK(area IN ('work','life')),
 entry_date TEXT NOT NULL,
 title TEXT NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','archived')),
 meta TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_entries_space_date ON daily_entries(space_id,entry_date,created_at);
CREATE TABLE IF NOT EXISTS daily_images (
 id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES daily_entries(id) ON DELETE CASCADE,
 space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
 width INTEGER NOT NULL CHECK(width>0), height INTEGER NOT NULL CHECK(height>0),
 sort_order INTEGER NOT NULL CHECK(sort_order>=0), created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_images_entry ON daily_images(entry_id,sort_order);
CREATE TABLE IF NOT EXISTS daily_uploads (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 created_by TEXT NOT NULL REFERENCES users(id),
 name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
 width INTEGER NOT NULL CHECK(width>0), height INTEGER NOT NULL CHECK(height>0),
 created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_uploads_owner ON daily_uploads(space_id,created_by,created_at);
CREATE TABLE IF NOT EXISTS daily_digests (
 space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 entry_date TEXT NOT NULL, digest TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(space_id,entry_date)
);
"""

RECOGNIZE_SYSTEM = (
    "你是中文个人工作台的图片记录助手。用户用图片快速记录今天做了什么，图片可能是白板、便签、票据、"
    "电脑或手机屏幕、文档、会议现场或日常场景。你只输出一个 JSON 对象，不要 Markdown、代码块、注释或多余文字。"
    "字段固定为：title（不超过 20 个字的记录标题，概括这组图片反映的事情）、"
    "summary（80 至 200 字中文摘要，按看到的事实描述做了什么、关键信息与进展，不臆测图片里没有的内容）、"
    "tags（1 至 3 个简短中文标签，每个不超过 8 个字，不带井号）、"
    "suggestions（0 至 5 条可以转成待办的具体行动项，每条不超过 30 个字，没有明确行动项时返回空数组）。"
    "不输出姓名、手机号、证件号、银行卡号等个人敏感信息；无法看清的内容不要编造。"
)
DIGEST_SYSTEM = (
    "你是中文个人工作台的每日小结助手。根据用户当天多条图片记录的标题、摘要和附言，"
    f"写一段不超过 {DIGEST_TARGET_CHARS} 字的中文小结，平实概括今天完成的事、进展和待跟进点。"
    "只输出小结正文，不要标题、Markdown、列表符号或多余文字；不要编造记录里没有的事情。"
)
_FAILURE_REASONS = frozenset((
    "auth_failed", "rate_limited", "model_denied", "provider_unavailable",
    "network_or_timeout", "invalid_response", "image_processing", "unknown",
))


class ProviderFailure(Exception):
    """An intentionally sanitized error with no provider URL, key or body."""


def log_vision_failure(operation, reason, http_status=0):
    """Log fixed diagnostic labels only; never vendor bodies or credentials."""
    if operation not in ("recognize", "digest"):
        return
    if reason not in _FAILURE_REASONS:
        reason = "unknown"
    if type(http_status) is not int or not 100 <= http_status <= 599:
        http_status = 0
    logging.warning("vision_failure operation=%s http_status=%d reason=%s", operation, http_status, reason)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_str():
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")


def clean_text(value, limit):
    """Strip control characters and bound length; never return None."""
    if not isinstance(value, str):
        return ""
    value = CONTROL_CHARS.sub("", value).strip()
    return value[:limit]


def valid_date(value):
    match = DATE_RE.fullmatch(value or "")
    if not match:
        return False
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31 or not 2000 <= year <= 2100:
        return False
    try:
        datetime(year, month, day, tzinfo=SHANGHAI_TZ)
    except ValueError:
        return False
    return True


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderFailure("提供商返回了不支持的重定向")


class VisionProvider:
    """Fixed-endpoint DashScope vision calls; configuration never reaches browsers."""

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.key = env.get("DASHSCOPE_API_KEY", "").strip()
        self.allowed = env.get("VISION_ENABLED", "1").strip() == "1"
        model = env.get("VISION_MODEL", DEFAULT_VISION_MODEL).strip()
        # An unsupported setting disables the feature; never silently call a
        # different, possibly billable model.
        self.model = model if model in VISION_MODELS else ""
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    @property
    def enabled(self):
        return bool(self.key and self.allowed and self.model)

    def _post(self, body, operation):
        request = Request(VISION_URL, json.dumps(body, ensure_ascii=False).encode(), {
            "Authorization": "Bearer " + self.key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, method="POST")
        try:
            with self.opener.open(request, timeout=60) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ProviderFailure("提供商响应超过大小限制")
                result = json.loads(data)
                if not isinstance(result, dict) or result.get("code"):
                    raise ProviderFailure("提供商未能完成请求，请稍后重试")
                return result
        except ProviderFailure:
            log_vision_failure(operation, "invalid_response")
            raise
        except HTTPError as exc:
            vendor_code = None
            try:
                error_data = exc.read(16 * 1024 + 1)
                if len(error_data) <= 16 * 1024:
                    error_result = json.loads(error_data)
                    if isinstance(error_result, dict):
                        vendor_code = error_result.get("code")
                        nested = error_result.get("error")
                        if (not isinstance(vendor_code, str) or not vendor_code) and isinstance(nested, dict):
                            vendor_code = nested.get("code")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError):
                pass
            finally:
                exc.close()
            if exc.code == 401:
                reason = "auth_failed"
            elif exc.code == 429:
                reason = "rate_limited"
            elif exc.code == 403:
                reason = "model_denied"
            elif exc.code in (500, 502, 503, 504):
                reason = "provider_unavailable"
            else:
                reason = "unknown"
            log_vision_failure(operation, reason, exc.code)
            if exc.code == 401:
                raise ProviderFailure("百炼认证失败，请管理员核对服务端密钥") from None
            if exc.code == 429:
                raise ProviderFailure("图片识别调用过于频繁，请稍后再试") from None
            if exc.code == 403:
                raise ProviderFailure("百炼拒绝了本次调用，请核对视觉模型权限与免费额度设置") from None
            if reason == "provider_unavailable":
                raise ProviderFailure("视觉模型暂时不可用，请稍后重试") from None
            raise ProviderFailure("视觉服务暂时不可用，请检查服务端配置或稍后重试") from None
        except (URLError, TimeoutError, OSError):
            log_vision_failure(operation, "network_or_timeout")
            raise ProviderFailure("无法连接视觉模型服务，请检查服务器网络后重试") from None
        except (ValueError, RecursionError):
            log_vision_failure(operation, "invalid_response")
            raise ProviderFailure("视觉模型返回了无法解析的内容，请稍后重试") from None

    def _completion(self, messages, operation):
        request = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"} if operation == "recognize" else None,
        }
        if self.model == "qwen3.8-flash":
            request.update(enable_thinking=False, max_completion_tokens=1600)
        else:
            request["max_tokens"] = 1600
        request = {key: value for key, value in request.items() if value is not None}
        result = self._post(request, operation)
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderFailure("提供商没有返回完整结果")
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if choice.get("finish_reason") != "stop" or not isinstance(content, str) or len(content) > 12_000:
            raise ProviderFailure("视觉识别未完成，请稍后重试")
        return content

    def recognize(self, data_uris, note):
        if not self.enabled:
            raise ProviderFailure("图片识别尚未配置")
        if not data_uris or len(data_uris) > MAX_IMAGES_PER_ENTRY:
            raise ProviderFailure("图片数量超出限制")
        content = [{"type": "text", "text": "这是今天的一组记录图片。" + (f"用户附言：{note}" if note else "")}]
        for uri in data_uris:
            content.append({"type": "image_url", "image_url": {"url": uri}})
        raw = self._completion(
            [{"role": "system", "content": RECOGNIZE_SYSTEM},
             {"role": "user", "content": content}],
            "recognize",
        )
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            raise ProviderFailure("识别结果格式无效，请重试") from None
        return validate_recognition(value)

    def digest(self, day_text):
        if not self.enabled:
            raise ProviderFailure("每日小结尚未配置")
        raw = self._completion(
            [{"role": "system", "content": DIGEST_SYSTEM},
             {"role": "user", "content": day_text[:4000]}],
            "digest",
        )
        digest = clean_text(raw, MAX_DIGEST_CHARS)
        if not digest:
            raise ProviderFailure("每日小结没有返回有效内容，请重试")
        return digest


def validate_recognition(value):
    """Validate and bound every model field before it can reach storage."""
    if not isinstance(value, dict):
        raise ProviderFailure("识别结果格式无效，请重试")
    title = clean_text(value.get("title"), 20)
    summary = clean_text(value.get("summary"), 400)
    if not title or not summary:
        raise ProviderFailure("识别结果缺少标题或摘要，请重试")

    def bounded_list(field, count, length):
        raw = value.get(field)
        if not isinstance(raw, list):
            return []
        items, seen = [], set()
        for item in raw[:count]:
            text = clean_text(item, length)
            if text and text not in seen:
                seen.add(text)
                items.append(text)
        return items

    return {
        "title": title,
        "summary": summary,
        "tags": bounded_list("tags", 3, 8),
        "suggestions": bounded_list("suggestions", 5, 30),
    }


def image_mime(payload):
    """Detect the real image type from magic bytes, never from the filename."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def inspect_image(payload):
    """Confirm the bytes are a decodable image and read its real dimensions."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        log_vision_failure("recognize", "image_processing")
        raise ProviderFailure("服务器缺少图片处理组件，请联系管理员") from None
    try:
        with warnings.catch_warnings():
            # Above MAX_IMAGE_PIXELS Pillow only emits a one-time warning and
            # hard-fails at twice the limit. Promote the warning so the first
            # oversized frame is rejected without allocating its pixels.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("图片像素超出限制，请压缩后再上传") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ValueError("图片内容无法识别，请转换为 JPEG、PNG 或 WebP 后重试") from None
    if width < MIN_SIDE or height < MIN_SIDE:
        raise ValueError("图片尺寸过小，请更换更清晰的图片")
    return width, height


def _downscale(payload, long_edge, quality):
    from PIL import Image, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                image.seek(0)
                if image.mode in ("RGBA", "LA", "P"):
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                width, height = image.size
                scale = min(1.0, long_edge / max(width, height))
                if scale < 1.0:
                    image = image.resize(
                        (max(1, round(width * scale)), max(1, round(height * scale))),
                        Image.LANCZOS,
                    )
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                return buffer.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        log_vision_failure("recognize", "image_processing")
        raise ProviderFailure("图片像素超出限制，请压缩后再试") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        log_vision_failure("recognize", "image_processing")
        raise ProviderFailure("图片处理失败，请更换图片后重试") from None


def recognition_copies(images):
    """Build bounded base64 JPEG copies; originals stay in private storage."""
    copies = []
    for payload in images:
        copies.append(_downscale(payload, RECOGNIZE_LONG_EDGE, 85))
    if sum(len(item) for item in copies) > RECOGNIZE_BUDGET:
        copies = [_downscale(payload, RECOGNIZE_FALLBACK_EDGE, 78) for payload in images]
    if sum(len(item) for item in copies) > RECOGNIZE_BUDGET:
        raise ProviderFailure("图片总体过大，请减少图片数量后重试")
    return ["data:image/jpeg;base64," + base64.b64encode(item).decode("ascii") for item in copies]


def initialize(server, provider=None):
    """Called once after the application's base schema exists."""
    import sys
    server.daily_error = getattr(sys.modules[type(server).__module__], "APIError")
    server.daily_provider = provider if provider is not None else VisionProvider()
    server.daily_slots = threading.BoundedSemaphore(2)
    server.daily_cleanup_lock = threading.Lock()
    server.last_daily_cleanup = 0.0
    with server.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(SCHEMA)
    cleanup_staging(server)


def fail(handler, status, message):
    raise handler.server.daily_error(status, message)


def provider_call(handler, function, *args):
    if not handler.server.daily_slots.acquire(blocking=False):
        fail(handler, 429, "视觉服务繁忙，请稍后重试")
    try:
        return function(*args)
    except ProviderFailure as exc:
        raise handler.server.daily_error(502, str(exc)) from None
    finally:
        handler.server.daily_slots.release()


def maybe_cleanup_staging(server):
    """Drive staging reclamation from authenticated traffic, no polling worker."""
    if not server.daily_cleanup_lock.acquire(blocking=False):
        return
    try:
        if time.monotonic() - server.last_daily_cleanup >= 60:
            server.last_daily_cleanup = time.monotonic()
            cleanup_staging(server)
    finally:
        server.daily_cleanup_lock.release()


def cleanup_staging(server):
    """Delete unpublished staged images past their TTL and queue object removal."""
    cutoff = int(time.time()) - STAGING_TTL
    try:
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM daily_uploads WHERE created_at<?", (cutoff,)
            ).fetchall()
            for row in rows:
                storage_jobs.queue_delete(conn, "uploads", row["id"], row["space_id"], row["size"])
                conn.execute("DELETE FROM daily_uploads WHERE id=?", (row["id"],))
        if rows:
            server.cleanup_media()
    except Exception:
        logging.warning("Daily staging cleanup deferred")


def entry_json(row, images=None, digest=None):
    result = {
        "id": row["id"],
        "spaceId": row["space_id"],
        "area": row["area"],
        "entryDate": row["entry_date"],
        "title": row["title"],
        "summary": row["summary"],
        "body": row["body"],
        "status": row["status"],
        "meta": json.loads(row["meta"]),
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "images": images or [],
    }
    if digest is not None:
        result["digest"] = digest
    return result


def image_json(row):
    return {
        "id": row["id"],
        "entryId": row["entry_id"],
        "name": row["name"],
        "mime": row["mime"],
        "size": row["size"],
        "width": row["width"],
        "height": row["height"],
        "sortOrder": row["sort_order"],
        "url": "/api/spaces/" + row["space_id"] + "/daily/images/" + row["id"],
        "createdAt": row["created_at"],
    }


def read_upload(handler):
    """Parse the single-image multipart body using the shared upload permit."""
    if not handler.server.upload_slots.acquire(blocking=False):
        fail(handler, 429, "上传任务较多，请稍后重试")
    handler._upload_slot = True
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data;") or "\r" in content_type or "\n" in content_type:
        fail(handler, 415, "请选择图片上传")
    body = handler.read_body(25 * 1024 * 1024 + 65_536)
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
    )
    if not message.is_multipart() or message.defects:
        fail(handler, 400, "上传格式无效")
    files = []
    for part in message.iter_parts():
        if part.is_multipart() or part.defects or part.get_content_disposition() != "form-data":
            fail(handler, 400, "上传格式无效")
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if payload is None:
            fail(handler, 400, "上传内容无效")
        filename = part.get_filename()
        if filename is not None:
            if name != "file":
                fail(handler, 400, "文件字段必须为 file")
            files.append((filename, payload))
    if len(files) != 1:
        fail(handler, 400, "每次请选择一张图片")
    filename, payload = files[0]
    if not payload or len(payload) > 25 * 1024 * 1024:
        fail(handler, 413, "图片须为 1 字节至 25 MB")
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = handler.string(filename, "文件名称", 255, True)
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        fail(handler, 400, "文件名称无效")
    mime = image_mime(payload)
    if mime is None:
        fail(handler, 400, "仅支持 JPEG、PNG、GIF 或 WebP 图片")
    try:
        width, height = inspect_image(payload)
    except ValueError as exc:
        fail(handler, 400, str(exc))
    return filename, mime, payload, width, height


def dispatch(handler, path, query):
    """Return False for other modules; own auth, body read and transactions."""
    match = ROUTE.fullmatch(path)
    if not match:
        return False
    sid, route = match[1], match[2] or ""
    method, server = handler.command, handler.server
    write = method not in ("GET", "HEAD")
    with server.db() as conn:
        user = handler.current_user(conn)
        handler.membership(conn, sid, user["id"], write=write)
    maybe_cleanup_staging(server)
    if method == "HEAD":
        fail(handler, 405, "不支持此请求方法")
    if route == "uploads" and method == "POST":
        # Per-account upload throttle, enforced before reading the large body.
        server.throttle(("daily-upload-hour", user["id"]), 60, 3600)
    data = None
    if method in ("POST", "PATCH", "PUT"):
        if route == "uploads" and method == "POST":
            parsed_upload = read_upload(handler)
        else:
            data = handler.read_json()
    elif write:
        handler.read_body(1024 * 1024)
    with server.db() as conn:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        user = handler.current_user(conn)
        uid = user["id"]
        handler.membership(conn, sid, uid, write=write)
        provider = server.daily_provider

        if route == "config" and method == "GET":
            result = {
                "vision": {
                    "enabled": bool(getattr(provider, "enabled", False)),
                    "provider": "dashscope",
                    "model": getattr(provider, "model", "") if getattr(provider, "enabled", False) else "",
                },
                "maxImages": MAX_IMAGES_PER_ENTRY,
                "maxUploadBytes": 25 * 1024 * 1024,
                "imageMimes": list(IMAGE_MIMES),
                "timeZone": "Asia/Shanghai",
            }
        elif route == "uploads" and method == "POST":
            result = create_upload(handler, conn, sid, uid, parsed_upload)
        elif route == "entries" and method == "GET":
            result = list_entries(handler, conn, sid, query)
        elif route == "entries" and method == "POST":
            result = create_entry(handler, conn, server, sid, uid, data, provider)
        elif route == "digest":
            result = digest_route(handler, conn, server, sid, uid, query, data, method, provider)
        elif recognize_match := re.fullmatch(rf"entries/({ID})/recognize", route):
            if method != "POST":
                fail(handler, 405, "不支持此请求方法")
            result = recognize_entry(handler, conn, server, sid, uid, recognize_match[1], data, provider)
        elif image_match := re.fullmatch(rf"images/({ID})", route):
            if method != "GET":
                fail(handler, 405, "不支持此请求方法")
            row = conn.execute("SELECT * FROM daily_images WHERE id=? AND space_id=?", (image_match[1], sid)).fetchone()
            if not row:
                fail(handler, 404, "图片不存在或无权访问")
            handler.membership(conn, sid, uid)
            conn.commit()
            handler.serve_media("uploads", row, row["mime"], row["name"])
            return True
        elif entry_match := re.fullmatch(rf"entries/({ID})", route):
            result = entry_detail(handler, conn, sid, uid, entry_match[1], method, data)
        else:
            fail(handler, 404, "接口不存在")
    # Only resource creation answers 201; recognition and digest generation
    # are actions on existing records and answer 200.
    response_status = 201 if method == "POST" and route in ("uploads", "entries") else 200
    handler.json_response(response_status, result)
    return True


def create_upload(handler, conn, sid, uid, parsed):
    filename, mime, payload, width, height = parsed
    server = handler.server
    space_bytes, total_bytes = storage_jobs.usage(conn, sid)
    if space_bytes + len(payload) > server.max_space_storage:
        fail(handler, 413, "空间存储容量已满，请先删除不需要的记录")
    if total_bytes + len(payload) > server.max_total_storage:
        fail(handler, 507, "服务器可用存储不足，请联系管理员")
    if server.media_storage.backend == "local" and shutil.disk_usage(server.data_dir).free < len(payload) + 100 * 1024 * 1024:
        fail(handler, 507, "服务器可用存储不足，请联系管理员")
    image_id = uuid.uuid4().hex
    storage_jobs.reserve(conn, "uploads", image_id, sid, len(payload))
    conn.commit()
    try:
        server.media_storage.put("uploads", image_id, payload, mime)
        with server.db() as check:
            check.execute("BEGIN IMMEDIATE")
            handler.current_user(check)
            handler.membership(check, sid, uid, write=True)
            storage_jobs.assert_pending(check, "uploads", image_id)
            check.execute(
                "INSERT INTO daily_uploads VALUES(?,?,?,?,?,?,?,?,?)",
                (image_id, sid, uid, filename, mime, len(payload), width, height, int(time.time())),
            )
            storage_jobs.publish(check, "uploads", image_id)
        return {"upload": {"id": image_id, "name": filename, "mime": mime,
                           "size": len(payload), "width": width, "height": height}}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        storage_jobs.abandon(server, "uploads", image_id, sid, len(payload))
        raise


def list_entries(handler, conn, sid, query):
    date = handler.string(query.get("date", [""])[0], "日期", 10)
    month = handler.string(query.get("month", [""])[0], "月份", 7)
    if date and not valid_date(date):
        fail(handler, 400, "日期格式无效")
    if month and not MONTH_RE.fullmatch(month):
        fail(handler, 400, "月份格式无效")
    if date:
        rows = conn.execute(
            "SELECT * FROM daily_entries WHERE space_id=? AND entry_date=? "
            "ORDER BY created_at DESC,id DESC LIMIT ?",
            (sid, date, ENTRY_LIST_LIMIT),
        ).fetchall()
    else:
        if not month:
            month = today_str()[:7]
        rows = conn.execute(
            "SELECT * FROM daily_entries WHERE space_id=? AND entry_date LIKE ? "
            "ORDER BY entry_date DESC,created_at DESC,id DESC LIMIT ?",
            (sid, month + "-%", ENTRY_LIST_LIMIT),
        ).fetchall()
    image_rows = conn.execute(
        "SELECT * FROM daily_images WHERE space_id=? ORDER BY entry_id,sort_order,id", (sid,)
    ).fetchall() if rows else []
    images_by_entry = {}
    for image in image_rows:
        images_by_entry.setdefault(image["entry_id"], []).append(image_json(image))
    digest_rows = {
        row["entry_date"]: row["digest"]
        for row in conn.execute(
            "SELECT entry_date,digest FROM daily_digests WHERE space_id=? AND entry_date LIKE ?",
            (sid, (date or month or today_str()[:7]) + ("" if date else "-%")),
        ).fetchall()
    }
    entries = [entry_json(row, images_by_entry.get(row["id"], [])) for row in rows]
    return {"entries": entries, "digests": digest_rows, "month": month or (date[:7] if date else today_str()[:7])}


def create_entry(handler, conn, server, sid, uid, data, provider):
    if not isinstance(data, dict):
        fail(handler, 400, "请求内容必须为对象")
    area = data.get("area", "work")
    if area not in ("work", "life"):
        fail(handler, 400, "请选择工作或生活分类")
    body = handler.string(data.get("body", ""), "附言", MAX_BODY)
    title = handler.string(data.get("title", ""), "标题", MAX_TITLE)
    summary = handler.string(data.get("summary", ""), "摘要", MAX_SUMMARY)
    raw_ids = data.get("imageIds", [])
    if not isinstance(raw_ids, list) or len(raw_ids) > MAX_IMAGES_PER_ENTRY:
        fail(handler, 400, f"每条记录最多 {MAX_IMAGES_PER_ENTRY} 张图片")
    image_ids, seen = [], set()
    for value in raw_ids:
        if not isinstance(value, str) or not ENTRY_ID_RE.fullmatch(value) or value in seen:
            fail(handler, 400, "图片标识无效")
        seen.add(value)
        image_ids.append(value)
    if not image_ids and not body:
        fail(handler, 400, "请至少添加一张图片或一段文字")
    staged = []
    if image_ids:
        placeholders = ",".join("?" for _ in image_ids)
        staged = conn.execute(
            f"SELECT * FROM daily_uploads WHERE space_id=? AND created_by=? AND id IN ({placeholders})",
            (sid, uid, *image_ids),
        ).fetchall()
        if len(staged) != len(image_ids):
            fail(handler, 400, "图片已过期或不属于当前账号，请重新上传")
    count = conn.execute("SELECT count(*) FROM daily_entries WHERE space_id=?", (sid,)).fetchone()[0]
    if count >= ENTRY_LIST_LIMIT:
        fail(handler, 409, f"每个空间最多保存 {ENTRY_LIST_LIMIT} 条今日记录，请先整理已有内容")
    entry_id, now, entry_date = uuid.uuid4().hex, stamp(), today_str()
    ai_status = "pending" if getattr(provider, "enabled", False) and image_ids else "disabled"
    meta = {"ai": {"status": ai_status, "model": "", "title": "", "summary": "",
                   "tags": [], "suggestions": [], "recognizedAt": "", "applied": False},
            "convertedSuggestions": [], "source": "daily"}
    if not title:
        title = (body.split("\n")[0][:20] if body else "今日记录")
    conn.execute(
        "INSERT INTO daily_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, sid, area, entry_date, title, summary, body, "active",
         json.dumps(meta, ensure_ascii=False, separators=(",", ":")), uid, now, now),
    )
    for order, row in enumerate(staged):
        conn.execute(
            "INSERT INTO daily_images (id,entry_id,space_id,name,mime,size,width,height,sort_order,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["id"], entry_id, sid, row["name"], row["mime"], row["size"],
             row["width"], row["height"], order, now),
        )
        conn.execute("DELETE FROM daily_uploads WHERE id=?", (row["id"],))
    images = conn.execute("SELECT * FROM daily_images WHERE entry_id=? ORDER BY sort_order,id", (entry_id,)).fetchall()
    return {"entry": entry_json(conn.execute("SELECT * FROM daily_entries WHERE id=?", (entry_id,)).fetchone(),
                                 [image_json(row) for row in images])}


def load_entry(handler, conn, sid, entry_id, write=False):
    row = conn.execute("SELECT * FROM daily_entries WHERE id=? AND space_id=?", (entry_id, sid)).fetchone()
    if not row:
        fail(handler, 404, "记录不存在或无权访问")
    handler.membership(conn, sid, handler.current_user(conn)["id"], write=write)
    return row


def entry_detail(handler, conn, sid, uid, entry_id, method, data):
    row = load_entry(handler, conn, sid, entry_id, write=method in ("PATCH", "DELETE"))
    if method == "GET":
        images = conn.execute("SELECT * FROM daily_images WHERE entry_id=? ORDER BY sort_order,id", (entry_id,)).fetchall()
        digest = conn.execute("SELECT digest FROM daily_digests WHERE space_id=? AND entry_date=?",
                              (sid, row["entry_date"])).fetchone()
        return {"entry": entry_json(row, [image_json(item) for item in images],
                                    digest["digest"] if digest else None)}
    if method == "PATCH":
        return patch_entry(handler, conn, sid, uid, row, data)
    if method == "DELETE":
        for image in conn.execute("SELECT * FROM daily_images WHERE entry_id=?", (entry_id,)).fetchall():
            storage_jobs.queue_delete(conn, "uploads", image["id"], sid, image["size"])
        conn.execute("DELETE FROM daily_entries WHERE id=?", (entry_id,))
        conn.commit()
        handler.server.cleanup_media()
        return {"ok": True}
    fail(handler, 405, "不支持此请求方法")


def patch_entry(handler, conn, sid, uid, row, data):
    if not isinstance(data, dict):
        fail(handler, 400, "请求内容必须为对象")
    meta = json.loads(row["meta"])
    title, summary, body, area, status = row["title"], row["summary"], row["body"], row["area"], row["status"]
    manual = False
    if "area" in data:
        area = data.get("area")
        if area not in ("work", "life"):
            fail(handler, 400, "请选择工作或生活分类")
    if "status" in data:
        status = data.get("status")
        if status not in ("active", "archived"):
            fail(handler, 400, "状态无效")
    if "title" in data:
        title = handler.string(data.get("title", ""), "标题", MAX_TITLE, True)
        manual = True
    if "summary" in data:
        summary = handler.string(data.get("summary", ""), "摘要", MAX_SUMMARY)
        manual = True
    if "body" in data:
        body = handler.string(data.get("body", ""), "附言", MAX_BODY)
        manual = True
    if data.get("applyAi") is True:
        ai = meta.get("ai") if isinstance(meta.get("ai"), dict) else None
        if not ai or ai.get("status") != "done":
            fail(handler, 400, "还没有可用的识别结果")
        title, summary = ai.get("title") or title, ai.get("summary") or summary
        ai["applied"] = True
    converted = data.get("convertedSuggestions")
    if converted is not None:
        if not isinstance(converted, list) or len(converted) > 5:
            fail(handler, 400, "已转换待办的数据无效")
        bounded = []
        for item in converted:
            text = clean_text(item, 30)
            if text and text not in bounded:
                bounded.append(text)
        meta["convertedSuggestions"] = bounded
    if manual:
        meta.setdefault("ai", {})["applied"] = True
    conn.execute(
        "UPDATE daily_entries SET area=?,title=?,summary=?,body=?,status=?,meta=?,updated_at=? WHERE id=?",
        (area, title, summary, body, status,
         json.dumps(meta, ensure_ascii=False, separators=(",", ":")), stamp(), row["id"]),
    )
    images = conn.execute("SELECT * FROM daily_images WHERE entry_id=? ORDER BY sort_order,id", (row["id"],)).fetchall()
    return {"entry": entry_json(conn.execute("SELECT * FROM daily_entries WHERE id=?", (row["id"],)).fetchone(),
                                 [image_json(item) for item in images])}


def recognize_entry(handler, conn, server, sid, uid, entry_id, data, provider):
    row = load_entry(handler, conn, sid, entry_id, write=True)
    if not getattr(provider, "enabled", False):
        fail(handler, 503, "图片识别尚未连接，请联系管理员配置服务端百炼密钥；可先手动填写摘要")
    server.throttle(("daily-recognize-hour", uid), 30, 3600)
    server.throttle(("daily-recognize-day", uid), 60, 86400)
    images = conn.execute("SELECT * FROM daily_images WHERE entry_id=? ORDER BY sort_order,id", (entry_id,)).fetchall()
    if not images:
        fail(handler, 400, "这条记录没有图片，无法识别")
    conn.commit()
    payloads = []
    try:
        for image in images:
            with server.media_storage.open("uploads", image["id"]) as stream:
                payloads.append(stream.body.read())
    except MissingMedia:
        fail(handler, 404, "原始图片不存在或已过期")
    except StorageError:
        fail(handler, 503, "媒体存储暂时不可用，请稍后重试")
    uris = recognition_copies(payloads)
    recognized = provider_call(handler, provider.recognize, uris, row["body"])
    with server.db() as check:
        check.execute("BEGIN IMMEDIATE")
        handler.current_user(check)
        handler.membership(check, sid, uid, write=True)
        latest = check.execute("SELECT * FROM daily_entries WHERE id=? AND space_id=?", (entry_id, sid)).fetchone()
        if not latest:
            fail(handler, 404, "记录不存在或无权访问")
        meta = json.loads(latest["meta"])
        now = stamp()
        previous_status = meta.get("ai", {}).get("status", "")
        first_recognition = previous_status != "done"
        ai = {"status": "done", "model": getattr(provider, "model", ""),
              "title": recognized["title"], "summary": recognized["summary"],
              "tags": recognized["tags"], "suggestions": recognized["suggestions"],
              "recognizedAt": now, "applied": bool(meta.get("ai", {}).get("applied"))}
        meta["ai"] = ai
        title, summary = latest["title"], latest["summary"]
        # The first successful recognition fills presentation fields while the
        # user has not edited them. Later recognitions stay in meta until the
        # user explicitly applies them.
        if first_recognition and not ai["applied"]:
            title = recognized["title"]
            if not summary:
                summary = recognized["summary"]
        check.execute("UPDATE daily_entries SET title=?,summary=?,meta=?,updated_at=? WHERE id=?",
                      (title, summary, json.dumps(meta, ensure_ascii=False, separators=(",", ":")), now, entry_id))
        refreshed = check.execute("SELECT * FROM daily_entries WHERE id=?", (entry_id,)).fetchone()
        image_rows = check.execute("SELECT * FROM daily_images WHERE entry_id=? ORDER BY sort_order,id", (entry_id,)).fetchall()
        return {"entry": entry_json(refreshed, [image_json(item) for item in image_rows])}


def digest_route(handler, conn, server, sid, uid, query, data, method, provider):
    if method == "GET":
        date = handler.string(query.get("date", [""])[0], "日期", 10) or today_str()
        if not valid_date(date):
            fail(handler, 400, "日期格式无效")
        row = conn.execute("SELECT * FROM daily_digests WHERE space_id=? AND entry_date=?", (sid, date)).fetchone()
        if not row:
            fail(handler, 404, "今天还没有生成小结")
        return {"digest": {"entryDate": row["entry_date"], "digest": row["digest"],
                           "updatedAt": row["updated_at"]}}
    if method != "POST":
        fail(handler, 405, "不支持此请求方法")
    date = handler.string((data or {}).get("date", today_str()), "日期", 10) or today_str()
    if not valid_date(date):
        fail(handler, 400, "日期格式无效")
    if not getattr(provider, "enabled", False):
        fail(handler, 503, "每日小结尚未连接，请联系管理员配置服务端百炼密钥")
    server.throttle(("daily-digest-day", uid), 20, 86400)
    rows = conn.execute(
        "SELECT title,summary,body FROM daily_entries WHERE space_id=? AND entry_date=? AND status='active' "
        "ORDER BY created_at",
        (sid, date),
    ).fetchall()
    if not rows:
        fail(handler, 400, "这一天还没有记录，先记一笔吧")
    parts = []
    for index, row in enumerate(rows, 1):
        segment = f"{index}. 标题：{row['title']}"
        if row["summary"]:
            segment += f"；摘要：{row['summary']}"
        if row["body"]:
            segment += f"；附言：{row['body']}"
        parts.append(segment)
    day_text = "\n".join(parts)
    conn.commit()
    digest = provider_call(handler, provider.digest, day_text)
    now = stamp()
    with server.db() as check:
        check.execute("BEGIN IMMEDIATE")
        handler.current_user(check)
        handler.membership(check, sid, uid, write=True)
        check.execute(
            "INSERT INTO daily_digests VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(space_id,entry_date) DO UPDATE SET digest=excluded.digest,"
            "created_by=excluded.created_by,updated_at=excluded.updated_at",
            (sid, date, digest, uid, now, now),
        )
        return {"digest": {"entryDate": date, "digest": digest, "updatedAt": now}}
