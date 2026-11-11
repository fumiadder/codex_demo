// 博客园合集/课程发布后的只读审核巡检：不修改任何文章。
// 用法：
//   node cnblogs-review-audit.mjs --collection=39559
//   node cnblogs-review-audit.mjs --collection="AI agent七期"
//   node cnblogs-review-audit.mjs --course=ai-agent-8
// 输出 state/<course>.review-audit.json 或 logs/review-audit-<collectionId>-<time>.json。
// 退出码：0 全部公开；10 存在待审/重审/隐藏/缺失；1 运行错误。
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { classifyVisibility, reviewLabel, summarizeReviewRows } from "./lib/cnblogs-review.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((arg) => {
  const index = arg.indexOf("=");
  return index < 0 ? [arg.slice(2), true] : [arg.slice(2, index), arg.slice(index + 1)];
}));
const COURSE = args.course || null;
let collectionInput = args.collection || null;
const USE_COURSE_STATE = Boolean(COURSE && !collectionInput);

function parseCollectionId(value) {
  const text = String(value || "").trim();
  return /^\d+$/.test(text) ? text : null;
}

async function resolveFromCourseState(course) {
  const file = path.join(paths.stateDir, course + ".publish-state.json");
  try {
    const state = JSON.parse(await fs.readFile(file, "utf8"));
    const entry = Object.values(state).find((item) => item && item.collectionId);
    if (!entry) return null;
    return { id: String(entry.collectionId), name: entry.collection ? String(entry.collection) : null };
  } catch {
    return null;
  }
}

async function resolveCollection(page) {
  let id = parseCollectionId(collectionInput);
  let name = id ? null : String(collectionInput || "").trim() || null;
  if (!id && USE_COURSE_STATE) {
    const fromState = await resolveFromCourseState(COURSE);
    if (fromState) { id = fromState.id; name = fromState.name; }
  }
  if (!id && name) {
    await page.goto("https://i.cnblogs.com/collections", { waitUntil: "domcontentloaded", timeout: 60000 });
    await new Promise((resolve) => setTimeout(resolve, 1800));
    const links = await page.evaluate(() => [...document.querySelectorAll('a[href*="collectionId="]')].map((link) => ({
      href: link.href,
      text: (link.textContent || "").trim().replace(/\s+/g, " "),
    })));
    const wanted = name.toLowerCase();
    const match = links.find((link) => link.text.toLowerCase() === wanted) || links.find((link) => link.text.toLowerCase().includes(wanted));
    if (!match) throw new Error("找不到合集：" + name);
    id = new URL(match.href).searchParams.get("collectionId");
    name = match.text;
  }
  if (!id) throw new Error("需要 --collection=<合集ID或名称>，或带合集记录的 --course=<courseId>");
  return { id, name };
}

async function main() {
  if (!COURSE && !collectionInput) {
    console.log("用法：node cnblogs-review-audit.mjs --collection=<合集ID/名称> | --course=<courseId> [--verbose]");
    process.exitCode = 1;
    return;
  }
  const ctx = await getContext({ headless: true });
  const page = ctx.pages()[0] || (await ctx.newPage());
  try {
    const collection = await resolveCollection(page);
    await page.goto("https://i.cnblogs.com/collections/detail?collectionId=" + encodeURIComponent(collection.id), { waitUntil: "domcontentloaded", timeout: 60000 });
    await new Promise((resolve) => setTimeout(resolve, 2200));
    if (/login|signin/i.test(page.url())) throw new Error("博客园后台未登录；先运行 npm run cnblogs:login");
    const pageText = await page.evaluate(() => document.body.innerText);
    const heading = (/合集:\s*([^\n]+)/.exec(pageText) || [])[1];
    if (heading) collection.name = heading.trim();
    const archiveRows = await page.evaluate(() => {
      const table = [...document.querySelectorAll("table")].find((candidate) => candidate.querySelector('a[href*="/p/"]'));
      if (!table) return [];
      return [...table.querySelectorAll("tbody tr")].map((tr) => {
        const link = tr.querySelector('a[href*="/p/"]');
        if (!link) return null;
        const cells = [...tr.children].map((cell) => (cell.textContent || "").trim().replace(/\s+/g, " "));
        const idMatch = (link.href || "").match(/\/p\/(\d+)/);
        return {
          position: Number(cells[0]),
          title: (link.textContent || "").trim().replace(/\s+/g, " "),
          href: link.href,
          postId: idMatch ? idMatch[1] : null,
          pageStatus: cells[3] || null,
          publishedAt: cells[4] || null,
          viewCount: Number(cells[5]) || 0,
        };
      }).filter(Boolean).filter((row) => Number.isFinite(row.position) && row.position > 0 && row.postId);
    });
    if (!archiveRows.length) throw new Error("后台合集中没有读到文章；页面结构可能变化：" + page.url());

    // 复核后台真实审核状态；分页结果按 postId 去重，避免滚动列表造成重复行。
    const backendRaw = await page.evaluate(async () => {
      const rows = [];
      for (let pageIndex = 0; pageIndex < 60; pageIndex++) {
        const response = await fetch("https://i.cnblogs.com/api/posts/list?p=" + pageIndex + "&cfg=0&t=1&s=", { headers: { Accept: "application/json" }, credentials: "include" });
        if (!response.ok) break;
        const data = await response.json();
        const list = data.postList || [];
        if (!list.length) break;
        rows.push(...list);
        if (list.length < 10) break;
      }
      return rows;
    });
    const backendById = new Map();
    for (const item of backendRaw) backendById.set(String(item.id), item);

    const urls = archiveRows.map((row) => row.href.endsWith(".html") ? row.href : row.href + ".html");
    const publicResults = new Array(archiveRows.length);
    let next = 0;
    async function worker() {
      while (true) {
        const index = next++;
        if (index >= urls.length) return;
        try {
          const response = await fetch(urls[index], { redirect: "follow", signal: AbortSignal.timeout(25000), headers: { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36", Accept: "text/html" } });
          const html = await response.text();
          publicResults[index] = { status: response.status, title: (/<title[^>]*>([\s\S]*?)<\/title>/i.exec(html) || [])[1]?.trim() || null, url: urls[index] };
        } catch (error) {
          publicResults[index] = { status: 0, title: null, url: urls[index], error: String(error.message || error) };
        }
      }
    }
    await Promise.all(Array.from({ length: 6 }, worker));

    const rows = archiveRows.map((row, index) => {
      const backend = backendById.get(String(row.postId));
      const visibility = classifyVisibility({ backendFound: Boolean(backend), reviewStatus: backend?.reviewStatus, publicStatus: publicResults[index]?.status });
      return {
        ...row,
        backendReviewStatus: backend ? backend.reviewStatus : null,
        backendReviewLabel: backend ? reviewLabel(backend.reviewStatus) : "后台无记录",
        backendReviewed: backend ? backend.reviewed : null,
        backendDateUpdated: backend ? backend.dateUpdated : null,
        publicStatus: publicResults[index]?.status,
        publicTitle: publicResults[index]?.title,
        state: visibility.state,
        label: visibility.label,
      };
    });
    const summary = summarizeReviewRows(rows);
    const report = { at: new Date().toISOString(), collection, summary, rows };
    const output = COURSE
      ? path.join(paths.stateDir, COURSE + ".review-audit.json")
      : path.join(paths.logsDir(), "review-audit-" + collection.id + "-" + new Date().toISOString().replace(/[-:]/g, "").replace(/\./g, "") + ".json");
    await fs.mkdir(path.dirname(output), { recursive: true });
    await fs.writeFile(output, JSON.stringify(report, null, 2));

    console.log("合集:", collection.name || "?", "(" + collection.id + ")");
    console.log("汇总:", JSON.stringify(summary));
    for (const row of rows) {
      const mark = row.state === "public" || row.state === "public-unregistered" ? "OK" : "!!";
      console.log(mark, String(row.position).padStart(2, "0"), row.label.padEnd(8), "http=" + row.publicStatus, row.title.slice(0, 72), "id=" + row.postId);
    }
    const issues = rows.filter((row) => row.state !== "public" && row.state !== "public-unregistered");
    if (issues.length) console.log("待处理:", issues.map((row) => row.postId + ":" + row.label + ":" + row.title).join("\n"));
    console.log("报告:", output);
    process.exitCode = summary.issues ? 10 : 0;
  } finally {
    await closeBrowser();
  }
}

main().catch(async (error) => {
  log.error(error.stack || error.message || String(error));
  await closeBrowser().catch(() => {});
  process.exitCode = 1;
});
