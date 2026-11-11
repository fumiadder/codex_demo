import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { paths } from "./paths.mjs";
import { log } from "./log.mjs";

// 全局共享一个持久化浏览器实例（登录态保存在 browser-profile/）
let _browser;
let _context;

export async function getContext({ headless = false } = {}) {
  if (_context) return _context;
  mkdirSync(paths.browserProfile, { recursive: true });
  const proxyServer = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy;
  _context = await chromium.launchPersistentContext(paths.browserProfile, {
    headless,
    proxy: proxyServer ? { server: proxyServer } : undefined,
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    args: ["--disable-blink-features=AutomationControlled"],
  });
  // 反自动化指纹
  await _context.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  _browser = _context.browser();
  log.info("浏览器已启动，profile:", paths.browserProfile);
  return _context;
}

export async function closeBrowser() {
  if (_context) {
    await _context.close().catch(() => {});
    _context = undefined;
    _browser = undefined;
  }
}

// 判断百度网盘是否已登录（页面内 fetch 受限域 API）
export async function assertBaiduLogin(page) {
  await page.goto("https://pan.baidu.com", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(2500);
  const ok = await page.evaluate(async () => {
    try {
      const r = await fetch("https://pan.baidu.com/api/list?dir=/&order=time&desc=1", {
        credentials: "include",
      });
      const j = await r.json();
      return j.errno === 0;
    } catch {
      return false;
    }
  });
  return ok;
}

// 拦截 XHR JSON 响应的通用助手
export function captureResponses(page, urlPattern, sink) {
  page.on("response", async (resp) => {
    try {
      const u = resp.url();
      if (!urlPattern.test(u)) return;
      const ct = resp.headers()["content-type"] || "";
      if (!ct.includes("json")) return;
      const json = await resp.json();
      sink({ url: u, json, status: resp.status() });
    } catch {}
  });
}

export async function humanDelay(ms = 600) {
  await new Promise((r) => setTimeout(r, ms + Math.floor(Math.random() * ms)));
}
