# 知序 · 个人工作台

应用源码位于 `workbench/`，包含独立账号、空间协作、工作／生活分区、私密密码箱、录音、媒体展示及“晚安故事”模块。晚安故事的文本搜索与音色合成独立，支持设备朗读、收藏、历史和助眠工具；真实克隆及联网检索需配置服务端接口。服务器保存用户数据，仓库只包含代码和虚构测试资料。

正式部署采用 Linux 服务器、Docker Compose 和 Caddy HTTPS，数据库、上传文件和证书使用持久化卷。配置与首次上线步骤见 `workbench/DEPLOY-PRODUCTION.md`。后续 `main` 提交经过检查，由 GitHub Actions 发布到已配置的同一正式服务器；服务器未配置时会明确跳过部署。

免费测试站：https://zhixu-free-test.onrender.com 。它会休眠且可能清空数据，仅用于体验；正式环境与测试站独立。验证记录见 `workbench/VERIFICATION.md`。
