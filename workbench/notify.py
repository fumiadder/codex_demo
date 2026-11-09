"""应用内通知与出站 Webhook（安全优先的新出站网络面）。

模块自有三张表、独立路由与维护入口，复用 server 的会话鉴权、CSRF、限流与
SQLite/PostgreSQL 持久层（? 占位由 persistence 自动转换）。所有出站请求：

- 仅允许 https；禁止 userinfo；DNS 解析后的【每个】地址都必须通过 SSRF 网段
  审查（loopback/private/link-local/reserved/multicast/unspecified/IPv6 ULP）；
- 禁止任何 3xx 重定向，杜绝跳转到内网；
- HMAC-SHA256 签名，固定 8 秒超时，2xx 成功、429/5xx/网络错误退避重试、
  其余 4xx 永久失败；
- 日志只记录 webhook id、event 与结果码，绝不记录完整 url、secret 或请求体。

通知生成由认证请求驱动（server.maybe_cleanup 节流调用），无常驻轮询 worker；
发送侧最多一个守护线程，线程内异常只告警不抛回请求线程。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ID = r"[a-f0-9]{32}"
ID_RE = re.compile(rf"^{ID}$")
DUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHANGHAI_TZ = timezone(timedelta(hours=8))

# 维护与发送参数。
MAINTENANCE_INTERVAL = 60
# 定时提醒：到点后最多延迟这么久仍会补发（避免服务刚好在那一分钟不可用而漏提醒）
REMIND_GRACE_SECONDS = 15 * 60
USER_BATCH = 200
MAX_SPACES_PER_USER = 500
MAX_ITEMS_PER_SPACE = 2000
PUMP_BATCH = 10
MAX_ATTEMPTS = 5
BACKOFF = (60, 300, 900, 3600)
REQUEST_TIMEOUT = 8
TEST_TIMEOUT = 6
MAX_ERROR_LEN = 500
MAX_URL_LEN = 2000
MAX_NAME_LEN = 60
MAX_EVENTS = 10

# 路由侧限流（按账号）。
MAX_WEBHOOKS = 10
WEBHOOK_WRITE_LIMIT = 30
WEBHOOK_WRITE_WINDOW = 3600
WEBHOOK_TEST_LIMIT = 5
WEBHOOK_TEST_WINDOW = 3600

ALLOWED_EVENTS = ("task.due", "*")
EVENT_TASK_DUE = "task.due"
EVENT_WEBHOOK_TEST = "webhook.test"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 space_id TEXT REFERENCES spaces(id) ON DELETE CASCADE,
 type TEXT NOT NULL,
 title TEXT NOT NULL,
 body TEXT NOT NULL DEFAULT '',
 data TEXT NOT NULL DEFAULT '{}',
 dedup_key TEXT,
 read INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user_read ON notifications(user_id,read,created_at DESC);
CREATE INDEX IF NOT EXISTS notifications_user_dedup ON notifications(user_id,dedup_key);
CREATE TABLE IF NOT EXISTS user_webhooks (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 url TEXT NOT NULL,
 secret TEXT NOT NULL,
 events TEXT NOT NULL DEFAULT '[]',
 enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS user_webhooks_user ON user_webhooks(user_id);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
 id TEXT PRIMARY KEY,
 webhook_id TEXT NOT NULL REFERENCES user_webhooks(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 event TEXT NOT NULL,
 payload TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','sent','failed')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at BIGINT NOT NULL,
 last_error TEXT NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_state ON webhook_deliveries(state,next_attempt_at);
"""

# 维护节流（模块级单调时钟）与单发送线程管理。
_maintenance_lock = threading.Lock()
_last_maintenance = 0.0
_pump_lock = threading.Lock()
_pump_thread = None

# 模块级解析器间接点：测试 monkeypatch notify._resolve 即可，不会污染全局
# socket.getaddrinfo（测试自身的 HTTP 客户端仍需真实解析 127.0.0.1）。
_resolve = socket.getaddrinfo


# --------------------------------------------------------------------------- #
# 时间/文本辅助（函数级间接，便于测试模拟时间）
# --------------------------------------------------------------------------- #
def _now():
    return int(time.time())


def _mono():
    return time.monotonic()


def _today():
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(text):
    if not isinstance(text, str):
        return ""
    return text[:MAX_ERROR_LEN]


def _parse_remind_at(value):
    """解析 meta.remindAt（YYYY-MM-DDTHH:MM）为 Unix 秒；非法返回 None。"""
    if not isinstance(value, str) or not value.strip()[:16]:
        return None
    try:
        parsed = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return int(time.mktime(parsed.timetuple()))


def _valid_due(value):
    if not isinstance(value, str) or not DUE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# SSRF 防护（纯函数 + 可 monkeypatch 的 socket/opener 入口）
# --------------------------------------------------------------------------- #
def _is_blocked_ip(ip):
    """命中 loopback/private/link-local/reserved/multicast/unspecified 即拦。"""
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # 显式再兜一层 IPv6 唯一本地地址 fc00::/7（is_private 已覆盖，双保险）。
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("fc00::/7"):
        return True
    return False


def validate_webhook_url(raw):
    """校验出站 URL，合法返回规范化 URL；任何疑点都抛中文 ValueError。"""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_LEN or "\x00" in raw:
        raise ValueError("Webhook 地址无效")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("Webhook 地址无法解析") from None
    # 严格 https：拒绝 http/file/gopher/ftp/javascript 等所有非 https scheme。
    if parsed.scheme != "https":
        raise ValueError("仅允许 https 协议的 Webhook 地址")
    # 禁止 userinfo（https://user:pass@host 中的 @ 一律拒绝）。
    if not parsed.netloc or "@" in parsed.netloc or parsed.username is not None:
        raise ValueError("Webhook 地址中不允许包含用户名或密码")
    host = parsed.hostname
    if not host:
        raise ValueError("Webhook 地址缺少有效的主机名")
    try:
        port = parsed.port or 443
    except ValueError:
        raise ValueError("Webhook 地址端口无效") from None
    try:
        infos = _resolve(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise ValueError("域名解析失败，无法确认目标地址安全") from None
    if not infos:
        raise ValueError("域名解析失败，无法确认目标地址安全")
    for info in infos:
        try:
            address = info[4][0]
            ip = ipaddress.ip_address(address)
        except (IndexError, ValueError):
            raise ValueError("Webhook 目标地址解析结果无效") from None
        if _is_blocked_ip(ip):
            raise ValueError("Webhook 目标指向内网或保留地址，已拒绝")
    return parsed.geturl()


class WebhookError(Exception):
    """已脱敏的 Webhook 错误，消息可直接回显用户，不含 url/secret。"""


class RedirectBlocked(WebhookError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    """任何 3xx 直接抛错：重定向是 SSRF 绕过内网限制的经典路径。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectBlocked("目标服务器返回了重定向，已拒绝")


def build_safe_opener():
    """装配禁重定向的 opener；测试通过 monkeypatch 本函数注入 fake opener。"""
    return build_opener(_NoRedirect())


class SendResult:
    __slots__ = ("ok", "status", "retryable", "error")

    def __init__(self, ok, status=0, retryable=False, error=""):
        self.ok = ok
        self.status = status
        self.retryable = retryable
        self.error = error


def send_webhook(url, secret, event, delivery_id, payload, *, opener=None, timeout=REQUEST_TIMEOUT):
    """同步发送一次签名 POST；永不抛异常，返回结构化 SendResult。"""
    try:
        safe_url = validate_webhook_url(url)
    except ValueError as exc:
        # 地址本身非法/指向内网：重试不会改变配置，按永久失败处理。
        return SendResult(False, 0, False, str(exc))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = Request(safe_url, data=body, headers={
        "Content-Type": "application/json",
        "X-Zhixu-Event": event,
        "X-Zhixu-Delivery": delivery_id,
        "X-Zhixu-Signature": signature,
    }, method="POST")
    active_opener = opener if opener is not None else build_safe_opener()
    try:
        with active_opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            # 有界排空响应体，避免连接无法复用/挂死；不记录内容。
            response.read(1024 * 1024)
        return SendResult(True, int(status), False, "")
    except RedirectBlocked as exc:
        return SendResult(False, 0, False, str(exc))
    except HTTPError as exc:
        code = exc.code
        try:
            exc.read(64 * 1024)
        except OSError:
            pass
        finally:
            exc.close()
        if code == 429 or 500 <= code <= 599:
            return SendResult(False, code, True, f"目标服务暂时不可用（HTTP {code}）")
        # 其余 4xx 永久失败（含 400/401/403/404 等）。
        return SendResult(False, code, False, f"目标服务拒绝了请求（HTTP {code}）")
    except (URLError, socket.timeout, TimeoutError, OSError):
        return SendResult(False, 0, True, "网络连接失败或请求超时")
    except Exception:
        # 发送器自身任何意外都不允许炸到维护/请求线程。
        return SendResult(False, 0, True, "Webhook 请求失败")


# --------------------------------------------------------------------------- #
# 维护：到期任务 -> 通知 + webhook 入队（请求驱动，无轮询常驻）
# --------------------------------------------------------------------------- #
def _hook_wants(hook_events, event):
    try:
        events = json.loads(hook_events)
    except (ValueError, TypeError):
        return False
    return isinstance(events, list) and ("*" in events or event in events)


def _enqueue_for_hooks(conn, uid, notification, hooks):
    """在当前事务内为订阅该事件的 webhook 各入一条 pending delivery。"""
    now = _now()
    for hook in hooks:
        if not _hook_wants(hook["events"], EVENT_TASK_DUE):
            continue
        delivery_id = uuid.uuid4().hex
        payload = {
            "event": EVENT_TASK_DUE,
            "deliveryId": delivery_id,
            "sentAt": _stamp(),
            "notification": notification,
        }
        conn.execute(
            "INSERT INTO webhook_deliveries "
            "(id,webhook_id,user_id,event,payload,state,attempts,next_attempt_at,last_error,created_at) "
            "VALUES (?,?,?,?,?,'pending',0,?,'',?)",
            (delivery_id, hook["id"], uid, EVENT_TASK_DUE,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now, now),
        )


def _scan_user(conn, uid, today, now):
    """扫描单个用户成员空间内未完成且到期的 task，返回待写通知清单。"""
    spaces = [
        row["space_id"]
        for row in conn.execute(
            "SELECT space_id FROM members WHERE user_id=? ORDER BY space_id LIMIT ?",
            (uid, MAX_SPACES_PER_USER),
        ).fetchall()
    ]
    candidates = []
    for sid in spaces:
        rows = conn.execute(
            "SELECT id,space_id,title,meta FROM items "
            "WHERE space_id=? AND kind='task' AND status<>'done' ORDER BY id LIMIT ?",
            (sid, MAX_ITEMS_PER_SPACE),
        ).fetchall()
        candidates.extend(rows)
    if not candidates:
        return []
    existing = {
        row["dedup_key"]
        for row in conn.execute(
            "SELECT dedup_key FROM notifications WHERE user_id=? AND dedup_key IS NOT NULL",
            (uid,),
        ).fetchall()
    }
    pending = []
    for item in candidates:
        try:
            meta = json.loads(item["meta"])
        except (ValueError, TypeError):
            continue
        if not isinstance(meta, dict):
            continue
        # 优先：任务设置了具体提醒时间（meta.remindAt，形如 2026-11-10T09:00）
        remind_at = meta.get("remindAt")
        if remind_at:
            moment = _parse_remind_at(remind_at)
            if moment is not None and 0 <= (now - moment) <= REMIND_GRACE_SECONDS:
                dedup_key = f"task-remind:{item['id']}:{remind_at}"
                if dedup_key in existing:
                    continue
                existing.add(dedup_key)
                pending.append((item, item["space_id"], remind_at, dedup_key))
                continue
            # 还没到点、或早已超出补发窗口：不再走「按日期提醒」路径，
            # 否则会给设了时间点的任务重复提醒。
            if moment is not None:
                continue
        due = meta.get("due")
        # ISO 日期可直接字符串比较；非法/未来日期跳过。
        if not _valid_due(due) or due > today:
            continue
        dedup_key = f"task-due:{item['id']}:{due}"
        if dedup_key in existing:
            continue
        existing.add(dedup_key)
        pending.append((item, item["space_id"], due, dedup_key))
    return pending


def _persist_notifications(server, uid, pending, today):
    if not pending:
        return 0
    created = 0
    with server.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 写事务内重新取 webhook，避免读/写事务间隙的配置变更。
        hooks = [
            dict(row)
            for row in conn.execute(
                "SELECT id,events FROM user_webhooks WHERE user_id=? AND enabled=1",
                (uid,),
            ).fetchall()
        ]
        for item, sid, due, dedup_key in pending:
            # 写锁内复查 dedup，跨请求并发也不会重复通知。
            if conn.execute(
                "SELECT 1 FROM notifications WHERE user_id=? AND dedup_key=?",
                (uid, dedup_key),
            ).fetchone():
                continue
            notification_id = uuid.uuid4().hex
            stamp = _stamp()
            title = item["title"]
            body = f"任务「{title}」已于 {due} 到期，请及时处理。"
            data = json.dumps(
                {"itemId": item["id"], "spaceId": sid, "due": due},
                ensure_ascii=False, separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO notifications "
                "(id,user_id,space_id,type,title,body,data,dedup_key,read,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (notification_id, uid, sid, EVENT_TASK_DUE, title, body, data, dedup_key, stamp),
            )
            notification = {
                "id": notification_id,
                "type": EVENT_TASK_DUE,
                "title": title,
                "body": body,
                "data": {"itemId": item["id"], "spaceId": sid, "due": due},
                "createdAt": stamp,
            }
            _enqueue_for_hooks(conn, uid, notification, hooks)
            created += 1
    return created


def _scan_due_tasks(server):
    today = _today()
    offset = 0
    while True:
        with server.db() as conn:
            users = conn.execute(
                "SELECT id FROM users ORDER BY id LIMIT ? OFFSET ?",
                (USER_BATCH, offset),
            ).fetchall()
        if not users:
            break
        for user_row in users:
            uid = user_row["id"]
            with server.db() as conn:
                pending = _scan_user(conn, uid, today, _now())
            _persist_notifications(server, uid, pending, today)
        offset += len(users)
        if len(users) < USER_BATCH:
            break


def run_maintenance(server, *, force=False):
    """maybe_cleanup 在锁内调用；自带 60s 节流，任何异常都不抛回请求线程。"""
    global _last_maintenance
    try:
        if not _maintenance_lock.acquire(blocking=False):
            return
        try:
            current = _mono()
            if not force and current - _last_maintenance < MAINTENANCE_INTERVAL:
                return
            _last_maintenance = current
            _scan_due_tasks(server)
        finally:
            _maintenance_lock.release()
        # 入队后驱动一次异步发送（内部仍保证最多一个发送线程）。
        pump(server)
    except Exception:
        logging.warning("Notification maintenance skipped")


# --------------------------------------------------------------------------- #
# 发送泵：模块锁 + 单守护线程，退避重试，异常只告警
# --------------------------------------------------------------------------- #
def pump(server):
    """确保至多一个守护发送线程在跑；已有存活线程则直接返回。"""
    global _pump_thread
    with _pump_lock:
        if _pump_thread is not None and _pump_thread.is_alive():
            return
        thread = threading.Thread(
            target=_pump_run, args=(server,), name="webhook-pump", daemon=True
        )
        _pump_thread = thread
    thread.start()


def _pump_run(server):
    try:
        _drain_deliveries(server)
    except Exception:
        logging.warning("Webhook delivery pump failed")


def _drain_deliveries(server, *, limit=PUMP_BATCH):
    """同步处理一批到期 delivery（测试直接调用本函数获得确定性）。"""
    now = _now()
    with server.db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE state='pending' AND next_attempt_at<=? "
            "ORDER BY next_attempt_at,id LIMIT ?",
            (now, limit),
        ).fetchall()
    for row in rows:
        _deliver_one(server, row)


def _deliver_one(server, row):
    delivery_id = row["id"]
    event = row["event"]
    try:
        with server.db() as conn:
            hook = conn.execute(
                "SELECT * FROM user_webhooks WHERE id=? AND enabled=1",
                (row["webhook_id"],),
            ).fetchone()
        if hook is None:
            # webhook 被删时 delivery 通常已级联删除；停用的 hook 不再发送。
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE webhook_deliveries SET state='failed',last_error=? "
                    "WHERE id=? AND state='pending'",
                    ("Webhook 已停用或被删除", delivery_id),
                )
            logging.warning("webhook skipped id=%s event=%s reason=disabled", row["webhook_id"], event)
            return
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            payload = {}
        result = send_webhook(hook["url"], hook["secret"], event, delivery_id, payload)
        now = _now()
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if result.ok:
                conn.execute(
                    "UPDATE webhook_deliveries SET state='sent',attempts=attempts+1,"
                    "last_error='' WHERE id=? AND state='pending'",
                    (delivery_id,),
                )
                logging.info("webhook sent id=%s event=%s status=%s", hook["id"], event, result.status)
            elif result.retryable:
                attempts = row["attempts"] + 1
                if attempts >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE webhook_deliveries SET state='failed',attempts=?,last_error=? "
                        "WHERE id=? AND state='pending'",
                        (attempts, _clip(result.error), delivery_id),
                    )
                else:
                    delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
                    conn.execute(
                        "UPDATE webhook_deliveries SET attempts=?,next_attempt_at=?,last_error=? "
                        "WHERE id=? AND state='pending'",
                        (attempts, now + delay, _clip(result.error), delivery_id),
                    )
                logging.warning(
                    "webhook retry id=%s event=%s status=%s attempts=%s",
                    hook["id"], event, result.status, attempts,
                )
            else:
                conn.execute(
                    "UPDATE webhook_deliveries SET state='failed',attempts=attempts+1,last_error=? "
                    "WHERE id=? AND state='pending'",
                    (_clip(result.error), delivery_id),
                )
                logging.warning(
                    "webhook failed id=%s event=%s status=%s", hook["id"], event, result.status
                )
    except Exception:
        # 线程内吞掉一切异常，只记录脱敏定位信息。
        logging.warning("webhook delivery error id=%s event=%s", delivery_id, event)


# --------------------------------------------------------------------------- #
# 独立 dispatch：自带鉴权，返回 False 透传给其他模块
# --------------------------------------------------------------------------- #
def initialize(server):
    """与 bedtime/daily/export 保持一致的初始化入口；本模块无目录初始化。"""
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:  # 测试子类可能位于其他模块
        from server import APIError as error
    server.notify_error = error


def fail(handler, status, message):
    error = getattr(handler.server, "notify_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def _clean_events(value):
    if not isinstance(value, list) or not value or len(value) > MAX_EVENTS:
        raise ValueError("事件订阅必须是非空数组，且不超过 10 项")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or item not in ALLOWED_EVENTS:
            raise ValueError("仅支持 task.due 或 * 事件")
        if item not in cleaned:
            cleaned.append(item)
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def _webhook_json(row, *, secret=None):
    try:
        events = json.loads(row["events"])
    except (ValueError, TypeError):
        events = []
    result = {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "events": events,
        "enabled": bool(row["enabled"]),
        "secretConfigured": True,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    # secret 只允许在创建/轮转的响应里出现一次。
    if secret is not None:
        result["secret"] = secret
    return result


def _notification_json(row):
    try:
        data = json.loads(row["data"])
    except (ValueError, TypeError):
        data = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "body": row["body"],
        "data": data,
        "read": bool(row["read"]),
        "spaceId": row["space_id"],
        "createdAt": row["created_at"],
    }


def _list_notifications(handler, conn, query):
    raw_limit = query.get("limit", [None])[0]
    limit = 20
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            fail(handler, 400, "limit 必须是 1 至 50 的整数")
        if not 1 <= limit <= 50:
            fail(handler, 400, "limit 必须是 1 至 50 的整数")
    uid = handler._notify_uid
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    unread = conn.execute(
        "SELECT count(*) FROM notifications WHERE user_id=? AND read=0",
        (uid,),
    ).fetchone()[0]
    return {"notifications": [_notification_json(row) for row in rows], "unreadCount": int(unread)}


def _mark_read(handler, conn, data):
    uid = handler._notify_uid
    if not isinstance(data, dict):
        fail(handler, 400, "请求内容必须为对象")
    if data.get("all") is True:
        changed = conn.execute(
            "UPDATE notifications SET read=1 WHERE user_id=? AND read=0",
            (uid,),
        ).rowcount
        return {"ok": True, "updated": int(changed)}
    notification_id = data.get("id")
    if not isinstance(notification_id, str) or not ID_RE.fullmatch(notification_id):
        fail(handler, 400, "通知 id 无效")
    changed = conn.execute(
        "UPDATE notifications SET read=1 WHERE id=? AND user_id=? AND read=0",
        (notification_id, uid),
    ).rowcount
    if not changed:
        # 不区分不存在/他人资源/已读：统一 404，避免暴露存在性。
        fail(handler, 404, "通知不存在或无权访问")
    return {"ok": True, "updated": 1}


def _get_owned_webhook(handler, conn, hook_id):
    row = conn.execute(
        "SELECT * FROM user_webhooks WHERE id=? AND user_id=?",
        (hook_id, handler._notify_uid),
    ).fetchone()
    if row is None:
        fail(handler, 404, "Webhook 不存在或无权访问")
    return row


def _list_webhooks(handler, conn):
    rows = conn.execute(
        "SELECT * FROM user_webhooks WHERE user_id=? ORDER BY created_at,id",
        (handler._notify_uid,),
    ).fetchall()
    return {"webhooks": [_webhook_json(row) for row in rows]}


def _create_webhook(handler, conn, data):
    server, uid = handler.server, handler._notify_uid
    server.throttle(("webhook-write", uid), WEBHOOK_WRITE_LIMIT, WEBHOOK_WRITE_WINDOW)
    count = conn.execute(
        "SELECT count(*) FROM user_webhooks WHERE user_id=?", (uid,)
    ).fetchone()[0]
    if count >= MAX_WEBHOOKS:
        fail(handler, 429, f"每个账号最多配置 {MAX_WEBHOOKS} 个 Webhook")
    name = handler.string(data.get("name", ""), "名称", MAX_NAME_LEN, True)
    raw_url = data.get("url", "")
    if not isinstance(raw_url, str) or not raw_url.strip():
        fail(handler, 400, "请输入 Webhook 地址")
    try:
        safe_url = validate_webhook_url(raw_url)
    except ValueError as exc:
        fail(handler, 400, str(exc))
    try:
        events_text = _clean_events(data.get("events"))
    except ValueError as exc:
        fail(handler, 400, str(exc))
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        fail(handler, 400, "enabled 必须是布尔值")
    hook_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    stamp = _stamp()
    conn.execute(
        "INSERT INTO user_webhooks (id,user_id,name,url,secret,events,enabled,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (hook_id, uid, name, safe_url, secret, events_text, 1 if enabled else 0, stamp, stamp),
    )
    row = conn.execute("SELECT * FROM user_webhooks WHERE id=?", (hook_id,)).fetchone()
    handler._notify_status = 201
    return _webhook_json(row, secret=secret)


def _patch_webhook(handler, conn, hook_id, data):
    server, uid = handler.server, handler._notify_uid
    server.throttle(("webhook-write", uid), WEBHOOK_WRITE_LIMIT, WEBHOOK_WRITE_WINDOW)
    row = _get_owned_webhook(handler, conn, hook_id)
    if not isinstance(data, dict) or not data:
        fail(handler, 400, "没有需要更新的字段")
    name, url, events_text, enabled = None, None, None, None
    if "name" in data:
        name = handler.string(data.get("name"), "名称", MAX_NAME_LEN, True)
    if "url" in data:
        raw_url = data.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            fail(handler, 400, "请输入 Webhook 地址")
        try:
            url = validate_webhook_url(raw_url)
        except ValueError as exc:
            fail(handler, 400, str(exc))
    if "events" in data:
        try:
            events_text = _clean_events(data.get("events"))
        except ValueError as exc:
            fail(handler, 400, str(exc))
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            fail(handler, 400, "enabled 必须是布尔值")
        enabled = 1 if data["enabled"] else 0
    conn.execute(
        "UPDATE user_webhooks SET name=COALESCE(?,name),url=COALESCE(?,url),"
        "events=COALESCE(?,events),enabled=COALESCE(?,enabled),updated_at=? "
        "WHERE id=? AND user_id=?",
        (name, url, events_text, enabled, _stamp(), hook_id, uid),
    )
    refreshed = conn.execute(
        "SELECT * FROM user_webhooks WHERE id=? AND user_id=?", (hook_id, uid)
    ).fetchone()
    # 任何响应都不回传 secret。
    return _webhook_json(refreshed)


def _rotate_secret(handler, conn, hook_id):
    server, uid = handler.server, handler._notify_uid
    server.throttle(("webhook-write", uid), WEBHOOK_WRITE_LIMIT, WEBHOOK_WRITE_WINDOW)
    _get_owned_webhook(handler, conn, hook_id)
    secret = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE user_webhooks SET secret=?,updated_at=? WHERE id=? AND user_id=?",
        (secret, _stamp(), hook_id, uid),
    )
    refreshed = conn.execute(
        "SELECT * FROM user_webhooks WHERE id=? AND user_id=?", (hook_id, uid)
    ).fetchone()
    return _webhook_json(refreshed, secret=secret)


def _test_webhook(handler, conn, hook_id):
    server, uid = handler.server, handler._notify_uid
    # 严格限流：5 次/小时。
    server.throttle(("webhook-test", uid), WEBHOOK_TEST_LIMIT, WEBHOOK_TEST_WINDOW)
    row = _get_owned_webhook(handler, conn, hook_id)
    delivery_id = uuid.uuid4().hex
    payload = {
        "event": EVENT_WEBHOOK_TEST,
        "deliveryId": delivery_id,
        "sentAt": _stamp(),
        "test": True,
    }
    result = send_webhook(
        row["url"], row["secret"], EVENT_WEBHOOK_TEST, delivery_id, payload,
        timeout=TEST_TIMEOUT,
    )
    if result.ok:
        return {"ok": True, "status": result.status}
    return {"ok": False, "error": result.error}


def _delete_webhook(handler, conn, hook_id):
    server, uid = handler.server, handler._notify_uid
    server.throttle(("webhook-write", uid), WEBHOOK_WRITE_LIMIT, WEBHOOK_WRITE_WINDOW)
    _get_owned_webhook(handler, conn, hook_id)
    # webhook_deliveries 通过外键 ON DELETE CASCADE 自动清除。
    conn.execute("DELETE FROM user_webhooks WHERE id=? AND user_id=?", (hook_id, uid))
    return {"ok": True}


_WEBHOOK_ITEM_RE = re.compile(rf"^/api/webhooks/({ID})(?:/(rotate-secret|test))?$")


def dispatch(handler, path, query):
    """自有路由返回 True；不认识的路径返回 False 透传。"""
    method = handler.command
    if not (path == "/api/notifications" or path.startswith("/api/notifications/")
            or path == "/api/webhooks" or path.startswith("/api/webhooks/")):
        return False

    server = handler.server
    # 所有路由统一登录校验，未登录按现有 401 范式抛出。
    with server.db() as conn:
        user = handler.current_user(conn)
    handler._notify_uid = user["id"]
    handler._notify_status = 200

    # 通知路由。
    if path == "/api/notifications":
        if method != "GET":
            fail(handler, 405, "不支持此请求方法")
        with server.db() as conn:
            result = _list_notifications(handler, conn, query)
        handler.json_response(200, result)
        return True
    if path == "/api/notifications/read":
        if method != "POST":
            fail(handler, 405, "不支持此请求方法")
        data = handler.read_json()
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = _mark_read(handler, conn, data)
        handler.json_response(200, result)
        return True
    if path.startswith("/api/notifications/"):
        fail(handler, 404, "接口不存在")

    # Webhook 路由。
    if path == "/api/webhooks":
        if method == "GET":
            with server.db() as conn:
                result = _list_webhooks(handler, conn)
            handler.json_response(200, result)
            return True
        if method == "POST":
            data = handler.read_json()
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                result = _create_webhook(handler, conn, data)
            handler.json_response(handler._notify_status, {"webhook": result})
            return True
        fail(handler, 405, "不支持此请求方法")

    match = _WEBHOOK_ITEM_RE.fullmatch(path)
    if not match:
        fail(handler, 404, "接口不存在")
    hook_id, sub_route = match[1], match[2]
    if sub_route is None:
        if method == "PATCH":
            data = handler.read_json()
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                result = _patch_webhook(handler, conn, hook_id, data)
            handler.json_response(200, {"webhook": result})
            return True
        if method == "DELETE":
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                result = _delete_webhook(handler, conn, hook_id)
            handler.json_response(200, result)
            return True
        fail(handler, 405, "不支持此请求方法")
    if sub_route == "rotate-secret":
        if method != "POST":
            fail(handler, 405, "不支持此请求方法")
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = _rotate_secret(handler, conn, hook_id)
        handler.json_response(200, {"webhook": result})
        return True
    if sub_route == "test":
        if method != "POST":
            fail(handler, 405, "不支持此请求方法")
        # 测试请求不需要请求体；若客户端携带了 JSON 体则消费掉，保持连接可复用。
        if handler.headers.get("Content-Length", "0") != "0":
            handler.read_json()
        with server.db() as conn:
            result = _test_webhook(handler, conn, hook_id)
        # 无论目标成功与否都回 200，结果放在 ok 字段；限流/越权仍走对应错误码。
        handler.json_response(200, result)
        return True
    fail(handler, 404, "接口不存在")
    return True
