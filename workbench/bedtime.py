"""Private bedtime stories, HTTP voice enrollment, and voice-independent text.

Uses only the standard library. Provider keys stay on the server. No requests
to a provider occur without the corresponding administrator configuration.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import ssl
import sys
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

SCHEMA = """
CREATE TABLE IF NOT EXISTS bedtime_favorites (
 space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 story_id TEXT NOT NULL, story TEXT NOT NULL, saved_at TEXT NOT NULL,
 PRIMARY KEY(space_id,user_id,story_id)
);
CREATE TABLE IF NOT EXISTS bedtime_history (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 story_id TEXT NOT NULL, story TEXT NOT NULL, voice_id TEXT NOT NULL,
 position_seconds REAL NOT NULL, played_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bedtime_history_owner ON bedtime_history(space_id,user_id,played_at);
CREATE TABLE IF NOT EXISTS bedtime_voices (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','ready','failed')),
 provider_voice TEXT NOT NULL, target_model TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bedtime_voices_owner ON bedtime_voices(space_id,user_id);
CREATE TABLE IF NOT EXISTS bedtime_audio (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 size INTEGER NOT NULL, created_at INTEGER NOT NULL
);
"""

VC_MODEL = "qwen3-tts-vc-2026-01-22"
SYSTEM_MODEL = "qwen3-tts-flash"
ENROLLMENT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
SYNTHESIS_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
MAX_STORY_CHARACTERS = 20_000
MAX_SAMPLE_BYTES = 1_500_000
MAX_VOICE_JSON_BYTES = 2_500_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
AUDIO_TTL = 3600
ID = r"[a-f0-9]{32}"
STORY_ID = r"[a-zA-Z0-9_-]{1,96}"
STORY_ROUTE = re.compile(rf"^/api/spaces/({ID})/bedtime(?:/(.*))?$")
SYSTEM_VOICES = [
    {"id": "cloud:Seren", "name": "小婉 · 助眠女声", "kind": "system"},
    {"id": "cloud:Serena", "name": "苏瑶 · 温柔女声", "kind": "system"},
    {"id": "cloud:Ethan", "name": "晨煦 · 温暖男声", "kind": "system"},
    {"id": "cloud:Arthur", "name": "徐大爷 · 长者声音", "kind": "system"},
    {"id": "cloud:Mochi", "name": "沙小弥 · 轻柔童声", "kind": "system"},
    {"id": "cloud:Cherry", "name": "芊悦 · 亲切女声", "kind": "system"},
]


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProviderFailure(Exception):
    """An intentionally sanitized error, with no provider URL, token, or body."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderFailure("提供商返回了不支持的重定向")


def public_url(value):
    """Validate a source link without fetching it; never request user URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or re.search(r"[\x00-\x20]", value)):
            return ""
    except ValueError:
        return ""
    return value


def result_audio_url(value):
    """Only download audio from the documented DashScope result OSS buckets."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ProviderFailure("提供商返回了无效的音频地址")
    try:
        parsed = urlsplit(value)
        allowed = re.fullmatch(r"dashscope-result-[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com", parsed.hostname or "")
        if (parsed.scheme not in ("https", "http") or not allowed or parsed.username
                or parsed.password or parsed.port is not None or not parsed.path
                or re.search(r"[\x00-\x20]", value)):
            raise ProviderFailure("提供商返回了不支持的音频地址")
    except ValueError:
        raise ProviderFailure("提供商返回了无效的音频地址") from None
    # Official examples include http; upgrade only this exact bucket allowlist.
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def pcm_sample(payload):
    """Check the real PCM data, not a browser-provided duration field."""
    if not payload or len(payload) > MAX_SAMPLE_BYTES:
        raise ValueError("音频文件最多 1.5 MB，请转换为 24 kHz 单声道 WAV")
    try:
        with wave.open(io.BytesIO(payload), "rb") as audio:
            rate, channels, width, frames = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
            if (audio.getcomptype() != "NONE" or channels != 1 or width != 2
                    or rate != 24_000 or not 10 <= frames / rate <= 30):
                raise ValueError("请上传 10–30 秒、24 kHz 单声道 16 位 PCM WAV 人声音频")
            decoded = audio.readframes(frames)
            if len(decoded) != frames * channels * width:
                raise ValueError("音频内容不完整，请重新导出")
            # Reject completely silent recordings. This is not speaker recognition.
            if not any(decoded):
                raise ValueError("音频没有可用声音，请重新录制")
            return frames / rate
    except (wave.Error, EOFError):
        raise ValueError("音频格式无效，请转换为单声道 PCM WAV 后上传") from None


class HTTPProviders:
    """All external requests are to fixed vendor endpoints, never story URLs."""
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.voice_key = env.get("DASHSCOPE_API_KEY", "").strip()
        self.search_provider = env.get("STORY_SEARCH_PROVIDER", "local").strip().lower()
        self.search_key = ""
        self.search_url = ""
        if self.search_provider == "tavily":
            self.search_key = env.get("TAVILY_API_KEY", "").strip()
            self.search_url = "https://api.tavily.com/search"
        elif self.search_provider == "opensearch":
            self.search_key = env.get("OPENSEARCH_API_KEY", "").strip()
            endpoint = env.get("OPENSEARCH_ENDPOINT", "").strip().rstrip("/")
            workspace = env.get("OPENSEARCH_WORKSPACE", "default").strip()
            try:
                parsed = urlsplit(endpoint)
                # Console-provided public endpoints are validated below. An
                # unrecognized/private host disables the provider rather than
                # allowing a configurable network fetch.
                allowed_host = re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.opensearch\.aliyuncs\.com", parsed.hostname or "")
                if (parsed.scheme == "https" and allowed_host and not parsed.username and not parsed.password
                        and parsed.port in (None, 443) and not parsed.path and not parsed.query and not parsed.fragment
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", workspace)):
                    self.search_url = endpoint + "/v3/openapi/workspaces/" + quote(workspace) + "/web-search/ops-web-search-001"
            except ValueError:
                pass
        elif self.search_provider != "local":
            self.search_provider = "unsupported"
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    @property
    def voice_enabled(self):
        return bool(self.voice_key)

    @property
    def search_enabled(self):
        return bool(self.search_key and self.search_url)

    def _json(self, url, key, body):
        request = Request(url, json.dumps(body, ensure_ascii=False).encode(), {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, method="POST")
        try:
            with self.opener.open(request, timeout=40) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ProviderFailure("提供商响应超过大小限制")
                result = json.loads(data)
                if not isinstance(result, dict) or result.get("code"):
                    raise ProviderFailure("提供商未能完成请求，请稍后重试")
                return result
        except ProviderFailure:
            raise
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            raise ProviderFailure("提供商暂时不可用，请检查服务端配置或稍后重试") from None

    def enroll(self, payload, preferred_name):
        if not self.voice_enabled:
            raise ProviderFailure("音色克隆尚未配置")
        data = self._json(ENROLLMENT_URL, self.voice_key, {
            "model": "qwen-voice-enrollment",
            "input": {"action": "create", "target_model": VC_MODEL,
                      "preferred_name": preferred_name,
                      "audio": {"data": "data:audio/wav;base64," + base64.b64encode(payload).decode()}},
        })
        output = data.get("output", {})
        voice = output.get("voice") if isinstance(output, dict) else None
        if not isinstance(voice, str) or not voice or len(voice) > 512:
            raise ProviderFailure("提供商没有返回可用音色")
        if output.get("target_model", VC_MODEL) != VC_MODEL:
            raise ProviderFailure("提供商返回了不兼容的音色模型")
        return voice, VC_MODEL

    def delete_voice(self, voice):
        self._json(ENROLLMENT_URL, self.voice_key, {
            "model": "qwen-voice-enrollment", "input": {"action": "delete", "voice": voice},
        })

    def synthesize(self, text, voice, model):
        data = self._json(SYNTHESIS_URL, self.voice_key, {
            "model": model, "input": {"text": text, "voice": voice, "language_type": "Chinese"},
        })
        output = data.get("output", {})
        audio = output.get("audio", {}) if isinstance(output, dict) else {}
        if not isinstance(audio, dict):
            raise ProviderFailure("提供商没有返回可用音频")
        inline = audio.get("data")
        if audio.get("url"):
            url = result_audio_url(audio["url"])
            try:
                # No Authorization header is sent to a result bucket, and
                # redirects are rejected rather than following arbitrary URLs.
                with self.opener.open(Request(url), timeout=40) as response:
                    payload = response.read(MAX_AUDIO_BYTES + 1)
            except ProviderFailure:
                raise
            except (HTTPError, URLError, TimeoutError, OSError):
                raise ProviderFailure("无法取得合成音频，请稍后重试") from None
        elif isinstance(inline, str) and inline:
            try:
                payload = base64.b64decode(inline, validate=True)
            except ValueError:
                raise ProviderFailure("提供商返回了无效的音频数据") from None
            if payload[:4] != b"RIFF":
                # The documented audio.data response is 24 kHz / 16-bit mono
                # little-endian PCM, not a WAV file. Give that exact format a
                # proper WAV header; do not invent an API output-format option.
                if not payload or len(payload) % 2 or len(payload) > MAX_AUDIO_BYTES - 44:
                    raise ProviderFailure("提供商返回了无效的 PCM 音频")
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as recording:
                    recording.setnchannels(1)
                    recording.setsampwidth(2)
                    recording.setframerate(24000)
                    recording.writeframes(payload)
                payload = buffer.getvalue()
        else:
            raise ProviderFailure("提供商没有返回可用音频")
        if not 44 <= len(payload) <= MAX_AUDIO_BYTES or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
            raise ProviderFailure("提供商返回了不支持的音频格式")
        return payload

    def search(self, keyword):
        if not self.search_enabled:
            raise ProviderFailure("全网故事搜索尚未配置")
        query = keyword + " 睡前故事"
        if self.search_provider == "tavily":
            result = self._json(self.search_url, self.search_key, {
                "query": query, "max_results": 5, "search_depth": "basic",
                "include_answer": False, "include_raw_content": "text",
            })
            rows = result.get("results", [])
        else:
            result = self._json(self.search_url, self.search_key, {
                "query": query, "query_rewrite": False, "top_k": 5,
                "content_type": "mainText", "way": "lite",
            })
            rows = result.get("result", {}).get("search_result", [])
        stories = []
        for row in rows[:5] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            url = public_url(row.get("url", row.get("link", "")))
            title = row.get("title", "")
            if not url or not isinstance(title, str) or not title.strip():
                continue
            text = row.get("raw_content") if self.search_provider == "tavily" else row.get("content")
            text = text.strip()[:MAX_STORY_CHARACTERS] if isinstance(text, str) else ""
            snippet = row.get("content", "") if self.search_provider == "tavily" else row.get("snippet", "")
            stories.append({
                "id": "web-" + hashlib.sha256(url.encode()).hexdigest()[:32],
                "title": title.strip()[:200], "category": "检索", "tags": ["网页文本"],
                "text": text, "snippet": snippet[:1000] if isinstance(snippet, str) else "",
                "readMinutes": max(1, math.ceil(len(text) / 250)) if text else 0,
                "source": {"kind": "web", "label": "搜索服务返回的网页正文" if text else "搜索摘要",
                           "url": url, "note": "正文可能不完整或含网页附加内容；版权归原作者，请核对原文。"},
                "fullText": bool(text),
                "isExcerpt": True,
            })
        return stories


def initialize(server, provider=None):
    """Called once after the application's base schema exists."""
    server.bedtime_error = getattr(sys.modules[type(server).__module__], "APIError")
    server.bedtime_provider = provider or HTTPProviders()
    server.bedtime_provider_slots = threading.BoundedSemaphore(2)
    server.bedtime_audio_dir = server.data_dir / "bedtime-audio"
    server.bedtime_audio_dir.mkdir(mode=0o700, exist_ok=True)
    entries = json.loads((Path(__file__).parent / "stories_data.json").read_text(encoding="utf-8"))
    server.bedtime_stories = {}
    for entry in entries:
        entry["source"] = {"kind": "original", "label": "知序原创故事"}
        entry["readMinutes"] = max(1, math.ceil(len(entry["text"]) / 250))
        entry["fullText"] = True
        entry["isExcerpt"] = False
        server.bedtime_stories[entry["id"]] = entry
    with server.db() as conn:
        conn.executescript(SCHEMA)
        # A process restart cannot silently pretend an enrollment completed.
        conn.execute("UPDATE bedtime_voices SET state='failed' WHERE state='pending'")
        cleanup_audio(server, conn)


def fail(handler, status, message):
    raise handler.server.bedtime_error(status, message)


def provider_call(handler, function, *args):
    if not handler.server.bedtime_provider_slots.acquire(blocking=False):
        fail(handler, 429, "云端声音服务繁忙，请稍后重试")
    try:
        return function(*args)
    except ProviderFailure as exc:
        fail(handler, 502, str(exc))
    finally:
        handler.server.bedtime_provider_slots.release()


def voice_body(handler):
    if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
        fail(handler, 415, "请使用 JSON 请求")
    if not handler.server.upload_slots.acquire(blocking=False):
        fail(handler, 429, "上传繁忙，请稍后重试")
    # The application's outer request finally releases this slot, including
    # when a bounded/slow body is rejected.
    handler._upload_slot = True
    try:
        value = json.loads(handler.read_body(MAX_VOICE_JSON_BYTES))
    except (ValueError, UnicodeDecodeError):
        fail(handler, 400, "JSON 格式无效")
    if not isinstance(value, dict):
        fail(handler, 400, "请求内容必须为对象")
    return value


def cleanup_audio(server, conn):
    rows = conn.execute("SELECT id FROM bedtime_audio WHERE created_at<?", (int(time.time()) - AUDIO_TTL,)).fetchall()
    for row in rows:
        (server.bedtime_audio_dir / row["id"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM bedtime_audio WHERE created_at<?", (int(time.time()) - AUDIO_TTL,))


def voice_json(row):
    return {"id": row["id"], "name": row["name"], "state": row["state"],
            "createdAt": row["created_at"], "provider": "dashscope", "kind": "clone"}


def story_input(handler, data, story_id):
    if not isinstance(data, dict):
        fail(handler, 400, "请提供故事文本")
    title = handler.string(data.get("title", ""), "故事标题", 200, True)
    text = handler.string(data.get("text", ""), "故事正文", MAX_STORY_CHARACTERS, True)
    category = handler.string(data.get("category", "自存"), "故事分类", 30, True)
    source = data.get("source", {})
    url = public_url(source.get("url", "")) if isinstance(source, dict) else ""
    # User imports cannot claim to be an original built-in story.
    result = {"id": story_id, "title": title, "text": text, "category": category,
              "tags": ["自存故事"], "readMinutes": max(1, math.ceil(len(text) / 250)),
              "fullText": True, "isExcerpt": True, "source": {"kind": "imported", "label": "用户保存的文本，请确认使用权"}}
    if url:
        result["source"]["url"] = url
    return result


def saved_story(handler, conn, sid, uid, story_id, data=None):
    story = handler.server.bedtime_stories.get(story_id)
    if story:
        return story
    row = conn.execute("SELECT story FROM bedtime_favorites WHERE space_id=? AND user_id=? AND story_id=?", (sid, uid, story_id)).fetchone()
    if row:
        return json.loads(row["story"])
    if data:
        return story_input(handler, data, story_id)
    fail(handler, 404, "故事不存在，请先保存故事文本")


def local_search(server, keyword, category):
    # Every result is a real, complete original text, independent of voices.
    aliases = {"童话": "儿童", "成人": "治愈", "成人治愈短篇": "治愈", "哄睡": "晚安", "古风小故事": "古风", "晚安小故事": "晚安"}
    category = aliases.get(category, category)
    words = [part for part in re.split(r"[\s,，、]+", keyword.casefold()) if part]
    selected = []
    for story in server.bedtime_stories.values():
        if category not in ("", "全部") and story["category"] != category:
            continue
        haystack = " ".join([story["title"], story["category"], *story["tags"], story["text"]]).casefold()
        score = sum(3 if word in " ".join([story["title"], *story["tags"]]) else 1 for word in words if word in haystack)
        if words and not score:
            # Chinese partial-word matching remains useful without an external
            # tokenizer; require most characters to match metadata, not prose.
            metadata = " ".join([story["title"], story["category"], *story["tags"]])
            chars = set(keyword.strip())
            if len(chars) < 2 or len(chars.intersection(metadata)) / len(chars) < 0.75:
                continue
        selected.append((score, story))
    selected.sort(key=lambda item: -item[0])
    return [story for _, story in selected]


def dispatch(handler, path, query):
    """Return False for other modules; own auth, body read and short transactions."""
    match = STORY_ROUTE.fullmatch(path)
    if not match:
        return False
    sid, route = match[1], match[2] or ""
    method, server = handler.command, handler.server
    write = method not in ("GET", "HEAD")
    with server.db() as conn:
        user = handler.current_user(conn)
        handler.membership(conn, sid, user["id"], write=write)
    data = None
    if method in ("POST", "PUT", "PATCH"):
        data = voice_body(handler) if route == "voices" and method == "POST" else handler.read_json()
    elif write:
        handler.read_body(1024 * 1024)
    with server.db() as conn:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        user = handler.current_user(conn)
        uid = user["id"]
        handler.membership(conn, sid, uid, write=write)
        provider = server.bedtime_provider
        if route == "config" and method == "GET":
            result = {"systemSpeech": True,
                      "webSearch": {"enabled": provider.search_enabled, "provider": provider.search_provider},
                      "voiceClone": {"enabled": provider.voice_enabled, "provider": "dashscope", "sampleRate": 24000,
                                     "minimumSeconds": 10, "maximumSeconds": 30},
                      "synthesis": {"enabled": provider.voice_enabled, "provider": "dashscope", "maxCharacters": 600,
                                    "systemVoices": SYSTEM_VOICES if provider.voice_enabled else []}}
        elif route == "search" and method == "GET":
            keyword = handler.string(query.get("q", [""])[0], "搜索词", 100)
            category = handler.string(query.get("category", ["全部"])[0], "分类", 30)
            scope = query.get("scope", ["local"])[0]
            if scope not in ("local", "web"):
                fail(handler, 400, "搜索范围无效")
            if scope == "local":
                result = {"stories": local_search(server, keyword, category), "scope": scope, "query": keyword}
            else:
                if not keyword:
                    fail(handler, 400, "请输入搜索词")
                if not provider.search_enabled:
                    fail(handler, 503, "全网检索尚未配置，请使用原创故事库")
                server.throttle(("bedtime-search", uid), 30, 60)
                # GET has no writer transaction; the API call holds no SQLite
                # write lock and all authorization is checked again afterwards.
                stories = provider_call(handler, provider.search, keyword)
                with server.db() as check:
                    handler.current_user(check)
                    handler.membership(check, sid, uid)
                handler.json_response(200, {"stories": stories, "scope": "web", "query": keyword})
                return True
        elif (story_match := re.fullmatch(rf"stories/({STORY_ID})", route)) and method == "GET":
            result = {"story": saved_story(handler, conn, sid, uid, story_match[1])}
        elif route == "favorites" and method == "GET":
            rows = conn.execute("SELECT story,saved_at FROM bedtime_favorites WHERE space_id=? AND user_id=? ORDER BY saved_at DESC,story_id", (sid, uid)).fetchall()
            result = {"favorites": [{**json.loads(row["story"]), "favorited_at": row["saved_at"]} for row in rows]}
        elif favorite_match := re.fullmatch(rf"favorites/({STORY_ID})", route):
            story_id = favorite_match[1]
            if method == "PUT":
                exists = conn.execute("SELECT 1 FROM bedtime_favorites WHERE space_id=? AND user_id=? AND story_id=?", (sid, uid, story_id)).fetchone()
                count = conn.execute("SELECT count(*) FROM bedtime_favorites WHERE space_id=? AND user_id=?", (sid, uid)).fetchone()[0]
                if not exists and count >= 200:
                    fail(handler, 409, "每个空间最多收藏 200 篇故事")
                story = saved_story(handler, conn, sid, uid, story_id, data.get("story"))
                conn.execute("INSERT INTO bedtime_favorites VALUES(?,?,?,?,?) ON CONFLICT(space_id,user_id,story_id) DO UPDATE SET story=excluded.story,saved_at=excluded.saved_at", (sid, uid, story_id, json.dumps(story, ensure_ascii=False), stamp()))
                result = {"story": story, "saved": True}
            elif method == "DELETE":
                conn.execute("DELETE FROM bedtime_favorites WHERE space_id=? AND user_id=? AND story_id=?", (sid, uid, story_id))
                result = {"ok": True}
            else:
                fail(handler, 405, "不支持此请求方法")
        elif route == "history":
            if method == "GET":
                rows = conn.execute("SELECT * FROM bedtime_history WHERE space_id=? AND user_id=? ORDER BY played_at DESC,id DESC LIMIT 200", (sid, uid)).fetchall()
                result = {"history": [{"id": row["id"], "storyId": row["story_id"], "title": json.loads(row["story"])["title"],
                                       "story": json.loads(row["story"]), "voiceId": row["voice_id"], "positionSeconds": row["position_seconds"],
                                       "playedAt": row["played_at"]} for row in rows]}
            elif method == "POST":
                story_id = handler.string(data.get("storyId", ""), "故事编号", 96, True)
                if not re.fullmatch(STORY_ID, story_id):
                    fail(handler, 400, "故事编号无效")
                story = saved_story(handler, conn, sid, uid, story_id, data.get("story"))
                voice_id = handler.string(data.get("voiceId", "system"), "音色编号", 512, True)
                position = data.get("positionSeconds", 0)
                if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or not 0 <= position <= 24 * 3600:
                    fail(handler, 400, "播放位置无效")
                history_id, played = uuid.uuid4().hex, stamp()
                # Keep one recent entry per story instead of recording every
                # browser boundary event as another history row.
                conn.execute("DELETE FROM bedtime_history WHERE space_id=? AND user_id=? AND story_id=?", (sid, uid, story_id))
                conn.execute("INSERT INTO bedtime_history VALUES(?,?,?,?,?,?,?,?)", (history_id, sid, uid, story_id, json.dumps(story, ensure_ascii=False), voice_id, position, played))
                conn.execute("DELETE FROM bedtime_history WHERE space_id=? AND user_id=? AND id NOT IN (SELECT id FROM bedtime_history WHERE space_id=? AND user_id=? ORDER BY played_at DESC,id DESC LIMIT 200)", (sid, uid, sid, uid))
                result = {"ok": True, "id": history_id}
            elif method == "DELETE":
                conn.execute("DELETE FROM bedtime_history WHERE space_id=? AND user_id=?", (sid, uid))
                result = {"ok": True}
            else:
                fail(handler, 405, "不支持此请求方法")
        elif route == "voices" and method == "GET":
            rows = conn.execute("SELECT * FROM bedtime_voices WHERE space_id=? AND user_id=? ORDER BY created_at,id", (sid, uid)).fetchall()
            result = {"voices": [voice_json(row) for row in rows], "systemVoices": SYSTEM_VOICES if provider.voice_enabled else []}
        elif route == "voices" and method == "POST":
            name = handler.string(data.get("name", ""), "音色名称", 60, True)
            if data.get("consent") is not True:
                fail(handler, 400, "请确认声音为本人或已获得声音所有人的授权")
            if data.get("mimeType") not in ("audio/wav", "audio/x-wav", "audio/wave"):
                fail(handler, 415, "请转换为 PCM WAV 后上传")
            encoded = data.get("audioBase64", "")
            if not isinstance(encoded, str) or len(encoded) > MAX_SAMPLE_BYTES * 4 // 3 + 4:
                fail(handler, 413, "音频文件最多 1.5 MB，请转换为 24 kHz 单声道 WAV")
            try:
                payload = base64.b64decode(encoded, validate=True)
                pcm_sample(payload)
            except ValueError as exc:
                fail(handler, 400, str(exc) if str(exc).startswith("音频") or str(exc).startswith("请") else "音频编码无效")
            if not provider.voice_enabled:
                fail(handler, 503, "音色克隆尚未配置，请联系管理员设置服务端模型密钥")
            server.throttle(("bedtime-enroll", uid), 5, 3600)
            server.throttle(("bedtime-enroll-day", uid), 10, 86400)
            count = conn.execute("SELECT count(*) FROM bedtime_voices WHERE space_id=? AND user_id=?", (sid, uid)).fetchone()[0]
            if count >= 10:
                fail(handler, 409, "每个空间最多保存 10 个专属音色")
            voice_id, created = uuid.uuid4().hex, stamp()
            conn.execute("INSERT INTO bedtime_voices VALUES(?,?,?,?,?,?,?,?)", (voice_id, sid, uid, name, "pending", "", VC_MODEL, created))
            conn.commit()
            try:
                remote_voice, target_model = provider_call(handler, provider.enroll, payload, "bedtime" + voice_id[:8])
            except server.bedtime_error:
                with server.db() as failed:
                    failed.execute("UPDATE bedtime_voices SET state='failed' WHERE id=? AND user_id=? AND space_id=?", (voice_id, uid, sid))
                raise
            try:
                with server.db() as ready:
                    ready.execute("BEGIN IMMEDIATE")
                    handler.current_user(ready)
                    handler.membership(ready, sid, uid, write=True)
                    ready.execute("UPDATE bedtime_voices SET state='ready',provider_voice=?,target_model=? WHERE id=? AND user_id=? AND space_id=?", (remote_voice, target_model, voice_id, uid, sid))
                    row = ready.execute("SELECT * FROM bedtime_voices WHERE id=? AND user_id=? AND space_id=?", (voice_id, uid, sid)).fetchone()
                    if not row:
                        fail(handler, 404, "音色创建已经取消")
                handler.json_response(201, {"voice": voice_json(row)})
                return True
            except server.bedtime_error:
                # A membership/session revoke during enrollment must not expose
                # the result or orphan a usable voice profile in another user.
                try:
                    provider_call(handler, provider.delete_voice, remote_voice)
                except server.bedtime_error:
                    pass
                with server.db() as failed:
                    failed.execute("DELETE FROM bedtime_voices WHERE id=? AND user_id=? AND space_id=?", (voice_id, uid, sid))
                raise
        elif voice_match := re.fullmatch(rf"voices/({ID})", route):
            if method != "DELETE":
                fail(handler, 405, "不支持此请求方法")
            row = conn.execute("SELECT * FROM bedtime_voices WHERE id=? AND space_id=? AND user_id=?", (voice_match[1], sid, uid)).fetchone()
            if not row:
                fail(handler, 404, "音色不存在")
            if row["state"] == "pending":
                fail(handler, 409, "音色正在创建，请稍后再删除")
            if row["provider_voice"]:
                if not provider.voice_enabled:
                    fail(handler, 503, "请恢复服务端密钥以同时删除云端音色")
                conn.commit()
                provider_call(handler, provider.delete_voice, row["provider_voice"])
                conn.execute("BEGIN IMMEDIATE")
                handler.current_user(conn)
                handler.membership(conn, sid, uid, write=True)
            conn.execute("DELETE FROM bedtime_voices WHERE id=? AND space_id=? AND user_id=?", (voice_match[1], sid, uid))
            result = {"ok": True}
        elif route == "synthesize" and method == "POST":
            text = handler.string(data.get("text", ""), "朗读文本", 600, True)
            voice_id = handler.string(data.get("voiceId", ""), "音色编号", 96, True)
            if not provider.voice_enabled:
                fail(handler, 503, "云端朗读尚未配置，请使用设备系统音色")
            if voice_id in {voice["id"] for voice in SYSTEM_VOICES}:
                remote_voice, target_model = voice_id.split(":", 1)[1], SYSTEM_MODEL
            else:
                row = conn.execute("SELECT * FROM bedtime_voices WHERE id=? AND space_id=? AND user_id=? AND state='ready'", (voice_id, sid, uid)).fetchone()
                if not row:
                    fail(handler, 404, "音色不存在或尚未就绪")
                remote_voice, target_model = row["provider_voice"], row["target_model"]
            server.throttle(("bedtime-tts", uid), 60, 60)
            # 100 requests × at most 600 characters bounds daily synthesis
            # input to 60,000 characters per account; this is not a free quota.
            server.throttle(("bedtime-tts-day", uid), 100, 86400)
            conn.commit()
            payload = provider_call(handler, provider.synthesize, text, remote_voice, target_model)
            if not isinstance(payload, bytes) or not 44 <= len(payload) <= MAX_AUDIO_BYTES or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
                fail(handler, 502, "提供商返回了无效音频")
            with server.db() as saved:
                saved.execute("BEGIN IMMEDIATE")
                handler.current_user(saved)
                handler.membership(saved, sid, uid, write=True)
                if not voice_id.startswith("cloud:") and not saved.execute("SELECT 1 FROM bedtime_voices WHERE id=? AND space_id=? AND user_id=? AND state='ready'", (voice_id, sid, uid)).fetchone():
                    fail(handler, 404, "音色已删除")
                cleanup_audio(server, saved)
                space_bytes = saved.execute("SELECT COALESCE(sum(size),0) FROM bedtime_audio WHERE space_id=?", (sid,)).fetchone()[0]
                space_bytes += saved.execute("SELECT COALESCE(sum(size),0) FROM files WHERE space_id=?", (sid,)).fetchone()[0]
                total_bytes = saved.execute("SELECT COALESCE(sum(size),0) FROM bedtime_audio").fetchone()[0]
                total_bytes += saved.execute("SELECT COALESCE(sum(size),0) FROM files").fetchone()[0]
                if space_bytes + len(payload) > server.max_space_storage or total_bytes + len(payload) > server.max_total_storage:
                    fail(handler, 413, "存储容量已满，请稍后重试或整理媒体文件")
                if shutil.disk_usage(server.data_dir).free < len(payload) + 100 * 1024 * 1024:
                    fail(handler, 507, "服务器可用存储不足，请联系管理员")
                audio_id = uuid.uuid4().hex
                audio_path = server.bedtime_audio_dir / audio_id
                try:
                    with audio_path.open("xb") as audio:
                        audio.write(payload)
                    os.chmod(audio_path, 0o600)
                    saved.execute("INSERT INTO bedtime_audio VALUES(?,?,?,?,?)", (audio_id, sid, uid, len(payload), int(time.time())))
                except Exception:
                    audio_path.unlink(missing_ok=True)
                    raise
            handler.json_response(200, {"audioUrl": f"/api/spaces/{sid}/bedtime/audio/{audio_id}", "mimeType": "audio/wav", "expiresIn": AUDIO_TTL})
            return True
        elif (audio_match := re.fullmatch(rf"audio/({ID})", route)) and method == "GET":
            row = conn.execute("SELECT * FROM bedtime_audio WHERE id=? AND space_id=? AND user_id=? AND created_at>?", (audio_match[1], sid, uid, int(time.time()) - AUDIO_TTL)).fetchone()
            if not row:
                fail(handler, 404, "朗读音频不存在或已过期，请重新生成")
            try:
                audio = (server.bedtime_audio_dir / row["id"]).read_bytes()
            except FileNotFoundError:
                fail(handler, 404, "朗读音频不存在，请重新生成")
            handler.send_response(200)
            handler.security_headers(file_response=True)
            handler.send_header("Content-Type", "audio/wav")
            handler.send_header("Cache-Control", "private, no-store")
            handler.send_header("Content-Length", str(len(audio)))
            handler.end_headers()
            handler.wfile.write(audio)
            return True
        else:
            fail(handler, 404, "接口不存在")
        if write:
            conn.commit()
    handler.json_response(200, result)
    return True
