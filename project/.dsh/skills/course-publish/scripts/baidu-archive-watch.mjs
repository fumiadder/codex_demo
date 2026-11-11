// 归档守护：轮询 ~/Downloads，等百度客户端把 .baiduyun.p.downloading 写完为最终文件，
// 体积稳定后做完整性校验（zip:unzip -t / rar:lsar），通过则移入 _manual/。
import { promises as fs } from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
const DOWNLOAD_DIR = path.join(os.homedir(), "Downloads");
const DEST = "/Users/fumi/Documents/DeepSeek_harness/downloads/ai-agent-8/_manual";
const FILES = [
  "day07-产品经理之PRD-需求评审-思维图-流程图-figma安装.rar",
  "day10-小游戏上线-提示词工程.zip",
  "day16-Codex开发微信小程序端-前后端联调-根据figma调整布局.zip",
  "day17-微信小程序-上线.zip",
  "day21.zip",
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const execP = (c, a) => new Promise((res) => execFile(c, a, (e, o, se) => res(!e)));
async function ok(file) {
  if (file.endsWith(".rar")) return execP("lsar", [file]);
  return execP("unzip", ["-t", file]);
}
const stable = new Map();
const done = new Set();
const deadline = Date.now() + 120 * 60000;
console.log("归档守护启动，监控", FILES.length, "个文件");
while (done.size < FILES.length && Date.now() < deadline) {
  for (const name of FILES) {
    if (done.has(name)) continue;
    const final = path.join(DOWNLOAD_DIR, name);
    const tmp = path.join(DOWNLOAD_DIR, name + ".baiduyun.p.downloading");
    let st = null; try { st = await fs.stat(final); } catch {}
    if (!st || !st.isFile()) { if (await fs.stat(tmp).catch(() => null)) console.log("下载中…", name); await sleep(2000); continue; }
    const prev = stable.get(name);
    if (!prev || prev.size !== st.size) { stable.set(name, { size: st.size, since: Date.now() }); await sleep(2000); continue; }
    if (Date.now() - prev.since < 8000) { await sleep(2000); continue; }
    console.log("校验", name, st.size);
    if (!(await ok(final))) { console.log("校验未通过，重置观察", name); stable.delete(name); await sleep(5000); continue; }
    await fs.rename(final, path.join(DEST, name));
    done.add(name);
    console.log("✓ 已归档", name, "(" + done.size + "/" + FILES.length + ")");
  }
  await sleep(3000);
}
console.log(done.size === FILES.length ? "全部归档完成" : "超时，未完成:" + FILES.filter((f) => !done.has(f)).join(","));
