# 待办「每天定时提醒」实施计划

## Context（为什么做）

用户希望给待办事项增加「每天定时提醒」：设置一个时刻，到点每天提醒，直到任务完成。

现有工作台已具备完整通知体系，本需求**不新建任何投递通道、不新增数据表、不引入新依赖**，只在现有定时提醒（一次性 `meta.remindAt`）的链路上增加「每日重复」语义：

- 通知生成在 [notify.py](file:///workspace/workbench/notify.py) `_scan_user` / `_persist_notifications`，由 [server.py](file:///workspace/workbench/server.py) `maybe_cleanup`（每 60s 节流，认证请求驱动）调用 `notify.run_maintenance`。
- 三通道已齐备且复用同一条通知：站内铃铛（notifications 表）、手机 Web Push（`webpush.deliver`）、出站 Webhook（`_enqueue_for_hooks`，事件 `task.due`）。
- 已确认产品语义：**任务标记为已完成/删除后停止每日提醒**（现有扫描 `status<>'done'` 已天然满足）；三通道全部启用。
- 移动端约束：安卓微信内置浏览器不支持 Web Push，需系统 Chrome 开启「手机提醒」；不支持时仍有应用内通知与 Webhook（可接 Bark/Server酱/企微/飞书）。

## 数据设计

任务 meta 新增一个字段（自由 JSON，零表结构变更，符合 AGENTS.md 约定）：

- `meta.remindDaily`：字符串，固定 `HH:MM`（24 小时制，上海时区，与服务端 `SHANGHAI_TZ` 一致）。空串/缺省 = 不启用每日提醒。
- 与一次性 `meta.remindAt` 相互独立，可同时存在；每日提醒优先级更高（设置了合法每日提醒后，到点当天不再走一次性/到期兜底，避免同一天重复打扰）。
- 每日去重键：`task-daily:{itemId}:{YYYY-MM-DD}`，日期取上海时区当天，天然保证「每天最多一条」且跨天自动重新触发。

## 改动点

### 1. 服务端校验 — [server.py](file:///workspace/workbench/server.py#L529-L539) `validate_meta_extensions`

在现有 `remindAt` 校验旁增加：`remindDaily` 存在且非空时，必须是匹配 `^\d{2}:\d{2}$` 且 `00<=HH<=23`、`00<=MM<=59` 的合法时刻，否则抛 `APIError(400, "每日提醒时间格式应为 HH:MM")`。空字符串放行（用于关闭）。

### 2. 扫描生成 — [notify.py](file:///workspace/workbench/notify.py#L326-L384) `_scan_user`

- 新增纯函数辅助（便于单测）：
  - `_parse_daily_time(value) -> (hh, mm) | None`：校验 `HH:MM`，非法返回 None。
  - `_daily_moment_today(hh, mm, now) -> int`：用上海时区当天日期 + hh:mm，经 `_parse_remind_at` 同款本地时间换算为 Unix 秒（保持与一次性提醒一致的时区处理）。
- 在每个候选任务的循环里、现有 `remindAt` 分支**之前**插入每日提醒分支：
  - 解析 `meta.remindDaily`；合法则算出今天该时刻 `moment`。
  - 当 `0 <= now - moment <= REMIND_GRACE_SECONDS`（复用现有 15 分钟补发窗口）且去重键 `task-daily:{id}:{today}` 不存在 → 加入 pending 并 `continue`。
  - 当天时刻还没到（`moment > now`）也 `continue`，避免回退到到期兜底造成同日重复；非法格式则不拦截，继续走原逻辑。
- `_persist_notifications` 中按 `dedup_key.startswith("task-daily:")` 分支生成中文文案与 payload：
  - body：`任务「{title}」的每日提醒时间到了（{HH:MM}）。`
  - payload 增加 `remindDaily: "HH:MM"`，保留 `itemId/spaceId/title/body`，使 Web Push 与 Webhook 无需改动即可投递。

### 3. 前端任务编辑器 — [app.js](file:///workspace/workbench/public/app.js#L3842-L3880)

- 在 `let ... remindAt;` 声明处增加 `remindDaily`；任务分支内新增 `input("time", ...)`，初值取 `item?.meta?.remindDaily || ""`，`aria-label="每日提醒时间"`，用 `labelField("每日提醒（可选，每天到点提醒）", remindDaily)` 放在一次性「提醒时间」字段之后。
- 保存逻辑（[app.js](file:///workspace/workbench/public/app.js#L4121-L4123) 同款）：`if (remindDaily.value) nextMeta.remindDaily = remindDaily.value; else delete nextMeta.remindDaily;`
- 演示模式（state.demo）无服务端维护，不产生推送，保持现状即可，不额外造假数据。

## TDD 测试清单（先写失败测试，再实现）

[tests/test_notify.py](file:///workspace/workbench/tests/test_notify.py)（沿用 `mock.patch.object(notify, "_now", ...)` 与 `_today` 模拟时间的现有模式）新增：

1. `test_daily_reminder_fires_once_today_and_repeats_next_day`：设 `remindDaily`，把 `_now` 拨到当天时刻之后（grace 内）→ 生成 1 条；再跑一次不重复；把 `_today`/`_now` 拨到次日同时刻 → 再生成 1 条，去重键日期不同。
2. `test_daily_reminder_future_time_silent`：时刻在未来 → 不生成、且不回退产生到期通知。
3. `test_done_task_daily_reminder_suppressed`：status=done → 不生成（验证「完成即停止」）。
4. `test_invalid_daily_time_falls_through`：脏值不报错，按原有到期逻辑处理。
5. `test_daily_reminder_payload_and_dedup_key`：断言 body 文案、payload.remindDaily 与 dedup_key 前缀。

[tests/test_server.py](file:///workspace/workbench/tests/test_server.py) `validate_meta_extensions` 增加：合法 `HH:MM` 放行；非法值（如 `"25:00"`、`"9:00"`、`"ab:30"`）抛 400；空串放行。

## 验证方式

1. 定向：`cd workbench && python -m unittest tests.test_notify tests.test_server -v`
2. 全量回归：`python -m unittest discover -s tests -v`（项目当前 357 项须全绿）。
3. 手机端验证（复用现有浏览器 QA 环境，320/375px 窄屏）：新建待办→设置每日提醒时刻→保存→重开确认回显；检查 time 输入在窄屏不溢出、弹窗可键盘操作。运行方式沿用 `QA_CHROMIUM=... uv run --with playwright --with pillow python scripts/<qa>.py`（不新增独立 QA 脚本，必要时扩展现有 mobile_qa）。
4. 端到端投递：本地起服务，设置一个 1–2 分钟后的每日时刻，等待维护窗口（或测试内 force）确认站内铃铛出现；已配置 Web Push/Webhook 时确认两通道同步收到。

## 发布

遵循 AGENTS.md：测试全绿后提交到 `main`，由 Workbench CI（单测 + 浏览器 QA + compose 校验）通过后触发 `workbench-production.yml` 自动部署到同一工作台。无数据库结构变更，升级/回退仅涉及代码与 meta JSON，旧数据不受影响；未配置部署凭据则明确报告未部署。
