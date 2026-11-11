// 批准后发布 CLI：只发布人工已批准（staging.status=approved）的章节，绝不发布未审核内容。
// 数据源是 preview MD（图片已是在线链接），发布前强制校验零本地 assets/ 引用，再 markdownToHtml。
// 用法：node publish-approved.mjs --course=ai-agent-8 [--only=slug] [--gap=45000] [--dry] [--skip-collection] [--force-new]
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { getCourse, loadCnblogsSecrets } from "./lib/config.mjs";
import { CnblogsClient } from "./lib/cnblogs.mjs";
import { markdownToHtml } from "./lib/markdown-html.mjs";
import { findLocalImageRefs } from "./lib/images-cnblogs.mjs";
import { loadStaging, saveStaging, STAGE, previewPath } from "./lib/staging.mjs";
import { verifyPost } from "./run.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const COURSE = args.course || "ai-agent-8";
const dry = Boolean(args.dry);
const gapMs = Number(args.gap || 45000);
const forceNew = Boolean(args["force-new"]);
const course = getCourse(COURSE);
// 博客园 getCategories 返回带前缀的完整分类名；把配置的逻辑分类映射到已有随笔分类，沿用历史发布口径
if (course.category === "AI/agent") course.category = "[随笔分类]AI/AI agent";
const pubFile = path.join(paths.stateDir, COURSE + ".publish-state.json");
const pub = await fs.readFile(pubFile, "utf8").then(JSON.parse).catch(() => ({}));
const savePub = () => fs.writeFile(pubFile, JSON.stringify(pub, null, 2));
const staging = await loadStaging(COURSE);

// 硬闸门：仅 approved
const targets = Object.values(staging.chapters).filter((e) => e.status === STAGE.APPROVED && (!args.only || e.slug === args.only));
if (!targets.length) { log.info("没有已批准(approved)待发布的章节"); process.exit(0); }
log.info((dry ? "DRY-RUN " : "") + "待发布（已批准）", targets.length, "篇");

// 发布前强制：preview 必须零本地图片引用
for (const e of targets) {
  const pv = await fs.readFile(e.previewPath || previewPath(COURSE, e.slug), "utf8");
  const residual = findLocalImageRefs(pv);
  if (residual.length) throw new Error("章节 " + e.slug + " 的 preview 仍含本地图片 " + residual.slice(0, 5).join(",") + "，禁止发布（先重新暂存上云）");
}

if (dry) { for (const e of targets) console.log("DRY", e.slug, "|", e.title); process.exit(0); }

const secrets = loadCnblogsSecrets();
const client = new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
await client.verify();
const cats = await client.getCategories();
if (!cats.includes(course.category)) throw new Error("博客园缺少分类「" + course.category + "」");
const recent = await client.getRecentPosts(100).catch(() => []);
const findExisting = (title) => { const hit = recent.find((p) => String(p.title || "").trim() === title); return hit ? String(hit.postid) : null; };

let ok = 0; const fail = [];
for (const [idx, e] of targets.entries()) {
  const slug = e.slug, title = e.title;
  try {
    const pvPath = e.previewPath || previewPath(COURSE, slug);
    const previewMd = await fs.readFile(pvPath, "utf8");
    const description = markdownToHtml(previewMd);
    const onlineImgs = [...new Set([...previewMd.matchAll(/\((https?:\/\/[^)]+)\)/g)].map((m) => m[1]).filter((u) => /cnblogs|img/.test(u)))];
    // force-new：明确要创建新文章时不按同标题旧文去重；已有 publish-state 的 slug 仍走更新，崩溃重跑不会重复。
    let postId = pub[slug] && !pub[slug].deleted ? pub[slug].postId : (forceNew ? null : findExisting(title));
    let isNew = false;
    if (postId) { await client.editPost(String(postId), { title, description, categories: [course.category] }, true); log.ok("更新博文", slug, postId); }
    else { postId = await client.newPost({ title, description, categories: [course.category] }, course.publish?.autoPublish !== false); isNew = true; log.ok("新发文", slug, postId); }
    const postUrl = "https://www.cnblogs.com/" + client.blogApp + "/p/" + postId + ".html";
    const v = await verifyPost(postUrl, onlineImgs).catch((err) => ({ httpOk: false, titleOk: false, badImages: [], error: String(err) }));
    pub[slug] = { postId: String(postId), postUrl, published: true, deleted: false, images: onlineImgs.length, verify: v, at: new Date().toISOString(), isNew, gated: "approved", reviewPending: v.httpOk === false && v.titleOk === true };
    await savePub();
    staging.chapters[slug].status = STAGE.PUBLISHED;
    staging.chapters[slug].publishedAt = new Date().toISOString();
    staging.chapters[slug].postId = String(postId);
    await saveStaging(COURSE, staging);
    ok++;
    if (isNew && idx < targets.length - 1) await new Promise((r) => setTimeout(r, gapMs));
  } catch (err) {
    fail.push({ slug, reason: String(err.message || err) });
    log.error("发布失败（保持 approved，不自动重试）", slug, err.message);
  }
}
console.log("PUBLISH_DONE ok=" + ok + " fail=" + fail.length);
for (const f of fail) console.log("FAIL", f.slug, f.reason);
