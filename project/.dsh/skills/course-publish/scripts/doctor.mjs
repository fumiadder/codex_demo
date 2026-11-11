// 环境体检：依赖、浏览器、配置、凭证、目录权限。不发起网络写操作。
// 用法：node doctor.mjs
import { existsSync, statSync, mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { paths } from "./lib/paths.mjs";
import { loadCourses, validateCourse } from "./lib/config.mjs";

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
}

// 1) 关键依赖可导入
for (const [mod, pkg] of [["playwright", "playwright"], ["mammoth", "mammoth"], ["turndown", "turndown"], ["yaml", "yaml"], ["pdfjs-dist", "pdfjs-dist/legacy/build/pdf.mjs"]]) {
  try {
    await import(pkg);
    check("依赖 " + mod, true);
  } catch (e) {
    check("依赖 " + mod, false, e.message);
  }
}

// 2) 浏览器可执行
try {
  const exe = chromium.executablePath();
  check("Playwright Chromium", existsSync(exe), exe);
} catch (e) {
  check("Playwright Chromium", false, e.message);
}

// 3) 系统工具
for (const bin of ["/usr/bin/unzip", "/usr/bin/textutil"]) {
  check("系统工具 " + bin, existsSync(bin));
}

// 4) 数据目录（缺失则自动创建）
for (const [label, p] of [
  ["state", paths.stateDir],
  ["downloads", paths.downloadsDir],
  ["output", paths.outputDir],
  ["logs", paths.logsDir()],
  ["browser-profile", paths.browserProfile],
  ["config", path.dirname(paths.config)],
  ["secrets", path.dirname(paths.cnblogsEnv)],
]) {
  try {
    if (!existsSync(p)) mkdirSync(p, { recursive: true });
    check("目录 " + label, statSync(p).isDirectory(), p);
  } catch (e) {
    check("目录 " + label, false, e.message);
  }
}

// 5) 配置（含重复 id 与每期 schema 校验）
try {
  const cfg = loadCourses();
  const bad = [];
  for (const c of cfg.courses) {
    try { validateCourse(c); } catch (e) { bad.push(e.message); }
  }
  check("courses.yaml 结构", bad.length === 0, bad.length ? bad.join(" | ") : cfg.courses.length + " 期全部合法");
} catch (e) {
  check("courses.yaml", false, e.message);
}

// 6) 凭证
if (existsSync(paths.cnblogsEnv)) {
  const mode = (statSync(paths.cnblogsEnv).mode & 0o777).toString(8);
  check("secrets/cnblogs.env 存在", true, "权限 " + mode + (mode === "600" ? " ✓" : "（建议 600，运行时会自动收紧）"));
} else {
  check("secrets/cnblogs.env", false, "未创建（见 secrets/README.md）");
}

// 7) 登录态
check("browser-profile 登录态", existsSync(paths.browserProfile) && statSync(paths.browserProfile).size >= 0,
  existsSync(paths.browserProfile) ? "目录存在，登录有效性需 npm run baidu:login 实测" : "尚未创建");

let fail = 0;
for (const r of results) {
  console.log((r.ok ? "✓ " : "✗ ") + r.name + (r.detail ? " — " + r.detail : ""));
  if (!r.ok) fail++;
}
console.log("\n体检结果：" + (results.length - fail) + "/" + results.length + " 通过");
process.exit(fail ? 2 : 0);
