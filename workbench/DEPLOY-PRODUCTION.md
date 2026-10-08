# 正式公网部署与持续更新

当前正式服务器、域名与 SSH 凭据尚未提供，**此配置没有部署到生产环境**。Render 免费测试站仍是测试环境，不能替代持久化生产存储。本方案不购买云资源，也不依赖 Render 插件。

## 选择服务器

使用已授权的 Linux 服务器，建议 Ubuntu 24.04、至少 2 核 / 2 GB 内存和足够容纳上传文件及备份的磁盘。应用为单节点 SQLite，短暂维护窗口内更新；此版本没有水平扩容或零停机发布承诺。中国内地服务器的域名需完成适用的备案手续；境外服务器仍需用真实国内移动网络验证访问。

服务器安装 Docker Engine、Docker Compose v2.24 或更高、Python 3、OpenSSH、`flock`。域名 A/AAAA 记录指向服务器，开放公网 TCP 80/443，SSH 仅授予运维人员。Caddy 自动申请 HTTPS 证书。不要将容器 8000 端口或数据目录暴露到公网。

在服务器创建 `/srv/zhixu/{shared,releases,incoming,state,backups}`，让专用部署账号拥有该目录，并授权其使用 Docker。Docker 权限接近主机 root 权限，使用专用密钥。若该账号没有授权，本配置不会自行提权。服务器需要能拉取 Python/Caddy 镜像；可以在 `.env` 配置经过授权的 `CADDY_IMAGE` 镜像来源。

## 一次性设置

把项目 `.env.example` 复制到服务器 `/srv/zhixu/shared/.env`，权限设为 `600`，修改 `WORKSPACE_DOMAIN`。从首次发布起保持 `COMPOSE_PROJECT_NAME`、`WORKSPACE_DATA_VOLUME` 不变。邀请码注册、正式持久化声明及关闭测试模式由 Compose 固定设置：

```text
REGISTRATION_MODE=invite
STORAGE_PERSISTENCE=persistent
WORKSPACE_TEST_MODE=0
```

数据在固定的 `zhixu_data` 卷，证书在 `zhixu_caddy_data` 与 `zhixu_caddy_config` 卷。发布目录是按完整源码 SHA 区分的代码；不是数据目录。不要使用 `docker compose down -v` 或清理这些卷。`persistent` 是部署配置声明，仍需磁盘备份与恢复演练。

睡前故事默认使用本地故事与设备系统音色。`DASHSCOPE_API_KEY` 留空时云端音色克隆和合成不可用；配置密钥后，用户实际调用可能产生服务商用量费用。实时搜索选择 `STORY_SEARCH_PROVIDER=opensearch` 或 `tavily`，并填写对应服务端配置。模型密钥只存在于服务器 `.env`，不写入源码、页面、GitHub 发布包或日志。产品不会因部署自动训练音色或创建收费资源。

## 首次注册与协作

首个健康部署完成后，在服务器终端执行：

```sh
cd /srv/zhixu/current
docker compose --env-file /srv/zhixu/shared/.env exec app python scripts/manage_access.py bootstrap --email owner@example.com
```

将输出的一次性邀请码通过可信渠道交给绑定邮箱的使用者，在网站注册。此操作只能在空账号库执行，邀请码默认 24 小时有效，数据库仅保存哈希。邀请码不要粘贴到公共聊天、CI 或部署日志。后续新用户用 `invite --email colleague@example.com`；遗失或不再需要的邀请码用 `revoke --email colleague@example.com` 撤销。注册只创建账号，已有空间的共享权限仍由空间主人单独授予。

## GitHub 持续更新

仓库 `.github/workflows/workbench-ci.yml` 在每次分支 push 和 pull request 时检查后端测试、JavaScript、脚本、Compose，以及移动端、跨标签账号隔离和晚安故事浏览器回归。标准公开仓库运行器用于这些检查；仓库改为私有后工作流会跳过，需先明确运行器和费用政策。

在仓库 Settings → Environments 创建 `production` 环境，并将 Deployment branches 限制为 `main`，设置以下 secrets。不要在聊天中粘贴私钥：

| 名称 | 用途 |
| --- | --- |
| `WORKBENCH_SSH_HOST` | 已授权服务器的域名或 IP |
| `WORKBENCH_SSH_USER` | 专用部署账号 |
| `WORKBENCH_SSH_PORT` | SSH 端口；为空默认 22 |
| `WORKBENCH_SSH_KEY` | 专用部署私钥 |
| `WORKBENCH_SSH_KNOWN_HOSTS` | 从可信控制台核对指纹后固定的完整 known_hosts 记录 |

必须独立核对服务器 SSH 指纹；不能仅从未认证的 `ssh-keyscan` 结果直接信任。非标准端口使用 `[host]:port` 格式记录。工作流强制 `StrictHostKeyChecking=yes`、`BatchMode=yes`，不会关闭身份检查或交互式接受未知主机。

`main` 的 push 先完成 **Workbench CI**，成功后 **Workbench production deployment** 发布同一个 40 位 SHA。也可手动运行生产工作流，填写已经通过 push CI 且属于 `main` 历史的 SHA。工作流再次验证提交、CI 结果与 main 归属；未配置必要服务器 secrets 时明确显示“未部署”，不连接未知主机。设置 `main` 分支保护，要求 **Workbench CI / checks** 通过再合并。后续新功能合并到 `main` 即可按这条流水线更新同一个站点。

如不使用 GitHub Actions，可在有成功 CI 的源码版本上生成代码包，再通过已核对身份的 SSH 上传：

```sh
python3 scripts/package_release.py --sha FULL_40_CHARACTER_SHA --output /tmp/workbench.tar.gz
```

源码包只包含应用、静态文件和运行脚本。不要自行将整个项目目录、真实数据、`.env` 或备份打包。解压到 `/srv/zhixu/releases/FULL_40_CHARACTER_SHA` 后执行：

```sh
bash /srv/zhixu/releases/FULL_40_CHARACTER_SHA/scripts/deploy.sh --root /srv/zhixu --sha FULL_40_CHARACTER_SHA
```

## 发布、失败回退与结构变化

发布先验证输入和 Compose，再构建新应用镜像并拉取网关镜像。旧应用在构建期间保持运行。构建成功后取得专属运维锁，停止此工作台应用，将 SQLite 数据库、WAL 与全部上传文件复制成同一时点快照，执行数据库检查和逐文件 SHA-256 检查。完整备份成功后才替换应用。应用内部健康、生产配置以及公网 HTTPS 首页/API/安全响应头全部通过后，才更新 `current` 与 `state/current-sha`。

应用失败时回退保存的不可变旧镜像，数据库与上传卷保留当下状态。**自动回退不恢复旧数据库**，因此不会用发布前快照覆盖部署期间已提交的新写入。初次发布没有旧镜像时失败会停止新应用、保留数据卷并报错。运维锁避免备份、部署重叠。

镜像记录 server 与 bedtime 全部 `SCHEMA` 的合并哈希。存在旧应用且哈希变化时，脚本在停止旧应用前拒绝自动更新，要求单独审查迁移、容量和回退兼容性。首次睡前模块的表是新增 `CREATE TABLE IF NOT EXISTS`，不修改现有工作台表；旧程序可以忽略新增表，但实际升级仍需遵循结构变化流程。此版本没有通用自动迁移绕过开关。

HTTPS/证书失败同样使工作流失败。检查域名解析、80/443、镜像拉取权限与服务器日志；不要忽略失败后继续宣布上线。真正国内可达性还需用中国移动/联通/电信网络打开正式域名，桌面与 iOS/Android 浏览器验证。

## 备份、恢复与演练

在服务器手动执行一致性备份；该命令短暂停止应用，成功或失败后恢复此前运行状态：

```sh
bash /srv/zhixu/current/scripts/backup.sh --root /srv/zhixu
```

`state/latest-backup` 记录快照目录。备份包含私密数据，目录权限为 `700`、清单 `600`；应在服务器运维计划中安排每日备份，并用受保护的异地加密存储保留副本。当前没有自动删除备份或代替用户购买备份存储。只存于同一台服务器的快照不能抵御整机故障。

恢复仅允许创建 **新的** 卷，并检查校验和、SQLite 与媒体引用；目标卷已经存在时直接拒绝：

```sh
bash /srv/zhixu/current/scripts/restore.sh --backup /srv/zhixu/backups/SNAPSHOT --new-volume zhixu_restore_20261008
```

该命令不会切换生产服务，也不会覆盖现有数据。验证恢复数据与账号后，另行在维护窗口决定是否将 `WORKSPACE_DATA_VOLUME` 指向恢复卷；切换会退回快照时点，需先保留现有卷并协调晚于快照的写入。恢复音色样本和数据库不代表服务商端的音色资源仍可用，需核对外部服务状态。

可在临时环境运行 `python3 scripts/ops_qa.py` 复现生产流程：新卷首发、更新保留数据、备份恢复新卷、拒绝覆盖、失败健康检查回退镜像并保留快照后的新写入、损坏快照拒绝恢复。演练只操作唯一临时项目和假数据，并清理自己的容器/卷，不连接正式服务器。实际结果见 `OPS-VERIFICATION.md`。
