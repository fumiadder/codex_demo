# Render Free + Neon Free：发布、存储与后续迁移

## 当前状态（2026-10-09）

采用已有 Render Free 服务 [zhixu-free-test.onrender.com](https://zhixu-free-test.onrender.com)，数据库与媒体放到 Neon Free。Neon 项目为 `blue-band-05556085` / `zhixu-workbench`，AWS 新加坡；数据库 `zhixu`，私有桶 `uploads`、`bedtime-audio` 已创建。Native SQL 已确认 PostgreSQL 17.11；13 张表、6 个索引、76 列与此前已验证源码一致。首位管理员邀请已签发，但账号尚未注册。

**用户已确认旧测试站没有真实数据，本次全新安装，无需迁移。新版尚未 Live，公网仍是旧版。** 用户已登录并确认 My Workspace，授权使用 Render 连接工具部署现有免费服务；私有配置和构建命令已应用，不需要再次登录、手动导入环境变量或重复修改构建命令。

已验证提交 `564386d` 的 [main CI](https://github.com/fumiadder/codex_demo/actions/runs/37926678128) 成功。但 Render 直接连接部署 `dep-db4de23l550s73bd8290` 与池化连接部署 `dep-db4dftdg1s2s739346hg` 均在约 10 秒后出现 PostgreSQL TCP 连接超时；两个私有桶的 S3 HEAD 启动检查通过。Native Neon SQL 的 `SELECT 1` 成功不能代替 Render 应用连接。真实云端 PUT／GET／Range／删除及重启保留尚未验收。

正在验证显式启用的 Neon WebSocket 传输。需先通过代码检查和 CI，再更新同一 Render 服务；不能把正在验证的实现或成功构建当作公网已上线。不创建收费服务、磁盘或定时任务。

| Neon Free 限额 | 规划 |
| --- | --- |
| PostgreSQL 1 GB | 包括账号、记录、媒体元数据及索引，持续检查使用量 |
| 对象存储 5 GB | 两个桶共享，应用总容量上限应留出余量 |
| 100 CU-hours／月 | 监控计算用量，保留自动休眠 |
| 月度出站流量 5 GB | 数据库与对象存储共享；音频播放、迁移校验和备份都可能消耗 |

免费额度不是永久服务承诺。超额前减少用量或停用相关功能，不能自动升级付费；也不能在 PostgreSQL 故障时自动改用 SQLite。大陆访问与首请求唤醒速度必须用实际网络验证，目前没有连通性保证。

## 服务端环境与访问控制

私有凭据文件位于 `/workspace/.zhixu-secrets/render-neon.env`，在仓库外。Render 已私下应用环境变量；更新环境变量会自动触发部署，即使 Auto-Deploy 关闭。不要把文件内容、数据库 URL、S3 密钥、邀请码或用户数据粘贴到聊天、构建日志、源码、发布包。

| 环境变量 | 配置 |
| --- | --- |
| `DATABASE_URL` | Neon `zhixu` 的连接串；必须非空，按所选传输使用匹配的私有配置 |
| `DATABASE_TRANSPORT` | 默认 `native`；待验证的 WSS 路径必须显式设置 `neon-ws`，不能自动回退 |
| `MEDIA_STORAGE_BACKEND` | `s3` |
| `AWS_REGION` | `ap-southeast-1` |
| `AWS_ENDPOINT_URL_S3` | 该项目实际的 HTTPS Neon S3 endpoint |
| `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` | 私有服务器凭据，服务器专用 storage:read/write；当前凭据是项目分支范围，桶级隔离由私有 API 授权负责 |
| `AWS_UPLOADS_BUCKET` | `uploads` |
| `AWS_BEDTIME_AUDIO_BUCKET` | `bedtime-audio` |
| `REGISTRATION_MODE` | `invite` |
| `WORKSPACE_TEST_MODE` | `0` |
| `STORAGE_PERSISTENCE` | 已配置 `persistent`；这是配置标记，仍需实际数据库和对象读写验收 |
| `TRUST_PROXY` | `1` |
| `ALLOWED_ORIGINS` | `https://zhixu-free-test.onrender.com` |
| `HOST`、`DATA_DIR` | `0.0.0.0`、`/tmp/zhixu-test-data`；此目录仅作临时目录 |
| `MAX_SPACE_STORAGE_BYTES` | 建议 `1073741824`（1 GiB） |
| `MAX_TOTAL_STORAGE_BYTES` | 建议 `4294967296`（4 GiB），为 5 GB 对象额度保留余量 |

确认并移除会覆盖上述配置的旧测试设置。桶保持私有；浏览器只通过登录后的同站 API 访问文件与音频，服务端检查账号、空间成员权限及 CSRF。不要发给前端 S3 密钥或长期签名 URL，也不要把“知道链接”当作访问权限。`/api/health` 只检查数据库，不能替代对象存储验证。

## 待验证的 Neon WebSocket 传输

`DATABASE_TRANSPORT=neon-ws` 仅改变数据库会话的网络传输：连接原始 Neon 直接主机名的 `wss://<direct-host>/v2`，由 pg8000 使用 PostgreSQL 原生协议、实际交互式事务、READ COMMITTED 隔离和既有 advisory lock。不是 HTTP SQL 请求或无状态事务模拟。

外层 WSS 强制 CA 与主机名验证，提供 TLS 加密。与官方 Neon SDK 的 WSS 用法一致，内层 PostgreSQL SSL 关闭。WSS 不支持 `channel_binding=require`，配置中明确要求时必须拒绝；因此候选 WSS 私有连接串显式移除该参数。原始严格 TLS／channel binding 的 native 连接串保留给可信终端的原生连接、CLI 和备份，不通过日志或源码公开。传输失败不会改用另一条连接路径或 SQLite。

这些是待验证实现和配置要求。尚不能宣称 WSS 已从 Render 成功连接、提交云端数据或通过上传验收；需要先完成本地真实协议／事务测试及 CI，再部署、核对实际运行结果。

## 以后存在旧数据时：快照与迁移

本次没有需迁移的旧数据，不执行以下流程。今后迁移已有安装时才使用，目标须另行准备为空的专用数据库与桶；当前已有 bootstrap 邀请的 Neon 目标不能直接当作空迁移目标。

先停止源端写入和目标应用，保留原始 SQLite、相关 WAL 与完整媒体导出。只复制一个正在写入的 `folio.sqlite3` 文件不构成可靠备份。以下 `/private/export/data` 和 `/private/backup/...` 均为示意路径：替换为操作者设备或受控私有备份目录，不放入仓库。源目录须包含 `folio.sqlite3`、`uploads/`、`bedtime-audio/`。

在已安装 `requirements.txt` 依赖的环境，从应用目录执行：

```bash
umask 077
python scripts/migrate_storage.py \
  --source-data /private/export/data \
  --snapshot-dir /private/backup/source-audit-01
```

默认只读源数据并创建新的私有快照，不连接云端、不迁移。它使用 SQLite backup API 捕获已提交 WAL 内容，检查完整性、外键、12 张业务表、每个被引用媒体的大小和 SHA-256；可额外存在空的 `storage_jobs` 表，队列非空则拒绝。保留账号 ID、密码哈希及密码箱密文，不输出个人行或对象名。不能据此声称密码箱密文已能解密，仍需原用户验收。

**每次命令都要求全新的 `--snapshot-dir`。** 失败后先检查并保留快照，不覆盖旧目录。目标检查前，仅在可信进程环境中加载私有云凭据：

```bash
python scripts/migrate_storage.py \
  --source-data /private/export/data \
  --snapshot-dir /private/backup/target-check-01 \
  --check-target
```

`--check-target` 只读检查目标表与对应对象，不创建缺失的数据库表。全新数据库可能返回“schema is not initialized”；源快照仍可用于审计，正式 `--apply` 才初始化目标表。目标业务表或 `storage_jobs` 有行、出现不支持的表、存在相同目标对象时均拒绝覆盖。这个检查不会证明整个桶为空，操作者必须另行确认两个桶专用且没有其他写入者。

只有源端停止写入、目标应用停止、目标数据库为空、两个新桶专用且没有其他写入者、独立备份可恢复时，才执行：

```bash
python scripts/migrate_storage.py \
  --source-data /private/export/data \
  --snapshot-dir /private/backup/migration-01 \
  --apply --maintenance-confirmed --exclusive-buckets-confirmed
```

数据库事务与 S3 不是跨系统原子提交。程序记录私有 `migration-journal.json`，上传后下载校验哈希，再逐行校验数据库。上传阶段失败时仅尝试删除本次创建的对象；数据库写入／提交结果不明时保留对象，journal 标记 `database-outcome-needs-verification`。此时保持维护状态，私下核对数据库实际提交情况及对象后再决定恢复；不要盲目重跑、删除媒体或删除 journal。桶接口没有可靠的 create-only PUT 保证，两个确认参数不能代替实际停止其他写入者。

## 首个账号与后续邀请

本次已绑定用户提供的管理员邮箱并签发一次性 bootstrap 邀请。私有文件 `/workspace/.zhixu-secrets/bootstrap-invite.json` 中的邀请码有效至 **2026-10-10 19:14（北京时间）**，尚未创建账号。先完成新版 Live 和存储验收，再由本人在网页注册并输入密码；不把密码或邀请码发到聊天、构建日志或公开页面。

以下命令仅供未来新的空安装重新签发使用，当前不需要重复 bootstrap。若今后迁移已有账号，先完成迁移并用原账号登录，不先写入 bootstrap 邀请。从操作者可信终端加载与目标数据库一致的 native `DATABASE_URL`；Render Free 不提供 SSH，也没有公共发邀请码 API。输出保存在仓库外的私有文件：

```bash
set -eu
umask 077
python -c 'import os; assert os.environ.get("DATABASE_URL"), "DATABASE_URL is required"'
python scripts/manage_access.py --data-dir /tmp/zhixu-bootstrap \
  bootstrap --email owner@example.com --ttl-hours 24 --json \
  > /private/backup/bootstrap-invite.json
```

邀请码绑定邮箱、一次性使用、默认 24 小时有效；只在操作者设备查看，经可信渠道交给本人。密码在网页注册时由本人输入，不出现在命令或聊天里。已有账号后新增用户使用 `invite`；遗失未过期邀请码先 `revoke`。邀请创建账号，不自动授予已有共享空间权限。

## 更新现有 Render 服务

当前由 Codex 使用已授权的 Render 连接工具部署。保持已有 Free 新加坡服务，实际已保存配置为：

- Repository：`fumiadder/codex_demo`；Branch：`deploy/zhixu-free-test`；Root Directory：空，即仓库根目录。
- Build Command：`cd workbench && pip install -r requirements.txt && python -m py_compile server.py bedtime.py persistence.py media_storage.py storage_jobs.py`。
- Start Command：`cd workbench && python server.py`；Health Check Path 当前为空，验收直接请求 `/api/health`。
- Auto-Deploy 当前关闭（`autoDeploy=false`／trigger `off`）；环境变量更新仍会触发部署。

代码先通过 Workbench CI，再推进部署分支并发布该提交。云端运行必须同时满足数据库可连接、私有桶可访问以及完整应用健康；构建成功或桶 HEAD 成功都不足以证明已上线。首轮验收后再按更新安排启用 After CI Checks Pass 自动部署。

只读公网工作流 `workbench-readonly.yml` 检查固定现有网站，支持手动运行及成功的 main Workbench CI 完成后运行，限公开仓库，不带凭据、不注册账号、不写入数据。不要运行面向旧临时开放注册测试站的 `workbench-smoke` 工作流。

上线验收需要：HTTPS 与安全响应头、`/api/health`、`/api/config` 返回 `testMode=false` / `storagePersistence=persistent` / `registrationMode=invite`；bootstrap 注册、记录及密码箱、未授权文件访问拒绝、私有文件上传下载、音频 Range、删除清理、退出／换账号／撤权、窄屏与横屏操作；重启后再次确认数据和媒体仍在。真实网络上检查大陆访问及 Render 休眠后的唤醒。通过后才记录“已发布”；本次没有迁移，不能记为“已迁移”。

内置原创故事、设备朗读等现有功能先验收。云端故事生成、实时全网检索和本人音色克隆的供应商密钥尚未配置，不属于当前已经可用的能力；上传素材不能等同于克隆或本人音色合成成功。

## 备份、清理与回退

Neon Free 只有约 6 小时 PITR 窗口及 1 个手动快照，不能代替独立备份。迁移前、每次发布前，以及使用期间定期在操作者设备或私有备份位置保存 PostgreSQL `pg_dump` 与两个桶的完整对象备份，保留清单／哈希，并在隔离环境验证恢复。备份涉及账号与密文，访问权限应限制为操作者；不要用公开下载链接。数据库与对象需在停写窗口取得一致备份，单独恢复其中一个可能产生缺失引用。

Neon 对象存储不支持可依赖的自动 lifecycle TTL；应用在启动或有登录用户请求时清理过期故事音频；S3 清理使用一次最多 4 个任务的临时后台线程，无固定轮询唤醒数据库，并以任务 generation 避免迟到上传删除丢失，并用持久化 `storage_jobs` 保留上传预留和删除重试。网络或权限失败不等于对象不存在；失败任务仍计入配额，后续重试。定期查看队列、对象使用量及出站流量，不能假设长时间休眠的服务仍在清理。

失败时先停写并检查实际数据库状态。可回退到兼容 PostgreSQL／S3 的已验证代码版本，保留同一私有云存储配置；最早的 SQLite-only 镜像不能直接接管迁移后的数据。需要恢复数据时从独立备份恢复到隔离目标、验证后再切换。不要清空 Neon、删除桶、自动降级 SQLite 或用空白数据库掩盖故障。

既有 native 存储实现已在本地 SQLite、真实 PostgreSQL 17、手机／PC 浏览器及合成的迁移样本中验证；供应商使用明确模拟，不验证真人克隆质量。`564386d` 的 main CI 已通过。WSS 新代码正在验证，不能沿用 native 测试结果宣称该路径通过。云端表结构、native SQL 查询与两个私有桶 HEAD 已核对；真实 Render WSS 连接、云端文件读写、公网 Live 和国内网络验收仍待执行。
