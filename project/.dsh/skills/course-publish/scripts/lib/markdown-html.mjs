// Markdown 发布渲染：博客园 MetaWeblog 的 description 按 HTML 解析，
// 必须先剥离 YAML frontmatter 并把 Markdown 转成 HTML，否则 #、![]() 等会原样显示成文本（排版错乱/裂图根因）。
// 图片引用改写统一复用 images-cnblogs.mjs 的 rewriteImageRefs（Map 版，覆盖三类语法）。
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: false });

// 课件里存在未闭合的 ``` 代码围栏（作者留一个未闭合围栏展示片段）。
// 不处理会把后续图片/标题全吞进代码块（渲染裂图）。保守兜底：
// 仅当某个围栏开启后**直到文末都没有闭合**时，把那一行孤立开启围栏降级为普通行（去掉反引号），
// 让它后续的内容按普通 Markdown 渲染；已正确闭合的围栏不受影响。
export function closeDanglingFences(md) {
  const text = String(md || "");
  const lines = text.split("\n");
  let fence = null; // 当前打开的围栏标记（``` 或 ~~~）
  let openIdx = -1; // 开启行索引
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trimStart();
    if (/^(```+|~~~+)/.test(t)) {
      const marker = t.startsWith("~") ? "~~~" : "```";
      if (fence === marker) { fence = null; openIdx = -1; }
      else if (!fence) { fence = marker; openIdx = i; }
    }
  }
  if (fence && openIdx >= 0) {
    // 孤立开启围栏：去除该行反引号，让其后的内容正常渲染（不吞图）
    lines[openIdx] = lines[openIdx].replace(/^(```+|~~~+)\s*(.*)$/, "$2").trimEnd();
  }
  return lines.join("\n");
}

// 去掉文档头部的 YAML frontmatter（---\n...\n---）
export function stripFrontmatter(md) {
  const text = String(md || "").replace(/^\uFEFF/, "");
  const m = text.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/);
  return m ? text.slice(m[0].length) : text;
}

// Markdown -> 博客园可发布的 HTML
export function markdownToHtml(md) {
  const body = closeDanglingFences(stripFrontmatter(md));
  return dedupeHtmlImages(marked.parse(body, { async: false }));
}

// 最终发布防线：引用式图片、原始 HTML、未闭合围栏等经过 marked 后，同一 src 也只渲染一次。
// 代码块中的 <img> 已被转义，不会被此正则误伤。
export function dedupeHtmlImages(html) {
  const seen = new Set();
  return String(html || "").replace(/<img\b[^>]*>/gi, (tag) => {
    const m = tag.match(/\bsrc=["\']([^"\']+)["\']/i) || tag.match(/\bsrc=([^\s>]+)/i);
    if (!m) return tag;
    const key = m[1].trim().replace(/&amp;/g, "&");
    if (seen.has(key)) return "";
    seen.add(key);
    return tag;
  });
}
