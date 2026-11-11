// 图片上博客园图床并回填 MD，逐张 200 校验；newMediaObject 失败则抛错转 UI 通道
import { promises as fs } from "node:fs";
import path from "node:path";
import { log } from "./log.mjs";
import { MediaQuotaError, isQuotaError } from "./media-quota.mjs";

export const MIME = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".bmp": "image/bmp",
};

export class MediaApiError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = "MediaApiError";
    this.cause = cause;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 单图上传间隔，规避高频触发图床账号级限流（566张/7分钟曾导致 403）；可用环境变量调 0（测试）
const mediaGapMs = () => Number(process.env.CNBLOGS_MEDIA_GAP_MS ?? 1000);

// 三类本地图片语法（行内含可选标题/尖括号、引用式定义、HTML img）
const RE_INLINE = /!\[[^\]]*\]\(\s*<?(?:\.\/)?(assets\/[^)\s>]+)>?(?:\s+[^)]*)?\)/g;
const RE_REFDEF = /^\s*\[[^\]]+\]:\s*<?(?:\.\/)?(assets\/[^\s>]+)>?/gm;
const RE_IMG = /<img\b[^>]*?\bsrc=["']?(?:\.\/)?(assets\/[^"'\s>]+)/gi;

const RE_ANY_INLINE = /!\[[^\]]*\]\(\s*<?(?:\.\/)?((?:https?:\/\/|assets\/)[^)\s>]+)>?(?:\s+[^)]*)?\)/g;
const RE_ANY_IMG = /<img\b[^>]*?\bsrc=["']?(?:\.\/)?((?:https?:\/\/|assets\/)[^"'\s>]+)[^>]*>/gi;

// 仅处理行内代码之外的文本，避免把语法示例当成真实图片删除。
function mapOutsideInlineCode(line, fn) {
  let out = "", plainStart = 0, i = 0;
  while (i < line.length) {
    if (line[i] !== "`") { i++; continue; }
    out += fn(line.slice(plainStart, i));
    let n = 1;
    while (line[i + n] === "`") n++;
    const marker = "`".repeat(n);
    const close = line.indexOf(marker, i + n);
    if (close < 0) return out + line.slice(i); // 未闭合反引号：保守保留余下行
    out += line.slice(i, close + n);
    i = close + n;
    plainStart = i;
  }
  return out + fn(line.slice(plainStart));
}

// 同一文章内同一 URL/内容哈希图片只保留第一次出现；不改动其他正文。
// 覆盖普通 Markdown 与 HTML img；围栏代码、行内代码中的示例原样保留。
export function dedupeImageRefs(md) {
  const seen = new Set();
  let removed = 0;
  let fence = null;
  const transform = (text) => {
    const keepFirst = (whole, rawUrl) => {
      const key = String(rawUrl).trim().replace(/&amp;/g, "&");
      if (!seen.has(key)) { seen.add(key); return whole; }
      removed++;
      return "";
    };
    return text.replace(RE_ANY_INLINE, keepFirst).replace(RE_ANY_IMG, keepFirst);
  };
  const lines = String(md || "").split("\n").map((line) => {
    const t = line.trimStart();
    const marker = t.startsWith("~~~") ? "~~~" : t.startsWith("```") ? "```" : null;
    if (marker) {
      if (fence === marker) fence = null;
      else if (!fence) fence = marker;
      return line;
    }
    return fence ? line : mapOutsideInlineCode(line, transform);
  });
  return { md: lines.join("\n"), removed };
}

// 提取 MD 中所有指向本地 assets/ 的图片引用（去重），覆盖非标准语法防止漏网裂图。
export function findLocalImageRefs(md) {
  const refs = new Set();
  for (const m of md.matchAll(RE_INLINE)) refs.add(m[1]);
  for (const m of md.matchAll(RE_REFDEF)) refs.add(m[1]);
  for (const m of md.matchAll(RE_IMG)) refs.add(m[1]);
  return [...refs];
}

// 按 urlMap 精确回填，保留 alt 与可选标题；返回新 MD。
export function rewriteImageRefs(md, urlMap) {
  // 先替换更长的 "./rel"，再替换裸 "rel"，避免残留前导 ./ （split/join 为字面替换，无需正则转义）
  const applyMap = (whole, rel) => {
    const url = urlMap.get(rel);
    if (!url) return whole;
    return whole.split("./" + rel).join(url).split(rel).join(url);
  };
  let out = md
    .replace(RE_INLINE, applyMap)
    .replace(RE_REFDEF, applyMap)
    .replace(RE_IMG, applyMap);
  // 兜底：裸路径（上面已覆盖常规语法，再保险一次，按字面量替换）
  for (const [rel, url] of urlMap) out = out.split(rel).join(url);
  return out;
}

export async function verifyPublicUrl(url) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(url, { signal: ctrl.signal, redirect: "follow" });
    const ct = r.headers.get("content-type") || "";
    if (!r.ok) return { ok: false, reason: "HTTP " + r.status };
    if (!ct.startsWith("image/")) return { ok: false, reason: "content-type=" + ct };
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: String(e.message || e) };
  } finally {
    clearTimeout(t);
  }
}

// mdPath: 合并后的 MD；assets 在其同级 assets/
// quota: 可选 QuotaTracker，到每日 500 张上限即抛 MediaQuotaError 干净暂停（次日自动续传）
// 返回 { mdPath, uploaded: [{rel,url,verified}], failed: [] }
export async function uploadChapterImages({ client, mdPath, quota = null }) {
  const md = await fs.readFile(mdPath, "utf8");
  const dir = path.dirname(mdPath);
  const rels = [...new Set(findLocalImageRefs(md))];
  log.info("待上传图片", rels.length, "张：", path.basename(mdPath));
  if (quota && !quota.canUpload()) {
    throw new MediaQuotaError("博客园图片今日配额已用尽（免费 500 张/天），明日自动重置后续传：" + path.basename(mdPath));
  }

  const urlMap = new Map();
  const uploaded = [];
  const failed = [];
  let mediaUnsupported = false;

  for (const [idx, rel] of rels.entries()) {
    if (idx > 0) await sleep(mediaGapMs());
    const abs = path.join(dir, rel);
    const ext = path.extname(rel).toLowerCase();
    let bits;
    try {
      bits = await fs.readFile(abs);
    } catch {
      failed.push({ rel, reason: "本地文件缺失" });
      continue;
    }
    let url = null;
    try {
      const res = await client.newMediaObject({ name: path.basename(rel), type: MIME[ext] || "application/octet-stream", bits });
      url = res;
    } catch (e) {
      if (isQuotaError(e)) {
        if (quota) await quota.markBlocked(e);
        throw new MediaQuotaError("博客园图片每日配额/权限受限（403，免费 500 张/天），已暂停，待配额恢复后续传：" + path.basename(mdPath), e);
      }
      if (/fault|401|403|不支持|disabled|forbidden/i.test(String(e.message))) mediaUnsupported = true;
      failed.push({ rel, reason: "newMediaObject 失败：" + e.message });
      continue;
    }
    if (!url) {
      failed.push({ rel, reason: "接口未返回 url" });
      continue;
    }
    const v = await verifyPublicUrl(url);
    if (!v.ok) {
      await new Promise((r) => setTimeout(r, 1500));
      const v2 = await verifyPublicUrl(url);
      if (!v2.ok) {
        failed.push({ rel, reason: "公网校验失败：" + v2.reason, url });
        continue;
      }
    }
    urlMap.set(rel, url);
    uploaded.push({ rel, url, verified: true });
    if (quota) await quota.recordUpload();
    log.ok("图片已上传并校验：", rel, "→", url.slice(0, 72));
  }

  if (mediaUnsupported) {
    throw new MediaApiError("博客园 newMediaObject 不可用，需要切换网页编辑器上传通道", failed);
  }

  // 回填，并强制兜底：回填后不得残留任何本地 assets/ 引用，否则计入 failed（章节会被 blocked，绝不发裂图）
  const out = rewriteImageRefs(md, urlMap);
  for (const rel of findLocalImageRefs(out)) {
    if (!urlMap.has(rel) && !failed.some((f) => f.rel === rel)) {
      failed.push({ rel, reason: "仍有本地图片引用未上云（上传失败或语法未识别）" });
    }
  }
  await fs.writeFile(mdPath, out);
  return { mdPath, uploaded, failed };
}
