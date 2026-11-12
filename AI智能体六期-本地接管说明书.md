# AI智能体六期：本地接管说明书

更新时间：2026-11-12 22:31（Asia/Shanghai）

## 1. 这份说明书解决什么问题

将课程 `ai-agent-6` 的下载、PPT 转图、图片上云、博客园发布和合集整理迁移到自己的电脑长期运行，避免远端沙箱断联、文件回滚和后台进程消失。

**核心原则：博客园线上内容是最终事实，本地 JSON 只是辅助状态。沙箱曾回滚，接管后先核验线上文章，再运行发布命令，防止重复发文。**

## 2. 当前进度（区分线上事实与沙箱文件）

| 项目 | 最后确认的结果 |
|---|---|
| 合集 | `AI智能体六期`，ID `45437`；此前已关联并排序 34 篇：Day00–Day32、Day41、Day47 |
| Day46 | **已经发布**，45 张 PPT 图片；地址：https://www.cnblogs.com/fuminer/p/23645478.html |
| Day46 合集 | 尚未归集。原因是浏览器中的 `i.cnblogs.com` 登录态失效；发布 API 不受此影响 |
| Day45 | 最后有效监控时为 **52.2%**，续传进程在线；尚未发布 |
| Day44、Day48 | 没有可靠六期源文件，保持 blocked |
| 其余待处理 PPT 章节 | Day33–Day40、Day42–Day43、Day49–Day52，共 14 章，仍需重新 stage、审核、发布 |
| 旧合集 `38391` | 等所有可发布章节完成后再删除，现在不要删 |

### 沙箱回滚警告

22:31 再次检查时，监控端口 `5718` 已失效，`.pipeline` 中的 `supervisor.mjs`、`monitor-server.mjs`、`run-daemon.sh`、`run-monitor.sh` 已消失。

同时，沙箱里的 `publish-state.json` 和 ZIP 文件也回滚到了较早版本：

- Day46 的本地发布记录又变成 `null`，但**线上文章确实存在，不得重发**。
- 沙箱 Day45 ZIP 回滚为 `88,858,624` 字节，不能把它当作 52.2% 的实时进度。
- 沙箱 Day46 ZIP 回滚为 `103,016,448` 字节，但线上 Day46 已发布，不需要为了发布而重下。

**本地接管时，以复制到本地的文件大小和博客园线上页面为准。不要沿用沙箱回滚后的 JSON 盲目自动发布。**

## 3. 必须复制到本地的目录

建议完整复制整个项目，至少保留下列内容：

```text
project/
├── .dsh/skills/course-publish/scripts/   # 下载、合并、上云、发布、工作台脚本
├── .pipeline/                            # 续传、监控脚本及日志（不含密码明文）
├── config/courses.yaml                   # 课程配置
├── secrets/cnblogs.env                   # 博客园 API 凭证，权限 600，勿提交 Git
├── browser-profile/                      # Playwright 持久化登录态
├── downloads/ai-agent-6/                 # ZIP 与解压文件，续传必须保留原路径
├── output/ai-agent-6/                    # 合并稿、图片和预览
├── state/ai-agent-6.*.json               # 发布、暂存、合集、配额等状态
└── .venv/                                # 可不复制，本地重新创建更稳妥
```

优先使用最新、未回滚的备份。复制后立即在本地再做一份冷备份，尤其是 `state/`、`downloads/` 和 `.pipeline/`。

**开始本地任务前，停掉远端下载和发布进程。两端不要同时运行，避免重复上传或重复发文。**

## 4. 本地环境要求

推荐 Linux 或 WSL。当前续传脚本使用了 Linux 的 `stat -c`，原守护进程还使用 `/proc`；macOS 不能直接照搬这些进程检测和文件大小命令。

需要安装：

- Node.js 20 或更新版本。
- Python 3、`venv`。
- `curl`、`unzip`。
- LibreOffice（提供 `soffice`），用于 PPT 转 PDF。
- PyMuPDF，用于 PDF 逐页转 PNG。
- Playwright Chromium 及系统依赖。

在本地项目根目录执行：

```bash
export COURSE_PUBLISH_ROOT="$PWD"
python3 -m venv .venv
.venv/bin/python -m pip install pymupdf
.venv/bin/python -c 'import pymupdf; print("PyMuPDF OK')"
soffice --version
node --version
```

按项目现有依赖配置安装 Node 包，然后安装 Chromium：

```bash
npx playwright install chromium
# Linux 如缺系统动态库，再执行：
npx playwright install-deps chromium
```

确认 `secrets/cnblogs.env` 存在且包含以下三个键，不要把值写进文档或日志：

```text
CNBLOGS_BLOG_APP=...
CNBLOGS_USERNAME=...
CNBLOGS_PASSWORD=...
```

## 5. 修改本地路径

项目主体脚本支持环境变量：

```bash
export COURSE_PUBLISH_ROOT=/你的绝对路径/project
```

但 `.pipeline` 中的旧脚本把根目录硬编码为 `/workspace/project`。本地路径不同时，需修改：

- `.pipeline/resume-day.sh`：`PIPE` 和 `DEST`。
- `.pipeline/pan-extract-cookies.mjs`：项目路径。
- 恢复后的 `supervisor.mjs`：`ROOT`。
- 恢复后的 `monitor-server.mjs`：`ROOT`。
- 恢复后的 `run-daemon.sh`、`run-monitor.sh`：项目路径。

不要把路径改成相对路径后就假设可用；这些脚本会从不同工作目录启动，建议使用绝对路径。

## 6. 接管后的第一步：线上对账，禁止直接重发

先打开并核验 Day46：

- 页面地址是否为 `https://www.cnblogs.com/fuminer/p/23645478.html`。
- 标题是否为 Day46 课程标题。
- 45 张图片是否可访问，无 403、裂图或本地路径。
- 页面是否显示合集 `AI智能体六期`。

如果文章正常，但本地 `state/ai-agent-6.publish-state.json` 的 `day46` 为 `null`：

1. 先备份 JSON。
2. 根据线上文章补记 `postId: 23645478`、`postUrl`、`published: true`、`images: 45` 和核验结果。
3. 将 `state/ai-agent-6.staging.json` 中 Day46 同步为已发布状态。
4. **不要执行 `publish-approved.mjs --only=day46`，更不要使用 `--force-new`。**

只有完成线上对账后，才能继续自动流水线。Day45 当前没有已知发布地址，仍按未发布处理。

## 7. 恢复百度网盘和博客园登录态

### 百度网盘

`.pipeline/pan-cookies.txt` 是 Netscape Cookie 文件，供 `curl -b` 使用，权限应为 `600`。如果下载返回登录页、403 或文件大小异常，需要重新登录百度网盘并提取 Cookie：

```bash
chmod 600 .pipeline/pan-cookies.txt
node .pipeline/pan-extract-cookies.mjs
```

该脚本使用 `browser-profile/` 的 Playwright 登录态。不要打印或外发 Cookie 内容。

### 博客园

发布、图片上云主要使用 `secrets/cnblogs.env` 中的 API 凭证；**合集归集使用浏览器登录态**，两者不是一回事。

合集操作前，用项目同一 `browser-profile/` 登录 `https://i.cnblogs.com/`。旧自动化若提示“博客园网页端未登录”，只影响合集归集，不代表文章发布失败。

## 8. Day45 断点续传

源文件：

```text
网盘：/自动上传/课程/Luffy/AI 智能体/6 期/day45.zip
本地：downloads/ai-agent-6/day45/_staging/day45.zip
预期：739,980,294 字节
```

保留已有 ZIP，然后运行：

```bash
chmod +x .pipeline/resume-day.sh
bash .pipeline/resume-day.sh day45
```

脚本使用 `curl -C -`，会从本地现有字节位置继续，不应删除原 ZIP。网络错误后会重试，单次连接最长 2400 秒。

检查进度：

```bash
stat -c %s downloads/ai-agent-6/day45/_staging/day45.zip
```

完成后必须同时满足：

1. 文件大小严格等于 `739980294`。
2. `.pipeline/day45-crc.log` 包含 `No errors detected`。
3. `unzip -t` 返回成功。

大小不符、CRC 失败或 Cookie 失效时，不得进入发布流程。CRC 失败应重新下载损坏文件，不能只改状态文件跳过校验。

## 9. Day45 下载后的发布流程

标准顺序如下，工作目录均为：

```bash
cd "$COURSE_PUBLISH_ROOT/.dsh/skills/course-publish/scripts"
```

### 9.1 解压并更新下载状态

解压目标：

```text
downloads/ai-agent-6/day45/_extracted/day45/
```

重合并前，将 `state/ai-agent-6.edition8-progress.json` 的 Day45 标记为 `downloaded: true`，否则 `edition8.mjs` 可能再次访问网盘下载。

### 9.2 重合并和 PPT 转图

```bash
node edition8.mjs --course=ai-agent-6 --only=day45
```

检查 `output/ai-agent-6/00-day45.md`：PPT 章节必须包含逐页图片引用，不能只有从 PPT 抽取的文本。图片数为 0 时停止发布。

### 9.3 图片上云，生成 preview

```bash
node stage.mjs --course=ai-agent-6 --only=day45
```

只有 `state/ai-agent-6.staging.json` 中 Day45 为 `staged` 才继续。图床 403、配额限制或部分图片失败时，保留待续传状态，不得发布残缺文章。

### 9.4 预览审核和批准

检查：

```text
output/ai-agent-6/previews/00-day45.preview.md
```

要求：

- 不含本地 `assets/`、文件路径或 `file://` 图片引用。
- 图片全部是在线 URL，数量与合并稿一致。
- 标题、正文、PPT 页序完整。

预览通过后，在发布工作台批准，或按既有状态格式把 Day45 设置为 `approved`。`publish-approved.mjs` 只接受 `approved`，这是发布安全闸门，不要绕过。

### 9.5 发布

```bash
node publish-approved.mjs --course=ai-agent-6 --only=day45
```

成功后核验 `publish-state.json` 中的 URL，并在线检查标题、图片和合集。Day46 已经发布，不要包含在本命令中。

## 10. 恢复长期守护与统一监控页

沙箱中的下列文件已丢失，需从最新本地备份恢复；没有备份时，应按本节职责重建后再启动：

- `.pipeline/supervisor.mjs`：并发监控 Day45/Day46，续传、自愈、CRC、发布和完成标志。
- `.pipeline/run-daemon.sh`：supervisor 异常退出后 30 秒重启，看到 `ALL_DONE` 后退出。
- `.pipeline/monitor-server.mjs`：统一页面和工作台反向代理。
- `.pipeline/run-monitor.sh`：监控服务退出后 3 秒重启。

**不要直接改用旧 `.pipeline/auto-pipeline.mjs` 替代。** 它仍使用会误判进程的 `pgrep -f`，且是早期串行版本。

恢复后启动：

```bash
chmod +x .pipeline/run-daemon.sh .pipeline/run-monitor.sh .pipeline/resume-day.sh
setsid nohup bash .pipeline/run-daemon.sh >> .pipeline/daemon.out 2>&1 < /dev/null &
setsid nohup bash .pipeline/run-monitor.sh >> .pipeline/monitor-wrapper.out 2>&1 < /dev/null &
```

统一入口：

- 监控与工作台：http://127.0.0.1:5718
- 工作台独立服务：http://127.0.0.1:5717
- 实时状态：http://127.0.0.1:5718/api/status

守护要求：

- supervisor 必须有单实例 PID 锁，不能重复启动。
- 下载进程检测使用 `/proc/<pid>/cmdline` 精确匹配 `resume-day.sh day45`，不要用 `pgrep -f`。
- 文件 10 分钟无增长才重启下载；正常退出且 CRC 通过的章节显示“已完成”，不是“未运行”。
- 发布前读取 `publish-state.json`，已发布章节只补合集，不重复发文。
- 完成标志：`DONE-day45`、`DONE-day46`、`ALL_DONE`。
- `DONE-day46` 只代表发文完成；合集归集仍需单独核验。

如果没有恢复监控页，至少让续传脚本在本地终端、`tmux` 或 `systemd` 中长期运行，并定期查看 `.pipeline/day45-download.log`。

## 11. 合集归集与最终顺序

重新登录博客园网页端后，将 Day46 归集到 `AI智能体六期`（ID `45437`）；Day45 发布后做同样操作。

最终按课程 Day 数字顺序排列。新文章加入后，要同时检查：

1. 博客园后台合集条目数和顺序。
2. 任一篇文章公开页显示的合集名称与数量。
3. 本地 `state/ai-agent-6.collection.json` 是否与线上一致。

注意：沙箱中的该文件也已回滚为早期创建记录，不能单独用它判断线上合集现状。

全部可发布章节完成前，不删除旧合集 `38391`。

## 12. 后续剩余任务

1. Day46：补合集、核验线上 45 张图片、同步本地发布与合集状态。
2. Day45：续传、大小校验、CRC、解压、重合并、PPT 图片审计、上云、批准、发布、合集归集。
3. Day33–Day40、Day42–Day43、Day49–Day52：图床可用后逐章重新 stage、审核和发布。
4. Day44、Day48：保持 blocked，除非找到可靠六期源文件。
5. 最后统一校验合集顺序、公开页图片和文章数量，再决定是否删除旧合集 `38391`。

## 13. 常见问题

| 现象 | 处理方式 |
|---|---|
| 下载慢，约几十到一百 KB/s | 百度网盘单连接限速及断连导致；保留 ZIP，让 `curl -C -` 续传，不要从头下载 |
| `SSL_ERROR_SYSCALL`、curl 28 超时 | 网盘连接中断；确认文件随后仍增长，持续无增长再重启续传 |
| PPT 合并后没有图片 | 检查 `soffice` 和 `.venv` 中的 `pymupdf`；禁止把纯文本降级稿当完整 PPT 发布 |
| 图床 403 | 停止发布，保留 staging 状态，等待图床恢复后重试 |
| 发文成功但合集失败 | 检查浏览器 `i.cnblogs.com` 登录态；不要重复发文 |
| 本地状态说 Day46 未发布 | 以线上 `23645478` 为准补状态，不要重发 |
| 面板 Day46 下载进程曾显示红色“未运行” | 是旧 UI 误判；已发布/CRC 完成应显示“已完成” |
| 沙箱脚本或进度消失 | 停止以沙箱为事实源，从本地备份恢复；以后定期备份 `state`、ZIP 和脚本 |

## 14. 每次启动前的安全清单

- [ ] 远端和本地没有同时执行发布。
- [ ] 已线上核验 Day46，未把它重新加入发布队列。
- [ ] 本地路径、`COURSE_PUBLISH_ROOT` 和 `.pipeline` 硬编码路径一致。
- [ ] 百度 Cookie、博客园 API 凭证、浏览器登录态分别可用。
- [ ] ZIP 大小与 CRC 都通过，才允许解压和合并。
- [ ] PPT 已逐页转图，preview 无本地图片、无 403。
- [ ] 只发布 `approved` 且未发布的章节。
- [ ] 日志、状态、下载文件和脚本均有本地备份。
