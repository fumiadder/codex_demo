// 转存编排：枚举分享 → diff 新章节 → 转存到目标目录 → 核验 → 落 manifest
// 用法：node baidu-transfer.mjs --course <id> [--dry-run] [--headless] [--url <只处理某个分享>]
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { getCourse } from "./lib/config.mjs";
import { loadManifest, saveManifest, setChapter } from "./lib/manifest.mjs";
import { BaiduPan, GateError } from "./lib/baidu.mjs";
import { normalize, diffNewChapters } from "./lib/names.mjs";
import { log } from "./lib/log.mjs";

function args() {
  const a = process.argv.slice(2);
  const out = { headless: false, "dry-run": false };
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith("--")) out[a[i].slice(2)] = a[i + 1] && !a[i + 1].startsWith("--") ? a[++i] : true;
  }
  return out;
}

async function main() {
  const a = args();
  const course = getCourse(a.course);
  const dryRun = a["dry-run"] === true || a["dry-run"] === "true";
  const m = loadManifest(course.id);
  const ctx = await getContext({ headless: a.headless === true || a.headless === "true" });
  const page = ctx.pages()[0] || (await ctx.newPage());
  const pan = new BaiduPan(page);
  pan.installCapture();
  const report = { newChapters: [], transferred: [], skipped: [], errors: [] };

  try {
    await pan.ensureLogin();
    for (const share of course.baidu.shares) {
      if (a.url && share.url !== a.url) continue;
      log.step("转存", share.url);
      let shareCtx;
      try {
        shareCtx = await pan.openShare(share);
      } catch (e) {
        if (e instanceof GateError) {
          report.errors.push({ stage: "openShare", kind: e.kind, message: e.message });
          log.error("闸门拦截：", e.kind, e.message);
          continue;
        }
        throw e;
      }

      const key = share.url;
      const prevChapters = m.shares[key]?.chapters || [];
      const newOnes = diffNewChapters(prevChapters, shareCtx.entries);

      // 保存分享快照
      m.shares[key] = {
        lastChecked: new Date().toISOString(),
        shareid: shareCtx.shareid,
        uk: shareCtx.uk,
        chapters: shareCtx.entries.map((e) => ({ fsid: e.fsid, name: e.name, isdir: e.isdir, size: e.size })),
      };

      log.info("分享顶层条目", shareCtx.entries.length, "个；新增", newOnes.length, "个");
      for (const ch of newOnes) {
        const norm = normalize(ch.name);
        report.newChapters.push(ch.name);
        setChapter(m, norm, { shareName: ch.name, shareFsid: ch.fsid, isdir: ch.isdir, status: "discovered" });
        if (dryRun) {
          log.warn("[dry-run] 将转存：", ch.name, "→", course.baidu.targetDir);
          continue;
        }
        try {
          await pan.transfer([ch.fsid], course.baidu.targetDir);
          const verify = await pan.verifyTransferred(course.baidu.targetDir, [ch.name]);
          if (!verify.ok) {
            setChapter(m, norm, { status: "blocked", blockReason: "转存后核验缺失: " + verify.missing.join(",") });
            report.errors.push({ chapter: ch.name, message: "核验缺失 " + verify.missing.join(",") });
          } else {
            setChapter(m, norm, { status: "transferred", transferredAt: new Date().toISOString() });
            report.transferred.push(ch.name);
            log.ok("已转存并核验：", ch.name);
          }
        } catch (e) {
          if (e instanceof GateError) {
            setChapter(m, norm, { status: "blocked", blockReason: e.kind + ": " + e.message });
            report.errors.push({ chapter: ch.name, kind: e.kind, message: e.message });
            log.error("闸门拦截：", ch.name, e.kind);
          } else {
            setChapter(m, norm, { status: "blocked", blockReason: String(e.message || e) });
            report.errors.push({ chapter: ch.name, message: String(e.message || e) });
          }
        }
      }
      saveManifest(course.id, m);
    }
  } finally {
    saveManifest(course.id, m);
    await closeBrowser();
  }
  console.log(JSON.stringify(report, null, 2));
  if (report.errors.length) process.exitCode = 10; // 10 = 有闸门/错误，需人工介入
}

main().catch((e) => { log.error(e.stack || String(e)); process.exit(1); });
