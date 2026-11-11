// 暂存 CLI：把已合并章节的图片上云（幂等缓存）并生成可预览的 preview MD，但不发布。
// 用法：node stage.mjs --course=ai-agent-8 [--only=slug] [--all-pending] [--force] [--dry]
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { getCourse, loadCnblogsSecrets } from "./lib/config.mjs";
import { CnblogsClient } from "./lib/cnblogs.mjs";
import { QuotaTracker } from "./lib/media-quota.mjs";
import { stageChapter, loadStaging, STAGE, previewPath } from "./lib/staging.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const COURSE = args.course || "ai-agent-8";
const dry = Boolean(args.dry);
const force = Boolean(args.force); // 已发布章节更新图片时显式重新暂存（默认不碰已发布）
const course = getCourse(COURSE);
const plan = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".chapter-plan.json"), "utf8"));
const progress = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".edition8-progress.json"), "utf8"));
const publishState = await fs.readFile(path.join(paths.stateDir, COURSE + ".publish-state.json"), "utf8").then(JSON.parse).catch(() => ({}));
const staging = await loadStaging(COURSE);
const outRoot = paths.courseOutput(COURSE);

const targets = [];
for (const c of plan.chapters) {
  const m = progress[c.slug];
  if (!m?.merged || m.blocked) continue;
  if (!force && publishState[c.slug]?.published) continue;
  const st = staging.chapters[c.slug]?.status;
  if (!force && (st === STAGE.APPROVED || st === STAGE.PUBLISHED)) continue;
  if (args.only && c.slug !== args.only) continue;
  if (args["all-pending"] === false && !args.only) continue;
  targets.push(c);
}
log.info("待暂存章节", targets.length, dry ? "(dry-run)" : "");
if (dry) { for (const c of targets) console.log("DRY", c.slug, "|", c.titleOverride || c.webTitle); process.exit(0); }

const secrets = loadCnblogsSecrets();
const client = new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
await client.verify();
const quota = new QuotaTracker(path.join(paths.stateDir, COURSE + ".media-quota.json"));
await quota.load();

let ok = 0, partial = 0;
for (const c of targets) {
  const slug = c.slug;
  const mdPath = path.join(outRoot, "00-" + slug + ".md");
  try {
    const entry = await stageChapter({ client, quota, course: COURSE, slug, title: c.titleOverride || c.webTitle, mdPath });
    if (entry.status === STAGE.STAGED) ok++; else partial++;
  } catch (e) {
    log.error("暂存失败", slug, e.message);
    if (String(e.name) === "MediaQuotaError") { log.warn("配额暂停，剩余章节保留待续传"); break; }
  }
}
console.log("STAGE_DONE staged=" + ok + " partial=" + partial);
