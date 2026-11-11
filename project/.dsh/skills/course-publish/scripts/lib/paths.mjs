// 数据根目录解析，同时支持两种安装布局：
//  1) 项目级 <workspace>/.dsh/skills/course-publish（数据在工作区）
//  2) 用户级 ~/.dsh/skills/course-publish（数据落在当前工作目录，可用环境变量覆盖）
// 优先级：COURSE_PUBLISH_ROOT 环境变量 > 当前工作目录（含 .dsh 或 config 标志）> skill 相对推导
import { dirname, resolve, join } from "node:path";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// scripts/lib -> scripts -> course-publish -> skills -> .dsh -> 项目根
const SKILL_PROJECT_ROOT = resolve(HERE, "../../../../..");

function detectRoot() {
  if (process.env.COURSE_PUBLISH_ROOT) return resolve(process.env.COURSE_PUBLISH_ROOT);
  const cwd = process.cwd();
  if (existsSync(join(cwd, ".dsh")) || existsSync(join(cwd, "config/courses.yaml"))) {
    return cwd;
  }
  return SKILL_PROJECT_ROOT;
}

export const WORKSPACE_ROOT = detectRoot();

export const paths = {
  root: WORKSPACE_ROOT,
  installLayout: existsSync(join(SKILL_PROJECT_ROOT, ".dsh", "skills", "course-publish")) ? "project" : "user",
  config: join(WORKSPACE_ROOT, "config/courses.yaml"),
  cnblogsEnv: join(WORKSPACE_ROOT, "secrets/cnblogs.env"),
  stateDir: join(WORKSPACE_ROOT, "state"),
  downloadsDir: join(WORKSPACE_ROOT, "downloads"),
  outputDir: join(WORKSPACE_ROOT, "output"),
  logsDir: () => join(WORKSPACE_ROOT, "logs"),
  browserProfile: join(WORKSPACE_ROOT, "browser-profile"),
  manifest: (courseId) => join(WORKSPACE_ROOT, "state", `${courseId}.manifest.json`),
  // 用户级安装时，node_modules 依赖从两个候选位置解析
  depsRoot: SKILL_PROJECT_ROOT,
  courseDownloads: (courseId) => join(WORKSPACE_ROOT, "downloads", courseId),
  courseOutput: (courseId) => join(WORKSPACE_ROOT, "output", courseId),
  logFile: (name) => join(WORKSPACE_ROOT, "logs", `${name}.log`),
};
