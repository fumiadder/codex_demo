// Day45/Day46 全自动流水线（持久化版）：监控下载(断线重启) -> CRC校验 -> 解压 -> 重合并 -> 图片上云(stage)
// -> 审批 -> 发布 -> 关联合集45437「AI智能体六期」(尽力而为)。
// 用户已明确授权：下载完成后自动发布。图片上云若仍403则每20分钟重试，绝不发布含本地图的内容。
// 所有脚本/日志/状态存于 /workspace/project/.pipeline，不依赖 /tmp；进度落盘，重启安全。
import { promises as fs, existsSync } from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { extractArchive } from "/workspace/project/.dsh/skills/course-publish/scripts/lib/baidu-download.mjs";
import { findLocalImageRefs } from "/workspace/project/.dsh/skills/course-publish/scripts/lib/images-cnblogs.mjs";

const execFileP = promisify(execFile);
const ROOT = "/workspace/project";
const PIPE = ROOT + "/.pipeline";
const SCRIPTS = ROOT + "/.dsh/skills/course-publish/scripts";
const STATE = ROOT + "/state";
const LOG = PIPE + "/auto-pipeline.log";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const EXPECT = { day45: 739980294, day46: 213965757 };

async function log(...a) {
  const line = "[" + new Date().toISOString() + "] " + a.join(" ");
  console.log(line);
  await fs.appendFile(LOG, line + "\n").catch(() => {});
}
async function runNode(script, args, timeout = 1800000) {
  const { stdout, stderr } = await execFileP(process.execPath, [script, ...args], {
    cwd: SCRIPTS, timeout, maxBuffer: 32 * 1024 * 1024,
  });
  return (stdout + stderr).trim();
}
async function isResumeAlive(day) {
  try { await execFileP("pgrep", ["-f", "resume-day.sh " + day]); return true; }
  catch { return false; }
}

async function waitDownload(day) {
  const crcLog = `${PIPE}/${day}-crc.log`;
  let restarts = 0;
  for (;;) {
    if (existsSync(crcLog)) {
      const t = await fs.readFile(crcLog, "utf8");
      if (/No errors detected/.test(t)) { await log(day, "下载完成且CRC校验通过"); return; }
      throw new Error(day + " CRC校验失败，需人工处理: " + t.slice(-300));
    }
    if (!(await isResumeAlive(day))) {
      if (++restarts > 12) throw new Error(day + " 下载重启次数过多，挂起");
      await log(day, "下载进程不在，自动重启续传（第" + restarts + "次）");
      execFile(`${PIPE}/resume-day.sh`, [day], { detached: true, stdio: "ignore" }).unref();
    }
    const size = await fs.stat(`${ROOT}/downloads/ai-agent-6/${day}/_staging/${day}.zip`).then((s) => s.size).catch(() => 0);
    await log(day, "下载中 " + (size / 1048576).toFixed(0) + "MB / " + (EXPECT[day] / 1048576).toFixed(0) + "MB");
    await sleep(60000);
  }
}

async function processDay(day) {
  const chapterDir = `${ROOT}/downloads/ai-agent-6/${day}`;
  const zip = `${chapterDir}/_staging/${day}.zip`;
  const exDir = `${chapterDir}/_extracted/${day}`;
  // 幂等：已发布则只补合集，不重复发文
  const pub0 = JSON.parse(await fs.readFile(`${STATE}/ai-agent-6.publish-state.json`, "utf8"));
  if (pub0[day]?.published) { await log(day, "已发布过，跳过发布，仅补合集"); return assignOnly(day); }
  // 1) 解压（清掉可能不完整的旧解压目录）
  await fs.rm(exDir, { recursive: true, force: true });
  await extractArchive(zip, exDir);
  await log(day, "已解压到 " + exDir);
  // 2) progress 落盘后再重合并（避免 edition8 重下）
  const progFile = `${STATE}/ai-agent-6.edition8-progress.json`;
  const prog = JSON.parse(await fs.readFile(progFile, "utf8"));
  prog[day] = { ...(prog[day] || {}), downloaded: true, files: 1, warnings: [], gated: [], at: new Date().toISOString() };
  delete prog[day].blocked;
  await fs.writeFile(progFile, JSON.stringify(prog, null, 2));
  // 3) 重合并（PPT 自动转逐页图片）
  const mergeOut = await runNode(`${SCRIPTS}/edition8.mjs`, [`--course=ai-agent-6`, `--only=${day}`]);
  await log(day, "重合并完成: " + mergeOut.split("\n").slice(-3).join(" | "));
  // 4) 审计：合并稿必须有图片（PPT章），否则停止
  const md = await fs.readFile(`${ROOT}/output/ai-agent-6/00-${day}.md`, "utf8");
  const imgCount = findLocalImageRefs(md).length;
  if (imgCount === 0) throw new Error(day + " 合并后无图片，疑似PPT转换失败，停止自动发布");
  await log(day, "审计通过：本地图片 " + imgCount + " 张待上云");
  // 5) stage（图片上云），403/配额则每20分钟重试，最多36次
  let staged = false;
  for (let attempt = 1; attempt <= 36 && !staged; attempt++) {
    const out = await runNode(`${SCRIPTS}/stage.mjs`, [`--course=ai-agent-6`, `--only=${day}`]).catch((e) => "ERR " + e.message);
    const staging = JSON.parse(await fs.readFile(`${STATE}/ai-agent-6.staging.json`, "utf8"));
    const st = staging.chapters[day]?.status;
    await log(day, "stage 第" + attempt + "次 status=" + st + " " + out.split("\n").slice(-2).join(" | ").slice(0, 300));
    if (st === "staged") { staged = true; break; }
    await sleep(20 * 60 * 1000);
  }
  if (!staged) throw new Error(day + " 图片上云持续失败（图床403未恢复），保持未发布");
  // 6) 审批（用户已授权自动发布；preview 零本地引用才批准）
  const pv = await fs.readFile(`${ROOT}/output/ai-agent-6/previews/00-${day}.preview.md`, "utf8");
  const residual = findLocalImageRefs(pv);
  if (residual.length) throw new Error(day + " preview 仍含本地图，禁止发布");
  const staging = JSON.parse(await fs.readFile(`${STATE}/ai-agent-6.staging.json`, "utf8"));
  staging.chapters[day].status = "approved";
  staging.chapters[day].approvedAt = new Date().toISOString();
  staging.chapters[day].approvedBy = "user-auto-instruction";
  await fs.writeFile(`${STATE}/ai-agent-6.staging.json`, JSON.stringify(staging, null, 2));
  // 7) 发布（最多3次，失败保持approved）
  let published = false;
  for (let attempt = 1; attempt <= 3 && !published; attempt++) {
    const out = await runNode(`${SCRIPTS}/publish-approved.mjs`, [`--course=ai-agent-6`, `--only=${day}`]).catch((e) => "ERR " + e.message);
    await log(day, "publish 第" + attempt + "次: " + out.split("\n").slice(-3).join(" | ").slice(0, 300));
    const pub = JSON.parse(await fs.readFile(`${STATE}/ai-agent-6.publish-state.json`, "utf8"));
    if (pub[day]?.published) { published = true; await log(day, "已发布 " + pub[day].postUrl); }
    else await sleep(5 * 60 * 1000);
  }
  if (!published) throw new Error(day + " 发布失败，保持approved待人工检查");
  await assignOnly(day);
}

async function assignOnly(day) {
  // 8) 关联合集（尽力而为，失败不阻塞，后续人工补齐排序）
  try {
    const { getContext, closeBrowser } = await import(`${SCRIPTS}/lib/browser.mjs`);
    const { assignCollection } = await import(`${SCRIPTS}/lib/cnblogs-ui.mjs`);
    const pub = JSON.parse(await fs.readFile(`${STATE}/ai-agent-6.publish-state.json`, "utf8"));
    const ctx = await getContext({ headless: true });
    const page = ctx.pages()[0] || (await ctx.newPage());
    const r = await assignCollection({ page, postUrl: pub[day].postUrl, collectionName: "AI智能体六期", courseId: "ai-agent-6" });
    await log(day, "合集归集 verified=" + r.verified);
    await closeBrowser();
  } catch (e) {
    await log(day, "合集归集失败（文章已发布，后续人工补）: " + e.message);
  }
}

await log("=== 自动流水线启动 ===");
const results = {};
for (const day of ["day46", "day45"]) {
  try {
    await waitDownload(day);
    await processDay(day);
    results[day] = "PUBLISHED";
  } catch (e) {
    results[day] = "BLOCKED: " + e.message;
    await log(day, "挂起: " + e.message);
  }
}
await log("=== PIPELINE_DONE " + JSON.stringify(results) + " ===");
