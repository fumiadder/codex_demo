# 免费公网部署测试

目标平台：Render Free Web Service，Singapore 区域，原 Python 后端运行，无第三方 Python 包。仅使用平台免费 HTTPS 子域名，不创建付费实例、持久化磁盘或数据库。本配置严格固定 `plan: free`，不自动升级规格。

2026-10-07 核对官方说明：https://render.com/docs/free 。免费服务有每月 750 实例小时限制；15 分钟没有访问时休眠，唤醒可能约需一分钟。SQLite、上传文件均属于临时文件系统，在休眠、重启或重新部署时可能清空，因此这次只运行测试数据。免费版不适合作为正式个人密码箱的长期存储。

Render 账号可能要求额外验证。没有账号授权时不能替用户创建服务；若账户要求信用卡或付费，应停在该步骤，不能以免费测试为由绑定支付方式。

## 部署配置

源码准备到现有 GitHub 项目 `fumiadder/codex_demo` 的 `workbench/` 目录，使用专门的测试部署分支。`render.yaml` 声明服务根目录、免费规格、区域、健康检查、测试模式和容量限制。测试版保留原有账号和空间授权；公开访问首页不会公开个人记录或密码箱。

在 Render 创建 Web Service 时选择：

- 仓库：`https://github.com/fumiadder/codex_demo`
- 分支：源码同步后记录的测试分支
- Root Directory：`workbench`
- Language：Python 3；Build：`python -m py_compile server.py`；Start：`python server.py`
- Region：Singapore；Instance Type：Free
- Health Check：`/api/health`
- 环境变量按 `render.yaml`；关闭自动部署

Render 负责公网 HTTPS，后端通过可信代理模式发出 Secure/HttpOnly/SameSite Cookie。免费测试提示由服务端 `/api/config` 驱动，登录前与登录后均展示；正式部署不启用 `WORKSPACE_TEST_MODE`。

## 发布后验收

确认返回实际 `https://….onrender.com` 地址且部署状态成功后，检查 `/api/health`、安全响应头、登录 Cookie，完整走一遍双账号空间隔离、共享/撤权、密码箱密文、图片/音频上传与录音保存流程。国内可达性需要在真实国内网络上进一步实测；新加坡区域本身不保证所有国内运营商均可访问。

当前状态以此文件与部署状态记录中的真实结果为准；仅准备配置或同步 GitHub 源码不代表网站已经发布。
