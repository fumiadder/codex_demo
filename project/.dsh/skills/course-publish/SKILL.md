---
name: course-publish
description: 订阅百度网盘分享链接中的课程更新，把新增章节转存到网盘目标目录、汇总为单章 Markdown（图片上博客园图床并校验公网可访问），与课程网站目录做安全匹配后按章发布到博客园（AI/agent 分类、对应期合集）。同时提供发布后公开可见性巡检、博客园审核/重审诊断、受控整篇重发与合集顺序修复。当用户给出百度网盘课程链接、要求同步课程章节、发布课程文档，或处理博客园公开页 404/重审/重发时使用。
whenToUse: 用户提到“网盘课程/第X期/新增章节/转存/汇总成 Markdown/发博客园/合集/课程订阅/公开页404/审核中/重审/重新发布/合集排序”，或工作区存在 config/courses.yaml 时。
---

# 课程网盘订阅与博客园发布技能

## 不可违反的闸门规则（最高优先级）

1. **匹配不确定就问，绝不乱发**：章节匹配只有 `high` 置信度才允许自动发布；`medium/ambiguous/none` 必须通过 `ask_user_question` 向用户出示【网站标题 ↔ 网盘候选】对照，选项含：采用当前匹配 / 手动指定网盘文件夹 / 跳过本章。
2. 用户裁定写入 manifest 的 `manualOverride`，后续运行永久生效，不重复询问。
3. 遇到百度验证码/风控/掉登录、博客园分类缺失、图片公网校验失败、扫描版 PDF：**暂停该章并报告**，不得绕过。
4. 发布默认 `publish=true`（用户已选择高置信自动发布）；任何低置信内容只能停在草稿或挂起状态。
5. 幂等：manifest 已记录 `postId` 的章节走 `editPost`，已转存的章节不重复转存，重跑安全；即使 manifest 未落盘，发布前也会用 getRecentPosts 按标题去重，崩溃重跑不发重复博文。
6. 韧性：课程站临时不可用时 toc 阶段自动回退上次快照（仅告警）；压缩包解压前做 zip-slip 校验，危险条目直接拒绝。
7. **发布必须传 HTML**：MetaWeblog 的 description 按 HTML 解析，发布前一律经 `lib/markdown-html.mjs` 的 `markdownToHtml`（剥离 YAML frontmatter + marked 转 HTML），严禁把原始 Markdown 直接当 HTML 提交（否则标题/图片语法显示成字面文本）。
8. **更新后必须复核合集**：MetaWeblog `editPost` 会清空已有合集归属；每次更新正文后必须通过合集管理的「关联博文」重新加入，并核验合集总数与课程顺序，不能以更新前状态为准。
9. **本地“已发布”不等于网页可见**：MetaWeblog 返回 postId、后台显示已发布，只代表博客园收文。公开页 404 且后台 `reviewStatus=0/2` 是审核/重审，不是本地失败、也不一定是删除；先跑只读审核巡检。除非用户明确要求重发，禁止用反复编辑、删文、换 ID 重发来“修 404”；重发后再次进入审核就必须停止并如实报告。

## 目录约定（相对工作区）

- `config/courses.yaml`：各期配置（复制 `config/courses.example.yaml`）。
- `secrets/cnblogs.env`：博客园 MetaWeblog 凭证（600 权限，不入库）。
- `browser-profile/`：Playwright 持久化登录态（百度网盘 + 课程站 + i.cnblogs.com）。
- `state/<course-id>.manifest.json`：事实状态机；`state/<id>.toc.json`：网站目录快照；`state/<id>.pending-gates.json`：待确认闸门；`state/<id>.publish-preview.json`：dry-run 预览；`state/<id>.review-audit.json`：发布后后台审核/公网可见性巡检快照。
- `downloads/<id>/<章节>/` 原始文件；`output/<id>/NN-slug.md` + `assets/` 汇总产物；`logs/` 运行日志与探针快照。

## 标准操作流程

> 路径说明：当前工作区源码可用 `.dsh/skills/course-publish/scripts/...`；安装到用户级后，在 skill 目录执行 `node scripts/...`，并用 `COURSE_PUBLISH_ROOT` 指向具体课程工作区，避免把不同期次的数据串在一起。

### A. 新期次 onboarding（一次性）

0. 先跑 `npm run doctor` 环境体检（18 项：依赖/Chromium/系统工具/数据目录/配置/凭证，缺目录自动创建），按提示补齐。
1. 向用户收集 6 项：① 分享链接+提取码；② 网盘目标目录绝对路径；③ 课程站目录 URL 与平台；④ 博客园 blogApp/账号/密码；⑤ 期号与合集名；⑥ 确认 AI/agent 分类存在。
2. 写 `config/courses.yaml`（已 gitignore）与 `secrets/cnblogs.env`（`chmod 600`）。
3. `npm run baidu:login`：有头浏览器中完成百度网盘登录；同窗口登录课程站；再跑 `npm run cnblogs:login` 登录 `i.cnblogs.com`（合集操作需要）。三个站点共用 browser-profile/。

### B. 每期/每次更新执行

命令前缀：`node .dsh/skills/course-publish/scripts/run.mjs --course <id>`（`--help` 查看全部参数与退出码；拼错的阶段名在读配置前即报错）

1. **先 dry-run 全景摸底**（不动网盘写入、不碰博客）：
   `--dry-run --only transfer,toc,match,download,merge`
   - 转存阶段 dry-run 只枚举不转存；下载/合并可对已转存章节执行；发布阶段只生成 `publish-preview.json`。
2. **处理闸门**：若退出码 10，读 `state/<id>.pending-gates.json`，用 `ask_user_question` 逐条询问，把答复写成 `state/<id>.decisions.json`（数组：`[{webIndex,action:"accept|map|skip",shareName?}]`），再带 `--decisions state/<id>.decisions.json` 重跑 match。
   - 闸门有两类：`chapter`（网站章节匹配不确定，必须确认，阻塞流程）与 `orphan-share`（网盘有但网站目录没有的文件夹，如加餐/资料包，仅提示不阻塞，默认不发布）。
   - 同一网盘文件夹被映射给两个章节会直接报"决策冲突"，必须修正后才能继续。
3. **正式转存与下载**：去掉 `--dry-run` 跑 `--only transfer,toc,match,download,merge`。
4. **传图与发布**：`--only images,publish`；发布后核对输出的 postUrl，确认标题=网站标题、分类=AI/agent、合集=对应期、图片全部公网可访问。
5. 单章调试：任意阶段加 `--chapter <webIndex>`。
6. 页面结构异常时先跑探针：`node .../baidu-probe.mjs --url '<分享链接>'`，快照在 `logs/baidu-probe-*.json`，据此修正 `lib/baidu.mjs` 选择器。

### B2. 人工审核工作台（默认推荐：先预览、人工批准、再发布）

为杜绝错配/裂图/排版错乱直接上线，提供本地工作台，把「图片上云」和「发布」拆成人工闸门：

```bash
node .dsh/skills/course-publish/scripts/workbench.mjs   # 启动后访问 http://127.0.0.1:5717（仅本机）
```

- **章节状态机**：`merged(已合并) → staged(已暂存) → approved(人工批准) → published(已发布)`；`partial` 表示有图片未上云、`blocked` 阻塞、`deleted` 已删除。
- **三步按钮**：① 暂存（`stage.mjs`：图片传博客园图床并逐张公网校验，生成 `output/<id>/previews/00-<slug>.preview.md`，源 `00-*.md` 不动）→ ② 预览检查并批准 → ③ 发布（`publish-approved.mjs`）。**章节重新合并后必须重新暂存生成预览，严禁继续发布旧预览。**
- **硬闸门**：只有 `approved` 且 preview 零本地 `assets/` 引用的章节才会发布；未暂存/partial/未批准一律拒绝（服务端二次校验，不依赖前端）。
- **三视图预览**：渲染预览（读者所见 HTML）、原始 Markdown（合并产物原文）、本地源文件（以**文件夹树**完整保留 `downloads/<course>/<slug>/` 的原始目录层级，可折叠展开；文本/图片/PDF/docx 网页内直看——docx 经 mammoth 转 HTML，压缩包内容同样按包内目录树呈现并在线解压单文件预览，含目录穿越防护），用于逐文件核对合并是否忠实、有无错漏。
- **幂等不重传**：图片相对路径→在线 URL 持久化在 `state/<id>.image-url-map.json`（assets 文件名即内容哈希），逐张落盘，崩溃/重跑零重传；暂存态存 `state/<id>.staging.json`。同一文章内同一 URL/内容哈希图片只保留第一次出现：暂存时 `dedupeImageRefs` 去重，`markdownToHtml` 最终再按 `<img src>` 去重；代码块和行内代码中的图片语法示例不受影响。
- **订阅管理**：工作台「订阅管理」页可对 `courses.yaml` 的 `baidu.shares` 增删分享链接；「扫描网盘新增章节」按钮后台跑 `run.mjs --only transfer,toc,match,download,merge`，任务日志可在「任务日志」页查看。
- CLI 等价物：`stage.mjs --course=<id> [--only=slug|--all-pending] [--force] [--dry]`；`publish-approved.mjs --course=<id> [--only=slug] [--dry] [--skip-collection] [--force-new]`。已发布文章要替换图片/正文时用 `stage --force`，批准后 `publish-approved` 会沿用 publish-state 的 postId 更新同一篇，不会新建。
- **工作台状态同步**：如果文章已经发布在博客园（例如重建合集后复用旧 postId），必须先把实际 `postId/postUrl/合集位置/审核状态` 写回 `state/<course>.publish-state.json`，并把对应 staging 标为 `published`；否则工作台仍会显示“已合并”，重复暂存/发布。
- **同标题跨期发布**：不同期次可能有完全相同标题（如陪跑 day01），默认同标题去重会改到别的期次文章。只有明确要创建另一期的新文章时才加 `--force-new`；已有 publish-state 的 slug 仍会更新，崩溃重跑不会重复。
- 图床每日 500 张限额（滚动/自然日，403 即到限），暂存到限自动暂停，配额恢复后续传，已上传图片靠 URL 映射不重复计费。

### B3. 发布后审核巡检（只读，默认动作）

```bash
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --collection=39559
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --collection="AI agent七期"
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --course=ai-agent-8
```

- 直接从博客园后台合集读取全部文章，拉取后台所有分页的 `reviewStatus`，再并发检查每篇公开 URL；**不改文章**。
- `reviewStatus`: `0` 待审核、`1` 已通过、`2` 重审中；只有 `1` 且公开页 200 才算真正公开。
- 报告写 `state/<course>.review-audit.json` 或 `logs/review-audit-<collectionId>-<时间>.json`；退出码 `0` 全公开，`10` 有阻塞，`1` 运行错误。
- 404 且后台 `0/2`：等待审核，不重发；404 且后台无记录：核查是否被删除。
- 新文章也会审核，重发换了 postId 但内容指纹没变，不能绕过审核；审核触发原因博客园不公开。
- 用户明确授权整篇重发时，按 [审核与重发](references/review-and-republish.md) 备份、删旧文、新建、重进合集和**校正合集顺序**；任何一步失败即停。
- 合集重建/替代：博客园最多公开 20 个合集；第 21 个后台正常但公开页 404。需先备份旧合集条目，再删除一个旧合集释放名额。批量关联后必须用 `serialNum` 校正顺序并从 `#mainContent .entrylist .entrylistItemTitle` 核验；不要把重审文章算作公开。详见 references/review-and-republish.md。

### B4. 章节计划与双轨内容（主课 + 陪跑）

- `state/<course>.chapter-plan.json` 支持 `track:"main"` 与 `track:"peipao"`；工作台按 `chapters` 全量展示，主课/陪跑共用同一份 `edition8-progress.json`。
- 章节标题取课程站目录中的 `dayXX-*` 标题；网盘单元名取分享内真实文件夹/压缩包名，不能照标题猜后缀。
- 同一 day 有多个重复目录时（如 `day24-仿真人实操(1)`），先比对内容再选 canonical 目录，不能把两个都并进一章。
- 分享顶层包一层课程总目录时，可将整包转存后使用 `/目标目录/总目录名/...`；若百度转存大目录超时，改为逐单元 `share/transfer`，目标目录直接放 day 单元。

### C. 章节状态机

`discovered → transferred → downloaded → merged → images-uploaded → matched → published`；
异常态 `blocked`（看 `blockReason`）与 `skipped`。匹配可在下载前完成，故 `matched` 与转存态会合并保留较高进度。

## 文档转换规则（lib/docs-md.mjs）

- **忠实原文（硬规则）**：正文就是课件原始内容，禁止任何额外加工——不加 frontmatter、章标题、「图片：」备注、PDF/PPTX 页码标题、警告块、分隔线，不做标题降级、不给 txt 套代码围栏。单文件章直接输出原文；多文件章才以「# 文件名」一级标题区分（`displayRel` 会剥掉 `_extracted/<包名>/` 解压包装层，保留包内真实相对路径）。章标题仅在发布时用作博客标题，不进正文。长 txt（≥1000 字符或 ≥30 行）使用默认收起的 `<details>` 展示，原文完整转义保留在自动换行的 `<pre>` 中，文件名标题仍保留；emoji 等非 BMP 字符使用 HTML 数字实体，防止博客园入库丢字。
- **长提示词/参考文档结构化折叠**：文件名命中 `提示词|小说原文|分镜描述词|剧本|资产信息|短剧内容审核|火山引擎剧本格式` 且 ≥1000 字符或 ≥30 行时，生成默认收起的 `<details class="course-prompt-fold">`。`<summary>` 与正文之间必须留空行，让 marked 继续解析内部 Markdown，保留标题、段落、列表、表格；**禁止**用 `<pre>` 把这类内容压成单块。`操作记录.md`、`进阶内容.md` 等讲解型文档不折叠。
- md/txt 原文直出；docx 用 mammoth 抽文本与内嵌图；doc 走 macOS textutil→html；html 用 turndown；pdf 用 pdfjs 抽原文文本（无文本层抛 SCANNED_PDF，挂起问用户，不 OCR）；pptx 抽幻灯片原文文字。
- **PPT 逐页转图片，不抽文本**：`.pptx` 经 LibreOffice 转 PDF、pymupdf 逐页导出 PNG（`pptxToImagesMd`），注册进 imageRegistry 复用 assets/content-hash 上传管线，正文为逐页 `![第 N 页](assets/hash.png)`。**发布场景禁止文本降级**：soffice 缺失或渲染失败时该章必须阻塞，先恢复/安装 LibreOffice 再重新合并、`stage --force`、批准和更新；否则会把整份 PPT 变成文字页。zip-slip 安全错误始终不降级。工作台源文件树与压缩包内 pptx 均支持 `?view=slides` 逐页图片预览（渲染缓存于 `state/.ppt-preview/`）。
- **只汇总文档类，排除源码附属文档**：仅 `.md/.txt/.pdf/.doc/.docx/.html/.pptx` 文档类才进正文；递归遍历时剪枝 `node_modules/.git/dist` 等目录（PRUNE_DIRS），并按文件名跳过源码/项目附属说明文档——`readme/license/notice/changelog/requirements/setup/install` 开头的文件（`isProjectMetaFile` + `PROJECT_META_NAMES`）不汇入正文，记入 `report.skipped`，避免把源码库 README、依赖 requirements 等拼进课件。
- 图片按 sha256 去重落 `assets/`（独立图片只登记不入正文，仅文档内引用的图片随原文保留）；视频/音频/安装包只登记到 skipped 清单；压缩包在下载阶段解压（zip 用 unzip，rar/7z 需 unar/7z）。md 内嵌图片引用兼容 URL 编码文件名与原生 `<img src>` 标签（`resolveImageAbs` 回退 decodeURIComponent）。

## 新增课程站平台适配器

`lib/toc.mjs` 默认 XHR JSON 递归 harvesting + DOM 启发式。新平台若失败：在 `scrapeToc` 增加 `if (course.toc.platform === 'xxx')` 分支，优先拦截该平台目录接口的 XHR（参考 `logs/toc-*.json` 快照里的 capturedUrls），输出统一 `[{index,title,url}]`。

## 排障手册（references/runbook.md 有详版）

| 现象 | 处理 |
|---|---|
| GateError login | 重跑 `npm run baidu:login` |
| GateError captcha/risk | 有头窗口人工过验证后重跑；降低频率 |
| newMediaObject 故障 | 检查 cnblogs.env；仍失败则在编辑器网页通道手动传图并回填（images-cnblogs.mjs 预留 MediaApiError） |
| 合集控件找不到 | 看 `logs/cnblogs-ui-*.png` 截图，更新 cnblogs-ui.mjs 选择器；博文已发布，补合集不影响正文 |
| 公开页 404 / 显示重审 | 跑 `cnblogs-review-audit.mjs` 核对 `reviewStatus` 与公开页；`0/2` 是审核阻塞，等待，不重发；只有用户明确要求才按 references/review-and-republish.md 整篇重发 |
| 退出码 10 | 读 pending-gates.json 走人工闸门流程 |
| dlink 返回 HTML 风控页 | 自动转 UI 下载；持续失败则人工下载后放入 downloads 对应章节目录，再从 merge 阶段续跑 |

## 实战经验

- 7/8 期全流程、嵌套分享、PCS 大文件、PPT 图片化、结构化折叠、工作台状态同步、合集上限与批量排序的踩坑与验证结果，见 [7/8 期实战经验](references/lessons-7-8.md)。
- 六期及后续期次优先按该文档中 B4/C 的双轨计划、`--force`、`--force-new` 和合集核验流程执行。

## 安装到用户级（跨工作区复用）

```bash
node .dsh/skills/course-publish/scripts/install-user.mjs            # 默认装到 ~/.dsh/skills/course-publish
node .dsh/skills/course-publish/scripts/install-user.mjs --target <dir>  # 自定义目标
```
- 安装器会复制 skill 并在目标目录就地 `npm install`，使其自包含；依赖中的 Playwright 固定为源工作区实际版本，避免用户级 `npm` 解析到新版本后与本机 Chromium 缓存不匹配。换机器若提示缺少浏览器，在目标目录执行 `npx playwright install chromium`。
- 数据根解析：环境变量 `COURSE_PUBLISH_ROOT` > 当前工作目录（含 `.dsh/` 或 `config/courses.yaml`）> skill 相对推导；因此从各课程工作区运行用户级副本时，数据互不串台。
- 升级：源 skill 改完后重跑安装器（目标会被整体替换）。

## 定时订阅（阶段 9）

- 手动订阅巡检：`npm run subscribe`（全部期次）或 `npm run subscribe -- --course <id>`；非交互、无头、headless 模式，报告写 `logs/subscribe-report-*.json`。退出码：0 成功；10 有闸门待确认；1 致命错误或存在 blocked 章节（单章失败不拖垮整轮）。
- DSH 定时：用 `schedule_create` 建每日 `daily` 提醒，prompt 中明确让 agent 执行 `npm run subscribe` 并处理 pending-gates（询问用户后写 decisions 回放）。若近期出现过风控，定时任务应改为有头模式（去掉 subscribe.mjs 里的 --headless）。
- 定时只自动发布高置信章节；任何歧义、验证码、分类/合集异常都挂起并在会话中向用户汇报。

## 测试

- 匹配引擎：`node --test .dsh/skills/course-publish/scripts/names.test.mjs`
- XML-RPC 编解码：`node .dsh/skills/course-publish/scripts/xmlrpc.selftest.mjs`
- 文档汇总夹具：`.venv/bin/python tests/make_fixtures.py && node --test tests/docs-md.test.mjs`
- 长提示词结构化折叠：`node --test tests/prompt-fold.test.mjs`
- 审核状态纯函数：`node --test .dsh/skills/course-publish/scripts/cnblogs-review.test.mjs`
