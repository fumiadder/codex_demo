"""Web Push (RFC 8030 / 8291 / 8292) 的最小实现，只依赖 ``cryptography``。

实现要点：
- VAPID（RFC 8292）：用 P-256 私钥签 ES256 JWT，附带公钥给推送服务校验。
- 负载加密（RFC 8291，aes128gcm）：与订阅端做 ECDH，HKDF 派生，AES-GCM 加密。
- 推送服务返回 404/410 表示订阅已失效，调用方应删除该订阅。

密钥只保存在服务器环境变量中，绝不下发到页面或日志。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import sys
import time

try:  # cryptography 为可选依赖：缺失时推送功能整体关闭，其他功能不受影响。
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在缺依赖时触发
    CRYPTO_AVAILABLE = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 endpoint TEXT NOT NULL,
 p256dh TEXT NOT NULL,
 auth TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS push_subscriptions_user ON push_subscriptions(user_id);
"""

# 推送服务返回这些状态码时说明订阅已失效。
GONE_STATUSES = (404, 410)
MAX_SUBSCRIPTIONS_PER_USER = 10
REQUEST_TIMEOUT = 8


def fail(handler, status, message):
    error = getattr(handler.server, "webpush_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(value):
    value = value.strip()
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def vapid_private_key():
    """从环境变量读取 VAPID 私钥（base64url DER）；缺失或非法时返回 None。"""
    raw = os.environ.get("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
    if not raw:
        return None
    try:
        return serialization.load_der_private_key(_b64url_decode(raw), password=None)
    except Exception:
        logging.warning("Web push VAPID key is invalid; push disabled")
        return None


def vapid_public_key_b64():
    """返回 VAPID 公钥（base64url，未压缩点），供浏览器订阅时使用。"""
    key = vapid_private_key()
    if key is None:
        return ""
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(public)


def generate_vapid_keys():
    """生成一对 VAPID 密钥，返回 (私钥 DER base64url, 公钥 base64url)。"""
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(der), _b64url(public)


def _vapid_headers(endpoint, subject):
    """构造 Authorization: vapid t=<JWT>,k=<公钥> 头。"""
    key = vapid_private_key()
    if key is None:
        raise ValueError("Web push is not configured")
    origin = _origin_of(endpoint)
    header = _b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64url(
        json.dumps(
            {"aud": origin, "exp": int(time.time()) + 12 * 3600, "sub": subject},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    der_signature = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # JWT 要求 r||s 定长拼接，需把 DER 编码拆开重排。
    r, s = _split_der_signature(der_signature)
    signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    jwt = f"{header}.{claims}.{signature}"
    return {
        "Authorization": f"vapid t={jwt},k={vapid_public_key_b64()}",
    }


def _origin_of(endpoint):
    parts = endpoint.split("/")
    if len(parts) > 2:
        return f"{parts[0]}//{parts[2]}"
    return endpoint


def _split_der_signature(der):
    """把 DER 编码的 ECDSA 签名拆成 (r, s)。"""
    if der[0] != 0x30:
        raise ValueError("unexpected signature encoding")
    length = der[1]
    offset = 2
    if length & 0x80:
        offset += length & 0x7F
    def read_int(start):
        if der[start] != 0x02:
            raise ValueError("unexpected integer encoding")
        size = der[start + 1]
        begin = start + 2
        return int.from_bytes(der[begin:begin + size], "big"), begin + size
    r, nxt = read_int(offset)
    s, _ = read_int(nxt)
    return r, s


def encrypt_payload(payload, p256dh, auth):
    """按 RFC 8291 加密推送负载，返回 aes128gcm 请求体。"""
    user_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _b64url_decode(p256dh)
    )
    auth_secret = _b64url_decode(auth)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ephemeral.exchange(ec.ECDH(), user_public)
    # RFC 8291: salt 16 字节，与 auth_secret 共同派生密钥
    salt = os.urandom(16)
    key_info = b"WebPush: info\x00" + _b64url_decode(p256dh) + ephemeral_public
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(shared)
    cek_info = b"Content-Encoding: aes128gcm\x00"
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=cek_info).derive(ikm)
    nonce_info = b"Content-Encoding: nonce\x00"
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=nonce_info).derive(ikm)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    # aes128gcm 记录：2 字节长度 + 2 字节填充 + 明文 + 16 字节 tag
    record_length = len(body) + 2 + 2 + 16
    length_prefix = struct.pack(">H", record_length)
    pad_length = struct.pack(">H", 1)  # padding 长度 = 1（一个 0x00 字节）
    plain = length_prefix + pad_length + body + b"\x00"
    cipher = AESGCM(cek).encrypt(nonce, plain, None)
    # 头部：4 字节记录总长 + 1 字节盐长 + 盐 + 1 字节密钥长 + 非压缩公钥(65 字节)
    ephemeral_public_len = len(ephemeral_public)
    header = (
        struct.pack(">I", len(cipher))
        + struct.pack(">B", len(salt))
        + salt
        + struct.pack(">B", ephemeral_public_len)
        + ephemeral_public
    )
    return header + cipher


def send_push(endpoint, p256dh, auth, payload, *, subject="mailto:admin@localhost", opener=None, timeout=REQUEST_TIMEOUT):
    """发送一条推送；返回 (ok, status, error)。永不抛异常。"""
    if not CRYPTO_AVAILABLE:
        return False, 0, "Web push 未启用"
    try:
        headers = _vapid_headers(endpoint, subject)
        body = encrypt_payload(payload, p256dh, auth)
    except Exception:
        return False, 0, "推送加密失败"
    headers.update(
        {
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL": "60",
            "Urgency": "normal",
            "Content-Length": str(len(body)),
        }
    )
    import urllib.error
    import urllib.request

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    active = opener or urllib.request.build_opener()
    try:
        with active.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 201)
            response.read(4096)
        return True, int(status), ""
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            exc.read(4096)
        finally:
            exc.close()
        return False, code, f"推送服务返回 {code}"
    except Exception:
        return False, 0, "推送请求失败"


def initialize(server):
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:
        from server import APIError as error
    server.webpush_error = error


def _now_iso():
    from server import now_iso

    return now_iso()


def _current_user(handler, conn):
    return handler.current_user(conn)


def dispatch(handler, path, query):
    """自有路由：/api/push/config 与 /api/push/subscribe。"""
    if path not in ("/api/push/config", "/api/push/subscribe"):
        return False
    method = handler.command
    server = handler.server
    if path == "/api/push/config":
        if method != "GET":
            fail(handler, 405, "不支持此请求方法")
        handler.json_response(
            200,
            {
                "enabled": bool(CRYPTO_AVAILABLE and vapid_private_key() is not None),
                "publicKey": vapid_public_key_b64(),
            },
        )
        return True
    # /api/push/subscribe
    with server.db() as conn:
        user = _current_user(handler, conn)
        uid = user["id"]
        if method == "POST":
            data = handler.read_json()
            if not isinstance(data, dict):
                fail(handler, 400, "请求内容必须为对象")
            endpoint = data.get("endpoint")
            keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
            p256dh, auth = keys.get("p256dh"), keys.get("auth")
            if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
                fail(handler, 400, "推送地址无效")
            if not isinstance(p256dh, str) or not isinstance(auth, str) or len(p256dh) > 200 or len(auth) > 100:
                fail(handler, 400, "推送密钥无效")
            if not CRYPTO_AVAILABLE or vapid_private_key() is None:
                fail(handler, 400, "服务端未配置推送密钥")
            count = conn.execute(
                "SELECT count(*) FROM push_subscriptions WHERE user_id=?", (uid,)
            ).fetchone()[0]
            existing = conn.execute(
                "SELECT id FROM push_subscriptions WHERE user_id=? AND endpoint=?", (uid, endpoint)
            ).fetchone()
            if existing is None and count >= MAX_SUBSCRIPTIONS_PER_USER:
                fail(handler, 400, "推送订阅过多，请先在设备上取消旧订阅")
            stamp = _now_iso()
            conn.execute("BEGIN IMMEDIATE")
            if existing is not None:
                conn.execute(
                    "UPDATE push_subscriptions SET p256dh=?,auth=?,updated_at=? WHERE id=?",
                    (p256dh, auth, stamp, existing["id"]),
                )
            else:
                import uuid

                conn.execute(
                    "INSERT INTO push_subscriptions (id,user_id,endpoint,p256dh,auth,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, uid, endpoint, p256dh, auth, stamp, stamp),
                )
            handler.json_response(200 if existing is not None else 201, {"ok": True})
            return True
        if method == "DELETE":
            data = handler.read_json()
            endpoint = data.get("endpoint") if isinstance(data, dict) else None
            if not isinstance(endpoint, str):
                fail(handler, 400, "推送地址无效")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?", (uid, endpoint)
            )
            handler.json_response(200, {"ok": True})
            return True
        fail(handler, 405, "不支持此请求方法")
    return True


def deliver(server, uid, title, body, data):
    """把一条通知推送到该用户所有订阅；订阅失效时自动清理。"""
    if not CRYPTO_AVAILABLE or vapid_private_key() is None:
        return 0
    payload = {"title": title, "body": body, "data": data or {}}
    delivered = 0
    try:
        with server.db() as conn:
            subs = conn.execute(
                "SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE user_id=?", (uid,)
            ).fetchall()
        gone = []
        for row in subs:
            ok, status, error = send_push(row["endpoint"], row["p256dh"], row["auth"], payload)
            if ok:
                delivered += 1
            elif status in GONE_STATUSES:
                gone.append(row["id"])
            elif error:
                logging.warning("push delivery failed status=%s", status)
        if gone:
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for sub_id in gone:
                    conn.execute("DELETE FROM push_subscriptions WHERE id=?", (sub_id,))
    except Exception:
        logging.warning("push delivery skipped")
    return delivered
