// 定时订阅包装：对全部（或指定）期次非交互复跑流水线，输出汇总报告。
// 用法：node subscribe.mjs [--course <id>] [--no-publish]
// 退出码：0 全部正常；10 有闸门待人工；1 致命错误
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { realpathSync } from "node:fs";
import { writeFileSync, mkdirSync } from "node:fs";
import { loadCourses } from "./lib/config.mjs";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ALL = "transfer,toc,match,download,merge,images,publish";
const NO_PUBLISH = "transfer,toc,match,download,merge,images";

export function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) out[argv[i].slice(2)] = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
  }
  return out;
}

export function stagesFor(noPublish) {
  return noPublish ? NO_PUBLISH : ALL;
}

export function selectCourses(cfg, wantedId) {
  const list = wantedId ? cfg.courses.filter((c) => c.id === wantedId) : cfg.courses;
  if (!list.length) throw new Error("没有匹配的 course: " + (wantedId || "*"));
  return list;
}

// 退出码归并：0 正常；10 闸门待确认；其余非零统一为 1。多期取最严重者（10 优先于 1，便于提醒先处理闸门）
export function worstExitCode(codes) {
  return codes.reduce((worst, code) => {
    const sev = code === 0 ? 0 : code === 10 ? 10 : 1;
    return Math.max(worst, sev);
  }, 0);
}

function isMainModule() {
  return realpathSync(new URL(import.meta.url)) === realpathSync(process.argv[1]);
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  const cfg = loadCourses();
  const courses = selectCourses(cfg, a.course);
  const stages = stagesFor(a["no-publish"] === true);
  const report = { at: new Date().toISOString(), courses: [] };
  const codes = [];

  for (const c of courses) {
    log.step("订阅检查", c.id, c.title || "");
    const r = spawnSync(
      process.execPath,
      [path.join(HERE, "run.mjs"), "--course", c.id, "--only", stages, "--headless", "true"],
      { encoding: "utf8", timeout: 30 * 60 * 1000 },
    );
    const code = r.status ?? 1;
    codes.push(code);
    report.courses.push({
      id: c.id,
      exitCode: code,
      needsAttention: code !== 0,
      tail: (r.stdout || "").split("\n").slice(-12).join("\n"),
      stderrTail: (r.stderr || "").split("\n").slice(-6).join("\n"),
    });
    log[code === 0 ? "ok" : "error"](c.id, "exit=", code);
  }

  mkdirSync(paths.logsDir(), { recursive: true });
  const file = path.join(paths.logsDir(), "subscribe-report-" + new Date().toISOString().slice(0, 10) + ".json");
  writeFileSync(file, JSON.stringify(report, null, 2));
  log.info("订阅报告：", file);
  const attention = report.courses.filter((c) => c.needsAttention);
  if (attention.length) log.error("需要人工介入的期次：", attention.map((c) => c.id).join(", "));
  process.exit(worstExitCode(codes));
}

if (isMainModule()) main();
