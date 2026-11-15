#!/usr/bin/env python3
"""ZhixuMarkdown（public/vendor/markdown.js）真实浏览器安全 QA。

参照 scripts/frontend_hardening_qa.py 的 Playwright + QA_CHROMIUM 模式：
- 通过 page.set_content 内联加载 markdown.js 源码（无第三方静态服务依赖）；
- 真实 Chromium 中断言块级/行内结构、XSS 防护、危险协议降级、裸链接与容错；
- 另起 python -m http.server 等价的临时静态服务，验证严格 CSP（script-src 'self'）
  下以 <script src> 同站加载可用；
- node --check 与禁用 API 静态扫描在 Python 侧完成。

运行：
  export QA_CHROMIUM=/path/to/chrome-headless-shell
  export PLAYWRIGHT_BROWSERS_PATH=/path/to/.pw-browsers
  /path/.venv/bin/python scripts/markdown_qa.py
退出码非 0 表示存在失败项。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "public" / "vendor" / "markdown.js"
DEFAULT_CHROMIUM = (
    "/Users/fumi/Documents/DeepSeek_harness/.pw-browsers/chromium_headless_shell-1234/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell"
)
DEFAULT_BROWSERS_PATH = "/Users/fumi/Documents/DeepSeek_harness/.pw-browsers"
CHROMIUM = os.environ.get("QA_CHROMIUM", DEFAULT_CHROMIUM)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", DEFAULT_BROWSERS_PATH)

passed = 0
failures = []


def check(name):
    def decorator(fn):
        def wrapped():
            global passed
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - QA 需要收集全部失败而不是中断
                failures.append((name, repr(exc)))
                print(f"FAIL {name}: {exc!r}", flush=True)
            else:
                passed += 1
                print(f"PASS {name}", flush=True)

        wrapped.__name__ = fn.__name__
        return wrapped

    return decorator


source = MD_PATH.read_text(encoding="utf-8")


# ---------- Python 侧静态检查 ----------

@check("源码不含 </script> 序列，可安全内联加载")
def static_no_script_close():
    assert "</script" not in source.lower()


@check("静态扫描：禁用 API（innerHTML/eval/Function/setAttribute 等）零命中")
def static_forbidden_apis():
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    patterns = [
        r"\.innerHTML\b",
        r"\.outerHTML\b",
        r"document\.write\b",
        r"insertAdjacentHTML",
        r"\beval\s*\(",
        r"new\s+Function\s*\(",
        r"setAttribute\s*\(",
        r"setTimeout\s*\(\s*['\"]",
        r"setInterval\s*\(\s*['\"]",
    ]
    hits = [p for p in patterns if re.search(p, stripped)]
    assert not hits, f"命中禁用 API: {hits}"


@check("node --check public/vendor/markdown.js 通过")
def static_node_check():
    node = shutil.which("node")
    assert node, "PATH 中找不到 node"
    proc = subprocess.run(
        [node, "--check", str(MD_PATH)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


# ---------- 浏览器内检查 ----------

HARNESS = """
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>zhixu-markdown-qa</title></head>
<body>
<div id="host"></div>
<script>
window.__xssFired = false;
window.__xss = function () { window.__xssFired = true; };
window.__dialogs = [];
window.__md = {
  render: function (md) {
    var host = document.getElementById('host');
    host.replaceChildren();
    host.appendChild(window.ZhixuMarkdown.render(md));
    return host;
  },
  renderInline: function (text) {
    var host = document.getElementById('host');
    host.replaceChildren();
    host.appendChild(window.ZhixuMarkdown.renderInline(text));
    return host;
  }
};
</script>
<script>
__MD_SOURCE__
</script>
</body>
</html>
"""


def run_browser_suite():
    dialogs = []
    page_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.set_content(HARNESS.replace("__MD_SOURCE__", source))

        def ev(js, arg=None):
            return page.evaluate(js, arg)

        @check("公开契约：window.ZhixuMarkdown.render/renderInline 存在且返回 DocumentFragment")
        def contract():
            info = ev(
                """() => ({
                  render: typeof window.ZhixuMarkdown.render,
                  inline: typeof window.ZhixuMarkdown.renderInline,
                  emptyNodeType: window.ZhixuMarkdown.render('').nodeType,
                  inlineNodeType: window.ZhixuMarkdown.renderInline('').nodeType
                })"""
            )
            assert info["render"] == "function" and info["inline"] == "function", info
            assert info["emptyNodeType"] == 11 and info["inlineNodeType"] == 11, info

        @check("结构：标题 h1-h6 标签与文本层级正确")
        def headings():
            md = "\n".join("#" * i + f" 标题{i}" for i in range(1, 7))
            got = ev(
                "(md) => { window.__md.render(md); return Array.from(document.querySelectorAll('#host h1,#host h2,#host h3,#host h4,#host h5,#host h6')).map(n => [n.tagName, n.textContent]); }",
                md,
            )
            assert got == [[f"H{i}", f"标题{i}"] for i in range(1, 7)], got

        @check("结构：段落拆分与软换行（空格）正确")
        def paragraphs():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { count: document.querySelectorAll('#host p').length,
                           first: document.querySelector('#host p').textContent };
                }""",
                "第一段\n第二行\n\n第二段",
            )
            assert got["count"] == 2 and got["first"] == "第一段 第二行", got

        @check("结构：行尾两空格硬换行生成 <br>")
        def hard_break():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { br: document.querySelectorAll('#host p br').length,
                           text: document.querySelector('#host p').textContent };
                }""",
                "第一行  \n第二行",
            )
            assert got["br"] == 1 and got["text"] == "第一行第二行", got

        @check("结构：加粗/斜体（* 与 _）/行内代码标签正确")
        def inline_basic():
            got = ev(
                """(md) => { window.__md.render(md);
                  return {
                    strong: document.querySelector('#host strong').textContent,
                    em: Array.from(document.querySelectorAll('#host em')).map(n => n.textContent),
                    code: document.querySelector('#host code').textContent,
                    underscoreIntraword: document.body.textContent.includes('foo_bar_baz')
                  };
                }""",
                "**粗体** *斜一* _斜二_ 与 `行内代码`，词内 foo_bar_baz 不解析",
            )
            assert got["strong"] == "粗体", got
            assert got["em"] == ["斜一", "斜二"], got
            assert got["code"] == "行内代码", got
            assert got["underscoreIntraword"] is True, got

        @check("结构：围栏代码块保留换行与原文，内部不解析 Markdown/HTML，支持语言类名与未闭合容错")
        def fenced_code():
            payload = "```js\nconst s = '<script>alert(1)</script>';\n**不是粗体**\n```"
            got = ev(
                """(md) => { window.__md.render(md);
                  return {
                    pre: document.querySelectorAll('#host pre').length,
                    cls: document.querySelector('#host pre code').className,
                    text: document.querySelector('#host pre code').textContent,
                    noMarkup: document.querySelectorAll('#host pre strong,#host pre script').length
                  };
                }""",
                payload,
            )
            assert got["pre"] == 1 and got["cls"] == "language-js", got
            assert got["text"] == "const s = '<script>alert(1)</script>';\n**不是粗体**", got
            assert got["noMarkup"] == 0, got
            unclosed = ev(
                """(md) => { window.__md.render(md);
                  return { pre: document.querySelectorAll('#host pre code').length,
                           text: document.querySelector('#host pre code').textContent };
                }""",
                "```\n未闭合围栏\n第二行",
            )
            assert unclosed["pre"] == 1 and "未闭合围栏" in unclosed["text"], unclosed

        @check("结构：无序列表（-/*/+）含一级嵌套，层级与条目正确")
        def unordered_list():
            md = "- a\n- b\n  - b1\n  - b2\n- c"
            got = ev(
                """(md) => { window.__md.render(md);
                  var host = document.getElementById('host');
                  var outer = host.querySelector('ul');
                  var top = Array.from(outer.children).filter(n => n.tagName === 'LI');
                  var nested = outer.querySelector('li ul');
                  return { top: top.map(li => li.firstChild.textContent),
                           nested: Array.from(nested.children).map(li => li.textContent),
                           nestedCount: nested.children.length };
                }""",
                md,
            )
            assert got["top"] == ["a", "b", "c"] and got["nested"] == ["b1", "b2"], got
            markers = ev(
                """(md) => { window.__md.render(md);
                  return Array.from(document.querySelectorAll('#host ul > li')).map(li => li.textContent);
                }""",
                "* 星号\n+ 加号",
            )
            assert markers == ["星号", "加号"], markers

        @check("结构：有序列表生成 ol/li 且顺序保留")
        def ordered_list():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { ol: document.querySelectorAll('#host ol').length,
                           items: Array.from(document.querySelectorAll('#host ol > li')).map(li => li.textContent) };
                }""",
                "1. 第一项\n2. 第二项",
            )
            assert got["ol"] == 1 and got["items"] == ["第一项", "第二项"], got

        @check("结构：引用块 blockquote 内支持行内语法")
        def blockquote():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { quote: document.querySelectorAll('#host blockquote').length,
                           strong: document.querySelector('#host blockquote strong').textContent,
                           text: document.querySelector('#host blockquote').textContent };
                }""",
                "> 引用里的 **重点** 内容",
            )
            assert got["quote"] == 1 and got["strong"] == "重点", got
            assert "引用里的" in got["text"], got

        @check("结构：分隔线生成 hr，段落保持独立")
        def hr_rule():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { hr: document.querySelectorAll('#host hr').length,
                           p: document.querySelectorAll('#host p').length };
                }""",
                "上文\n\n---\n\n下文",
            )
            assert got == {"hr": 1, "p": 2}, got

        @check("链接：http/https 白名单链接可点，带 rel 且外链 target=_blank")
        def safe_link():
            got = ev(
                """(md) => { window.__md.render(md);
                  var a = document.querySelector('#host a');
                  return { href: a.href, rel: a.getAttribute('rel'), target: a.target, text: a.textContent };
                }""",
                "[知序工作台](https://example.com/path?q=1)",
            )
            assert got["href"] == "https://example.com/path?q=1", got
            assert "noopener" in got["rel"] and "noreferrer" in got["rel"], got
            assert got["target"] == "_blank" and got["text"] == "知序工作台", got

        @check("链接：裸 URL/裸邮箱自动链接，尾标点裁剪，mailto 不带 _blank")
        def bare_links():
            got = ev(
                """(md) => { window.__md.render(md);
                  return Array.from(document.querySelectorAll('#host a')).map(a => ({
                    href: a.href, target: a.target, rel: a.getAttribute('rel'), text: a.textContent
                  }));
                }""",
                "打开 https://example.com/a 。再看 http://b.cn/x. 邮箱 a.b@example.com 收尾",
            )
            by_href = {a["href"]: a for a in got}
            assert "https://example.com/a" in by_href, got
            assert by_href["https://example.com/a"]["target"] == "_blank", got
            assert "http://b.cn/x" in by_href and by_href["http://b.cn/x"]["text"] == "http://b.cn/x", got
            mail = by_href.get("mailto:a.b@example.com")
            assert mail and mail["target"] == "" and "noopener" in mail["rel"], got

        @check("XSS：原始 <script>/<img onerror> 只出文本，无脚本元素与 on* 属性，钩子未触发")
        def xss_raw_html():
            md = "<script>window.__xss();</script>\n<img src=x onerror=window.__xss()>"
            got = ev(
                """(md) => { window.__md.render(md);
                  var host = document.getElementById('host');
                  return {
                    script: host.querySelectorAll('script').length,
                    img: host.querySelectorAll('img').length,
                    onAttrs: Array.from(host.querySelectorAll('*')).filter(e =>
                      e.getAttributeNames().some(n => n.toLowerCase().startsWith('on'))).length,
                    text: host.textContent,
                    fired: window.__xssFired
                  };
                }""",
                md,
            )
            assert got["script"] == 0 and got["img"] == 0 and got["onAttrs"] == 0, got
            assert "<script>" in got["text"] and "onerror" in got["text"], got
            assert got["fired"] is False, got

        @check("XSS：javascript:/data:/vbscript: 链接降级为纯文本，无危险 href 且不可点")
        def dangerous_protocols():
            payloads = [
                "[x](javascript:alert(1))",
                "[a](data:text/html,<script>alert(1)</script>)",
                "[v](vbscript:msgbox(1))",
                "裸链 javascript:alert(1)",
                "[换协议](JAVASCRIPT:alert(document.cookie))",
            ]
            for md in payloads:
                got = ev(
                    """(md) => { window.__md.render(md);
                      var host = document.getElementById('host');
                      return { anchors: host.querySelectorAll('a').length,
                               badHref: Array.from(host.querySelectorAll('[href]')).some(e =>
                                 /^(javascript|data|vbscript):/i.test(e.getAttribute('href'))),
                               text: host.textContent };
                    }""",
                    md,
                )
                assert got["anchors"] == 0 and got["badHref"] is False, (md, got)
                assert "alert" in got["text"] or "msgbox" in got["text"], (md, got)
            assert ev("() => window.__xssFired") is False

        @check("XSS：混合攻击渲染后全局不变量成立（无 script/on*，href 仅白名单协议）")
        def combined_invariants():
            md = "\n\n".join(
                [
                    "<script>window.__xss()</script>",
                    "<img src=x onerror=window.__xss()>",
                    "[bad](javascript:window.__xss())",
                    "[ok](https://example.com)",
                    "`onerror=window.__xss()`",
                    "https://example.org and mailto:t@example.com",
                ]
            )
            got = ev(
                """(md) => { window.__md.render(md);
                  var host = document.getElementById('host');
                  return {
                    script: host.querySelectorAll('script').length,
                    img: host.querySelectorAll('img').length,
                    onAttrs: Array.from(host.querySelectorAll('*')).filter(e =>
                      e.getAttributeNames().some(n => n.toLowerCase().startsWith('on'))).length,
                    protocols: Array.from(host.querySelectorAll('a')).map(a => a.protocol),
                    codeText: host.querySelector('code').textContent,
                    fired: window.__xssFired
                  };
                }""",
                md,
            )
            assert got["script"] == 0 and got["img"] == 0 and got["onAttrs"] == 0, got
            assert sorted(set(got["protocols"])) == ["https:", "mailto:"], got
            assert "onerror" in got["codeText"] and got["fired"] is False, got

        @check("容错：畸形/空/非字符串输入不抛错，均返回 DocumentFragment 且无 pageerror")
        def malformed():
            cases = [
                "",
                "**未闭合",
                "[未闭合](http://",
                "`未闭合的代码",
                "#",
                ">>>> 层层引用",
                "[]()",
                "[ ](https://example.com)",
                "- 孤立项\n  续行文字",
                "a*b*c _d_ e",
                "```\n围栏内 - 不是列表\n**未闭合",
                None,
                12345,
                {"not": "a string"},
            ]
            ok = ev(
                """(cases) => cases.map(c => {
                  try {
                    var r1 = window.ZhixuMarkdown.render(c);
                    var r2 = window.ZhixuMarkdown.renderInline(c);
                    return r1.nodeType === 11 && r2.nodeType === 11;
                  } catch (e) { return false; }
                })""",
                cases,
            )
            assert all(ok), list(zip(cases, ok))

        @check("性能：>100KB 输入截断渲染不卡死，截断提示落地")
        def long_input():
            md = ("段落 **加粗** 文本与裸链 https://example.com/page\n" * 4000)[: 150 * 1024]
            assert len(md) > 100 * 1024
            started = time.perf_counter()
            got = ev(
                """(md) => { window.__md.render(md);
                  var host = document.getElementById('host');
                  return { nodes: host.querySelectorAll('*').length,
                           note: (host.querySelector('.markdown-truncated') || {}).textContent || null,
                           length: host.textContent.length };
                }""",
                md,
            )
            elapsed = time.perf_counter() - started
            assert elapsed < 15, f"渲染耗时 {elapsed:.2f}s 超出预算"
            assert got["nodes"] > 0 and got["note"] == "（内容过长，已截断）", got
            assert got["length"] <= 100 * 1024 + 64, got

        @check("转义：< > & 引号等原样按文本显示，可往返读取")
        def escape_roundtrip():
            sample = "a < b && c > d & \"双引号\" '单引号'"
            got = ev(
                "(s) => { window.__md.render(s); return document.querySelector('#host p').textContent; }",
                sample,
            )
            assert got == sample, got

        @check("行内代码内部不解析 Markdown/HTML，完全按文本")
        def code_span_literal():
            got = ev(
                """(md) => { window.__md.render(md);
                  return { code: document.querySelector('#host code').textContent,
                           strong: document.querySelectorAll('#host strong').length,
                           elements: document.querySelectorAll('#host *:not(code)').length };
                }""",
                "`**不是粗体** <img src=x>`",
            )
            assert got["code"] == "**不是粗体** <img src=x>", got
            assert got["strong"] == 0, got

        @check("renderInline：只处理行内语法，不产生块级标签，HTML 仍按文本")
        def inline_only():
            got = ev(
                """(t) => { window.__md.renderInline(t);
                  var host = document.getElementById('host');
                  return { strong: host.querySelectorAll('strong').length,
                           blocks: host.querySelectorAll('p,h1,h2,h3,ul,ol,blockquote,pre,hr').length,
                           text: host.textContent };
                }""",
                "# 不是标题 **粗体** <b>标签</b>",
            )
            assert got["strong"] == 1 and got["blocks"] == 0, got
            assert "<b>标签</b>" in got["text"], got

        @check("全过程：无浏览器 dialog、无 pageerror")
        def no_dialog_or_pageerror():
            assert dialogs == [], f"出现 {len(dialogs)} 个 dialog: {dialogs}"
            assert page_errors == [], "pageerror: " + " | ".join(page_errors)

        for fn in list(locals().values()):
            if callable(fn) and getattr(fn, "__name__", "").startswith(("contract", "headings", "paragraphs", "hard_break", "inline_basic", "fenced_code", "unordered_list", "ordered_list", "blockquote", "hr_rule", "safe_link", "bare_links", "xss_", "dangerous_", "combined_", "malformed", "long_input", "escape_", "code_span", "inline_only", "no_dialog")):
                fn()

        context.close()
        browser.close()


def run_csp_suite():
    """严格 CSP（script-src 'self'）下经临时静态服务以 <script src> 同站加载。"""
    with tempfile.TemporaryDirectory(prefix="zhixu-md-csp-") as www:
        www_dir = Path(www)
        (www_dir / "markdown.js").write_text(source, encoding="utf-8")
        (www_dir / "boot.js").write_text(
            "document.getElementById('host').appendChild("
            "window.ZhixuMarkdown.render('# CSP OK\\n\\n- a\\n- b'));",
            encoding="utf-8",
        )
        (www_dir / "harness.html").write_text(
            """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; script-src 'self'; style-src 'unsafe-inline'">
</head><body><div id="host"></div>
<script src="./markdown.js"></script>
<script src="./boot.js"></script>
</body></html>""",
            encoding="utf-8",
        )

        class QuietHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=www, **kwargs)

            def log_message(self, fmt, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        csp_errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=CHROMIUM,
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = browser.new_page()
                page.on("pageerror", lambda e: csp_errors.append(str(e)))
                page.goto(
                    f"http://127.0.0.1:{server.server_port}/harness.html",
                    wait_until="networkidle",
                )
                got = page.evaluate(
                    """() => ({
                      loaded: typeof window.ZhixuMarkdown === 'object',
                      h1: document.querySelector('#host h1') && document.querySelector('#host h1').textContent,
                      items: document.querySelectorAll('#host li').length
                    })"""
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        @check("CSP：script-src 'self' 下同站 <script src> 加载并渲染成功")
        def csp_result():
            assert not csp_errors, "CSP pageerror: " + " | ".join(csp_errors)
            assert got == {"loaded": True, "h1": "CSP OK", "items": 2}, got

        csp_result()


for fn in list(globals().values()):
    if callable(fn) and getattr(fn, "__name__", "") in {
        "static_no_script_close",
        "static_forbidden_apis",
        "static_node_check",
    }:
        fn()

run_browser_suite()
run_csp_suite()

print(f"\n==== ZhixuMarkdown QA 完成：PASS {passed}，FAIL {len(failures)} ====", flush=True)
if failures:
    for name, err in failures:
        print(f" - {name}: {err}", flush=True)
    sys.exit(1)
sys.exit(0)
