// 交互式登录：打开有头浏览器，等待用户完成百度网盘/课程站登录后回车结束
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { getContext, closeBrowser, assertBaiduLogin } from "./lib/browser.mjs";
import { log } from "./lib/log.mjs";

const rl = createInterface({ input: stdin, output: stdout });

async function main() {
  const ctx = await getContext({ headless: false });
  const page = ctx.pages()[0] || (await ctx.newPage());

  await page.goto("https://pan.baidu.com", { waitUntil: "domcontentloaded" });
  log.info("请在打开的浏览器窗口中扫码/登录百度网盘。");
  log.info("如需同时登录课程网站，可在该浏览器新标签页打开课程页一并登录。");

  // 轮询登录状态，最长等 10 分钟
  const deadline = Date.now() + 10 * 60 * 1000;
  let logged = false;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5000));
    if (await assertBaiduLogin(page).catch(() => false)) {
      logged = true;
      break;
    }
  }
  if (!logged) throw new Error("等待百度登录超时（10 分钟）");
  log.ok("百度网盘登录态已确认并保存到 browser-profile/");
  await rl.question("登录完成后按回车关闭浏览器……");
  await closeBrowser();
  rl.close();
}

main().catch((e) => {
  log.error(e.stack || String(e));
  process.exit(1);
});
