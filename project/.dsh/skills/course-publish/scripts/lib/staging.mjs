// 预览闸门（人工审核后才允许发布）：
// 1) 图片只上传一次并把 相对路径->在线URL 持久化到 image-url-map（assets 文件名即内容哈希，天然幂等，重跑零重传）；
// 2) 生成独立 preview MD（本地引用全部替换为在线链接），源 00-*.md 保持不动；
// 3) 章节状态机 merged -> staged -> approved -> published，只有 staged 且无残留本地图的章节可被批准。
import { promises as fs } from "node:fs";
import path from "node:path";
import { paths } from "./paths.mjs";
import { log } from "./log.mjs";
import { findLocalImageRefs, rewriteImageRefs, dedupeImageRefs, MIME, verifyPublicUrl } from "./images-cnblogs.mjs";
import { MediaQuotaError, isQuotaError } from "./media-quota.mjs";

export const STAGE = { MERGED: "merged", STAGED: "staged", PARTIAL: "partial", APPROVED: "approved", PUBLISHED: "published" };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const mediaGapMs = () => Number(process.env.CNBLOGS_MEDIA_GAP_MS ?? 1000);

async function loadJson(file, fallback) {
  try { return JSON.parse(await fs.readFile(file, "utf8")); } catch { return fallback; }
}
async function saveJson(file, data) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, JSON.stringify(data, null, 2));
}

export const stagingFile = (course) => path.join(paths.stateDir, course + ".staging.json");
export const urlMapFile = (course) => path.join(paths.stateDir, course + ".image-url-map.json");
export const previewDir = (course) => path.join(paths.courseOutput(course), "previews");
export const previewPath = (course, slug) => path.join(previewDir(course), "00-" + slug + ".preview.md");

// 纯函数：状态机校验。只有 staged（图片全部上云、无残留）可批准。
export function canApprove(entry) {
  return Boolean(entry) && entry.status === STAGE.STAGED;
}
export function reduceApprove(entry) {
  if (!canApprove(entry)) throw new Error("章节未就绪（status=" + (entry && entry.status) + "），不能批准发布");
  return { ...entry, status: STAGE.APPROVED, approvedAt: new Date().toISOString() };
}

// 纯函数：根据残留本地引用判断暂存结果状态
export function stageStatus(localRefCount, failedCount) {
  if (localRefCount === 0) return STAGE.STAGED;
  return failedCount > 0 ? STAGE.PARTIAL : STAGE.STAGED;
}

// 单章暂存：上传未缓存图片 -> 写 preview -> 更新状态机。源 md 不修改。
export async function stageChapter({ client, quota = null, course, slug, title, mdPath }) {
  const mapFile = urlMapFile(course);
  const stgFile = stagingFile(course);
  const urlMap = await loadJson(mapFile, {});
  const staging = await loadJson(stgFile, { chapters: {} });
  const md = await fs.readFile(mdPath, "utf8");
  const dir = path.dirname(mdPath);
  const rels = [...new Set(findLocalImageRefs(md))];

  if (quota && rels.some((r) => !urlMap[r]) && !quota.canUpload()) {
    throw new MediaQuotaError("图片今日配额已用尽，暂存已暂停，配额恢复后续传：" + slug);
  }

  let cached = 0, uploadedNow = 0;
  const failed = [];
  for (const [idx, rel] of rels.entries()) {
    if (urlMap[rel]) { cached++; continue; }
    if (idx > 0) await sleep(mediaGapMs());
    let bits;
    try { bits = await fs.readFile(path.join(dir, rel)); }
    catch { failed.push({ rel, reason: "本地文件缺失" }); continue; }
    try {
      const ext = path.extname(rel).toLowerCase();
      const url = await client.newMediaObject({ name: path.basename(rel), type: MIME[ext] || "application/octet-stream", bits });
      const v = await verifyPublicUrl(url);
      if (!v.ok) { failed.push({ rel, reason: "公网校验失败：" + v.reason, url }); continue; }
      urlMap[rel] = url;
      await saveJson(mapFile, urlMap); // 逐张落盘，崩溃不丢、不重传
      uploadedNow++;
      if (quota) await quota.recordUpload({ course, slug, file: path.basename(rel), rel, bytes: bits.length, url });
    } catch (e) {
      if (isQuotaError(e)) {
        if (quota) await quota.markBlocked(e);
        throw new MediaQuotaError("图片配额/权限受限(403)，已暂停，恢复后续传：" + slug, e);
      }
      failed.push({ rel, reason: "newMediaObject 失败：" + e.message });
    }
  }

  const rewritten = rewriteImageRefs(md, new Map(Object.entries(urlMap)));
  const deduped = dedupeImageRefs(rewritten);
  const previewMd = deduped.md;
  const residual = findLocalImageRefs(previewMd);
  for (const rel of residual) if (!failed.some((f) => f.rel === rel)) failed.push({ rel, reason: "preview 仍残留本地引用" });
  const pvPath = previewPath(course, slug);
  await fs.mkdir(path.dirname(pvPath), { recursive: true });
  await fs.writeFile(pvPath, previewMd);

  const status = failed.length ? STAGE.PARTIAL : STAGE.STAGED;
  staging.chapters[slug] = {
    ...(staging.chapters[slug] || {}),
    slug, title, status, mdPath, previewPath: pvPath,
    imageCount: rels.length, cached, uploadedNow, failed, duplicateImagesRemoved: deduped.removed,
    stagedAt: new Date().toISOString(),
  };
  await saveJson(stgFile, staging);
  log.ok("暂存" + (failed.length ? "(部分)" : "") + " " + slug, "图片" + rels.length + "（新传" + uploadedNow + "/缓存" + cached + "）");
  return staging.chapters[slug];
}

export async function loadStaging(course) { return loadJson(stagingFile(course), { chapters: {} }); }
export async function saveStaging(course, staging) { return saveJson(stagingFile(course), staging); }
