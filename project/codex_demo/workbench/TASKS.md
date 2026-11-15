# 任务管理：重复任务、子任务、多标签、跨空间搜索、月历与 Markdown

本批把市面个人任务工具的常见能力补齐，同时保持知序「无数据库结构变更、数据仍在用户自己的服务器」的底色。重复规则、子任务与多标签全部收纳在既有 `items.meta` 自由 JSON 中（16KB 上限），不新增表、不改列，旧镜像可直接运行现有数据库，回滚时新字段被旧程序忽略。

## 重复任务（recur.py）

- 规则存于 `meta.recurrence`，字段为 RRULE-lite 子集：`freq`（daily/weekly/monthly/yearly）、`interval`（缺省 1）、`byday`（仅 weekly，MO..SU 去重子集）、`until`（YYYY-MM-DD）与 `count`（含首次到期的总次数，二者互斥）、`completed`（服务端滚动时维护）。
- `recur.py` 是纯函数模块（无 IO、无表、无 SCHEMA）：`validate_recurrence` 校验、`next_due` 推算下一到期日、`describe_recurrence` 生成中文描述。月末溢出收敛到当月最后一天，闰年 2/29 在平年收敛到 2/28，脏日期退回今天，绝不因坏数据让请求报错。
- 滚动语义：对带规则的任务把状态改成 `done` 时，服务端在同一写事务内判断——还有下一期则状态回到 `active`、`meta.due` 前进、`completed` 加一；`until`/`count` 耗尽则置 `done` 并写 `recurrence.exhausted=true`。非重复任务、以及未把状态改成 done 的请求完全保持旧行为，已 done 的重复任务重复提交不会复活。
- 演示模式无后端，前端 `rolloverRecurrence` 逐行复刻同一套语义。

## 子任务与多标签

- 子任务存 `meta.subtasks`，最多 20 项，每项 `{id: 32 位小写 hex, title: 1..120 字, done: 布尔}`，服务端在 `item_input` 统一校验，非法返回 400 中文。卡片展示「已完成 x/y」与可勾选迷你清单，勾选即 PATCH 整个数组；只读成员禁用。
- 多标签存 `meta.tags`（去重后最多 8 个、每个 1..20 字），并继续兼容旧的单值 `meta.tag`。`GET /api/spaces/:sid/tags` 聚合两者返回 `{tags:[{name,count}]}`（读权限即可），待办与笔记视图据此渲染标签筛选条。

## 跨空间搜索

- `GET /api/search?q=关键词`（登录），按账号 30 次/60 秒限流，关键词 1..100 字。
- 仅在调用者是成员的空间内搜索 `items`（title/body/meta.tag/meta.tags）与 `daily_entries`（title/summary/body）；通过 members JOIN 在 SQL 层限定空间，没有成员空间时直接返回空集合、绝不回退全库。密码箱、会话、用户凭据一律不参与，命中结果也不回显其他 meta 字段。
- LIKE 通配符 `%`/`_` 经 ESCAPE 转义按字面匹配；SQL 预筛后再由 Python 把命中严格收敛回契约字段，片段为不含 HTML 的纯文本，按更新时间降序、上限 100。
- 前端「跨空间搜索」视图（⌘K、顶栏按钮、侧边导航统一入口）按空间分组展示，点击可跨空间跳转打开；旧的单空间搜索弹层已移除，避免两套入口。演示模式在本地数据上做同名过滤。

## 日程月历

- 月历是当前空间、当前分区的只读视图（周一起 42 格），把任务的 `meta.due` 落到日期格，重复任务用纯函数 `nextOccurrences` 计算未来 3 次落点（受 count/until/exhausted 约束）。支持上一月/今天/下一月导航，点日期可筛选或新建预填该日期的任务；窄屏下条目收敛为圆点，不产生横向溢出。

## 安全 Markdown

- `public/vendor/markdown.js` 是自研同站渲染器（MIT，见同目录 LICENSE），仅用 DOM API 构建节点，用户文本只走 `textContent`，无 `innerHTML`/HTML 透传；链接经 `new URL` 协议白名单（http/https/mailto），`javascript:`/`data:` 等降级为不可点纯文本。
- 笔记详情与含标记的任务正文经 `window.ZhixuMarkdown.render` 渲染，编辑器内仍为纯文本原文；渲染器缺失或异常时降级为纯文本，保证内容始终可读。CSP `script-src 'self'` 无需调整，零外部资源。

## 验证与兼容

- 后端 `tests/test_recur.py`（19 项）与 `tests/test_server.py` 新增 8 项覆盖滚动、耗尽、非法 meta、标签聚合、跨空间隔离、通配符与限流；`tests/test_package_release.py` 守卫所有根模块在发布清单、Dockerfile COPY、Render 编译命令三处不缺失。
- 浏览器 `scripts/recur_calendar_qa.py`（21 项）与 `scripts/markdown_qa.py`（25 项）走真实 HTTP 与真实 chromium，覆盖真实/演示双模式、服务端 400、XSS 降级与窄屏。
- 无 SCHEMA 变化，`ops_backup.py` 计算的 schema 哈希与上一版一致；`recur.py` 无 SCHEMA 常量，按既有约定不进入哈希模块清单。
