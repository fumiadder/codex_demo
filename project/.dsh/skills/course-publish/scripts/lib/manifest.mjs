import { readFileSync, writeFileSync, existsSync, mkdirSync, renameSync } from "node:fs";
import { paths } from "./paths.mjs";

// manifest 结构：
// { courseId, shares: { [url]: { lastChecked, chapters: [{name,fsid,size,mtime,transferred}] } },
//   chapters: { [normName]: { shareName, webTitle, index, status, confidence, manualOverride,
//     files: [{rel, sha256, kind}], mdPath, postId, postUrl, collection, publishedAt } } }
export function loadManifest(courseId) {
  const p = paths.manifest(courseId);
  if (!existsSync(p)) {
    return { courseId, shares: {}, chapters: {}, createdAt: new Date().toISOString() };
  }
  return JSON.parse(readFileSync(p, "utf8"));
}

export function saveManifest(courseId, m) {
  mkdirSync(paths.stateDir, { recursive: true });
  m.updatedAt = new Date().toISOString();
  const p = paths.manifest(courseId);
  // 原子写：先写临时文件再 rename，避免进程中断导致状态文件截断损坏
  const tmp = p + ".tmp-" + process.pid;
  writeFileSync(tmp, JSON.stringify(m, null, 2));
  renameSync(tmp, p);
  return p;
}

// 章节状态机
export const STATUS = [
  "discovered",      // 网盘分享中发现
  "transferred",     // 已转存到目标目录
  "downloaded",      // 文件已下载
  "merged",          // 已生成合并 MD
  "images-uploaded", // 图片已上图床
  "matched",         // 与网站目录完成高置信匹配（或人工确认）
  "published",       // 博客已发布（含合集）
  "blocked",         // 被闸门拦截，等待用户
  "skipped",         // 用户裁定跳过
];

export function setChapter(m, key, patch) {
  const prev = m.chapters[key] || { status: "discovered" };
  m.chapters[key] = { ...prev, ...patch };
  return m.chapters[key];
}
