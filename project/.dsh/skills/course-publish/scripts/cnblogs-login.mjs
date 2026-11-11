// 交互式登录博客园网页端（合集操作需要 i.cnblogs.com 登录态）
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { getContext, closeBrowser } from "./lib/browser.mjs";
import { assertCnblogsLogin } from "./lib/cnblogs-ui.mjs";
import { log } from "./lib/log.mjs";

const rl = createInterface({ input: stdin, output: stdout });

async function main() {
  const ctx = await getContext({ headless: false });
  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.goto("https://i.cnblogs.com/login", { waitUntil: "domcontentloaded" });
  log.info("请在浏览器中登录博客园（账号密码或扫码），登录后脚本会自动检测……");
  const deadline = Date.now() + 10 * 60 * 1000;
  let ok = false;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5000));
    try {
      await assertCnblogsLogin(page);
      ok = true;
      break;
    } catch {}
  }
  if (!ok) throw new Error("等待博客园登录超时（10 分钟）");
  log.ok("博客园网页端登录态已确认并保存到 browser-profile/");
  await rl.question("按回车关闭浏览器……");
  await closeBrowser();
  rl.close();
}
main().catch((e) => { log.error(e.stack || String(e)); process.exit(1); });
