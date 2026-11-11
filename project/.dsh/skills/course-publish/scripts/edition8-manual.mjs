// 手动大文件接入：读取 downloads/ai-agent-8/_manual 中用户用客户端下载的归档，解压并合并到对应挂起章节。纯本地，不启动浏览器。
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { resolveInside, extractArchive } from "./lib/baidu-download.mjs";
import { buildChapterMarkdown } from "./lib/docs-md.mjs";
const COURSE = "ai-agent-8";
const apply = process.argv.includes("--apply");
const plan = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".chapter-plan.json"), "utf8"));
const progressFile = path.join(paths.stateDir, COURSE + ".edition8-progress.json");
const progress = JSON.parse(await fs.readFile(progressFile, "utf8"));
const dlRoot = paths.courseDownloads(COURSE);
const outRoot = paths.courseOutput(COURSE);
const manualDir = path.join(dlRoot, "_manual");
await fs.mkdir(manualDir, { recursive: true });
const entries = (await fs.readdir(manualDir, { withFileTypes: true })).filter((e) => e.isFile()).map((e) => e.name);
if (!entries.length) { console.log("_manual 为空，请把客户端下载的 6 个归档放入: " + manualDir); process.exit(0); }
const results = [];
for (const name of entries) {
  const m = name.match(/day\s*(\d+)/i);
  if (!m) { results.push({ name, ok: false, reason: "文件名无法识别 day 编号" }); continue; }
  const dayNum = String(Number(m[1])).padStart(2, "0");
  const chapter = plan.chapters.find((c) => c.track === "main" && String(c.day).padStart(2, "0") === dayNum);
  if (!chapter) { results.push({ name, ok: false, reason: "找不到 day" + dayNum + " 对应章节" }); continue; }
  const archive = path.join(manualDir, name);
  const chapterDir = resolveInside(dlRoot, chapter.slug);
  const stem = name.replace(/\.[^.]+$/, "");
  const exDir = resolveInside(chapterDir, "_extracted", stem);
  if (!apply) { results.push({ name, ok: true, dryRun: true, slug: chapter.slug, title: chapter.webTitle, exDir }); continue; }
  try {
    await fs.mkdir(exDir, { recursive: true });
    await extractArchive(archive, exDir);
    const md = await buildChapterMarkdown({ chapterDir, outDir: outRoot, chapterTitle: chapter.webTitle, frontmatter: { webIndex: chapter.webIndex, slug: chapter.slug, track: chapter.track, day: chapter.day, source: chapter.units.join(" | ") + " | [手动客户端下载]", gated: "" } });
    progress[chapter.slug] = { ...(progress[chapter.slug] || {}), downloaded: true, blocked: undefined, gated: [], merged: true, mdPath: md.mdPath, report: md.report, manual: true, at: new Date().toISOString() };
    delete progress[chapter.slug].blocked;
    await fs.writeFile(progressFile, JSON.stringify(progress, null, 2));
    results.push({ name, ok: true, slug: chapter.slug, md: path.basename(md.mdPath), converted: md.report.converted.length });
    log.ok("手动章节已合并", chapter.slug);
  } catch (e) { results.push({ name, ok: false, reason: String(e.message || e) }); }
}
console.log(JSON.stringify(results, null, 1));
if (!apply) console.log("DRY-RUN 完成，确认映射无误后加 --apply 执行");
