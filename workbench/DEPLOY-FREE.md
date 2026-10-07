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
- 服务日志已记录首页、样式、脚本、公开配置与用户状态请求到达；应用日志不包含响应状态码，不能仅凭这些日志断言公网功能检查通过。

非敏感部署状态另存于 `render-deployment.json`。自动部署关闭，后续文档和检查脚本提交不会自行重启测试站。

## 发布后验收

确认返回实际 `https://….onrender.com` 地址且部署状态成功后，检查 `/api/health`、安全响应头、登录 Cookie，完整走一遍双账号空间隔离、共享/撤权、密码箱密文、图片/音频上传与录音保存流程。国内可达性需要在真实国内网络上进一步实测；新加坡区域本身不保证所有国内运营商均可访问。

当前执行环境的出站代理禁止直接访问该 Render 子域名，网页读取工具也无法读取它。线上验收采用公开 GitHub 仓库的标准免费 Linux runner，检查脚本为 `scripts/public_deploy_qa.py`，只生成随机测试账号和虚构记录。CI 不配置周期任务，也不写入密钥或输出测试账号口令。具体检查结果见 `VERIFICATION.md`。
