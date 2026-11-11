// 百度分享页直连下载：不依赖转存到自己网盘。
// 流程：调用方用 BaiduPan.openShare 拿到 shareCtx（shareid/uk/seid/entries）→ 选章 →
//   单文件直接下载；文件夹（及未来的多文件选择）百度会打包 zip，下载后用 extractArchive 解压。
// 安全：所有本地写入经 baidu-download 的 resolveInside 限定在 destDir 内；
//   条目名含绝对路径或 .. 段时 planShareDownloads 直接 fail-closed，不产生任何写入。
// 网络：downloadOne/extract 可通过 deps 注入（测试用 mock）；默认实现走 context.request + pan.apiPost。
// 注意：/share/download 的 sign/timestamp 与文件夹打包任务细节需用 baidu-probe 真机校准（见 defaultDownloadOne）。
import { promises as fs } from "node:fs";
import path from "node:path";
import { classify, unsafeEntryNames, resolveInside, extractArchive } from "./baidu-download.mjs";
import { sha256File } from "./docs-md.mjs";
import { log } from "./log.mjs";

// ---------- 1. 分享下载 URL/查询串参数解析（纯函数，防御式，无网络） ----------
const SHARE_DL_KEYS = ["shareid", "uk", "seid", "sign", "timestamp", "bdstoken", "from", "pwd"];

function emptyParams() {
  return Object.fromEntries(SHARE_DL_KEYS.map((k) => [k, null]));
}

// 接受完整 URL、协议相对 URL、裸查询串（带不带前导 ? 均可）；任何畸形输入返回全 null 而不抛错。
// 数值类参数（shareid/uk 可能是超过安全整数的大数字或负数）一律保留字符串，避免精度丢失。
export function extractShareDlParams(input) {
  if (typeof input !== "string") return emptyParams();
  const s = input.trim();
  if (!s) return emptyParams();
  let query = "";
  const qIdx = s.indexOf("?");
  if (qIdx >= 0) {
    query = s.slice(qIdx + 1);
  } else if (!/[\s/]/.test(s) && s.includes("=")) {
    // 裸查询串，如 shareid=1&uk=-2
    query = s;
  } else {
    return emptyParams();
  }
  const hashIdx = query.indexOf("#");
  if (hashIdx >= 0) query = query.slice(0, hashIdx);
  let sp;
  try {
    sp = new URLSearchParams(query);
  } catch {
    return emptyParams();
  }
  const out = emptyParams();
  for (const k of SHARE_DL_KEYS) {
    const v = sp.get(k);
    if (v !== null && v !== "") out[k] = v;
  }
  return out;
}

// ---------- 2. 下载计划（纯函数） ----------
function assertSafeEntryName(rel) {
  if (typeof rel !== "string" || !rel) {
    throw new Error("分享条目名为空或非字符串，拒绝计划下载");
  }
  if (rel.includes("\0")) {
    throw new Error("分享条目名含 NUL 字符，拒绝: " + rel);
  }
  // 复用已导出的 zip-slip 判定：绝对路径（/ 或 \ 开头）与任何 .. 段一律拒绝
  const unsafe = unsafeEntryNames([rel]);
  if (unsafe.length) {
    throw new Error("分享条目名含绝对路径或 .. 越界段，拒绝计划下载: " + rel);
  }
  return rel;
}

// 输入 openShare 风格条目数组（{name,fsid,isdir,size,path?}），可选 children/list 嵌套（防御式展平）。
// 输出每项 {name, fsid, isdir, needFolderZip, safeName}；文件夹保留并标记 needFolderZip=true。
export function planShareDownloads(entries) {
  if (!Array.isArray(entries)) {
    throw new Error("planShareDownloads 输入必须是分享条目数组");
  }
  const planned = [];
  const walk = (list, prefix) => {
    for (const raw of list) {
      if (!raw || typeof raw !== "object") {
        throw new Error("分享条目不是对象，拒绝计划下载");
      }
      const isdir = raw.isdir === true || raw.isdir === 1;
      const base = raw.name == null ? "" : String(raw.name);
      const rel = prefix ? prefix + "/" + base : base;
      const safeName = assertSafeEntryName(rel);
      planned.push({
        name: rel,
        fsid: raw.fsid ?? null,
        isdir,
        needFolderZip: isdir, // 百度对文件夹/多文件选择下载统一打包 zip
        safeName,
      });
      const kids = raw.children || raw.list;
      if (Array.isArray(kids) && kids.length) walk(kids, rel);
    }
  };
  walk(entries, "");
  return planned;
}

// ---------- 3. 下载缓冲体校验（空包/HTML 风控页） ----------
function guardDownloadBuffer(buf, name) {
  if (!(buf instanceof Uint8Array)) {
    throw new Error("下载结果不是二进制缓冲: " + name);
  }
  if (!buf.length) {
    throw new Error("下载内容为空: " + name);
  }
  const head = buf.slice(0, 200).toString("utf8");
  if (/<html|<!DOCTYPE/i.test(head) && !/\.(html?)$/i.test(name)) {
    throw new Error("下载返回疑似 HTML 风控页: " + head.slice(0, 80).replace(/\s+/g, " "));
  }
}

// ---------- 4. 分享下载参数归集：shareCtx + 捕获 XHR + 页面 yunData ----------
function collectShareDlParams(pan, shareCtx) {
  const fromUrl = extractShareDlParams(shareCtx && shareCtx.url ? shareCtx.url : "");
  const params = { ...fromUrl };
  if (shareCtx) {
    if (!params.shareid && shareCtx.shareid != null) params.shareid = String(shareCtx.shareid);
    if (!params.uk && shareCtx.uk != null) params.uk = String(shareCtx.uk);
    if (!params.seid && shareCtx.seid != null) params.seid = String(shareCtx.seid);
  }
  const hits = pan && typeof pan.captured === "function"
    ? pan.captured(/share\/(tplview|list|wxlist|setting|download)/)
    : [];
  for (const h of hits) {
    const p = extractShareDlParams(h && h.url ? h.url : "");
    for (const k of ["sign", "timestamp", "bdstoken", "seid"]) {
      if (!params[k] && p[k]) params[k] = p[k];
    }
  }
  return params;
}

async function fillFromYunData(page, params) {
  if (!page || typeof page.evaluate !== "function") return;
  const got = await page.evaluate(() => {
    const out = { sign: null, timestamp: null, bdstoken: null };
    try {
      const y = window.yunData || {};
      out.sign = y.SIGN || y.sign || null;
      out.timestamp = y.TIMESTAMP || y.timestamp || null;
      out.bdstoken = y.MYBDSTOKEN || null;
    } catch {}
    return out;
  }).catch(() => null);
  if (!got) return;
  if (!params.sign && got.sign) params.sign = String(got.sign);
  if (!params.timestamp && got.timestamp) params.timestamp = String(got.timestamp);
  if (!params.bdstoken && got.bdstoken) params.bdstoken = String(got.bdstoken);
}

// ---------- 5. 默认网络实现（context.request + BaiduPan 凭证模式） ----------
// [真机校准点] 以下接口路径/入参/响应结构基于百度网盘历史分享下载协议，离线无法验证：
//   a) POST /share/download 是否仍为现行路径，channel/clienttype/web 等固定参数是否齐全；
//   b) 文件夹/多文件场景可能不直接回 dlink，而是 errno 表示“正在打包”+任务轮询（/share/download 重试或 task 接口）；
//   c) 响应 dlink 可能是字符串也可能是对象（{dlink:{dlink,url,...}}），此处两种都兼容；
//   d) sign/timestamp 的页面全局名（yunData.SIGN/TIMESTAMP）可能变化，探针 logs/baidu-probe-*.json 可核对。
async function defaultDownloadOne(arg) {
  const { context, page, pan, shareCtx, plan } = arg;
  if (!context || !context.request || typeof context.request.get !== "function") {
    throw new Error("默认分享下载器需要 context.request（Playwright APIRequestContext）");
  }
  if (!pan || typeof pan.apiPost !== "function") {
    throw new Error("默认分享下载器需要 BaiduPan 实例（apiPost 凭证请求）");
  }
  const params = collectShareDlParams(pan, shareCtx);
  await fillFromYunData(page, params);
  if (!params.shareid || params.uk == null) {
    throw new Error("缺少 shareid/uk，无法构造分享下载请求，请先用 baidu-probe 校准");
  }
  const api =
    "https://pan.baidu.com/share/download?shareid=" + encodeURIComponent(params.shareid) +
    "&uk=" + encodeURIComponent(params.uk) +
    "&seid=" + encodeURIComponent(params.seid || "") +
    "&sign=" + encodeURIComponent(params.sign || "") +
    "&timestamp=" + encodeURIComponent(params.timestamp || "") +
    "&bdstoken=" + encodeURIComponent(params.bdstoken || "") +
    "&channel=chunlei&clienttype=0&web=1";
  const body = "fsidlist=" + encodeURIComponent(JSON.stringify([String(plan.fsid)])) + "&type=n";
  const j = await pan.apiPost(api, body);
  if (!j || j.errno !== 0) {
    throw new Error("分享下载接口失败 errno=" + (j && j.errno) + " " + JSON.stringify(j || {}).slice(0, 200));
  }
  const dlink = (j.dlink && (j.dlink.dlink || j.dlink.url)) || j.dlink || j.url;
  if (typeof dlink !== "string" || !/^https?:\/\//.test(dlink)) {
    throw new Error("分享下载接口未返回可用 dlink: " + JSON.stringify(j).slice(0, 200));
  }
  const resp = await context.request.get(dlink + (dlink.includes("?") ? "&" : "?") + "origin=dl", {
    headers: {
      "User-Agent": "netdisk; pan.baidu.com",
      Referer: (shareCtx && shareCtx.url) || "https://pan.baidu.com/",
    },
    maxRedirects: 5,
    timeout: 300000,
  });
  if (!resp.ok()) throw new Error("分享 dlink HTTP " + resp.status());
  const buf = await resp.body();
  guardDownloadBuffer(buf, plan.safeName);
  return buf;
}

// ---------- 6. 本地编排辅助 ----------
function flatZipName(safeName) {
  // 落地 zip 名扁平化（分隔符 → _），确保暂存路径只有一段，无法构造穿越
  return safeName.replace(/[\\/]+/g, "_") + ".zip";
}

async function walkRelative(dir, prefix, out) {
  let ents;
  try {
    ents = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const ent of ents) {
    const abs = path.join(dir, ent.name);
    const rel = prefix ? prefix + "/" + ent.name : ent.name;
    if (ent.isDirectory()) {
      await walkRelative(abs, rel, out);
    } else {
      out.push(rel);
    }
  }
}

async function registerFiles(absDir, relPrefix, files) {
  const ents = await fs.readdir(absDir, { withFileTypes: true });
  for (const ent of ents) {
    const abs = path.join(absDir, ent.name);
    const rel = relPrefix ? relPrefix + "/" + ent.name : ent.name;
    if (ent.isDirectory()) {
      await registerFiles(abs, rel, files);
      continue;
    }
    const st = await fs.stat(abs);
    const sha = await sha256File(abs).catch(() => null);
    files.push({ rel, sha256: sha, kind: classify(path.extname(ent.name)), size: st.size });
  }
}

// ---------- 7. 章节下载编排 ----------
// opts: { context, page, pan, shareCtx, chapterName, destDir }；deps: { downloadOne?, extract? }
// 返回与 downloadChapter 同构的 { chapterDir, files, skippedMedia, warnings, allLocal }，可直接接入 stageDownload。
export async function downloadShareChapter(opts, deps = {}) {
  const {
    context = null,
    page = null,
    pan = null,
    shareCtx = null,
    chapterName = null,
    destDir = null,
  } = opts || {};
  if (!shareCtx || !Array.isArray(shareCtx.entries)) {
    throw new Error("shareCtx.entries 缺失，无法从分享页下载章节");
  }
  if (typeof chapterName !== "string" || !chapterName) {
    throw new Error("chapterName 缺失或非法");
  }
  if (typeof destDir !== "string" || !destDir) {
    throw new Error("destDir 缺失或非法");
  }
  const downloadOne = typeof deps.downloadOne === "function" ? deps.downloadOne : defaultDownloadOne;
  const extract = typeof deps.extract === "function" ? deps.extract : extractArchive;

  const entry = shareCtx.entries.find((e) => e && e.name === chapterName);
  if (!entry) {
    const avail = shareCtx.entries.map((e) => e && e.name).filter(Boolean).join("、");
    throw new Error("分享中找不到章节「" + chapterName + "」；可用条目：" + (avail || "(空)"));
  }
  const [plan] = planShareDownloads([entry]); // 危险名在此 fail-closed，先于任何写入
  await fs.mkdir(destDir, { recursive: true });
  const chapterDir = resolveInside(destDir, "."); // 规范化基目录；后续每次写入再单独过 resolveInside

  const files = [];
  const skippedMedia = [];
  const warnings = [];

  if (plan.needFolderZip) {
    // 文件夹：百度打包 zip → 章节目录内暂存 → 解压到 _extracted/<名> → 删包（失败则保留压缩包并抛错）
    const stagingDir = resolveInside(chapterDir, ".share-staging");
    await fs.mkdir(stagingDir, { recursive: true });
    const zipFile = resolveInside(stagingDir, flatZipName(plan.safeName));
    const buf = await downloadOne({ context, page, pan, shareCtx, entry, plan, expectZip: true });
    guardDownloadBuffer(buf, zipFile);
    await fs.writeFile(zipFile, buf);
    warnings.push({ file: plan.safeName, reason: "文件夹由百度打包为 zip，下载后解压到 _extracted" });
    const stem = path.basename(zipFile, ".zip");
    const exDir = resolveInside(chapterDir, "_extracted", stem);
    try {
      await extract(zipFile, exDir);
    } catch (e) {
      throw new Error("章节 zip 解压失败（已保留压缩包待人工处理）: " + (e.message || e));
    }
    await fs.unlink(zipFile).catch(() => {});
    // Node22 下 recursive:false 对目录会抛 EISDIR；暂存目录由本函数创建且只放临时 zip，可整体递归删除
    await fs.rm(stagingDir, { recursive: true, force: true }).catch(() => {});
    await registerFiles(exDir, "_extracted/" + stem, files);
    log.ok("分享文件夹已解压：", plan.safeName, "->", exDir);
  } else {
    const kind = classify(path.extname(plan.safeName));
    if (kind === "media") {
      // 音视频/安装包：不下载、不登记 files，与 baidu-download.downloadChapter 的 skippedMedia 语义一致
      skippedMedia.push({ rel: plan.safeName, size: entry.size || 0, reason: "音视频/安装包不汇总，分享页直连跳过下载" });
    } else {
      const buf = await downloadOne({ context, page, pan, shareCtx, entry, plan, expectZip: false });
      guardDownloadBuffer(buf, plan.safeName);
      const dest = resolveInside(chapterDir, plan.safeName);
      await fs.mkdir(path.dirname(dest), { recursive: true });
      await fs.writeFile(dest, buf);
      const sha = await sha256File(dest).catch(() => null);
      files.push({ rel: plan.safeName, sha256: sha, kind, size: buf.length });
      log.ok("分享单文件已下载：", plan.safeName, buf.length, "字节");
    }
  }

  const allLocal = [];
  await walkRelative(chapterDir, "", allLocal);
  return { chapterDir, files, skippedMedia, warnings, allLocal };
}