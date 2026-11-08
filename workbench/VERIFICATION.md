# 验证记录

## 首页今日记事去重与日程弹窗（2026-11-08）

- 首页「本月日程」的今天分组继续聚合待办、今日记录、随笔与媒体；删除重复的「今天记下的事」「今日记录」首页卡片及其无用样式，不改动原始记录或数据库。
- 今日记录在首页本月日程、当日回顾和日历的日程视图中均改为打开详情弹窗，不再跳转记录页；弹窗显示时间、分区、摘要、正文与私有图片，可继续编辑。待办、随笔和媒体沿用既有弹窗。
- 真实后端浏览器 QA：`daily_browser_qa.py` 全部通过，覆盖今日记录只在本月日程出现一次、首页与日程页点击 URL 不变、正文／摘要／两张图片、Escape 与关闭按钮、键盘 Enter 打开；320px、390px 竖屏和 844×390 横屏弹窗均在视口内且无页面横向溢出。截图：`artifacts/workbench-month-agenda.png`、`artifacts/workbench-agenda-dialog-mobile.png`。
- `recur_calendar_qa.py`：21 项通过，无 pageerror；`node --check public/app.js` 与 `git diff --check` 通过。
- 后端未改动。全量 344 项 unittest 中，已有文档记录的 macOS `test_large_base64_clone` 网络 BrokenPipe／ConnectionReset 仍复现，与本次纯前端修改无关；排除该已知平台用例后 343 项通过（15 项按环境跳过），正式 CI 仍需全绿后才允许生产发布。

## 用户名 / 手机号登录（第二批）

登录标识从邮箱改为「用户名或手机号二选一」，同时保留旧的邮箱登录通道，避免打断脚本与既有习惯。

设计取舍：`users` 表被 members/items/sessions/daily_*/notify_*/templates/settings 等十余张表以外键引用，重建它需要临时关闭外键并重写所有依赖行，风险远高于收益。因此新增增量表 `user_identities(user_id, username UNIQUE, phone UNIQUE)` 承载登录标识，`users` 结构与全部外键保持原样，旧镜像可直接忽略新表，回滚安全。没有邮箱的账号在 `users.email` 写入 `@users.invalid` 占位地址，读接口一律把它还原为空串，不会暴露给浏览器。

- 注册：必填用户名（3–30 位，字母数字下划线点短横线），手机号为可选 11 位；为兼容既有脚本，用户名位置若填的是邮箱则自动取前缀派生用户名。
- 登录：`identifier` 依次匹配用户名、手机号，最后回退到旧邮箱；错误统一返回「用户名或密码错误」，不暴露账号是否存在。
- 加成员：按用户名/手机号查找用户，列表返回 `login` 字段优先展示登录名。
- 设置：新增「登录方式」区块，改绑用户名或手机号需验证当前密码。
- 邀请：`manage_access.py invite --email` 现在可填用户名或手机号（首次引导 bootstrap 仍要求真实邮箱）；修复了注册时用合成邮箱比对付费预留名、导致所有用户名邀请被拒的缺陷（提交 db78881）。
- 存量迁移：启动时把没有身份行的账号按邮箱前缀补一条用户名，冲突时加后缀，兜底用账号 id，`fuminer` 因此可直接用 `2521532473` 登录，id 与全部数据不变。

验证（2026-11-08）：

- 全量 344 项单测通过（仅已知 macOS `test_large_base64_clone` BrokenPipe 偶发）；关键路径另有脚本覆盖：注册/登录/改绑 15 项、邀请用户名与手机号、存量迁移。
- 浏览器 QA：mobile 9、frontend_hardening 4、account_export 16、recur_calendar 22、daily 5、life_entry 6、collaboration 7、bedtime 11、menu_visual 2 全部通过。
- 在服务器上用一次性 PostgreSQL 容器精确复现 CI 的 native PG 步骤：**344 项全绿**，排除方言差异。
- 部署：schema 哈希 `03580192…` 走审查式迁移（先 `backup.sh --leave-stopped` 备份 `20261108T084210Z-39cbca31`，再构建、健康检查、配置断言、软链切换）；邀请修复版哈希不变走标准 deploy.sh。生产确认 fuminer 已迁移出 `username=2521532473`。
- 生产烟测：用户名邀请注册、`/api/me` 身份、用户名与手机号登录、错误密码 401、改绑后新手机号登录、按用户名加成员、fuminer 用户名与旧邮箱均可识别；有成员的空间拒绝注销（保护机制生效）。测试账号已全部清理，仅剩真实用户与其 1 条真实「今日记录」。

## 通知、Webhook、模板库、日程与快捷方式（批次 2b）

本批引入了结构变更：新增 `notify.py`（notifications / user_webhooks / webhook_deliveries）、`templates.py`（item_templates）与 `settings.py`（user_settings）三个模块，共 5 张新表，全部为 `CREATE TABLE IF NOT EXISTS` 增量且挂 `users` 外键 `ON DELETE CASCADE`。schema 哈希由 `83d99f53…` 变为 `cfc70bbf…`，`ops_backup.py` 与 `deploy.sh` 的模块清单已同序加入 notify/templates/settings，两处计算结果一致。

后端：

- `notify.py`：到期任务扫描挂在既有 `maybe_cleanup` 维护锁内、请求驱动、60 秒节流，按 `task-due:{item_id}:{due}` 去重（写事务内复查）；Webhook 发送器强制 https、拒绝 userinfo、对 `getaddrinfo` 的每个地址做 ipaddress 判定（环回/私网/链路本地/保留/组播/未指定/`fc00::/7`）、拒绝一切 3xx 重定向、8 秒超时、HMAC-SHA256 签名，仅 2xx 成功、429/5xx/超时可重试并按 60/300/900/3600 退避、5 次置 failed，日志只记 id/event/状态码。发送泵为模块锁 + 单守护线程，线程内异常仅告警。
- `templates.py`：10 个内置模板常量（不落库）+ 每账号自定义模板，meta 复用 `validate_meta_extensions` 收口、16KB 上限、50 个上限，builtin 与他人资源统一 404。
- `settings.py`：`user_settings` 存每账号快捷方式，服务端白名单 `SHORTCUT_VIEWS` 校验，最多 8 个，去重。
- 三个模块均登记进 `package_release.py`、`Dockerfile`、`render.yaml`，`test_package_release` 守卫全绿。

前端（真实与演示双模式语义一致）：

- 通知铃铛：顶栏未读角标、面板列出 task.due 通知、单条/全部已读、点击跳转对应空间并打开任务；演示模式用本地 mock。
- Webhook 设置：账号设置内新增「出站提醒」区块，添加（公网 https）、一次性 secret 展示与复制、启用开关、测试、轮转 secret、二次确认删除。
- 模板库：新建流程新增「从模板新建」，选中后预填标题/正文/分区/meta（含标签、重复规则、子任务）；模块库新增「模板库」卡片，内置只读、自定义可编辑/删除；编辑器新建时提供「保存为模板」。
- 日程视图：月历新增「日历 / 日程」双模式切换，日程按日期分组（含星期、农历、今天徽标），任务显示时间段（`meta.startTime/endTime`，缺省「全天」），今日记录归入对应日期，支持「更早/回到本周/更晚」。
- 首页快捷方式：stats 下方「常用功能」条 + 「调整」面板，服务端按账号持久化。
- 农历换算为纯前端查表（1900–2100 压缩表），零网络零依赖；Node 单测 6/6 覆盖三个春节基准年。

关键修复：`settings.py` 原用 `from server import APIError` 取异常类，在 `python server.py` 启动时运行模块是 `__main__`，拿到的是另一个类对象，导致校验错误变成 500；改为与 notify/templates 一致的 `sys.modules.get(type(server).__module__)` 后，生产模式下非法快捷方式正确返回 400（提交 bd5caa0）。

本地验证（2026-11-08，Asia/Shanghai）：

- `node --check`、`py_compile`、`bash -n` 通过；全量 `unittest discover -s tests` 344 项仅已知 macOS `test_large_base64_clone` BrokenPipe 偶发。
- 浏览器回归：recur_calendar 21、mobile 9、frontend_hardening 4、account_export 15、daily 5、menu_visual 2、life_entry 6、collaboration 7、bedtime 11 全部通过；新增功能在真实模式与演示模式均逐步实操验证（通知角标/面板/已读、Webhook 全流程含 SSRF 拒绝、模板预填与保存、日程时间段与农历、快捷方式持久化），320px 无横向溢出、无 pageerror。

部署（提交 6a22f4c + 修复 bd5caa0，CI run 42387047326 / 42460207893 均全绿）：

- 2b 首版因 schema 变更走审查式迁移：`backup.sh --leave-stopped` 先做一致性备份（`20261107T160245Z-a5dc35dc`，checksums/SQLite integrity/上传引用均 PASS），预飞容器验证模块导入与哈希 `cfc70bbf`，再由 compose 构建启动、wait_healthy、配置断言（testMode=False/persistent/invite）、软链切换，5 张新表确认创建、真实用户数据完好。
- 修复版 bd5caa0 哈希不变，走标准 `deploy.sh`（备份→构建→健康→网关→HTTPS 验证 PASS），current 指向 `bd5caa0`。
- 端到端生产烟测 26/26 通过：快捷方式（保存/读取/非法 400/未登录 401）、模板（内置 10/创建/列表/编辑/内置只读 404/删除）、通知（到期生成/关联正确/全部已读）、Webhook（一次性 secret/列表不回传/内网 SSRF 拒绝/非 https 拒绝/轮转/停用/删除）、账号注销（密码门控/凭证失效）。
- 清理：烟测临时账号（含此前两次失败尝试留下的 3 个）已全部删除，复核后仅剩真实用户 fuminer 与其 1 条真实「今日记录」，notifications/user_webhooks/webhook_deliveries/item_templates/user_settings 均为 0。

## 任务管理增强：重复任务、子任务、多标签、跨空间搜索、月历与 Markdown（批次 2a）

本批零数据库结构变更：重复规则、子任务、多标签全部存于既有 `items.meta`，不新增表、不改列。`ops_backup.py` 计算的 schema 哈希仍为 `83d99f53…2838`，与生产一致，部署结构守卫直接通过；新模块 `recur.py` 无 SCHEMA 常量，按约定不进入哈希清单。三个 subagent 分波并行（后端、Markdown 渲染器文件独立先做，前端对着真实后端一次性接入），避免同改 app.js 冲突。

后端：

- 新增纯函数 `recur.py`（RRULE-lite：daily/weekly/monthly/yearly、interval、byday、until/count 互斥、completed），月末与闰年收敛、脏日期回退今天、RRULE COUNT 含首次语义；`server.py` 在 `item_input` 收口校验 recurrence/subtasks(≤20)/tags(≤8)，PATCH 置 done 时在同一事务内滚动到期日或标记 exhausted，非重复任务零影响。
- 新增 `GET /api/spaces/:sid/tags` 标签聚合（兼容旧 meta.tag）与 `GET /api/search` 跨成员空间搜索：members JOIN 在 SQL 层限定空间、空集合不回退全库、LIKE 通配符转义、meta 命中 Python 二次收敛仅 tag/tags、密码箱/会话/凭据零参与，30 次/60 秒限流，结果上限 100。
- `recur.py` 已登记进 package_release、Dockerfile COPY、render.yaml 三处清单，`test_package_release` 守卫全绿；新增 27 项后端测试（recur 19、server 8）。全量 294 项单测仅 `test_large_base64_clone` 的 macOS 回环 BrokenPipe 既有偶发失败（Linux CI 不受影响）。

前端与渲染器：

- 新增自研同站安全 Markdown 渲染器 `public/vendor/markdown.js`（MIT + LICENSE），仅 DOM API、无 HTML 透传、链接协议白名单、危险协议降级纯文本、异常降级 textContent。
- app.js 接入七项能力：重复任务编辑器与中文描述/完成次数/耗尽态、子任务迷你清单、多标签芯片与筛选条、跨空间搜索视图（⌘K/顶栏/侧边三入口统一，旧单空间 modal 已删除）、周一起月历与重复落点、笔记与任务正文 Markdown 渲染；真实/演示双模式语义对齐。新视图在标题映射、areaViews、render 映射、导航、SVG sprite 全量注册。
- 新增 `scripts/markdown_qa.py`（25 项）与 `scripts/recur_calendar_qa.py`（21 项），均支持 QA_CHROMIUM；CI 已登记这两个浏览器步骤。

本地验证（2026-11-07，Asia/Shanghai）：

- `node --check`、`py_compile` 通过；禁用 API（innerHTML/eval/alert 等）静态扫描零命中，新增 CSS 零硬编码颜色，全部使用既有 token。
- 浏览器 QA：recur_calendar（21）、markdown（25）、mobile（9）、frontend_hardening（4）、life_entry（6）、account_export（15）、daily（5）、menu_visual（2）、bedtime（11）退出码均为 0。
- 两个偶发失败已对照确认非本批回归：`collaboration_qa` 的录音按钮断言单次失败后连跑 3 次全绿，且本批未改任何录音/getUserMedia 代码、stash 到基线同样通过；`warm_story_qa` 语音事件断言 1 过 1 败，是批次 0/1 已记录的 bedtime 既有 headless 时序竞态（bedtime 文件本批未改）。

生产部署与烟测（2026-11-07，提交 6066ca3，CI run 42335198191 全绿）：

- 发布包含 recur.py、export.py、public/vendor 与两个新 QA，SOURCE_SHA 校验一致、无 .env/数据库/备份等敏感物；服务器上对发布副本的 Dockerfile 仅 sed 阿里云 pip 镜像，与 current 的差异只有新增 recur.py。
- 预飞（compose 构建一次性容器、临时 /data，不触生产）：schema 哈希仍为 `83d99f53…2838`，全部模块导入成功（含 recur 中文描述），`/api/health` 健康，`/vendor/markdown.js` 以 text/javascript 返回，`/api/search` 与 `/api/spaces/:id/tags` 未认证返回 401（已接线而非 404）；验证后容器、镜像与临时目录已清理。
- `deploy.sh` 部署：镜像 `zhixu-app:6066ca3…` healthy，current 软链与 current-sha 切换，网关正常，schema 标签保持 `83d99f53…2838`（结构守卫按零变更直接通过），自动备份 `20261107T035247Z-00b04a1f` 生成；外部 HTTPS 复查安全头（CSP `script-src 'self'`、HSTS、nosniff、DENY 等）齐全，app.js 含新函数、vendor 200、搜索 401。
- 端到端烟测 26/26 通过（一次性邀请注册的临时账号，结束后密码门控注销）：每日任务完成后回到 active 且 due 前进到次日、completed=1、子任务与标签保留；count=1 任务完成即 done+exhausted；非法子任务 id 返回 400；`/tags` 聚合新标签并兼容旧 meta.tag；跨两个空间搜索同时命中 item 与 daily、含空间名与纯文本片段、密码箱专属词无结果、空关键词 400、未认证 401。
- 残留与数据完好：注销后 users 仅余真实用户 fuminer（1 个成员关系完好），烟测账号在 items/daily_entries/sessions/spaces 等表零残留；维护流量生成的当日快照 `folio-20261107-040209.db` 正常落盘。

## 数据主权与账号生命周期（批次 1）

本批次新增数据导出、自动快照与账号/空间生命周期，无数据库结构变化（无新表、无 ALTER/DROP），旧镜像可直接运行现有数据库。三个 subagent 按冻结契约并行（导出模块、账号生命周期、设置中心），文件域不重叠。

后端：

- 新增 `export.py`：owner 专属单空间导出（JSON/Markdown/HTML），SQL 层绑定 `space_id`，媒体流式入 zip，临时文件 0600 并在结束时清理；密码箱仅导出本人密文并标注仍需原密码；会话、邀请码、密码哈希、其他空间数据一律不导出；HTML/Markdown 全量转义，归档路径白名单防 zip-slip；按账号每小时 6 次限流。
- 自动快照只在 SQLite 方言、由认证流量驱动的维护路径执行，每天一份、保留 7 份、0700/0600 权限；**快照为全库文件、无任何 HTTP 下载路由**，仅运维可接触。实现中发现并修复了在线备份复制 WAL 文件头导致 `os.replace` 丢最新页的隐患：改为隔离临时目录备份、`wal_checkpoint(TRUNCATE)` 并切回 rollback 日志后原子落位。
- `PATCH /api/me` 支持改昵称与改密码（嵌套 `{password:{currentPassword,newPassword}}`），改密成功轮换全部会话；`DELETE /api/me/account` 与 `DELETE /api/spaces/:sid` 均先校验密码/所有者，在有其他成员或用户在他人空间留有共享记录（`created_by` 无级联）时 fail-closed 返回 409，媒体对象先入删除队列再级联；撤权成员时清理其晚安故事收藏、历史、音色、私有音频与密码箱副本，共享条目保留。
- 新增 21 项后端测试（`test_export` 11、`test_server` 新增 10），全量 263 项中仅 `test_large_base64_clone` 的 macOS 回环 BrokenPipe 既有偶发失败（隔离可通过、Linux CI 不受影响）。

前端：

- 空间管理内新增 owner 可见的三格式导出（非 owner 提示无权限、演示模式禁用），blob 下载的对象 URL 统一登记并回收；侧栏新增「账号设置」：改昵称、改密码（含强度与一致性校验）、注销账号（需登录密码且逐字键入「删除我的账号」双条件启用）、删除空间（需键入空间全名，409 中文指引原样展示）。
- 全程无 alert/confirm/innerHTML，错误走 role=alert 与 toast，样式仅用现有 token、44px 触控、320px 窄屏适配；新增 `scripts/account_export_qa.py` 真实 HTTP 集成测试 15 项。

本地验证（2026-11-07，Asia/Shanghai）：

- 后端 263 项除已知偶发外全绿；`node --check`、`py_compile`、`bash -n` 通过。
- 浏览器 QA：account_export（15）、mobile（9）、frontend_hardening（4）、life_entry（6）、daily（5）、menu_visual（2）、collaboration（7）、bedtime（11）退出码均为 0。
- `warm_story_qa` 出现语音事件时序断言偶发失败（3 次中 1 次通过）；已用 stash 在批次 0 的 HEAD 上对照复现同样失败，且 `bedtime.html` 只加载 `bedtime.js`、本批次未改动任何 bedtime 文件，确认为与本次改动无关的既有 QA 时序竞态。
- CI 工作流补登 account_export 浏览器检查。生产部署与烟测在提交通过 CI 后另行记录。
- 发布前打包自查发现并修复一个会导致生产启动失败的缺陷：`scripts/package_release.py` 的 `ALLOW_FILES` 是显式清单，未包含新增根模块 `export.py`，旧清单打出的不可变镜像会在 `import export` 时崩溃。已把 `export.py` 加入清单，并新增 `tests/test_package_release.py` 守卫：断言根目录每个运行时模块都在发布清单内、且清单条目都真实存在，今后新增根模块漏登记会在单测阶段直接失败。
- 首次生产部署（49c5246）被 `deploy.sh` 的健康检查拦截并自动回滚：新容器启动即 `ModuleNotFoundError: No module named 'export'`。根因是除打包清单外还存在第二份显式模块清单——`Dockerfile` 的 `COPY` 逐文件列举同样漏了 `export.py`，tarball 含模块但镜像内没有。回滚后旧镜像健康、数据库与上传卷原样保留，生产未受影响。修复：Dockerfile COPY 与 `render.yaml` 编译命令补齐全部根模块（顺带补上历史遗漏的 `neon_transport.py`）；`scripts/ops_qa.py` 的第三份副本清单改为直接复用 `package_release.ALLOW_FILES`（同时补上它历史遗漏的 `daily.py`）；守卫测试扩展为同时校验发布清单、Dockerfile COPY、render.yaml 三处与根模块完全一致（267 项单测，仅已知 macOS 偶发失败）。`ops_backup.py`、`migrate_storage.py` 的清单只收集带 `SCHEMA` 常量的模块，`export.py` 无 SCHEMA，不纳入是正确行为。

生产部署（2026-11-07，Asia/Shanghai，最终提交 `1a5f7c2`）：

- 修复后先在服务器做不触碰生产的预飞：用 `docker compose build` 构建独立 `zhixu-preflight` 标签，以临时空数据目录跑一次性容器，确认 `import export/server/daily/bedtime` 全部成功、`/api/health` 返回 ok、容器 healthy、无 traceback 后销毁预飞容器与临时数据；同时算出 `SCHEMA_HASH=83d99f53…2838` 与现网完全一致（本批无结构变化，结构守卫通过）。
- 正式 `deploy.sh`：自动备份（`backups/20261106T171543Z-41af7021`）→ 构建（层缓存命中，秒级）→ 健康检查通过 → 切换，无回滚。`current` 与 `state/current-sha` 指向 `1a5f7c2`，镜像标签 revision/schema 正确，网关与应用均 healthy。此前失败的 49c5246 也留有独立备份（`20261106T170141Z`）。
- 一次性邀请注册的临时账号完成 18 项真实链路烟测并全部通过：注册、建空间、播种任务；JSON/Markdown/HTML 三种导出均为合法 zip 且含对应条目（`export.json`/`INDEX.md`/`index.html`）；非法格式 400、匿名导出 401；改昵称、改密码并轮换会话、旧密码拒绝、新密码登录、错误旧密码 400；删除无成员空间 200 且列表消失；注销账号 200 后会话失效且无法再登录。测试结束账号经注销接口自删除，九张关联表零残留，真实用户 `2521532473@qq.com` 数据完好、无孤儿媒体与删除任务。
- 自动快照经认证流量的维护路径真实生成：`/data/snapshots/folio-20261106-171803.db`（目录 0700、文件 0600），复制到临时库独立打开 `PRAGMA integrity_check=ok` 且含拍摄时点数据，证明 WAL 收敛修复在生产生效、快照为自包含单文件；当天仅一份，符合每日一份契约。
- 外部复核：新前端（账号设置入口）已对外提供，HSTS、nosniff、DENY 等安全头齐全，未认证访问受保护接口一律 401，`/api/config` 仍为 `testMode=false / persistent / invite`。GitHub Workbench CI 对 `1a5f7c2` 全绿（含新增的数据导出/账号生命周期浏览器检查与打包清单守卫）。

## 审计缺陷修复与安全加固（批次 0）

本次修复来自四个只读审计（前端、后端、测试基线、市面对标）发现的 P1/P2，无数据库结构变化（无 ALTER/DROP、无新表），旧镜像可直接运行现有数据库。

后端（`server.py`、`daily.py`、`bedtime.py`、`storage_jobs.py`）：

- 今日记录暂存图片纳入存储配额：`storage_jobs.usage()` 统计表加入 `daily_uploads`，1 小时暂存窗口不再脱离配额；`linked()` 同时检查 `files`、`daily_uploads`、`daily_images`，慢 PUT 的迟到对象不会被误删。
- 图片解压炸弹防护：模块级 `Image.MAX_IMAGE_PIXELS=6400 万`，检查与降采样路径都把 Pillow 警告提升为错误，超大尺寸图片在分配像素前返回 400 中文提示。
- 晚安故事朗读合成的音色存在性、配额与磁盘检查全部前移到付费供应商调用之前，最坏 10MB 预检、保存时按真实大小二次校验，容量不足不再产生费用。
- 深层嵌套 JSON 的 `RecursionError` 返回 400 而非 500；限流桶超 5000 个时淘汰最旧 25% 桶，不再对全部用户误伤 429；过期会话在启动与低频维护路径有界分批清扫（每批 1000 行）。
- 今日记录图片上传增加每账号 60 次/小时限流，在读取大报文前生效。
- 新增 9 项单元测试覆盖配额三态不双重计数、暂存拒绝无残留、上传限流不波及他人、炸弹 PNG 拦截、供应商零调用的配额预检、深嵌套 JSON、限流淘汰与会话清扫。

前端（`public/app.js`、`public/bedtime.js`、`public/bedtime.html`、`public/styles.css`）：

- 图片识别建议转待办的初始状态由 `todo` 改为 `active`，与编辑器和状态机一致，并有浏览器断言拦截请求载荷与持久化值。
- 新建密码箱持久化失败时保留已派生密钥与表单上下文，页面给出「重试创建密码箱」原地重试（PUT 幂等），不再清空状态造成分叉。
- 晚安故事成员资格轮询只在明确 401/403 时结束会话，网络抖动、超时与 AbortError 静默等待下一轮，不中断朗读。
- 演示数据补齐生活区媒体与今日记录；失败上传瓦片提供「重试／移除」并联动提交按钮；灯箱支持左右方向键循环、Escape 关闭且监听随弹窗清理；PDF 预览 iframe 使用不含脚本权限的 sandbox；429 读取 Retry-After 给出倒计时文案、503 保留后端具体中文提示；移除上传大小死参数与全部 25MB 硬编码（单一常量）；故事分类栏说明古风、寓言为经典库专属。
- 新增 `scripts/frontend_hardening_qa.py`（密码箱 503 重试、429/503 文案映射、PDF sandbox、无页面错误），`free_deploy_qa.py` 改为支持 `QA_CHROMIUM`。

本地验证（2026-11-06，Asia/Shanghai）：

- 后端 242 项单元测试：除 `test_large_base64_clone_owner_isolation_and_provider_id_not_exposed` 的 macOS 回环大报文偶发 BrokenPipe 外全部通过；该用例在改动前的 HEAD 基线上同概率偶发（对照 4 次 1 过 3 败），隔离复跑可通过，Linux CI 不受影响，15 项 Postgres 跳过项为配置门禁。
- 浏览器 QA 独立复跑 8 套全部退出 0：life_entry（6）、mobile（9）、daily（5）、menu_visual（2）、frontend_hardening（4）、bedtime（11）、warm_story（11）、collaboration（7）。
- `node --check`、`py_compile`、`bash -n` 全部通过；CI 工作流补登 collaboration、menu_visual、frontend_hardening 三个浏览器检查。

生产部署（2026-11-06，Asia/Shanghai，提交 `b90d61d`）：

- 服务器 `/srv/zhixu` 走标准 `deploy.sh`：自动备份（`backups/20261106T155448Z-d5692d1f`）→ 构建 → 健康检查 → 切换；无数据库结构变化，镜像 schema 标签与上一版同为 `83d99f53…2838`，结构守卫直接通过，失败回滚镜像保留。
- 首次构建因容器内 pip 直连 pypi.org 超时中止（仅构建阶段，未触碰运行中的应用）；按既往国内部署做法在服务器发布目录的 Dockerfile 追加阿里云镜像源（与上一版发布 Dockerfile 完全一致，仓库源码不包含镜像源），重建后通过。
- 部署后外部断言：`/api/health` 返回 ok，`/api/config` 为 `testMode=false / persistent / invite`，未认证访问受保护接口一律 401，HSTS、nosniff、CSP（frame-ancestors 'none'）等安全头齐全，新 `app.js` 已对外提供。
- 一次性邀请注册的临时账号完成 13 项真实链路烟测并全部通过：邀请注册/登录、建空间、active 待办创建与持久化、今日记录配置、真实 PNG 上传（经配额与像素检查路径）、图片入记录、记录列表、密码箱写入与读回、未授权拒绝、登出后会话失效。测试结束后该账号、空间、记录、3 个私有图片对象与邀请码全部删除，九张关联表零残留；真实用户 `2521532473@qq.com` 的空间与数据保持原样，磁盘无孤儿对象，应用持续 healthy。
- GitHub 三个工作流（Workbench CI、production deployment 凭据校验、read-only production check）对该提交均为 success；其中 production deployment 的实际部署步骤因未配置服务器密钥而跳过，线上更新由上述手动部署完成。

## 工作／生活分区菜单差异化

生活区不再复用同一份菜单：晚安故事并入生活区主导航，私密密码箱仅保留在工作区，共有功能在两个分区使用不同名称（工作区：今日记录／待办事项／灵感与笔记／媒体资料库；生活区：今日瞬间／生活清单／心情笔记／回忆收藏），页面标题与说明文案同步切换，移动端底部导航文案随分区变化。处于工作专属视图时切换到生活区会自动回到总览，不会停留在不可达的视图上。

- `life_entry_browser_qa.py` 断言改为新结构：工作区隐藏晚安故事且显示密码箱、生活区反之，并核对分区标签与菜单名称，6 组检查全部通过。
- `mobile_qa.py` 改为从主导航打开晚安故事，9 组移动端检查（含 320/360/390/430px 竖屏与横屏无溢出、抽屉焦点约束）全部通过。
- `daily_browser_qa.py` 全部通过；后端 233 项单元测试除已知回环大报文偶发 BrokenPipe（隔离复跑通过、Linux CI 不受影响）外全部通过。
- `scripts/menu_visual_check.py` 输出两个分区的完整菜单可见性矩阵，验证密码箱→生活区自动回退总览、移动端标签切换，截图存于 `artifacts/menu-*.png`。

## 暖色睡前故事更新

本节记录本次代码的本地验证。以下检查使用临时账号与数据，不代表新版已在免费站或国内生产环境上线；下方历史验证记录保留原有范围。

- 54 项真实 HTTP 后端测试全部通过：30 项工作台测试、24 项晚安故事测试。新增检查覆盖个性化生成参数、只读禁写、无密钥返回 503、供应商失败、生成后的退出／撤权重验、账号限额及并发限制、严格文本结构、温和问题与安抚结尾。年龄筛选修正后，24 项晚安故事测试再次通过；没有年龄标注的旧故事不会被默认归入儿童年龄组。
- 新增 `scripts/warm_story_qa.py`，11 组 Chromium 检查全部通过。覆盖 320–1440px 布局、暖色与夜间字号、本地字体实际加载、减少动态效果偏好、生成参数与 1／5 分钟故事、文本与音色独立、段句高亮和分页、温和提问与继续、收藏／历史元数据、真实 WebAudio 钢琴与白噪音，以及退出、撤权和切换空间后的延迟结果拒绝。生成流程使用明确标注的模拟供应商，朗读事件使用设备语音替身；请求经过真实 HTTP 服务，WebAudio 节点实际启动与关闭。这些检查不验证真实 AI 输出质量或克隆音色相似度。
- 定时检查覆盖 30／60／90 分钟：测试推进到期时钟后，朗读、背景音和试听停止，AudioContext 关闭，页面进入休眠并可唤醒。退出会清除旧故事、问题、音色与定时状态，旧截止时间不会再次打开休眠页。
- 既有浏览器回归全部通过：晚安故事 11 组、生活入口 6 组、手机工作台 9 组、电脑上传与朗读 8 组。覆盖原有账号／空间隔离、上传入口登录返回、移动布局、真实录音解码及试听；电脑脚本的云端联动组仍使用模拟供应商。
- `public/app.js`、`public/bedtime.js` 的 JavaScript 语法检查通过。新版 Docker 镜像构建与一次性容器启动检查通过：实际以 UID 10001 运行，健康接口、暖光页面、同站字体正常，认证后的原创库共 14 篇，未配置生成服务返回 503。检查只使用独立 tmpfs 数据，完成后停止并删除测试容器，没有操作真实数据卷。
- 原创库共 14 篇，六类故事与约 1／5 分钟分级均已检查。个性化生成连接固定的百炼 `qwen-plus` JSON 接口；缺少服务端密钥时关闭生成入口并返回 503，不用原创库冒充生成结果。固定安抚结尾只追加一次，故事正文不与音色绑定。
- 中文 WebFont 随站点提供，源自 Noto Sans SC 并保留 OFL 1.1 许可、固定来源和子集说明；400／500 字重实际浏览器加载通过，无外部字体 CDN 依赖。截图为 `artifacts/bedtime-warm-desktop.png`、`artifacts/bedtime-warm-mobile.png`、`artifacts/bedtime-warm-night.png`，均使用虚构测试内容，云服务未配置。
- `server.py` 与 `bedtime.py` 的数据库结构声明已逐项对比已发布提交 `487598f32786186cdec10cbad5b28334bae6229e`，未改变数据库结构。

目前没有真实模型／音色服务凭据，也没有已配置的正式服务器、域名或部署认证。免费 Render 站关闭自动部署，仍运行旧版；本次检查没有验证新版公网部署、国内运营商访问、真实 AI 音质或手机锁屏持续播放。发布源码和部署新版是分别记录的步骤。

## 移动端、晚安故事与正式发布准备

验证日期：2026-10-08（Asia/Shanghai）。以下检查使用本地临时数据与测试资源；当前免费公网地址仍运行旧测试版本，新功能尚未发布到公网。

- 47 项真实 HTTP 后端测试全部通过：30 项工作台与邀请注册检查，17 项晚安故事检查。覆盖邀请码绑定邮箱、哈希、有效期、单次使用与并发事务；原数据库升级；账号与空间隔离；只读禁写；跨标签预期账号校验；媒体与朗读音频共用额度；克隆 WAV 格式和 10–30 秒时长；真实接口适配的数据流、外部失败、并发限制、退出和撤权后的结果拒绝；私人音频鉴权与过期。服务商响应在测试中模拟，没有调用付费接口或测量云端音质。
- 晚安故事 Chromium 检查通过：真实登录与空间 API、分类和文本搜索、切换音色不重搜、收藏历史、主动保存离线文本、助眠工具、手机日夜模式、延迟响应与撤权／账号边界。设备语音事件仅在 QA 中模拟；WebAudio 白噪音实际启动与停止。
- 手机工作台 9 组检查通过，覆盖空间慢加载期间禁用新建、320／360／390／430px 与横屏、导航抽屉焦点、背景点击、Escape 关闭、真实笔记保存和无横向溢出。实际双标签退出／换号会清除旧正文、已解锁密码箱密钥与条目，并撤销媒体预览 URL；旧账号延迟返回的 401 不会清除新账号会话。截图使用测试账号与虚构内容。
- 生活入口 6 组浏览器检查通过：晚安故事仅在生活类别显示、从当前空间进入、320／360／390／430px 无溢出、现有会话和真实注册后的上传返回、拒绝外站与异常返回路径、账号各自记住空间。晚安故事返回生活首页，演示入口要求先登录。
- 电脑晚安故事覆盖 320–1920px、桌面三栏预览、通用上传链接不含账号／空间标识、匿名登录返回、只读协作空间不影响打开自己的上传入口、实际 WAV 解码与本机试听、关闭／退出撤销预览，以及无权限空间拒绝。脚本为 `scripts/desktop_story_qa.py`，截图为 `artifacts/bedtime-desktop.png`、`artifacts/voice-upload-desktop.png` 和 `artifacts/workbench-life-desktop.png`；截图中云端服务未配置。
- 桌面脚本共 8 组通过，准确 10 秒／30 秒录音经浏览器解码、转换后可正常本机试听。第 8 组在独立临时服务器注入明确标注的模拟供应商：真实上传、用户授权确认、克隆档案入库、自动选择音色、保留故事正文不重搜、私人音频鉴权与浏览器实际播放、模拟联网文本继续使用同一音色。该组不调用真实供应商，不验证本人音色相似度、外部联网可用性或合成延迟。
- 原工作台 Chromium 完整流程 9 组回归通过：注册、笔记安全渲染、任务、工作生活过滤与持久化、媒体解码、密码箱正确／错误密码与刷新锁定、真实录音上传及退出清理。
- 免费环境提示回归 5 组通过：登录前后及密码箱提示、手机无溢出、已声明持久化环境隐藏提示、未声明存储与配置失败时显示待确认状态。
- 协作与录音回归 7 组通过：真实双账号共享、只读／编辑切换、撤权、上传失败保留录音及重试只保存一份。
- 真实容器首发、升级保留数据、完整一致性备份、恢复到新卷、拒绝覆盖、失败健康检查回退旧镜像并保留新写入、结构变化守卫和损坏备份拒绝均通过。详细记录见 `OPS-VERIFICATION.md`；首次上线与后续自动更新见 `DEPLOY-PRODUCTION.md`。

云端音色克隆和联网故事检索仅在服务端配置相应密钥后启用。当前没有真实服务商凭据、正式服务器或域名；没有验证云端克隆质量、真实手机锁屏播放、生产证书或国内运营商访问。

## 第一版历史验证

验证日期：2026-10-07（Asia/Shanghai）。本地功能检查与免费公网部署验收分别记录，不代表已在国内生产网络上线。

## 已完成

- 19 项真实 HTTP 后端集成测试通过：多账号隔离、空间成员/编辑/只读权限、成员撤权、密码派生及会话哈希、CSRF、媒体鉴权与 Range、活动文件格式安全下载、限流、负载及空间配额、慢上传期间并发保存与权限重验，以及测试模式公开配置不泄露服务端信息。
- Chromium 浏览器完整流程通过：真实注册与空空间、笔记保存/安全文本渲染、任务完成、工作与生活分区、刷新持久化、图片上传解码、密码箱加密与正确/错误密码解锁、刷新后的锁定、真实录音上传与音频预览、退出清除旧账号内容。脚本为 `scripts/browser_qa.py`。
- 额外安全浏览器验证通过：阻塞下一账号的空间请求时不会显示上一账号的内容；密码箱 PUT 请求不包含独立密码或明文；切空间时销毁解锁数据；延迟的旧解锁不会注入新空间。
- 额外 7 项协作/录音浏览器检查通过：界面创建协作空间、按邮箱授予只读权限、只读界面不可写、升级编辑后协作写入、移除成员后禁止访问、录音上传 503 时保留原音频、解除失败后重试只保存一份并成功解码。脚本 `scripts/collaboration_qa.py` 自动启停独立临时服务器，不会写入现有工作台数据。
- 桌面 1440px 与手机 390px 截图已检查；无横向溢出，手机导航可打开并在进入功能后收起。截图位于 `artifacts/`，均为明确标注的演示数据。
- Docker 镜像构建成功，容器以非 root 用户运行，`/api/health`、首页及脚本响应正常。Compose 配置校验与 JavaScript 语法检查通过。
- 免费环境登录前后提示、手机布局、密码箱提示、正式环境隐藏提示、配置读取失败时的提示均通过独立浏览器检查，脚本为 `scripts/free_deploy_qa.py`。

## 免费公网部署

Render 实例 https://zhixu-free-test.onrender.com 已在 2026-10-07 11:07:58 UTC 部署为 `live`，免费规格、新加坡节点，无收费磁盘或数据库。平台日志确认监听 `0.0.0.0:10000`，近期错误日志为空。

2026-10-07 11:19:18 UTC，GitHub Actions 真实公网 HTTPS 验收通过 12 组检查，工作流终态 `success`：https://github.com/fumiadder/codex_demo/actions/runs/37613219063 。应用运行提交为 `3d4bb527f95407b6591c19e449e929dcc06f3307`，检查脚本提交为 `02417cef77947facab8578ecbc783129e987e076`；两者应用文件一致，后续文档提交不触发 Render 重部署。

- 证书默认校验、健康与配置、首页与静态文件、CSP/HSTS 和缓存策略。
- 两个随机测试账号注册、登录、Secure/HttpOnly/SameSite Cookie、退出后会话失效。
- 私人空间与未邀请成员隔离、工作／生活过滤。
- 微小 PNG 上传、文件鉴权、Range 206 和无效范围 416。
- viewer 读取与禁写、editor 协作、移除后记录／文件／密码箱访问即时失效。
- 随机不透明密码箱载荷保存、按用户隔离、拒绝额外明文字段；此项验证 API，不把随机字节当成浏览器加密测试。
- 跨站写入拒绝；测试条目与上传删除，账号退出。没有账号／空间删除接口，因此保留临时账号、空空间及所有者随机密文样例。

## 仍需目标部署验证

免费测试不替代国内生产环境与持久化存储验收。地址与配置见 `DEPLOY-FREE.md`，免费文件系统可能在休眠、重启或部署后清空。

需要国内云服务器连接方式、域名解析与云厂商要求的备案条件。当前没有实际生产域名、国内访问地址或已连接的目标服务器，因此未发布生产实例。

国内不同运营商网络可达性、真实手机麦克风权限及浏览器语音转写服务连通性，仍需实际设备验收。Headless Chromium 无法可靠模拟切换浏览器标签的可见性变化，未把模拟事件当作真实标签切换测试；该自动锁定处理器已代码检查，切空间和页面刷新锁定已实测。

## 今日记录浏览器检查

`scripts/daily_browser_qa.py` 用临时数据目录启动真实服务，覆盖无密钥降级链路：模块库入口、两阶段私图上传与就绪缩略图、建记录后的时间线与九宫格、待连接 AI 卡片不伪造结果、灯箱计数、手动编辑摘要、总览卡片，以及 320/390px 窄屏无横向溢出与导航抽屉。

```bash
QA_CHROMIUM=/usr/bin/chromium python scripts/daily_browser_qa.py
```

本地 macOS 若 Playwright 自带浏览器目录不可写，可用 `PLAYWRIGHT_BROWSERS_PATH` 指向可写目录并经 `QA_CHROMIUM` 指定 headless shell。截图输出到 `artifacts/workbench-daily-mobile.png`。后端视觉识别另有 `tests/test_daily.py`（含 FakeProvider 的成功、限流、配额、暂存回收与权限用例）；无密钥端到端冒烟确认识别与小结返回明确 503，上传、建条目、原图服务与删除全部正常。

## 可重复浏览器检查

开发环境需要 Playwright 和系统 Chromium；应用运行本身不需要这些工具。使用临时数据目录启动测试服务，避免把测试账号写入个人生产空间：

```bash
HOST=127.0.0.1 PORT=8123 DATA_DIR=/tmp/zhixu-browser-test python server.py
# 另一个终端
QA_BASE_URL=http://127.0.0.1:8123 QA_CHROMIUM=/usr/bin/chromium python scripts/browser_qa.py
```

测试录音使用浏览器生成的麦克风信号，不会读取真实设备声音。演示截图不包含真实私密信息。
