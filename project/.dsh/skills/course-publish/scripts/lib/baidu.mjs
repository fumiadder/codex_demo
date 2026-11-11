// 百度网盘操作：分享枚举 / 转存 / 自有目录管理。XHR 优先，DOM 兜底；风控即抛 GateError。
import { log } from "./log.mjs";
import { humanDelay } from "./browser.mjs";

export class GateError extends Error {
  constructor(message, kind, detail) {
    super(message);
    this.name = "GateError";
    this.kind = kind; // "login" | "captcha" | "risk" | "ui-changed" | "password"
    this.detail = detail;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class BaiduPan {
  constructor(page) {
    this.page = page;
    this.bdstoken = null;
    this._shareCtx = null;
    this._captured = [];
  }

  // ---- 通用：页面上下文内带凭证请求 ----
  async apiGet(url) {
    return this.page.evaluate(async (u) => {
      const r = await fetch(u, { credentials: "include", headers: { Accept: "application/json" } });
      const text = await r.text();
      try { return JSON.parse(text); } catch { return { _raw: text.slice(0, 500), _status: r.status }; }
    }, url);
  }

  async apiPost(url, formBody) {
    return this.page.evaluate(
      async ({ u, body }) => {
        const r = await fetch(u, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", Accept: "application/json" },
          body,
        });
        const text = await r.text();
        try { return JSON.parse(text); } catch { return { _raw: text.slice(0, 500), _status: r.status }; }
      },
      { u: url, body: formBody },
    );
  }

  async ensureLogin() {
    await this.page.goto("https://pan.baidu.com", { waitUntil: "domcontentloaded", timeout: 60000 });
    await sleep(2500);
    const j = await this.apiGet("https://pan.baidu.com/api/list?dir=%2F&order=time&desc=1");
    if (j.errno !== 0) throw new GateError("百度网盘未登录或登录态失效，请重新运行 baidu:login", "login", j);
    this.bdstoken = await this._findBdstoken();
    log.ok("百度登录态有效，bdstoken =", this.bdstoken ? "已获取" : "未获取(将走DOM)");
  }

  async _findBdstoken() {
    const v = await this.page.evaluate(() => {
      try {
        if (window.locals && typeof window.locals.get === "function") {
          const t = window.locals.get("bdstoken");
          if (t) return t;
        }
      } catch {}
      try { if (window.yunData && window.yunData.MYBDSTOKEN) return window.yunData.MYBDSTOKEN; } catch {}
      const m = document.documentElement.innerHTML.match(/"bdstoken"\s*:\s*"?([0-9a-zA-Z]+)"?/);
      return m ? m[1] : null;
    });
    return v;
  }

  // ---- 自有网盘目录 ----
  async ownList(dir) {
    const j = await this.apiGet("https://pan.baidu.com/api/list?dir=" + encodeURIComponent(dir) + "&order=time&desc=1");
    if (j.errno !== 0) throw new Error("列目录失败 " + dir + ": errno=" + j.errno);
    return (j.list || []).map((f) => ({
      fsid: f.fs_id,
      name: f.server_filename,
      isdir: f.isdir === 1,
      size: f.size,
      mtime: f.server_mtime,
    }));
  }

  async ensureOwnDir(targetDir) {
    const segs = targetDir.split("/").filter(Boolean);
    let cur = "";
    for (const seg of segs) {
      const next = cur + "/" + seg;
      const list = await this.ownList(cur || "/").catch(() => []);
      const hit = list.find((f) => f.isdir && f.name === seg);
      if (!hit) {
        if (!this.bdstoken) throw new GateError("目标目录不存在且无 bdstoken 无法自动建目录: " + next, "ui-changed");
        const body = "path=" + encodeURIComponent(next) + "&isdir=1&block_list=%5B%5D";
        const j = await this.apiPost("https://pan.baidu.com/api/create?a=commit&bdstoken=" + this.bdstoken, body);
        if (j.errno !== 0 && j.errno !== -8) throw new Error("创建目录失败 " + next + ": " + JSON.stringify(j).slice(0, 200));
        log.ok("已创建网盘目录", next);
        await humanDelay(800);
      }
      cur = next;
    }
    return targetDir;
  }

  // ---- 分享页 ----
  installCapture() {
    this.page.on("response", async (resp) => {
      try {
        const u = resp.url();
        if (!/pan\.baidu\.com\/(share|api)\//.test(u)) return;
        const ct = resp.headers()["content-type"] || "";
        if (!ct.includes("json")) return;
        const json = await resp.json().catch(() => null);
        if (json) this._captured.push({ url: u, json });
      } catch {}
    });
  }

  captured(pattern) {
    return this._captured.filter((c) => pattern.test(c.url));
  }

  async openShare(share) {
    this._shareCtx = null;
    this._captured = [];
    const url = share.url;
    const pwd = share.password || (url.match(/[?&]pwd=([0-9a-zA-Z]{4})/) || [])[1] || "";
    await this.page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
    await sleep(3000);
    await this._handlePassword(pwd);
    await this._waitShareList();
    await this._detectGate();
    const ctx = await this._buildShareCtx();
    this._shareCtx = ctx;
    return ctx;
  }

  async _handlePassword(pwd) {
    if (!pwd) return;
    // 已自动填码进入列表则无需操作
    if (await this._hasFileList()) return;
    // 仅在确有“提取码”输入控件时才填码；严禁回退到裸 input，以免误操作搜索框等无关元素
    const pwdSelectors = [".pickpw_input", "input.share-input", "input[type=password]"];
    let box = null;
    for (const sel of pwdSelectors) {
      const cand = await this.page.$(sel).catch(() => null);
      if (cand && (await cand.isVisible().catch(() => false))) {
        box = cand;
        break;
      }
    }
    if (!box) {
      // 没有密码框：多半是列表加载慢（XHR 未到），再等一轮；仍无则按 UI 变化上报而非误判密码错误
      await this._waitShareList().catch(() => {
        throw new GateError("分享列表加载超时且未找到提取码输入框（页面结构可能变化），请用 baidu-probe 排查", "ui-changed");
      });
      return;
    }
    await box.click();
    await box.fill(pwd, { timeout: 5000 }).catch(async () => {
      await this.page.keyboard.type(pwd, { delay: 80 });
    });
    const btnSelectors = [".pickpw-btn", "button.share-submit", "button:has-text('提取')", "button:has-text('提交')"];
    let clicked = false;
    for (const sel of btnSelectors) {
      const btn = await this.page.$(sel).catch(() => null);
      if (btn && (await btn.isVisible().catch(() => false)) && (await btn.isEnabled().catch(() => true))) {
        await btn.click();
        clicked = true;
        break;
      }
    }
    if (!clicked) await box.press("Enter").catch(() => {});
    await sleep(3000);
    if (!(await this._hasFileList())) {
      throw new GateError("提取码提交后未出现文件列表，可能提取码错误或页面结构变化", "password", { pwdProvided: true });
    }
  }

  // 捕获的 share/list XHR 是否已给出有效列表（新版分享页 DOM 选择器失效，以 XHR 为准）
  _xhrListReady() {
    return this.captured(/\/share\/list/).some(
      (c) => c.json && c.json.errno === 0 && Array.isArray(c.json.list) && c.json.list.length > 0,
    );
  }

  async _hasFileList() {
    if (this._xhrListReady()) return true;
    return this.page.evaluate(() => {
      const nodes = document.querySelectorAll(".file-item, .share-list .list-item, [data-id], tr.file-item");
      return nodes.length > 0;
    });
  }

  async _waitShareList() {
    for (let i = 0; i < 20; i++) {
      if (await this._hasFileList()) return;
      await sleep(1000);
    }
    // 最后再检查是否其实是密码页
    const needPwd = await this.page.evaluate(() => /提取码|访问密码/.test(document.body.innerText));
    if (needPwd) throw new GateError("分享需要提取码但未提供", "password");
    throw new GateError("分享文件列表加载超时（页面结构可能已变化），请用 baidu-probe 探针排查", "ui-changed");
  }

  async _detectGate() {
    const hit = await this.page.evaluate(() => {
      const t = document.body.innerText || "";
      if (/验证码|安全验证|滑块|请完成.*验证/.test(t)) return "captcha";
      if (/登录后|请先登录|立即登录/.test(t) && !/注销|退出/.test(t)) return "login";
      return null;
    });
    if (hit === "captcha") throw new GateError("百度出现验证码/安全验证，请在浏览器中手动完成后重跑", "captcha");
    if (hit === "login") throw new GateError("百度登录态在分享页失效，请重新登录", "login");
  }

  async _buildShareCtx() {
    // 优先从捕获的 share/list XHR 取条目；shareid/uk 优先读页面 yunData（新版 URL 不再携带）
    const yd = await this.page
      .evaluate(() => {
        try {
          const y = window.yunData || {};
          const uk0 = y.share_uk ?? y.uk;
          return {
            shareid: y.shareid != null ? String(y.shareid) : null,
            uk: uk0 != null ? String(uk0) : null,
            bdstoken: y.bdstoken || null,
          };
        } catch {
          return {};
        }
      })
      .catch(() => ({}));
    // 优先从捕获的 share/list XHR 取 shareid/uk 与条目
    const listHits = this.captured(/\/share\/list/);
    let entries = [];
    let shareid = yd.shareid || null;
    let uk = yd.uk || null;
    for (const h of listHits) {
      // 顶层条目来自 root=1（或无 dir 参数）的根列表调用；其 path 是分享者完整路径，不能再用 path==="/" 判定
      const isRootCall = /[?&]root=1/.test(h.url) || !/[?&]dir=/.test(h.url);
      if (Array.isArray(h.json.list)) {
        for (const f of h.json.list) {
          const atTop =
            isRootCall || f.path === "/" || f.parent_path === "/" || (f.path === undefined && f.parent_path === undefined);
          if (atTop) entries.push(f);
        }
      }
    }
    if (!entries.length) {
      // DOM 兜底
      entries = await this._domEntries();
    }
    const top = entries.map((f) => ({
      fsid: f.fs_id,
      name: f.server_filename || f.filename,
      isdir: Number(f.isdir) === 1,
      size: Number(f.size) || 0,
      path: f.path || null,
    }));
    // 去重（XHR 可能重复捕获）
    const seen = new Set();
    const uniq = top.filter((e) => (seen.has(e.fsid) ? false : (seen.add(e.fsid), true)));
    // 尽力提取 seid（部分转存接口需要）
    let seid = null;
    for (const h of this.captured(/share\/(list|wxlist|tplview)/)) {
      const m = h.url.match(/[?&]seid=([0-9]+)/);
      if (m) { seid = m[1]; break; }
    }
    if (!seid) {
      seid = await this.page.evaluate(() => {
        try { if (window.yunData?.SEID) return String(window.yunData.SEID); } catch {}
        const m = document.documentElement.innerHTML.match(/"seid"\s*:\s*"?(\d+)"?/);
        return m ? m[1] : null;
      }).catch(() => null);
    }
    if (!shareid || !uk) {
      log.warn("未从 XHR 拿到 shareid/uk，转存将走 DOM 模式");
    }
    return { shareid, uk, seid, entries: uniq, url: this.page.url() };
  }

  // 构造分享内文件夹的 sharelink 路径（根文件夹形如 /sharelink<share_uk>-<fs_id>/<名>）
  static sharelinkPath(shareUk, fsid, name) {
    return "/sharelink" + shareUk + "-" + fsid + "/" + name;
  }

  // 枚举分享内某个目录（dirPath 为 sharelink 完整路径）。同源 fetch 自带登录 cookie。
  async listShareDir(shareCtx, dirPath) {
    if (!shareCtx || shareCtx.shareid == null || shareCtx.uk == null) {
      throw new GateError("缺少 shareid/uk，无法枚举分享子目录，请先 openShare", "ui-changed");
    }
    const u =
      "https://pan.baidu.com/share/list?uk=" + shareCtx.uk +
      "&shareid=" + shareCtx.shareid +
      "&sekey=" + encodeURIComponent(shareCtx.sekey || "") +
      "&dir=" + encodeURIComponent(dirPath) +
      "&root=0&page=1&num=200&order=time&desc=1&showempty=0&web=1";
    const j = await this.page.evaluate(async (url) => {
      const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }, u);
    if (j.errno !== 0) {
      throw new GateError("枚举分享子目录失败 errno=" + j.errno + " dir=" + dirPath, "ui-changed", j);
    }
    return (j.list || []).map((f) => ({
      fsid: String(f.fs_id),
      name: f.server_filename || f.filename,
      isdir: Number(f.isdir) === 1,
      size: Number(f.size) || 0,
      path: f.path || null,
    }));
  }

  async _domEntries() {
    return this.page.evaluate(() => {
      const rows = document.querySelectorAll(".file-item, .share-list .list-item, tr.file-item");
      const out = [];
      rows.forEach((row, i) => {
        const name =
          row.querySelector(".file-name .text, .filename, .text, a")?.textContent?.trim() ||
          row.getAttribute("data-name") ||
          row.innerText.split("\n")[0].trim();
        const fsid = row.getAttribute("data-id") || row.getAttribute("data-fsid") || "dom-" + i;
        const isdir = !!row.querySelector(".icon-dir, .file-icon.dir") || /(^|\s)dir(\s|$)/.test(row.className);
        if (name) out.push({ fs_id: fsid, server_filename: name, isdir: isdir ? 1 : 0, size: 0, path: "/" });
      });
      return out;
    });
  }

  // 转存：先 XHR，失败回退 DOM
  async transfer(fsids, targetDir) {
    await this.ensureOwnDir(targetDir);
    if (this._shareCtx?.shareid && this.bdstoken) {
      const j = await this.apiPost(
        "https://pan.baidu.com/share/transfer?shareid=" + this._shareCtx.shareid +
          "&from=" + this._shareCtx.uk + "&bdstoken=" + this.bdstoken + "&seid=" + (this._shareCtx.seid || ""),
        "fsidlist=" + encodeURIComponent(JSON.stringify(fsids.map(String))) + "&path=" + encodeURIComponent(targetDir),
      );
      if (j.errno === 0) {
        log.ok("XHR 转存成功 →", targetDir, fsids.length, "项");
        return { mode: "xhr", result: j };
      }
      if (j.errno === -140) throw new GateError("转存触发百度风控(errno=-140)，请稍后手动在浏览器确认", "risk", j);
      log.warn("XHR 转存失败，回退 DOM：errno=" + j.errno, JSON.stringify(j).slice(0, 160));
    }
    return this._transferByDom(fsids, targetDir);
  }

  async _transferByDom(fsids, targetDir) {
    // 勾选目标行（按 fsid 或名称）
    const names = new Set(this._shareCtx.entries.filter((e) => fsids.includes(e.fsid)).map((e) => e.name));
    await this.page.evaluate((names) => {
      const rows = document.querySelectorAll(".file-item, .share-list .list-item, tr.file-item");
      rows.forEach((row) => {
        const name = row.querySelector(".file-name .text, .filename, .text, a")?.textContent?.trim();
        if (names.has(name)) {
          const checker = row.querySelector(".checker, input[type=checkbox], .checkbox") || row;
          if (!row.className.includes("selected")) checker.click();
        }
      });
    }, [...names]);
    await humanDelay(600);
    // 点击“保存到”
    const saveBtn = await this.page.locator("text=保存到").first().click({ timeout: 8000 }).catch(() => null);
    if (saveBtn === null) throw new GateError("找不到“保存到”按钮，页面结构可能变化", "ui-changed");
    await sleep(2000);
    // 在弹窗中逐级进入目标目录并新建缺失文件夹
    const segs = targetDir.split("/").filter(Boolean);
    for (const seg of segs) {
      const clicked = await this.page.evaluate((seg) => {
        const items = document.querySelectorAll(".dialog .tree-item, .dialog .file-item, .dialog li, [role=treeitem]");
        for (const it of items) {
          if (it.textContent.trim().startsWith(seg)) { it.click(); return true; }
        }
        return false;
      }, seg).catch(() => false);
      if (!clicked) {
        // 新建文件夹
        await this.page.locator("text=新建文件夹").first().click({ timeout: 4000 }).catch(() => {});
        await this.page.keyboard.type(seg, { delay: 60 });
        await this.page.keyboard.press("Enter");
      }
      await sleep(800);
    }
    await this.page.locator(".dialog button:has-text('确定'), .dialog button:has-text('保存到这里'), button:has-text('确认保存')").first().click({ timeout: 8000 });
    await sleep(2500);
    await this._detectGate();
    log.ok("DOM 转存完成 →", targetDir);
    return { mode: "dom" };
  }

  // 转存后核验目标目录确实包含这些名称
  async verifyTransferred(targetDir, names) {
    const list = await this.ownList(targetDir).catch(() => []);
    const have = new Set(list.map((f) => f.name));
    const missing = names.filter((n) => !have.has(n));
    return { ok: missing.length === 0, missing, present: [...have] };
  }
}
