// 课程发布本地工作台：订阅管理 + 扫描 + 暂存(图片上云/在线链接) + 人工预览/批准 + 批准后发布。
// 仅监听 127.0.0.1，不对外暴露。重活以子任务方式复用已测 CLI，任务日志落盘并可在 UI 查看。
import http from "node:http";
import { promises as fs, existsSync, createReadStream } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import os from "node:os";
import { createRequire } from "node:module";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import { paths } from "./lib/paths.mjs";
import { log } from "./lib/log.mjs";
import { loadCourses } from "./lib/config.mjs";
import { buildChapterView, summarize } from "./lib/workbench-state.mjs";
import { loadStaging, saveStaging, STAGE, previewPath } from "./lib/staging.mjs";
import { markdownToHtml, stripFrontmatter } from "./lib/markdown-html.mjs";
const require2 = createRequire(import.meta.url);
const execFileP = promisify(execFile);
import { findLocalImageRefs } from "./lib/images-cnblogs.mjs";
import { walk, DOC_EXTS, IMG_EXTS, ARCHIVE_EXTS, SKIP_EXTS_MEDIA } from "./lib/docs-md.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.WORKBENCH_PORT || 5717);
const HOST = "127.0.0.1";

// ---------- 任务运行器 ----------
const jobs = new Map();
function startJob(kind, label, args) {
  const id = kind + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6);
  const logPath = path.join(paths.logsDir(), "workbench-jobs", id + ".log");
  fs.mkdir(path.dirname(logPath), { recursive: true });
  const child = spawn(process.execPath, args, { cwd: paths.root, env: process.env });
  let out = "";
  const append = (chunk) => { out += chunk; if (out.length > 200000) out = out.slice(-200000); fs.appendFile(logPath, chunk).catch(() => {}); };
  child.stdout.on("data", append);
  child.stderr.on("data", append);
  const job = { id, kind, label, args, status: "running", startedAt: new Date().toISOString(), logPath, code: null, get output() { return out; } };
  jobs.set(id, job);
  child.on("close", (code) => { job.status = code === 0 ? "done" : "error"; job.code = code; job.finishedAt = new Date().toISOString(); log.info("任务结束", id, "code=" + code); });
  child.on("error", (e) => { job.status = "error"; job.error = String(e); });
  return job;
}
const scriptArg = (name) => path.join(HERE, name);

// ---------- 数据读取 ----------
async function readJson(file, fallback) { try { return JSON.parse(await fs.readFile(file, "utf8")); } catch { return fallback; } }
async function courseState(course) {
  const plan = await readJson(path.join(paths.stateDir, course + ".chapter-plan.json"), { chapters: [] });
  const progress = await readJson(path.join(paths.stateDir, course + ".edition8-progress.json"), {});
  const staging = await loadStaging(course);
  const publishState = await readJson(path.join(paths.stateDir, course + ".publish-state.json"), {});
  const quota = await readJson(path.join(paths.stateDir, course + ".media-quota.json"), null);
  const view = buildChapterView(plan, progress, staging.chapters || {}, publishState);
  return { course, summary: summarize(view), chapters: view, quota };
}

// ---------- HTTP 工具 ----------
const json = (res, code, data) => { res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" }); res.end(JSON.stringify(data)); };
const readBody = (req) => new Promise((resolve) => { let b = ""; req.on("data", (c) => { b += c; if (b.length > 2e6) req.destroy(); }); req.on("end", () => { try { resolve(b ? JSON.parse(b) : {}); } catch { resolve({}); } }); });

// ---------- 路由 ----------
const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://" + HOST);
  const p = u.pathname;
  try {
    if (req.method === "GET" && p === "/") return serveUi(res);
    if (req.method === "GET" && p === "/app.js") return serveStatic(res, path.join(HERE, "lib", "workbench-app.js"), "application/javascript; charset=utf-8");
    if (req.method === "GET" && p === "/quota") return serveQuota(res, u);
    if (req.method === "GET" && p === "/api/state") { const cs = loadCourses().courses.map((c) => c.id); const course = u.searchParams.get("course") || cs[0]; return json(res, 200, { courses: cs, current: course, ...(await courseState(course)), jobs: [...jobs.values()].slice(-12).reverse().map(publicJob) }); }
    if (req.method === "GET" && p === "/api/preview") return handlePreview(req, res, u);
    if (req.method === "GET" && p === "/api/sources") return handleSources(res, u);
    if (req.method === "GET" && p === "/api/source-file") return handleSourceFile(res, u);
    if (req.method === "GET" && p === "/api/ppt-slide") return handlePptSlide(res, u);
    if (req.method === "GET" && p === "/api/archive-list") return handleArchiveList(res, u);
    if (req.method === "GET" && p === "/api/archive-file") return handleArchiveFile(res, u);
    if (req.method === "GET" && p === "/api/jobs") return json(res, 200, [...jobs.values()].slice(-20).reverse().map(publicJob));
    if (req.method === "GET" && p.startsWith("/api/jobs/")) { const j = jobs.get(p.split("/").pop()); return j ? json(res, 200, publicJob(j, true)) : json(res, 404, { error: "任务不存在" }); }
    if (req.method === "GET" && p === "/api/subscriptions") return handleListSubs(res);
    if (req.method === "POST" && p === "/api/subscriptions") return handleAddSub(req, res);
    if (req.method === "DELETE" && p === "/api/subscriptions") return handleDelSub(req, res, u);
    if (req.method === "POST") {
      const body = await readBody(req);
      if (p === "/api/scan") return json(res, 200, publicJob(startJob("scan", "扫描新增章节(" + body.course + ")", [scriptArg("run.mjs"), "--course", body.course, "--only", "transfer,toc,match,download,merge"])));
      if (p === "/api/stage") return handleStage(res, body);
      if (p === "/api/approve") return handleApprove(res, body, true);
      if (p === "/api/unapprove") return handleApprove(res, body, false);
      if (p === "/api/publish") return handlePublish(res, body);
    }
    json(res, 404, { error: "未知路由 " + p });
  } catch (e) { json(res, 500, { error: String(e.message || e) }); }
});

function publicJob(j, withOutput) { return { id: j.id, kind: j.kind, label: j.label, status: j.status, code: j.code, startedAt: j.startedAt, finishedAt: j.finishedAt, logPath: j.logPath, error: j.error, ...(withOutput ? { output: j.output } : {}) }; }

async function handlePreview(req, res, u) {
  const course = u.searchParams.get("course"), slug = u.searchParams.get("slug");
  const staging = await loadStaging(course);
  const entry = staging.chapters[slug];
  const pv = entry && entry.previewPath && existsSync(entry.previewPath) ? entry.previewPath : path.join(paths.courseOutput(course), "00-" + slug + ".md");
  if (!existsSync(pv)) return json(res, 404, { error: "章节 Markdown 不存在：" + slug });
  const md = await fs.readFile(pv, "utf8");
  const local = findLocalImageRefs(md);
  const sourceMdPath = path.join(paths.courseOutput(course), "00-" + slug + ".md");
  return json(res, 200, { slug, source: entry && entry.previewPath === pv ? "preview" : "source", status: entry ? entry.status : "merged", hasLocalImages: local.length, html: markdownToHtml(md), md: stripFrontmatter(md), rawMd: md, filePath: pv, sourceMdPath, previewPath: entry && entry.previewPath ? entry.previewPath : "" });
}


// ---------- 本地源文件浏览（供与合并产物逐文件对比） ----------
const TEXT_VIEW_EXT = new Set([".md", ".markdown", ".txt", ".html", ".htm", ".json", ".css", ".js", ".mjs", ".py", ".csv", ".xml", ".log", ".yaml", ".yml", ".sh", ".ts", ".ini", ".conf"]);
const IMG_MIME = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp" };
const DOWNLOAD_EXT = new Set([".doc", ".docx", ".pptx", ".zip", ".rar", ".7z", ".tar", ".gz", ".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flv", ".exe", ".dmg", ".pkg"]);

function chapterRoot(course, slug) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(slug || "")) throw new Error("非法章节标识");
  const root = path.resolve(paths.courseDownloads(course), slug);
  if (!existsSync(root)) throw new Error("本地源目录不存在：" + root);
  return root;
}
function safeInside(root, rel) {
  const abs = path.resolve(root, rel);
  if (abs !== root && !abs.startsWith(root + path.sep)) return null; // 防目录穿越
  return abs;
}
function fileKind(ext) {
  if (DOC_EXTS.includes(ext)) return "doc";
  if (IMG_EXTS.includes(ext)) return "image";
  if (ARCHIVE_EXTS.includes(ext)) return "archive";
  if (SKIP_EXTS_MEDIA.includes(ext)) return "media";
  return "other";
}

async function handleSources(res, u) {
  try {
    const course = u.searchParams.get("course");
    const slug = u.searchParams.get("slug");
    const root = chapterRoot(course, slug);
    const all = await walk(root);
    const files = [];
    for (const abs of all) {
      const st = await fs.stat(abs);
      const ext = path.extname(abs).toLowerCase();
      files.push({ rel: path.relative(root, abs), ext, bytes: st.size, kind: fileKind(ext) });
    }
    // 解压产物 _extracted 是流水线工作目录：只要章节里还有原始压缩包/散文件，就隐藏它避免目录树膨胀；
    // 仅当全部内容都在 _extracted 下（无原始归档留存）时才保留显示
    const hasOutside = files.some((f) => !f.rel.split(path.sep)[0].startsWith("_extracted"));
    const visible = hasOutside ? files.filter((f) => !f.rel.startsWith("_extracted" + path.sep)) : files;
    return json(res, 200, { root, files: visible, hiddenExtracted: hasOutside });
  } catch (e) { return json(res, 404, { error: String(e.message || e) }); }
}

async function handleSourceFile(res, u) {
  try {
    const course = u.searchParams.get("course");
    const slug = u.searchParams.get("slug");
    const rel = u.searchParams.get("rel") || "";
    const root = chapterRoot(course, slug);
    const abs = safeInside(root, rel);
    if (!abs || !existsSync(abs) || !(await fs.stat(abs)).isFile()) return json(res, 404, { error: "文件不存在" });
    const ext = path.extname(abs).toLowerCase();
    const base = path.basename(abs);
    const disp = "attachment; filename*=UTF-8''" + encodeURIComponent(base);
    if (IMG_MIME[ext]) { res.writeHead(200, { "Content-Type": IMG_MIME[ext] }); return createReadStream(abs).pipe(res); }
    if (ext === ".pdf") { res.writeHead(200, { "Content-Type": "application/pdf" }); return createReadStream(abs).pipe(res); }
    if (TEXT_VIEW_EXT.has(ext)) { res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" }); return createReadStream(abs).pipe(res); }
    if (ext === ".docx" && u.searchParams.get("view") === "html") {
      return serveDocxHtml(res, abs);
    }
    if ((ext === ".pptx" || ext === ".ppt") && u.searchParams.get("view") === "slides") {
      return servePptSlides(res, abs, course, slug, rel);
    }
    res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Disposition": disp });
    return createReadStream(abs).pipe(res);
  } catch (e) { return json(res, 400, { error: String(e.message || e) }); }
}

// docx → 网页 HTML（mammoth 内嵌图转 data URL，单文件直接可看）
async function docxToHtml(file) {
  const mammoth = require2("mammoth");
  const result = await mammoth.convertToHtml(
    { path: file },
    {
      convertImage: mammoth.images.imgElement(async (image) => {
        const buf = await image.read();
        const mime = image.contentType || "image/png";
        return { src: "data:" + mime + ";base64," + buf.toString("base64") };
      }),
    },
  );
  const body = result.value
    .replace(/\[(?!(?:[A-Za-z]+\]))((?:image|figure)[^\]]*)\]/g, "[$1]")
    .replace(/<img /g, '<img style="max-width:100%" ');
  return "<!doctype html><html><head><meta charset=\"utf-8\"><title>预览</title></head><body style=\"max-width:760px;margin:24px auto;padding:0 16px;font:15px/1.8 -apple-system,'PingFang SC',sans-serif\">" + body + "</body></html>";
}
// PPT → 逐页 PNG 预览：渲染到 state/.ppt-preview/<hash>/ 缓存，返回内嵌图片的 HTML
async function handlePptSlide(res, u) {
  const key = u.searchParams.get("key") || "";
  const name = u.searchParams.get("name") || "";
  if (!/^[a-f0-9]{16}$/.test(key) || !/^slide-\d+\.png$/.test(name)) return json(res, 400, { error: "参数非法" });
  const file = path.join(paths.stateDir, ".ppt-preview", key, name);
  if (!existsSync(file)) return json(res, 404, { error: "不存在" });
  res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "max-age=3600" });
  return createReadStream(file).pipe(res);
}

async function servePptSlides(res, abs, course, slug, rel) {
  try {
    const { pptxRenderToDir } = await import("./lib/ppt-images.mjs");
    const { createHash } = await import("node:crypto");
    const stat = await fs.stat(abs);
    const key = createHash("sha256").update(abs + stat.size + stat.mtimeMs).digest("hex").slice(0, 16);
    const outDir = path.join(paths.stateDir, ".ppt-preview", key);
    let slides;
    const done = path.join(outDir, ".done");
    if (existsSync(done)) {
      slides = (await fs.readdir(outDir)).filter((f) => /^slide-\d+\.png$/.test(f)).sort();
    } else {
      slides = await pptxRenderToDir(abs, outDir);
      await fs.writeFile(done, "1");
    }
    const base = "/api/ppt-slide?key=" + key + "&name=";
    const imgs = slides.map((s, i) => '<div style="margin:0 0 18px"><div style="color:#888;font:12px sans-serif;margin-bottom:4px">第 ' + (i + 1) + " 页</div><img src=\"" + base + s + "\" style=\"max-width:100%;border:1px solid #ddd;border-radius:6px\"></div>").join("\n");
    const html = "<!doctype html><html><head><meta charset=\"utf-8\"><title>PPT 预览</title></head><body style=\"max-width:900px;margin:20px auto;padding:0 16px\">" + imgs + "</body></html>";
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(html);
  } catch (e) { return json(res, 500, { error: "PPT 渲染失败: " + String(e.message || e) }); }
}

async function serveDocxHtml(res, file) {
  try {
    const html = await docxToHtml(file);
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(html);
  } catch (e) { json(res, 500, { error: "docx 预览转换失败：" + (e.message || e) }); }
}

// ---------- 压缩包内容浏览（网页内逐层查看） ----------
async function listArchiveEntries(abs) {
  const out = await execFileP("lsar", ["-j", abs], { maxBuffer: 64 * 1024 * 1024 });
  const data = JSON.parse(out.stdout);
  return (data.lsarContents || [])
    .filter((e) => !e.XADIsDirectory && typeof e.XADFileName === "string")
    .map((e) => ({ name: e.XADFileName, bytes: e.XADFileSize || 0 }));
}
// 压缩包内提取单个条目到临时文件（-f 精确匹配整名，避免通配误命中；拒绝路径穿越条目）
async function extractArchiveEntry(abs, entry) {
  if (!entry || entry.includes("\\0") || /(^|\/)\.\.\//.test(entry) || entry.startsWith("/")) throw new Error("非法条目路径");
  const entries = await listArchiveEntries(abs);
  if (!entries.some((e) => e.name === entry)) throw new Error("条目不存在：" + entry);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "wb-arc-"));
  try {
    await execFileP("unar", ["-f", "-o", tmp, abs, entry], { maxBuffer: 64 * 1024 * 1024 });
    const direct = path.join(tmp, entry);
    if (existsSync(direct)) return { tmp, file: direct };
    const found = (await walk(tmp)).filter((f) => path.basename(f) === path.basename(entry));
    if (!found.length) throw new Error("解压后未找到条目");
    return { tmp, file: found[0] };
  } catch (e) {
    await fs.rm(tmp, { recursive: true, force: true }).catch(() => {});
    throw e;
  }
}
async function handleArchiveList(res, u) {
  try {
    const course = u.searchParams.get("course"), slug = u.searchParams.get("slug"), rel = u.searchParams.get("rel");
    const root = chapterRoot(course, slug);
    const abs = safeInside(root, rel);
    if (!abs || !existsSync(abs)) return json(res, 404, { error: "压缩包不存在" });
    const entries = await listArchiveEntries(abs);
    return json(res, 200, { entries: entries.map((e) => ({ name: e.name, bytes: e.bytes, kind: fileKind(path.extname(e.name).toLowerCase()) })) });
  } catch (e) { return json(res, 400, { error: "读取压缩包失败：" + (e.message || e) }); }
}
async function handleArchiveFile(res, u) {
  let tmp = null;
  try {
    const course = u.searchParams.get("course"), slug = u.searchParams.get("slug"), rel = u.searchParams.get("rel"), entry = u.searchParams.get("entry");
    const root = chapterRoot(course, slug);
    const abs = safeInside(root, rel);
    if (!abs || !existsSync(abs)) return json(res, 404, { error: "压缩包不存在" });
    const out = await extractArchiveEntry(abs, entry);
    tmp = out.tmp;
    const ext = path.extname(out.file).toLowerCase();
    const base = path.basename(out.file);
    res.on("close", () => fs.rm(tmp, { recursive: true, force: true }).catch(() => {}));
    if (IMG_MIME[ext]) { res.writeHead(200, { "Content-Type": IMG_MIME[ext] }); return createReadStream(out.file).pipe(res); }
    if (ext === ".pdf") { res.writeHead(200, { "Content-Type": "application/pdf" }); return createReadStream(out.file).pipe(res); }
    if (TEXT_VIEW_EXT.has(ext)) { res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" }); return createReadStream(out.file).pipe(res); }
    if (ext === ".docx" && u.searchParams.get("view") === "html") {
      try {
        const html = await docxToHtml(out.file);
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(html);
      } catch (e) { json(res, 500, { error: "docx 预览转换失败：" + (e.message || e) }); }
      return;
    }
    if ((ext === ".pptx" || ext === ".ppt") && u.searchParams.get("view") === "slides") {
      // 渲染到 state 缓存（图片不依赖临时解压目录，tmp 关闭清理不影响）
      return servePptSlides(res, out.file, course, slug, rel + "::" + entry);
    }
    res.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename*=UTF-8''" + encodeURIComponent(base) });
    return createReadStream(out.file).pipe(res);
  } catch (e) { return json(res, 400, { error: String(e.message || e) }); }
}

function handleStage(res, body) {
  const slugs = Array.isArray(body.slugs) ? body.slugs : [];
  const args = [scriptArg("stage.mjs"), "--course=" + body.course];
  if (slugs.length === 1) args.push("--only=" + slugs[0]); else args.push("--all-pending");
  return json(res, 200, publicJob(startJob("stage", "暂存/图片上云(" + body.course + ")", args)));
}

async function handleApprove(res, body, approve) {
  const course = body.course; const slugs = Array.isArray(body.slugs) ? body.slugs : [];
  const staging = await loadStaging(course);
  const changed = []; const rejected = [];
  for (const slug of slugs) {
    const e = staging.chapters[slug];
    if (!e) { rejected.push({ slug, reason: "未暂存" }); continue; }
    if (approve) {
      if (e.status !== STAGE.STAGED) { rejected.push({ slug, reason: "状态 " + e.status + " 不可批准（需 staged）" }); continue; }
      if ((e.failed || []).length) { rejected.push({ slug, reason: "有 " + e.failed.length + " 张图片未上云" }); continue; }
      e.status = STAGE.APPROVED; e.approvedAt = new Date().toISOString();
    } else {
      if (e.status !== STAGE.APPROVED) { rejected.push({ slug, reason: "非 approved 不可撤回" }); continue; }
      e.status = STAGE.STAGED; e.approvedAt = null;
    }
    changed.push(slug);
  }
  await saveStaging(course, staging);
  return json(res, 200, { changed, rejected });
}

function handlePublish(res, body) {
  const slugs = Array.isArray(body.slugs) ? body.slugs : [];
  const args = [scriptArg("publish-approved.mjs"), "--course=" + body.course, "--gap=" + (body.gap || 45000)];
  if (slugs.length === 1) args.push("--only=" + slugs[0]);
  return json(res, 200, publicJob(startJob("publish", "批准后发布(" + body.course + ")", args)));
}

// ---------- 订阅管理（courses.yaml shares） ----------
async function handleListSubs(res) {
  const raw = await fs.readFile(paths.config, "utf8");
  const cfg = parseYaml(raw) || {};
  return json(res, 200, (cfg.courses || []).map((c) => ({ id: c.id, title: c.title, collection: c.collection, toc: c.toc && c.toc.url, targetDir: c.baidu && c.baidu.targetDir, shares: (c.baidu && c.baidu.shares) || [] })));
}
async function handleAddSub(req, res) {
  const body = await readBody(req);
  const raw = await fs.readFile(paths.config, "utf8"); const cfg = parseYaml(raw) || {};
  const c = (cfg.courses || []).find((x) => x.id === body.course);
  if (!c) return json(res, 404, { error: "课程不存在：" + body.course });
  c.baidu = c.baidu || { shares: [] }; c.baidu.shares = c.baidu.shares || [];
  if (!/^https?:\/\//.test(body.url || "")) return json(res, 400, { error: "分享链接非法" });
  if (c.baidu.shares.some((s) => s.url === body.url)) return json(res, 409, { error: "该分享已存在" });
  c.baidu.shares.push({ url: body.url, password: body.password || "" });
  await fs.writeFile(paths.config, stringifyYaml(cfg));
  return json(res, 200, { ok: true });
}
async function handleDelSub(req, res, u) {
  const course = u.searchParams.get("course"), url = u.searchParams.get("url");
  const raw = await fs.readFile(paths.config, "utf8"); const cfg = parseYaml(raw) || {};
  const c = (cfg.courses || []).find((x) => x.id === course);
  if (!c || !c.baidu || !Array.isArray(c.baidu.shares)) return json(res, 404, { error: "课程/分享不存在" });
  const before = c.baidu.shares.length;
  c.baidu.shares = c.baidu.shares.filter((s) => s.url !== url);
  if (c.baidu.shares.length === before) return json(res, 404, { error: "未找到该分享" });
  await fs.writeFile(paths.config, stringifyYaml(cfg));
  return json(res, 200, { ok: true });
}

async function serveQuota(res, u) {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
  const quotaRows = [];
  for (const file of await fs.readdir(paths.stateDir).catch(() => [])) {
    if (!file.endsWith(".media-quota.json")) continue;
    try {
      const q = JSON.parse(await fs.readFile(path.join(paths.stateDir, file), "utf8"));
      quotaRows.push({ course: file.replace(".media-quota.json", ""), ...q });
    } catch {}
  }
  const ledger = [];
  try {
    for (const line of (await fs.readFile(path.join(paths.stateDir, "media-uploads.jsonl"), "utf8")).split("\n")) {
      if (line.trim()) { try { ledger.push(JSON.parse(line)); } catch {} }
    }
  } catch {}
  const dates = [...new Set([...quotaRows.map((q) => q.date), ...ledger.map((e) => e.date)])].filter(Boolean).sort().reverse();
  let sections = "";
  for (const date of dates) {
    const qs = quotaRows.filter((q) => q.date === date);
    const es = ledger.filter((e) => e.date === date);
    sections += "<section class='card'><h2>" + esc(date) + "</h2><p class='muted'>配额汇总（课程级记录；博客园 500 张/日限额为账号级）</p><table><thead><tr><th>课程</th><th>已用</th><th>限额</th><th>状态</th></tr></thead><tbody>";
    for (const q of qs) sections += "<tr><td>" + esc(q.course) + "</td><td>" + esc(q.uploaded) + "</td><td>" + esc(q.limit) + "</td><td>" + (q.blocked ? "已触发 403，暂停" : "可继续") + "</td></tr>";
    if (!qs.length) sections += "<tr><td colspan='4' class='muted'>没有配额汇总</td></tr>";
    sections += "</tbody></table>";
    if (es.length) {
      sections += "<h3>逐张上传明细（" + es.length + "）</h3><table><thead><tr><th>时间</th><th>课程</th><th>章节</th><th>文件</th><th>大小</th><th>图片 URL</th></tr></thead><tbody>";
      for (const e of es) sections += "<tr><td>" + esc(String(e.at || "").replace("T", " ").slice(0, 19)) + "</td><td>" + esc(e.course || "") + "</td><td>" + esc(e.slug || "") + "</td><td>" + esc(e.file || "") + "</td><td>" + esc(e.bytes ? Math.round(e.bytes / 1024) + " KB" : "-") + "</td><td>" + (e.url ? "<a href='" + esc(e.url) + "' target='_blank'>打开</a>" : "-") + "</td></tr>";
      sections += "</tbody></table>";
    } else if (qs.length) {
      sections += "<p class='muted'>该日只有配额汇总，升级账本后新上传才会记录逐张文件。</p>";
    }
    sections += "</section>";
  }
  const html = "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>图床配额</title><style>body{margin:0;font-family:-apple-system,PingFang SC,sans-serif;background:#f6f7f9;color:#1f2430}header{background:#fff;border-bottom:1px solid #e3e6eb;padding:14px 20px;display:flex;gap:14px;align-items:center}main{max-width:1200px;margin:0 auto;padding:20px}.card{background:#fff;border:1px solid #e3e6eb;border-radius:12px;padding:16px;margin-bottom:16px}h1{font-size:17px;margin:0}h2{font-size:16px;margin:0 0 8px}h3{font-size:14px;margin:16px 0 8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #e3e6eb;padding:8px 6px;font-size:13px;word-break:break-all}.muted{color:#6b7280}a{color:#2563eb}</style></head><body><header><h1>图床配额</h1><a href='/'>返回工作台</a></header><main><div class='card'><p>本页显示各课程的每日配额汇总，以及升级后记录的逐张上传明细。博客园实际限额为账号级 500 张/日；课程级配额文件用于暂停保护，汇总数不等于账号总消耗。</p></div>" + (sections || "<div class='card muted'>暂无记录</div>") + "</main></body></html>";
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(html);
}

async function serveUi(res) {
  const html = await fs.readFile(path.join(HERE, "lib", "workbench-ui.html"), "utf8");
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }); res.end(html);
}

async function serveStatic(res, file, contentType) {
  if (!existsSync(file)) { res.writeHead(404); return res.end("not found"); }
  res.writeHead(200, { "Content-Type": contentType });
  createReadStream(file).pipe(res);
}

server.listen(PORT, HOST, () => log.ok("工作台已启动：http://" + HOST + ":" + PORT));
