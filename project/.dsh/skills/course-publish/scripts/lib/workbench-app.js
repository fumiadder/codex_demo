const $ = (s) => document.querySelector(s);
let state = null, currentCourse = null;
const quotaLink = document.createElement("a");
quotaLink.href = "/quota"; quotaLink.textContent = "图床配额"; quotaLink.style.cssText = "border:1px solid #e3e6eb;border-radius:8px;padding:7px 11px;color:#1f2430;text-decoration:none;background:#fff";
document.querySelector("header")?.appendChild(quotaLink);
const STATUS_LABEL = { pending: "待处理", merged: "已合并", staged: "已暂存", approved: "已批准", published: "已发布", partial: "部分暂存", blocked: "已阻塞", deleted: "已删除" };

async function api(url, opt) {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
}
async function refresh() {
  const sel = $("#courseSel");
  const j = await api("/api/state" + (currentCourse ? ("?course=" + encodeURIComponent(currentCourse)) : ""));
  if (!state || sel.options.length !== j.courses.length) {
    sel.innerHTML = j.courses.map((c) => '<option ' + (c === j.current ? "selected" : "") + ">" + c + "</option>").join("");
    currentCourse = j.current;
  }
  state = j;
  renderStats(j.summary); renderRows(j.chapters); renderQuota(j.quota); renderJobs(j.jobs);
}
function renderStats(s) {
  const order = ["total", "merged", "staged", "partial", "approved", "published", "blocked", "deleted"];
  const labels = { total: "章节总数", merged: "已合并", staged: "已暂存", partial: "部分", approved: "已批准", published: "已发布", blocked: "阻塞", deleted: "已删除" };
  $("#stats").innerHTML = order.filter((k) => s[k]).map((k) => '<div class="stat"><b>' + s[k] + "</b><span>" + labels[k] + "</span></div>").join("");
}
function renderQuota(q) {
  $("#quotaBox").textContent = q ? ("图床今日 " + (q.used || 0) + "/" + (q.limit || 500) + (q.blocked ? "（已到限，次日恢复）" : "")) : "图床配额：—";
}
function renderRows(ch) {
  $("#rows").innerHTML = ch.map((c) => '<tr>'
    + '<td><input type="checkbox" class="ck" data-slug="' + c.slug + '"></td>'
    + "<td>" + c.slug + (c.track !== "main" ? ' <span class="mut">(' + c.track + ")</span>" : "") + "</td>"
    + '<td class="title" title="' + escapeHtml(c.title) + '">' + escapeHtml(c.title) + "</td>"
    + '<td><span class="badge b-' + c.status + '">' + (STATUS_LABEL[c.status] || c.status) + "</span></td>"
    + "<td>" + (c.imageCount == null ? "—" : c.imageCount + (c.failedImages ? ' <span class="err">(' + c.failedImages + "失败)</span>" : "")) + "</td>"
    + '<td><button class="rowlink" onclick="preview(\'' + c.slug + "')\">预览</button></td>"
    + '<td>' + (c.postUrl ? '<a class="rowlink" href="' + c.postUrl + '" target="_blank">查看</a>' : "—") + "</td></tr>").join("");
}
function picked() { return [...document.querySelectorAll(".ck:checked")].map((x) => x.dataset.slug); }
async function post(url, body) {
  try { const j = await api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); await refresh(); return j; }
  catch (e) { alert(e.message); }
}
$("#selAll").onchange = (e) => document.querySelectorAll(".ck").forEach((c) => (c.checked = e.target.checked));
$("#courseSel").onchange = (e) => { currentCourse = e.target.value; refresh(); };
$("#scanBtn").onclick = () => post("/api/scan", { course: currentCourse });
$("#stageBtn").onclick = () => { const s = picked(); if (!s.length) return alert("请先勾选章节"); post("/api/stage", { course: currentCourse, slugs: s }); };
$("#approveBtn").onclick = () => { const s = picked(); if (!s.length) return alert("请先勾选已暂存章节"); post("/api/approve", { course: currentCourse, slugs: s }); };
$("#unapproveBtn").onclick = () => { const s = picked(); if (!s.length) return; post("/api/unapprove", { course: currentCourse, slugs: s }); };
$("#publishBtn").onclick = async () => {
  const s = picked(); if (!s.length) return alert("请先勾选已批准章节");
  if (!confirm("确认发布所选已批准章节到博客园？仅 approved 状态会被发布。")) return;
  await post("/api/publish", { course: currentCourse, slugs: s });
};
let previewData = null;
async function preview(slug) {
  const j = await api("/api/preview?course=" + encodeURIComponent(currentCourse) + "&slug=" + encodeURIComponent(slug));
  previewData = j;
  $("#modalTitle").textContent = slug;
  $("#modalMeta").textContent = (j.source === "preview" ? "在线预览（图片已是博客园链接）" : "源文件预览（图片尚未上云）") + " ｜ " + j.filePath;
  previewData = j;
  previewData.course = currentCourse;
  const warn = j.hasLocalImages ? '<div class="warnbar">该预览含 ' + j.hasLocalImages + ' 张本地图片，需先「暂存」上云后再发布，否则会裂图。</div>' : "";
  $("#modalBody").innerHTML = warn
    + '<div class="pv-tabs"><button class="pvtab on" data-v="rendered" onclick="switchPreview(\'rendered\')">渲染预览（读者所见）</button><button class="pvtab" data-v="raw" onclick="switchPreview(\'raw\')">原始 Markdown</button><button class="pvtab" data-v="sources" onclick="switchPreview(\'sources\')">本地源文件（对比课件原文）</button></div>'
    + '<div id="pv-rendered" class="post-wrap"><aside class="toc" id="toc"></aside><div class="post" id="postBody">' + j.html + "</div></div>"
    + '<pre id="pv-raw" class="raw" style="display:none">' + escapeHtml(j.rawMd || j.md) + "</pre>"
    + '<div id="pv-sources" style="display:none"><div class="src-root" id="srcRoot">正在读取本地源目录…</div><div class="src-wrap"><div class="src-list" id="srcList"></div><div class="src-view" id="srcView"><div class="mut">点击左侧文件名查看课件原文，与中间的合并 Markdown 逐文件对比</div></div></div></div>';
  $("#modalFoot").innerHTML = '<button class="ghost" onclick="closeModal()">关闭</button>';
  $("#mask").classList.add("show"); $("#modal").classList.add("show");
  buildToc();
}
// 从渲染出的标题生成大纲，可点击跳转
function buildToc() {
  const box = document.getElementById("toc");
  const body = document.getElementById("postBody");
  if (!box || !body) return;
  const heads = body.querySelectorAll("h1,h2,h3,h4");
  if (!heads.length) { box.innerHTML = '<div class="mut" style="padding:10px">无标题大纲</div>'; return; }
  let html = '<div class="toc-title">大纲</div>';
  heads.forEach((h, i) => {
    const level = Number(h.tagName.slice(1));
    if (!h.id) h.id = "toc-h-" + i;
    html += '<a href="#' + h.id + '" class="toc-l toc-l' + level + '" data-id="' + h.id + '">' + escapeHtml(h.textContent.trim() || "(无标题)") + "</a>";
  });
  box.innerHTML = html;
  box.onclick = (e) => {
    const a = e.target.closest(".toc-l");
    if (!a) return;
    e.preventDefault();
    const target = document.getElementById(a.dataset.id);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    box.querySelectorAll(".toc-l").forEach((el) => el.classList.toggle("on", el === a));
  };
}
function switchPreview(v) {
  document.querySelectorAll(".pvtab").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("#pv-rendered").style.display = v === "rendered" ? "" : "none";
  $("#pv-raw").style.display = v === "raw" ? "" : "none";
  $("#pv-sources").style.display = v === "sources" ? "" : "none";
  if (v === "sources") loadSources();
}
const KIND_LABEL = { doc: "文档", image: "图片", archive: "压缩包", media: "音视频", other: "其他" };
// 由带相对路径的文件列表构建目录树，完整保留文件夹层级
function buildFileTree(entries, pathKey) {
  const root = { name: "", folder: true, children: new Map() };
  for (const f of entries) {
    const parts = String(f[pathKey]).split("/").filter(Boolean);
    let node = root;
    parts.forEach((part, i) => {
      const isLast = i === parts.length - 1;
      if (!node.children.has(part)) node.children.set(part, { name: part, folder: !isLast, children: new Map() });
      node = node.children.get(part);
      if (isLast) { node.folder = false; node.kind = f.kind; node.bytes = f.bytes; node.rel = f[pathKey]; }
    });
  }
  return root;
}
function naturalCmp(a, b) { return a.localeCompare(b, "zh-Hans-CN", { numeric: true, sensitivity: "base" }); }
// collapsed=true 时文件夹默认收起（顶层展开，深层收起）
function treeNodeHtml(node, depth, collapsed) {
  const kids = [...node.children.values()].sort((a, b) => (b.folder - a.folder) || naturalCmp(a.name, b.name));
  return kids.map((n) => {
    const pad = 10 + depth * 16;
    if (n.folder) {
      const isCol = collapsed !== false; // 默认折叠；显式传 false 才展开
      return '<div class="tree-row tree-folder' + (isCol ? " collapsed" : "") + '" style="padding-left:' + pad + 'px"><span class="chev">▾</span><span class="tree-ico">📁</span>' + escapeHtml(n.name) + "</div>"
        + '<div class="tree-children' + (isCol ? " collapsed" : "") + '">' + treeNodeHtml(n, depth + 1, true) + "</div>";
    }
    return '<div class="tree-row tree-file" data-rel="' + encodeURIComponent(n.rel) + '" data-kind="' + n.kind + '" style="padding-left:' + (pad + 14) + 'px"><span class="src-kind k-' + n.kind + '">' + (KIND_LABEL[n.kind] || n.kind) + "</span><span>" + escapeHtml(n.name) + "</span></div>";
  }).join("");
}
// 绑定树交互：文件夹行折叠/展开，文件行交给 onFile
function bindTree(container, onFile) {
  container.onclick = (e) => {
    const folder = e.target.closest(".tree-folder");
    if (folder) {
      const kids = folder.nextElementSibling;
      const collapsed = kids.classList.toggle("collapsed");
      folder.classList.toggle("collapsed", collapsed);
      return;
    }
    const file = e.target.closest(".tree-file");
    if (file) onFile(decodeURIComponent(file.dataset.rel), file.dataset.kind);
  };
}
async function loadSources() {
  if (!previewData || previewData.sourcesLoaded) return;
  try {
    const j = await api("/api/sources?course=" + encodeURIComponent(previewData.course) + "&slug=" + encodeURIComponent(previewData.slug));
    previewData.sourcesLoaded = true;
    $("#srcRoot").textContent = "本地源目录：" + j.root + "（共 " + j.files.length + " 个文件；文档类才会汇总进 Markdown）";
    const list = $("#srcList");
    list.innerHTML = j.files.length ? treeNodeHtml(buildFileTree(j.files, "rel"), 0) : '<div class="mut">目录为空</div>';
    bindTree(list, (rel, kind) => openSource(rel, kind));
  } catch (e) {
    $("#srcRoot").textContent = "读取失败：" + e.message;
  }
}
async function openSource(rel, kind) {
  const url = "/api/source-file?course=" + encodeURIComponent(previewData.course) + "&slug=" + encodeURIComponent(previewData.slug) + "&rel=" + encodeURIComponent(rel);
  document.querySelectorAll(".tree-file").forEach((el) => el.classList.toggle("active", decodeURIComponent(el.dataset.rel) === rel));
  const head = '<div class="src-view-head"><b>' + escapeHtml(rel) + '</b> <a href="' + url + '" target="_blank" rel="noopener">新标签打开 / 下载</a></div>';
  if (kind === "image") { $("#srcView").innerHTML = head + '<img class="src-img" src="' + url + '" alt="' + escapeHtml(rel) + '">'; return; }
  if (rel.toLowerCase().endsWith(".pdf")) { $("#srcView").innerHTML = head + '<iframe class="src-frame" src="' + url + '"></iframe>'; return; }
  if (/\.docx$/i.test(rel)) { $("#srcView").innerHTML = head + '<iframe class="src-frame" src="' + url + '&view=html"></iframe>'; return; }
  if (/\.pptx?$/i.test(rel)) { $("#srcView").innerHTML = head + '<div class="mut" style="margin-bottom:6px">正在渲染 PPT 为逐页图片…</div><iframe class="src-frame" src="' + url + '&view=slides"></iframe>'; return; }
  if (kind === "archive") { await openArchive(rel, head); return; }
  if (kind === "media" || /\.(doc|exe|dmg|pkg)$/i.test(rel)) { $("#srcView").innerHTML = head + '<div class="mut">该类型不支持网页内预览，请点右上角链接下载后查看。</div>'; return; }
  try {
    const r = await fetch(url);
    const text = await r.text();
    $("#srcView").innerHTML = head + '<pre class="raw src-text">' + escapeHtml(text) + "</pre>";
  } catch (e) {
    $("#srcView").innerHTML = head + '<div class="mut">读取失败：' + escapeHtml(e.message) + "</div>";
  }
}
// 压缩包网页内逐层查看：列出条目，点条目就地解压预览
async function openArchive(rel, head) {
  const listUrl = "/api/archive-list?course=" + encodeURIComponent(previewData.course) + "&slug=" + encodeURIComponent(previewData.slug) + "&rel=" + encodeURIComponent(rel);
  $("#srcView").innerHTML = head + '<div class="mut">正在读取压缩包目录…</div>';
  let entries;
  try {
    const j = await api(listUrl);
    entries = j.entries || [];
  } catch (e) {
    $("#srcView").innerHTML = head + '<div class="mut">读取压缩包失败：' + escapeHtml(e.message) + "</div>";
    return;
  }
  if (!entries.length) { $("#srcView").innerHTML = head + '<div class="mut">压缩包为空</div>'; return; }
  const fmt = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + "MB" : n > 1024 ? (n / 1024).toFixed(1) + "KB" : n + "B";
  $("#srcView").innerHTML = head
    + '<div class="mut" style="margin-bottom:8px">共 ' + entries.length + ' 个文件，保留压缩包内目录结构，点击文件可在网页内预览</div>'
    + '<div class="arc-list" id="arcList">' + treeNodeHtml(buildFileTree(entries, "name"), 0, true) + "</div>"
    + '<div class="arc-view" id="arcView"></div>';
  bindTree(document.getElementById("arcList"), (name) => openArchiveEntry(rel, name, head));
}
async function openArchiveEntry(rel, entry, head) {
  const url = "/api/archive-file?course=" + encodeURIComponent(previewData.course) + "&slug=" + encodeURIComponent(previewData.slug) + "&rel=" + encodeURIComponent(rel) + "&entry=" + encodeURIComponent(entry);
  document.querySelectorAll("#arcList .tree-file").forEach((el) => el.classList.toggle("active", decodeURIComponent(el.dataset.rel) === entry));
  const box = document.getElementById("arcView");
  if (!box) return;
  const ehead = '<div class="src-view-head"><b>' + escapeHtml(entry) + '</b> <a href="' + url + '" target="_blank" rel="noopener">新标签打开 / 下载</a></div>';
  box.innerHTML = ehead + '<div class="mut">正在解压…</div>';
  if (/\.(png|jpe?g|gif|webp|bmp)$/i.test(entry)) { box.innerHTML = ehead + '<img class="src-img" src="' + url + '" alt="">'; return; }
  if (/\.pdf$/i.test(entry)) { box.innerHTML = ehead + '<iframe class="src-frame" src="' + url + '"></iframe>'; return; }
  if (/\.docx$/i.test(entry)) { box.innerHTML = ehead + '<iframe class="src-frame" src="' + url + '&view=html"></iframe>'; return; }
  if (/\.pptx?$/i.test(entry)) { box.innerHTML = ehead + '<div class="mut" style="margin-bottom:6px">正在渲染 PPT 为逐页图片…</div><iframe class="src-frame" src="' + url + '&view=slides"></iframe>'; return; }
  if (/\.(zip|rar|7z|tar|gz|mp4|mov|mkv|avi|mp3|m4a|wav|flv|exe|dmg|pkg|doc)$/i.test(entry)) { box.innerHTML = ehead + '<div class="mut">该类型不支持网页内预览，请点右上角链接下载后查看。</div>'; return; }
  try {
    const r = await fetch(url);
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || "HTTP " + r.status); }
    const text = await r.text();
    box.innerHTML = ehead + '<pre class="raw src-text">' + escapeHtml(text) + "</pre>";
  } catch (e) {
    box.innerHTML = ehead + '<div class="mut">预览失败：' + escapeHtml(e.message) + "</div>";
  }
}
window.switchPreview = switchPreview;
function closeModal() { $("#mask").classList.remove("show"); $("#modal").classList.remove("show"); }
$("#mask").onclick = closeModal;
window.preview = preview; window.closeModal = closeModal;

async function loadSubs() {
  const subs = await api("/api/subscriptions");
  $("#subCard").innerHTML = subs.map((c) => '<h3 style="margin:4px 0 10px">' + escapeHtml(c.title || c.id) + ' <span class="mut">(' + c.id + ")</span></h3>"
    + '<div class="muted-note" style="margin-bottom:8px">目录页：' + escapeHtml(c.toc || "") + " ｜ 网盘目录：" + escapeHtml(c.targetDir || "") + " ｜ 合集：" + escapeHtml(c.collection || "") + "</div>"
    + (c.shares || []).map((s) => '<div class="sub"><code style="flex:1">' + escapeHtml(s.url) + "</code> " + (s.password ? '<span class="mut">pwd:' + escapeHtml(s.password) + "</span>" : "") + ' <button class="ghost" onclick="delSub(\'' + c.id + "','" + encodeURIComponent(s.url) + "')\">删除</button></div>").join("")
    + '<div class="sub"><input id="newUrl-' + c.id + '" placeholder="百度网盘分享链接 https://..." style="flex:1;min-width:280px"><input id="newPwd-' + c.id + '" placeholder="提取码" style="width:110px"><button onclick="addSub(\'' + c.id + "')\">+ 新增订阅</button></div>").join("");
}
async function addSub(cid) {
  const url = $("#newUrl-" + cid).value.trim(), password = $("#newPwd-" + cid).value.trim();
  if (!url) return;
  try { await api("/api/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ course: cid, url, password }) }); loadSubs(); }
  catch (e) { alert(e.message); }
}
async function delSub(cid, encUrl) {
  if (!confirm("删除该分享订阅？")) return;
  await api("/api/subscriptions?course=" + encodeURIComponent(cid) + "&url=" + encUrl, { method: "DELETE" });
  loadSubs();
}
window.addSub = addSub; window.delSub = delSub;

function renderJobs(jobs) {
  if (!jobs) return;
  $("#jobList").innerHTML = jobs.length ? jobs.map((j) => '<div class="job"><span class="dot ' + j.status + '"></span><b>' + escapeHtml(j.label) + '</b><span class="mut">' + j.status + (j.code != null ? " (" + j.code + ")" : "") + '</span><span class="grow"></span><span class="mut">' + (j.finishedAt || j.startedAt || "").replace("T", " ").slice(0, 19) + '</span><button class="ghost" onclick="jobLog(\'' + j.id + "')\">日志</button></div>").join("") : '<p class="mut">暂无任务</p>';
}
async function jobLog(id) {
  const j = await api("/api/jobs/" + id);
  $("#modalTitle").textContent = j.label; $("#modalMeta").textContent = j.status;
  $("#modalBody").innerHTML = '<pre class="log">' + escapeHtml(j.output || "(无输出)") + "</pre>";
  $("#modalFoot").innerHTML = '<button class="ghost" onclick="closeModal()">关闭</button>';
  $("#mask").classList.add("show"); $("#modal").classList.add("show");
}
window.jobLog = jobLog;

document.querySelectorAll("nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  ["chap", "sub", "jobs"].forEach((t) => ($("#tab-" + t).style.display = b.dataset.tab === t ? "" : "none"));
  if (b.dataset.tab === "sub") loadSubs();
});
refresh(); setInterval(refresh, 5000);
