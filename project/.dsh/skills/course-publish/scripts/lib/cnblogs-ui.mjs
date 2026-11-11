// 博客园网页端操作：合集选择（MetaWeblog 不支持合集）。选择器基于启发式，失效时截图抛错
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./paths.mjs";
import { log } from "./log.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(page, tag) {
  try {
    const p = path.join(paths.logsDir(), "cnblogs-ui-" + tag + "-" + Date.now() + ".png");
    await page.screenshot({ path: p, fullPage: true });
    return p;
  } catch {
    return null;
  }
}

// 确认 i.cnblogs.com 已登录
export async function assertCnblogsLogin(page) {
  await page.goto("https://i.cnblogs.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(2000);
  const url = page.url();
  if (/login|signin/i.test(url)) {
    throw new Error("博客园网页端未登录，请在浏览器 profile 中登录 i.cnblogs.com 后重试");
  }
  const body = await page.evaluate(() => document.body.innerText.slice(0, 400)).catch(() => "");
  if (/登录|账号密码/.test(body) && !/退出|我的博客/.test(body)) {
    throw new Error("博客园网页端疑似未登录，请先登录");
  }
}

// 从公开博文 HTML 提取纯标题（博客园标题常带 " - 我的博客 - 博客园" 等后缀）
export function extractPostTitle(html) {
  const m = String(html || "").match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (!m) return null;
  let title = m[1].replace(/\s+/g, " ").trim();
  // 循环剥离尾部含"博客/cnblogs"的站点后缀段
  const suffix = /\s*[-_|·]\s*[^-_|·]*(博客|cnblogs)[^-_|·]*$/i;
  while (suffix.test(title)) title = title.replace(suffix, "").trim();
  return title || null;
}

// 后台列表行文本是否对应目标标题（归一化包含匹配，容忍列表上的操作按钮文案）
export function rowMatchesTitle(rowText, title) {
  if (!title) return false;
  const norm = (s) => String(s).replace(/\s+/g, "").toLowerCase();
  const t = norm(title);
  const r = norm(rowText);
  return r.includes(t) && t.length >= 4;
}

// 给已发布文章设置合集。postUrl: 公开文章 URL
export async function assignCollection({ page, postUrl, collectionName, courseId }) {
  await assertCnblogsLogin(page);

  // 先抓公开页标题，用于在后台博文列表中精确定位行（postId 通常不显示在列表里）
  let titleHint = (postUrl.match(/p\/(\d+)/) || [])[1] || postUrl;
  try {
    const resp = await fetch(postUrl, { redirect: "follow" });
    if (resp.ok) {
      const t = extractPostTitle(await resp.text());
      if (t) titleHint = t;
    }
  } catch (e) {
    log.warn("公开页标题抓取失败，回退 postId 定位：", e.message);
  }

  // 进入编辑页（博文后台编辑 URL 通常可从公开页“编辑”或 i 后台 posts 列表进入）
  await page.goto("https://i.cnblogs.com/posts", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(2500);

  // 在博文列表找到目标文章并点编辑
  const row = page.locator("table tr, .post-item, .list-item").filter({ hasText: titleHint }).first();
  if (await row.count().catch(() => 0)) {
    await row.locator("a:has-text('编辑'), button:has-text('编辑')").first().click().catch(() => {});
  } else {
    // 直接尝试约定编辑 URL
    const id = (postUrl.match(/p\/(\d+)/) || [])[1];
    if (id) await page.goto("https://i.cnblogs.com/posts/edit?postId=" + id, { waitUntil: "domcontentloaded" });
  }
  await sleep(2500);

  // 合集控件：博客园编辑页合集为下拉/多选；先找“合集/文集/专栏”相关 label
  const handle = page.locator(
    "label:has-text('合集'), label:has-text('文集'), .collection-select, [class*=collection], [class*=Collection]",
  ).first();
  if (!(await handle.count())) {
    const p = await shot(page, "no-collection-" + courseId);
    throw new Error("编辑页未找到合集控件（页面结构可能变化），截图：" + p);
  }
  await handle.click();
  await sleep(1000);

  // 选择已有合集
  const option = page.locator(".el-select-dropdown__item, .dropdown-item, li, option").filter({ hasText: collectionName }).first();
  if (await option.count().catch(() => 0)) {
    await option.click();
    log.ok("已选择合集：", collectionName);
  } else {
    // 创建新合集
    log.warn("合集不存在，尝试新建：", collectionName);
    await page.locator("button:has-text('新建'), button:has-text('创建'), a:has-text('新建合集')").first().click().catch(() => {});
    await sleep(800);
    const input = page.locator("input:visible, .el-input__inner:visible").last();
    await input.fill(collectionName);
    await page.keyboard.press("Enter");
    await sleep(1000);
  }

  await page.locator("button:has-text('保存'), button:has-text('发布'), #btnPublish, .publish-btn").first().click();
  await sleep(3000);
  await shot(page, "after-save-" + courseId);

  // 公开页核验
  await page.goto(postUrl, { waitUntil: "domcontentloaded" });
  await sleep(1500);
  const ok = await page.evaluate((name) => document.body.innerText.includes(name), collectionName).catch(() => false);
  if (!ok) log.warn("公开页未核验到合集名，需人工确认：", collectionName);
  return { collection: collectionName, verified: ok };
}

