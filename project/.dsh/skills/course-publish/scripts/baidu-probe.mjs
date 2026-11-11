// 探针：对真实分享链接抓取 XHR/DOM 快照，校准选择器与接口。用法：
// node baidu-probe.mjs --url "https://pan.baidu.com/s/xxx?pwd=abcd" [--pwd abcd]
import { writeFileSync } from "node:fs";
import path from "node:path";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { BaiduPan } from "./lib/baidu.mjs";

function args() {
  const a = process.argv.slice(2);
  const out = {};
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith("--")) out[a[i].slice(2)] = a[i + 1] && !a[i + 1].startsWith("--") ? a[++i] : true;
  }
  return out;
}

async function main() {
  const a = args();
  if (!a.url) throw new Error("需要 --url");
  const ctx = await getContext({ headless: false });
  try {
    const page = ctx.pages()[0] || (await ctx.newPage());
    const pan = new BaiduPan(page);
    pan.installCapture();

    await pan.ensureLogin().catch((e) => log.warn("登录检查：", e.message));
    await pan.openShare({ url: a.url, password: a.pwd }).catch((e) => log.warn("openShare:", e.message));
    await new Promise((r) => setTimeout(r, 4000));

    const dom = await page.evaluate(() => {
      const q = (s) => document.querySelectorAll(s).length;
      const sample = (s) => [...document.querySelectorAll(s)].slice(0, 5).map((n) => n.innerText.trim().slice(0, 120));
      const container = document.querySelector(".share-list, .file-list, #share-novip, main, .app");
      return {
        url: location.href,
        title: document.title,
        counts: {
          ".file-item": q(".file-item"),
          ".list-item": q(".share-list .list-item"),
          "[data-id]": q("[data-id]"),
          "tr.file-item": q("tr.file-item"),
        },
        rowsText: sample(".file-item, .share-list .list-item, tr.file-item"),
        buttons: [...document.querySelectorAll("button, a.button, [role=button]")].map((b) => b.textContent.trim()).filter(Boolean).slice(0, 40),
        containerHtmlHead: container ? container.outerHTML.slice(0, 2500) : null,
      };
    });

    const dump = {
      time: new Date().toISOString(),
      dom,
      captured: pan._captured.map((c) => ({
        url: c.url,
        json: (() => {
          const s = JSON.stringify(c.json);
          return JSON.parse(s.length > 8000 ? s.slice(0, 8000) : s);
        })(),
      })),
    };
    const out = path.join(paths.logsDir(), "baidu-probe-" + Date.now() + ".json");
    writeFileSync(out, JSON.stringify(dump, null, 2));
    log.ok("探针快照已写入", out);
    log.info("顶层条目数(DOM/XHR)：", dom.rowsText.length, "/", pan.captured(/share\/list/).length);
  } finally {
    await closeBrowser();
  }
}
main().catch((e) => { log.error(e.stack || String(e)); process.exit(1); });
