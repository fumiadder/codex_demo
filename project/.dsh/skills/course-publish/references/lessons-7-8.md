# 7/8 期实战经验（课程发布与合集重建）

## 1. 章节计划：课程站 TOC 是课时级，不是章节级
- 课程站目录可能给出 300+ 个课时标题（含小节），而网盘分享只有 dayXX 主课文件夹/压缩包。
- 不能把课时逐个建文；先按 `dayXX-课程标题` 的 TOC 一级节点建立 day 级计划，再把分享中的真实 day 单元映射进 `units`。
- 主课和陪跑分成两个 track：`main`（day01..dayN）和 `peipao`（陪跑课件 day01..dayN），共用一个 chapter-plan / progress，工作台按 46 章展示。
- 同一天的重复目录（如 `day24-仿真人实操(1)`）先比对内容，只选 canonical 一个，绝不把两份都并进同一章。

## 2. 网盘分享的嵌套根目录
- 分享如果顶层只有一个 `AI智能体X期` 总目录，内部才是 dayXX：
  - 整包转存大目录可能返回 errno=4 超时；目标为空就是没成功，不要当作已转存。
  - 改为逐单元 `share/transfer`，把 dayXX 直接放进 `/目标/7 期`，陪跑放 `/目标/7 期/陪跑课件`；计划 units 用直接相对路径。
  - 分享直连 `/share/download` 对嵌套条目可能 errno=2；不要绕过页面权限。
- PCS 报 `大文件授权限制`（75MB/350MB 等）时，使用百度分享页官方 UI：
  - 进入总目录，悬停 day 行，点行内「下载」，在「下载内容较大」弹窗选「浏览器下载」。
  - Playwright `page.waitForEvent("download")` + `download.saveAs()` 保存到本地章节目录，再解压、标记 progress downloaded，重跑 `edition8 --only=<slug>`。
- PCS 下载遇到远端 0 字节文件（如 `美术风格.txt`）：
  - 先用 `pan.ownList` 确认远端 size=0；在本机预建同名空文件，再重跑下载，后续文件会继续补齐。非 0 字节不能这样处理。

## 3. PPT 必须逐页转图片，不能文本降级
- 所有 `.pptx/.ppt` 都要经 LibreOffice → PDF → 逐页 PNG，正文是 `![第 N 页](assets/hash.png)`。
- macOS 上 `/usr/local/bin/soffice` 可能指向已删除的 `/Applications/LibreOffice.app`；Homebrew 缓存 DMG 仍可恢复：挂载 DMG，`ditto LibreOffice.app /Applications/`，验证 `soffice --headless --version`。
- 缺渲染器时该章阻塞，不能用文本抽取页面发布。工作台源文件 PPT 预览同样依赖 soffice。
- 替换已发布文章的 PPT 图片：`stage --force --only=<slug>` → 工作台批准 → `publish-approved --course=<id>`，沿用 publish-state 的 postId，不要新建。

## 4. 长提示词/小说/剧本/资产信息结构化折叠
- 命中 `提示词|小说原文|分镜描述词|剧本|资产信息|短剧内容审核|火山引擎剧本格式` 且 ≥1000 字或 ≥30 行，使用默认收起的 `<details class="course-prompt-fold">`。
- summary 与正文之间必须有空行，让 marked 解析内部 Markdown；内部保留 h1/h2/段落/列表/表格，**禁止**用 `<pre>` 单块压成一行。
- `操作记录.md`、`进阶内容.md` 等讲解文不折叠；长 txt 仍用 `<details class="course-txt-fold">` + `<pre wrap>` 保留原文行。
- 2026-11 的 7 期 day26：5 个结构化折叠 + 3 个 txt 折叠；day27：1 + 2。线上读回 HTML 验证折叠数量。

## 5. 工作台状态与博客状态同步
- 工作台只读 `state/<course>.chapter-plan.json` / `edition8-progress.json` / `staging.json` / `publish-state.json`。
- 已有线上文章，必须把实际 postId/URL/合集位置/reviewStatus 写回 publish-state，并把 staging 对应标为 published；否则工作台显示“已合并”且重复暂存。
- Workbench status `published` 代表后台收文（isPublished），不是公网可见。reviewStatus 0/2 仍可能是 404；同步 reviewPending，不把它误判为失败。
- 编辑文章会清空合集归属；每次更新后必须用「关联博文」重新加入合集并核验顺序。

## 6. 合集重建、20 个公开上限与批量排序
- 博客园最多公开 20 个合集；第 21 个后台条目正常但公开页 404（匿名访问合集页当前也会跳登录）。
- 替代旧期次：备份旧合集元数据/entries → 新合集关联/排序 → 删除旧合集释放名额；合集删除只丢分组，不删文章。
- 批量关联 `PUT /api/collections/<id>/entries {postIds}` 需页面 XSRF-TOKEN；只建成员，不保证顺序。
- 排序 `PUT /api/collections/<id>/entries/<entryId>/serialNum` 的 serialNum 是 0 基目标；批量写后重新读取，常见首两篇对调，最后按 postId 单独修正。
- 公开页主列表用 `#mainContent .entrylist .entrylistItemTitle` 核对，不统计侧栏“最新随笔/推荐”链接。

## 7. 同标题跨期与内容审计
- 7/8 期陪跑大量同名（day01-开课开营等）；默认同标题去重会 editPost 改到另一期。
- 只有明确要创建另一期新文章时用 `publish-approved --force-new`；已有 publish-state 的 slug 仍走原 postId 更新，崩溃重跑不重复。
- 全量审计用 MetaWeblog `getPost` 对比本地 Markdown：结构化/txt 折叠数量、图片数量、标题。只更新不匹配的章节。
- 图片缺口要扣除可复用的同哈希 image-url-map（8 期已有 926 张，7 期主课 298 引用中 93 张可复用）；其余受图床 500/日限制，不能一次硬塞，宁可次日续传，不发布半截文章。

## 8. 七期实际结果（2026-11）
- 主课 29 + 陪跑 17 = 46 章全部合并，工作台 46/46 已发布。
- 陪跑 17 个 PPT 全部逐页图片化，238 个图片引用、215 个唯一文件，0 失败。
- 合集 46 条，day01→day29、陪跑 day01→day17 顺序精确。
- 后台 37 篇通过、9 篇重审（day21/day28 + 7 篇陪跑）；工作台状态与后台 isPublished 同步，公开可见性另记 reviewStatus。
