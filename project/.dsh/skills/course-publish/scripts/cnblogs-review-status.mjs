// 博客园文章审核状态复查：读后台 /api/posts/list，映射本次发布章节的 reviewStatus（0待审/1通过/2重审）。
// 用法：node cnblogs-review-status.mjs [courseId]（默认 ai-agent-8）
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { paths } from "./lib/paths.mjs";
const courseId = process.argv[2] || "ai-agent-8";
const ctx = await getContext({ headless: true });
const page = ctx.pages()[0] || (await ctx.newPage());
await page.goto("https://i.cnblogs.com/posts", { waitUntil: "domcontentloaded" });
await new Promise((r) => setTimeout(r, 4000));
const all = await page.evaluate(async () => {
  const out = [];
  for (let p = 0; p < 8; p++) {
    const r = await fetch("https://i.cnblogs.com/api/posts/list?p=" + p + "&cfg=0&t=1&s=", { headers: { Accept: "application/json" }, credentials: "include" });
    const j = await r.json();
    const list = j.postList || [];
    if (!list.length) break;
    for (const x of list) out.push({ id: x.id, title: x.title, reviewStatus: x.reviewStatus, reviewed: x.reviewed, isPublished: x.isPublished, viewCount: x.viewCount });
    if (list.length < 10) break;
  }
  return out;
});
await closeBrowser();
const pubFile = path.join(paths.stateDir, courseId + ".publish-state.json");
let pub = {};
try { pub = JSON.parse(readFileSync(pubFile, "utf8")); } catch {}
const byId = new Map(all.map((x) => [String(x.id), x]));
const rows = [];
for (const [slug, v] of Object.entries(pub)) {
  if (!v.published) continue;
  const x = byId.get(String(v.postId));
  rows.push({ slug, postId: v.postId, reviewStatus: x ? x.reviewStatus : "NOT_FOUND", reviewed: x ? x.reviewed : null, views: x ? x.viewCount : null });
}
const label = (s) => (s === 1 ? "已通过/公开" : s === 0 ? "待审核/404" : s === 2 ? "重审中/404" : String(s));
const pending = rows.filter((r) => r.reviewStatus === 0 || r.reviewStatus === 2);
const approved = rows.filter((r) => r.reviewStatus === 1);
console.log("后台总文章:", all.length, " 本次", rows.length, "篇：公开", approved.length, " 待审", pending.length);
for (const r of rows) console.log(" ", r.slug.padEnd(14), label(r.reviewStatus), "views=" + r.views);
if (pending.length) console.log("待审核:", pending.map((r) => r.slug).join(","));
writeFileSync(path.join(paths.stateDir, courseId + ".review-state.json"), JSON.stringify({ at: new Date().toISOString(), rows }, null, 2));
