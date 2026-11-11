// 已转存章节文件下载：递归列目录 → dlink 下载（UI 兜底）→ 解压 → 分类登记
import { promises as fs } from "node:fs";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { paths } from "./paths.mjs";
import { sha256File } from "./docs-md.mjs";
import { ARCHIVE_EXTS, DOC_EXTS, IMG_EXTS, SKIP_EXTS_MEDIA } from "./docs-md.mjs";
import { log } from "./log.mjs";

const execFileP = promisify(execFile);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function classify(ext) {
  ext = ext.toLowerCase();
  if (DOC_EXTS.includes(ext)) return "doc";
  if (IMG_EXTS.includes(ext)) return "image";
  if (ARCHIVE_EXTS.includes(ext)) return "archive";
  if (SKIP_EXTS_MEDIA.includes(ext)) return "media";
  return "other";
}

export async function listRecursive(pan, dir, prefix = "") {
  const out = [];
  const entries = await pan.ownList(dir);
  for (const e of entries) {
    const rel = prefix ? prefix + "/" + e.name : e.name;
    if (e.isdir) {
      await sleep(300);
      out.push(...(await listRecursive(pan, dir === "/" ? "/" + e.name : dir + "/" + e.name, rel)));
    } else {
      out.push({ name: e.name, rel, fsid: e.fsid, size: e.size, parentDir: dir });
    }
  }
  return out;
}

async function getDlink(pan, fsid) {
  const url =
    "https://pan.baidu.com/api/filemetas?fsidlist=" + encodeURIComponent("[" + fsid + "]") +
    "&dlink=1&target=" + encodeURIComponent("[" + fsid + "]");
  const j = await pan.apiGet(url);
  const item = (j.list || j.info || [])[0];
  if (!item?.dlink) throw new Error("filemetas 无 dlink: " + JSON.stringify(j).slice(0, 200));
  return item.dlink;
}

// 网页端对大文件（实测约 >46MB）返回 31326/hitcode119 授权限制，要求客户端或 SVIP；Range 也被封。
// 单独抛出以便上层"跳过并标记"，而非让整章失败或静默丢内容。
export class GatedDownloadError extends Error {
  constructor(panPath, status, body) {
    super("GATED_LARGE_FILE: 网页端无授权下载大文件 " + panPath + " (HTTP " + status + " " + String(body).slice(0, 80).replace(/\s+/g, " ") + ")");
    this.name = "GatedDownloadError";
    this.panPath = panPath;
  }
}

// PCS 2.0 按"绝对路径"下载自有网盘文件（新版网页端通道，无需 sign/dlink，
// 依赖浏览器 context 中由 plantcookie 种下的 stoken/ett cookie；须用 context.request 而非页面内 fetch 以规避 CORS）。
export async function downloadPcsByPath({ context, panPath, dest, displayName = null }) {
  const url =
    "https://d.pcs.baidu.com/rest/2.0/pcs/file?method=download&app_id=250528&path=" + encodeURIComponent(panPath);
  const resp = await context.request.get(url, { maxRedirects: 5, timeout: 300000 });
  if (resp.status() === 403) {
    const body = (await resp.body().catch(() => Buffer.alloc(0))).toString("utf8");
    if (body.includes("31326") || body.includes("not authorized")) throw new GatedDownloadError(panPath, 403, body);
  }
  if (!resp.ok()) throw new Error("PCS 下载 HTTP " + resp.status() + " " + panPath);
  const body = await resp.body();
  if (!body.length) throw new Error("PCS 下载内容为空: " + panPath);
  const head = body.slice(0, 200).toString("utf8");
  const nm = displayName || dest;
  // 鉴权失效/风控会回 JSON 或 HTML 错误页而非文件流
  if ((/<html|<!DOCTYPE/i.test(head) || /^\{/.test(head.trim())) && !nm.match(/\.(html?|json)$/i)) {
    throw new Error("PCS 返回非文件内容（疑似鉴权失效/风控）: " + head.slice(0, 100).replace(/\s+/g, " "));
  }
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await fs.writeFile(dest, body);
  return body.length;
}

// 单文件 dlink 下载（自有网盘，旧通道）。供"单元本身是压缩包文件"的场景复用；与 downloadViaRequest 同一通道。
export async function downloadOwnFile({ context, pan, fsid, dest, displayName = null }) {
  const dlink0 = await getDlink(pan, fsid);
  const dlink = dlink0 + (dlink0.includes("?") ? "&" : "?") + "origin=dl";
  const resp = await context.request.get(dlink, {
    headers: { "User-Agent": "netdisk; pan.baidu.com", Referer: "https://pan.baidu.com/" },
    maxRedirects: 5,
    timeout: 300000,
  });
  if (!resp.ok()) throw new Error("dlink HTTP " + resp.status());
  const body = await resp.body();
  if (!body.length) throw new Error("下载内容为空");
  const head = body.slice(0, 200).toString("utf8");
  const nm = displayName || dest;
  if (/<html|<!DOCTYPE/i.test(head) && !nm.match(/\.(html?)$/i)) {
    throw new Error("下载返回疑似 HTML 风控页: " + head.slice(0, 80).replace(/\s+/g, " "));
  }
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await fs.writeFile(dest, body);
  return body.length;
}

async function downloadViaRequest(context, pan, file, dest) {
  const dlink0 = await getDlink(pan, file.fsid);
  const dlink = dlink0 + (dlink0.includes("?") ? "&" : "?") + "origin=dl";
  const resp = await context.request.get(dlink, {
    headers: {
      "User-Agent": "netdisk; pan.baidu.com",
      Referer: "https://pan.baidu.com/",
    },
    maxRedirects: 5,
    timeout: 300000,
  });
  if (!resp.ok()) throw new Error("dlink HTTP " + resp.status());
  const body = await resp.body();
  if (!body.length) throw new Error("下载内容为空");
  // 百度风控会返回 HTML 提示页
  const head = body.slice(0, 200).toString("utf8");
  if (/<html|<!DOCTYPE/i.test(head) && !file.name.match(/\.(html?)$/i)) {
    throw new Error("下载返回疑似 HTML 风控页: " + head.slice(0, 80).replace(/\s+/g, " "));
  }
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await fs.writeFile(dest, body);
  return body.length;
}

async function downloadViaUI(page, pan, context, file, targetDir, dest) {
  log.warn("UI 兜底下载：", file.rel);
  await page.goto("https://pan.baidu.com/disk/main#/index?category=all&path=" + encodeURIComponent(targetDir), {
    waitUntil: "domcontentloaded",
  });
  await sleep(3000);
  const dlPromise = page.waitForEvent("download", { timeout: 60000 });
  await page.evaluate((name) => {
    const rows = document.querySelectorAll(".file-item, tr.file-item, .list-item");
    for (const row of rows) {
      if (row.textContent.includes(name)) {
        row.querySelector(".checker, input[type=checkbox]")?.click();
        row.click();
        return true;
      }
    }
    return false;
  }, file.name);
  await sleep(800);
  await page.locator("text=下载").first().click({ timeout: 8000 });
  const download = await dlPromise;
  await fs.mkdir(path.dirname(dest), { recursive: true });
  await download.saveAs(dest);
}

// zip-slip 防护：压缩包条目不得是绝对路径或含 .. 跨目录段
export function unsafeEntryNames(names) {
  return names.filter((n) => {
    const parts = String(n).split(/[\\/]/);
    return n.startsWith("/") || n.startsWith("\\") || parts.includes("..");
  });
}

// 本地写入边界守卫：解析后的路径必须仍在 baseDir 内，否则拒绝（防网盘条目的 ../ 越界）
export function resolveInside(baseDir, ...segments) {
  const root = path.resolve(baseDir);
  const resolved = path.resolve(root, ...segments);
  if (resolved !== root && !resolved.startsWith(root + path.sep)) {
    throw new Error("本地路径越界，拒绝写入: " + resolved + "（基目录 " + root + "）");
  }
  return resolved;
}

// lsar 列表：首行为"<归档路径>: <格式>"表头，其余每行一个条目；unar/lsar 可自动识别 GBK 中文文件名
async function unarList(file) {
  const { stdout } = await execFileP("lsar", [file]);
  const lines = stdout.split("\n").map((s) => s.trim()).filter(Boolean);
  return lines.slice(1);
}

async function listArchiveEntries(file, ext) {
  if (ext === ".zip") {
    // 中文 Windows 压缩包文件名常为 GBK，/usr/bin/unzip 会报 Illegal byte sequence；unar 可自动识别编码
    return unarList(file).catch(async () => {
      const { stdout } = await execFileP("/usr/bin/unzip", ["-Z1", file]);
      return stdout.split("\n").map((s) => s.trim()).filter(Boolean);
    });
  }
  if (ext === ".tar" || ext === ".gz") {
    const { stdout } = await execFileP("/usr/bin/tar", ["-tf", file]);
    return stdout.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  if (ext === ".rar") {
    return unarList(file);
  }
  if (ext === ".7z") {
    const { stdout } = await execFileP("7z", ["l", "-slt", file]);
    return stdout.split("\n").filter((l) => l.startsWith("Path = ")).map((l) => l.slice(7).trim());
  }
  return [];
}

async function extractArchive(file, destDir) {
  const ext = path.extname(file).toLowerCase();
  await fs.mkdir(destDir, { recursive: true });
  const unsafe = unsafeEntryNames(await listArchiveEntries(file, ext).catch(() => []));
  if (unsafe.length) {
    throw new Error("压缩包含不安全路径，已拒绝解压: " + unsafe.slice(0, 3).join(", "));
  }
  if (ext === ".zip") {
    // unar 自动处理 GBK 中文文件名；-D 强制扁平解压到目标目录（与 unzip -d 一致）
    try {
      await execFileP("unar", ["-D", "-force-overwrite", "-o", destDir, file]);
    } catch (e) {
      await execFileP("/usr/bin/unzip", ["-oq", file, "-d", destDir]);
    }
  } else if (ext === ".tar" || ext === ".gz") {
    await execFileP("/usr/bin/tar", ["-xf", file, "-C", destDir]);
  } else if (ext === ".rar" || ext === ".7z") {
    const bin = ext === ".rar" ? "unar" : "7z";
    try {
      if (ext === ".rar") await execFileP("unar", ["-D", "-force-overwrite", "-o", destDir, file]);
      else await execFileP("7z", ["x", "-y", "-o" + destDir, file]);
    } catch (e) {
      throw new Error("缺少解压器 " + bin + "（可 brew install unar 或 7zip）: " + e.message);
    }
  } else {
    throw new Error("不支持的压缩格式 " + ext);
  }
}

// 下载并解压某章节。返回 { files, skippedMedia, warnings }
export async function downloadChapter({ context, page, pan, course, shareName }) {
  const targetDir = course.baidu.targetDir.replace(/\/$/, "") + "/" + shareName;
  // 章节目录本身越界（异常 shareName）直接 fail-closed，由上层标记 blocked
  const chapterDir = resolveInside(paths.courseDownloads(course.id), shareName);
  await fs.mkdir(chapterDir, { recursive: true });

  const remote = await listRecursive(pan, targetDir);
  log.info("远端文件数：", remote.length, "（", shareName, "）");
  const files = [];
  const skippedMedia = [];
  const warnings = [];

  for (const f of remote) {
    const kind = classify(path.extname(f.name));
    // 单文件 rel 越界：跳过并告警，不拖垮整章
    let dest;
    try {
      dest = resolveInside(chapterDir, f.rel);
    } catch (e) {
      warnings.push({ file: f.rel, reason: "跳过：" + e.message });
      continue;
    }
    if (kind === "media") {
      skippedMedia.push({ rel: f.rel, size: f.size, reason: "音视频不汇总" });
      continue;
    }
    try {
      let bytes = 0;
      if (kind === "doc" || kind === "image" || kind === "other") {
        try {
          bytes = await downloadViaRequest(context, pan, f, dest);
        } catch (e) {
          log.warn("dlink 失败，转 UI：", f.rel, e.message);
          await downloadViaUI(page, pan, context, f, path.posix.dirname(targetDir + "/" + f.rel), dest);
          bytes = (await fs.stat(dest)).size;
        }
      } else if (kind === "archive") {
        await downloadViaRequest(context, pan, f, dest).catch(async () => {
          await downloadViaUI(page, pan, context, f, path.posix.dirname(targetDir + "/" + f.rel), dest);
        });
        const exDir = path.join(chapterDir, "_extracted", path.basename(f.rel).replace(/\.[^.]+$/, ""));
        try {
          await extractArchive(dest, exDir);
          warnings.push({ file: f.rel, reason: "已解压到 _extracted" });
        } catch (e) {
          warnings.push({ file: f.rel, reason: "解压失败：" + e.message });
        }
      }
      const sha = await sha256File(dest).catch(() => null);
      files.push({ rel: f.rel, sha256: sha, kind, size: bytes || f.size });
      await sleep(500 + Math.random() * 800);
    } catch (e) {
      warnings.push({ file: f.rel, reason: "下载失败：" + (e.message || e) });
    }
  }

  // 统计解压后新增文档
  const allLocal = [];
  async function walk(d, prefix = "") {
    for (const ent of await fs.readdir(d, { withFileTypes: true })) {
      const p = path.join(d, ent.name);
      const rel = prefix ? prefix + "/" + ent.name : ent.name;
      if (ent.isDirectory()) await walk(p, rel);
      else allLocal.push(rel);
    }
  }
  await walk(chapterDir);
  return { chapterDir, files, skippedMedia, warnings, allLocal };
}

export { extractArchive };
