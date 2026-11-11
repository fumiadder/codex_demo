// 第八期专用下载+合并驱动：读取显式章节计划，兼容"文件夹单元"与"压缩包文件单元"，断点续跑
import { promises as fs } from "node:fs";
import path from "node:path";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { BaiduPan } from "./lib/baidu.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { classify, resolveInside, extractArchive, downloadPcsByPath, GatedDownloadError } from "./lib/baidu-download.mjs";
import { buildChapterMarkdown, PRUNE_DIRS } from "./lib/docs-md.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const COURSE = args.course || "ai-agent-8";
const plan = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".chapter-plan.json"), "utf8"));
const progressFile = path.join(paths.stateDir, COURSE + ".edition8-progress.json");
const progress = await fs.readFile(progressFile, "utf8").then((s) => JSON.parse(s)).catch(() => ({}));
const saveProgress = () => fs.writeFile(progressFile, JSON.stringify(progress, null, 2));
const dlRoot = paths.courseDownloads(COURSE);
const outRoot = paths.courseOutput(COURSE);
await fs.mkdir(dlRoot, { recursive: true });
await fs.mkdir(outRoot, { recursive: true });

async function remoteStat(pan, fullPath) {
  const parent = path.posix.dirname(fullPath);
  const base = path.posix.basename(fullPath);
  const list = await pan.ownList(parent);
  const hit = list.find((f) => f.name === base);
  if (!hit) throw new Error("远端不存在: " + fullPath);
  return hit;
}

// 剪枝版远端递归：跳过 node_modules 等依赖目录，限制深度，单调用 20s 超时，避免软件项目依赖树导致数千次调用
async function prunedWalkRemote(pan, dir, prefix, depth, out) {
  if (depth > 10) return;
  const entries = await Promise.race([
    pan.ownList(dir),
    new Promise((_, rej) => setTimeout(() => rej(new Error("列目录超时: " + dir)), 20000)),
  ]);
  for (const e of entries) {
    const rel = prefix ? prefix + "/" + e.name : e.name;
    if (e.isdir) {
      if (PRUNE_DIRS.has(e.name)) { log.info("剪枝跳过依赖目录", rel); continue; }
      await prunedWalkRemote(pan, dir + "/" + e.name, rel, depth + 1, out);
    } else {
      out.push({ name: e.name, rel, fsid: e.fsid, size: e.size, parentDir: dir });
    }
  }
}

async function handleUnit(ctx, page, pan, targetDir, unit, chapterDir) {
  const full = targetDir.replace(/\/$/, "") + "/" + unit;
  const st = await remoteStat(pan, full);
  const warnings = [];
  const gated = [];
  let fileCount = 0;
  if (st.isdir) {
    const remote = [];
    await prunedWalkRemote(pan, full, "", 0, remote);
    log.info("文件夹单元", unit, "远端有效文件", remote.length);
    for (const f of remote) {
      const kind = classify(path.extname(f.name));
      if (kind === "media") continue;
      const dest = resolveInside(chapterDir, f.rel);
      try {
        if (kind === "archive") {
          const pcsPath = f.parentDir + "/" + f.name;
          await downloadPcsByPath({ context: ctx, panPath: pcsPath, dest, displayName: f.name });
          const exDir = path.join(chapterDir, "_extracted", path.basename(f.rel).replace(/\.[^.]+$/, ""));
          await extractArchive(dest, exDir)
            .then(() => fs.rm(dest, { force: true }).catch(() => {}))
            .catch((e) => warnings.push("解压失败 " + f.rel + ": " + e.message));
        } else {
          await fs.mkdir(path.dirname(dest), { recursive: true });
          if (await fs.access(dest).then(() => true).catch(() => false)) { fileCount++; continue; }
          await downloadPcsByPath({ context: ctx, panPath: f.parentDir + "/" + f.name, dest, displayName: f.name });
        }
      } catch (e) {
        if (e instanceof GatedDownloadError) { gated.push({ rel: f.rel, mb: +(f.size / 1048576).toFixed(1) }); log.warn("受限跳过", f.rel); continue; }
        throw e;
      }
      fileCount++;
      await new Promise((r) => setTimeout(r, 400 + Math.random() * 600));
    }
  } else {
    const kind = classify(path.extname(st.name));
    if (kind === "media") return { fileCount: 0, warnings: ["跳过媒体 " + st.name], gated };
    const staging = resolveInside(chapterDir, "_staging", path.basename(st.name));
    await fs.mkdir(path.dirname(staging), { recursive: true });
    try {
      await downloadPcsByPath({ context: ctx, panPath: full, dest: staging, displayName: st.name });
    } catch (e) {
      if (e instanceof GatedDownloadError) return { fileCount: 0, warnings, gated: [{ rel: st.name, mb: +(st.size / 1048576).toFixed(1) }] };
      throw e;
    }
    if (kind === "archive") {
      const exDir = resolveInside(chapterDir, "_extracted", path.basename(st.name).replace(/\.[^.]+$/, ""));
      await extractArchive(staging, exDir);
    }
    fileCount++;
  }
  return { fileCount, warnings, gated };
}

const ctx = await getContext({ headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());
const pan = new BaiduPan(page);
await pan.ensureLogin();
// PCS 下载依赖 stoken/ett cookie：先进入网盘主页触发 plantcookie
await page.goto("https://pan.baidu.com/disk/main", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
await new Promise((r) => setTimeout(r, 5000));
const targetDir = plan.targetDir; // 计划单元路径已含根目录 AI智能体八期/...，不再追加
const only = args.only || null;
const doMerge = args.phase !== "download";
const chapters = plan.chapters.filter((c) => !only || c.slug === only);
const blocked = [];
for (const c of chapters) {
  try {
    // 计划中无单元（分享缺失）的章节：明确挂起，不尝试空下载/空合并
    if (!c.units || c.units.length === 0) {
      const reason = c.blockReason || "分享缺少该 day 单元";
      progress[c.slug] = { downloaded: false, files: 0, blocked: reason, missingInShare: true, at: new Date().toISOString() };
      await saveProgress();
      blocked.push({ slug: c.slug, reason });
      log.error("章节缺失已挂起", c.slug, reason);
      continue;
    }
    const chapterDir = resolveInside(dlRoot, c.slug);
    await fs.mkdir(chapterDir, { recursive: true });
    if (progress[c.slug]?.downloaded) { log.info("跳过已下载", c.slug); }
    else {
      let total = 0; const warns = []; const gatedAll = [];
      for (const unit of c.units) {
        log.step("下载", c.slug, unit);
        const r = await handleUnit(ctx, page, pan, targetDir, unit, chapterDir);
        total += r.fileCount; warns.push(...r.warnings); gatedAll.push(...(r.gated || []));
      }
      if (total === 0 && gatedAll.length) {
        const reason = "大文件授权限制且章内无其他可用内容: " + gatedAll.map((g) => g.rel + "(" + g.mb + "MB)").join("; ");
        progress[c.slug] = { downloaded: false, files: 0, gated: gatedAll, blocked: reason, at: new Date().toISOString() };
        await saveProgress();
        blocked.push({ slug: c.slug, reason });
        log.error("章节挂起", c.slug, reason);
        continue;
      }
      progress[c.slug] = { downloaded: true, files: total, warnings: warns, gated: gatedAll, at: new Date().toISOString() };
      await saveProgress();
    }
    if (doMerge) {
      const md = await buildChapterMarkdown({ chapterDir, outDir: outRoot, chapterTitle: c.webTitle, frontmatter: { webIndex: c.webIndex, slug: c.slug, track: c.track, day: c.day, source: c.units.join(" | "), gated: (progress[c.slug].gated || []).map((g) => g.rel) }, includeDocs: c.includeDocs || null });
      progress[c.slug] = { ...progress[c.slug], merged: true, mdPath: md.mdPath, report: md.report };
      await saveProgress();
      log.ok("已合并", c.slug, "->", path.basename(md.mdPath), progress[c.slug].gated?.length ? ("(含受限跳过 " + progress[c.slug].gated.length + " 项)") : "");
    }
  } catch (e) {
    blocked.push({ slug: c.slug, reason: String(e.message || e) });
    progress[c.slug] = { ...(progress[c.slug] || {}), blocked: String(e.message || e) };
    await saveProgress();
    log.error("章节失败已挂起", c.slug, e.message);
  }
}
await closeBrowser();
console.log("DONE chapters=" + chapters.length + " blocked=" + blocked.length);
if (blocked.length) for (const b of blocked) console.log("BLOCKED", b.slug, b.reason);
