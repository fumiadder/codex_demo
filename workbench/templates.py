"""任务/笔记模板库：内置预设（只读常量）+ 每账号自定义模板。

模块自有一张 ``item_templates`` 表、独立路由与维护入口，复用 server 的会话
鉴权、CSRF 与 SQLite/PostgreSQL 持久层（? 占位由 persistence 自动转换为 %s）。
内置模板只存在于代码常量中，绝不落库、不写迁移数据；自定义模板通过
``user_id`` 外键 ``ON DELETE CASCADE`` 随账号注销自动清零。

路由（全部需要登录，未登录按现有 401 范式拒绝）：
- GET    /api/templates          内置模板（固定顺序在前）+ 本人自定义模板
- POST   /api/templates          创建本人自定义模板（每账号上限 50 个）
- PATCH  /api/templates/:id      更新本人自定义模板
- DELETE /api/templates/:id      删除本人自定义模板

``builtin-`` 前缀的资源只读，PATCH/DELETE 一律 404；他人资源与不存在资源
统一 404，不暴露存在性。模块无常驻线程、无网络访问、无第三方依赖。
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid

# 与 items 的既有约束保持同一套口径，长度上限在路由层强制。
MAX_NAME_LEN = 60
MAX_TITLE_LEN = 200
MAX_BODY_LEN = 20_000
META_MAX_BYTES = 16 * 1024
MAX_TEMPLATES_PER_USER = 50

KINDS = ("task", "note", "media")
AREAS = ("work", "life")
BUILTIN_PREFIX = "builtin-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_templates(
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('task','note','media')),
 area TEXT NOT NULL CHECK(area IN ('work','life')),
 title TEXT NOT NULL DEFAULT '',
 body TEXT NOT NULL DEFAULT '',
 meta TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS item_templates_user ON item_templates(user_id, updated_at DESC);
"""

# 子任务示例使用固定的 32 位小写十六进制 id，确保通过 validate_meta_extensions。
_SUBTASK_ID_1 = "0123456789abcdef0123456789abcdef"
_SUBTASK_ID_2 = "fedcba9876543210fedcba9876543210"
_SUBTASK_ID_3 = "11112222333344445555666677778888"

# 内置预设：顺序即展示顺序，仅存在于本常量中，不写入数据库。
# 每项 meta 都必须能通过 FolioHandler.validate_meta_extensions 校验。
BUILTIN_TEMPLATES = (
    {
        "id": "builtin-daily-standup",
        "builtin": True,
        "name": "每日站会",
        "kind": "task",
        "area": "work",
        "title": "每日站会",
        "body": (
            "## 昨日完成\n- \n\n## 今日计划\n1. \n2. \n\n## 需要协助/阻塞\n- "
        ),
        "meta": {"tags": ["站会", "协作"]},
    },
    {
        "id": "builtin-weekly-report",
        "builtin": True,
        "name": "周报",
        "kind": "note",
        "area": "work",
        "title": "本周工作周报",
        "body": (
            "## 本周重点\n1. \n2. \n\n## 进展与数据\n- \n\n## 问题与风险\n- "
            "\n\n## 下周计划\n1. \n2. "
        ),
        "meta": {"tags": ["周报"]},
    },
    {
        "id": "builtin-meeting-minutes",
        "builtin": True,
        "name": "会议纪要",
        "kind": "note",
        "area": "work",
        "title": "会议纪要",
        "body": (
            "## 会议信息\n- 时间：\n- 参会人：\n\n## 讨论要点\n1. \n\n## 决议事项\n- "
            "\n\n## 行动项\n- [ ] 负责人 / 截止日期："
        ),
        "meta": {"tags": ["会议"]},
    },
    {
        "id": "builtin-project-retro",
        "builtin": True,
        "name": "项目复盘",
        "kind": "note",
        "area": "work",
        "title": "项目复盘",
        "body": (
            "## 目标与结果\n- 预期：\n- 实际：\n\n## 做得好的\n1. \n\n## 待改进\n1. "
            "\n\n## 下次行动\n- [ ] "
        ),
        "meta": {"tags": ["复盘", "项目"]},
    },
    {
        "id": "builtin-reading-list",
        "builtin": True,
        "name": "读书清单",
        "kind": "task",
        "area": "life",
        "title": "读书清单",
        "body": "## 想读\n- \n\n## 在读\n- \n\n## 读书笔记与摘录\n- ",
        "meta": {"tags": ["阅读"]},
    },
    {
        "id": "builtin-shopping-list",
        "builtin": True,
        "name": "购物清单",
        "kind": "task",
        "area": "life",
        "title": "购物清单",
        "body": "出门前核对一遍，买完逐项勾选。",
        "meta": {
            "subtasks": [
                {"id": _SUBTASK_ID_1, "title": "蔬菜与水果", "done": False},
                {"id": _SUBTASK_ID_2, "title": "牛奶和鸡蛋", "done": False},
                {"id": _SUBTASK_ID_3, "title": "日用品补给", "done": False},
            ]
        },
    },
    {
        "id": "builtin-fitness-plan",
        "builtin": True,
        "name": "健身计划",
        "kind": "task",
        "area": "life",
        "title": "每周健身计划",
        "body": "## 训练内容\n- 热身 10 分钟\n- 力量训练 40 分钟\n- 拉伸 10 分钟\n\n## 身体记录\n- 体重：\n- 备注：",
        "meta": {"tags": ["健身"], "recurrence": {"freq": "weekly", "byday": ["MO", "WE", "FR"]}},
    },
    {
        "id": "builtin-bedtime-review",
        "builtin": True,
        "name": "睡前复盘",
        "kind": "note",
        "area": "life",
        "title": "睡前复盘",
        "body": (
            "## 今天三件好事\n1. \n2. \n3. \n\n## 今天的遗憾\n- \n\n## 明天最重要的一件事\n- "
        ),
        "meta": {"tags": ["复盘", "睡眠"]},
    },
    {
        "id": "builtin-travel-prep",
        "builtin": True,
        "name": "旅行准备",
        "kind": "task",
        "area": "life",
        "title": "旅行准备清单",
        "body": "## 行程\n- 往返交通：\n- 住宿：\n\n## 注意事项\n- 证件与应急联系方式随身带。",
        "meta": {"tags": ["旅行"]},
    },
    {
        "id": "builtin-idea-inbox",
        "builtin": True,
        "name": "灵感收集",
        "kind": "note",
        "area": "life",
        "title": "灵感收集箱",
        "body": "## 随手记录\n- \n\n## 可落地的想法\n1. \n\n## 下一步\n- [ ] ",
        "meta": {"tags": ["灵感"]},
    },
)

_COLLECTION_PATH = "/api/templates"
_ITEM_RE = re.compile(r"^/api/templates/([^/]+)$")


# --------------------------------------------------------------------------- #
# 独立 dispatch：自带鉴权，返回 False 透传给其他模块（范式对齐 notify/export）
# --------------------------------------------------------------------------- #
def initialize(server):
    """与 notify/export 保持一致的初始化入口；本模块无目录或线程初始化。"""
    error = getattr(sys.modules.get(type(server).__module__), "APIError", None)
    if error is None:  # 测试子类可能位于其他模块
        from server import APIError as error
    server.templates_error = error


def fail(handler, status, message):
    error = getattr(handler.server, "templates_error", None)
    if error is None:
        from server import APIError as error
    raise error(status, message)


def _stamp():
    """UTC ISO-8601 秒级时间戳，形状与 server.now_iso() 一致（如 2026-11-07T04:42:00Z）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _template_json(row):
    """数据库行转 camelCase JSON；meta 脏数据兜底为空对象，风格对齐 item_json。"""
    try:
        meta = json.loads(row["meta"])
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "area": row["area"],
        "title": row["title"],
        "body": row["body"],
        "meta": meta,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "builtin": False,
    }


def _prepare_payload(handler, data, current=None):
    """POST/PATCH 共用的字段校验，返回可直接落库的六元组。

    复用 handler.string 的长度/NUL/必填校验与 validate_meta_extensions 的
    recurrence/subtasks/tags 校验；非法时抛 APIError 400 并正常向上传播。
    """
    current = current or {}
    name = handler.string(data.get("name", current.get("name", "")), "模板名称", MAX_NAME_LEN, True)
    kind = data.get("kind", current.get("kind"))
    if kind not in KINDS:
        fail(handler, 400, "模板类型无效")
    area = data.get("area", current.get("area"))
    if area not in AREAS:
        fail(handler, 400, "请选择工作或生活分类")
    title = handler.string(data.get("title", current.get("title", "")), "标题", MAX_TITLE_LEN)
    body = handler.string(data.get("body", current.get("body", "")), "内容", MAX_BODY_LEN)
    meta = data["meta"] if "meta" in data else current.get("meta", {})
    if not isinstance(meta, dict):
        fail(handler, 400, "meta 必须为对象")
    # 非法 recurrence/subtasks/tags 会抛 APIError 400，让其正常传播。
    handler.validate_meta_extensions(meta)
    meta_str = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(meta_str.encode("utf-8")) > META_MAX_BYTES:
        fail(handler, 400, "附加信息超过大小限制")
    return name, kind, area, title, body, meta_str


def _get_owned_template(handler, conn, uid, template_id):
    """仅返回本人自定义模板；内置模板、他人资源与不存在资源统一 404。"""
    if template_id.startswith(BUILTIN_PREFIX):
        fail(handler, 404, "模板不存在或无权访问")
    row = conn.execute(
        "SELECT * FROM item_templates WHERE id=? AND user_id=?",
        (template_id, uid),
    ).fetchone()
    if row is None:
        fail(handler, 404, "模板不存在或无权访问")
    return row


def _list_templates(conn, uid):
    rows = conn.execute(
        "SELECT * FROM item_templates WHERE user_id=? ORDER BY updated_at DESC,id DESC",
        (uid,),
    ).fetchall()
    # 内置模板固定顺序在前；拷贝一份避免调用方意外改写模块常量。
    templates = [dict(item, builtin=True) for item in BUILTIN_TEMPLATES]
    templates.extend(_template_json(row) for row in rows)
    return {"templates": templates}


def _create_template(handler, conn, uid, data):
    count = conn.execute(
        "SELECT count(*) FROM item_templates WHERE user_id=?", (uid,)
    ).fetchone()[0]
    if count >= MAX_TEMPLATES_PER_USER:
        fail(handler, 400, f"每个账号最多保存 {MAX_TEMPLATES_PER_USER} 个自定义模板")
    name, kind, area, title, body, meta_str = _prepare_payload(handler, data)
    template_id = uuid.uuid4().hex
    stamp = _stamp()
    conn.execute(
        "INSERT INTO item_templates (id,user_id,name,kind,area,title,body,meta,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (template_id, uid, name, kind, area, title, body, meta_str, stamp, stamp),
    )
    row = conn.execute(
        "SELECT * FROM item_templates WHERE id=? AND user_id=?", (template_id, uid)
    ).fetchone()
    return _template_json(row)


def _patch_template(handler, conn, uid, template_id, data):
    row = _get_owned_template(handler, conn, uid, template_id)
    if not isinstance(data, dict) or not data:
        fail(handler, 400, "没有需要更新的字段")
    current = dict(row)
    try:
        current["meta"] = json.loads(row["meta"])
    except (ValueError, TypeError):
        current["meta"] = {}
    name, kind, area, title, body, meta_str = _prepare_payload(handler, data, current)
    stamp = _stamp()
    changed = conn.execute(
        "UPDATE item_templates SET name=?,kind=?,area=?,title=?,body=?,meta=?,updated_at=? "
        "WHERE id=? AND user_id=?",
        (name, kind, area, title, body, meta_str, stamp, template_id, uid),
    ).rowcount
    if not changed:
        fail(handler, 404, "模板不存在或无权访问")
    refreshed = conn.execute(
        "SELECT * FROM item_templates WHERE id=? AND user_id=?", (template_id, uid)
    ).fetchone()
    return _template_json(refreshed)


def _delete_template(handler, conn, uid, template_id):
    _get_owned_template(handler, conn, uid, template_id)
    conn.execute(
        "DELETE FROM item_templates WHERE id=? AND user_id=?", (template_id, uid)
    )
    return {"ok": True}


def dispatch(handler, path, query):
    """自有路由返回 True；不认识的路径返回 False 透传。"""
    if path != _COLLECTION_PATH and not path.startswith(_COLLECTION_PATH + "/"):
        return False

    server = handler.server
    method = handler.command
    # 所有路由统一登录校验，未登录按现有 401 范式抛出（current_user 抛 APIError）。
    with server.db() as conn:
        user = handler.current_user(conn)
    uid = user["id"]

    if path == _COLLECTION_PATH:
        if method == "GET":
            with server.db() as conn:
                result = _list_templates(conn, uid)
            handler.json_response(200, result)
            return True
        if method == "POST":
            data = handler.read_json()
            with server.db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                template = _create_template(handler, conn, uid, data)
            handler.json_response(201, {"template": template})
            return True
        fail(handler, 405, "不支持此请求方法")

    match = _ITEM_RE.fullmatch(path)
    if not match:
        fail(handler, 404, "接口不存在")
    template_id = match.group(1)

    if method == "PATCH":
        data = handler.read_json()
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            template = _patch_template(handler, conn, uid, template_id, data)
        handler.json_response(200, {"template": template})
        return True
    if method == "DELETE":
        with server.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = _delete_template(handler, conn, uid, template_id)
        handler.json_response(200, result)
        return True
    fail(handler, 405, "不支持此请求方法")
    return True
