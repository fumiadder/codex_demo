import { readFileSync, existsSync, chmodSync } from "node:fs";
import { parse as parseYaml } from "yaml";
import { paths } from "./paths.mjs";

export function loadCourses() {
  if (!existsSync(paths.config)) {
    throw new Error("缺少配置文件 " + paths.config + "，请从 config/courses.example.yaml 复制并填写");
  }
  const raw = readFileSync(paths.config, "utf8");
  const cfg = parseYaml(raw) || {};
  if (!Array.isArray(cfg.courses) || cfg.courses.length === 0) {
    throw new Error("courses.yaml 中没有 courses 列表");
  }
  // 重复 id 会导致 manifest/产物目录互相覆盖
  const seen = new Set();
  for (const c of cfg.courses) {
    if (!c || !c.id) throw new Error("courses.yaml 存在缺少 id 的期次条目");
    if (seen.has(c.id)) throw new Error("courses.yaml 中 id 重复：" + c.id);
    seen.add(c.id);
  }
  return cfg;
}

const URL_RE = /^https?:\/\//;

// 单期配置结构校验：在启动期给出可操作的中文报错，而不是等到深层阶段
export function validateCourse(c) {
  const errs = [];
  if (!c || !c.baidu || typeof c.baidu.targetDir !== "string" || !c.baidu.targetDir.trim()) {
    errs.push("baidu.targetDir 缺失（网盘目标目录绝对路径）");
  }
  if (!c || !Array.isArray(c.baidu && c.baidu.shares)) {
    errs.push("baidu.shares 缺失或不是数组");
  } else {
    for (const [i, s] of c.baidu.shares.entries()) {
      if (!s || !s.url || !URL_RE.test(s.url)) {
        errs.push("baidu.shares[" + i + "].url 非法：" + JSON.stringify(s && s.url));
      }
    }
  }
  if (!c || !c.toc || !c.toc.url) errs.push("toc.url 缺失（课程网站目录页）");
  if (errs.length) {
    throw new Error("期次「" + ((c && c.id) || "?") + "」配置有问题：\n - " + errs.join("\n - "));
  }
  return true;
}

export function getCourse(id) {
  const cfg = loadCourses();
  const c = cfg.courses.find((x) => x.id === id);
  if (!c) {
    throw new Error("配置中找不到 course id: " + id + "；可选：" + cfg.courses.map((x) => x.id).join(", "));
  }
  validateCourse(c);
  return c;
}

// 极简 KEY=VALUE 解析（无引号/引号两种）
export function loadCnblogsSecrets() {
  if (!existsSync(paths.cnblogsEnv)) {
    throw new Error("缺少 " + paths.cnblogsEnv + "，请参照 secrets/README.md 创建");
  }
  try { chmodSync(paths.cnblogsEnv, 0o600); } catch {}
  const out = {};
  for (const line of readFileSync(paths.cnblogsEnv, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    let v = m[2];
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    out[m[1]] = v;
  }
  for (const k of ["CNBLOGS_BLOG_APP", "CNBLOGS_USERNAME", "CNBLOGS_PASSWORD"]) {
    if (!out[k]) throw new Error(paths.cnblogsEnv + " 缺少 " + k);
  }
  return out;
}
