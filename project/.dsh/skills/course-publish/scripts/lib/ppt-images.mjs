import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

const execFileP = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
// 仓库根 .venv 的 python（含 pymupdf）
const VENV_PY = path.resolve(HERE, "../../../../../.venv/bin/python");
const PY = existsSync(VENV_PY) ? VENV_PY : "python3";

function sofficeBin() {
  const cands = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    "soffice",
    "libreoffice",
  ];
  for (const c of cands) if (existsSync(c)) return c;
  return "soffice"; // 让 PATH 兜底
}

// 把 PPT/PPTX 每页渲染为 PNG，注册进 imageRegistry，返回按页排列的图片 markdown。
// 复用现有 assets/ content-hash 管线：合并产物里是本地 assets/ 引用，暂存时统一上传图床并改写为在线 URL。
export async function pptxToImagesMd(file, imageRegistry) {
  // zip-slip 前置校验：拒绝绝对路径/.. 条目（在解压/渲染前拦截恶意包）
  const list = await execFileP("/usr/bin/unzip", ["-Z1", file]).catch(() => ({ stdout: "" }));
  const unsafe = list.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
    .filter((n) => n.startsWith("/") || n.startsWith("\\") || n.split(/[\\/]/).includes(".."));
  if (unsafe.length) throw new Error("pptx 含不安全路径，已拒绝解析: " + unsafe.slice(0, 3).join(", "));
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "pptimg-"));
  try {
    const soffice = sofficeBin();
    // PPT -> PDF
    await execFileP(soffice, ["--headless", "--convert-to", "pdf", "--outdir", tmp, file], { timeout: 180000 });
    const base = path.basename(file).replace(/\.(pptx?|PPTX?)$/, "");
    let pdf = path.join(tmp, base + ".pdf");
    if (!existsSync(pdf)) {
      const pdfs = (await fs.readdir(tmp)).filter((f) => f.toLowerCase().endsWith(".pdf"));
      if (!pdfs.length) throw new Error("LibreOffice 未产出 PDF");
      pdf = path.join(tmp, pdfs[0]);
    }
    // PDF -> 每页 PNG（pymupdf）
    const script = `
import sys, json, pymupdf
pdf, outdir = sys.argv[1], sys.argv[2]
doc = pymupdf.open(pdf)
paths = []
for i, page in enumerate(doc):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))  # 2x 清晰度
    p = f"{outdir}/slide-{i+1:03d}.png"
    pix.save(p)
    paths.append(p)
print(json.dumps(paths))
`;
    const { stdout } = await execFileP(PY, ["-c", script, pdf, tmp], { timeout: 180000, maxBuffer: 16 * 1024 * 1024 });
    const slidePaths = JSON.parse(stdout.trim().split("\n").pop());
    const parts = [];
    for (let i = 0; i < slidePaths.length; i++) {
      const buf = await fs.readFile(slidePaths[i]);
      const hash = crypto.createHash("sha256").update(buf).digest("hex");
      if (!imageRegistry.has(hash)) {
        imageRegistry.set(hash, { buffer: buf, rel: "assets/" + hash.slice(0, 16) + ".png", hash });
      }
      parts.push("![第 " + (i + 1) + " 页](" + imageRegistry.get(hash).rel + ")");
    }
    return parts.join("\n\n");
  } finally {
    await fs.rm(tmp, { recursive: true, force: true }).catch(() => {});
  }
}

// 供工作台预览：把 PPT 每页渲染为 PNG 到 outDir，返回相对文件名数组（不注册上传）。
export async function pptxRenderToDir(file, outDir) {
  await fs.mkdir(outDir, { recursive: true });
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "pptprev-"));
  try {
    const soffice = sofficeBin();
    await execFileP(soffice, ["--headless", "--convert-to", "pdf", "--outdir", tmp, file], { timeout: 180000 });
    const base = path.basename(file).replace(/\.(pptx?|PPTX?)$/, "");
    let pdf = path.join(tmp, base + ".pdf");
    if (!existsSync(pdf)) {
      const pdfs = (await fs.readdir(tmp)).filter((f) => f.toLowerCase().endsWith(".pdf"));
      if (!pdfs.length) throw new Error("LibreOffice 未产出 PDF");
      pdf = path.join(tmp, pdfs[0]);
    }
    const script = `
import sys, json, pymupdf
pdf, outdir = sys.argv[1], sys.argv[2]
doc = pymupdf.open(pdf)
paths = []
for i, page in enumerate(doc):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
    p = f"{outdir}/slide-{i+1:03d}.png"
    pix.save(p)
    paths.append(f"slide-{i+1:03d}.png")
print(json.dumps(paths))
`;
    const { stdout } = await execFileP(PY, ["-c", script, pdf, outDir], { timeout: 180000, maxBuffer: 16 * 1024 * 1024 });
    return JSON.parse(stdout.trim().split("\n").pop());
  } finally {
    await fs.rm(tmp, { recursive: true, force: true }).catch(() => {});
  }
}
