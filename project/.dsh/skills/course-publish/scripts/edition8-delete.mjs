// 第八期博文批量删除：按 publish-state 中的 postId 调 blogger.deletePost（移入博客园回收站，可短期恢复）。
// 删除前先把 postId/标题/URL 全量备份到 deletion-backup.json；默认 dry-run，--apply 才执行，--only=slug 可单篇试删。
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { getCourse, loadCnblogsSecrets } from "./lib/config.mjs";
import { CnblogsClient } from "./lib/cnblogs.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const COURSE = "ai-agent-8";
const APPLY = Boolean(args.apply);
const only = args.only || null;
const gapMs = Number(args.gap || 4000);
const plan = JSON.parse(await fs.readFile(path.join(paths.stateDir, COURSE + ".chapter-plan.json"), "utf8"));
const titleOf = new Map(plan.chapters.map((c) => [c.slug, c.titleOverride || c.webTitle]));
const pubFile = path.join(paths.stateDir, COURSE + ".publish-state.json");
const pub = JSON.parse(await fs.readFile(pubFile, "utf8"));
const backupFile = path.join(paths.stateDir, COURSE + ".deletion-backup.json");

const targets = Object.entries(pub)
  .filter(([slug, v]) => v.published && !v.deleted && (!only || slug === only))
  .map(([slug, v]) => ({ slug, postId: String(v.postId), title: titleOf.get(slug) || slug, postUrl: v.postUrl, reviewPending: Boolean(v.reviewPending) }));

if (!targets.length) { log.info("没有可删除的已发布记录"); process.exit(0); }
log.info((APPLY ? "执行删除" : "DRY-RUN") + " 目标 " + targets.length + " 篇");

// 删除前全量备份（含本次未删的，保证可从回收站按图索骥）
const backup = { generatedAt: new Date().toISOString(), note: "postId→标题/URL 映射，用于博客园回收站恢复", posts: targets };
await fs.writeFile(backupFile, JSON.stringify(backup, null, 2));
log.ok("已写备份:", backupFile);
for (const t of targets) log.info("  -", t.postId, t.slug, t.title.slice(0, 40));

if (!APPLY) { log.info("DRY-RUN 结束，确认后加 --apply 执行（可先 --only=slug 单篇试删）"); process.exit(0); }

const secrets = loadCnblogsSecrets();
const client = new CnblogsClient({ blogApp: secrets.CNBLOGS_BLOG_APP, username: secrets.CNBLOGS_USERNAME, password: secrets.CNBLOGS_PASSWORD });
await client.verify();

let ok = 0, fail = 0;
for (const t of targets) {
  try {
    await client.deletePost(t.postId);
    // 验证：getPost 应失败（文章已不在）
    let gone = false;
    try { await client.getPost(t.postId); } catch { gone = true; }
    pub[t.slug] = { ...pub[t.slug], published: false, deleted: true, deletedAt: new Date().toISOString(), deleteVerified: gone, postId: t.postId };
    await fs.writeFile(pubFile, JSON.stringify(pub, null, 2));
    ok++;
    log.ok("已删除 " + t.slug + " (" + t.postId + ")" + (gone ? " 已确认移除" : " 接口已受理但getPost仍可读"));
  } catch (e) {
    fail++;
    pub[t.slug] = { ...pub[t.slug], deleteError: String(e.message || e) };
    await fs.writeFile(pubFile, JSON.stringify(pub, null, 2));
    log.error("删除失败 " + t.slug, e.message);
  }
  await new Promise((r) => setTimeout(r, gapMs));
}
log.info(`删除完成：成功 ${ok}/${targets.length}，失败 ${fail}`);
