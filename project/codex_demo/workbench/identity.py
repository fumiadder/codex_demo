"""Account identity: username / phone sign-in on top of the existing users row.

The `users` table keeps its shape (id + email + name + password_hash) because
dozens of tables reference `users.id`; rebuilding it would require disabling
foreign keys and rewriting every dependent row. Instead this module adds one
additive table that carries the login identifiers, so the change is purely
incremental and an older image simply ignores it.

Migration is automatic on startup: every existing account whose email prefix is
unique becomes a username, so the current operator can sign in with it.
"""
from __future__ import annotations

import re
import sys

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_identities (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 username TEXT UNIQUE,
 phone TEXT UNIQUE,
 updated_at TEXT NOT NULL
);
"""

USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{2,29}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
MAX_USERNAME = 30
MIN_USERNAME = 3


def fail(handler, status, message):
    error = getattr(handler.server, "identity_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def initialize(server):
    """Bind APIError from the module that actually runs the server."""
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:  # test subclasses may live in another module
        from server import APIError as error
    server.identity_error = error


def normalize_username(raw):
    if not isinstance(raw, str):
        raise ValueError("用户名必须是文本")
    value = raw.strip()
    if not MIN_USERNAME <= len(value) <= MAX_USERNAME:
        raise ValueError(f"用户名需为 {MIN_USERNAME} 到 {MAX_USERNAME} 个字符")
    if not USERNAME_RE.match(value):
        raise ValueError("用户名只能包含字母、数字、下划线、点和短横线，且需以字母或数字开头")
    return value


def normalize_phone(raw):
    if not isinstance(raw, str):
        raise ValueError("手机号必须是文本")
    value = raw.strip()
    if not value:
        return ""
    if not PHONE_RE.match(value):
        raise ValueError("请输入有效的 11 位手机号")
    return value


def normalize_identity_input(data):
    """Extract username/phone from a registration payload.

    A username is preferred, but a legacy caller that only supplies an email is
    accepted: the address prefix becomes the username so existing scripts and
    tests keep working through the transition.
    """
    raw_username = data.get("username", "")
    raw_phone = data.get("phone", "")
    if raw_username and "@" in raw_username:
        # A caller still passing an address in the username slot (existing QA
        # scripts and operator tooling): treat it as email and derive the name.
        derived = email_prefix(raw_username.strip().lower())
        if not derived:
            raise ValueError("用户名需为 3 到 30 个字符")
        if len(derived) < MIN_USERNAME:
            derived = (derived + "_user")[:MAX_USERNAME]
        username = normalize_username(derived)
        # Mutate the caller's payload in place so the email and the derived-name
        # marker reach the registration code that reads the same dict.
        data.setdefault("email", raw_username.strip().lower())
        if not data.get("email"):
            data["email"] = raw_username.strip().lower()
        data["_derived_from_email"] = True
    elif raw_username:
        username = normalize_username(raw_username)
    else:
        derived = email_prefix((data.get("email") or "").strip().lower())
        if not derived:
            raise ValueError("请填写用户名（3 到 30 个字符）")
        # Keep deriving usable names even for very short prefixes such as
        # "a@example.com"; pad with the account-independent suffix below.
        if len(derived) < MIN_USERNAME:
            derived = (derived + "_user")[:MAX_USERNAME]
        username = normalize_username(derived)
    phone = normalize_phone(raw_phone)
    return username, phone


def email_prefix(email):
    """Derive a candidate username from an email, or '' when unusable."""
    if not isinstance(email, str) or "@" not in email:
        return ""
    prefix = email.split("@", 1)[0]
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "", prefix).strip(".-_")
    if not prefix:
        return ""
    return prefix[:MAX_USERNAME]


def _taken(conn, username):
    return conn.execute(
        "SELECT 1 FROM user_identities WHERE username=?", (username,)
    ).fetchone() is not None


def migrate_existing(conn, *, now):
    """Give every account without an identity row a username from its email.

    Runs once per installation: accounts that already have a row are skipped, so
    it is safe to call on every startup. Collisions fall back to a suffixed name
    and, as a last resort, to the account id.
    """
    created = 0
    rows = conn.execute(
        "SELECT u.id, u.email FROM users u "
        "LEFT JOIN user_identities i ON i.user_id = u.id "
        "WHERE i.user_id IS NULL"
    ).fetchall()
    for row in rows:
        base = email_prefix(row["email"])
        candidates = []
        if base:
            candidates.append(base)
            for suffix in range(2, 50):
                candidates.append(f"{base[:MAX_USERNAME - len(str(suffix)) - 1]}_{suffix}")
        candidates.append(f"user_{row['id'][:12]}")
        chosen = None
        for candidate in candidates:
            try:
                candidate = normalize_username(candidate)
            except ValueError:
                continue
            if not _taken(conn, candidate):
                chosen = candidate
                break
        if chosen is None:  # pragma: no cover - cannot happen with the id fallback
            continue
        conn.execute(
            "INSERT INTO user_identities (user_id, username, phone, updated_at) "
            "VALUES (?,?,?,?)",
            (row["id"], chosen, None, now),
        )
        created += 1
    return created


def lookup_user_id(conn, identifier):
    """Resolve a login identifier (username or phone) to a user id, or None."""
    if not isinstance(identifier, str):
        return None
    value = identifier.strip()
    if not value:
        return None
    row = conn.execute(
        "SELECT user_id FROM user_identities WHERE username=? OR phone=?",
        (value, value),
    ).fetchone()
    return row["user_id"] if row else None


def identity_of(conn, user_id):
    row = conn.execute(
        "SELECT username, phone FROM user_identities WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None:
        return {"username": None, "phone": None}
    return {"username": row["username"], "phone": row["phone"]}


def describe_user(conn, user_id):
    """Merge the login identity into the profile returned to the browser."""
    ident = identity_of(conn, user_id)
    display = ident["username"] or ident["phone"] or ""
    return {"username": ident["username"], "phone": ident["phone"], "login": display}


def validate_new_identity(conn, username, phone, *, explicit=False):
    """Validate and reserve-check a brand new username/phone pair.

    When the name was derived from a legacy email (``explicit`` is False) a
    collision is resolved by adding a numeric suffix instead of failing, so
    existing email-based registration keeps working. An explicitly chosen name
    always fails loudly.
    """
    uname = normalize_username(username)
    tel = normalize_phone(phone)
    if _taken(conn, uname):
        if explicit:
            raise ValueError("该用户名已被使用")
        for suffix in range(2, 100):
            candidate = f"{uname[:MAX_USERNAME - len(str(suffix)) - 1]}_{suffix}"
            if not _taken(conn, candidate):
                uname = candidate
                break
        else:
            raise ValueError("该用户名已被使用")
    if tel:
        row = conn.execute(
            "SELECT 1 FROM user_identities WHERE phone=?", (tel,)
        ).fetchone()
        if row:
            raise ValueError("该手机号已被使用")
    return uname, tel


def create_identity(conn, user_id, username, phone, *, now):
    conn.execute(
        "INSERT INTO user_identities (user_id, username, phone, updated_at) "
        "VALUES (?,?,?,?)",
        (user_id, username, phone or None, now),
    )


def update_identity(conn, user_id, *, username=None, phone=None, now):
    """Update one or both identifiers, enforcing uniqueness."""
    sets, values = [], []
    if username is not None:
        row = conn.execute(
            "SELECT user_id FROM user_identities WHERE username=?", (username,)
        ).fetchone()
        if row and row["user_id"] != user_id:
            raise ValueError("该用户名已被使用")
        sets.append("username=?")
        values.append(username)
    if phone is not None:
        row = conn.execute(
            "SELECT user_id FROM user_identities WHERE phone=?", (phone,)
        ).fetchone()
        if row and row["user_id"] != user_id:
            raise ValueError("该手机号已被使用")
        sets.append("phone=?")
        values.append(phone or None)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([now, user_id])
    conn.execute(
        "UPDATE user_identities SET " + ", ".join(sets) + " WHERE user_id=?", values
    )


def dispatch(handler, path, query):
    """Own /api/me/identity; return False for every other path."""
    if path != "/api/me/identity":
        return False
    method = handler.command
    if method != "PATCH":
        fail(handler, 405, "不支持此请求方法")
    data = handler.read_json()
    if not isinstance(data, dict):
        fail(handler, 400, "请求内容必须为对象")
    server = handler.server
    with server.db() as conn:
        user = handler.current_user(conn)
        uid = user["id"]
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        password = data.get("password", "")
        from server import verify_password

        if not isinstance(password, str) or not verify_password(password, row["password_hash"]):
            fail(handler, 400, "密码错误，请重新输入")
        current = identity_of(conn, uid)
        try:
            username = None
            if "username" in data:
                username = normalize_username(data.get("username"))
            phone = None
            if "phone" in data:
                phone = normalize_phone(data.get("phone"))
        except ValueError as exc:
            fail(handler, 400, str(exc))
        if username is not None and username != current["username"]:
            pass
        if phone is not None and phone == current["phone"]:
            phone = None
        if username is None and phone is None:
            fail(handler, 400, "没有需要更新的登录方式")
        if current["username"] is None and username is None:
            fail(handler, 400, "请先设置用户名")
        conn.execute("BEGIN IMMEDIATE")
        try:
            update_identity(conn, uid, username=username, phone=phone, now=_now())
        except ValueError as exc:
            raise APIError_400(handler, str(exc))
        result = describe_user(conn, uid)
    handler.json_response(200, {"identity": result})
    return True


def APIError_400(handler, message):
    error = getattr(handler.server, "identity_error", None)
    if error is None:
        from server import APIError as error
    return error(400, message)


def _now():
    from server import now_iso

    return now_iso()