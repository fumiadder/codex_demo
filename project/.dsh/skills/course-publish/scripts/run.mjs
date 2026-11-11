// 流水线编排：转存 → 目录 → 匹配(闸门) → 下载 → 合并 → 传图 → 发布(分类+合集)
// 用法：
//   node run.mjs --course <id> [--only transfer,toc,...] [--chapter <webIndex>]
//                [--dry-run] [--headless] [--decisions <file>]
// 退出码：0 成功；10 有闸门待人工确认（见 state/<id>.pending-gates.json）；1 致命错误
import { promises as fs, readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { getCourse, loadCnblogsSecrets } from "./lib/config.mjs";
import { loadManifest, saveManifest, setChapter } from "./lib/manifest.mjs";
import { BaiduPan, GateError } from "./lib/baidu.mjs";
import { downloadChapter } from "./lib/baidu-download.mjs";
import { scrapeToc } from "./lib/toc.mjs";
import { matchChapters, normalize, diffNewChapters } from "./lib/names.mjs";
import { buildPendingGates, loadDecisions, validateDecisions } from "./lib/gates.mjs";
import { buildChapterMarkdown } from "./lib/docs-md.mjs";
import { uploadChapterImages, MediaApiError } from "./lib/images-cnblogs.mjs";
import { markdownToHtml } from "./lib/markdown-html.mjs";
import { CnblogsClient, resolveExistingPostId } from "./lib/cnblogs.mjs";
import { assignCollection } from "./lib/cnblogs-ui.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function args() {
  const a = process.argv.slice(2);
  const out = { "dry-run": false, headless: false };
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith("--")) {
      const k = a[i].slice(2);
      out[k] = a[i + 1] && !a[i + 1].startsWith("--") ? a[++i] : true;
    }
  }
  return out;
}

const ALL_STAGES = ["transfer", "toc", "match", "download", "merge", "images", "publish"];

async function stageTransfer(ctx, page, course, m, dryRun, panOverride = null, discoverOnly = false) {
  const pan = panOverride || new BaiduPan(page);
  if (!panOverride) {
    pan.installCapture();
    await pan.ensureLogin();
  }
  const report = { newChapters: [], errors: [], discoverOnly };
  for (const share of course.baidu.shares) {
    log.step("转存", share.url);
    let shareCtx;
    try {
      shareCtx = await pan.openShare(share);
    } catch (e) {
      if (e instanceof GateError) {
        report.errors.push({ kind: e.kind, message: e.message });
        log.error("闸门：", e.kind, e.message);
        continue;
      }
      throw e;
    }
    const key = share.url;
    const prev = m.shares[key]?.chapters || [];
    const newOnes = diffNewChapters(prev, shareCtx.entries);
    m.shares[key] = {
      lastChecked: new Date().toISOString(),
      shareid: shareCtx.shareid,
      uk: shareCtx.uk,
      chapters: shareCtx.entries.map((e) => ({ fsid: e.fsid, name: e.name, isdir: e.isdir, size: e.size })),
    };
    log.info("顶层", shareCtx.entries.length, "新增", newOnes.length);
    for (const ch of newOnes) {
      const norm = normalize(ch.name);
      report.newChapters.push(ch.name);
      setChapter(m, norm, { shareName: ch.name, shareFsid: ch.fsid, isdir: ch.isdir, status: "discovered" });
      if (discoverOnly) { log.info("[discover] 仅枚举不转存：", ch.name); continue; }
      if (dryRun) { log.warn("[dry-run] 将转存：", ch.name); continue; }
      try {
        await pan.transfer([ch.fsid], course.baidu.targetDir);
        const v = await pan.verifyTransferred(course.baidu.targetDir, [ch.name]);
        if (!v.ok) setChapter(m, norm, { status: "blocked", blockReason: "转存核验缺失:" + v.missing.join(",") });
        else {
          setChapter(m, norm, { status: "transferred", transferredAt: new Date().toISOString() });
          log.ok("已转存：", ch.name);
        }
      } catch (e) {
        setChapter(m, norm, { status: "blocked", blockReason: (e.kind || "") + ": " + e.message });
        report.errors.push({ chapter: ch.name, kind: e.kind, message: e.message });
      }
    }
  }
  return report;
}

function tocPath(courseId) {
  return path.join(paths.stateDir, courseId + ".toc.json");
}

async function stageToc(ctx, page, course, scraper = scrapeToc) {
  const file = tocPath(course.id);
  try {
    const { items, snapshot } = await scraper({ page, course });
    await fs.writeFile(file, JSON.stringify({ items, snapshot, at: new Date().toISOString() }, null, 2));
    return items;
  } catch (e) {
    // 课程站临时不可用/登录失效：有历史快照则回退，保证订阅不中断（新章节匹配仍以网盘为准）
    if (existsSync(file)) {
      const prev = JSON.parse(readFileSync(file, "utf8"));
      log.warn("目录抓取失败，回退到上次快照：", file, "原因：", e.message || e);
      return prev.items;
    }
    throw e;
  }
}

async function stageMatch(course, m, decisionsFile, onlyChapter) {
  const tocFile = tocPath(course.id);
  if (!existsSync(tocFile)) {
    throw new Error("缺少网站目录快照 " + tocFile + "，请先运行 --only toc（需已登录课程站）");
  }
  const toc = JSON.parse(readFileSync(tocFile, "utf8"));
  let web = toc.items;
  if (onlyChapter) web = web.filter((w) => String(w.index) === String(onlyChapter));

  // 网盘章节：来自分享快照中的目录
  const shareSet = new Map();
  for (const s of Object.values(m.shares)) {
    for (const c of s.chapters || []) {
      if (c.isdir !== false) shareSet.set(c.name, { name: c.name });
    }
  }
  const shareChapters = [...shareSet.values()];

  let result = matchChapters(web, shareChapters);

  // 回放人工决策
  const decisions = loadDecisions(course.id, decisionsFile) || [];
  validateDecisions(decisions, shareChapters.map((s) => s.name));
  const decByIndex = new Map(decisions.map((d) => [Number(d.webIndex), d]));
  const skippedWeb = new Set();
  const assigned = new Map(); // shareName -> webIndex，防止一对多冲突
  for (const p of result.pairs) {
    if (p.confidence === "high" && p.shareName) assigned.set(p.shareName, p.webIndex);
  }
  for (const p of result.pairs) {
    const d = decByIndex.get(p.webIndex);
    if (!d) continue;
    if (d.action === "skip") {
      skippedWeb.add(p.webIndex);
      continue;
    }
    if (d.action === "map" && d.shareName) {
      const owner = assigned.get(d.shareName);
      if (owner != null && owner !== p.webIndex) {
        throw new Error("决策冲突：网盘文件夹「" + d.shareName + "」被同时映射到 webIndex " + owner + " 和 " + p.webIndex);
      }
      p.shareName = d.shareName;
      p.confidence = "high";
      p.score = 1;
      p.manual = true;
      assigned.set(d.shareName, p.webIndex);
    }
    if (d.action === "accept" && p.shareName) {
      p.confidence = "high";
      p.manual = true;
    }
  }
  result.pairs = result.pairs.filter((p) => !skippedWeb.has(p.webIndex));

  // 历史人工裁定直接生效
  for (const p of result.pairs) {
    if (p.confidence !== "high") {
      const rec = Object.values(m.chapters).find((c) => c.webIndex === p.webIndex && c.manualOverride);
      if (rec?.manualOverride?.shareName) {
        p.shareName = rec.manualOverride.shareName;
        p.confidence = "high";
        p.manual = true;
      }
    }
  }

  // 写 manifest
  for (const p of result.pairs) {
    if (p.confidence === "high" && p.shareName) {
      const norm = normalize(p.shareName);
      const cur = m.chapters[norm] || {};
      setChapter(m, norm, {
        ...cur,
        shareName: p.shareName,
        webIndex: p.webIndex,
        webTitle: p.webTitle,
        confidence: "high",
        matchedAt: new Date().toISOString(),
        ...(p.manual ? { manualOverride: { shareName: p.shareName, webTitle: p.webTitle, webIndex: p.webIndex } } : {}),
        status: ["discovered", "transferred", "downloaded", "merged", "images-uploaded", "matched", "published"].includes(cur.status)
          ? (cur.status === "discovered" ? "matched" : cur.status)
          : "matched",
      });
    }
  }

  const pending = result.pairs.filter((p) => p.confidence !== "high");
  const gates = buildPendingGates(course.id, result.pairs, shareChapters, result.unresolvedShare);
  // orphan-share 仅提示不阻塞；阻塞只看章节匹配闸门
  return { result, pendingCount: pending.length, orphanCount: gates.gates.filter((g) => g.type === "orphan-share").length, gatesFile: gates.file };
}

async function stageDownload(ctx, page, course, m, onlyChapter, deps = {}) {
  const downloader = deps.downloadChapter || downloadChapter;
  let pan = deps.pan || null;
  // 注入下载器（测试/离线）时跳过真实浏览器初始化；生产路径保持原样
  if (!deps.downloadChapter && !deps.pan) {
    pan = new BaiduPan(page);
    await pan.ensureLogin();
  }
  const chapters = Object.values(m.chapters).filter(
    (c) => c.webTitle && ["matched", "transferred"].includes(c.status) &&
      (!onlyChapter || String(c.webIndex) === String(onlyChapter)),
  );
  const out = [];
  for (const c of chapters) {
    log.step("下载", c.webIndex, c.shareName);
    try {
      const r = await downloader({ context: ctx, page, pan, course, shareName: c.shareName });
      const norm = normalize(c.shareName);
      setChapter(m, norm, {
        status: "downloaded",
        chapterDir: r.chapterDir,
        files: r.files,
        skippedMedia: r.skippedMedia,
        warnings: r.warnings,
      });
      saveManifest(course.id, m);
      out.push({ chapter: c.shareName, files: r.files.length, media: r.skippedMedia.length, warnings: r.warnings });
    } catch (e) {
      setChapter(m, normalize(c.shareName), { status: "blocked", blockReason: "下载失败：" + (e.message || e) });
      saveManifest(course.id, m);
      log.error("章节下载失败已挂起：", c.shareName, e.message);
    }
  }
  return out;
}

async function stageMerge(course, m, onlyChapter, deps = {}) {
  const builder = deps.buildChapterMarkdown || buildChapterMarkdown;
  const out = [];
  const blocked = [];
  for (const c of Object.values(m.chapters)) {
    if (c.status !== "downloaded") continue;
    if (onlyChapter && String(c.webIndex) !== String(onlyChapter)) continue;
    log.step("合并", c.webIndex, c.webTitle);
    try {
      const outDir = paths.courseOutput(course.id);
      const r = await builder({
        chapterDir: c.chapterDir,
        outDir,
        chapterTitle: c.webTitle,
        frontmatter: {
          index: c.webIndex,
          slug: c.shareName,
          course: course.id,
          edition: course.edition || "",
          source: c.shareName,
        },
      });
      setChapter(m, normalize(c.shareName), { status: "merged", mdPath: r.mdPath, mergeReport: r.report });
      saveManifest(course.id, m);
      out.push({ mdPath: r.mdPath, report: r.report });
    } catch (e) {
      // 单章合并失败只挂起该章，其余章节继续（与 download/images 隔离策略一致）
      setChapter(m, normalize(c.shareName), { status: "blocked", blockReason: "合并失败：" + (e.message || e) });
      saveManifest(course.id, m);
      blocked.push({ chapter: c.webTitle || c.shareName, reason: String(e.message || e) });
      log.error("章节合并失败已挂起：", c.shareName, e.message);
    }
  }
  return { out, blocked };
}

async function stageImages(course, m, onlyChapter) {
  const out = [];
  const blocked = [];
  // 没有待传图的 merged 章节时直接跳过，避免无谓要求博客园凭证
  const eligible = Object.values(m.chapters).filter(
    (c) => c.status === "merged" && (!onlyChapter || String(c.webIndex) === String(onlyChapter)),
  );
  if (!eligible.length) {
    log.info("无 merged 章节需要传图，跳过 images 阶段");
    return { out, blocked };
  }
  const secrets = loadCnblogsSecrets();
  const client = new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
  for (const c of Object.values(m.chapters)) {
    if (c.status !== "merged") continue;
    if (onlyChapter && String(c.webIndex) !== String(onlyChapter)) continue;
    log.step("传图", c.webTitle);
    try {
      let r;
      for (let attempt = 1; attempt <= 3; attempt++) {
        try { r = await uploadChapterImages({ client, mdPath: c.mdPath }); break; }
        catch (e) {
          // 图床限流(403/MediaApiError)：退避后重试，避免一次性把章节判死
          if (e instanceof MediaApiError && attempt < 3) {
            const wait = 60000 * attempt;
            log.warn("图床接口受限，" + wait / 1000 + "s 后重试(" + attempt + "/2)：", c.webTitle);
            await sleep(wait);
            continue;
          }
          throw e;
        }
      }
      if (r.failed.length) {
        setChapter(m, normalize(c.shareName), { status: "blocked", blockReason: "图片失败:" + r.failed.map((f) => f.rel).join(",") });
        saveManifest(course.id, m);
        blocked.push({ chapter: c.webTitle, reason: r.failed.map((f) => f.reason).join("; ") });
        log.error("章节图片未全部达标，标记 blocked 并继续后续章节：", c.webTitle);
        continue;
      }
      setChapter(m, normalize(c.shareName), { status: "images-uploaded", images: r.uploaded.map((u) => u.url) });
      saveManifest(course.id, m);
      out.push(r);
    } catch (e) {
      // 含 MediaApiError（图床接口整体不可用）：单章隔离，不拖垮整轮
      setChapter(m, normalize(c.shareName), { status: "blocked", blockReason: "传图异常:" + e.message });
      saveManifest(course.id, m);
      blocked.push({ chapter: c.webTitle, reason: String(e.message || e) });
      log.error("传图异常，章节 blocked：", c.webTitle, e.message);
    }
  }
  return { out, blocked };
}

// 公开页复核：页面可访问 + 有标题 + 每张图公网可取且 content-type 为 image/*
// 单图失败（含超时/网络错误）只计入 badImages，不抛错，交由人工复核
const BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36";
export async function verifyPost(postUrl, imageUrls, { timeoutMs = 30000 } = {}) {
  const r = await fetch(postUrl, {
    signal: AbortSignal.timeout(timeoutMs),
    headers: { "User-Agent": BROWSER_UA, Accept: "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9" },
    redirect: "follow",
  });
  const html = await r.text();
  const titleOk = /<title>[^<]+<\/title>/.test(html);
  const bad = [];
  for (const u of imageUrls || []) {
    try {
      const ir = await fetch(u, { method: "GET", signal: AbortSignal.timeout(timeoutMs), redirect: "follow", headers: { "User-Agent": BROWSER_UA } });
      const ct = ir.headers.get("content-type") || "";
      if (!ir.ok || !ct.startsWith("image/")) bad.push(u);
    } catch (e) {
      log.error("图片公网校验失败：", u, e.name || e.message);
      bad.push(u);
    }
  }
  return { httpOk: r.ok, titleOk, badImages: bad };
}

async function stagePublish(ctx, page, course, m, dryRun, onlyChapter, deps = {}) {
  const wantedCat = course.category || "AI/agent";

  // dry-run：不需要凭证/网络，只为已合并章节生成发布预览
  if (dryRun) {
    const previews = [];
    for (const c of Object.values(m.chapters)) {
      if (!["merged", "images-uploaded", "published"].includes(c.status)) continue;
      if (onlyChapter && String(c.webIndex) !== String(onlyChapter)) continue;
      if (!c.mdPath) continue;
      previews.push({
        webIndex: c.webIndex,
        title: (course.publish?.titlePrefix || "") + c.webTitle,
        category: wantedCat,
        collection: course.collection,
        mdPath: c.mdPath,
      });
    }
    const f = path.join(paths.stateDir, course.id + ".publish-preview.json");
    await fs.writeFile(f, JSON.stringify({ previews, at: new Date().toISOString(), dryRun: true }, null, 2));
    log.warn("[dry-run] 发布预览（未连博客园）：", f, previews.length, "篇");
    return [];
  }

  const client = deps.client || (() => {
    const secrets = loadCnblogsSecrets();
    return new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
  })();
  const verifyFn = deps.verifyPost || verifyPost;
  await client.verify();
  const cats = await client.getCategories();
  if (!cats.includes(wantedCat)) {
    const msg = "博客园缺少分类「" + wantedCat + "」，现有分类：" + cats.slice(0, 30).join(" | ");
    log.error(msg);
    const f = path.join(paths.stateDir, course.id + ".pending-gates.json");
    await fs.writeFile(f, JSON.stringify({ gates: [{ type: "missing-category", wanted: wantedCat, existing: cats }] }, null, 2));
    throw Object.assign(new Error(msg), { exitCode: 10 });
  }

  const previews = [];
  const out = [];
  // 发布幂等：一次性拉最近文章，崩溃重跑时按标题复用 postid，避免重复发文
  const recentPosts = await client.getRecentPosts(100).catch((e) => {
    log.warn("getRecentPosts 失败，跳过标题去重（首次发布不受影响）：", e.message);
    return [];
  });
  for (const c of Object.values(m.chapters)) {
    if (!["images-uploaded", "published"].includes(c.status)) continue;
    if (onlyChapter && String(c.webIndex) !== String(onlyChapter)) continue;
    if (c.status === "published" && !onlyChapter) continue; // 已发布章节默认不重复发布，显式 --chapter 可强制更新
    const title = (course.publish?.titlePrefix || "") + c.webTitle;
    const description = markdownToHtml(await fs.readFile(c.mdPath, "utf8"));
    previews.push({ webIndex: c.webIndex, title, category: wantedCat, collection: course.collection, mdPath: c.mdPath });

    let postId = c.postId;
    let isNew = false;
    if (!postId) {
      const existing = resolveExistingPostId(recentPosts, title);
      if (existing) {
        postId = existing;
        log.warn("发现同标题已存在文章，改为更新而非新建：", title, "postId=" + postId);
      }
    }
    if (!postId) {
      postId = await client.newPost({ title, description, categories: [wantedCat] }, course.publish?.autoPublish !== false);
      isNew = true;
      log.ok("博文已" + (course.publish?.autoPublish !== false ? "发布" : "存草稿") + "：", postId);
    } else {
      await client.editPost(postId, { title, description, categories: [wantedCat] }, true);
      log.ok("博文已更新：", postId);
    }
    const postUrl = "https://www.cnblogs.com/" + client.blogApp + "/p/" + postId + ".html";
    setChapter(m, normalize(c.shareName), { postId: String(postId), postUrl, publishedAt: new Date().toISOString() });
    saveManifest(course.id, m);

    // 合集（网页端）
    if (course.collection && c.collection !== course.collection) {
      try {
        const r = await assignCollection({ page, postUrl, collectionName: course.collection, courseId: course.id });
        setChapter(m, normalize(c.shareName), { collection: course.collection, collectionVerified: r.verified });
      } catch (e) {
        setChapter(m, normalize(c.shareName), { collectionError: String(e.message || e) });
        log.error("合集设置失败（博文已发布，需人工补合集）：", e.message);
      }
    }

    // 公开页复核
    const v = await verifyFn(postUrl, c.images || []).catch((e) => ({ httpOk: false, titleOk: false, badImages: [], error: String(e) }));
    // 账号发文审核机制：页面暂 404 但标题已存在 = 审核中（非失败，勿重发）
    const reviewPending = v.httpOk === false && v.titleOk === true && (v.badImages || []).length === 0;
    setChapter(m, normalize(c.shareName), { status: "published", verify: v, reviewPending });
    saveManifest(course.id, m);
    if (reviewPending) log.warn("公开页审核中（通过后自动公开，勿重发）：", postUrl);
    else if (!v.httpOk || v.badImages.length) log.error("公开页复核异常：", JSON.stringify(v));
    else log.ok("发布并复核成功：", postUrl);
    out.push({ postId, postUrl, verify: v });
    // 发文节奏：新文拉长间隔规避高频审核/频控，更新旧文短停顿（可经 course.publish 覆盖）
    await sleep(isNew ? (course.publish?.newPostGapMs ?? 45000) : (course.publish?.editGapMs ?? 2000));
  }
  return out;
}

const USAGE = [
  "课程网盘订阅 → 文档汇总 → 博客园发布 流水线",
  "",
  "用法：node run.mjs --course <id> [选项]",
  "  --only <stage,...>   只跑指定阶段，顺序：" + ALL_STAGES.join(","),
  "  --chapter <webIndex> 只处理某一章（单章调试/强制重发）",
  "  --decisions <file>   回放人工闸门决策 JSON",
  "  --dry-run            不写网盘、不发博客，只枚举/生成预览",
  "  --headless true      无头浏览器（定时任务用）",
  "",
  "退出码：0 成功；10 有闸门待确认；1 致命错误或存在 blocked 章节",
].join("\n");

async function main() {
  const a = args();
  if (a.help || a.h) {
    console.log(USAGE);
    return;
  }
  if (!a.course) throw new Error("必须提供 --course <id>（--help 查看用法）");
  const stages = a.only ? String(a.only).split(",").map((s) => s.trim()) : ALL_STAGES;
  const badStage = stages.find((s) => !ALL_STAGES.includes(s));
  if (badStage) throw new Error("未知阶段「" + badStage + "」，可选：" + ALL_STAGES.join(","));
  const course = getCourse(a.course);
  const m = loadManifest(course.id);
  const dryRun = a["dry-run"] === true || a["dry-run"] === "true";
  const onlyChapter = a.chapter ? String(a.chapter) : null;
  const needsBrowser = stages.some((s) => ["transfer", "toc", "download", "publish"].includes(s));

  let ctx, page;
  if (needsBrowser) {
    ctx = await getContext({ headless: a.headless === true || a.headless === "true" });
    page = ctx.pages()[0] || (await ctx.newPage());
  }
  let exitCode = 0;
  try {
    if (stages.includes("transfer")) await stageTransfer(ctx, page, course, m, dryRun);
    if (stages.includes("toc")) await stageToc(ctx, page, course);
    if (stages.includes("match")) {
      const r = await stageMatch(course, m, a.decisions, onlyChapter);
      log.info("匹配完成：待确认", r.pendingCount, "闸门文件", r.gatesFile);
      if (r.pendingCount > 0 && !a.decisions) exitCode = 10;
    }
    if (exitCode === 0 && stages.includes("download")) await stageDownload(ctx, page, course, m, onlyChapter);
    if (exitCode === 0 && stages.includes("merge")) await stageMerge(course, m, onlyChapter);
    if (exitCode === 0 && stages.includes("images") && !dryRun) await stageImages(course, m, onlyChapter);
    if (exitCode === 0 && stages.includes("publish")) await stagePublish(ctx, page, course, m, dryRun, onlyChapter);
  } finally {
    saveManifest(course.id, m);
    if (ctx) await closeBrowser();
  }
  if (exitCode === 10) {
    log.error("存在待人工确认的闸门，请查看 state/" + course.id + ".pending-gates.json，确认后用 --decisions 回放");
  }
  // 有章节被 blocked（转存/下载/传图等局部失败）：其余工作照常完成，但以非零码提示人工介入
  const blockedChapters = Object.values(m.chapters || {}).filter((c) => c.status === "blocked");
  if (exitCode === 0 && blockedChapters.length) {
    exitCode = 1;
    log.error("存在 " + blockedChapters.length + " 个 blocked 章节：", blockedChapters.map((c) => c.webTitle || c.shareName).join("、"));
    for (const c of blockedChapters) log.error("  blocked：", c.webTitle || c.shareName, "—", c.blockReason || "");
  }
  process.exitCode = exitCode;
}

// 供集成测试与定时任务复用
export { stageMatch, stageMerge, stageToc, stageTransfer, stageImages, stagePublish, stageDownload, tocPath };

import { pathToFileURL } from "node:url";
import { realpathSync } from "node:fs";
function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(new URL(import.meta.url)) === realpathSync(process.argv[1]);
  } catch {
    return import.meta.url === pathToFileURL(process.argv[1]).href;
  }
}
if (isMainModule()) {
  main().catch((e) => {
    log.error(e.stack || String(e));
    process.exit(e.exitCode || 1);
  });
}
