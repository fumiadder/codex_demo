// 把 skill 安装到用户级目录（默认 ~/.dsh/skills/course-publish），并就地安装 npm 依赖使其自包含。
// 用法：node install-user.mjs [--target <dir>] [--no-install]
import { cpSync, existsSync, mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = path.resolve(HERE, ".."); // course-publish/
const args = process.argv.slice(2);
const ti = args.indexOf("--target");
const target = ti >= 0 ? args[ti + 1] : path.join(homedir(), ".dsh", "skills", "course-publish");
const skipInstall = args.includes("--no-install");

console.log("源 skill：", SKILL_DIR);
console.log("目标目录：", target);

// 1) 复制（排除 node_modules 与测试产物）
if (existsSync(target)) rmSync(target, { recursive: true, force: true });
mkdirSync(path.dirname(target), { recursive: true });
cpSync(SKILL_DIR, target, {
  recursive: true,
  filter: (src) => {
    const rel = path.relative(SKILL_DIR, src);
    if (!rel) return true;
    return !rel.split(path.sep)[0].match(/^(node_modules|test-output)$/);
  },
});

// 2) 写最小 package.json 并安装依赖（用户级 skill 自包含）
if (!skipInstall) {
  const pkg = JSON.parse(readFileSync(path.resolve(SKILL_DIR, "../../../package.json"), "utf8"));
  // 固定为源工作区实际安装的 Playwright 版本，避免用户级 npm 解析到新版本后找不到匹配浏览器缓存。
  let playwrightVersion = null;
  try {
    playwrightVersion = JSON.parse(readFileSync(path.resolve(SKILL_DIR, "../../../node_modules/playwright/package.json"), "utf8")).version;
  } catch {}
  const dependencies = { ...(pkg.dependencies || {}) };
  if (playwrightVersion) dependencies.playwright = playwrightVersion;
  writeFileSync(
    path.join(target, "package.json"),
    JSON.stringify(
      {
        name: "course-publish-skill",
        version: pkg.version || "0.1.0",
        private: true,
        type: "module",
        scripts: {
          selftest: "node scripts/xmlrpc.selftest.mjs",
          "test:names": "node --test scripts/names.test.mjs",
          doctor: "node scripts/doctor.mjs",
          run: "node scripts/run.mjs",
          subscribe: "node scripts/subscribe.mjs",
        },
        dependencies,
      },
      null,
      2,
    ) + "\n",
  );
  console.log("安装依赖到用户级 skill（可能需要一两分钟）……");
  execSync("npm install --omit=dev --no-audit --no-fund", { cwd: target, stdio: "inherit" });
}

// 3) 校验
const checks = ["SKILL.md", "scripts/run.mjs", "scripts/lib/paths.mjs", "scripts/cnblogs-review-audit.mjs", "references/runbook.md", "references/review-and-republish.md"];
for (const c of checks) {
  if (!existsSync(path.join(target, c))) throw new Error("安装校验失败，缺少 " + c);
}

// 4) 安装后内置自检（不触网，仅本地编解码与匹配）
if (!skipInstall) {
  console.log("运行安装后自检……");
  execSync("npm run selftest", { cwd: target, stdio: "inherit" });
  execSync("npm run test:names", { cwd: target, stdio: "inherit" });
}
console.log("\n安装完成：" + target);
console.log("数据目录按运行时 cwd 自动识别（含 .dsh/ 或 config/courses.yaml 的目录），也可用 COURSE_PUBLISH_ROOT 环境变量指定。");
