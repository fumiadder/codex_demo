# 知序工作台开发约定

这是面向国内服务器部署的个人工作台，入口是 `server.py` 与 `public/index.html`。不依赖外部字体、CDN 或第三方登录。对已部署版本的修改应保留用户数据目录和持久化卷。

用户要求以后按需把功能加入同一工作台。优先增加明确的模块入口与独立 API，不另建孤立应用。新 API 复用服务端账号、空间成员权限和 CSRF 校验；前端隐藏按钮不能代替授权。共享空间的普通工作／生活记录均可被空间成员读取，密码箱始终按当前用户隔离。

`public/app.js` 使用原生浏览器 API；`server.py` 使用 Python 标准库和 SQLite。新依赖应有明确用途，并注意国内运行条件。模型密钥保存在服务端，不能存入页面或源码。

项目级技能已安装：`.agents/skills/find-skills/SKILL.md` 用于后续能力发现，`.agents/skills/frontend-design/SKILL.md` 用于界面设计；先读取再应用。技能来源记录在各目录的 `provenance.txt`。

安全及权限修改运行 `python -m unittest discover -s tests -v`。浏览器测试脚本及截图记录在验证文档中。部署配置为 Docker Compose + Caddy；正式国内访问要在真实部署后验证，不能以本地测试替代。
