// 从持久化浏览器 profile 提取网盘 Cookie 到 Netscape 文件，供 curl 断点续传使用。
// 安全：Cookie 只写入项目 .pipeline/pan-cookies.txt（权限600），不打印到 stdout。
import { promises as fs } from "node:fs";
import { getContext, closeBrowser } from "/workspace/project/.dsh/skills/course-publish/scripts/lib/browser.mjs";

const ctx = await getContext({ headless: true });
try {
  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.goto("https://pan.baidu.com/disk/main", { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(6000);
  const cookies = await ctx.cookies([
    "https://pan.baidu.com",
    "https://d.pcs.baidu.com",
    "https://www.baidu.com",
  ]);
  if (!cookies.length) throw new Error("未读到任何 Cookie，登录态可能已失效");
  const lines = ["# Netscape HTTP Cookie File"];
  for (const c of cookies) {
    const flag = c.domain.startsWith(".") ? "TRUE" : "FALSE";
    const secure = c.secure ? "TRUE" : "FALSE";
    const expires = c.expires && c.expires > 0 ? Math.floor(c.expires) : 0;
    lines.push([c.domain, flag, c.path, secure, expires, c.name, c.value].join("\t"));
  }
  await fs.writeFile("/workspace/project/.pipeline/pan-cookies.txt", lines.join("\n") + "\n", { mode: 0o600 });
  console.log("COOKIES_OK count=" + cookies.length + " names=" + cookies.map((c) => c.name).join(","));
} finally {
  await closeBrowser();
}
