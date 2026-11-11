// 课程网站目录抓取：XHR JSON 优先，DOM 兜底；输出标准 [{index,title,url}]，原始快照存档备查
import { writeFileSync } from "node:fs";
import path from "node:path";
import { paths } from "./paths.mjs";
import { log } from "./log.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 从任意 JSON 中递归找“像章节标题”的字符串数组
function harvestTitles(node, acc, depth = 0) {
  if (depth > 9 || node == null) return;
  if (Array.isArray(node)) {
    // 章节对象数组：含 title/name 字段
    const objs = node.filter((x) => x && typeof x === "object");
    if (objs.length >= 2) {
      const titles = objs
        .map((o) => o.title || o.name || o.chapter_title || o.article_title || o.column_title)
        .filter((t) => typeof t === "string" && t.length >= 2 && t.length <= 120);
      if (titles.length >= Math.max(2, objs.length * 0.6)) {
        for (const t of titles) acc.add(t.trim());
      }
    }
    for (const x of node) harvestTitles(x, acc, depth + 1);
  } else if (typeof node === "object") {
    for (const v of Object.values(node)) harvestTitles(v, acc, depth + 1);
  }
}

function domExtract(page) {
  return page.evaluate(() => {
    const out = [];
    const seen = new Set();
    const push = (title, url) => {
      title = (title || "").replace(/\s+/g, " ").trim();
      if (!title || title.length < 2 || title.length > 120) return;
      const key = title;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ title, url: url || null });
    };
    // 常见目录容器：列表/表格/目录组件
    const sels = [
      ".catalog li", ".chapter li", ".directory li", ".toc li", ".article-list li",
      "[class*=catalog] [class*=item]", "[class*=chapter] [class*=item]",
      "[class*=Catalog] [class*=Item]", "[class*=menu] li", "ol li", "ul li",
      "table tr", "[role=listitem]", "[role=row]",
    ];
    for (const sel of sels) {
      const nodes = document.querySelectorAll(sel);
      if (nodes.length < 2) continue;
      for (const n of nodes) {
        const a = n.querySelector("a[title], a");
        const t = a?.getAttribute("title") || a?.textContent || n.textContent;
        push(t, a?.href || null);
      }
      if (out.length >= 3) break;
    }
    // 兜底：标题标签
    if (out.length < 2) {
      for (const h of document.querySelectorAll("h1,h2,h3,h4")) push(h.textContent, null);
    }
    return out;
  });
}

// 路飞学城章节解析（纯函数，便于单测）。容错多种 data 形状，保留接口返回顺序。
export function parseLuffycitySections(json) {
  if (!json || json.code !== 0 || json.data == null) return [];
  const out = [];
  const seen = new Set();
  const push = (title) => {
    title = String(title || "").replace(/\s+/g, " ").trim();
    if (!title || title.length < 2 || title.length > 120) return;
    if (seen.has(title)) return;
    seen.add(title);
    out.push({ index: out.length + 1, title, url: null });
  };
  const walk = (node) => {
    if (node == null) return;
    if (Array.isArray(node)) {
      for (const x of node) walk(x);
      return;
    }
    if (typeof node !== "object") return;
    // 叶子：课时对象
    const leaf = node.title || node.name || node.section_name || node.chapter_title;
    const children = node.sections || node.children || node.chapters || node.lessons || node.course_sections;
    if (Array.isArray(children)) {
      if (leaf) push(leaf);
      for (const c of children) walk(c);
    } else if (leaf) {
      push(leaf);
    }
  };
  const data = json.data;
  if (Array.isArray(data)) walk(data);
  else walk(data.sections || data.list || data.results || data);
  return out;
}

// 路飞学城：拦截 api.luffycity.com 的 play/sections 响应（页面自身请求带登录 cookie，规避跨域 fetch 限制）
export async function scrapeLuffycity({ page, course }) {
  const m = course.toc.url.match(/(?:play|course)\/(\d+)/);
  const playId = course.toc.playId || (m ? m[1] : null);
  if (!playId) throw new Error("无法从路飞学城 URL 解析 playId：" + course.toc.url);
  const wantRe = new RegExp("api\\.luffycity\\.com/api/v1/play/sections/.*section_id=" + playId);
  const captured = [];
  const onResp = async (resp) => {
    try {
      if (!wantRe.test(resp.url())) return;
      const json = await resp.json().catch(() => null);
      if (json) captured.push(json);
    } catch {}
  };
  page.on("response", onResp);
  await page.goto(course.toc.url, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  // 等待接口返回，最长 20s
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline && captured.length === 0) {
    await sleep(1000);
  }
  page.off("response", onResp);

  let items = [];
  let raw = null;
  if (captured.length) {
    raw = captured[captured.length - 1];
    items = parseLuffycitySections(raw);
  }
  if (items.length === 0) {
    log.warn("路飞 sections 接口未取到/未解析出章节，回退通用 XHR/DOM 收割");
    return scrapeToc({ page, course });
  }
  const dump = {
    time: new Date().toISOString(),
    courseId: course.id,
    platform: "luffycity",
    url: course.toc.url,
    items,
    apiCode: raw && raw.code,
  };
  const out = path.join(paths.logsDir(), "toc-" + course.id + "-" + Date.now() + ".json");
  writeFileSync(out, JSON.stringify(dump, null, 2));
  log.ok("路飞学城目录抓取", items.length, "章，快照：", out);
  return { items, snapshot: out };
}

export async function scrapeToc({ page, course }) {
  if (course.toc && course.toc.platform === "luffycity") {
    return scrapeLuffycity({ page, course });
  }
  const captured = [];
  const onResp = async (resp) => {
    try {
      const ct = resp.headers()["content-type"] || "";
      if (!ct.includes("json")) return;
      const json = await resp.json().catch(() => null);
      if (json) captured.push({ url: resp.url(), json });
    } catch {}
  };
  page.on("response", onResp);

  await page.goto(course.toc.url, { waitUntil: "networkidle", timeout: 60000 }).catch(async () => {
    await page.goto(course.toc.url, { waitUntil: "domcontentloaded", timeout: 60000 });
  });
  // 滚动触发懒加载
  for (let i = 0; i < 8; i++) {
    await page.mouse.wheel(0, 2400);
    await sleep(700);
  }
  await sleep(1500);

  // 1) XHR  harvesting
  const titleSet = new Set();
  for (const c of captured) harvestTitles(c.json, titleSet);
  let items = [...titleSet].map((t) => ({ title: t, url: null }));

  // 2) DOM 兜底/合并
  const domItems = await domExtract(page);
  if (domItems.length > items.length) items = domItems;
  else {
    const have = new Set(items.map((x) => x.title));
    for (const d of domItems) if (!have.has(d.title)) items.push(d);
  }
  page.off("response", onResp);

  // 过滤明显非章节条目（导航/按钮文案）
  const bad = /^(首页|登录|注册|下载|更多|展开|收起|目录|课程介绍|猜你喜欢|相关推荐)$/;
  items = items.filter((x) => !bad.test(x.title.trim()));

  // 标准化序号
  items = items.map((x, i) => ({ index: i + 1, title: x.title.trim(), url: x.url }));

  const dump = {
    time: new Date().toISOString(),
    courseId: course.id,
    platform: course.toc.platform,
    url: course.toc.url,
    items,
    xhrCount: captured.length,
    xhrTitles: [...titleSet],
    capturedUrls: captured.map((c) => c.url),
  };
  const out = path.join(paths.logsDir(), "toc-" + course.id + "-" + Date.now() + ".json");
  writeFileSync(out, JSON.stringify(dump, null, 2));
  log.ok("目录抓取", items.length, "章，快照：", out);
  if (items.length === 0) {
    throw new Error("未抓到任何章节条目，请检查登录态或用 toc 快照排查：" + out);
  }
  return { items, snapshot: out };
}
