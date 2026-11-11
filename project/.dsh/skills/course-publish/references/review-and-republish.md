# 发布后公开可见性、审核与整篇重发

## 核心区分

- **本地已发布**：MetaWeblog `newPost/editPost` 返回 postId，后台文章记录 `isPublished=true`。这只代表博客园**接收了文章**。
- **网页已公开**：公开 URL 返回 200，且后台 `reviewStatus=1`。只有这个状态才能对读者说“已上线”。
- `state/<course>.publish-state.json` 中的 `published:true` 不等于公开可见；`verify.httpOk:false` + `reviewPending:true` 表示后台已收文但公开页暂不可见。

## 后台审核状态

| reviewStatus | 含义 | 公开页 |
|---|---|---|
| 0 | 待审核 | 通常 404 |
| 1 | 已通过 | 200 |
| 2 | 重审中 | 通常 404 |

`reviewStatus` 不在 MetaWeblog 协议里，只能通过已登录的 `i.cnblogs.com/api/posts/list` 读取；脚本已完成分页到全部后台文章，不能只取最近 80 篇，否则老文章会被误判为“后台无记录”。

**新文章同样会审核**。博客园判断的是账号、内容和发布节奏，不是文章 ID 新旧。整篇重发只是换了 postId，正文、图片、标题仍相同；如果账号近期集中发文或内容被自动抽检，新文仍可能进入 0/2 审核。审核状态不会公开具体触发原因。

## 只读审核巡检（默认动作）

```bash
# 直接指定合集 ID（推荐用于任意历史期次）
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --collection=39559

# 指定合集名称（脚本会从 i.cnblogs.com 合集列表解析 ID）
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --collection="AI agent七期"

# 指定课程：从 state/<course>.publish-state.json 里取 collectionId
node .dsh/skills/course-publish/scripts/cnblogs-review-audit.mjs --course=ai-agent-8
```

- 行为：读后台合集目录 → 拉取全部后台文章状态 → 并发检查每篇公开 URL；**不修改任何文章**。
- 报告：指定课程时写 `state/<course>.review-audit.json`，否则写 `logs/review-audit-<collectionId>-<timestamp>.json`。
- 退出码：`0` 全部公开；`10` 有待审/重审/隐藏/缺失；`1` 运行错误。
- 解读：
  - 后台有记录 + `reviewStatus=1` + 公开 200 → 公开；
  - `reviewStatus=0/2` 或公开 404 → 审核阻塞，**不是本地发布失败**；
  - 后台无记录 + 公开 404 → 可能已删除或链接/ID 错；
  - 公开 200 但后台无记录 → 历史文章不在最近分页，或后台记录缺失；先确认，不要直接判删除。

## 待审/重审的处理

1. 不重发、不反复编辑、不删文来“修 404”；这些动作会重置审核队列，可能产生重复文章。
2. 等待博客园审核结束；用同一 ID 复查。
3. 只有用户明确授权“重新发布这篇”时才走整篇重发；执行前先备份旧文和本地状态。
4. 若重发后仍进入 0/2，停止继续重发并如实报告；不要用另一篇文章或清空正文绕过审核。

## 用户明确授权后的整篇重发流程

> 这是最后手段，不是自动重试路径。7 期/8 期等已发布合集不要批量执行。

### 1. 备份（必须）

建立 `state/<slug>-republish-backup-<UTC压缩时间>/`，至少保存：

- `publish-state.json`
- `staging.json`
- `<slug>.preview.md`
- 源 `00-<slug>.md`
- 旧文章的 `getPost` 完整返回（标题、正文、分类、时间）
- 旧 `deletion-backup.json`（不要覆盖原文件）

同时记录旧 postId、标题、URL、旧 `reviewStatus`、旧 `httpOk/httpTitle`、合集 ID/位置。

### 2. 校验待发布内容

- preview **零本地 `assets/` 引用**；
- 逐张图片公网 URL 可访问且 `content-type: image/*`；
- 同一文章无重复图片；
- 用 `markdownToHtml(preview)` 与旧后台 `description` 对比。允许 **博客园存储层吞掉 emoji** 这种平台差异，但正文、标题、图片集合必须一致；若有任何正文差异，停止并询问用户。
- 预览在上次批准后不能悄悄变化：与备份 preview 做 hash 相等检查。

### 3. 删除旧文并验证

- 先 `getPost(oldId)` 确认旧文仍在、标题一致；
- `blogger.deletePost` 删除（进回收站，短期可恢复）；
- 轮询 `getPost(oldId)` 应失败；
- `getRecentPosts(100)` 中不应再出现同标题，否则会触发“同标题更新”，无法创建新文。

### 4. 修改本地状态，但不要伪造

- `publish-state` 旧记录标记 `published:false, deleted:true, deleteVerified:true, republishedFromId:<oldId>`；
- `staging` 对应章节必须从 `published` 改回 **`approved`**（publish-approved 只发布 approved 目标）；
- 保留备份与旧 ID 映射；绝不能只改 UI 状态。

### 5. 新建并核验

```bash
node .dsh/skills/course-publish/scripts/publish-approved.mjs \
  --course=<course> --only=<slug> --skip-collection
```

- 得到**新的 postId**；不能复用旧 ID。
- 用 `getPost` 读回新文，核对标题、分类、HTML 图片数/唯一 URL 数、详情折叠、无本地 assets。
- 公开页若仍 404，写 `reviewPending`，**不要继续重发**。

### 6. 合集关联与顺序

- 进入 `https://i.cnblogs.com/collections/detail?collectionId=<id>`。
- 点「关联博文」→ 搜索完整标题 → 勾选新 postId 唯一一行 → 关联；确认合集总数恢复。
- 旧 postId 必须已移出合集/不存在。
- 点「移动至...」调整顺序。**注意：该控件“移动到第 N”实际可能插到第 N+1 位**（向下移动时尤其明显）。例如目标第 22 位，先填 22，若落到第 23，再填 21。每次改完必须重新加载页面，按 postId 核对目标位置和前后相邻文章。
- 最后记录 `collectionPosition`、`collectionCount`、`collectionVerified`，并重新跑公开页审核。

## 七期试用基线（2026-11-09）

只读巡检 `--collection=39559`：

- 合集 29 篇，公开 27 篇，重审中 2 篇；
- `day21-漫剧初始+工具介绍`（ID 22498622）与 `day28-发行`（ID 22498718）为 404 重审；
- 其余 27 篇公开页 200；早期 12 篇也能正确读取后台记录（修复分页上限后）。
- 试用没有修改七期任何文章、合集或顺序。

## 合集重建/替代（博客园公开合集 20 个上限）

- 管理端可以创建超过 20 个合集，但博客园只生成前 20 个公开合集页；第 21 个后台有条目、`postCount` 正常，`/collections/<id>` 仍返回 404。
- 判断方法：对后台全部合集并发请求 `https://www.cnblogs.com/<blogApp>/collections/<collectionId>`；若 20 个 200、最新一个 404，就是名额上限，不是文章或排序问题。
- `GET /api/collections` 里的 `isVisible` 不能作为公开标志：本次旧合集和新合集都为 false，但前 20 个公开页均 200。
- 要公开第 21 个合集，必须在旧合集和新合集内容一致、顺序核验通过后，**删除/退役一个旧合集释放名额**；删除合集只删分组，不删除文章。删除前必须备份旧合集的元数据和全部 entry（postId、标题、顺序）到 `state/<course>.collection-migration.json`。
- 本次七期替代：备份旧「AI agent七期」→ 新「AI智能体七期」29 篇自由排序核验通过 → 删除旧合集 → 新合集 public URL 立即从 404 变 200，文章和顺序未受影响。

## 后台合集批量关联与排序（官方后台同源接口）

人工 UI 仍是首选；需要批量迁移 20+ 篇时，可在已登录的 i.cnblogs 页面上下文中调用同一官方接口：

1. 新建合集时选「自由排序」，新博文默认插入「末尾」。
2. 读取条目：
   `GET /api/collections/<id>/entries?pageIndex=1&pageSize=200`
   返回每条 entry 的内部 `id`、`postId`、当前 `serialNum`。
3. 批量加入会员：
   `PUT /api/collections/<id>/entries`，body `{"postIds":[19982513,...]}`。
   必须带当前页自动生成的 `X-XSRF-TOKEN`（从 `XSRF-TOKEN` cookie 解码即可）；缺该头返回 400「会话校验失败」。
4. 批量加入**不会**自动按 postIds 数组写自定义顺序；刚关联的列表可能仍按发布时间展示。必须再做排序并刷新核验。
5. 单条排序：
   `PUT /api/collections/<id>/entries/<entryId>/serialNum`，body `{"serialNum":0}`。
   `serialNum` 是 **0 基** 目标位置；一次写入后可能影响其他条目，批量循环后仍可能只剩首尾少数条对调，必须整页重新加载按 postId 核验并单独修正。
6. 公开页核验要限定主列表，不能用全页链接总数（会混入“最新随笔/推荐”侧栏）：
   `#mainContent .entrylist .entrylistItemTitle` 的标题与 `/p/<postId>` 链接才属于合集正文列表。
7. 公开页即使在重审中的文章标题仍可能出现在合集列表，但文章 URL 可能 404；是否公开仍以文章级 `reviewStatus=1 + HTTP 200` 为准，合集条目数与文章公开数要分别报告。
