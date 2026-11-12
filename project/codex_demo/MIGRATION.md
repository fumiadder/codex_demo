# 知序工作台 · 迁移与复现指南

本快照是当前项目 `main` 分支（提交 `516f266`）的完整代码。解压后即可在另一台终端跑起来并复现当前功能状态。

## 快照内容

- **后端**：`server.py`（主服务）+ 模块 `daily.py`、`bedtime.py`、`export.py`、`notify.py`、`recur.py`、`templates.py`、`settings.py`、`identity.py`、`webpush.py`、`persistence.py`、`storage_jobs.py`、`media_storage.py`、`neon_transport.py`
- **前端**：`public/`（`app.js`、`index.html`、`styles.css`、`sw.js`、`manifest.webmanifest`、`vendor/markdown.js`、`icons/`）
- **测试**：`tests/`（单元测试）、`scripts/`（浏览器 QA、发布/部署/备份脚本）
- **部署**：`Dockerfile`、`compose.yaml`、`Caddyfile`、`render.yaml`、`.github/workflows/`
- **文档**：`README.md`、`VERIFICATION.md`、`AGENTS.md` 等

## 在新终端跑起来

### 方式 A：本地直接跑（最简单）

```bash
# 1. 解压
tar xzf zhixu-migration.tar.gz
cd codex_demo/workbench

# 2. 准备 Python 3.12 并装依赖（SQLite 模式无需装 PG 依赖也行，但全量建议装齐）
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# 浏览器 QA 需要：pip install playwright && playwright install chromium

# 3. 本地起服务（开发模式，注册开放）
DATA_DIR=./data PORT=8000 python server.py
# 打开 http://127.0.0.1:8000 即可
```

### 方式 B：Docker Compose（与生产一致）

```bash
cd codex_demo/workbench
mkdir -p data
docker compose up -d --build
# 打开 http://127.0.0.1
```

### 方式 C：部署到服务器（复刻当前生产）

1. 把整包传到服务器，解压到 `/srv/zhixu/`
2. 准备 `.env`（参考「当前状态」一节）
3. 执行 `bash scripts/deploy.sh --root /srv/zhixu --sha $(git rev-parse HEAD)`（脚本会构建、备份、健康检查、失败回滚）

## 当前状态（生产）

- 域名：`https://fuminer.xyz`（Let's Encrypt 正式证书，绿锁）
- 部署方式：Docker Compose + Caddy，源码在 `/srv/zhixu/releases/<sha>`，当前目录软链到该版本
- 账号：`fuminer`（用户名 `2521532473`）、`vivi`
- 后端：SQLite 持久化在容器卷 `/data/folio.sqlite3`
- 通知：站内通知 + Webhook（企业微信/Bark 等）+ Web Push（VAPID 密钥在 `.env` 的 `WEB_PUSH_VAPID_PRIVATE_KEY`）

## 当前已实现的功能

| 模块 | 能力 |
|---|---|
| 首页 | 两栏：左待办（待办/已完成/已删除状态标签）、右月历 + 本月日程（带图片缩略图，点日历内容弹窗） |
| 记录 | 「记一笔」统一弹窗：写随笔 / 设待办（日期+优先级）/ 附件 / 语音转文字 / 语音录入 |
| 今日记录 | 图片速记 + AI 识别（无密钥时显示待连接） |
| 待办 | 重复任务（每天/每周/每月/每年、间隔、截止/次数）、子任务、多标签、时间段、定时提醒（remindAt） |
| 通知 | 铃铛未读角标、站内通知列表、到期/定时提醒 |
| Webhook | SSRF 防护（仅公网 https，拒内网/保留地址/重定向），一次性 secret，轮转/测试/删除 |
| Web Push | 手机锁屏推送（VAPID + RFC8291 加密） |
| PWA | 可安装到手机桌面，图标显示未读数 |
| 模板库 | 10 个内置模板 + 自定义模板 |
| 快捷方式 | 首页常用功能自定义（服务端按账号持久化） |
| 日程月历 | 月历 + 日程双视图，农历显示，内容前几个字预览 |
| 触感反馈 | 手机点击按钮轻微震动（15ms，尊重减少动画设置） |
| 密码箱 | 端到端加密的私密记录 |
| 空间协作 | 多空间、成员权限（owner/editor/viewer）、加成员按用户名/手机号 |
| 数据导出 | JSON / Markdown / HTML 三种格式打包下载，含媒体文件 |
| 账号 | 用户名或手机号登录/注册，邀请制（一次性邀请码） |

## 验证

本地跑全量测试：

```bash
cd codex_demo/workbench
python -m unittest discover -s tests        # 单元测试（SQLite）
# 浏览器 QA（需要 playwright + chromium）：
QA_CHROMIUM=$(which chrome-headless-shell 2>/dev/null || echo /usr/bin/chromium) \
  python scripts/mobile_qa.py
python scripts/daily_browser_qa.py
python scripts/recur_calendar_qa.py
```

已知偶发：`tests/test_bedtime.py` 的 `test_large_base64_clone_owner_isolation_and_provider_id_not_exposed` 在 macOS 上偶尔因回环 BrokenPipe 失败，Linux CI 稳定通过。

## 生产验证过的功能

- 2b 批次（通知/Webhook/模板/快捷方式）生产烟测 26/26 PASS
- 登录改造：存量账号自动迁移，用户名 `2521532473` 可登录
- 今日记录：上传图片 → 保存 → 列表 → 图片显示全链路
- 手机端窄屏无横向溢出（320/360/390/430/844px 均通过）

## 注意

- `.env`、邀请码、VAPID 私钥、用户数据**不在**快照里，需要单独迁移或重新生成
- 如果迁移到不同域名/IP，记得改 `.env` 里的 `WORKSPACE_DOMAIN` 与 `WORKSPACE_SITE_ADDRESS`
- Web Push 依赖 HTTPS；换域名后 VAPID 密钥可复用
