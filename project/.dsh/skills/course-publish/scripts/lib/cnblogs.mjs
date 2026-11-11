// 博客园 MetaWeblog 封装：https://rpc.cnblogs.com/metaweblog/<blogApp>
import { xmlrpcCall } from "./xmlrpc.mjs";
import { log } from "./log.mjs";

export class CnblogsClient {
  constructor({ blogApp, username, password, endpoint = null }) {
    if (!blogApp || !username || !password) throw new Error("博客园凭证不完整");
    this.blogApp = blogApp;
    // endpoint 仅测试时可覆盖；默认走博客园官方 MetaWeblog
    this.endpoint = endpoint || `https://rpc.cnblogs.com/metaweblog/${encodeURIComponent(blogApp)}`;
    this.username = username;
    this.password = password;
    this.blogId = "0"; // MetaWeblog 约定占位，getUsersBlogs 会返回真实 blogid
  }

  call(method, ...params) {
    return xmlrpcCall(this.endpoint, method, params);
  }

  async verify() {
    const blogs = await this.call("blogger.getUsersBlogs", "", this.username, this.password);
    const blog = Array.isArray(blogs) ? blogs[0] : blogs;
    if (blog?.blogid) this.blogId = String(blog.blogid);
    log.ok("博客园凭证有效，blogid =", this.blogId, "blogName =", blog?.blogName);
    return blog;
  }

  async getCategories() {
    const cats = await this.call("metaWeblog.getCategories", this.blogId, this.username, this.password);
    return (Array.isArray(cats) ? cats : []).map((c) => c.title).filter(Boolean);
  }

  // file: { name, type, bits: Buffer } -> { url }
  async newMediaObject(file) {
    const res = await this.call(
      "metaWeblog.newMediaObject",
      this.blogId,
      this.username,
      this.password,
      { name: file.name, type: file.type, bits: file.bits },
    );
    return res?.url || null;
  }

  // post: { title, description, categories: [], mt_keywords? } ; publish=false 存草稿
  async newPost(post, publish) {
    const struct = {
      title: post.title,
      description: post.description,
      categories: post.categories || [],
      ...(post.mt_keywords ? { mt_keywords: post.mt_keywords } : {}),
    };
    const id = await this.call("metaWeblog.newPost", this.blogId, this.username, this.password, struct, publish === true);
    return String(id);
  }

  async editPost(postId, post, publish) {
    const struct = {
      title: post.title,
      description: post.description,
      categories: post.categories || [],
      ...(post.mt_keywords ? { mt_keywords: post.mt_keywords } : {}),
    };
    return this.call("metaWeblog.editPost", String(postId), this.username, this.password, struct, publish === true);
  }

  async getPost(postId) {
    return this.call("metaWeblog.getPost", String(postId), this.username, this.password);
  }

  // 删除文章（移入博客园回收站，可在后台短期恢复）。blogger.deletePost(appkey, postid, user, pwd, publish)
  async deletePost(postId) {
    return this.call("blogger.deletePost", "", String(postId), this.username, this.password, true);
  }

  // 最近文章（发布幂等防护用）
  async getRecentPosts(count = 50) {
    const posts = await this.call("metaWeblog.getRecentPosts", this.blogId, this.username, this.password, Number(count));
    return Array.isArray(posts) ? posts : [];
  }
}

// 纯函数：按标题在最近文章中找已存在的 postid（用于崩溃重跑去重）
export function resolveExistingPostId(recentPosts, title) {
  const hit = (recentPosts || []).find((p) => String(p.title || "").trim() === String(title || "").trim());
  return hit ? String(hit.postid) : null;
}
