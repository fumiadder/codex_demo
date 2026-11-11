// 第八期发布驱动：对已完成合并的章节执行 图床上传→发文→公开页核验→（可选）合集归集。
// 复用既有可靠件：uploadChapterImages(零本地残留守卫) / CnblogsClient(幂等按标题去重) / verifyPost / assignCollection。
// 用法：node edition8-publish.mjs [--only=slug] [--skip-collection] [--dry]
import { promises as fs } from "node:fs";
import path from "node:path";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { getCourse, loadCnblogsSecrets } from "./lib/config.mjs";
import { CnblogsClient } from "./lib/cnblogs.mjs";
import { uploadChapterImages, findLocalImageRefs, MediaApiError } from "./lib/images-cnblogs.mjs";
import { QuotaTracker, MediaQuotaError } from "./lib/media-quota.mjs";
import { markdownToHtml } from "./lib/markdown-html.mjs";
import { verifyPost } from "./run.mjs";
import { assignCollection } from "./lib/cnblogs-ui.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const COURSE = "ai-agent-8";
const course = getCourse(COURSE); course.category = "[随笔分类]AI/AI agent"; // 复用博客园已有随笔分类（getCategories 返回带前缀的完整title）
const plan = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".chapter-plan.json"), "utf8"));
const progress = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".edition8-progress.json"), "utf8"));
const pubFile = path.join(paths.stateDir, COURSE + ".publish-state.json");
const pub = await fs.readFile(pubFile, "utf8").then((s) => JSON.parse(s)).catch(() => ({}));
const savePub = () => fs.writeFile(pubFile, JSON.stringify(pub, null, 2));
const outRoot = paths.courseOutput(COURSE);
const only = args.only || null;
const dry = "dry" in args;
const skipCollection = "skip-collection" in args;
const zeroImageOnly = "zero-image" in args;
const gapMs = Number(args.gap || 45000); // 新发文最小间隔，规避博客园高频发文审核/频控；更新旧文间隔较短

const targets = [];
for (const c of plan.chapters) {
  if (!(progress[c.slug]?.merged && !progress[c.slug]?.blocked && (!only || c.slug === only))) continue;
  if (zeroImageOnly) {
    const txt = await fs.readFile(path.join(outRoot, "00-" + c.slug + ".md"), "utf8");
    if (new Set(findLocalImageRefs(txt)).size > 0) continue;
  }
  targets.push(c);
}
log.info("待发布章节", targets.length, dry ? "(dry-run)" : zeroImageOnly ? "(仅零图片)" : "");

let client = null;
if (!dry) {
  const secrets = loadCnblogsSecrets();
  client = new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
  await client.verify();
  const cats = await client.getCategories();
  if (!cats.includes(course.category)) throw new Error("博客园缺少分类「" + course.category + "」，现有：" + cats.slice(0, 30).join(" | "));
  log.ok("凭证有效，分类已确认");
}

// 幂等：先拉最近文章，同标题则更新而非重复发文
const recent = client ? await client.getRecentPosts(100).catch(() => []) : [];
const findExisting = (title) => { const hit = recent.find((p) => String(p.title || "").trim() === title); return hit ? String(hit.postid) : null; };

const results = [];
// 图片每日配额守卫（免费 500 张/天），跨天自动重置；到限即停，剩余章节保持干净可续传
const quotaFile = path.join(paths.stateDir, COURSE + ".media-quota.json");
const quota = dry ? null : await new QuotaTracker(quotaFile).load();
let quotaStopped = false;
for (const c of targets) {
  if (quotaStopped) { results.push({ slug: c.slug, ok: false, reason: "图片配额暂停，待次日续传" }); continue; }
  const slug = c.slug;
  const title = c.titleOverride || c.webTitle;
  const mdPath = path.join(outRoot, "00-" + slug + ".md");
  try {
    if (dry) {
      const md = await fs.readFile(mdPath, "utf8");
      results.push({ slug, title, bytes: md.length, postId: pub[slug]?.postId || null });
      continue;
    }
    if (pub[slug]?.published && !only) { log.info("跳过已发布", slug); continue; }
    log.step("处理", slug, title);
    // 1) 图片上云（若 md 内已无本地引用，uploadChapterImages 应快速无操作返回）；图床限流(403/MediaApiError)时退避重试
    let img;
    for (let attempt = 1; attempt <= 4; attempt++) {
      try { img = await uploadChapterImages({ client, mdPath, quota }); break; }
      catch (e) {
        if (e instanceof MediaQuotaError) { quotaStopped = true; throw e; }
        if (e instanceof MediaApiError && attempt < 4) { const wait = 90000 * attempt; log.warn("图床限流，" + wait / 1000 + "s 后重试(" + attempt + "/3)：", slug); await new Promise((r) => setTimeout(r, wait)); continue; }
        throw e;
      }
    }
    if (img.failed.length) throw new Error("图片未全部上云: " + img.failed.map((f) => f.rel).join(","));
    const images = (img.uploaded || []).map((u) => u.url);
    // 2) 发文或更新
    const description = markdownToHtml(await fs.readFile(mdPath, "utf8"));
    let postId = pub[slug]?.postId || findExisting(title);
    let isNew = false;
    if (postId) {
      await client.editPost(postId, { title, description, categories: [course.category] }, true);
      log.ok("更新博文", slug, postId);
    } else {
      postId = await client.newPost({ title, description, categories: [course.category] }, course.publish?.autoPublish !== false);
      isNew = true;
      log.ok("新发文", slug, postId);
    }
    const postUrl = "https://www.cnblogs.com/" + client.blogApp + "/p/" + postId + ".html";
    // 3) 公开页核验
    const v = await verifyPost(postUrl, images).catch((e) => ({ httpOk: false, titleOk: false, badImages: [], error: String(e) }));
    pub[slug] = { postId: String(postId), postUrl, published: true, images: images.length, verify: v, at: new Date().toISOString(), collection: pub[slug]?.collection || null, isNew, reviewPending: v.httpOk === false && v.titleOk === true };
    await savePub();
    if (!v.httpOk || v.badImages.length) log.warn("公开页暂不可访问（账号发文审核机制，通过后自动公开，勿重发）", slug, JSON.stringify(v));
    else log.ok("公开页核验通过", slug);
    // 发文节奏：新文按 gap 间隔，更新旧文只短暂停顿
    await new Promise((r) => setTimeout(r, isNew ? gapMs : 3000));
    results.push({ slug, postId: String(postId), ok: true, badImages: v.badImages.length });
  } catch (e) {
    if (e instanceof MediaQuotaError) {
      pub[slug] = { ...(pub[slug] || {}), quotaPending: true, error: String(e.message || e) };
      await savePub();
      results.push({ slug, ok: false, quota: true, reason: "图片每日配额用尽，次日自动续传" });
      log.warn("图片配额暂停（不重发、不改写md）：", slug);
      continue;
    }
    pub[slug] = { ...(pub[slug] || {}), error: String(e.message || e) };
    await savePub();
    results.push({ slug, ok: false, reason: String(e.message || e) });
    log.error("发布失败已挂起", slug, e.message);
  }
}

// 4) 合集归集（浏览器，页面结构脆弱，独立阶段，失败不阻塞已发布结果）
if (!dry && !skipCollection && course.collection) {
  const need = targets.filter((c) => pub[c.slug]?.published && pub[c.slug]?.collection !== course.collection);
  if (need.length) {
    log.info("开始合集归集", need.length, "篇 ->", course.collection);
    const ctx = await getContext({ headless: true });
    const page = ctx.pages()[0] || (await ctx.newPage());
    for (const c of need) {
      try {
        const r = await assignCollection({ page, postUrl: pub[c.slug].postUrl, collectionName: course.collection, courseId: COURSE });
        pub[c.slug].collection = course.collection;
        pub[c.slug].collectionVerified = !!(r && r.verified);
        await savePub();
        log.ok("合集已归集", c.slug);
      } catch (e) {
        pub[c.slug].collectionError = String(e.message || e);
        await savePub();
        log.error("合集失败（博文已发布）", c.slug, e.message);
      }
    }
    await closeBrowser();
  }
}

const okN = results.filter((r) => r.ok).length;
const fail = results.filter((r) => r.ok === false);
console.log("DONE ok=" + okN + " fail=" + fail.length + (dry ? " (dry)" : ""));
for (const f of fail) console.log("FAIL", f.slug, f.reason);
if (dry) for (const r of results) console.log("DRY", r.slug, "|", r.title, "|", r.bytes + "B");
