# 博客园 MetaWeblog 接口备忘

Endpoint: `https://rpc.cnblogs.com/metaweblog/<blogApp>`（XML-RPC over HTTP POST）

| 方法 | 参数 | 用途 |
|---|---|---|
| blogger.getUsersBlogs | "", user, pass | 验证凭证、取 blogid |
| metaWeblog.getCategories | blogid, user, pass | 分类列表（取 title） |
| metaWeblog.newMediaObject | blogid, user, pass, {name,type,bits(base64)} | 传图，返回 {url} |
| metaWeblog.newPost | blogid, user, pass, {title,description,categories[]}, publish(bool) | 发文/草稿，返回 postid |
| metaWeblog.editPost | postid, user, pass, struct, publish | 更新，幂等重发用它 |
| metaWeblog.getPost | postid, user, pass | 读回文章 |
| metaWeblog.getRecentPosts | blogid, user, pass, count | 最近文章，发布幂等按标题去重用 |

注意：
- 合集（文集）不在 MetaWeblog 协议内，只能 i.cnblogs.com 网页端/后台同源 API 操作（见 lib/cnblogs-ui.mjs 与 references/review-and-republish.md 的合集重建章节）。
- 合集后台 API（需要 i.cnblogs 登录态）：`GET /api/collections`、`GET /api/collections/<id>`、`GET /api/collections/<id>/entries?pageIndex=1&pageSize=200`、`PUT /api/collections/<id>/entries`（body `{postIds:[...]}`）、`PUT /api/collections/<id>/entries/<entryId>/serialNum`（body `{serialNum:0}`，0 基）、`DELETE /api/collections/<id>`。写操作需带页面的 X-XSRF-TOKEN，否则 400「会话校验失败」。
- 文章 URL 规则：`https://www.cnblogs.com/<blogApp>/p/<postid>.html`。
- XML 实体与 CDATA 由 lib/xmlrpc.mjs 统一处理；fault 响应抛错带 faultCode/faultString。
- 发布幂等：stagePublish 先拉 getRecentPosts(100)，manifest 无 postId 但存在同标题旧文时改走 editPost，崩溃重跑不产生重复博文（纯函数 resolveExistingPostId 有单测）。
