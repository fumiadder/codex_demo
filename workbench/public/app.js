"use strict";

const $ = (selector, context = document) => context.querySelector(selector);
const $$ = (selector, context = document) => [
  ...context.querySelectorAll(selector),
];
const state = {
  user: null,
  spaces: [],
  space: null,
  items: [],
  area: "work",
  view: "overview",
  demo: false,
  testMode: false,
  storagePersistence: null,
  registrationMode: null,
  taskFilter: "all",
  mediaFilter: "all",
  register: false,
  request: 0,
  sessionEpoch: 0,
  authRequest: 0,
  loading: false,
  vault: {
    key: null,
    entries: [],
    payload: undefined,
    spaceId: null,
    loaded: false,
    loading: false,
    activity: Date.now(),
    epoch: 0,
  },
  demoStore: {},
  demoVaults: {},
  objectURLs: new Set(),
};
const names = {
  overview: "今天，也从容一点。",
  tasks: "把重要的事，向前推进。",
  notes: "让灵感有个落脚的地方。",
  media: "所有资料，随手可得。",
  vault: "只属于你的私密空间。",
  spaces: "各有空间，也能一起。",
  modules: "从需要出发，慢慢长大。",
};
const descriptions = {
  overview: "让每个想法有去处，让每一步都算数。",
  tasks: "一步一步，把想做的变成已完成。",
  notes: "值得留下的念头，不必等到明天。",
  media: "图片、视频、音频与文档，收在同一个地方。",
  vault: "你的个人密码箱独立保存，空间成员无法看到其中内容。",
  spaces: "独立空间隔离数据，用明确的权限一起协作。",
  modules: "基础功能已就绪，AI 能力将在接入模型服务后启用。",
};
const roleNames = { owner: "所有者", editor: "编辑者", viewer: "只读成员" };
const kindNames = { task: "待办", note: "笔记", media: "媒体" };
const fieldKeys = ["kind", "area", "title", "body", "status", "meta"];
let modalCleanup = null;
let modalReturnFocus = null;
let drawerReturnFocus = null;
const mobileLayout = window.matchMedia(
  "(max-width: 760px), (orientation: landscape) and (max-height: 500px) and (max-width: 1024px)",
);

function closeDrawer(restoreFocus = true) {
  const wasOpen = $("#sidebar").classList.contains("open");
  $("#sidebar").classList.remove("open");
  document.body.classList.remove("drawer-open");
  $(".workspace-body").inert = false;
  $("#drawer-backdrop").hidden = true;
  for (const id of ["#mobile-menu", "#mobile-more"])
    $(id).setAttribute("aria-expanded", "false");
  syncDrawerLayout();
  if (wasOpen && restoreFocus && drawerReturnFocus?.isConnected)
    drawerReturnFocus.focus({ preventScroll: true });
  drawerReturnFocus = null;
}
function openDrawer(trigger = $("#mobile-menu")) {
  if (!mobileLayout.matches || !state.user) return;
  drawerReturnFocus = trigger;
  $("#sidebar").classList.add("open");
  $("#sidebar").inert = false;
  $("#sidebar").removeAttribute("aria-hidden");
  $("#sidebar").setAttribute("role", "dialog");
  $("#sidebar").setAttribute("aria-modal", "true");
  $("#drawer-backdrop").hidden = false;
  $(".workspace-body").inert = true;
  document.body.classList.add("drawer-open");
  for (const id of ["#mobile-menu", "#mobile-more"])
    $(id).setAttribute("aria-expanded", "true");
  $("#drawer-close").focus({ preventScroll: true });
}
function syncDrawerLayout() {
  const drawer = $("#sidebar");
  const mobileClosed = mobileLayout.matches && !drawer.classList.contains("open");
  drawer.inert = mobileClosed;
  if (mobileClosed) drawer.setAttribute("aria-hidden", "true");
  else drawer.removeAttribute("aria-hidden");
  if (!mobileLayout.matches || mobileClosed) {
    drawer.removeAttribute("role");
    drawer.removeAttribute("aria-modal");
  }
}
function updateVisualViewport() {
  const viewport = window.visualViewport;
  document.documentElement.style.setProperty(
    "--visible-viewport-height",
    `${Math.round(viewport?.height || window.innerHeight)}px`,
  );
  document.documentElement.style.setProperty(
    "--visual-viewport-top",
    `${Math.round(viewport?.offsetTop || 0)}px`,
  );
  document.body.classList.toggle(
    "mobile-editing",
    mobileLayout.matches &&
      document.activeElement?.matches("input, textarea, select"),
  );
}
function updateRegistrationControls() {
  const invitationRequired = state.register && state.registrationMode === "invite";
  $("#invitation-label").hidden = !invitationRequired;
  $("#auth-invitation").required = invitationRequired;
  $("#auth-toggle").disabled = state.registrationMode === null;
  const note = $("#auth-registration-note");
  note.hidden = state.registrationMode === "open";
  note.textContent = state.registrationMode === "invite"
    ? "注册需要管理员提供的邀请码，已有账户可以直接登录。"
    : "暂时无法确认注册设置，已有账户可以直接登录；刷新页面后可再尝试注册。";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function svg(name, small = false) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("class", `icon${small ? " small" : ""}`);
  node.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  node.append(use);
  return node;
}
function button(text, className, action, iconName) {
  const b = el("button", className);
  b.type = "button";
  if (iconName) b.append(svg(iconName, true));
  if (text) b.append(document.createTextNode(text));
  if (action) b.addEventListener("click", action);
  return b;
}
function iconButton(name, label, action) {
  const b = button("", "icon-button", action, name);
  b.setAttribute("aria-label", label);
  b.title = label;
  return b;
}
function labelField(text, control) {
  const label = el("label", "", text);
  label.append(control);
  return label;
}
function input(type = "text", placeholder = "", value = "") {
  const node = el("input");
  node.type = type;
  node.placeholder = placeholder;
  node.value = value;
  return node;
}
function select(options, value) {
  const node = el("select");
  for (const [key, text] of options) {
    const opt = el("option", "", text);
    opt.value = key;
    node.append(opt);
  }
  node.value = value;
  return node;
}
function currentItems(kind) {
  return state.items.filter(
    (item) => item.area === state.area && (!kind || item.kind === kind),
  );
}
function writable() {
  return state.space && state.space.role !== "viewer";
}
function ensureWrite() {
  if (writable()) return true;
  toast("这个空间为只读，请联系空间所有者调整权限。");
  return false;
}
function shortDate(value) {
  if (!value) return "刚刚";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    timeZone: "Asia/Shanghai",
  }).format(d);
}
function relativeDate(value) {
  if (!value) return "刚刚";
  const diff = Date.now() - new Date(value).getTime();
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return `${Math.max(1, Math.floor(diff / 60000))} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  return shortDate(value);
}
function sizeString(bytes = 0) {
  return bytes < 1024
    ? `${bytes} B`
    : bytes < 1048576
      ? `${(bytes / 1024).toFixed(0)} KB`
      : `${(bytes / 1048576).toFixed(1)} MB`;
}
function toast(message, error = false) {
  const node = el("div", `toast${error ? " error" : ""}`, message);
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 4500);
}
function errorMessage(error) {
  return error?.message || "暂时无法完成，请重试。";
}
async function api(path, method = "GET", data) {
  const expectedUser = state.demo ? null : state.user?.id;
  const session = state.sessionEpoch;
  const options = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (expectedUser) options.headers["X-Expected-User"] = expectedUser;
  if (method !== "GET") options.headers["X-Requested-With"] = "Workspace";
  if (data instanceof FormData) options.body = data;
  else if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("无法连接工作台服务，请检查网络后重试。");
  }
  if (response.status === 401 && expectedUser &&
      state.user?.id === expectedUser && state.sessionEpoch === session) {
    showAuth();
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("服务响应异常，请稍后重试。");
  }
  if (!response.ok) {
    const err = new Error(result.error || result.message || "请求未完成");
    err.status = response.status;
    throw err;
  }
  return result;
}
function measureEnvironmentBanner() {
  const banner = $("#test-environment-banner");
  document.documentElement.style.setProperty(
    "--environment-banner-height",
    `${banner.hidden ? 0 : banner.offsetHeight}px`,
  );
}
async function loadConfiguration() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/api/config", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error("无法读取环境设置");
    const config = await response.json();
    if (
      typeof config.testMode !== "boolean" ||
      !["ephemeral", "persistent", "unknown"].includes(config.storagePersistence)
    )
      throw new Error("环境设置格式异常");
    state.testMode = config.testMode;
    state.storagePersistence = config.storagePersistence;
    state.registrationMode = config.registrationMode === undefined
      ? "open"
      : ["open", "invite"].includes(config.registrationMode)
        ? config.registrationMode
        : null;
  } catch {
    state.testMode = null;
    state.storagePersistence = null;
    state.registrationMode = null;
  } finally {
    window.clearTimeout(timeout);
  }
  const banner = $("#test-environment-banner");
  banner.hidden = state.testMode === false && state.storagePersistence === "persistent";
  document.body.classList.toggle("environment-banner-visible", !banner.hidden);
  if (!banner.hidden) {
    banner.replaceChildren(
      el(
        "strong",
        "",
        state.testMode ? "免费测试环境" : "存储状态未确认",
      ),
      el(
        "span",
        "",
        state.testMode
          ? "重启或休眠可能清空记录与上传文件，请使用测试数据。"
          : "请使用测试数据，暂不要保存重要记录或私密信息。",
      ),
    );
  }
  updateRegistrationControls();
  measureEnvironmentBanner();
}
function loading(container) {
  container.replaceChildren();
  const row = el("div", "loading-panel");
  row.append(el("span", "spinner"), el("span", "", "正在打开你的空间…"));
  container.append(row);
}
function emptyState(container, iconName, title, text, actionText, action) {
  const node = el("div", "empty-state");
  const ico = el("div", "empty-icon");
  ico.append(svg(iconName));
  node.append(ico, el("h3", "", title), el("p", "", text));
  if (actionText && action)
    node.append(button(actionText, "button soft", action, "plus"));
  container.append(node);
}
function actionsRow(...buttons) {
  const row = el("div", "modal-actions");
  row.append(...buttons);
  return row;
}
function showModal(title, build, eyebrow = "") {
  const active = document.activeElement;
  const origin = $("#modal").contains(active) ? modalReturnFocus : active;
  closeModal(false);
  closeDrawer(false);
  modalReturnFocus = origin?.closest("#sidebar") && mobileLayout.matches
    ? $("#mobile-more")
    : origin;
  $("#modal-title").textContent = title;
  $("#modal-eyebrow").textContent = eyebrow;
  $("#modal-eyebrow").hidden = !eyebrow;
  const body = $("#modal-body");
  body.replaceChildren();
  build(body);
  updateVisualViewport();
  $("#modal").showModal();
  const focus = $('input:not([type="hidden"]),textarea', body);
  if (mobileLayout.matches) $("#modal-close").focus({ preventScroll: true });
  else if (focus) window.setTimeout(() => {
    if ($("#modal").open && focus.isConnected) focus.focus();
  }, 80);
  document.body.classList.add("modal-open");
}
function closeModal(restoreFocus = true) {
  const wasOpen = $("#modal").open;
  if (modalCleanup) {
    const cleanup = modalCleanup;
    modalCleanup = null;
    cleanup();
  }
  if ($("#modal").open) $("#modal").close();
  $("#modal-body").replaceChildren();
  document.body.classList.remove("modal-open");
  if (wasOpen && restoreFocus && modalReturnFocus?.isConnected &&
    !modalReturnFocus.closest("[hidden], [inert]"))
    modalReturnFocus.focus({ preventScroll: true });
  modalReturnFocus = null;
  updateVisualViewport();
}

function demoItems() {
  const now = Date.now();
  const stamp = (offset) => new Date(now - offset * 3600000).toISOString();
  const make = (
    id,
    kind,
    area,
    title,
    body,
    meta = {},
    status = "active",
    age = 2,
  ) => ({
    id,
    kind,
    area,
    title,
    body,
    meta,
    status,
    created_at: stamp(age),
    updated_at: stamp(age),
    space_id: "demo-personal",
  });
  return [
    make(
      "d1",
      "task",
      "work",
      "梳理工作台的第一版功能",
      "确定需要先实现的功能与优先级。",
      {
        tag: "产品规划",
        priority: "high",
        due: new Intl.DateTimeFormat("en-CA", {
          timeZone: "Asia/Shanghai",
        }).format(new Date()),
      },
      "active",
      1,
    ),
    make(
      "d2",
      "task",
      "work",
      "准备周五的项目分享",
      "整理本周进展，留下需要大家讨论的问题。",
      { tag: "协作", due: "" },
      "active",
      4,
    ),
    make(
      "d3",
      "task",
      "work",
      "整理灵感与参考资料",
      "把零散的记录放到同一个地方。",
      { tag: "知识整理" },
      "active",
      8,
    ),
    make(
      "d4",
      "task",
      "work",
      "留出半小时，回顾这一周",
      "",
      { tag: "个人成长" },
      "done",
      12,
    ),
    make(
      "n1",
      "note",
      "work",
      "关于工作台的一些想法",
      "工具不必一次拥有所有功能。\n先把每天最重要的事放在手边，让空间随着需要一起成长。",
      { tag: "产品想法" },
      "active",
      1,
    ),
    make(
      "n2",
      "note",
      "work",
      "少一点切换，多一点专注",
      "把同一件事的资料放在一起。\n给深度工作留一个不被打扰的时间段，做完之后再处理消息。",
      { tag: "工作方法" },
      "active",
      5,
    ),
    make(
      "n3",
      "note",
      "work",
      "下一次分享的开场",
      "从一个真实的小问题开始讲。\n比起罗列所有功能，更想分享让事情变简单的那个瞬间。",
      { tag: "随手记" },
      "active",
      26,
    ),
    make(
      "m1",
      "media",
      "work",
      "一段北岸的风景",
      "",
      {
        demoPreview: "landscape",
        mime: "image/demo",
        name: "示例风景 · 无实际文件",
        size: 0,
      },
      "active",
      3,
    ),
    make(
      "m2",
      "media",
      "work",
      "项目规划与参考",
      "",
      {
        demoPreview: "document",
        mime: "application/pdf",
        name: "示例文档 · 无实际文件",
        size: 0,
      },
      "active",
      8,
    ),
    make(
      "m3",
      "media",
      "work",
      "想到就说下来",
      "",
      {
        demoPreview: "audio",
        mime: "audio/demo",
        name: "示例录音 · 无实际文件",
        size: 0,
      },
      "active",
      11,
    ),
    make(
      "l1",
      "task",
      "life",
      "安排一个没有日程的周末",
      "找一条散步的小路，给自己留点空白。",
      { tag: "照顾自己" },
      "active",
      5,
    ),
    make(
      "l2",
      "task",
      "life",
      "给家人打个电话",
      "问问他们这一周过得怎么样。",
      { tag: "家人" },
      "active",
      8,
    ),
    make(
      "l3",
      "task",
      "life",
      "读完床头的那本书",
      "",
      { tag: "阅读" },
      "done",
      22,
    ),
    make(
      "ln1",
      "note",
      "life",
      "生活里的小确幸",
      "今天出门时刚好碰到阳光。\n路边的桂花开了，慢一点才能闻到。",
      { tag: "日常片刻" },
      "active",
      2,
    ),
    make(
      "ln2",
      "note",
      "life",
      "下一个目的地",
      "找一个可以走路逛的小城。\n不用赶景点，留一个下午坐在咖啡馆里。",
      { tag: "旅行灵感" },
      "active",
      20,
    ),
    make(
      "ln3",
      "note",
      "life",
      "想做给自己的晚餐",
      "番茄炖牛肉，配一小碗米饭。\n把音乐打开，认真过一个平常的晚上。",
      { tag: "生活清单" },
      "active",
      32,
    ),
  ];
}
function enterDemo() {
  state.demo = true;
  state.user = {
    id: "demo-user",
    name: "知序体验者",
    email: "demo@local.example",
  };
  state.spaces = [
    { id: "demo-personal", name: "我的空间", role: "owner", member_count: 1 },
    { id: "demo-team", name: "一起做点事", role: "owner", member_count: 3 },
  ];
  state.demoStore = {
    "demo-personal": demoItems(),
    "demo-team": [
      {
        id: "dt1",
        kind: "task",
        area: "work",
        title: "一起梳理下一步计划",
        body: "演示协作空间内独立的任务列表。",
        status: "active",
        meta: { tag: "团队" },
        created_at: new Date().toISOString(),
      },
    ],
  };
  state.area = "work";
  state.view = "overview";
  enterApp();
}
async function enterApp() {
  const session = ++state.sessionEpoch;
  state.request++;
  state.items = [];
  state.spaces = [];
  state.space = null;
  $("#new-button").disabled = true;
  $("#mobile-new").disabled = true;
  if (state.demo)
    state.spaces = [
      { id: "demo-personal", name: "我的空间", role: "owner", member_count: 1 },
      { id: "demo-team", name: "一起做点事", role: "owner", member_count: 3 },
    ];
  loading($("#view-content"));
  $("#current-space-name").textContent = "正在打开空间";
  $("#current-space-role").textContent = "";
  $("#auth-screen").hidden = true;
  $("#app-shell").hidden = false;
  $("#demo-banner").hidden = !state.demo;
  $("#user-name").textContent = state.user.name || "我的账户";
  $("#user-avatar").textContent = (state.user.name || "知").slice(0, 1);
  $("#user-status").textContent = state.demo ? "本地演示" : "个人账户";
  $("#sync-status").replaceChildren(
    el("span"),
    document.createTextNode(state.demo ? "本地演示" : "空间已连接"),
  );
  if (!state.demo) {
    try {
      const result = await api("/api/spaces");
      if (session !== state.sessionEpoch) return;
      state.spaces = result.spaces || result;
    } catch (err) {
      if (session !== state.sessionEpoch) return;
      toast(errorMessage(err), true);
      showAuth();
      return;
    }
  }
  if (!state.spaces.length) {
    showCreateSpace();
    render();
    return;
  }
  const preferred =
    state.spaces.find((s) => s.id === sessionStorage.getItem("zhixu-space")) ||
    state.spaces[0];
  await switchSpace(preferred.id);
}
function bedtimeBoundary(kind, spaceId = state.space?.id) {
  if (state.demo || !state.user) return;
  try {
    const prefix = `zhixu:bedtime:stories:${state.user.id}:`;
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith(prefix) && (kind === "logout" || key === prefix + spaceId))
        localStorage.removeItem(key);
    }
    localStorage.setItem("zhixu:session-event", JSON.stringify({
      kind, userId: state.user.id, spaceId, at: Date.now(),
    }));
  } catch { /* Account cleanup does not depend on available browser storage. */ }
}
function openBedtime() {
  if (state.demo) {
    toast("晚安故事使用真实账号与空间，请先登录或创建账户。");
    return;
  }
  if (state.space) location.href = `/bedtime.html?space=${encodeURIComponent(state.space.id)}`;
}
function showAuth() {
  bedtimeBoundary("logout");
  ++state.sessionEpoch;
  ++state.authRequest;
  ++state.request;
  closeModal();
  closeDrawer(false);
  for (const media of $$("audio, video")) {
    media.pause();
    media.removeAttribute("src");
    media.load();
  }
  for (const url of state.objectURLs) URL.revokeObjectURL(url);
  state.objectURLs.clear();
  lockVault(false);
  state.demo = false;
  state.demoStore = {};
  state.demoVaults = {};
  state.user = null;
  state.space = null;
  state.spaces = [];
  state.items = [];
  state.view = "overview";
  state.vault.payload = undefined;
  state.vault.loaded = false;
  state.vault.loading = false;
  state.vault.spaceId = null;
  state.loading = false;
  $("#view-content").replaceChildren();
  $("#current-space-name").textContent = "我的空间";
  $("#current-space-role").textContent = "";
  $("#user-name").textContent = "";
  $("#user-status").textContent = "";
  $("#footer-space").textContent = "";
  $("#page-title").textContent = names.overview;
  $("#auth-password").value = "";
  $("#auth-invitation").value = "";
  $("#auth-submit").disabled = false;
  $("#auth-screen").hidden = false;
  $("#app-shell").hidden = true;
  document.body.classList.remove("life-area");
}
window.addEventListener("storage", (event) => {
  if (event.key !== "zhixu:session-event" || !event.newValue ||
      !state.user || state.demo) return;
  let boundary;
  try { boundary = JSON.parse(event.newValue); } catch { return; }
  if ((boundary.kind === "logout" && boundary.userId === state.user.id) ||
      (boundary.kind === "login" && boundary.userId !== state.user.id)) {
    showAuth();
    toast("登录状态已在另一页面变更，请重新登录。");
  }
});
async function switchSpace(id) {
  const space = state.spaces.find((s) => s.id === id);
  if (!space) return;
  if (state.space && state.space.id !== id) bedtimeBoundary("spacechange", state.space.id);
  lockVault(false);
  closeModal();
  closeDrawer(false);
  state.space = space;
  state.vault.payload = undefined;
  state.vault.loaded = false;
  state.vault.loading = false;
  state.vault.spaceId = id;
  state.items = [];
  sessionStorage.setItem("zhixu-space", id);
  $("#current-space-name").textContent = space.name;
  $("#current-space-role").textContent =
    `${space.member_count > 1 ? "共享空间" : "独立空间"} · ${roleNames[space.role] || "成员"}`;
  $("#footer-space").textContent = `${space.name} · 数据按空间隔离`;
  $("#new-button").disabled = !writable();
  $("#mobile-new").disabled = !writable();
  await loadItems();
}
async function loadItems() {
  const token = ++state.request;
  state.loading = true;
  render();
  try {
    if (state.demo) state.items = state.demoStore[state.space.id] || [];
    else {
      const result = await api(`/api/spaces/${state.space.id}/items`);
      if (token !== state.request) return;
      state.items = result.items || result;
    }
    state.loading = false;
    render();
  } catch (err) {
    if (token !== state.request) return;
    state.loading = false;
    const panel = el("div", "error-panel");
    panel.append(
      el("p", "", errorMessage(err)),
      button("重新加载", "button secondary", loadItems),
    );
    $("#view-content").replaceChildren(panel);
    toast(errorMessage(err), true);
  }
}
function navigate(view) {
  if (view === "bedtime") { openBedtime(); return; }
  if (!names[view]) return;
  state.view = view;
  closeModal();
  closeDrawer(false);
  render();
  if (mobileLayout.matches) {
    window.scrollTo({ top: 0, behavior: "instant" });
    $("#main-content").focus({ preventScroll: true });
  }
  if (view === "vault") loadVault();
}
function setArea(area) {
  state.area = area;
  state.taskFilter = "all";
  state.mediaFilter = "all";
  closeModal();
  render();
}
function render() {
  document.body.classList.toggle("life-area", state.area === "life");
  $$(".nav-item, [data-mobile-view]").forEach((b) => {
    const view = b.dataset.view || b.dataset.mobileView;
    b.classList.toggle("active", view === state.view);
    if (view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $("#mobile-more").classList.toggle("active", !["overview", "tasks", "notes"].includes(state.view));
  $("#mobile-new").disabled = !writable();
  $$("[data-area]").forEach((b) => {
    b.classList.toggle("active", b.dataset.area === state.area);
    b.setAttribute("aria-pressed", String(b.dataset.area === state.area));
  });
  $("#page-title").textContent =
    state.area === "life" && state.view === "overview"
      ? "给生活，留一点空白。"
      : names[state.view];
  $("#page-description").textContent =
    state.area === "life" && state.view === "overview"
      ? "记下喜欢的瞬间，照顾那些重要的小事。"
      : descriptions[state.view];
  $("#page-eyebrow").textContent =
    state.view === "overview"
      ? state.area === "work"
        ? "知序 · 个人工作台"
        : "生活，也是重要的事"
      : "";
  $("#page-eyebrow").hidden = state.view !== "overview";
  $("#topbar-description").textContent =
    state.area === "work" ? "专注当下，把想法变成进展" : "放慢一点，让日常有光";
  $("#nav-task-count").textContent = currentItems("task").filter(
    (t) => t.status !== "done",
  ).length;
  const content = $("#view-content");
  content.replaceChildren();
  if (state.loading) {
    loading(content);
    return;
  }
  if (!state.space) {
    emptyState(
      content,
      "users",
      "先创建一个空间",
      "工作与生活可以在同一个空间分别记录，也可以建立多个独立空间。",
      "创建空间",
      showCreateSpace,
    );
    return;
  }
  ({
    overview: renderOverview,
    tasks: renderTasks,
    notes: renderNotes,
    media: renderMedia,
    vault: renderVault,
    spaces: renderSpaces,
    modules: renderModules,
  })[state.view](content);
}

function cardHeading(title, iconName, actionText, action, tag) {
  const heading = el("div", "card-heading");
  const titleWrap = el("div", "card-title");
  titleWrap.append(svg(iconName), el("h2", "", title));
  if (tag) titleWrap.append(el("span", "mini-tag", tag));
  heading.append(titleWrap);
  if (actionText) heading.append(button(actionText, "text-button", action));
  return heading;
}
function sectionHeading(title, subtitle, view) {
  const heading = el("div", "section-heading");
  const left = el("div");
  left.append(el("h2", "", title));
  if (subtitle) left.append(el("span", "section-caption", subtitle));
  heading.append(
    left,
    button("查看全部 ↗", "text-button", () => navigate(view)),
  );
  return heading;
}
function renderOverview(content) {
  const tasks = currentItems("task"),
    notes = currentItems("note"),
    media = currentItems("media");
  const stats = el("div", "stats-grid");
  for (const [iconName, title, value, detail, color, view] of [
    [
      "task",
      "待完成事项",
      tasks.filter((t) => t.status !== "done").length,
      "件待办",
      "",
      "tasks",
    ],
    ["note", "留下的灵感", notes.length, "条记录", "orange", "notes"],
    ["folder", "媒体资料", media.length, "份文件", "blue", "media"],
    ["users", "我的空间", state.spaces.length, "个空间", "purple", "spaces"],
  ]) {
    const card = button("", "stat-card", () => navigate(view));
    const icon = el("span", `stat-icon ${color}`);
    icon.append(svg(iconName));
    const body = el("div");
    body.append(el("small", "", title));
    const valueWrap = el("div", "stat-value");
    valueWrap.append(el("strong", "", value), el("span", "", detail));
    body.append(valueWrap);
    card.append(icon, body);
    stats.append(card);
  }
  content.append(stats);
  const grid = el("div", "overview-grid");
  const taskCard = el("section", "card");
  taskCard.append(
    cardHeading(
      state.area === "work" ? "今天，先做这几件事" : "给自己的一点安排",
      "task",
      "全部待办 ↗",
      () => navigate("tasks"),
      "我的节奏",
    ),
  );
  const list = el("div", "task-list");
  const sorted = [...tasks].sort(
    (a, b) => Number(a.status === "done") - Number(b.status === "done"),
  );
  if (sorted.length)
    sorted.slice(0, 4).forEach((item) => list.append(taskRow(item)));
  else
    emptyState(
      list,
      "task",
      "今天想推进什么？",
      "从一件小事开始，给想做的事情一个位置。",
      writable() ? "添加第一件待办" : null,
      () => showEditor("task"),
    );
  taskCard.append(list);
  const foot = el("div", "card-action-footer");
  foot.append(
    button("添加待办", "add-task-button", () => showEditor("task"), "plus"),
    el(
      "span",
      "",
      tasks.length
        ? `${tasks.filter((t) => t.status === "done").length} / ${tasks.length} 已完成`
        : "一步一步，自有进展",
    ),
  );
  $("button", foot).disabled = !writable();
  taskCard.append(foot);
  grid.append(taskCard);
  const capture = el("section", "card quick-capture");
  capture.append(cardHeading("想到，就记下来", "note"));
  const inner = el("div", "capture-inner");
  const text = el("textarea", "capture-input");
  text.placeholder =
    state.area === "work"
      ? "一个想法、一段灵感，或者今天的思考…"
      : "今天的小确幸，想去的地方，想做的事…";
  text.maxLength = 200000;
  text.setAttribute("aria-label", "快速记录");
  text.disabled = !writable();
  const bottom = el("div", "capture-bottom");
  const mic = iconButton("mic", "语音录入", showVoice);
  const upload = iconButton("upload", "上传媒体", chooseFiles);
  const save = button("保存记录", "button primary", async () => {
    if (!text.value.trim()) {
      text.focus();
      return;
    }
    save.disabled = true;
    try {
      const body = text.value.trim();
      await saveItem({
        kind: "note",
        area: state.area,
        title: body.split("\n")[0].slice(0, 200),
        body,
        status: "active",
        meta: { tag: "随手记" },
      });
      text.value = "";
      toast(state.demo ? "记录已加入本地演示" : "记录已保存");
    } catch (err) {
      toast(errorMessage(err), true);
      save.disabled = false;
    }
  });
  [mic, upload, save].forEach((b) => (b.disabled = !writable()));
  bottom.append(mic, upload, save);
  inner.append(
    text,
    bottom,
    el(
      "p",
      "capture-hint",
      state.demo
        ? "演示记录仅保留在当前页面"
        : "记录到当前空间，与工作 / 生活分区同步",
    ),
  );
  capture.append(inner);
  grid.append(capture);
  content.append(grid);
  content.append(sectionHeading("最近的灵感与笔记", "想法值得被留下", "notes"));
  const noteGrid = el("div", "notes-grid overview-notes");
  if (notes.length)
    notes.slice(0, 3).forEach((item) => noteGrid.append(noteCard(item)));
  else {
    const panel = el("section", "card");
    emptyState(
      panel,
      "note",
      "让第一个想法留下来",
      "文字、语音或灵感，都可以成为一条记录。",
      writable() ? "写一条笔记" : null,
      () => showEditor("note"),
    );
    noteGrid.append(panel);
  }
  content.append(noteGrid);
  const lower = el("div", "lower-grid");
  const mediaCard = el("section", "card media-card");
  mediaCard.append(
    cardHeading("随手可得的资料", "folder", "打开资料库 ↗", () =>
      navigate("media"),
    ),
  );
  const mediaGrid = el("div", "media-grid");
  if (media.length)
    media.slice(0, 3).forEach((item) => mediaGrid.append(mediaTile(item)));
  else {
    emptyState(
      mediaCard,
      "upload",
      "把资料放在手边",
      "上传图片、视频、音频或 PDF，随时预览。",
      writable() ? "上传第一份资料" : null,
      chooseFiles,
    );
  }
  if (media.length) mediaCard.append(mediaGrid);
  lower.append(mediaCard);
  const spaceSummary = el("section", "space-summary");
  spaceSummary.append(
    el(
      "h2",
      "",
      state.space.member_count > 1
        ? "一起，把事情做好"
        : "有自己的空间，也能一起",
    ),
  );
  spaceSummary.append(
    el(
      "p",
      "",
      state.space.member_count > 1
        ? "共享记录与资料，用明确的权限协作。"
        : "把项目分享给伙伴，让协作自然发生。",
    ),
  );
  const members = el("div", "space-members");
  const stack = el("div", "avatar-stack");
  stack.append(el("span", "", (state.user.name || "我").slice(0, 1)));
  if (state.demo && state.space.member_count > 1)
    stack.append(el("span", "", "林"), el("span", "", "陈"));
  members.append(
    stack,
    el(
      "span",
      "",
      `${state.space.member_count || 1} 位成员 · ${roleNames[state.space.role]}`,
    ),
  );
  spaceSummary.append(
    members,
    button("管理空间与协作 ↗", "button", () => navigate("spaces")),
  );
  const art = el("span", "space-summary-art");
  art.append(svg("users"));
  spaceSummary.append(art);
  lower.append(spaceSummary);
  content.append(lower);
}
function taskRow(item) {
  const row = el("div", `task-row${item.status === "done" ? " done" : ""}`);
  const check = button(
    "",
    `task-check${item.status === "done" ? " checked" : ""}`,
    async () => {
      if (!ensureWrite()) return;
      check.disabled = true;
      try {
        await saveItem(
          { ...item, status: item.status === "done" ? "active" : "done" },
          item.id,
        );
      } catch (err) {
        toast(errorMessage(err), true);
        check.disabled = false;
      }
    },
  );
  check.setAttribute(
    "aria-label",
    item.status === "done"
      ? `将「${item.title}」标为未完成`
      : `完成「${item.title}」`,
  );
  check.setAttribute("aria-pressed", String(item.status === "done"));
  check.disabled = !writable();
  if (item.status === "done") check.append(svg("check"));
  const info = button("", "task-info", () => showEditor("task", item));
  info.append(el("strong", "", item.title));
  const meta = item.meta || {};
  const sub = [
    meta.due ? `计划 ${meta.due}` : "不赶时间，一步一步",
    meta.tag ? ` · ${meta.tag}` : "",
  ].join("");
  info.append(el("small", "", sub));
  row.append(check, info);
  if (meta.priority === "high" && item.status !== "done")
    row.append(el("span", "tag orange priority-tag", "优先"));
  row.append(
    el(
      "span",
      `tag${state.area === "life" ? " orange" : ""}`,
      meta.tag || (state.area === "life" ? "生活" : "工作"),
    ),
  );
  row.append(iconButton("more", "查看待办", () => showEditor("task", item)));
  return row;
}
function noteCard(item) {
  const card = button("", "note-card", () => showEditor("note", item));
  card.append(
    el("span", "note-label", item.meta?.tag || "随手记"),
    el("h3", "", item.title),
    el("p", "", item.body || "点击查看这条记录"),
  );
  const foot = el("div", "note-footer");
  foot.append(
    el("span", "", relativeDate(item.updated_at || item.created_at)),
    svg("note"),
  );
  card.append(foot);
  return card;
}
function mediaType(item) {
  const mime = item.meta?.mime || "";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  return "document";
}
function mediaURL(item) {
  if (item.meta?.localURL)
    return state.objectURLs.has(item.meta.localURL) ? item.meta.localURL : "";
  const id = item.meta?.fileId;
  if (id && /^[a-f0-9-]+$/i.test(id)) return `/api/files/${id}`;
  const url = item.meta?.url;
  return typeof url === "string" && /^\/api\/files\/[a-f0-9-]+$/i.test(url)
    ? url
    : "";
}
function mediaTile(item) {
  const tile = button("", "media-tile", () => showMedia(item));
  const thumb = el("div", "media-thumb");
  const type = mediaType(item);
  const url = mediaURL(item);
  if (type === "image" && url) {
    const img = el("img");
    img.src = url;
    img.alt = item.title;
    img.loading = "lazy";
    thumb.append(img);
  } else if (item.meta?.demoPreview === "landscape")
    thumb.append(el("div", "demo-landscape"));
  else if (item.meta?.demoPreview === "document")
    thumb.append(el("div", "demo-doc"));
  else if (item.meta?.demoPreview === "audio") {
    const wave = el("div", "demo-wave");
    [
      9, 18, 28, 16, 36, 25, 13, 31, 20, 38, 23, 14, 29, 19, 10, 25, 16, 8,
    ].forEach((height) => {
      const bar = el("span");
      bar.style.height = `${height}px`;
      wave.append(bar);
    });
    thumb.append(wave);
  } else
    thumb.append(
      svg(
        type === "image"
          ? "image"
          : type === "audio"
            ? "mic"
            : type === "video"
              ? "play"
              : "note",
      ),
    );
  thumb.append(
    el(
      "span",
      "media-kind",
      { image: "图片", video: "视频", audio: "音频", document: "文档" }[type],
    ),
  );
  tile.append(
    thumb,
    el("h3", "", item.title),
    el(
      "small",
      "",
      item.meta?.demoPreview
        ? "演示样例"
        : `${sizeString(item.meta?.size)} · ${shortDate(item.created_at)}`,
    ),
  );
  return tile;
}
function toolbar(content, actionText, action, iconName = "plus") {
  const bar = el("div", "view-toolbar");
  const info = el(
    "span",
    "item-count",
    `${currentItems().length} 条记录 · ${state.space.name}`,
  );
  bar.append(info);
  if (writable())
    bar.append(button(actionText, "button primary", action, iconName));
  else bar.append(el("span", "readonly-pill", "这个空间为只读"));
  content.append(bar);
  return bar;
}
function renderTasks(content) {
  const tasks = currentItems("task");
  const bar = toolbar(content, "添加待办", () => showEditor("task"));
  const tabs = el("div", "view-tabs");
  for (const [key, text] of [
    ["all", "全部"],
    ["active", "待完成"],
    ["done", "已完成"],
  ]) {
    const b = button(
      `${text} ${tasks.filter((t) => key === "all" || (key === "done" ? t.status === "done" : t.status !== "done")).length}`,
      state.taskFilter === key ? "active" : "",
      () => {
        state.taskFilter = key;
        render();
      },
    );
    tabs.append(b);
  }
  bar.replaceChild(tabs, bar.firstChild);
  const card = el("section", "card");
  const list = el("div", "task-list");
  const filtered = tasks
    .filter(
      (t) =>
        state.taskFilter === "all" ||
        (state.taskFilter === "done"
          ? t.status === "done"
          : t.status !== "done"),
    )
    .sort((a, b) => Number(a.status === "done") - Number(b.status === "done"));
  if (filtered.length) filtered.forEach((item) => list.append(taskRow(item)));
  else
    emptyState(
      list,
      "task",
      state.taskFilter === "done"
        ? "完成的事情，会在这里"
        : "从一件想做的事开始",
      state.taskFilter === "done"
        ? "完成待办之后，这里会留下你的进展。"
        : "给待办设置标签和计划日期，找到自己的节奏。",
      writable() && state.taskFilter !== "done" ? "添加待办" : null,
      () => showEditor("task"),
    );
  card.append(list);
  content.append(card);
}
function renderNotes(content) {
  toolbar(content, "写一条笔记", () => showEditor("note"));
  const grid = el("div", "notes-grid notes-full");
  const notes = currentItems("note");
  if (notes.length) notes.forEach((item) => grid.append(noteCard(item)));
  else
    emptyState(
      grid,
      "note",
      "想法不必完整，先记下来",
      "一句话、一段思考、一次语音，都能成为下一步的起点。",
      writable() ? "留下第一条记录" : null,
      () => showEditor("note"),
    );
  content.append(grid);
}
function renderMedia(content) {
  const bar = toolbar(content, "上传文件", chooseFiles, "upload");
  const tabs = el("div", "view-tabs");
  for (const [key, text] of [
    ["all", "全部"],
    ["image", "图片"],
    ["video", "视频"],
    ["audio", "音频"],
    ["document", "文档"],
  ])
    tabs.append(
      button(text, state.mediaFilter === key ? "active" : "", () => {
        state.mediaFilter = key;
        render();
      }),
    );
  bar.replaceChild(tabs, bar.firstChild);
  const note = el(
    "p",
    "view-top-note",
    "每份文件最大 25 MB。可以将文件拖到此处，或通过语音录入保存录音。",
  );
  content.append(note);
  const items = currentItems("media").filter(
    (item) =>
      state.mediaFilter === "all" || mediaType(item) === state.mediaFilter,
  );
  const grid = el("div", "view-media-grid");
  if (items.length) items.forEach((item) => grid.append(mediaTile(item)));
  else
    emptyState(
      grid,
      "folder",
      "把资料放进你的空间",
      "支持图片、视频、音频、PDF 与文本文件。上传后，点击资料即可预览。",
      writable() ? "上传资料" : null,
      chooseFiles,
    );
  content.append(grid);
}
function renderSpaces(content) {
  const bar = el("div", "view-toolbar");
  bar.append(
    el(
      "span",
      "item-count",
      `${state.spaces.length} 个空间 · 每个空间独立授权`,
    ),
    button("创建共享空间", "button primary", showCreateSpace, "plus"),
  );
  content.append(bar);
  const grid = el("div", "space-grid");
  state.spaces.forEach((space) => {
    const card = el(
      "section",
      `card space-card${space.id === state.space.id ? " active-space" : ""}`,
    );
    const icon = el("span", "space-card-icon");
    icon.append(svg(space.member_count > 1 ? "users" : "folder"));
    card.append(
      icon,
      el("h3", "", space.name),
      el(
        "p",
        "",
        `${space.member_count || 1} 位成员 · ${roleNames[space.role] || "成员"}${space.id === state.space.id ? " · 当前空间" : ""}`,
      ),
    );
    const row = el("div");
    row.append(
      button(
        space.id === state.space.id ? "管理成员" : "进入空间",
        "button secondary",
        () =>
          space.id === state.space.id
            ? showMembers(space)
            : switchSpace(space.id),
        space.id === state.space.id ? "users" : "arrow",
      ),
    );
    if (space.id !== state.space.id)
      row.append(button("查看成员", "text-button", () => showMembers(space)));
    card.append(row);
    grid.append(card);
  });
  content.append(grid);
  const note = el("div", "module-note");
  note.append(
    svg("shield"),
    el(
      "span",
      "",
      "工作与生活分别记录；空间决定谁能访问，成员权限决定能否编辑。私密密码箱在每个空间内独立归属个人。",
    ),
  );
  content.append(note);
}
function renderModules(content) {
  const modules = [
    [
      "cloud",
      "晚安故事",
      "独立寻找故事，切换朗读声音，收藏文本与轻声入眠。",
      "bedtime",
      true,
    ],
    [
      "task",
      "待办事项",
      "把需要推进的事情记下来，完成后留下进展。",
      "tasks",
      true,
    ],
    [
      "note",
      "灵感与笔记",
      "文字与语音都能成为记录，工作与生活各有去处。",
      "notes",
      true,
    ],
    [
      "folder",
      "媒体资料库",
      "上传图片、视频、音频与 PDF，直接在空间中预览。",
      "media",
      true,
    ],
    [
      "lock",
      "私密密码箱",
      "个人内容在浏览器中加密，用独立密码开启。",
      "vault",
      true,
    ],
    [
      "users",
      "空间与协作",
      "邀请已注册的伙伴，设置编辑或只读权限。",
      "spaces",
      true,
    ],
    [
      "puzzle",
      "AI 助手",
      "对话、摘要与任务建议将在接入模型服务后开放。",
      "",
      false,
    ],
  ];
  const grid = el("div", "module-grid");
  modules.forEach(([iconName, title, text, view, enabled]) => {
    const card = el("section", "card module-card");
    const top = el("div");
    const icon = el("span", "module-icon");
    icon.append(svg(iconName));
    top.append(
      icon,
      el(
        "span",
        `tag${enabled ? "" : " orange"}`,
        enabled ? "已可使用" : "待接入",
      ),
    );
    card.append(top, el("h3", "", title), el("p", "", text));
    if (enabled)
      card.append(button("打开功能", "text-button", () => navigate(view)));
    else card.append(el("div", "helper", "需要配置模型服务与独立 API 密钥"));
    grid.append(card);
  });
  content.append(grid);
  const note = el("div", "module-note");
  note.append(
    svg("puzzle"),
    el(
      "span",
      "",
      "按需添加，保持轻盈。当前版本包含记录、资料、协作和密码箱；AI、日历与更多工具将按实际需求继续接入。",
    ),
  );
  content.append(note);
}

async function saveItem(data, id) {
  if (!ensureWrite()) throw new Error("没有编辑权限");
  const sid = state.space.id,
    session = state.sessionEpoch;
  const payload = Object.fromEntries(
    fieldKeys.map((key) => [key, data[key] ?? (key === "meta" ? {} : "")]),
  );
  let result;
  if (state.demo) {
    result = {
      item: {
        ...data,
        id: id || `local-${crypto.randomUUID()}`,
        space_id: sid,
        created_at: data.created_at || new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    };
    const store = state.demoStore[sid];
    const index = store.findIndex((item) => item.id === id);
    if (index >= 0) store[index] = result.item;
    else store.unshift(result.item);
  } else
    result = await api(
      id ? `/api/items/${id}` : `/api/spaces/${sid}/items`,
      id ? "PATCH" : "POST",
      payload,
    );
  if (state.space?.id === sid && state.sessionEpoch === session) {
    const index = state.items.findIndex((item) => item.id === result.item.id);
    if (index >= 0) state.items[index] = result.item;
    else if (!state.demo) state.items.unshift(result.item);
    render();
  }
  return result.item;
}
async function deleteItem(item) {
  if (!ensureWrite()) return;
  const sid = state.space.id,
    session = state.sessionEpoch,
    demo = state.demo,
    store = demo ? state.demoStore[sid] : state.items;
  if (!demo) await api(`/api/items/${item.id}`, "DELETE");
  const index = store.findIndex((t) => t.id === item.id);
  if (index >= 0) store.splice(index, 1);
  if (item.meta?.localURL) {
    URL.revokeObjectURL(item.meta.localURL);
    state.objectURLs.delete(item.meta.localURL);
  }
  if (state.space?.id === sid && state.sessionEpoch === session) {
    render();
    toast(demo ? "已从本地演示移除" : "已删除");
  }
}
function confirmDelete(item) {
  showModal("删除这条记录？", (body) => {
    body.append(
      el(
        "p",
        "modal-body-note",
        `「${item.title}」删除后无法恢复。${item.kind === "media" ? "对应的媒体文件也会删除。" : ""}`,
      ),
    );
    const error = el("p", "form-error");
    const remove = button("确认删除", "button danger", async () => {
      remove.disabled = true;
      try {
        await deleteItem(item);
        closeModal();
      } catch (err) {
        error.textContent = errorMessage(err);
        remove.disabled = false;
      }
    });
    body.append(
      error,
      actionsRow(button("取消", "button secondary", closeModal), remove),
    );
  });
}
function showEditor(kind, item) {
  if (!item && !ensureWrite()) return;
  const canEdit = writable();
  showModal(
    item
      ? kind === "task"
        ? "这件待办"
        : "这条笔记"
      : kind === "task"
        ? "添加一件待办"
        : "留下一个想法",
    (body) => {
      const form = el("form", "editor-main");
      const title = input(
        "text",
        kind === "task" ? "想推进哪件事？" : "给这条记录起个名字",
        item?.title || "",
      );
      title.required = true;
      title.maxLength = 200;
      const text = el("textarea");
      text.placeholder =
        kind === "task" ? "补充说明、要点或参考…" : "一个想法，也值得留下来。";
      text.value = item?.body || "";
      text.maxLength = 200000;
      form.append(
        labelField("标题", title),
        labelField(kind === "task" ? "补充说明" : "正文", text),
      );
      const meta = el("div", "editor-meta");
      const area = select(
        [
          ["work", "工作"],
          ["life", "生活"],
        ],
        item?.area || state.area,
      );
      const tag = input(
        "text",
        "例如：项目、阅读、旅行",
        item?.meta?.tag || "",
      );
      tag.maxLength = 40;
      meta.append(labelField("所属分区", area), labelField("标签", tag));
      let due, priority, status;
      if (kind === "task") {
        due = input("date", "", item?.meta?.due || "");
        priority = select(
          [
            ["normal", "普通"],
            ["high", "优先"],
          ],
          item?.meta?.priority || "normal",
        );
        status = select(
          [
            ["active", "待完成"],
            ["done", "已完成"],
          ],
          item?.status === "done" ? "done" : "active",
        );
        meta.append(
          labelField("计划日期", due),
          labelField("优先级", priority),
          labelField("当前状态", status),
        );
      }
      form.append(meta);
      const error = el("p", "form-error");
      const save = el(
        "button",
        "button primary",
        state.demo ? "保存到演示空间" : "保存记录",
      );
      save.type = "submit";
      const cancel = button(
        canEdit ? "取消" : "关闭",
        "button secondary",
        closeModal,
      );
      const buttons = [];
      if (item && canEdit)
        buttons.push(
          button("删除", "button danger", () => confirmDelete(item), "trash"),
        );
      buttons.push(cancel);
      if (canEdit) buttons.push(save);
      else
        form.append(
          el("p", "helper", "你拥有当前空间的只读权限，可以查看记录。"),
        );
      form.append(error, actionsRow(...buttons));
      if (!canEdit)
        $$("input,textarea,select", form).forEach(
          (control) => (control.disabled = true),
        );
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!canEdit) return;
        save.disabled = true;
        error.textContent = "";
        try {
          const fields = {
            kind,
            area: area.value,
            title: title.value.trim(),
            body: text.value,
            status: status?.value || item?.status || "active",
            meta: { ...(item?.meta || {}), tag: tag.value.trim() },
          };
          if (kind === "task") {
            fields.meta.due = due.value;
            fields.meta.priority = priority.value;
          }
          await saveItem(fields, item?.id);
          closeModal();
          toast(state.demo ? "已保存到本地演示" : "记录已保存");
        } catch (err) {
          error.textContent = errorMessage(err);
          save.disabled = false;
        }
      });
      body.append(form);
    },
  );
}
function showNew() {
  if (!ensureWrite()) return;
  showModal("给想法一个位置", (body) => {
    const options = el("div", "new-options");
    for (const [iconName, title, text, action] of [
      ["task", "待办事项", "记录接下来要做的事", () => showEditor("task")],
      ["note", "灵感笔记", "留住此刻的想法", () => showEditor("note")],
      ["mic", "语音录入", "说下来，保存为录音", showVoice],
      ["upload", "上传资料", "图片、视频、音频与文档", chooseFiles],
    ]) {
      const b = button("", "new-option", action);
      b.append(svg(iconName), el("strong", "", title), el("small", "", text));
      options.append(b);
    }
    body.append(options);
  });
}
function chooseFiles() {
  if (!ensureWrite()) return;
  closeModal();
  $("#file-input").value = "";
  $("#file-input").click();
}
async function uploadFiles(files) {
  if (!ensureWrite() || !files.length) return 0;
  const sid = state.space.id,
    area = state.area,
    session = state.sessionEpoch,
    demo = state.demo;
  let completed = 0;
  for (const file of files) {
    if (state.sessionEpoch !== session || state.space?.id !== sid) break;
    try {
      if (file.size < 1 || file.size > 25 * 1024 * 1024)
        throw new Error(`「${file.name}」须在 1 字节到 25 MB 之间。`);
      let item;
      if (state.demo) {
        const url = URL.createObjectURL(file);
        state.objectURLs.add(url);
        item = {
          id: `local-${crypto.randomUUID()}`,
          kind: "media",
          area,
          title: file.name,
          body: "",
          status: "active",
          created_at: new Date().toISOString(),
          meta: {
            name: file.name,
            mime: file.type,
            size: file.size,
            localURL: url,
          },
        };
        state.demoStore[sid].unshift(item);
      } else {
        const data = new FormData();
        data.append("file", file);
        data.append("area", area);
        const result = await api(`/api/spaces/${sid}/uploads`, "POST", data);
        item = result.item;
      }
      if (state.space?.id === sid && state.sessionEpoch === session && !demo)
        state.items.unshift(item);
      completed++;
    } catch (err) {
      toast(errorMessage(err), true);
    }
  }
  if (completed && state.sessionEpoch === session) {
    if (state.space?.id === sid) render();
    toast(
      demo ? `${completed} 份文件已加入本地演示` : `已上传 ${completed} 份文件`,
    );
  }
  return completed;
}
function showMedia(item) {
  showModal(item.title, (body) => {
    const preview = el("div", "media-preview");
    const url = mediaURL(item),
      type = mediaType(item);
    if (url) {
      if (type === "image") {
        const image = el("img");
        image.src = url;
        image.alt = item.title;
        preview.append(image);
      } else if (type === "video" || type === "audio") {
        const node = el(type);
        node.controls = true;
        node.preload = "metadata";
        node.src = url;
        preview.append(node);
        modalCleanup = () => {
          node.pause();
          node.removeAttribute("src");
          node.load();
        };
      } else if (item.meta?.mime === "application/pdf") {
        const frame = el("iframe");
        frame.src = url;
        frame.title = item.title;
        preview.append(frame);
      } else
        preview.append(el("p", "preview-text", "这份文件可以下载后查看。"));
    } else
      preview.append(
        el(
          "p",
          "preview-text",
          "这是一份演示样例，没有实际文件。试着上传自己的图片或录音，即可直接预览。",
        ),
      );
    body.append(preview);
    const details = el("div", "media-details");
    details.append(
      el("span", "", item.meta?.name || item.title),
      el(
        "span",
        "",
        item.meta?.demoPreview ? "演示样例" : sizeString(item.meta?.size),
      ),
      el("span", "", shortDate(item.created_at)),
    );
    body.append(details);
    const actions = [];
    if (writable())
      actions.push(
        button("删除", "button danger", () => confirmDelete(item), "trash"),
      );
    if (url) {
      const link = el("a", "button secondary", "下载文件");
      link.href = url;
      link.download = item.meta?.name || item.title;
      actions.push(link);
    }
    actions.push(button("关闭", "button primary", closeModal));
    body.append(actionsRow(...actions));
  });
}
function showCreateSpace() {
  showModal("创建一个独立空间", (body) => {
    const form = el("form");
    const name = input("text", "例如：个人计划、家庭、小组项目");
    name.required = true;
    name.maxLength = 80;
    form.append(
      labelField("空间名称", name),
      el(
        "p",
        "helper",
        "每个空间分别授权。邀请成员后，对方可以查看空间内的工作和生活记录。私人内容请保留在个人空间或密码箱。",
      ),
    );
    const error = el("p", "form-error");
    const save = el("button", "button primary", "创建空间");
    save.type = "submit";
    form.append(
      error,
      actionsRow(button("取消", "button secondary", closeModal), save),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        let space;
        if (state.demo) {
          space = {
            id: `demo-${crypto.randomUUID()}`,
            name: name.value.trim(),
            role: "owner",
            member_count: 1,
          };
          state.demoStore[space.id] = [];
        } else {
          const result = await api("/api/spaces", "POST", {
            name: name.value.trim(),
          });
          space = result.space;
        }
        state.spaces.push(space);
        await switchSpace(space.id);
        state.view = "overview";
        render();
        toast(state.demo ? "已创建本地演示空间" : "空间已创建");
      } catch (err) {
        error.textContent = errorMessage(err);
        save.disabled = false;
      }
    });
    body.append(form);
  });
}
function showSpaceSwitcher() {
  showModal("切换空间", (body) => {
    const choices = el("div", "space-choices");
    state.spaces.forEach((space) => {
      const b = button(
        "",
        `space-choice${space.id === state.space?.id ? " active" : ""}`,
        () => switchSpace(space.id),
      );
      const avatar = el(
        "span",
        "space-avatar",
        (space.name || "我").slice(0, 1),
      );
      const info = el("div");
      info.append(
        el("strong", "", space.name),
        el(
          "small",
          "",
          `${space.member_count || 1} 位成员 · ${roleNames[space.role]}`,
        ),
      );
      b.append(avatar, info);
      if (space.id === state.space?.id) b.append(svg("check", true));
      choices.append(b);
    });
    body.append(
      choices,
      actionsRow(button("创建空间", "button soft", showCreateSpace, "plus")),
    );
  });
}
async function showMembers(space) {
  const session = state.sessionEpoch;
  showModal(`${space.name} · 空间成员`, (body) => {
    loading(body);
  });
  const body = $("#modal-body");
  try {
    const result = state.demo
      ? {
          members: [
            {
              id: "demo-user",
              name: state.user.name,
              email: state.user.email,
              role: "owner",
            },
            ...(space.member_count > 1
              ? [
                  {
                    id: "demo-2",
                    name: "林小禾",
                    email: "lin@demo.example",
                    role: "editor",
                  },
                  {
                    id: "demo-3",
                    name: "陈一",
                    email: "chen@demo.example",
                    role: "viewer",
                  },
                ]
              : []),
          ],
        }
      : await api(`/api/spaces/${space.id}/members`);
    if (
      state.sessionEpoch !== session ||
      !$("#modal").open ||
      $("#modal-title").textContent !== `${space.name} · 空间成员`
    )
      return;
    body.replaceChildren();
    const list = el("div", "member-list");
    (result.members || result).forEach((member) => {
      const row = el("div", "member-row");
      const avatar = el(
        "span",
        "user-avatar",
        (member.name || member.email).slice(0, 1),
      );
      const info = el("div");
      info.append(
        el("strong", "", member.name || "空间成员"),
        el("small", "", member.email),
      );
      row.append(avatar, info, el("span", "tag", roleNames[member.role]));
      if (space.role === "owner" && member.role !== "owner") {
        row.append(
          iconButton("close", "移除成员", () =>
            confirmRemoveMember(space, member),
          ),
        );
      }
      list.append(row);
    });
    body.append(list);
    body.append(
      el(
        "p",
        "helper",
        state.demo
          ? "这里展示协作角色样例。演示无法向真实账户授予访问权限。"
          : "成员能查看这个空间内的工作与生活记录。私人内容请保留在个人空间或密码箱。所有者管理成员；编辑者可以记录与上传；只读成员可以查看和下载。",
      ),
    );
    if (space.role === "owner" && !state.demo) {
      const form = el("form", "invite-form");
      form.append(el("h3", "", "添加或调整成员"));
      const row = el("div", "form-row");
      const email = input("email", "对方已注册的邮箱");
      email.required = true;
      const role = select(
        [
          ["editor", "编辑者"],
          ["viewer", "只读成员"],
        ],
        "editor",
      );
      const add = el("button", "button primary", "保存权限");
      add.type = "submit";
      const roleLabel = labelField("访问权限", role);
      roleLabel.className = "role-label";
      row.append(labelField("成员邮箱", email), roleLabel, add);
      const error = el("p", "form-error");
      form.append(
        row,
        error,
        el(
          "p",
          "helper",
          "请对方先创建知序账号。同一邮箱再次添加会更新其访问权限。",
        ),
      );
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        add.disabled = true;
        try {
          await api(`/api/spaces/${space.id}/members`, "POST", {
            email: email.value.trim(),
            role: role.value,
          });
          const fresh = await api("/api/spaces");
          state.spaces = fresh.spaces;
          state.space = state.spaces.find((s) => s.id === state.space.id);
          await showMembers(state.spaces.find((s) => s.id === space.id));
          render();
          toast("成员权限已保存");
        } catch (err) {
          error.textContent = errorMessage(err);
          add.disabled = false;
        }
      });
      body.append(form);
    }
  } catch (err) {
    if (state.sessionEpoch === session)
      body.replaceChildren(el("p", "form-error", errorMessage(err)));
  }
}
function confirmRemoveMember(space, member) {
  showModal("移除这位成员？", (body) => {
    body.append(
      el(
        "p",
        "modal-body-note",
        `移除「${member.name || member.email}」后，对方将无法再访问这个空间，其个人密码箱也会从这个空间删除。`,
      ),
    );
    const error = el("p", "form-error");
    const remove = button("移除成员", "button danger", async () => {
      remove.disabled = true;
      try {
        await api(`/api/spaces/${space.id}/members/${member.id}`, "DELETE");
        space.member_count = Math.max(1, (space.member_count || 2) - 1);
        await showMembers(space);
        render();
        toast("成员已移除");
      } catch (err) {
        error.textContent = errorMessage(err);
        remove.disabled = false;
      }
    });
    body.append(
      error,
      actionsRow(
        button("取消", "button secondary", () => showMembers(space)),
        remove,
      ),
    );
  });
}
function showSearch() {
  showModal("在当前空间寻找", (body) => {
    const query = input("search", "搜索标题、正文或标签");
    query.setAttribute("aria-label", "搜索记录");
    const results = el("div", "search-results");
    const update = () => {
      results.replaceChildren();
      const term = query.value.trim().toLocaleLowerCase();
      const items = state.items
        .filter(
          (item) =>
            !term ||
            `${item.title}\n${item.body}\n${item.meta?.tag || ""}`
              .toLocaleLowerCase()
              .includes(term),
        )
        .slice(0, 30);
      if (!items.length) {
        emptyState(
          results,
          "search",
          "还没找到这条记录",
          "试试其他关键词。密码箱内容不会出现在搜索结果里。",
        );
        return;
      }
      items.forEach((item) => {
        const b = button("", "search-result", () =>
          item.kind === "media" ? showMedia(item) : showEditor(item.kind, item),
        );
        const info = el("span");
        info.append(
          el("strong", "", item.title),
          el(
            "small",
            "",
            `${item.area === "life" ? "生活" : "工作"} · ${kindNames[item.kind]}`,
          ),
        );
        b.append(
          svg(
            item.kind === "media"
              ? "folder"
              : item.kind === "task"
                ? "task"
                : "note",
          ),
          info,
          svg("arrow", true),
        );
        results.append(b);
      });
    };
    query.addEventListener("input", update);
    body.append(
      query,
      el("p", "helper", "搜索当前空间的工作与生活记录，私密密码箱不参与搜索。"),
      results,
    );
    update();
  });
}
function showProfile() {
  showModal("我的账户", (body) => {
    const info = el("div", "profile-details");
    const name = el("p");
    name.append(
      el("span", "", "名字"),
      document.createTextNode(state.user.name),
    );
    const email = el("p");
    email.append(
      el("span", "", "邮箱"),
      document.createTextNode(state.user.email),
    );
    info.append(name, email);
    body.append(
      info,
      el(
        "p",
        "helper",
        state.demo
          ? "当前为本地演示。创建账户后，记录将保存到你所部署的云端工作台。"
          : "你的登录身份与密码箱密码相互独立。退出后，当前解锁的密码箱会立即锁定。",
      ),
    );
    body.append(
      actionsRow(
        button(
          state.demo ? "退出演示" : "退出登录",
          "button secondary",
          async () => {
            try {
              if (!state.demo) await api("/api/auth/logout", "POST");
              for (const url of state.objectURLs) URL.revokeObjectURL(url);
              state.objectURLs.clear();
              state.demo = false;
              state.demoStore = {};
              state.demoVaults = {};
              closeModal();
              showAuth();
            } catch (err) {
              toast(errorMessage(err), true);
            }
          },
        ),
      ),
    );
  });
}

function bytesToBase64(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary);
}
function base64ToBytes(base64) {
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}
async function deriveKey(password, salt, iterations = 310000) {
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations, hash: "SHA-256" },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}
function lockVault(notify = true) {
  const hadKey = !!state.vault.key;
  state.vault.epoch++;
  state.vault.loading = false;
  state.vault.key = null;
  state.vault.entries = [];
  state.vault.activity = Date.now();
  if (state.view === "vault" && hadKey) {
    closeModal();
    render();
  }
  if (hadKey && notify) toast("密码箱已锁定");
}
async function loadVault() {
  if (!state.space || state.vault.loaded || state.vault.loading) return;
  state.vault.loading = true;
  const id = state.space.id,
    userId = state.user?.id,
    epoch = state.vault.epoch;
  try {
    const result = state.demo
      ? { vault: state.demoVaults[id] || null }
      : await api(`/api/spaces/${id}/vault`);
    if (
      state.space?.id !== id ||
      state.user?.id !== userId ||
      state.vault.epoch !== epoch
    )
      return;
    state.vault.payload = result.vault;
    state.vault.loaded = true;
    state.vault.loading = false;
    if (state.view === "vault") render();
  } catch (err) {
    if (
      state.space?.id === id &&
      state.user?.id === userId &&
      state.vault.epoch === epoch
    ) {
      state.vault.loading = false;
      const content = $("#view-content");
      const panel = el("div", "error-panel");
      panel.append(
        el("p", "", errorMessage(err)),
        button("重新加载", "button secondary", loadVault),
      );
      content.replaceChildren(panel);
    }
  }
}
async function persistVault(entries) {
  const vault = state.vault;
  if (!vault.key) throw new Error("密码箱已锁定，请重新解锁。");
  const id = state.space.id,
    epoch = vault.epoch,
    key = vault.key,
    iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    new TextEncoder().encode(JSON.stringify(entries)),
  );
  if (epoch !== vault.epoch) throw new Error("密码箱已锁定，本次修改未保存。");
  const payload = {
    version: 1,
    salt: vault.payload.salt,
    iv: bytesToBase64(iv),
    ciphertext: bytesToBase64(encrypted),
    kdfIterations: 310000,
  };
  if (state.demo) state.demoVaults[id] = payload;
  else await api(`/api/spaces/${id}/vault`, "PUT", payload);
  if (state.space?.id !== id || epoch !== vault.epoch) return;
  vault.payload = payload;
  vault.entries = entries;
  vault.activity = Date.now();
  render();
}
function renderVault(content) {
  if (!state.vault.loaded) {
    loading(content);
    loadVault();
    return;
  }
  if (state.testMode !== false || state.storagePersistence !== "persistent") {
    const note = el(
      "p",
      "vault-environment-note",
      state.testMode
        ? "请仅使用测试内容，密码箱密文也可能清空。"
        : "存储状态暂未确认，请仅使用测试内容。",
    );
    note.id = "vault-environment-note";
    content.append(note);
  }
  if (!globalThis.crypto?.subtle) {
    const panel = el("section", "card vault-locked");
    panel.append(
      el("h2", "", "请通过 HTTPS 打开密码箱"),
      el(
        "p",
        "",
        "浏览器加密需要安全连接。使用已配置 HTTPS 的域名访问，或在本机通过 localhost 预览。",
      ),
    );
    content.append(panel);
    return;
  }
  if (!state.vault.key) {
    const existing = !!state.vault.payload;
    const panel = el("section", "card vault-locked");
    const symbol = el("div", "vault-symbol");
    symbol.append(svg("lock"));
    panel.append(
      symbol,
      el("h2", "", existing ? "密码箱已经锁好" : "给私密内容，一把自己的钥匙"),
      el(
        "p",
        "",
        existing
          ? "输入密码箱密码，在当前浏览器中解锁。切换空间、离开页面或闲置 5 分钟后会自动锁定。"
          : "设置独立的密码箱密码。内容先在你的浏览器中加密，再保存到云端，密码不会发送到服务器。",
      ),
    );
    if (!existing && !writable()) {
      panel.append(
        el(
          "p",
          "danger-note",
          "当前空间为只读，暂时无法创建密码箱。请空间所有者调整权限。",
        ),
      );
      content.append(panel);
      return;
    }
    const form = el("form", "vault-form");
    const password = input(
      "password",
      existing ? "输入密码箱密码" : "至少 10 位，建议使用长密码",
    );
    password.required = true;
    password.minLength = existing ? 1 : 10;
    password.maxLength = 200;
    password.autocomplete = existing ? "current-password" : "new-password";
    form.append(labelField(existing ? "密码箱密码" : "设置独立密码", password));
    let confirm;
    if (!existing) {
      confirm = input("password", "再次输入密码");
      confirm.required = true;
      confirm.autocomplete = "new-password";
      form.append(labelField("确认密码", confirm));
    }
    const error = el("p", "form-error");
    const save = el(
      "button",
      "button primary full",
      existing ? "解锁密码箱" : "创建密码箱",
    );
    save.type = "submit";
    form.append(error, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (!existing && password.value !== confirm.value) {
        error.textContent = "两次输入的密码不同，请重新确认。";
        return;
      }
      const secret = password.value;
      password.value = "";
      if (confirm) confirm.value = "";
      const id = state.space.id,
        epoch = state.vault.epoch;
      save.disabled = true;
      save.textContent = "正在浏览器中解密…";
      try {
        const salt = existing
          ? base64ToBytes(state.vault.payload.salt)
          : crypto.getRandomValues(new Uint8Array(16));
        const key = await deriveKey(
          secret,
          salt,
          existing ? state.vault.payload.kdfIterations : 310000,
        );
        let entries = [];
        if (existing) {
          const decoded = await crypto.subtle.decrypt(
            { name: "AES-GCM", iv: base64ToBytes(state.vault.payload.iv) },
            key,
            base64ToBytes(state.vault.payload.ciphertext),
          );
          entries = JSON.parse(new TextDecoder().decode(decoded));
          if (!Array.isArray(entries)) throw new Error("密码箱内容格式无效。");
        }
        if (state.space?.id !== id || state.vault.epoch !== epoch) return;
        state.vault.key = key;
        state.vault.entries = entries;
        state.vault.activity = Date.now();
        if (!existing) {
          state.vault.payload = { salt: bytesToBase64(salt) };
          try {
            await persistVault([]);
          } catch (err) {
            state.vault.key = null;
            state.vault.entries = [];
            state.vault.payload = null;
            throw err;
          }
        }
        render();
        toast(existing ? "密码箱已解锁" : "密码箱已创建");
      } catch (err) {
        if (state.space?.id !== id || state.vault.epoch !== epoch) return;
        error.textContent =
          err.name === "OperationError"
            ? "密码不正确，或密文已损坏。请检查密码后重试。"
            : errorMessage(err);
        save.disabled = false;
        save.textContent = existing ? "解锁密码箱" : "创建密码箱";
        password.focus();
      }
    });
    panel.append(form);
    const security = el("div", "vault-security");
    security.append(
      svg("shield"),
      el("span", "", "浏览器内加密 · AES-256-GCM"),
    );
    panel.append(security);
    if (!existing)
      panel.append(
        el(
          "div",
          "vault-note",
          "请妥善保管密码。密码箱密码无法找回，也无法通过登录密码重置。",
        ),
      );
    content.append(panel);
    return;
  }
  const bar = el("div", "view-toolbar");
  bar.append(
    el(
      "span",
      "item-count",
      `${state.vault.entries.length} 条私密记录 · 当前已解锁`,
    ),
  );
  const actions = el("div", "vault-toolbar");
  actions.append(
    el("span", "helper", "闲置 5 分钟自动锁定"),
    button("立即锁定", "button secondary", () => lockVault(), "lock"),
  );
  if (writable())
    actions.append(
      button("添加私密记录", "button primary", () => showVaultEditor(), "plus"),
    );
  bar.append(actions);
  content.append(bar);
  const grid = el("div", "notes-grid");
  if (state.vault.entries.length)
    state.vault.entries.forEach((entry) => {
      const b = button("", "vault-entry", () => showVaultEditor(entry));
      const h = el("h3");
      h.append(svg("lock"), document.createTextNode(entry.title));
      b.append(
        h,
        el("p", "", "点击打开这条私密记录"),
        el("small", "", relativeDate(entry.updatedAt)),
      );
      grid.append(b);
    });
  else
    emptyState(
      grid,
      "lock",
      "你的秘密，有地方安放了",
      "添加私密笔记或账号信息。所有内容都会加密保存。",
      writable() ? "添加私密记录" : null,
      () => showVaultEditor(),
    );
  content.append(grid);
}
function showVaultEditor(entry) {
  if (!state.vault.key) return;
  const epoch = state.vault.epoch;
  showModal(entry ? "私密记录" : "添加私密记录", (body) => {
    body.append(el("div", "secret-label", "只在当前解锁期间可见"));
    const form = el("form", "editor-main");
    const title = input("text", "记录名称", entry?.title || "");
    title.required = true;
    title.maxLength = 200;
    const text = el("textarea");
    text.value = entry?.body || "";
    text.placeholder = "账号、密码或只属于你的内容…";
    text.maxLength = 100000;
    form.append(labelField("名称", title), labelField("私密内容", text));
    const error = el("p", "form-error");
    const save = el("button", "button primary", "加密保存");
    save.type = "submit";
    const buttons = [];
    if (entry && writable())
      buttons.push(
        button(
          "删除",
          "button danger",
          () => {
            const remove = () => {
              if (epoch !== state.vault.epoch) return;
              showModal("删除私密记录？", (container) => {
                container.append(
                  el("p", "modal-body-note", "删除后无法恢复。"),
                );
                const removeError = el("p", "form-error");
                const confirm = button(
                  "确认删除",
                  "button danger",
                  async () => {
                    confirm.disabled = true;
                    try {
                      await persistVault(
                        state.vault.entries.filter((e) => e.id !== entry.id),
                      );
                      closeModal();
                      toast("私密记录已删除");
                    } catch (err) {
                      removeError.textContent = errorMessage(err);
                      confirm.disabled = false;
                    }
                  },
                );
                container.append(
                  removeError,
                  actionsRow(
                    button("取消", "button secondary", closeModal),
                    confirm,
                  ),
                );
              });
            };
            remove();
          },
          "trash",
        ),
      );
    buttons.push(button("关闭", "button secondary", closeModal));
    if (writable()) buttons.push(save);
    else
      $$("input,textarea", form).forEach(
        (control) => (control.readOnly = true),
      );
    form.append(error, actionsRow(...buttons));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!writable()) return;
      save.disabled = true;
      try {
        if (epoch !== state.vault.epoch) throw new Error("密码箱已锁定");
        const value = {
          id: entry?.id || crypto.randomUUID(),
          title: title.value.trim(),
          body: text.value,
          updatedAt: new Date().toISOString(),
        };
        const next = state.vault.entries.map((e) =>
          e.id === value.id ? value : e,
        );
        if (!entry) next.unshift(value);
        await persistVault(next);
        title.value = "";
        text.value = "";
        closeModal();
        toast(state.demo ? "密文已保存到本地演示" : "私密记录已加密保存");
      } catch (err) {
        error.textContent = errorMessage(err);
        save.disabled = false;
      }
    });
    body.append(form);
  });
}

function showVoice() {
  if (!ensureWrite()) return;
  const sid = state.space.id,
    area = state.area;
  showModal("想到，就说下来", (body) => {
    const panel = el("div", "voice-panel");
    const orb = el("div", "voice-orb");
    orb.append(svg("mic"));
    const timer = el("div", "voice-time", "00:00");
    const status = el("p", "voice-status", "点击开始，录下此刻的想法");
    const controls = el("div", "voice-controls");
    const start = button("开始录音", "button primary", startRecording, "mic");
    const stop = button("停止录音", "button secondary", stopRecording);
    stop.disabled = true;
    controls.append(start, stop);
    const audio = el("audio");
    audio.controls = true;
    audio.hidden = true;
    const explanation = el(
      "p",
      "voice-explanation",
      "录音需要麦克风权限与 HTTPS 连接。音频只在点击保存时上传到当前空间，每份文件最大 25 MB。",
    );
    const transcriptWrap = el("div", "voice-transcript");
    const transcript = el("textarea");
    transcript.placeholder =
      "浏览器支持时，可在这里看到语音转写；也可以手动补充。";
    transcript.setAttribute("aria-label", "语音转写或补充记录");
    transcriptWrap.append(labelField("转写与补充（可选）", transcript));
    const error = el("p", "form-error");
    const save = button("保存录音", "button primary", async () => {
      if (!blob) return;
      if (state.space?.id !== sid) {
        error.textContent = "空间已切换，请在目标空间重新录音。";
        return;
      }
      save.disabled = true;
      try {
        const ext = blob.type.includes("ogg")
          ? "ogg"
          : blob.type.includes("mp4")
            ? "m4a"
            : "webm";
        const file = new File(
          [blob],
          `语音记录-${new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date()).replaceAll(":", "-").replace(" ", "-")}.${ext}`,
          { type: blob.type },
        );
        const uploaded = await uploadFiles([file]);
        if (!uploaded) throw new Error("录音没有上传成功，请重试。");
        if (transcript.value.trim())
          await saveItem({
            kind: "note",
            area,
            title: transcript.value.trim().split("\n")[0].slice(0, 200),
            body: transcript.value.trim(),
            status: "active",
            meta: { tag: "语音记录" },
          });
        closeModal();
      } catch (err) {
        error.textContent = errorMessage(err);
        save.disabled = false;
      }
    });
    save.disabled = true;
    const noteSave = button("保存文字", "button secondary", async () => {
      if (!transcript.value.trim()) {
        transcript.focus();
        return;
      }
      if (state.space?.id !== sid) {
        error.textContent = "空间已切换，请重新打开录入。";
        return;
      }
      noteSave.disabled = true;
      try {
        await saveItem({
          kind: "note",
          area,
          title: transcript.value.trim().split("\n")[0].slice(0, 200),
          body: transcript.value.trim(),
          status: "active",
          meta: { tag: "语音记录" },
        });
        closeModal();
        toast("文字记录已保存");
      } catch (err) {
        error.textContent = errorMessage(err);
        noteSave.disabled = false;
      }
    });
    panel.append(
      orb,
      timer,
      status,
      controls,
      audio,
      transcriptWrap,
      explanation,
      error,
      actionsRow(noteSave, save),
    );
    body.append(panel);
    let recorder = null,
      stream = null,
      blob = null,
      chunks = [],
      tick = null,
      startAt = 0,
      recognition = null,
      previewURL = null,
      disposed = false;
    const Recognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      explanation.textContent +=
        " 当前浏览器不支持语音转写，可以正常录音保存，也可以手动补充文字。";
    } else {
      explanation.textContent +=
        " 可选语音转写可能调用浏览器提供商的服务；网络不通时仍可正常保存录音。";
      const check = el("input");
      check.type = "checkbox";
      check.checked = false;
      check.style.width = "auto";
      const choice = el("label");
      choice.style.cssText =
        "display:flex;gap:8px;align-items:center;margin-top:14px;font-size:10px;color:#99a88e";
      choice.append(
        check,
        document.createTextNode("启用浏览器语音转写（可选）"),
      );
      transcriptWrap.before(choice);
      transcriptWrap.dataset.enabled = "false";
      check.addEventListener("change", () => {
        transcriptWrap.dataset.enabled = String(check.checked);
      });
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      start.disabled = true;
      status.textContent = "当前浏览器不支持录音，请使用近期版本的浏览器。";
      explanation.textContent =
        "当前设备无法使用麦克风录音。可以在上方输入文字并保存，或通过「上传文件」保存已有音频。";
    }
    async function startRecording() {
      error.textContent = "";
      start.disabled = true;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (disposed) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        chunks = [];
        blob = null;
        save.disabled = true;
        audio.pause();
        audio.hidden = true;
        audio.removeAttribute("src");
        if (previewURL) {
          URL.revokeObjectURL(previewURL);
          previewURL = null;
        }
        timer.textContent = "00:00";
        const mime = [
          "audio/webm;codecs=opus",
          "audio/ogg;codecs=opus",
          "audio/mp4",
        ].find((type) => MediaRecorder.isTypeSupported(type));
        recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
        recorder.addEventListener("dataavailable", (event) => {
          if (event.data.size) chunks.push(event.data);
        });
        recorder.addEventListener("stop", () => {
          stream?.getTracks().forEach((t) => t.stop());
          clearInterval(tick);
          recognition?.stop();
          orb.classList.remove("recording");
          stop.disabled = true;
          start.disabled = false;
          start.textContent = "重新录音";
          if (disposed) return;
          blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
          if (previewURL) URL.revokeObjectURL(previewURL);
          previewURL = URL.createObjectURL(blob);
          audio.src = previewURL;
          audio.hidden = false;
          save.disabled = !blob.size || blob.size > 25 * 1024 * 1024;
          status.textContent = save.disabled
            ? "录音超过 25 MB，请缩短后重试"
            : "录音已就绪，可以预听后保存。";
        });
        recorder.start(1000);
        startAt = Date.now();
        orb.classList.add("recording");
        status.textContent = "正在录音…";
        stop.disabled = false;
        tick = window.setInterval(() => {
          const sec = Math.floor((Date.now() - startAt) / 1000);
          timer.textContent = `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
          if (chunks.reduce((sum, b) => sum + b.size, 0) > 24 * 1024 * 1024)
            stopRecording();
        }, 500);
        if (Recognition && transcriptWrap.dataset.enabled === "true") {
          recognition = new Recognition();
          recognition.lang = "zh-CN";
          recognition.continuous = true;
          recognition.interimResults = false;
          recognition.addEventListener("result", (event) => {
            for (let i = event.resultIndex; i < event.results.length; i++)
              if (event.results[i].isFinal)
                transcript.value +=
                  (transcript.value ? "\n" : "") +
                  event.results[i][0].transcript;
          });
          recognition.addEventListener("error", () => {
            explanation.textContent =
              "语音转写暂时不可用；录音仍在继续，你可以保存音频并手动补充文字。";
          });
          try {
            recognition.start();
          } catch {
            explanation.textContent = "语音转写未能启动；录音仍可保存。";
          }
        }
      } catch (err) {
        start.disabled = false;
        status.textContent = "麦克风没有开启";
        error.textContent =
          err.name === "NotAllowedError"
            ? "请在浏览器中允许麦克风权限，再试一次。"
            : err.name === "NotFoundError"
              ? "没有找到麦克风，可以上传已有录音或输入文字。"
              : errorMessage(err);
        stream?.getTracks().forEach((t) => t.stop());
      }
    }
    function stopRecording() {
      if (recorder && recorder.state !== "inactive") recorder.stop();
      recognition?.stop();
    }
    modalCleanup = () => {
      disposed = true;
      stopRecording();
      stream?.getTracks().forEach((t) => t.stop());
      clearInterval(tick);
      recognition?.abort();
      audio.pause();
      audio.removeAttribute("src");
      if (previewURL) URL.revokeObjectURL(previewURL);
      transcript.value = "";
      blob = null;
      chunks = [];
    };
  });
}

function updateDate() {
  const now = new Date();
  $("#today-date").textContent = new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    timeZone: "Asia/Shanghai",
  }).format(now);
  $("#today-weekday").textContent =
    new Intl.DateTimeFormat("zh-CN", {
      weekday: "long",
      timeZone: "Asia/Shanghai",
    }).format(now) + " · 一天一个小进展";
}
$("#auth-toggle").addEventListener("click", () => {
  if (state.registrationMode === null && !state.register) return;
  state.register = !state.register;
  $("#name-label").hidden = !state.register;
  $("#auth-name").required = state.register;
  $("#auth-title").textContent = state.register
    ? "从自己的空间开始"
    : "欢迎回来";
  $("#auth-subtitle").textContent = state.register
    ? "创建账户，把重要的工作与生活放在一起。"
    : "进入你的空间，继续今天的好进展。";
  $("#auth-submit").replaceChildren(
    document.createTextNode(state.register ? "创建自己的空间" : "进入工作台"),
    svg("arrow"),
  );
  $("#auth-switch-text").textContent = state.register
    ? "已经有自己的空间？"
    : "还没有自己的空间？";
  $("#auth-toggle").textContent = state.register ? "登录账户" : "创建账户";
  $("#auth-password").autocomplete = state.register
    ? "new-password"
    : "current-password";
  $("#auth-error").textContent = "";
  updateRegistrationControls();
});
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submission = ++state.authRequest;
  const submit = $("#auth-submit");
  submit.disabled = true;
  $("#auth-error").textContent = "";
  try {
    const data = {
      email: $("#auth-email").value.trim(),
      password: $("#auth-password").value,
    };
    if (state.register) {
      if (state.registrationMode === null)
        throw new Error("暂时无法确认注册设置，请刷新页面后重试。");
      data.name = $("#auth-name").value.trim();
      if (state.registrationMode === "invite")
        data.invitationCode = $("#auth-invitation").value.trim();
    }
    const result = await api(
      `/api/auth/${state.register ? "register" : "login"}`,
      "POST",
      data,
    );
    if (submission !== state.authRequest) return;
    state.demo = false;
    state.user = result.user;
    bedtimeBoundary("login");
    $("#auth-password").value = "";
    $("#auth-invitation").value = "";
    state.view = "overview";
    state.area = "work";
    await enterApp();
  } catch (err) {
    if (submission === state.authRequest)
      $("#auth-error").textContent = errorMessage(err);
  } finally {
    if (submission === state.authRequest) submit.disabled = false;
  }
});
$("#demo-button").addEventListener("click", enterDemo);
$("#demo-exit").addEventListener("click", () => {
  state.demo = false;
  state.demoStore = {};
  state.demoVaults = {};
  closeModal();
  for (const url of state.objectURLs) URL.revokeObjectURL(url);
  state.objectURLs.clear();
  showAuth();
  if (!state.register) $("#auth-toggle").click();
});
$$("[data-view]").forEach((b) =>
  b.addEventListener("click", () => navigate(b.dataset.view)),
);
$$("[data-area]").forEach((b) =>
  b.addEventListener("click", () => setArea(b.dataset.area)),
);
$$("[data-go]").forEach((b) =>
  b.addEventListener("click", () => navigate(b.dataset.go)),
);
$$("[data-mobile-view]").forEach((b) =>
  b.addEventListener("click", () => navigate(b.dataset.mobileView)),
);
$(".sidebar .brand").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("overview");
});
$("#new-button").addEventListener("click", showNew);
$("#mobile-new").addEventListener("click", showNew);
$("#space-switcher").addEventListener("click", showSpaceSwitcher);
$("#search-button").addEventListener("click", showSearch);
$("#profile-button").addEventListener("click", showProfile);
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeModal();
});
$("#modal").addEventListener("click", (event) => {
  if (event.target === $("#modal")) {
    const rect = $("#modal").getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      closeModal();
  }
});
for (const id of ["#mobile-menu", "#mobile-more"])
  $(id).addEventListener("click", () => {
    if ($("#sidebar").classList.contains("open")) closeDrawer();
    else openDrawer($(id));
  });
$("#drawer-close").addEventListener("click", () => closeDrawer());
$("#drawer-backdrop").addEventListener("click", () => closeDrawer());
mobileLayout.addEventListener("change", () => {
  closeDrawer(false);
  updateVisualViewport();
});
$("#file-input").addEventListener("change", (event) =>
  uploadFiles([...event.target.files]),
);
document.addEventListener("keydown", (event) => {
  if (mobileLayout.matches && $("#sidebar").classList.contains("open")) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key === "Tab") {
      const controls = $$("button:not(:disabled), a[href], [tabindex='0']", $("#sidebar"))
        .filter((node) => node.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (!$("#sidebar").contains(document.activeElement) ||
        (event.shiftKey && document.activeElement === first)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
      return;
    }
  }
  if (
    (event.metaKey || event.ctrlKey) &&
    event.key.toLowerCase() === "k" &&
    state.user
  ) {
    event.preventDefault();
    showSearch();
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "1" && state.user) {
    event.preventDefault();
    navigate("overview");
  }
});
let dragDepth = 0;
document.addEventListener("dragenter", (event) => {
  if (event.dataTransfer?.types.includes("Files") && state.user && writable()) {
    event.preventDefault();
    dragDepth++;
    $("#main-content").classList.add("drop-active");
  }
});
document.addEventListener("dragover", (event) => {
  if (event.dataTransfer?.types.includes("Files") && state.user) {
    event.preventDefault();
    event.dataTransfer.dropEffect = writable() ? "copy" : "none";
  }
});
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) {
    dragDepth = 0;
    $("#main-content").classList.remove("drop-active");
  }
});
document.addEventListener("drop", (event) => {
  if (event.dataTransfer?.files.length && state.user) {
    event.preventDefault();
    dragDepth = 0;
    $("#main-content").classList.remove("drop-active");
    uploadFiles([...event.dataTransfer.files]);
  }
});
for (const eventName of ["pointerdown", "keydown", "touchstart"])
  document.addEventListener(
    eventName,
    () => {
      if (state.vault.key) state.vault.activity = Date.now();
    },
    { passive: true },
  );
document.addEventListener("visibilitychange", () => {
  if (document.hidden) lockVault(false);
  else if (state.user && state.view === "vault" && !state.vault.loaded)
    loadVault();
});
window.addEventListener("pagehide", () => lockVault(false));
window.setInterval(() => {
  if (state.vault.key && Date.now() - state.vault.activity >= 5 * 60 * 1000)
    lockVault();
}, 15000);
window.setInterval(updateDate, 60000);
window.addEventListener("resize", updateVisualViewport);
window.visualViewport?.addEventListener("resize", updateVisualViewport);
window.visualViewport?.addEventListener("scroll", updateVisualViewport);
document.addEventListener("focusin", updateVisualViewport);
document.addEventListener("focusout", () => window.setTimeout(updateVisualViewport, 0));
$("#modal").addEventListener("focusin", (event) => {
  if (mobileLayout.matches && event.target.matches("input, textarea, select"))
    window.setTimeout(() => {
      if ($("#modal").open && event.target.isConnected)
        event.target.scrollIntoView({ block: "nearest", behavior: "instant" });
    }, 180);
});
window.addEventListener("resize", measureEnvironmentBanner);
if (globalThis.ResizeObserver)
  new ResizeObserver(measureEnvironmentBanner).observe(
    $("#test-environment-banner"),
  );
async function init() {
  syncDrawerLayout();
  updateVisualViewport();
  updateDate();
  await loadConfiguration();
  try {
    const result = await api("/api/me");
    state.user = result.user;
    state.demo = false;
    await enterApp();
  } catch (err) {
    showAuth();
    if (err.status !== 401) $("#auth-error").textContent = errorMessage(err);
  }
}
init();
