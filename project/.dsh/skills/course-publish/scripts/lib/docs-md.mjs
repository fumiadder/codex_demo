// 多格式文档 → 单章合并 Markdown
import { promises as fs } from "node:fs";
import { existsSync, createReadStream, readFileSync } from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import TurndownService from "turndown";
import { pptxToImagesMd } from "./ppt-images.mjs";

const require = createRequire(import.meta.url);
const execFileP = promisify(execFile);
const FENCE = String.fromCharCode(96).repeat(3);

export const DOC_EXTS = [".md", ".markdown", ".txt", ".pdf", ".doc", ".docx", ".html", ".htm", ".pptx"];
export const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"];
export const ARCHIVE_EXTS = [".zip", ".rar", ".7z", ".tar", ".gz"];
export const SKIP_EXTS_MEDIA = [".mp4", ".mov", ".mkv", ".avi", ".mp3", ".m4a", ".wav", ".flv", ".wmv", ".exe", ".dmg", ".pkg"];

const turndown = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced", bulletListMarker: "-" });

export function naturalSort(a, b) {
  return a.localeCompare(b, "zh-Hans-CN", { numeric: true, sensitivity: "base" });
}

// 递归遍历时剪枝的目录名：软件依赖/构建产物/版本控制等，绝非课程文档，跳过可避免数千文件的噪声与膨胀
export const PRUNE_DIRS = new Set([
  "node_modules", ".git", "dist", "build", ".next", "out", "coverage",
  ".venv", "venv", "__pycache__", ".cache", ".idea", ".vscode", ".turbo",
  ".gradle", "target", "bin", "obj",
  // Python 打包/运行库目录（PyInstaller _internal、时区库、依赖元数据、vendor）
  "_internal", "tzdata", "_vendor", "vendor",
]);

export async function walk(dir) {
  const out = [];
  async function rec(d) {
    const entries = await fs.readdir(d, { withFileTypes: true });
    entries.sort((x, y) => naturalSort(x.name, y.name));
    for (const e of entries) {
      const full = path.join(d, e.name);
      if (e.isDirectory()) {
        if (PRUNE_DIRS.has(e.name)) continue;
        if (/\.(dist-info|egg-info|egg)$/.test(e.name)) continue; // Python 打包元数据目录
        await rec(full);
      } else out.push(full);
    }
  }
  await rec(dir);
  return out;
}

export function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash("sha256");
    const s = createReadStream(file);
    s.on("error", reject);
    s.on("data", (c) => h.update(c));
    s.on("end", () => resolve(h.digest("hex")));
  });
}

// 标题层级整体下移一级（# → ##），避免与章标题冲突；跳过代码围栏与缩进代码块
export function demoteHeadings(md) {
  const lines = md.split("\n");
  let fence = null; // 当前围栏标记：FENCE（反引号）或 TILDE
  const TILDE = "~~~";
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trimStart();
    if (trimmed.startsWith(FENCE) || trimmed.startsWith(TILDE)) {
      const marker = trimmed.startsWith(TILDE) ? TILDE : FENCE;
      fence = fence === marker ? null : fence || marker;
      continue;
    }
    if (fence) continue;
    if (/^\s{4,}\S/.test(lines[i])) continue;
    lines[i] = lines[i].replace(/^(#{1,5}) /, (m, hashes) => "#" + hashes + " ");
  }
  return lines.join("\n");
}

// md 里的相对图片路径解析并登记到 assets
// 兼容源 md 使用 URL 编码文件名（如 %E4%B8%8B）而磁盘是解码中文名（如下午）的情况
export function resolveImageAbs(fileDir, clean) {
  const literal = path.resolve(fileDir, clean);
  if (existsSync(literal)) return literal;
  if (clean.includes("%")) {
    try {
      const decoded = path.resolve(fileDir, decodeURIComponent(clean));
      if (existsSync(decoded)) return decoded;
    } catch { /* 非法编码则忽略，按缺失处理 */ }
  }
  return literal; // 不存在时返回原路径，registerImage 会返回 null
}

function rewriteMdImages(md, fileDir, imageRegistry) {
  let out = md.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m, alt, src) => {
    if (/^https?:\/\//.test(src)) return m;
    const clean = src.replace(/^["']|["']$/g, "").split(/[?#]/)[0];
    const abs = resolveImageAbs(fileDir, clean);
    const rel = registerImage(abs, imageRegistry);
    return rel ? "![" + alt + "](" + rel + ")" : m;
  });
  // 兼容 md 内嵌原生 <img src="./assets/x.png">（含 URL 编码文件名），仅替换 src，保留 alt/style 等属性
  out = out.replace(/<img\b[^>]*>/gi, (tag) => {
    const m = tag.match(/\bsrc=(["'])(.*?)\1/i);
    if (!m || /^https?:/i.test(m[2])) return tag;
    const clean = m[2].split(/[?#]/)[0];
    const abs = resolveImageAbs(fileDir, clean);
    const rel = registerImage(abs, imageRegistry);
    return rel ? tag.replace(m[2], rel) : tag;
  });
  return out;
}

function registerImage(abs, imageRegistry) {
  if (!existsSync(abs)) return null;
  const hash = crypto.createHash("sha256").update(readFileSync(abs)).digest("hex");
  if (!imageRegistry.has(hash)) {
    const ext = path.extname(abs).toLowerCase();
    imageRegistry.set(hash, { abs, rel: "assets/" + hash.slice(0, 16) + ext, hash });
  }
  return imageRegistry.get(hash).rel;
}

async function convertMd(file) {
  const raw = await fs.readFile(file, "utf8");
  return normalizeLineEndings(raw); // 忠实原文：仅统一换行，防止未闭合代码围栏吞掉后续内容
}

// 将 \r\n / \r 统一为 \n；课件来自 Windows，未闭合 ``` 围栏在 CRLF 下会把后续图片吞进代码块导致裂图
export function normalizeLineEndings(text) {
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

async function convertTxt(file) {
  return fs.readFile(file, "utf8"); // 原文直出
}

async function convertHtml(file, imageRegistry) {
  const html = await fs.readFile(file, "utf8");
  const fileDir = path.dirname(file);
  const td = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });
  td.addRule("localImages", {
    filter: (node) => node.nodeName === "IMG" && node.getAttribute("src") && !/^https?:/.test(node.getAttribute("src")),
    replacement: (content, node) => {
      const abs = resolveImageAbs(fileDir, node.getAttribute("src").split(/[?#]/)[0]);
      const rel = registerImage(abs, imageRegistry);
      return rel ? "![" + (node.getAttribute("alt") || "") + "](" + rel + ")" : "";
    },
  });
  return td.turndown(html);
}

async function convertDocx(file, imageRegistry) {
  const mammoth = await import("mammoth");
  const result = await mammoth.convertToHtml(
    { path: file },
    {
      convertImage: mammoth.images.imgElement(async (image) => {
        const buf = await image.read();
        const hash = crypto.createHash("sha256").update(buf).digest("hex");
        const ext = (image.contentType || "image/png").split("/")[1].replace("jpeg", "jpg");
        if (!imageRegistry.has(hash)) {
          imageRegistry.set(hash, { buffer: buf, rel: "assets/" + hash.slice(0, 16) + "." + ext, hash });
        }
        return { src: imageRegistry.get(hash).rel };
      }),
    },
  );
  return { md: turndown.turndown(result.value), messages: result.messages };
}

async function convertDoc(file) {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "doc2md-"));
  try {
    await execFileP("/usr/bin/textutil", ["-convert", "html", "-output", path.join(tmp, "out.html"), file]);
    const html = await fs.readFile(path.join(tmp, "out.html"), "utf8");
    return turndown.turndown(html);
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
}

async function convertPdf(file) {
  const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const pdfjsRoot = path.dirname(require.resolve("pdfjs-dist/package.json"));
  const data = new Uint8Array(await fs.readFile(file));
  const doc = await pdfjs
    .getDocument({
      data,
      useSystemFonts: true,
      cMapUrl: path.join(pdfjsRoot, "cmaps") + path.sep,
      cMapPacked: true,
      standardFontDataUrl: path.join(pdfjsRoot, "standard_fonts") + path.sep,
    })
    .promise;
  const parts = [];
  let textChars = 0;
  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const tc = await page.getTextContent();
    const text = tc.items.map((it) => ("str" in it ? it.str : "")).join(" ");
    textChars += text.trim().length;
    parts.push(text.trim());
  }
  if (textChars < 20) {
    const err = new Error("SCANNED_PDF");
    err.code = "SCANNED_PDF";
    err.file = file;
    throw err;
  }
  return parts.join("\n\n");
}

// pptx 本质是 zip：抽取幻灯片文本大纲
async function convertPptx(file) {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "pptx-"));
  try {
    // zip-slip 校验：拒绝绝对路径/.. 条目
    const list = await execFileP("/usr/bin/unzip", ["-Z1", file]).catch(() => ({ stdout: "" }));
    const unsafe = list.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
      .filter((n) => n.startsWith("/") || n.startsWith("\\") || n.split(/[\\/]/).includes(".."));
    if (unsafe.length) throw new Error("pptx 含不安全路径，已拒绝解析: " + unsafe.slice(0, 3).join(", "));
    await execFileP("/usr/bin/unzip", ["-oq", file, "-d", tmp]).catch(async () => {
      await execFileP("tar", ["-xf", file, "-C", tmp]);
    });
    const slideDir = path.join(tmp, "ppt", "slides");
    if (!existsSync(slideDir)) return "";
    const slides = (await fs.readdir(slideDir))
      .filter((n) => /^slide\d+\.xml$/.test(n))
      .sort((a, b) => Number(a.match(/\d+/)[0]) - Number(b.match(/\d+/)[0]));
    const parts = [];
    for (const s of slides) {
      const xml = await fs.readFile(path.join(slideDir, s), "utf8");
      const texts = [...xml.matchAll(/<a:t>([^<]*)<\/a:t>/g)].map((m) => m[1].trim()).filter(Boolean);
      const n = s.match(/\d+/)[0];
      parts.push(texts.join("\n"));
    }
    return parts.join("\n\n");
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
}

// 一级标题展示用文件名：剥掉解压包装层 _extracted/<压缩包名>/，保留课件包内真实相对路径
export function displayRel(rel) {
  const parts = rel.split(/[\\/]/);
  if (parts[0] === "_extracted" && parts.length > 2) return parts.slice(2).join("/");
  return rel;
}

// 项目附属元文档：软件源码/依赖包里的 README/LICENSE 等说明文件，非课件正文，不汇总
// 仅按文件名识别（readme/license/notice/changelog/requirements/setup/install 开头）
export const PROJECT_META_NAMES = /^(readme|license|notice|changelog|requirements|setup|install|entry_points|top_level|record|metadata|wheel|installer|lorem ipsum)\b/i;
function isProjectMetaFile(rel) {
  const file = (rel.split(/[\\/]/).pop() || "");
  const base = file.replace(/\.[^.]+$/, "");
  if (PROJECT_META_NAMES.test(base)) return true;
  const name = file.toLowerCase();
  if (name === "index.html" || name === "index.htm") return true; // 前端入口页，非课件文档
  if (/_doc\.(html|htm|txt)$/.test(name)) return true; // 爬虫抓取的接口文档（如 hifly_doc.html/txt）
  return false;
}

const TXT_FOLD_MIN_CHARS = 1000;
const TXT_FOLD_MIN_LINES = 30;
const PROMPT_FOLD_MIN_CHARS = 1000;
const PROMPT_FOLD_MIN_LINES = 30;
// 课件里的长提示词/小说原文/剧本/规范参考：整篇默认收起，但内部继续按 Markdown 结构渲染。
const PROMPT_FOLD_PATH = /提示词|小说原文|分镜描述词|剧本|资产信息|短剧内容审核|火山引擎剧本格式/i;
const PROMPT_FOLD_EXCLUDE_PATH = /操作记录|进阶内容/i;
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  // 博客园数据库会丢弃部分非 BMP 字符（如 emoji）；数字实体入库后仍由浏览器还原显示。
  .replace(/[\u{10000}-\u{10FFFF}]/gu, (ch) => "&#x" + ch.codePointAt(0).toString(16).toUpperCase() + ";");

// 长 txt 仅改变展示方式：默认折叠，原文逐字保留在 pre 中并自动换行，避免单行工作流撑长页面。
export function foldLongTextDocument(rel, body) {
  const text = String(body || "").trim();
  const lines = text ? text.split(/\r?\n/).length : 0;
  if (text.length < TXT_FOLD_MIN_CHARS && lines < TXT_FOLD_MIN_LINES) return body;
  const name = displayRel(rel).split("/").pop() || "文本";
  return '<details class="course-txt-fold"><summary>展开查看 ' + escapeHtml(name) + '</summary>\n'
    + '<pre style="white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere;">'
    + escapeHtml(text) + '</pre>\n</details>';
}

export function foldLongTxtDocument(rel, body) {
  if (!/\.txt$/i.test(rel)) return body;
  return foldLongTextDocument(rel, body);
}

// 长参考文档（提示词/小说/剧本/规范）默认折叠。这里不能套 <pre>：
// <details> 与正文之间留空行，交给 marked 继续解析标题、段落、列表和表格，折叠后仍有结构。
export function shouldFoldStructuredDocument(rel, body) {
  if (!PROMPT_FOLD_PATH.test(rel) || PROMPT_FOLD_EXCLUDE_PATH.test(rel)) return false;
  const text = String(body || "").trim();
  const lines = text ? text.split(/\r?\n/).length : 0;
  return text.length >= PROMPT_FOLD_MIN_CHARS || lines >= PROMPT_FOLD_MIN_LINES;
}

export function foldStructuredDocument(rel, body) {
  const text = String(body || "").trim();
  const lines = text ? text.split(/\r?\n/).length : 0;
  const name = displayRel(rel).split("/").pop() || "参考内容";
  let kind = "参考资料";
  if (/提示词/.test(rel)) kind = "提示词";
  else if (/小说原文/.test(rel)) kind = "小说原文";
  else if (/分镜描述词|剧本/.test(rel)) kind = "剧本/分镜";
  else if (/短剧内容审核/.test(rel)) kind = "规范参考";
  else if (/资产信息/.test(rel)) kind = "资产信息";
  return '<details class="course-prompt-fold"><summary>展开查看 ' + escapeHtml(kind) + '：' + escapeHtml(name)
    + '（' + lines + ' 行）</summary>\n\n' + text + '\n\n</details>';
}

// 主入口：把章节目录合并为单个 MD，返回 { mdPath, imageRegistry, report }
export async function buildChapterMarkdown({ chapterDir, outDir, chapterTitle, frontmatter = {}, includeDocs = null }) {
  await fs.mkdir(path.join(outDir, "assets"), { recursive: true });
  const imageRegistry = new Map();
  const report = { converted: [], skipped: [], warnings: [], scannedPdf: [] };
  const allFiles = await walk(chapterDir);
  const allowedDocs = includeDocs ? new Set(includeDocs.map((rel) => rel.replace(/\\/g, "/"))) : null;
  const sections = [];

  for (const abs of allFiles) {
    const ext = path.extname(abs).toLowerCase();
    const rel = path.relative(chapterDir, abs);
    if (IMG_EXTS.includes(ext)) {
      registerImage(abs, imageRegistry); // 独立图片仅登记去重，正文不额外加备注
      report.converted.push(rel);
      continue;
    }
    if (SKIP_EXTS_MEDIA.includes(ext)) {
      report.skipped.push({ file: rel, reason: "音视频/安装包，不汇总" });
      continue;
    }
    if (ARCHIVE_EXTS.includes(ext)) {
      report.warnings.push({ file: rel, reason: "压缩包应在下载阶段解压；如看到此条说明漏解压" });
      continue;
    }
    const relPosix = rel.split(path.sep).join("/");
    // includeDocs 显式点名的非文档文本（如工作流/配置文件导出）按纯文本原文处理，仅在计划显式指定时生效
    const allowExplicitText = Boolean(allowedDocs && allowedDocs.has(relPosix) && !DOC_EXTS.includes(ext));
    if (!DOC_EXTS.includes(ext) && !allowExplicitText) {
      report.skipped.push({ file: rel, reason: "不支持的扩展名 " + ext });
      continue;
    }
    if (allowedDocs && !allowedDocs.has(relPosix)) {
      report.skipped.push({ file: rel, reason: "不在本章主讲义清单，不汇总" });
      continue;
    }
    if (isProjectMetaFile(rel)) {
      report.skipped.push({ file: rel, reason: "源码/项目附属说明文档，不汇总" });
      continue;
    }
    try {
      let body;
      const messages = [];
      if (ext === ".md" || ext === ".markdown") body = await convertMd(abs);
      else if (ext === ".txt" || allowExplicitText) body = await convertTxt(abs);
      else if (ext === ".html" || ext === ".htm") body = await convertHtml(abs, imageRegistry);
      else if (ext === ".docx") {
        const r = await convertDocx(abs, imageRegistry);
        body = r.md;
        messages.push(...r.messages);
      } else if (ext === ".doc") body = await convertDoc(abs);
      else if (ext === ".pdf") body = await convertPdf(abs);
      else if (ext === ".pptx") {
        // PPT 优先逐页转图片（LibreOffice+pymupdf）；无渲染环境时降级为文本抽取。
        // 安全类错误（不安全路径）必须抛出，不可降级放过恶意包。
        try {
          body = await pptxToImagesMd(abs, imageRegistry);
        } catch (err) {
          if (/不安全路径/.test(String(err && err.message))) throw err;
          messages.push("PPT 图片渲染不可用，降级为文本抽取: " + String((err && err.message) || err).slice(0, 120));
          body = await convertPptx(abs);
        }
      }
      body = rewriteMdImages(body, path.dirname(abs), imageRegistry);
      if (ext === ".txt") body = foldLongTxtDocument(rel, body);
      else if (allowExplicitText) body = foldLongTextDocument(rel, body);
      else if (shouldFoldStructuredDocument(rel, body)) body = foldStructuredDocument(rel, body);
      sections.push({ rel, body });
      report.converted.push(rel);
      if (messages.length) report.warnings.push({ file: rel, reason: messages.slice(0, 5).map((m) => m.message).join("; ") });
    } catch (e) {
      if (e.code === "SCANNED_PDF") {
        report.scannedPdf.push(rel);
        report.warnings.push({ file: rel, reason: "扫描版 PDF 无文本层，已挂起等待人工处理" });
      } else {
        report.warnings.push({ file: rel, reason: "转换失败：" + (e.message || e) });
      }
    }
  }

  for (const [, meta] of imageRegistry) {
    const target = path.join(outDir, meta.rel);
    if (!existsSync(target)) {
      const buf = meta.buffer ?? (await fs.readFile(meta.abs));
      await fs.writeFile(target, buf);
    }
  }

  // 忠实原文：单文件直接输出原文；多文件才以「# 文件名」一级标题区分，除此之外不加任何内容
  const docs = sections.filter((s) => s.body && s.body.trim());
  let md;
  if (docs.length <= 1) {
    md = docs.length ? docs[0].body.trim() + "\n" : "";
  } else {
    md = docs.map((s) => "# " + displayRel(s.rel) + "\n\n" + s.body.trim()).join("\n\n") + "\n";
  }
  md = normalizeLineEndings(md); // 统一换行（覆盖 docx/html/pdf 等转换器残留 \r），防止未闭合围栏吞图
  const slug = (frontmatter.slug || chapterTitle).replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
  const mdPath = path.join(outDir, String(frontmatter.index ?? 0).padStart(2, "0") + "-" + slug + ".md");
  await fs.writeFile(mdPath, md);
  return { mdPath, imageRegistry, report };
}
