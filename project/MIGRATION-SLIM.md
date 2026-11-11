# 课程发布项目精简迁移包

## 包内容
- `.dsh/skills/course-publish`：完整可执行代码、SKILL.md、references
- `config/courses.yaml`
- `state/`：52章计划、merge/progress、staging、publish-state、审核状态、配额账本、图床映射、内容审计
- `output/**/*.md`：已合并 Markdown 和 preview 文本（**不含 assets 图片**）
- `package.json`、`package-lock.json`
- `MIGRATION-SLIM.md`

## 不含
- `output/**/assets/`：图片资源，避免 1.3GB
- `downloads/`：原始课件，5.6GB
- `browser-profile/`：登录 Cookie
- `node_modules/`

## 新环境恢复
1) 解压到新目录，执行路径重写（不同绝对路径时必须）：
```bash
NEW_ROOT="$(pwd)" python3 - <<'PY'
import os, pathlib
old = "/Users/fumi/Documents/DeepSeek_harness"
new = os.getcwd()
for p in pathlib.Path("state").glob("*.json"):
    t = p.read_text()
    if old in t:
        p.write_text(t.replace(old, new))
PY
```
2) 安装依赖：`npm ci`；需要时 `npx playwright install chromium`。
3) 从旧终端安全复制 `secrets/cnblogs.env` 到 `secrets/` 并 `chmod 600`。
4) 重新登录百度/博客园：
```bash
node .dsh/skills/course-publish/scripts/baidu-login.mjs
node .dsh/skills/course-publish/scripts/cnblogs-login.mjs
```
5) 启动工作台：`node .dsh/skills/course-publish/scripts/workbench.mjs`，打开 `http://127.0.0.1:5717`。

## 当前状态（2026-11-11）
- 七期：46/46 已发布，合集 45423，37通过/9重审；17篇陪跑PPT已图片化。
- 八期：合集 45406，39篇，审核状态看 `state/ai-agent-8.review-state.json`。
- 六期：52章计划，49已合并、8已发布、24已暂存、17未暂存；阻塞 day44/day45/day48。
- 六期新合集 `45437` 已创建，旧合集 `38391` 未删除；新合集目前不能公开（21个，旧合集删除后才释放名额）。
- 六期图床今天已成功上传215张，随后403；`state/ai-agent-6.media-quota.json` 已 blocked。别把500当作实际可用额度。
- 7期配额文件102只是主课更新批次，未包含陪跑238张；账号实际约523时触发403，历史明细无法完整还原。

## 继续六期的步骤
**重要：本精简包没有 output/assets 和 downloads。六期已staged的24章preview含在线URL，可直接批准发布；17个未暂存/需要新图的章节必须先重新补齐源文件/图片。**

1. 确认配额页 `/quota` 没有 blocked；用 `state/media-uploads.jsonl` 看今天逐张上传。
2. 对已经有在线 preview 的 `staged` 章节：工作台批准 → 
   ```bash
   node .dsh/skills/course-publish/scripts/publish-approved.mjs --course=ai-agent-6 --force-new
   ```
3. 对缺本地图片的章节：从分享重新转存/下载合并（重置该slug的 downloaded/merged 进度，补齐 `output/ai-agent-6/assets`），再 stage。不要把没有本地 assets 的 Markdown 直接发布。
4. 全部50条（实际49条可用）发布后按课程顺序关联合集45437；核验后在后台删除旧合集38391释放20合集名额。
5. day44/day48 分享缺失、day45下载异常，不得伪造；保持 blocked，或补源后再发。
6. 工作台所有状态以 state 文件为准，发布器必须带 `--force-new`，避免误改旧六期同标题文章。

## Agent 开场提示
> 工作区先读 `MIGRATION-SLIM.md`、`.dsh/skills/course-publish/SKILL.md`、`references/lessons-7-8.md`。当前六期新合集45437、旧合集38391未删；六期8已发布、24已暂存、17未暂存、3缺源阻塞。先确认图床配额和 assets 是否齐全，再继续 stage/publish；禁止重复发已有文章，禁止误改旧六期文章。
