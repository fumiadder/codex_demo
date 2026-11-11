# 排障手册 runbook

## 百度网盘

### 登录失效（GateError: login）
- 运行 `npm run baidu:login`，在有头浏览器重新扫码；登录态写入 `browser-profile/`。
- 验证：打开 pan.baidu.com，页面右上角显示账号即有效。

### 验证码 / 风控（captcha / errno=-140）
- 保持有头模式（不要加 --headless），在弹出的浏览器里手动完成滑块/验证，然后重跑同一命令；manifest 已记录进度，不会重复转存。
- 连续风控：拉长 humanDelay、减少单次转存项数，隔天再跑。

### 分享页结构变化（ui-changed）
- 跑 `node scripts/baidu-probe.mjs --url '<分享链接>' --pwd <提取码>`。
- 看 `logs/baidu-probe-*.json`：captured 里的 share/list XHR 是否含 list；dom.counts 各选择器命中数；buttons 文案。
- 据此更新 `lib/baidu.mjs` 的 `_hasFileList/_domEntries/_transferByDom` 选择器。

### PCS 报“大文件授权限制”
- 大文件（本次 day12 75.7MB、day14 349.8MB）走 PCS 会被限制；不要在同一路径反复重试。
- 在 `browser-profile` 打开分享页，进入课程总目录，悬停目标行点行内「下载」，弹窗选择「浏览器下载」，Playwright `page.waitForEvent("download")` + `download.saveAs(dest)` 保存到 `downloads/<course>/<slug>/`；再解压、把进度标记 `downloaded:true`，重跑 `edition8.mjs --course=<id> --only=<slug>`。
- 分享直连 `/share/download` 对嵌套子条目可能返回 `errno=2`，不要绕过页面权限；官方浏览器下载通道是可复现路径。

### PCS 下载内容为空
- 先用 `pan.ownList` 查远端文件大小；若远端就是 0 字节（如 day24 的 `美术风格.txt`），在对应本地路径预建同名空文件，再重跑 `edition8 --only=<slug>`，它会跳过已存在文件并继续下载后续项。
- 远端大小非 0 才是真实下载失败，应重新下载而不是覆盖成空文件。

### 下载返回 HTML 风控页
- 模块自动切换 UI 下载通道；仍失败时人工下载文件放入 `downloads/<id>/<章节>/` 保持相对目录，然后从 `--only merge` 续跑。

## 博客园

### MetaWeblog 认证失败
- 检查 `secrets/cnblogs.env` 三项是否正确；blogApp 是博客 URL 中 `cnblogs.com/` 后的名字。
- 部分账号需在博客园后台设置独立 MetaWeblog 密码。

### newMediaObject 不可用
- images-cnblogs.mjs 抛 MediaApiError 时：登录 i.cnblogs.com，在随笔编辑器手动上传一张图确认通道；
  若接口确被禁用，改用编辑器网页上传（在 cnblogs-ui.mjs 增加上传流程），回填 URL 到 MD。

### 公开页 404 / 后台显示“重审中”
- 先跑 `node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --collection=<ID>`；后台 `reviewStatus=0` 是待审，`2` 是重审，公开页 404 属审核阻塞。
- 本地 `published:true` 只代表 MetaWeblog 收文，不代表公网可见；`httpOk:false` / `reviewPending:true` 本来就是审核态。
- **不要**反复编辑、删除或换新 ID 重发来修 404，审核不会因此绕过；新文章同样会进入审核。
- 只有用户明确授权整篇重发时，按 `references/review-and-republish.md`：备份旧文→删除并验证→把 staging 改回 approved→publish-approved 新建→合集「关联博文」→「移动至」校正顺序（注意 N/N+1 偏移）→重新巡检。

### 合集设置失败
- 合集只能网页端操作；看 `logs/cnblogs-ui-*.png` 截图定位控件，更新 cnblogs-ui.mjs。
- 博文本身已发布成功，合集可事后补，不影响内容。
- 合集管理地址：`https://i.cnblogs.com/collections/detail?collectionId=<ID>`；「关联博文」搜索完整标题后只勾选唯一 postId。
- 「移动至...」填写的序号可能实际落在下一位置；改完必须刷新并按 postId 核对最终位置。

### 工作台显示“已合并”但线上其实已发布
- 这是 publish-state 未同步，不是需要重发。读取线上合集 entries 的实际 postId、URL、顺序和 reviewStatus，写回 `state/<course>.publish-state.json`，同时把 staging 对应章节标为 `published`；工作台会立即显示已发布且不再暂存。

### 同标题跨期陪跑/课程
- 7 期与 8 期陪跑可能使用相同标题；`publish-approved` 默认按同标题会命中旧期文章并 editPost。要创建另一期的新文章必须显式加 `--force-new`，否则会误改旧期内容。已有 publish-state 的 slug 重跑仍更新原文章。

### 分类 AI/agent 不存在
- 流程会中止并把现有分类写入 pending-gates.json；让用户在博客园后台建好分类后重跑 publish。

## 文档转换

- PPT 发布必须逐页转图片，不能降级为文本：soffice 缺失/渲染失败时该章阻塞，先恢复/安装 LibreOffice（Homebrew 缓存 DMG 可 ditto 到 `/Applications/LibreOffice.app`），再重新合并、`stage --force`、批准并用原有 postId 更新。
- 长提示词/小说/剧本/资产/规范文档应折叠：文件名命中 `提示词|小说原文|分镜描述词|剧本|资产信息|短剧内容审核|火山引擎剧本格式` 且 ≥1000 字或 ≥30 行时，输出默认收起的 `<details class="course-prompt-fold">`。内部保留 Markdown 标题/段落/列表/表格，不能改用 `<pre>` 单块；`操作记录`/`进阶内容` 等讲解文不折叠。
- SCANNED_PDF：该章挂起，问用户是否提供文字版/允许 OCR（默认不 OCR）。
- doc 转换依赖 macOS 自带 textutil；Linux 环境需换 libreoffice headless。
- rar/7z 解压：`brew install unar 7zip`。

## 状态修复

- 某章状态错乱：直接编辑 `state/<id>.manifest.json` 中对应章节的 status 后重跑；
  重新发布某章：删除其 postId 字段（会新建）或保留（走 editPost 更新）。
- 完全重来一期：删除该 course 的 manifest/toc/output/downloads 中对应文件。
- manifest 采用临时文件 + 原子 rename 写入，进程中断不会产生截断损坏的状态文件。

## 退出码与闸门

- `0` 全流程成功；`10` 存在章节匹配闸门（pending-gates.json），需人工确认后用 `--decisions` 回放；`1` 致命错误或存在 blocked 章节。
- 闸门两类：`chapter`（网站章节匹配不确定，阻塞，必须确认）；`orphan-share`（网盘有、网站目录没有的文件夹，仅提示不阻塞，默认不发布）。
- 同一网盘文件夹被映射给两个章节会直接报"决策冲突"，修正 decisions 后才能继续。
- 下载/合并/传图单章失败只把该章标记 blocked，不拖垮同轮其他章节；定时任务据非零退出码报警。
- 发布后 `verifyPost` 复核公开页：页面可达、含标题、每张图 GET 公网可取且 content-type 为 image/*；图床签名过期返回 HTML、404、超时（默认 30s）都计入 badImages 并告警，单图异常不抛错中断。
- 传图阶段 `findLocalImageRefs` 识别行内（含标题/尖括号）、引用式、HTML <img> 三种本地 `assets/` 语法；回填后做**零本地残留**校验，任何未成功上云的引用都会进入 failed 并把该章顶成 blocked，杜绝带本地图发布导致裂图。

## 环境体检与彩排

- 新环境先跑 `npm run doctor`：依赖/Chromium/系统工具/数据目录/配置/凭证 18 项；缺失的数据目录会自动创建。
- 无凭证彩排：`--only publish --dry-run` 不连博客园，只生成 `state/<id>.publish-preview.json`，可提前核对标题（网站命名）、分类、合集。
- 压缩包解压前做 zip-slip 校验，含绝对路径或 `..` 条目的压缩包直接拒绝并标记 warnings。
- 网盘条目名写入本地前经 `resolveInside` 边界守卫：单文件 rel 越界跳过并告警，异常 shareName 直接 fail-closed（章节 blocked），防止网盘条目名的路径穿越。
- courses.yaml 在加载/体检时做 schema 校验（重复 id、缺 baidu.targetDir/shares/toc.url、非法分享 URL），错误在启动期即给出可操作中文提示。
- 网盘文件夹名带 `【更新】`、`[课件]`、`〖资料〗` 等装饰不影响序号提取与匹配；序号冲突的同名章节会被强制压到高置信阈值以下，必须人工确认。
