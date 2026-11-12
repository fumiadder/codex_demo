# 日历与记录联动实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 月历日期格直接展开待办/记录的标题、首图与标签；日程同步显示图片与标签；标签筛选同时作用于月历与日程；待办支持上传图片，记录支持人工勾选标签。

**Architecture:** 复用现有数据结构：`items` 与 `daily_entries` 保持分表；记录人工标签写入 `meta.tags`；待办图片复用 `files` 表 + 私有 `uploads` 存储命名空间；前端把两类数据合并为日历视图，不新增统一日程接口。AI 标签 `meta.ai.tags` 与人工标签严格隔离。

**Tech Stack:** Python 3（`http.server`、SQLite、`storage_jobs`）、原生 JS、CSS、unittest（stdlib）、Playwright QA。

**对应规格:** `docs/superpowers/specs/2026-11-12-calendar-records-design.md`

**运行约定：**
- 所有命令以 `/workspace/workbench` 为工作目录；
- 测试命令：`python3 -m unittest tests.test_daily tests.test_server -v`（或按任务指定单测）；
- 不要改动 `/workspace/.uploads`；不要把 `.superpowers/` 加入版本控制；提交前用 `git status` 确认暂存内容只包含本任务文件。

---

## 文件地图

| 文件 | 责任 | 涉及任务 |
| --- | --- | --- |
| `workbench/daily.py` | 记录创建/编辑接口增加人工 `tags` 参数与校验 | T1 |
| `workbench/tests/test_daily.py` | 记录标签测试 | T1 |
| `workbench/server.py` | 待办图片上传/删除路由；`item` 响应附带 `images`；`space_tags` 聚合记录标签 | T2、T3、T4 |
| `workbench/tests/test_server.py` | 待办图片、标签聚合、权限测试 | T2、T3、T4 |
| `workbench/public/app.js` | 月历与日程合并渲染、标签筛选、待办编辑器图片、记录编辑器标签、通用大图查看 | T5–T9 |
| `workbench/public/styles.css` | 日期格缩略图/标签样式、待办图片区、移动端适配 | T10 |
| `workbench/scripts/daily_browser_qa.py` | 端到端验证：标签筛选、日历图片、灯箱 | T11 |
| `workbench/scripts/recur_calendar_qa.py` | 回归：重复任务与新字段共存、日期格不溢出 | T11 |

每个任务独立可提交；T1–T4 为后端，可串行；T5 依赖 T1–T4；T6–T9 前端可串行；T10–T11 收尾。

---

## Task 1: 今日记录支持人工标签

**Files:**
- Modify: `workbench/daily.py`（常量区约 37–44、`create_entry` 约 713–767、`patch_entry` 约 770–846）
- Test: `workbench/tests/test_daily.py`（`EntryHandler` 子类追加）

- [ ] **Step 1: 写失败测试**

在 `workbench/tests/test_daily.py` 的 `EntryHandler` 类中追加：

```python
    def test_create_accepts_manual_tags(self):
        status, body, _ = self.request(
            "POST", f"/api/spaces/{'0'*32}/daily",
            {"area": "work", "title": "阅读", "tags": ["学习", "阅读"]}, self.cookie)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["entry"]["meta"]["tags"], ["学习", "阅读"])
        self.assertNotIn("ai", body["entry"]["meta"])

    def test_patch_replaces_tags(self):
        _, body, _ = self.request("POST", f"/api/spaces/{'0'*32}/daily",
            {"area": "work", "title": "阅读", "tags": ["学习"]}, self.cookie)
        entry_id = body["entry"]["id"]
        status, patched, _ = self.request("PATCH", f"/api/spaces/{'0'*32}/daily/{entry_id}",
            {"tags": ["工作", "阅读"]}, self.cookie)
        self.assertEqual(status, 200, patched)
        self.assertEqual(patched["entry"]["meta"]["tags"], ["工作", "阅读"])
        self.assertNotIn("applied", patched["entry"]["meta"].get("ai", {}))

    def test_tags_validation(self):
        for bad in ("学习", [123], ["学习"] * 9, ["超" * 21]):
            with self.subTest(tags=bad):
                status, body, _ = self.request("POST", f"/api/spaces/{'0'*32}/daily",
                    {"area": "work", "title": "x", "tags": bad}, self.cookie)
                self.assertEqual(status, 400, body)
                self.assertIn("标签", body["error"])

    def test_ai_tags_stay_separate(self):
        entry_id = self.seed_entry()
        status, body, _ = self.request("PATCH", f"/api/spaces/{'0'*32}/daily/{entry_id}",
            {"tags": ["学习"]}, self.cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["entry"]["meta"]["tags"], ["学习"])
        self.assertNotIn("tags", body["entry"]["meta"].get("ai", {}))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_daily.EntryHandler.test_create_accepts_manual_tags tests.test_daily.EntryHandler.test_patch_replaces_tags tests.test_daily.EntryHandler.test_tags_validation tests.test_daily.EntryHandler.test_ai_tags_stay_separate -v
```

预期：4 个用例失败（`KeyError: 'tags'` 或断言失败）。

- [ ] **Step 3: 实现标签校验与透传**

在 `daily.py` 常量区（`MAX_BODY` 附近）新增：

```python
MAX_TAGS = 8
MAX_TAG_CHARS = 20


def clean_tags(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("标签必须是数组")
    result = []
    seen = set()
    for tag in raw:
        if not isinstance(tag, str):
            raise ValueError("标签必须是字符串")
        tag = tag.strip()
        if not tag or len(tag) > MAX_TAG_CHARS:
            raise ValueError("每个标签需为 1 至 20 个字符")
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    if len(result) > MAX_TAGS:
        raise ValueError("最多添加 8 个标签")
    return result
```

`create_entry` 中，`db = self.connect()` 之前插入校验：

```python
    try:
        tags = clean_tags(data.get("tags"))
    except ValueError as exc:
        return self.json({"error": str(exc)}, 400)
```

把写入 meta 的 `json.dumps({}, ensure_ascii=False)` 改为：

```python
    json.dumps({"tags": tags}, ensure_ascii=False)
```

`patch_entry` 中，写权限校验之后、读取 `meta` 之后插入：

```python
    try:
        new_tags = clean_tags(data["tags"]) if "tags" in data else None
    except ValueError as exc:
        return self.json({"error": str(exc)}, 400)
    if new_tags is not None:
        meta["tags"] = new_tags
```

注意：`new_tags` 不参与 `meta.ai.applied` 标记逻辑。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_daily -v
```

预期：全部通过，包括既有 `EntryHandler` 与 `EntryEndpoints` 用例。

- [ ] **Step 5: 提交**

```bash
cd /workspace && git add workbench/daily.py workbench/tests/test_daily.py && git commit -m "feat(daily): 今日记录支持人工标签"
```

---

## Task 2: 待办图片上传与删除接口

**Files:**
- Modify: `workbench/server.py`（路由 `do_POST` 约 866、`do_DELETE` 约 1083、`read_upload` 约 1304、`sniff_mime` 约 1275、`api` 路由区约 892）
- Test: `workbench/tests/test_server.py`

- [ ] **Step 1: 写失败测试**

在 `workbench/tests/test_server.py` 中新增类（放在文件末尾，复用 `ServerTestCase` 的 `request/register/member/post_item`）：

```python
    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

    def upload_item_image(self, cookie, item_id, content=None, name="a.png",
                          mime="image/png", filename="a.png"):
        content = self.PNG if content is None else content
        boundary = "folio-image-boundary"
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
               f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode() + content \
            + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", f"/api/items/{item_id}/images", cookie=cookie, raw=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def test_item_image_upload_and_list(self):
        cookie, _, space = self.register()
        _, body, _ = self.post_item(cookie, space["id"])
        item_id = body["item"]["id"]
        status, uploaded, _ = self.upload_item_image(cookie, item_id)
        self.assertEqual(status, 201, uploaded)
        self.assertEqual(len(uploaded["images"]), 1)
        self.assertEqual(uploaded["images"][0]["mime"], "image/png")
        self.assertTrue(uploaded["images"][0]["url"].startswith("/api/files/"))
        status, items, _ = self.request("GET", f"/api/spaces/{space['id']}/items", cookie=cookie)
        self.assertEqual(len(items["items"][0]["images"]), 1)

    def test_item_image_rejects_non_image_and_overflow(self):
        cookie, _, space = self.register()
        _, body, _ = self.post_item(cookie, space["id"])
        item_id = body["item"]["id"]
        status, error, _ = self.upload_item_image(cookie, item_id, content=b"plain text",
            mime="text/plain", filename="a.txt")
        self.assertEqual(status, 400, error)
        for _ in range(9):
            self.assertEqual(self.upload_item_image(cookie, item_id)[0], 201)
        status, error, _ = self.upload_item_image(cookie, item_id)
        self.assertEqual(status, 409, error)
        self.assertIn("9", error["error"])

    def test_item_image_delete(self):
        cookie, _, space = self.register()
        _, body, _ = self.post_item(cookie, space["id"])
        item_id = body["item"]["id"]
        _, uploaded, _ = self.upload_item_image(cookie, item_id)
        file_id = uploaded["images"][0]["id"]
        status, result, _ = self.request("DELETE", f"/api/items/{item_id}/images/{file_id}", cookie=cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["images"], [])
        with self.db as db:
            self.assertIsNone(db.execute("SELECT 1 FROM files WHERE id=?", (file_id,)).fetchone())

    def test_item_image_viewer_cannot_write(self):
        owner, _, space = self.register()
        self.member(owner, space["id"], "viewer@example.com", "viewer")
        viewer, _, _ = self.register("viewer@example.com", "只读")
        _, body, _ = self.post_item(owner, space["id"])
        item_id = body["item"]["id"]
        status, error, _ = self.upload_item_image(viewer, item_id)
        self.assertEqual(status, 403, error)

    def test_item_image_scoped_to_task_and_member(self):
        cookie, _, space = self.register()
        _, body, _ = self.post_item(cookie, space["id"], kind="note")
        note_id = body["item"]["id"]
        status, _, _ = self.upload_item_image(cookie, note_id)
        self.assertEqual(status, 400)
        other, _, _ = self.register("other@example.com")
        _, body, _ = self.post_item(cookie, space["id"])
        status, _, _ = self.upload_item_image(other, body["item"]["id"])
        self.assertEqual(status, 404)

    def test_item_images_do_not_leak_to_media(self):
        cookie, _, space = self.register()
        self.upload(cookie, space["id"], content=b"media bytes", name="m.txt")
        status, items, _ = self.request("GET", f"/api/spaces/{space['id']}/items", cookie=cookie)
        media = [i for i in items["items"] if i["kind"] == "media"][0]
        self.assertNotIn("images", media)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server.ServerTestCase.test_item_image_upload_and_list tests.test_server.ServerTestCase.test_item_image_rejects_non_image_and_overflow -v
```

预期：404（路由不存在）。

- [ ] **Step 3: 实现上传路由**

在 `server.py` 路由分派处（`do_POST` 调 `self.api(...)` 之前的既有 `match = re.fullmatch(r"/api/items/([a-f0-9]{32})", path)` 附近）新增：

```python
        image_match = re.fullmatch(r"/api/items/([a-f0-9]{32})/images", path)
        if image_match and self.command == "POST":
            return self.upload_item_image(image_match[1])
        image_delete = re.fullmatch(r"/api/items/([a-f0-9]{32})/images/([a-f0-9]{32})", path)
        if image_delete and self.command == "DELETE":
            return self.delete_item_image(image_delete[1], image_delete[2])
```

新增方法（放在 `create_item` 附近）：

```python
    ITEM_IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")
    MAX_ITEM_IMAGES = 9

    def item_image_payload(self, db, item_id):
        rows = db.execute(
            "SELECT id,name,mime,size,created_at FROM files "
            "WHERE item_id=? ORDER BY created_at,id", (item_id,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "mime": r["mime"],
                 "size": r["size"], "url": f"/api/files/{r['id']}",
                 "createdAt": r["created_at"]} for r in rows]

    def upload_item_image(self, item_id):
        if not self.user:
            return self.json({"error": "请先登录"}, 401)
        if not self.csrf_valid():
            return self.json({"error": "请求校验失败"}, 403)
        filename, declared, payload = self.read_upload()
        mime = self.sniff_mime(payload, filename, declared)
        if mime not in self.ITEM_IMAGE_MIMES:
            return self.json({"error": "仅支持 PNG、JPEG、GIF、WebP 图片"}, 400)
        if not payload:
            return self.json({"error": "请选择图片"}, 400)
        file_id = uuid.uuid4().hex
        released = False
        try:
            reservation = storage_jobs.reserve(
                self.db_path, "uploads", file_id, payload, owner=self.user["id"])
            with self.db as db:
                item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not item:
                    raise ApiError(404, "待办不存在")
                self.membership(item["space_id"], write=True)
                if item["kind"] != "task":
                    raise ApiError(400, "仅待办支持图片")
                count = db.execute(
                    "SELECT COUNT(*) AS c FROM files WHERE item_id=?", (item_id,)).fetchone()["c"]
                if count >= self.MAX_ITEM_IMAGES:
                    raise ApiError(409, "每条待办最多 9 张图片")
                self.ensure_capacity(item["space_id"], len(payload), 1)
                at = self.now()
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                    (file_id, item["space_id"], item_id, filename, mime, len(payload), "uploads", file_id, at))
                db.execute(
                    "UPDATE spaces SET updated_at=? WHERE id=?", (at, item["space_id"]))
                images = self.item_image_payload(db, item_id)
            storage_jobs.publish(self.db_path, reservation["journal_id"])
            released = True
        except Exception:
            if not released:
                storage_jobs.release(self.db_path, "uploads", file_id)
            raise
        self.json({"image": images[-1], "images": images}, 201)

    def delete_item_image(self, item_id, file_id):
        if not self.user:
            return self.json({"error": "请先登录"}, 401)
        if not self.csrf_valid():
            return self.json({"error": "请求校验失败"}, 403)
        with self.db as db:
            item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise ApiError(404, "待办不存在")
            self.membership(item["space_id"], write=True)
            file = db.execute(
                "SELECT * FROM files WHERE id=? AND item_id=?", (file_id, item_id)).fetchone()
            if not file:
                raise ApiError(404, "图片不存在")
            db.execute("DELETE FROM files WHERE id=?", (file_id,))
            images = self.item_image_payload(db, item_id)
        storage_jobs.queue_delete(self.db_path, file["storage_namespace"], file["storage_key"])
        self.json({"ok": True, "images": images})
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server.ServerTestCase.test_item_image_upload_and_list tests.test_server.ServerTestCase.test_item_image_rejects_non_image_and_overflow tests.test_server.ServerTestCase.test_item_image_delete tests.test_server.ServerTestCase.test_item_image_viewer_cannot_write tests.test_server.ServerTestCase.test_item_image_scoped_to_task_and_member -v
```

预期：`test_item_images_do_not_leak_to_media` 仍失败（列表接口还没带 `images`），其余通过。

- [ ] **Step 5: 提交**

```bash
cd /workspace && git add workbench/server.py workbench/tests/test_server.py && git commit -m "feat(server): 待办图片上传与删除接口"
```

---

## Task 3: 待办响应附带图片数组

**Files:**
- Modify: `workbench/server.py`（`api` 的 items GET 分支约 491–508、`item_payload`、条目 PATCH 响应）
- Test: `workbench/tests/test_server.py`（复用 T2 的 `test_item_image_upload_and_list`、`test_item_images_do_not_leak_to_media`）

- [ ] **Step 1: 实现批量图片装配**

在 `server.py` 新增：

```python
    def attach_item_images(self, db, rows, payloads):
        task_ids = [r["id"] for r in rows if r["kind"] == "task"]
        if not task_ids:
            return
        marks = ",".join("?" * len(task_ids))
        images = db.execute(
            f"SELECT id,item_id,name,mime,size,created_at FROM files "
            f"WHERE item_id IN ({marks}) ORDER BY created_at,id", task_ids).fetchall()
        grouped = {}
        for image in images:
            grouped.setdefault(image["item_id"], []).append({
                "id": image["id"], "name": image["name"], "mime": image["mime"],
                "size": image["size"], "url": f"/api/files/{image['id']}",
                "createdAt": image["created_at"]})
        for payload in payloads:
            if payload["kind"] == "task":
                payload["images"] = grouped.get(payload["id"], [])
```

items GET 分支改为：

```python
        rows = db.execute(...).fetchall()
        payloads = [self.item_payload(r, db) for r in rows]
        self.attach_item_images(db, rows, payloads)
        return self.json({"items": payloads})
```

单条 `PATCH /api/items/{id}` 返回前同样调用：

```python
        payload = self.item_payload(row, db)
        self.attach_item_images(db, [row], [payload])
        return self.json({"item": payload})
```

`POST /api/spaces/{sid}/items` 创建响应同样补上（`images` 为 `[]`）。

- [ ] **Step 2: 跑测试确认通过**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server.ServerTestCase.test_item_image_upload_and_list tests.test_server.ServerTestCase.test_item_images_do_not_leak_to_media -v
```

预期：全部通过。

- [ ] **Step 3: 回归全量后端测试**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server tests.test_daily -v
```

预期：全部通过。

- [ ] **Step 4: 提交**

```bash
cd /workspace && git add workbench/server.py workbench/tests/test_server.py && git commit -m "feat(server): 待办响应附带图片数组"
```

---

## Task 4: 标签聚合包含今日记录

**Files:**
- Modify: `workbench/server.py`（`space_tags` 约 514–534）
- Test: `workbench/tests/test_server.py`

- [ ] **Step 1: 写失败测试**

在 `test_server.py` 追加：

```python
    def test_tags_include_daily_entries_not_ai(self):
        cookie, _, space = self.register()
        sid = space["id"]
        self.task(cookie, sid, meta={"tags": ["学习"]})
        status, _, _ = self.request("POST", f"/api/spaces/{sid}/daily",
            {"area": "work", "title": "阅读", "tags": ["学习", "阅读"]}, cookie=cookie)
        self.assertEqual(status, 201)
        with self.db as db:
            entry_id = db.execute("SELECT id FROM daily_entries").fetchone()["id"]
            db.execute("UPDATE daily_entries SET meta=? WHERE id=?",
                (json.dumps({"tags": ["学习", "阅读"], "ai": {"tags": ["AI标签"]}}), entry_id))
        status, body, _ = self.request("GET", f"/api/spaces/{sid}/tags", cookie=cookie)
        self.assertEqual(status, 200)
        counts = {t["name"]: t["count"] for t in body["tags"]}
        self.assertEqual(counts.get("学习"), 2)
        self.assertEqual(counts.get("阅读"), 1)
        self.assertNotIn("AI标签", counts)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server.ServerTestCase.test_tags_include_daily_entries_not_ai -v
```

预期：`学习` 计数为 1，断言失败。

- [ ] **Step 3: 扩展聚合**

在 `space_tags` 中，处理 `items` 的循环之后、排序之前插入：

```python
        daily_rows = db.execute("SELECT meta FROM daily_entries WHERE space_id=?", (sid,)).fetchall()
        for row in daily_rows:
            try:
                meta = json.loads(row["meta"] or "{}")
            except (TypeError, ValueError):
                continue
            tags = meta.get("tags")
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, str) and tag.strip():
                    counts[tag.strip()] += 1
```

- [ ] **Step 4: 跑测试确认通过并回归**

```bash
cd /workspace/workbench && python3 -m unittest tests.test_server.ServerTestCase.test_tags_include_daily_entries_not_ai tests.test_server.ServerTestCase.test_tags_support_multi_values_legacy_values_and_deletion -v
```

预期：全部通过。

- [ ] **Step 5: 提交**

```bash
cd /workspace && git add workbench/server.py workbench/tests/test_server.py && git commit -m "feat(server): 标签聚合适配今日记录"
```

---

## Task 5: 月历合并待办与记录，接入标签筛选

**Files:**
- Modify: `workbench/public/app.js`（`renderAgenda` 约 4865–4952、`renderCalendar` 约 4954–5104、`loadSpaceTags` 重渲染白名单约 3055）

- [ ] **Step 1: 抽取日历数据装配辅助函数**

在 `renderAgenda` 之前新增：

```python
// 日历视图数据：待办与记录合并，标签筛选共用 itemMatchesTag
function calendarEntries(dateKey, items) {
  const result = [];
  items.forEach((item) => {
    const info = dueInfo(item);
    if (info.dateKey !== dateKey) return;
    if (!itemMatchesTag(item, state.tagFilter)) return;
    result.push({ kind: "item", source: item, time: info.sortable || "" });
  });
  (state.daily.entries || []).forEach((entry) => {
    if (entry.entryDate !== dateKey || entry.area !== state.area) return;
    if (entry.status === "archived") return;
    if (!itemMatchesTag(entry, state.tagFilter)) return;
    result.push({ kind: "daily", source: entry, time: entry.createdAt || "" });
  });
  result.sort((a, b) => (a.kind === b.kind
    ? a.time.localeCompare(b.time)
    : a.kind === "item" ? -1 : 1));
  return result;
}
```

- [ ] **Step 2: 月历模式加载记录**

在 `renderCalendar` 开头补充：

```javascript
  if (state.calendar.mode === "month") {
    if (!state.calendar.month) state.calendar.month = selectedDate().slice(0, 7);
    loadDailyEntries(state.calendar.month);
    loadSpaceTags();
  }
```

`loadSpaceTags` 的重渲染白名单改为 `["tasks", "notes", "calendar"]`。

- [ ] **Step 3: 日期格与详情面板改用合并数据**

`renderCalendar` 中：

- 删除 `const daily = new Map()` 及其待办装配循环，改用 `calendarEntries(key, items)`；
- 详情面板遍历 `calendarEntries(state.calendar.filter, items)`：
  - `kind === "item"` 沿用现有待办行；
  - `kind === "daily"` 渲染记录行（标题、标签、首图），点击 `showDailyEntry`（该函数在 T8 改为读 state 时同步调整调用，不再传参）；
  - 面板底部始终保留「新建待办 / 新建记录」；
  - 空状态文案：`state.tagFilter ? "该日期没有匹配当前标签的内容。" : "这一天还没有安排。"`；
- `renderAgenda` 的按日循环改用 `calendarEntries(dateKey, items)`，每条待办与记录都附加首图、数量与标签 chip（复用 T7 的 `appendEntryMedia`）。

- [ ] **Step 4: 月历顶部接入筛选条**

在 `renderCalendar` 的月历分支，`card` 创建后、`content.append(card)` 前插入 `renderTagFilterBar(content)`。

- [ ] **Step 5: 验证**

启动开发服务器，用浏览器验证：
- 月历日期格能看到记录标题；
- 标签筛选后月历与日程同步收敛；
- 切换标签不改变选中日期与月份。

- [ ] **Step 6: 提交**

```bash
cd /workspace && git add workbench/public/app.js && git commit -m "feat(calendar): 月历合并待办与记录并支持标签筛选"
```

---

## Task 6: 日历与日程显示首图与标签

**Files:**
- Modify: `workbench/public/app.js`、`workbench/public/styles.css`

- [ ] **Step 1: 新增媒体渲染辅助函数**

```javascript
// 首图 + 数量：日历与日程共用的图片展示
function entryImages(target) {
  if (target.kind === "item") return Array.isArray(target.source.images) ? target.source.images : [];
  return (target.source.images || []).map((img) => ({
    id: img.id, name: img.name, mime: img.mime, url: dailyImageURL(img),
  }));
}

function appendEntryMedia(row, target, { compact = false } = {}) {
  const images = entryImages(target);
  if (!images.length) return;
  const buttonNode = el("button", compact ? "entry-thumb compact" : "entry-thumb");
  buttonNode.type = "button";
  buttonNode.setAttribute("aria-label", `查看 ${images.length} 张图片`);
  const img = el("img");
  img.src = images[0].url;
  img.alt = images[0].name || "首图";
  img.loading = "lazy";
  img.addEventListener("error", () => { buttonNode.classList.add("is-broken"); img.remove(); });
  buttonNode.append(img);
  if (images.length > 1) buttonNode.append(el("span", "entry-thumb-count", `${images.length} 张`));
  buttonNode.addEventListener("click", (event) => {
    event.stopPropagation();
    openImageViewer(images, 0);
  });
  row.append(buttonNode);
}

function appendEntryTags(row, target, limit = 2) {
  const tags = itemTags(target.source);
  if (!tags.length) return;
  const wrap = el("span", "entry-tags");
  tags.slice(0, limit).forEach((name) => wrap.append(el("span", "tag", name)));
  if (tags.length > limit) wrap.append(el("span", "entry-tags-more", `+${tags.length - limit}`));
  row.append(wrap);
}
```

- [ ] **Step 2: 通用大图查看**

把现有 `daily-lightbox` 打开逻辑抽为 `openImageViewer(images, startIndex)`：

```javascript
let imageViewerCleanup = null;
function openImageViewer(images, startIndex = 0) {
  if (!images.length) return;
  let index = Math.max(0, Math.min(startIndex, images.length - 1));
  const lightbox = el("div", "daily-lightbox");
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-modal", "true");
  lightbox.setAttribute("aria-label", "查看图片");
  const closeButton = button("关闭", "daily-lightbox-close", () => imageViewerCleanup?.());
  const counter = el("span", "daily-lightbox-counter");
  const previous = button("上一张", "daily-lightbox-nav", () => show(index - 1));
  const image = el("img", "daily-lightbox-image");
  image.alt = "查看大图";
  const next = button("下一张", "daily-lightbox-nav", () => show(index + 1));
  function show(nextIndex) {
    index = (nextIndex + images.length) % images.length;
    image.src = images[index].url;
    counter.textContent = `${index + 1} / ${images.length}`;
    previous.disabled = next.disabled = images.length <= 1;
  }
  const returnFocus = document.activeElement;
  imageViewerCleanup = () => {
    lightbox.remove();
    imageViewerCleanup = null;
    returnFocus?.focus?.();
  };
  lightbox.addEventListener("click", (event) => { if (event.target === lightbox) imageViewerCleanup(); });
  document.addEventListener("keydown", function onKey(event) {
    if (!lightbox.isConnected) { document.removeEventListener("keydown", onKey); return; }
    if (event.key === "Escape") { event.preventDefault(); imageViewerCleanup(); }
    if (event.key === "ArrowLeft") show(index - 1);
    if (event.key === "ArrowRight") show(index + 1);
  });
  lightbox.append(closeButton, counter, previous, image, next);
  $("#app").append(lightbox);
  show(index);
  closeButton.focus();
}
```

`showDailyEntry` 中的图片点击改为 `openImageViewer(entry.images.map(img => ({...img, url: dailyImageURL(img)})), i)`。

- [ ] **Step 3: 接入日期格与日程**

`renderCalendar` 的日期格内容行结构改为：

```javascript
const row = button("", "calendar-entry" + (done ? " is-done" : ""), ...);
row.append(el("span", "calendar-entry-dot"));
const titleSpan = el("span", "calendar-entry-title", title);
row.append(titleSpan);
appendEntryMedia(row, target, { compact: true });
appendEntryTags(row, target, 2);
```

日程行（`agenda-entry`）同样追加 `appendEntryMedia(row, target)` 与 `appendEntryTags(row, target, 3)`。

- [ ] **Step 4: 样式**

`styles.css` 追加：

```css
.calendar-entry { flex-wrap: wrap; row-gap: 2px; }
.calendar-entry-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.entry-thumb { position: relative; width: 40px; height: 40px; flex: 0 0 40px; padding: 0; border: 0; border-radius: 8px; overflow: hidden; background: var(--surface-2, #eee); cursor: zoom-in; }
.entry-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.entry-thumb.compact { width: 28px; height: 28px; flex-basis: 28px; border-radius: 6px; }
.entry-thumb-count { position: absolute; right: 0; bottom: 0; padding: 0 3px; font-size: 10px; line-height: 14px; color: #fff; background: rgba(0,0,0,.65); border-radius: 4px 0 0 0; }
.entry-thumb.is-broken { background: repeating-linear-gradient(45deg, #ddd, #ddd 4px, #eee 4px, #eee 8px); }
.entry-tags { display: inline-flex; gap: 4px; max-width: 100%; overflow: hidden; }
.entry-tags .tag { font-size: 10px; padding: 0 6px; border-radius: 999px; background: var(--tag-bg, #eef); white-space: nowrap; }
.entry-tags-more { font-size: 10px; color: var(--muted); }
@media (max-width: 760px) {
  .calendar-entry { gap: 3px; }
  .entry-thumb.compact { width: 22px; height: 22px; flex-basis: 22px; }
}
```

- [ ] **Step 5: 验证与提交**

浏览器检查首图、数量、标签 chip、灯箱与键盘操作。

```bash
cd /workspace && git add workbench/public/app.js workbench/public/styles.css && git commit -m "feat(calendar): 日期格与日程显示首图和标签"
```

---

## Task 7: 记录编辑器支持勾选标签

**Files:**
- Modify: `workbench/public/app.js`（`showDailyEntry` 约 6981–7104、`showDailyComposer` 提交 payload 约 7295）

- [ ] **Step 1: 编辑弹窗加标签编辑器**

`showDailyEntry` 中，在 `fields.append(titleField, summaryField, ...)` 之后追加：

```javascript
  const tagEditor = buildTagEditor(canWrite ? itemTags(entry) : [], {
    placeholder: "输入标签后回车，如：学习",
    disabled: !canWrite,
  });
  const tagsField = field("标签", tagEditor.node);
  fields.append(tagsField);
```

提交 `payload` 增加 `tags: tagEditor.getTags()`；标签按钮禁用条件同步改为检查 `tagsField` 控件。

- [ ] **Step 2: 快速记录弹窗加标签**

`showDailyComposer` 中 `const form = el("form", ...)` 之后插入 `const composerTags = buildTagEditor([], { placeholder: "标签，如：学习" })`，`fields` 中追加标签字段；`payload` 增加 `tags: composerTags.getTags()`。

- [ ] **Step 3: 保存后刷新标签聚合**

`showDailyEntry` 与 `showDailyComposer` 保存成功、以及 `showDailyEntry` 删除成功的分支中，加：

```javascript
state.tagsLoadedSpace = null;
if (state.view === "calendar" || state.view === "daily") loadSpaceTags();
```

`loadSpaceTags` 重渲染白名单加入 `"daily"`。

- [ ] **Step 4: 展示标签**

`showDailyEntry` 头部与记录卡片中，用 `itemTags(entry)` 渲染 `.tag` chip。

- [ ] **Step 5: 验证与提交**

```bash
cd /workspace && git add workbench/public/app.js && git commit -m "feat(daily): 记录支持勾选标签并接入聚合"
```

---

## Task 8: 待办编辑器支持图片

**Files:**
- Modify: `workbench/public/app.js`（`showEditor` 约 3756–3902、待办列表项 `renderTasks`）
- Modify: `workbench/public/styles.css`

- [ ] **Step 1: 编辑器图片区块**

在 `showEditor` 的表单中、`kind !== "note"` 分支内追加：

```javascript
  const imageList = el("div", "item-image-list");
  const imageInput = el("input");
  imageInput.type = "file";
  imageInput.accept = "image/png,image/jpeg,image/gif,image/webp";
  imageInput.multiple = true;
  const imageStatus = el("p", "form-help", "最多 9 张，单张不超过 25 MB");
  function renderItemImages(images) {
    imageList.replaceChildren();
    images.forEach((image) => {
      const cell = el("span", "item-image-cell");
      const img = el("img");
      img.src = image.url;
      img.alt = image.name;
      cell.append(img);
      const remove = button("删除", "item-image-remove", async () => {
        remove.disabled = true;
        try {
          await api(`/api/items/${item.id}/images/${image.id}`, { method: "DELETE" });
          item.images = item.images.filter((imgItem) => imgItem.id !== image.id);
          renderItemImages(item.images);
        } catch (err) { toast(errorMessage(err), true); remove.disabled = false; }
      });
      remove.type = "button";
      cell.append(remove);
      imageList.append(cell);
    });
  }
```

新建待办时图片选择在保存后逐个上传（`for ... of imageInput.files`，`FormData` 字段名 `file`，端点 `POST /api/items/{id}/images`）；已有待办选择后立即上传并刷新 `item.images`。失败时 toast 并保留已创建的待办。

- [ ] **Step 2: 待办列表显示图片标记**

`renderTasks` 的条目中，当 `item.images?.length` 时显示首图缩略图与数量，点击打开 `openImageViewer`。

- [ ] **Step 3: 样式**

```css
.item-image-list { display: flex; flex-wrap: wrap; gap: 8px; }
.item-image-cell { position: relative; width: 72px; height: 72px; }
.item-image-cell img { width: 100%; height: 100%; object-fit: cover; border-radius: 8px; }
.item-image-remove { position: absolute; top: -6px; right: -6px; font-size: 10px; }
```

- [ ] **Step 4: 验证与提交**

```bash
cd /workspace && git add workbench/public/app.js workbench/public/styles.css && git commit -m "feat(tasks): 待办支持上传与删除图片"
```

---

## Task 9: 演示模式对齐

**Files:**
- Modify: `workbench/public/app.js`（`demoDailyEntries` 约 5846、`demoStore` 中待办种子）

- [ ] **Step 1:** `demoDailyEntries` 的每条记录 `meta` 增加 `tags`（如 `["学习"]`、`["工作"]`），演示空间 `state.spaceTags` 由 `loadSpaceTags` 的演示分支聚合（当前演示分支直接返回空，需改为扫描 `state.demoStore` 与 `state.demoDaily`）。
- [ ] **Step 2:** 演示待办图片用 `URL.createObjectURL` 生成的对象 URL 存入 `item.images`（结构与真实接口一致），对象 URL 注册进 `state.objectURLs` 便于统一释放。
- [ ] **Step 3:** 演示模式下待办上传/删除走本地分支：文件读为对象 URL 后更新 `item.images`，不发起网络请求。
- [ ] **Step 4: 验证与提交**

```bash
cd /workspace && git add workbench/public/app.js && git commit -m "feat(demo): 演示模式支持标签与待办图片"
```

---

## Task 10: 响应式与视觉回归

**Files:**
- Modify: `workbench/public/styles.css`

- [ ] **Step 1:** 375px 宽度下检查月历：日期格不横向溢出、标签不遮挡日期数字、首图不放大日期格；必要时缩小 `.calendar-entry` 的 `gap` 与字体。
- [ ] **Step 2:** 1920px 宽度下检查：日期格内容行最多 3 条 + `+N`，详情面板按钮不被挤出卡片。
- [ ] **Step 3:** 长标题（>40 字）与 8 个标签的记录：标题省略、标签 `+N` 折叠。
- [ ] **Step 4: 提交**

```bash
cd /workspace && git add workbench/public/styles.css && git commit -m "style(calendar): 日期格图片与标签的响应式适配"
```

---

## Task 11: 端到端 QA

**Files:**
- Modify: `workbench/scripts/daily_browser_qa.py`、`workbench/scripts/recur_calendar_qa.py`

- [ ] **Step 1:** `daily_browser_qa.py` 增加场景：
  1. 创建带 `tags=["学习"]` 与 2 张图片的记录；
  2. 打开月历，断言日期格出现首图、`2 张` 与 `学习` chip；
  3. 点击标签 `学习`，断言月历与日程只剩该标签内容；
  4. 点击首图，断言灯箱出现且可切换 2 张图片；
  5. 再次点击 `学习` 取消筛选，断言全部内容恢复。
- [ ] **Step 2:** `recur_calendar_qa.py` 增加：重复任务与新 `images` 字段共存时日期格布局不溢出；标签筛选不影响重复任务的重复展开。
- [ ] **Step 3: 运行 QA**

```bash
cd /workspace/workbench && python3 scripts/daily_browser_qa.py && python3 scripts/recur_calendar_qa.py
```

预期：全部通过，无控制台错误。

- [ ] **Step 4: 全量回归**

```bash
cd /workspace/workbench && python3 -m unittest discover -s tests -v
```

预期：全部通过。

- [ ] **Step 5: 提交**

```bash
cd /workspace && git add workbench/scripts/daily_browser_qa.py workbench/scripts/recur_calendar_qa.py && git commit -m "test(calendar): 日历标签与图片的端到端 QA"
```

---

## 自检记录

- 规格 §3.1 日期格 → T5、T6、T10；§3.2 标签筛选 → T4、T5、T7；§3.3 日程 → T5、T6；§3.4 编辑器 → T7、T8；
- 规格 §4.1 记录标签 → T1；§4.2 待办图片 → T2、T3；§4.3 标签聚合 → T4；
- 规格 §5 接口 → T1–T4；§6 前端 → T5–T9；§7 边界 → T2（图片校验）、T6（图片加载失败占位）、T7（旧数据无标签）；
- 规格 §8 测试 → T1–T4 单测、T11 QA；
- 演示模式（规格 §6 最后一行）→ T9。
