/*!
 * ZhixuMarkdown — 知序工作台安全 Markdown 渲染器（同站 vendored 库）
 *
 * 用途：
 *   把 Markdown 文本渲染为真实 DOM 节点（DocumentFragment），调用方 append 即可。
 *   挂载全局 window.ZhixuMarkdown，公开契约：
 *     render(markdownText)       -> DocumentFragment  块级 + 行内完整渲染
 *     renderInline(text)         -> DocumentFragment  仅行内语法（标题/列表内复用）
 *
 * 安全模型（本文件的核心不变量）：
 *   1. 仅使用 document.createElement / createTextNode / createDocumentFragment
 *      等 DOM API 构建节点；所有用户文本一律经 textContent 或文本节点落地。
 *      绝不使用 innerHTML / outerHTML / document.write / insertAdjacentHTML，
 *      因此原始 HTML（<script>、<img onerror=...> 等）只能按文本显示，
 *      不存在任何 HTML 透传面，< > & 等字符原样可见。
 *   2. 不使用 eval / new Function / setTimeout(字符串)，不绑定任何 on* 事件属性，
 *      在严格 CSP（script-src 'self'）下可直接运行，零外部资源、零网络请求。
 *   3. 链接仅放行 http:、https:、mailto: 三种协议，用 URL 构造器解析校验；
 *      javascript:、data:、vbscript: 等一律降级为不可点击的纯文本。
 *      放行的 a 节点统一带 rel="noopener noreferrer"，http/https 外链 target=_blank。
 *   4. 围栏代码块与行内代码完全按文本处理，不解析其中任何 Markdown/HTML。
 *   5. 容错：未闭合标记与畸形输入不抛异常、不产生半截坏节点；超过 100KB 截断。
 *
 * 这是知序工作台自研实现，未拷贝任何第三方代码。
 * MIT License，版权所有 © 2026 知序工作台 contributors（详见同目录 LICENSE）。
 */
(function (global) {
  "use strict";

  // node --check 等非浏览器环境只做语法检查；加载期不触碰 document。
  if (typeof window === "undefined" || typeof document === "undefined" || !global) {
    return;
  }

  var MAX_INPUT = 100 * 1024; // 输入长度上限（字符），超出先截断保证耗时有上界
  var MAX_BLOCK_DEPTH = 20; // 块级递归深度上限，畸形嵌套不爆栈
  var MAX_INLINE_DEPTH = 20; // 行内嵌套深度上限
  var MAX_SCAN = 512; // 括号配对等前向扫描的最大窗口
  var SAFE_PROTOCOLS = ["http:", "https:", "mailto:"];
  var TRUNCATED_NOTE = "（内容过长，已截断）";

  // 与 public/app.js 的 el() 同一风格：DOM 创建 + className + textContent。
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function frag() {
    return document.createDocumentFragment();
  }
  function textNode(value) {
    return document.createTextNode(value);
  }
  function isSpace(ch) {
    return ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f" || ch === "\v";
  }
  function isAlnum(ch) {
    return /[A-Za-z0-9]/.test(ch);
  }
  function leadingSpaces(line) {
    var n = 0;
    while (n < line.length && line.charAt(n) === " ") n++;
    return n;
  }

  // ---------- 链接安全 ----------

  // 返回可安全赋给 a.href 的字符串；协议不在白名单或 URL 畸形时返回 null。
  function safeHref(raw) {
    if (typeof raw !== "string") return null;
    var trimmed = raw.trim();
    if (!trimmed || trimmed.length > 2048) return null;
    if (/[\u0000-\u001f\u007f]/.test(trimmed)) return null; // 拒绝控制字符
    var url;
    try {
      url = new URL(trimmed);
    } catch (err) {
      return null; // 相对路径、畸形 URL 均不生成可点链接
    }
    if (SAFE_PROTOCOLS.indexOf(url.protocol) === -1) return null;
    return url.href;
  }

  function makeAnchor(href, isMail) {
    var anchor = el("a");
    anchor.href = href; // 仅属性赋值，来源已经过 URL 白名单校验
    anchor.rel = "noopener noreferrer";
    if (!isMail) anchor.target = "_blank"; // mailto 不需要新窗口
    return anchor;
  }

  // ---------- 行内解析 ----------

  // 在 src[start..] 寻找长度恰好为 run 的反引号围栏（更长的串不算闭合）。
  // 扫描窗口受限，保证重复的未闭合标记不会退化为 O(n^2)。
  function findCodeSpanClose(src, start, run) {
    var max = Math.min(src.length, start + MAX_SCAN);
    var j = start;
    while (j < max) {
      if (src.charAt(j) !== "`") {
        j++;
        continue;
      }
      var len = 0;
      while (j + len < max && src.charAt(j + len) === "`") len++;
      if (len === run) return j;
      j += len;
    }
    return -1;
  }

  function codeSpanText(raw) {
    var s = raw.replace(/\r?\n/g, " ");
    // CommonMark 风格：两侧各一个空格且内容不全是空格时，去掉这一对空格。
    if (
      s.length >= 2 &&
      s.charAt(0) === " " &&
      s.charAt(s.length - 1) === " " &&
      s.replace(/ /g, "") !== ""
    ) {
      s = s.slice(1, -1);
    }
    return s;
  }

  function findBracketClose(src, openIndex) {
    var depth = 0;
    var max = Math.min(src.length, openIndex + MAX_SCAN);
    for (var j = openIndex; j < max; j++) {
      var ch = src.charAt(j);
      if (ch === "\\") {
        j++;
        continue;
      }
      if (ch === "[") depth++;
      else if (ch === "]") {
        depth--;
        if (depth === 0) return j;
      }
    }
    return -1;
  }

  function findParenClose(src, openParen) {
    var depth = 0;
    var max = Math.min(src.length, openParen + MAX_SCAN);
    for (var j = openParen; j < max; j++) {
      var ch = src.charAt(j);
      if (ch === "\\") {
        j++;
        continue;
      }
      if (ch === "(") depth++;
      else if (ch === ")") {
        depth--;
        if (depth === 0) return j;
      }
    }
    return -1;
  }

  // 解析 [文字](url)，返回 { node, next }；不满足语法返回 null。
  // 危险协议不返回 null，而是返回原样纯文本节点（不可点）。
  function tryLink(src, i, depth, options) {
    if (options.forbidLinks) return null;
    var labelEnd = findBracketClose(src, i);
    if (labelEnd < 0 || src.charAt(labelEnd + 1) !== "(") return null;
    var parenEnd = findParenClose(src, labelEnd + 1);
    if (parenEnd < 0) return null;
    var label = src.slice(i + 1, labelEnd);
    if (!label.trim()) return null;
    var inner = src.slice(labelEnd + 2, parenEnd).trim();
    if (!inner) return null;
    var destination = inner.split(/\s+/)[0]; // 其余部分视为可选 title，本子集忽略
    if (destination.charAt(0) === "<" && destination.charAt(destination.length - 1) === ">") {
      destination = destination.slice(1, -1);
    }
    var href = safeHref(destination);
    if (!href) {
      // 安全红线：危险/畸形链接降级为整段原样纯文本，绝不生成可点节点。
      return { node: textNode(src.slice(i, parenEnd + 1)), next: parenEnd + 1 };
    }
    var anchor = makeAnchor(href, /^mailto:/i.test(href));
    if (depth < MAX_INLINE_DEPTH) {
      parseInline(anchor, label, depth + 1, { forbidLinks: true });
    } else {
      anchor.textContent = label;
    }
    return { node: anchor, next: parenEnd + 1 };
  }

  // 解析 **粗体**、*斜体*、_斜体_；未闭合或不满足侧翼规则返回 null（标记按字面量落地）。
  function tryEmphasis(src, i, depth) {
    if (depth >= MAX_INLINE_DEPTH) return null;
    var marker = src.charAt(i);
    var runLen = 1;
    if (marker === "*" && src.charAt(i + 1) === "*") runLen = 2;
    var afterOpen = src.charAt(i + runLen);
    if (afterOpen === "" || isSpace(afterOpen)) return null;
    if (marker === "_" && i > 0 && isAlnum(src.charAt(i - 1))) return null; // 词内 foo_bar 不解析

    var close = -1;
    var j = i + runLen;
    var scanMax = Math.min(src.length, i + runLen + MAX_SCAN);
    while (j < scanMax) {
      if (src.charAt(j) !== marker) {
        j++;
        continue;
      }
      if (marker === "*") {
        var doubled = src.charAt(j + 1) === "*";
        if (runLen === 2) {
          if (doubled) {
            if (!isSpace(src.charAt(j - 1))) {
              close = j;
              break;
            }
            j += 2;
            continue;
          }
          j++;
          continue;
        }
        // 单 * 闭合时，跳过成对的 **，避免吃掉粗体标记
        if (doubled) {
          j += 2;
          continue;
        }
      }
      var prev = src.charAt(j - 1);
      var next = src.charAt(j + 1);
      if (isSpace(prev)) {
        j++;
        continue;
      }
      if (marker === "_" && isAlnum(next)) {
        j++;
        continue;
      }
      close = j;
      break;
    }
    if (close === -1 || close <= i + runLen) return null;

    var inner = src.slice(i + runLen, close);
    var node = el(runLen === 2 ? "strong" : "em");
    parseInline(node, inner, depth + 1, {});
    return { node: node, next: close + runLen };
  }

  // 裸 URL / 裸邮箱自动链接；不合法或处于词中间时返回 null。
  function tryBareUrl(src, i) {
    if (i > 0 && /[A-Za-z0-9@]/.test(src.charAt(i - 1))) return null;
    var rest = src.slice(i);
    var m = /^(https?:\/\/|mailto:)[^\s<>\[\]"'`]+/i.exec(rest);
    var raw = null;
    var isMail = false;
    if (m) {
      raw = m[0];
      isMail = /^mailto:/i.test(raw);
    } else {
      var email = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/.exec(rest);
      if (email) {
        raw = email[0];
        isMail = true;
      }
    }
    if (!raw) return null;
    // 去掉结尾不属于 URL 的标点；右括号需按配对情况裁剪
    while (raw.length > 1 && /[.,!?;:]/.test(raw.charAt(raw.length - 1))) {
      raw = raw.slice(0, -1);
    }
    while (
      raw.charAt(raw.length - 1) === ")" &&
      raw.split("(").length - 1 < raw.split(")").length - 1
    ) {
      raw = raw.slice(0, -1);
    }
    var href = safeHref(isMail && !/^mailto:/i.test(raw) ? "mailto:" + raw : raw);
    if (!href) return null;
    var anchor = makeAnchor(href, isMail);
    anchor.textContent = raw; // URL 本身作为链接文字，按文本落地
    return { node: anchor, next: i + raw.length };
  }

  // 行内主循环：把节点追加进 parent。options.forbidLinks 时链接语法按字面量处理。
  function parseInline(parent, src, depth, options) {
    options = options || {};
    var i = 0;
    var buffer = "";
    function flush() {
      if (buffer) {
        parent.appendChild(textNode(buffer));
        buffer = "";
      }
    }
    while (i < src.length) {
      var ch = src.charAt(i);

      // 反斜杠转义：\* \_ \` \\ \[ \] 等
      if (ch === "\\" && i + 1 < src.length && /[\\`*_{}\[\]()#+\-.!~<>|]/.test(src.charAt(i + 1))) {
        buffer += src.charAt(i + 1);
        i += 2;
        continue;
      }

      // 行内代码：内容原样文本，不解析任何语法
      if (ch === "`") {
        var run = 0;
        while (i + run < src.length && src.charAt(i + run) === "`") run++;
        var closePos = findCodeSpanClose(src, i + run, run);
        if (closePos >= 0) {
          flush();
          var code = el("code", null, codeSpanText(src.slice(i + run, closePos)));
          parent.appendChild(code);
          i = closePos + run;
          continue;
        }
        buffer += src.slice(i, i + run); // 未闭合：反引号按字面量
        i += run;
        continue;
      }

      // [文字](url)
      if (ch === "[" && !options.forbidLinks) {
        var link = tryLink(src, i, depth, options);
        if (link) {
          flush();
          parent.appendChild(link.node);
          i = link.next;
          continue;
        }
      }

      // 粗体 / 斜体
      if (ch === "*" || ch === "_") {
        var em = tryEmphasis(src, i, depth);
        if (em) {
          flush();
          parent.appendChild(em.node);
          i = em.next;
          continue;
        }
      }

      // 裸链接
      if (!options.forbidLinks) {
        var bare = tryBareUrl(src, i);
        if (bare) {
          flush();
          parent.appendChild(bare.node);
          i = bare.next;
          continue;
        }
      }

      // 默认：字面量字符。< > & 等全部随文本节点落地，天然不可执行。
      buffer += ch;
      i++;
    }
    flush();
  }

  // ---------- 块级解析 ----------

  function matchFence(s) {
    var m = /^(`{3,}|~{3,})[ \t]*([^\n`]*?)[ \t]*$/.exec(s);
    if (!m) return null;
    var info = m[2] || "";
    if (!/^[A-Za-z0-9_+#.\-]{0,32}$/.test(info)) info = ""; // info 只放行安全类名字符
    return { ch: m[1].charAt(0), len: m[1].length, info: info };
  }

  function matchClosingFence(line, ch, len) {
    var indent = leadingSpaces(line);
    if (indent > 3) return false;
    var t = line.slice(indent);
    return new RegExp("^" + (ch === "~" ? "~" : "`") + "{" + len + ",}[ \\t]*$").test(t);
  }

  function matchHeading(s) {
    var m = /^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/.exec(s);
    if (!m) return null;
    var content = m[2] === undefined ? "" : m[2];
    content = content.replace(/[ \t]+#+[ \t]*$/, "").trim();
    return { level: m[1].length, text: content };
  }

  function matchHr(s) {
    return /^([-*_])(?:[ \t]*\1){2,}[ \t]*$/.test(s);
  }

  function matchListItem(s) {
    var bullet = /^([-*+])[ \t]+(.*)$/.exec(s);
    if (bullet) {
      return {
        ordered: false,
        markerLen: bullet[0].length - bullet[2].length,
        content: bullet[2],
      };
    }
    var numbered = /^(\d{1,9}[.)])[ \t]+(.*)$/.exec(s);
    if (numbered) {
      return {
        ordered: true,
        markerLen: numbered[0].length - numbered[2].length,
        content: numbered[2],
      };
    }
    return null;
  }

  function buildCodeBlock(codeLines, info) {
    var pre = el("pre");
    var code = el("code");
    if (info) code.className = "language-" + info;
    code.textContent = codeLines.join("\n"); // 原样保留换行，不解析内部任何语法
    pre.appendChild(code);
    return pre;
  }

  // 收集一个列表段并构建 ul/ol（支持嵌套，深度受 MAX_BLOCK_DEPTH 保护）。
  function parseList(lines, start, end, depth) {
    var baseIndent = leadingSpaces(lines[start]);
    var items = [];
    var current = null;
    var i = start;
    while (i < end) {
      var line = lines[i];
      if (line.trim() === "") {
        var k = i + 1;
        while (k < end && lines[k].trim() === "") k++;
        if (k >= end) {
          i = k;
          break;
        }
        var nextIndent = leadingSpaces(lines[k]);
        var nextItem = nextIndent >= baseIndent && matchListItem(lines[k].slice(nextIndent));
        if (nextItem) {
          i = k; // 空行后仍是列表项，保留在同一段
          continue;
        }
        break; // 空行后是普通段落，列表结束
      }
      var indent = leadingSpaces(line);
      var item = indent >= baseIndent ? matchListItem(line.slice(indent)) : null;
      if (item) {
        current = {
          indent: indent,
          ordered: item.ordered,
          contentCol: indent + item.markerLen,
          text: item.content,
          extra: [],
        };
        items.push(current);
        i++;
        continue;
      }
      if (current && indent > baseIndent) {
        current.extra.push(line); // 项的续行（可能是嵌套列表或说明文字）
        i++;
        continue;
      }
      break;
    }

    var wrapper = frag();
    var idx = 0;
    while (idx < items.length) {
      var built = buildListItems(items, idx, items[idx].indent, depth);
      wrapper.appendChild(built.node);
      idx = built.next;
    }
    return { node: wrapper, next: i };
  }

  function buildListItems(items, idx, indent, depth) {
    var list = el(items[idx].ordered ? "ol" : "ul");
    var liNode = null;
    while (idx < items.length && items[idx].indent >= indent) {
      if (items[idx].indent > indent) {
        if (!liNode) break; // 畸形数据保护：悬空嵌套直接收尾
        var child = buildListItems(items, idx, items[idx].indent, depth + 1);
        liNode.appendChild(child.node);
        idx = child.next;
        continue;
      }
      var item = items[idx];
      // 同级标记类型切换（ul 中出现有序项）时拆成新列表
      if (liNode && item.ordered !== (list.tagName === "OL")) break;
      liNode = el("li");
      parseInline(liNode, item.text, 0, {});
      if (item.extra.length) {
        var dedented = item.extra.map(function (l) {
          var sp = leadingSpaces(l);
          return sp >= item.contentCol ? l.slice(item.contentCol) : l.slice(Math.min(sp, item.contentCol));
        });
        if (depth < MAX_BLOCK_DEPTH) {
          parseBlocks(liNode, dedented, 0, dedented.length, depth + 1);
        } else {
          parseInline(liNode, dedented.join(" "), 0, {});
        }
      }
      list.appendChild(liNode);
      idx++;
    }
    return { node: list, next: idx };
  }

  function parseParagraph(container, lines, i, end) {
    var gathered = [];
    while (i < end) {
      var line = lines[i];
      if (line.trim() === "") break;
      var indent = leadingSpaces(line);
      if (indent <= 3) {
        var t = line.slice(indent);
        if (
          matchFence(t) ||
          matchHeading(t) ||
          matchHr(t) ||
          t.charAt(0) === ">" ||
          matchListItem(t)
        ) {
          break; // 遇到块级起始行，段落结束
        }
      }
      gathered.push(line);
      i++;
    }
    var p = el("p");
    for (var k = 0; k < gathered.length; k++) {
      if (k > 0) {
        if (/[ \t]{2,}$/.test(gathered[k - 1])) {
          p.appendChild(el("br")); // 硬换行（行尾两空格）
        } else {
          p.appendChild(textNode(" ")); // 软换行按空格处理
        }
      }
      parseInline(p, gathered[k].replace(/[ \t]+$/, ""), 0, {});
    }
    container.appendChild(p);
    return i;
  }

  function parseBlocks(container, lines, start, end, depth) {
    var i = start;
    while (i < end) {
      var line = lines[i];
      if (line.trim() === "") {
        i++;
        continue;
      }
      var indent = leadingSpaces(line);
      if (indent > 3) {
        i = parseParagraph(container, lines, i, end); // 4 空格缩进按普通段落文本处理
        continue;
      }
      var trimmed = line.slice(indent);

      var fence = matchFence(trimmed);
      if (fence) {
        var codeLines = [];
        var k = i + 1;
        while (k < end && !matchClosingFence(lines[k], fence.ch, fence.len)) {
          codeLines.push(lines[k]);
          k++;
        }
        if (k < end) k++; // 吞掉闭合围栏；未闭合则容错到文本结尾
        container.appendChild(buildCodeBlock(codeLines, fence.info));
        i = k;
        continue;
      }

      var heading = matchHeading(trimmed);
      if (heading) {
        var h = el("h" + heading.level);
        parseInline(h, heading.text, 0, {});
        container.appendChild(h);
        i++;
        continue;
      }

      if (matchHr(trimmed)) {
        container.appendChild(el("hr"));
        i++;
        continue;
      }

      if (trimmed.charAt(0) === ">") {
        var quoteLines = [];
        while (i < end) {
          var qIndent = leadingSpaces(lines[i]);
          if (qIndent > 3) break;
          var qTrim = lines[i].slice(qIndent);
          if (qTrim.charAt(0) !== ">") break;
          var rest = qTrim.slice(1);
          if (rest.charAt(0) === " ") rest = rest.slice(1);
          quoteLines.push(rest);
          i++;
        }
        var quote = el("blockquote");
        if (depth < MAX_BLOCK_DEPTH) {
          parseBlocks(quote, quoteLines, 0, quoteLines.length, depth + 1);
        } else {
          parseInline(quote, quoteLines.join(" "), 0, {});
        }
        container.appendChild(quote);
        continue;
      }

      if (matchListItem(trimmed)) {
        var listResult = parseList(lines, i, end, depth);
        container.appendChild(listResult.node);
        i = listResult.next;
        continue;
      }

      i = parseParagraph(container, lines, i, end);
    }
    return i;
  }

  // ---------- 公开 API ----------

  function normalizeInput(input) {
    var source = input === null || input === undefined ? "" : String(input);
    var truncated = false;
    if (source.length > MAX_INPUT) {
      source = source.slice(0, MAX_INPUT);
      truncated = true;
    }
    return { text: source.replace(/\r\n?/g, "\n"), truncated: truncated };
  }

  function render(markdownText) {
    var root = frag();
    var data = normalizeInput(markdownText);
    if (!data.text) return root;
    var lines = data.text.split("\n");
    try {
      parseBlocks(root, lines, 0, lines.length, 0);
    } catch (err) {
      // 兜底：任何意外都不抛给调用方，输入整体按纯文本落地，保证不出现半截坏节点
      root = frag();
      root.appendChild(textNode(data.text));
    }
    if (data.truncated) root.appendChild(el("p", "markdown-truncated", TRUNCATED_NOTE));
    return root;
  }

  function renderInline(text) {
    var root = frag();
    var data = normalizeInput(text);
    if (!data.text) return root;
    try {
      parseInline(root, data.text, 0, {});
    } catch (err) {
      root = frag();
      root.appendChild(textNode(data.text));
    }
    return root;
  }

  global.ZhixuMarkdown = {
    render: render,
    renderInline: renderInline,
  };
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);
