# Render Free + Neon Free：迁移与发布操作

## 当前状态（2026-10-09）

采用已有 Render Free 服务 [zhixu-free-test.onrender.com](https://zhixu-free-test.onrender.com)，数据库与媒体放到 Neon Free。Neon 项目为 `blue-band-05556085` / `zhixu-workbench`，AWS 新加坡；数据库 `zhixu`，私有桶 `uploads`、`bedtime-audio` 已创建。Native SQL 已确认 PostgreSQL 17.11；13 张表、6 个索引、76 列均与源码一致，13 张表均为空。应用 psycopg 的真实事务与 HTTP 功能已在本地 PostgreSQL 17 验证，云端 psycopg 连接及真实 S3 对象读写尚未验证。

**尚未迁移旧数据，尚未把本次修改发布到公网。** 旧 Render 服务是否有需保留的账号、记录或媒体仍待确认。Render Free 没有可用的 SSH／完整数据导出 API；必须先取得可恢复的完整导出，或由用户确认旧服务没有需保留的数据，再部署。旧实例的临时磁盘可能在重启、休眠或部署时丢失，不能先部署再补备份。

本次采用手动 Render Dashboard 操作，不使用 Render 插件工具，不创建收费服务、磁盘或定时任务。

| Neon Free 限额 | 规划 |
| --- | --- |
| PostgreSQL 1 GB | 包括账号、记录、媒体元数据及索引，持续检查使用量 |
| 对象存储 5 GB | 两个桶共享，应用总容量上限应留出余量 |
| 100 CU-hours／月 | 监控计算用量，保留自动休眠 |
| 月度出站流量 5 GB | 数据库与对象存储共享；音频播放、迁移校验和备份都可能消耗 |

免费额度不是永久服务承诺。超额前减少用量或停用相关功能，不能自动升级付费；也不能在 PostgreSQL 故障时自动改用 SQLite。大陆访问与首请求唤醒速度必须用实际网络验证，目前没有连通性保证。

## 服务端环境与访问控制

私有凭据文件位于 `/workspace/.zhixu-secrets/render-neon.env`，在仓库外。由操作者在可信终端加载并在 Render Environment 中私下配置；不要把文件内容、数据库 URL、S3 密钥、邀请码或用户数据粘贴到聊天、构建日志、源码、发布包。

| 环境变量 | 配置 |
| --- | --- |
| `DATABASE_URL` | Neon `zhixu` 的 PostgreSQL TLS 连接串；必须非空 |
| `MEDIA_STORAGE_BACKEND` | `s3` |
| `AWS_REGION` | `ap-southeast-1` |
| `AWS_ENDPOINT_URL_S3` | 该项目实际的 HTTPS Neon S3 endpoint |
| `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` | 私有服务器凭据，服务器专用 storage:read/write；当前凭据是项目分支范围，桶级隔离由私有 API 授权负责 |
| `AWS_UPLOADS_BUCKET` | `uploads` |
| `AWS_BEDTIME_AUDIO_BUCKET` | `bedtime-audio` |
| `REGISTRATION_MODE` | `invite` |
| `WORKSPACE_TEST_MODE` | `0` |
| `STORAGE_PERSISTENCE` | `persistent`，只在数据库与对象后端均验证后设置 |
| `TRUST_PROXY` | `1` |
| `ALLOWED_ORIGINS` | `https://zhixu-free-test.onrender.com` |
| `HOST`、`DATA_DIR` | `0.0.0.0`、`/tmp/zhixu-test-data`；此目录仅作临时目录 |
| `MAX_SPACE_STORAGE_BYTES` | 建议 `1073741824`（1 GiB） |
| `MAX_TOTAL_STORAGE_BYTES` | 建议 `4294967296`（4 GiB），为 5 GB 对象额度保留余量 |

确认并移除会覆盖上述配置的旧测试设置。桶保持私有；浏览器只通过登录后的同站 API 访问文件与音频，服务端检查账号、空间成员权限及 CSRF。不要发给前端 S3 密钥或长期签名 URL，也不要把“知道链接”当作访问权限。`/api/health` 只检查数据库，不能替代对象存储验证。

## 旧数据快照与迁移

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

## 首个账号与邀请

已有账号必须先迁移，之后用原账号登录。只有用户确认全新安装且无需迁移时，才对新 Neon 数据库生成 bootstrap 邀请。**不要在迁移前 bootstrap**：它会写入邀请表，使目标不再为空。

从操作者的可信终端加载与应用一致的 `DATABASE_URL`；Render Free 不提供 SSH，也没有公共发邀请码 API。在确定数据库已正确初始化后执行，邮箱替换为本人邮箱，输出保存在仓库外的私有文件：

```bash
set -eu
umask 077
python -c 'import os; assert os.environ.get("DATABASE_URL"), "DATABASE_URL is required"'
python scripts/manage_access.py --data-dir /tmp/zhixu-bootstrap \
  bootstrap --email owner@example.com --ttl-hours 24 --json \
  > /private/backup/bootstrap-invite.json
```

邀请码绑定邮箱、一次性使用、默认 24 小时有效；只在操作者设备查看，经可信渠道交给本人。密码在网页注册时由本人输入，不出现在命令或聊天里。已有账号后新增用户使用 `invite`；遗失未过期邀请码先 `revoke`。邀请创建账号，不自动授予已有共享空间权限。

## 手动更新现有 Render 服务

先通过代码检查和本地验证，验证 psycopg 真实连接、两个私有桶的上传／下载／Range／删除，并完成旧数据决策与备份。仅使用已有 Free 服务，在 Dashboard → Settings 核对：

- Repository 保持现有仓库；Branch：`deploy/zhixu-free-test`；Root Directory：`workbench`。
- Build Command：`pip install -r requirements.txt && python -m py_compile server.py bedtime.py persistence.py media_storage.py storage_jobs.py`。
- Start Command：`python server.py`；Health Check Path：`/api/health`；实例保持 Free、新加坡。
- Auto-Deploy 保持关闭（`autoDeploy=false`／trigger `off`），私下配置上述环境变量。

在已验证提交上执行一次 Manual Deploy。通过首轮公网检查后，再按更新安排启用自动部署；此前不要让推送自动重启旧实例。不要沿用旧的仅 `py_compile server.py` 构建命令，运行 PostgreSQL 与 S3 后端需要安装依赖。

上线验收需要：HTTPS 与安全响应头、`/api/health`、`/api/config` 返回 `testMode=false` / `storagePersistence=persistent` / `registrationMode=invite`；原账号或 bootstrap 注册、记录及密码箱、未授权文件访问拒绝、私有文件上传下载、音频 Range、删除清理、退出／换账号／撤权、窄屏与横屏操作；重启后再次确认原有数据和媒体仍在。真实网络上检查大陆访问及 Render 休眠后的唤醒。全部通过后才记录“已发布、已迁移”。

## 备份、清理与回退

Neon Free 只有约 6 小时 PITR 窗口及 1 个手动快照，不能代替独立备份。迁移前、每次发布前，以及使用期间定期在操作者设备或私有备份位置保存 PostgreSQL `pg_dump` 与两个桶的完整对象备份，保留清单／哈希，并在隔离环境验证恢复。备份涉及账号与密文，访问权限应限制为操作者；不要用公开下载链接。数据库与对象需在停写窗口取得一致备份，单独恢复其中一个可能产生缺失引用。

Neon 对象存储不支持可依赖的自动 lifecycle TTL；应用在启动或有登录用户请求时清理过期故事音频；S3 清理使用一次最多 4 个任务的临时后台线程，无固定轮询唤醒数据库，并以任务 generation 避免迟到上传删除丢失，并用持久化 `storage_jobs` 保留上传预留和删除重试。网络或权限失败不等于对象不存在；失败任务仍计入配额，后续重试。定期查看队列、对象使用量及出站流量，不能假设长时间休眠的服务仍在清理。

失败时先停写并检查实际数据库状态。可回退到兼容 PostgreSQL／S3 的已验证代码版本，保留同一私有云存储配置；最早的 SQLite-only 镜像不能直接接管迁移后的数据。需要恢复数据时从独立备份恢复到隔离目标、验证后再切换。不要清空 Neon、删除桶、自动降级 SQLite 或用空白数据库掩盖故障。

已验证：SQLite 101 项检查通过（5 项 PG 专用检查跳过）；本地 PostgreSQL 105 项通过（SQLite 旧库升级检查另在 SQLite 模式执行）。手机 9 组、PC 8 组、睡前故事 11 组、暖光故事 11 组、生活入口 6 组浏览器检查通过，供应商明确模拟，不验证真人克隆质量。云端表结构和私有桶属性已核对；真实云端文件读写、旧数据迁移、公网 Live 和国内网络验收仍待执行。
