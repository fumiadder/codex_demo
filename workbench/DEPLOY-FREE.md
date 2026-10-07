# 免费公网部署测试

目标平台：Render Free Web Service，Singapore 区域，原 Python 后端运行，无第三方 Python 包。仅使用平台免费 HTTPS 子域名，不创建付费实例、持久化磁盘或数据库。本配置严格固定 `plan: free`，不自动升级规格。

2026-10-07 核对官方说明：https://render.com/docs/free 。免费服务有每月 750 实例小时限制；15 分钟没有访问时休眠，唤醒可能约需一分钟。SQLite、上传文件均属于临时文件系统，在休眠、重启或重新部署时可能清空，因此这次只运行测试数据。免费版不适合作为正式个人密码箱的长期存储。

本次使用已连接的 Render 账号创建免费服务，没有创建收费磁盘、数据库或绑定支付方式。

## 部署配置

源码位于现有 GitHub 项目 `fumiadder/codex_demo` 的 `workbench/` 目录，部署分支为 `deploy/zhixu-free-test`。`render.yaml` 声明服务根目录、免费规格、区域、健康检查、测试模式和容量限制。测试版保留原有账号和空间授权；公开访问首页不会公开个人记录或密码箱。

在 Render 创建 Web Service 时选择：

- 仓库：`https://github.com/fumiadder/codex_demo`
- 分支：`deploy/zhixu-free-test`
- Root Directory：`workbench`
- Language：Python 3；Build：`python -m py_compile server.py`；Start：`python server.py`
- Region：Singapore；Instance Type：Free
- Health Check：`/api/health`
- 环境变量按 `render.yaml`；关闭自动部署

Render 负责公网 HTTPS，后端通过可信代理模式发出 Secure/HttpOnly/SameSite Cookie。免费测试提示由服务端 `/api/config` 驱动，登录前与登录后均展示；正式部署不启用 `WORKSPACE_TEST_MODE`。

本次原生部署工具不支持 `rootDir` 和自定义健康检查路径，实际使用 `cd workbench && python -m py_compile server.py` 构建、`cd workbench && python server.py` 启动，平台采用默认首页探测。服务正确读取 Render 注入的端口 `10000`。Blueprint 配置保留 `/api/health`，便于以后从控制台按该文件重新创建。

## 已发布实例

- 测试站：https://zhixu-free-test.onrender.com
- 控制台：https://dashboard.render.com/web/srv-db32fonavr4c739idbg0
- 服务 ID：`srv-db32fonavr4c739idbg0`；部署 ID：`dep-db32fpnavr4c739idef0`
- 应用提交：`3d4bb527f95407b6591c19e449e929dcc06f3307`
- 2026-10-07 11:07:58 UTC 部署终态为 `live`；免费实例、新加坡区域、自动部署关闭、服务未暂停，错误日志为空。
- 2026-10-07 11:19:18 UTC，标准 GitHub Actions Linux runner 完成 12 组真实公网检查，全部通过。检查记录：https://github.com/fumiadder/codex_demo/actions/runs/37613219063 。HTTPS 证书由 Python 默认信任链验证，首页、静态资源、健康接口与公开配置均返回预期结果。

非敏感部署状态另存于 `render-deployment.json`。自动部署关闭，后续文档和检查脚本提交不会自行重启测试站。

## 发布后验收

已在线验证健康接口、免费测试配置、安全响应头、Secure/HttpOnly/SameSite Cookie、双账号独立空间、工作/生活分区、共享空间 viewer/editor 权限、即时撤权、图片上传和字节范围、按用户隔离的密码箱密文接口、CSRF 拒绝及退出登录。浏览器加密、音频录制与手机布局已有本地浏览器验证；实际设备麦克风、转写服务和国内网络连通性仍需在真实设备上检查。新加坡区域本身不保证所有国内运营商均可访问。

当前执行环境的出站代理禁止直接访问该 Render 子域名，网页读取工具也无法读取它。线上验收采用公开 GitHub 仓库的标准免费 Linux runner，检查脚本为 `scripts/public_deploy_qa.py`，只生成随机测试账号和虚构记录。CI 不配置周期任务，也不写入密钥或输出测试账号口令。具体检查结果见 `VERIFICATION.md`。

验收已删除创建的普通条目和上传文件并退出测试账号。当前 API 没有删除账号、空间和所有者密码箱的接口，临时 `TEST_` 账号、空空间与随机密文样例仍留在临时数据库中，免费实例重置后会清空。这些测试口令已丢弃，不作为公共体验账号；请注册自己的测试账号。
