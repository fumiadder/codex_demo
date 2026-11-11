// 百度网盘桌面客户端驱动下载：网页端勾选文件→点“使用客户端下载”→捕获 baiduyunguanjia evoked scheme→系统 open 重放，
// 由已登录的 BaiduNetdisk 客户端经 P2SP 通道下载到 ~/Downloads，校验完整性后归档到 _manual/。
// 背景：≥46MB 文件 REST 直链被 31326 网关拦截，必须走客户端；本机 osascript 无辅助功能权限，故用 scheme 重放替代 UI 自动化。
// 用法：node baidu-client-drive.mjs [--files=day07,day21] [--apply] [--timeout-min=60]
import { promises as fs } from "node:fs";
import { execFile } from "node:child_process";
import path from "node:path";
import os from "node:os";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";

const args = Object.fromEntries(process.argv.slice(2).map((a) => { const i = a.indexOf("="); return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]; }));
const APPLY = Boolean(args.apply);
const COURSE = "ai-agent-8";
const PAN_PATH = ["自动上传", "课程", "Luffy", "AI 智能体", "8 期", "AI智能体八期"];
const WANT_PREFIX = (args.files ? String(args.files).split(",") : ["day07", "day10", "day16", "day17", "day21", "day23"]).map((s) => s.trim().toLowerCase());
const DOWNLOAD_DIR = path.join(os.homedir(), "Downloads");
const DEST_DIR = path.join(paths.root, "downloads", COURSE, "_manual");
const STATE_FILE = path.join(paths.stateDir, COURSE + ".client-drive.json");
const TIMEOUT_MIN = Number(args["timeout-min"] || 90);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const execFileP = (cmd, params) => new Promise((res, rej) => execFile(cmd, params, (e, stdout, stderr) => (e ? rej(e) : res({ stdout, stderr }))));

async function loadState() { try { return JSON.parse(await fs.readFile(STATE_FILE, "utf8")); } catch { return { files: {} }; } }
async function saveState(s) { await fs.writeFile(STATE_FILE, JSON.stringify(s, null, 2)); }

// 完整性：zip 用 unzip -t；rar 用 lsar（本机已装 unar）
async function archiveOk(file) {
  const ext = path.extname(file).toLowerCase();
  try {
    if (ext === ".rar") { const r = await execFileP("lsar", [file]); return r.stdout.length > 0; }
    const r = await execFileP("unzip", ["-t", file]); return /No errors detected/.test(r.stdout + r.stderr);
  } catch { return false; }
}

async function ensureAppRunning() {
  try { await execFileP("pgrep", ["-x", "BaiduNetdisk"]); log.ok("网盘客户端运行中"); }
  catch { log.info("启动网盘客户端…"); await execFileP("open", ["-a", "BaiduNetdisk"]); await sleep(15000); }
}

async function captureSchemes() {
  await ensureAppRunning();
  await fs.mkdir(DEST_DIR, { recursive: true });
  const state = await loadState();
  const ctx = await getContext({ headless: true });
  const page = ctx.pages()[0] || (await ctx.newPage());
  const cdp = await ctx.newCDPSession(page);
  await cdp.send("Network.enable");
  let lastScheme = null;
  cdp.on("Network.requestWillBeSent", (e) => { if (e.request.url.startsWith("baiduyunguanjia:")) lastScheme = e.request.url; });
  await page.goto("https://pan.baidu.com/disk/main", { waitUntil: "domcontentloaded", timeout: 60000 });
  await sleep(9000);
  async function enterFolder(name) {
    const cell = page.locator(".wp-s-pan-list__file-name").filter({ hasText: name }).first();
    await cell.waitFor({ state: "visible", timeout: 15000 });
    await cell.dblclick();
    await sleep(4500);
  }
  for (const f of PAN_PATH) await enterFolder(f);
  const nameCells = await page.locator(".wp-s-pan-list__file-name").allInnerTexts();
  const names = nameCells.map((t) => t.split("\n")[0].trim()).filter(Boolean);
  const uniq = [...new Set(names)];
  const targets = [];
  for (const name of uniq) {
    const low = name.toLowerCase();
    if (WANT_PREFIX.some((p) => low.startsWith(p) || low.includes(p + ".") || low.includes(p + "-") || low.includes(p + "_"))) targets.push(name);
  }
  log.info("目录内匹配目标 " + targets.length + " 个：" + targets.join(" | "));
  const plans = [];
  for (const name of targets) {
    if (state.files[name]?.archived) { log.info("已归档跳过：" + name); continue; }
    plans.push(name);
  }
  if (!APPLY) { await closeBrowser(); log.info("DRY-RUN 匹配目标：" + JSON.stringify(plans)); return { plans, state }; }
  for (const name of plans) {
    try {
      lastScheme = null;
      const row = page.locator("tr.wp-s-pan-table__body-row").filter({ hasText: name }).first();
      await row.click();
      await sleep(1200);
      await page.locator(".wp-s-agile-tool-bar__header").getByText("下载", { exact: true }).first().click({ force: true });
      await sleep(3500);
      await page.locator("button.nd-download__guide-action-item.is-app").first().click({ force: true });
      // 实测：headless Chromium 点击该按钮会经 LaunchServices 直接唤起客户端建任务（5/5 成功）；
      // CDP 对外部协议导航的 requestWillBeSent 上报不稳定，故 scheme 仅作遥测，缺失不判失败，也不 open 重放（避免重复任务）。
      const t0 = Date.now();
      while (!lastScheme && Date.now() - t0 < 4000) await sleep(300);
      if (lastScheme) log.ok("遥测到 scheme：" + lastScheme.slice(0, 90));
      else log.info("点击已唤起客户端（无 scheme 事件，属正常）：" + name);
      state.files[name] = { ...(state.files[name] || {}), queuedAt: new Date().toISOString(), scheme: lastScheme ? lastScheme.slice(0, 200) : null, downloadDir: DOWNLOAD_DIR, status: "queued" };
      await saveState(state);
      await sleep(2500);
      await page.keyboard.press("Escape").catch(() => {});
      await page.locator("body").click({ position: { x: 5, y: 400 } }).catch(() => {});
      await sleep(1500);
    } catch (e) {
      log.error("建任务失败 " + name, e.message);
      state.files[name] = { ...(state.files[name] || {}), status: "error", error: String(e.message) };
      await saveState(state);
    }
  }
  await closeBrowser();
  return { plans, state };
}

// 轮询 ~/Downloads：文件出现、体积连续稳定、归档校验通过后移入 _manual
async function watchAndArchive(plans, state) {
  if (!APPLY) return [];
  const deadline = Date.now() + TIMEOUT_MIN * 60000;
  const pending = new Set(plans.filter((n) => state.files[n]?.status !== "error"));
  const lastSize = new Map();
  while (pending.size && Date.now() < deadline) {
    for (const name of [...pending]) {
      const fp = path.join(DOWNLOAD_DIR, name);
      let st = null;
      try { st = await fs.stat(fp); } catch {}
      if (!st || !st.isFile()) { await sleep(3000); continue; }
      const prev = lastSize.get(name) || { size: -1, since: 0 };
      if (st.size === prev.size) {
        if (Date.now() - prev.since >= 8000) {
          log.info("校验完整性：" + name + " (" + st.size + " bytes)");
          if (await archiveOk(fp)) {
            const dest = path.join(DEST_DIR, name);
            await fs.rename(fp, dest);
            state.files[name] = { ...state.files[name], status: "archived", archivedAt: new Date().toISOString(), bytes: st.size, dest };
            await saveState(state);
            pending.delete(name);
            log.ok("已归档：" + name);
          } else {
            log.warn("归档校验未通过，继续等待：" + name);
            lastSize.set(name, { size: st.size, since: Date.now() });
          }
        }
      } else {
        lastSize.set(name, { size: st.size, since: Date.now() });
        log.step("下载中", name, (st.size / 1048576).toFixed(1) + "MB");
      }
    }
    await sleep(4000);
  }
  if (pending.size) log.warn("超时未完成：" + [...pending].join(","));
  return [...pending];
}

const { plans, state } = await captureSchemes();
const leftover = await watchAndArchive(plans, state);
const done = plans.filter((n) => state.files[n]?.status === "archived").length;
log.info(`客户端驱动结束：归档 ${done}/${plans.length}` + (leftover.length ? "，未完成 " + leftover.length : ""));
