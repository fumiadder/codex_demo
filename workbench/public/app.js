"use strict";

const $ = (selector, context = document) => context.querySelector(selector);
const $$ = (selector, context = document) => [
  ...context.querySelectorAll(selector),
];
function requestedArea() {
  return new URLSearchParams(location.search).get("area") === "life" ? "life" : "work";
}
const state = {
  user: null,
  spaces: [],
  space: null,
  items: [],
  area: requestedArea(),
  view: "overview",
  demo: false,
  testMode: false,
  storagePersistence: null,
  registrationMode: null,
  taskFilter: "all",
  mediaFilter: "all",
  tagFilter: null,
  spaceTags: [],
  tagsLoadedSpace: null,
  tagsLoading: false,
  search: { query: "", results: [], loading: false, request: 0, queried: "", error: "" },
  calendar: { month: "", filter: null, mode: "month" },
  notifications: { items: [], unread: 0, loaded: false, loading: false },
  shortcuts: null,
  templates: { list: [], loaded: false, loading: false },
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
  demoDaily: {},
  objectURLs: new Set(),
  daily: {
    config: null,
    entries: [],
    digests: {},
    month: "",
    loading: false,
    loadedSpace: null,
    composer: [],
  },
};
// 单一事实源：/api/config 未下发上传上限时，与服务端 MAX_UPLOAD_BYTES 保持一致
const MAX_UPLOAD_MB = 25;
// 与注册表单 minlength 及服务端账号校验保持一致的密码最小长度
const PASSWORD_MIN_LENGTH = 10;
// 注销账号时必须逐字键入的确认短语
const ACCOUNT_DELETE_PHRASE = "删除我的账号";
// 进行中的导出请求，防止重复点击造成重入
const exportPending = new Set();
const names = {
  overview: "今天，也从容一点。",
  daily: "拍下来，就是今天的记录。",
  search: "找一找，所有空间里的记录。",
  tasks: "把重要的事，向前推进。",
  calendar: "一张月历，安顿每个到期日。",
  notes: "让灵感有个落脚的地方。",
  media: "所有资料，随手可得。",
  vault: "只属于你的私密空间。",
  spaces: "各有空间，也能一起。",
  modules: "从需要出发，慢慢长大。",
};
const descriptions = {
  overview: "让每个想法有去处，让每一步都算数。",
  daily: "拍照或选图快速记下今天，AI 帮你识别、总结并提出待办。",
  search: "跨你加入的所有空间搜索待办、笔记与今日记录，密码箱不参与搜索。",
  tasks: "一步一步，把想做的变成已完成。",
  calendar: "按日期查看计划与重复任务，点日期即可新建或筛选。",
  notes: "值得留下的念头，不必等到明天。",
  media: "图片、视频、音频与文档，收在同一个地方。",
  vault: "你的个人密码箱独立保存，空间成员无法看到其中内容。",
  spaces: "独立空间隔离数据，用明确的权限一起协作。",
  modules: "基础功能已就绪，AI 能力将在接入模型服务后启用。",
};
// The life area gets its own menu labels and page copy so the two areas read
// as distinct spaces rather than one identical list with an extra entry.
const lifeNames = {
  overview: "给生活，留一点空白。",
  daily: "把今天的光，存进相册。",
  search: "找一找，生活里记下的每件事。",
  tasks: "慢慢来，照顾好每一件小事。",
  calendar: "日子有节奏，生活不慌张。",
  notes: "心情和念头，都值得被记下。",
  media: "收藏声音与画面，也收藏回忆。",
};
const lifeDescriptions = {
  overview: "记下喜欢的瞬间，照顾那些重要的小事。",
  daily: "拍照或选图留下此刻，AI 帮你识别、总结并提出待办。",
  search: "跨你加入的所有空间搜索生活记录，私密密码箱不参与搜索。",
  tasks: "不慌不忙，把生活里的小事一一安顿。",
  calendar: "按日期查看生活安排与重复提醒，点日期即可新建或筛选。",
  notes: "随手写下心情、灵感与想说的话。",
  media: "照片、声音和文件，替你收好每一段回忆。",
};
function areaName(view) {
  return state.area === "life" && lifeNames[view]
    ? lifeNames[view]
    : names[view];
}
function areaDescription(view) {
  return state.area === "life" && lifeDescriptions[view]
    ? lifeDescriptions[view]
    : descriptions[view];
}
function applyAreaNav() {
  const isLife = state.area === "life";
  const label = $("#main-nav-label");
  if (label) label.textContent = isLife ? "我的生活" : "我的工作台";
  $$(".nav-item[data-areas]").forEach((button) => {
    const allowed = (button.dataset.areas || "").split(/\s+/);
    button.hidden = !allowed.includes(state.area);
  });
  $$("[data-label-work]").forEach((node) => {
    node.textContent = isLife
      ? node.dataset.labelLife
      : node.dataset.labelWork;
  });
}
const roleNames = { owner: "所有者", editor: "编辑者", viewer: "只读成员" };
const kindNames = { task: "待办", note: "笔记", media: "媒体" };
const fieldKeys = ["kind", "area", "title", "body", "status", "meta"];
// Only return to the bedtime module after sign-in. Rebuild the URL so a
// copied link cannot turn this auth page into an external redirect.
function allowedBedtimeReturn(raw) {
  if (typeof raw !== "string" || !raw.startsWith("/") || raw.includes("\\")) return "";
  try {
    const target = new URL(raw, location.origin);
    if (target.origin !== location.origin || target.pathname !== "/bedtime.html") return "";
    const query = new URLSearchParams();
    const space = target.searchParams.get("space");
    if (space && /^[A-Za-z0-9_-]{1,128}$/.test(space)) query.set("space", space);
    if (target.searchParams.get("panel") === "voices") query.set("panel", "voices");
    const search = query.toString();
    return "/bedtime.html" + (search ? `?${search}` : "");
  } catch { return ""; }
}
let authReturnPath = allowedBedtimeReturn(new URLSearchParams(location.search).get("next"));
function updateAuthReturnNote() {
  const note = $("#auth-return-note");
  note.hidden = !authReturnPath;
  note.textContent = authReturnPath.includes("panel=voices")
    ? "登录后继续上传你的声音，音色档案保存在你自己的空间。"
    : "登录后继续打开生活专区的晚安故事。";
}
function finishAuthReturn() {
  if (state.demo || !state.user || !authReturnPath) return false;
  const target = allowedBedtimeReturn(authReturnPath);
  authReturnPath = "";
  if (!target) return false;
  location.replace(target);
  return true;
}
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
// ---------------------------------------------------------------------------
// 手机端触感反馈：点击按钮时轻微震动一次。
// 只在触屏设备上生效，尊重系统「减少动态效果」设置；不支持振动 API 的浏览器
// 静默跳过，不影响任何交互。使用事件委托，避免给每个按钮单独绑定。
// ---------------------------------------------------------------------------
const hapticPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
const HAPTIC_DURATION = 12; // 毫秒：足够感知、又不产生明显嗡鸣
let lastHapticAt = 0;

function hapticsAvailable() {
  return typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
}
function shouldHaptic(target) {
  if (!hapticsAvailable()) return false;
  if (hapticPreference.matches) return false;
  // 触屏优先：桌面鼠标点击不震动；平板/手机按粗指针判定。
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  if (!coarse && !mobileLayout.matches) return false;
  return Boolean(target);
}
function hapticFeedback(target) {
  if (!shouldHaptic(target)) return;
  // 连点防抖：40ms 内不重复触发，避免列表快速点击时连续嗡鸣。
  const now = Date.now();
  if (now - lastHapticAt < 40) return;
  lastHapticAt = now;
  try {
    navigator.vibrate(HAPTIC_DURATION);
  } catch {
    /* 振动失败不影响交互 */
  }
}
function bindHaptics() {
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target instanceof Element
        ? event.target.closest(
            'button, [role="button"], .nav-item, .stat-card, .search-result, .calendar-cell, .agenda-item, .template-option',
          )
        : null;
      if (!target) return;
      // 禁用态与只读控件不反馈。
      if (target.disabled || target.getAttribute("aria-disabled") === "true") return;
      hapticFeedback(target);
    },
    { passive: true },
  );
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
  if (error?.message) return error.message;
  if (error?.status === 429) return "操作太频繁，请稍后再试。";
  if (error?.status === 503) return "服务暂未连接，请稍后再试。";
  return "暂时无法完成，请重试。";
}
// 统一映射限流与暂不可用响应：429 优先采用 Retry-After（秒）提示；
// 503 不覆盖后端给出的具体中文 message，缺省时才用通用文案。
function apiStatusMessage(status, result, response) {
  const detail = result?.error || result?.message || "";
  if (status === 429) {
    const retryAfter = Number.parseInt(
      response.headers.get("Retry-After") || "",
      10,
    );
    if (Number.isFinite(retryAfter) && retryAfter > 0)
      return `操作太频繁，请 ${retryAfter} 秒后再试`;
    return detail || "操作太频繁，请稍后再试。";
  }
  if (status === 503) return detail || "服务暂未连接，请稍后再试。";
  return detail || "请求未完成";
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
    const err = new Error(
      apiStatusMessage(response.status, result, response),
    );
    err.status = response.status;
    throw err;
  }
  return result;
}
// ---------------------------------------------------------------------------
// 重复任务 / 子任务 / 标签 / Markdown / 月历：前端纯函数区
// 日期语义与后端 recur.py 对齐，全部使用原生 Date、不做任何 IO，真实模式与
// 演示模式共用；真实模式的滚动以服务端响应为准，这里仅用于演示模式兜底。
// ---------------------------------------------------------------------------
const RECUR_FREQ_OPTIONS = [
  ["none", "不重复"],
  ["daily", "每天"],
  ["weekly", "每周"],
  ["monthly", "每月"],
  ["yearly", "每年"],
];
const RECUR_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const RECUR_WEEKDAY_CN = {
  MO: "周一", TU: "周二", WE: "周三", TH: "周四",
  FR: "周五", SA: "周六", SU: "周日",
};
const TAG_LIMIT = 8;
const TAG_MAX_LENGTH = 20;
const SUBTASK_LIMIT = 20;

function isoDateString(date) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}
function parseISODate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}
function addCalendarMonths(date, months) {
  const target = new Date(date.getTime());
  const day = target.getDate();
  target.setDate(1);
  target.setMonth(target.getMonth() + months);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  target.setDate(Math.min(day, lastDay));
  return target;
}
function addCalendarYears(date, years) {
  const target = new Date(date.getTime());
  const day = target.getDate();
  target.setDate(1);
  target.setFullYear(target.getFullYear() + years);
  const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
  target.setDate(Math.min(day, lastDay));
  return target;
}
// JS getDay() 以周日为 0，转换成 MO=0..SU=6 的 weekday 下标
function weekdayIndex(date) {
  return (date.getDay() + 6) % 7;
}
// 与 recur.py next_due 同语义：以 fromDate 为当前到期日推算下一次，耗尽返回 null
function nextDueDate(rec, fromDate) {
  if (!rec || !rec.freq) return null;
  const start = parseISODate(fromDate) || new Date();
  const interval = Number.isInteger(rec.interval) && rec.interval >= 1 ? rec.interval : 1;
  let candidate;
  if (rec.freq === "daily") {
    candidate = new Date(start.getTime());
    candidate.setDate(candidate.getDate() + interval);
  } else if (rec.freq === "weekly") {
    if (Array.isArray(rec.byday) && rec.byday.length) {
      const targets = new Set(
        rec.byday.map((day) => RECUR_WEEKDAYS.indexOf(day)).filter((i) => i >= 0),
      );
      candidate = new Date(start.getTime());
      candidate.setDate(candidate.getDate() + 1);
      let guard = 0;
      while (!targets.has(weekdayIndex(candidate)) && guard++ < 8)
        candidate.setDate(candidate.getDate() + 1);
    } else {
      candidate = new Date(start.getTime());
      candidate.setDate(candidate.getDate() + interval * 7);
    }
  } else if (rec.freq === "monthly") {
    candidate = addCalendarMonths(start, interval);
  } else if (rec.freq === "yearly") {
    candidate = addCalendarYears(start, interval);
  } else {
    return null;
  }
  if (rec.until) {
    const until = parseISODate(rec.until);
    if (until && candidate.getTime() > until.getTime()) return null;
  }
  if (rec.count !== undefined && rec.count !== null) {
    const completed = Number.isInteger(rec.completed) ? rec.completed : 0;
    if (completed + 1 >= rec.count) return null;
  }
  return isoDateString(candidate);
}
// 与 recur.py describe_recurrence 同语义的中文人话描述
function describeRecurrence(rec, due) {
  if (!rec || !rec.freq) return "";
  const interval = Number.isInteger(rec.interval) && rec.interval >= 1 ? rec.interval : 1;
  const anchor = parseISODate(due);
  let text;
  if (rec.freq === "daily") {
    text = interval === 1 ? "每天" : `每 ${interval} 天`;
  } else if (rec.freq === "weekly") {
    const prefix = interval === 1 ? "每周" : `每 ${interval} 周`;
    if (Array.isArray(rec.byday) && rec.byday.length) {
      const picked = new Set(rec.byday);
      const names = RECUR_WEEKDAYS.filter((day) => picked.has(day)).map(
        (day) => RECUR_WEEKDAY_CN[day],
      );
      text = `${prefix}的${names.join("、")}`;
    } else text = prefix;
  } else if (rec.freq === "monthly") {
    if (anchor)
      text = interval === 1
        ? `每月 ${anchor.getDate()} 日`
        : `每 ${interval} 个月的 ${anchor.getDate()} 日`;
    else text = interval === 1 ? "每月" : `每 ${interval} 个月`;
  } else {
    if (anchor)
      text = interval === 1
        ? `每年 ${anchor.getMonth() + 1} 月 ${anchor.getDate()} 日`
        : `每 ${interval} 年的 ${anchor.getMonth() + 1} 月 ${anchor.getDate()} 日`;
    else text = interval === 1 ? "每年" : `每 ${interval} 年`;
  }
  const suffixes = [];
  if (rec.until) suffixes.push(`直到 ${rec.until}`);
  if (rec.count !== undefined && rec.count !== null)
    suffixes.push(`共 ${rec.count} 次`);
  if (suffixes.length) text += `，${suffixes.join("，")}`;
  return text;
}
// 演示模式专用：复刻服务端 apply_recurrence_rollover 的滚动语义
function rolloverRecurrence(item) {
  const rec = item.meta?.recurrence;
  if (!rec || item.status !== "done") return item;
  const next = nextDueDate(rec, item.meta?.due);
  if (next === null) {
    return {
      ...item,
      status: "done",
      meta: { ...item.meta, recurrence: { ...rec, exhausted: true } },
    };
  }
  const previous = Number.isInteger(rec.completed) ? rec.completed : 0;
  return {
    ...item,
    status: "active",
    meta: {
      ...item.meta,
      due: next,
      recurrence: { ...rec, completed: previous + 1 },
      lastCompletedAt: new Date().toISOString(),
    },
  };
}
// 月历用：从当前 due 起（含本次）取未来若干次到期日，受 until/count 约束
function nextOccurrences(item, limit = 3) {
  const due = item.meta?.due;
  const rec = item.meta?.recurrence;
  if (!due) return [];
  if (rec?.exhausted) return [due];
  if (!rec || !rec.freq) return [due];
  const remaining = Number.isInteger(rec.count)
    ? Math.max(0, rec.count - (Number.isInteger(rec.completed) ? rec.completed : 0))
    : Infinity;
  const cap = Math.min(limit, remaining);
  const dates = [];
  let cursor = due;
  let guard = 0;
  while (dates.length < cap && guard++ < 400) {
    dates.push(cursor);
    const next = nextDueDate(rec, cursor);
    if (next === null) break;
    cursor = next;
  }
  return dates;
}
// 生成 UUID。crypto.randomUUID 只在安全上下文（HTTPS/localhost）可用，
// 通过 HTTP 访问时为 undefined，会直接抛错让整个流程崩溃。这里统一降级到
// crypto.getRandomValues（HTTP 下仍可用），最后再退回时间戳加随机数。
function randomUUID() {
  if (typeof crypto !== "undefined") {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    if (typeof crypto.getRandomValues === "function") {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
      bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
      const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
  }
  // 最后兜底：碰撞概率足够低，仅用于本地临时标识。
  return `tmp-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
}

function makeSubtaskId() {
  return randomUUID().replace(/-/g, "");
}
function itemTags(item, prefillMeta) {
  const source = item ? item.meta : prefillMeta;
  const tags = [];
  const seen = new Set();
  for (const tag of source?.tags || []) {
    if (typeof tag === "string" && tag.trim() && !seen.has(tag.trim())) {
      seen.add(tag.trim());
      tags.push(tag.trim());
    }
  }
  const legacy = source?.tag;
  if (typeof legacy === "string" && legacy.trim() && !seen.has(legacy.trim()))
    tags.push(legacy.trim());
  return tags;
}
function itemMatchesTag(item, tagName) {
  if (!tagName) return true;
  return itemTags(item).includes(tagName);
}
function subtaskProgress(item) {
  const list = Array.isArray(item.meta?.subtasks) ? item.meta.subtasks : [];
  const done = list.filter((sub) => sub.done === true).length;
  return { total: list.length, done };
}
function hasMarkdownMarkup(text) {
  if (typeof text !== "string" || !text.trim()) return false;
  return /(^|\n)\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s)|(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|:[a-z0-9_+-]+:)/.test(
    text,
  );
}
// 安全 Markdown 渲染：渲染器不可用或异常时降级 textContent 纯文本
function renderMarkdownInto(container, text) {
  container.replaceChildren();
  const renderer = window.ZhixuMarkdown;
  if (renderer && typeof renderer.render === "function") {
    try {
      container.append(renderer.render(text || ""));
      return;
    } catch {
      /* 渲染异常时降级为纯文本，保证内容仍可读 */
    }
  }
  container.textContent = text || "";
}

// ---------------------------------------------------------------------------
// 通知铃铛：登录后按会话拉取，进入应用/切空间/保存条目后顺带刷新，不轮询
// ---------------------------------------------------------------------------
async function loadNotifications(force = false) {
  if (state.demo) {
    // 演示模式用本地 mock 数据，便于离线体验完整界面
    if (!state.notifications.loaded || force) {
      state.notifications.items = demoNotifications();
      state.notifications.unread = state.notifications.items.filter((n) => !n.read).length;
      state.notifications.loaded = true;
    }
    updateNotifyBadge();
    return;
  }
  if (!state.user || (state.notifications.loading && !force)) return;
  state.notifications.loading = true;
  const session = state.sessionEpoch;
  try {
    const result = await api("/api/notifications?limit=20");
    if (state.sessionEpoch !== session) return;
    state.notifications.items = Array.isArray(result.notifications) ? result.notifications : [];
    state.notifications.unread = Number(result.unreadCount) || 0;
    state.notifications.loaded = true;
  } catch {
    /* 铃铛静默失败，不打扰主流程 */
  } finally {
    if (state.sessionEpoch === session) state.notifications.loading = false;
  }
  updateNotifyBadge();
}
function updateNotifyBadge() {
  const badge = $("#notify-badge");
  if (!badge) return;
  const count = state.demo
    ? state.notifications.items.filter((n) => !n.read).length
    : state.notifications.unread;
  badge.hidden = count <= 0;
  badge.textContent = count > 99 ? "99+" : String(count);
  $("#notify-button")?.setAttribute(
    "aria-label",
    count > 0 ? `通知，${count} 条未读` : "通知",
  );
}
function notificationTime(stamp) {
  const then = new Date(stamp).getTime();
  if (!Number.isFinite(then)) return "";
  const diff = Date.now() - then;
  const minute = 60000;
  if (diff < minute) return "刚刚";
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  if (diff < 48 * 60 * minute) return "昨天";
  return new Date(stamp).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}
function demoNotifications() {
  const now = Date.now();
  return [
    {
      id: "demo-n1",
      type: "task.due",
      title: "整理家庭相册",
      body: "任务「整理家庭相册」已于今天到期，请及时处理。",
      data: {},
      read: 0,
      createdAt: new Date(now - 32 * 60000).toISOString(),
    },
    {
      id: "demo-n2",
      type: "task.due",
      title: "给妈妈回电话",
      body: "任务「给妈妈回电话」已于昨天到期，请及时处理。",
      data: {},
      read: 1,
      createdAt: new Date(now - 26 * 3600000).toISOString(),
    },
  ];
}
function showNotifications() {
  loadNotifications();
  showModal("通知", (body) => {
    const list = el("div", "notify-list");
    const render = () => {
      list.replaceChildren();
      const items = state.notifications.items;
      if (!state.notifications.loaded && !state.demo) {
        const row = el("div", "loading-panel");
        row.append(el("span", "spinner"), el("span", "", "正在加载通知…"));
        list.append(row);
        return;
      }
      if (!items.length) {
        emptyState(list, "bell", "暂时没有新通知", "任务到期时会在这里提醒你。");
        return;
      }
      for (const n of items) {
        const rowItem = el("div", `notify-item${n.read ? "" : " unread"}`);
        const iconWrap = el("span", "notify-item-icon");
        iconWrap.append(svg("task"));
        const textWrap = el("button", "notify-item-main", () => openNotification(n));
        textWrap.type = "button";
        textWrap.append(
          el("strong", "", n.title || "提醒"),
          el("small", "", n.body || ""),
          el("small", "notify-time", notificationTime(n.createdAt)),
        );
        rowItem.append(iconWrap, textWrap);
        if (!n.read) {
          const mark = button("标为已读", "button soft compact", async () => {
            await markNotificationRead(n.id);
            render();
          });
          rowItem.append(mark);
        }
        list.append(rowItem);
      }
    };
    render();
    const footer = actionsRow(
      button("全部已读", "button soft", async () => {
        await markAllNotificationsRead();
        render();
      }),
    );
    body.append(list, footer);
  }, "提醒与待办");
}
async function markNotificationRead(id) {
  if (state.demo) {
    const target = state.notifications.items.find((n) => n.id === id);
    if (target) target.read = 1;
    state.notifications.unread = state.notifications.items.filter((n) => !n.read).length;
    updateNotifyBadge();
    return;
  }
  try {
    await api("/api/notifications/read", "POST", { id });
    const target = state.notifications.items.find((n) => n.id === id);
    if (target) target.read = 1;
    state.notifications.unread = Math.max(0, state.notifications.unread - 1);
    updateNotifyBadge();
  } catch (err) {
    toast(errorMessage(err), true);
  }
}
async function markAllNotificationsRead() {
  if (state.demo) {
    state.notifications.items.forEach((n) => (n.read = 1));
    state.notifications.unread = 0;
    updateNotifyBadge();
    return;
  }
  try {
    await api("/api/notifications/read", "POST", { all: true });
    state.notifications.items.forEach((n) => (n.read = 1));
    state.notifications.unread = 0;
    updateNotifyBadge();
  } catch (err) {
    toast(errorMessage(err), true);
  }
}
async function openNotification(n) {
  closeModal();
  if (n.type === "task.due" && n.data?.itemId) {
    const sid = n.data.spaceId;
    if (sid && state.space?.id !== sid && !state.demo) {
      try {
        await switchSpace(sid);
      } catch {
        /* 空间可能已无权限，继续尝试本地查找 */
      }
    }
    const item = state.items.find((candidate) => candidate.id === n.data.itemId);
    if (item) showEditor("task", item);
    else toast("这条任务可能已被删除或移动。");
    return;
  }
}
// 首页快捷方式（2b P5）在下方区块完整实现；此处先提供安全空实现，避免
// enterApp 在功能完成前引用未定义函数。
// 首页快捷方式：服务端 /api/settings 持久化（按账号隔离），演示模式存内存。
// 可选的页面白名单与服务端 settings.SHORTCUT_VIEWS 保持一致。
const SHORTCUT_VIEWS = [
  ["overview", "总览", "grid"],
  ["daily", "今日记录", "camera"],
  ["tasks", "待办事项", "task"],
  ["calendar", "日历日程", "calendar"],
  ["notes", "灵感笔记", "note"],
  ["media", "媒体资料", "folder"],
  ["search", "全局搜索", "search"],
  ["vault", "私密密码箱", "lock"],
  ["spaces", "空间与协作", "users"],
  ["modules", "模块库", "puzzle"],
  ["bedtime", "晚安故事", "cloud"],
];
const MAX_SHORTCUTS = 8;
function shortcutLabel(view) {
  const found = SHORTCUT_VIEWS.find(([id]) => id === view);
  return found ? { label: found[1], icon: found[2] } : { label: view, icon: "grid" };
}
function demoShortcuts() {
  return ["tasks", "calendar", "daily"];
}
async function loadShortcuts(force = false) {
  if (state.demo) {
    if (!state.shortcuts || force) state.shortcuts = demoShortcuts();
    return;
  }
  if (!state.user || (state.shortcutsLoading && !force)) return;
  state.shortcutsLoading = true;
  const session = state.sessionEpoch;
  try {
    const result = await api("/api/settings");
    if (state.sessionEpoch !== session) return;
    state.shortcuts = Array.isArray(result.shortcuts) ? result.shortcuts : [];
  } catch {
    if (state.sessionEpoch === session && state.shortcuts === null) state.shortcuts = [];
  } finally {
    if (state.sessionEpoch === session) state.shortcutsLoading = false;
  }
}
async function saveShortcuts(next) {
  const cleaned = Array.from(new Set(next)).slice(0, MAX_SHORTCUTS);
  if (state.demo) {
    state.shortcuts = cleaned;
    return true;
  }
  try {
    const result = await api("/api/settings", "PATCH", { shortcuts: cleaned });
    state.shortcuts = Array.isArray(result.shortcuts) ? result.shortcuts : cleaned;
    return true;
  } catch (err) {
    toast(errorMessage(err), true);
    return false;
  }
}
function renderShortcutBar(content) {
  const views = state.shortcuts === null ? [] : state.shortcuts;
  if (!views.length) return;
  const bar = el("section", "card shortcut-bar");
  const head = el("div", "shortcut-head");
  head.append(el("span", "shortcut-label", "常用功能"));
  head.append(
    button("调整", "text-button", showShortcutSettings, "settings"),
  );
  bar.append(head);
  const row = el("div", "shortcut-row");
  for (const view of views) {
    const { label, icon } = shortcutLabel(view);
    const item = button("", "shortcut-item", () => navigate(view));
    item.append(svg(icon), el("span", "", label));
    row.append(item);
  }
  bar.append(row);
  content.append(bar);
}
function showShortcutSettings() {
  showModal("首页快捷方式", (body) => {
    body.append(
      el(
        "p",
        "helper",
        `最多 ${MAX_SHORTCUTS} 个。选中的功能会出现在首页顶部，一键直达。`,
      ),
    );
    const current = new Set(state.shortcuts || []);
    const grid = el("div", "shortcut-picker");
    const boxes = [];
    for (const [view, label, icon] of SHORTCUT_VIEWS) {
      const row = el("label", "shortcut-choice");
      const box = el("input");
      box.type = "checkbox";
      box.checked = current.has(view);
      box.value = view;
      boxes.push(box);
      const text = el("span", "shortcut-choice-text");
      text.append(svg(icon), document.createTextNode(label));
      row.append(box, text);
      grid.append(row);
    }
    const error = el("p", "form-error");
    error.setAttribute("role", "alert");
    const save = button("保存", "button primary", async () => {
      const picked = boxes.filter((box) => box.checked).map((box) => box.value);
      if (picked.length > MAX_SHORTCUTS) {
        error.textContent = `最多选择 ${MAX_SHORTCUTS} 个`;
        return;
      }
      save.disabled = true;
      if (await saveShortcuts(picked)) {
        closeModal();
        render();
        toast("快捷方式已更新");
      }
      save.disabled = false;
    });
    body.append(grid, error, actionsRow(save));
  }, "个性化");
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

// 从 Content-Disposition 解析下载文件名，兼容 filename* 与普通 filename
function downloadFilename(disposition, fallback) {
  if (typeof disposition !== "string") return fallback;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* 编码异常时退回普通 filename */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
  if (plain) return plain[1].trim();
  return fallback;
}

// 用隐藏锚点触发 blob 下载，返回对象 URL 供调用方在 finally 中回收
function triggerBlobDownload(blob, filename) {
  const objectURL = URL.createObjectURL(blob);
  state.objectURLs.add(objectURL);
  const anchor = el("a");
  anchor.href = objectURL;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  return objectURL;
}

// 空间数据导出：GET 拿到 zip 二进制流，校验类型后落地为浏览器下载
async function exportSpaceData(space, format, trigger) {
  if (state.demo) {
    toast("演示模式不提供导出", true);
    return;
  }
  const key = `${space.id}:${format}`;
  if (exportPending.has(key)) return;
  const originalText = trigger.textContent;
  exportPending.add(key);
  trigger.disabled = true;
  trigger.textContent = "导出中…";
  let objectURL = "";
  try {
    const path = `/api/spaces/${encodeURIComponent(space.id)}/export?format=${encodeURIComponent(format)}`;
    let response;
    try {
      response = await fetch(path, {
        method: "GET",
        credentials: "same-origin",
        headers: {
          Accept: "application/zip",
          ...(state.user?.id ? { "X-Expected-User": state.user.id } : {}),
        },
      });
    } catch {
      throw new Error("无法连接工作台服务，请检查网络后重试。");
    }
    if (response.status === 401) {
      showAuth();
      throw new Error("登录已过期，请重新登录。");
    }
    const contentType = response.headers.get("Content-Type") || "";
    if (!response.ok || !contentType.includes("zip")) {
      let result = {};
      try {
        result = await response.json();
      } catch {
        /* 非 JSON 错误响应，交给通用状态文案 */
      }
      throw Object.assign(
        new Error(apiStatusMessage(response.status, result, response)),
        { status: response.status },
      );
    }
    const blob = await response.blob();
    const date = new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      timeZone: "Asia/Shanghai",
    })
      .format(new Date())
      .replace(/\//g, "-");
    const fallback = `知序导出-${space.name}-${format}-${date}.zip`;
    const filename = downloadFilename(
      response.headers.get("Content-Disposition"),
      fallback,
    );
    objectURL = triggerBlobDownload(blob, filename);
    toast(`已开始导出 ${format.toUpperCase()} 文件`);
  } catch (err) {
    toast(errorMessage(err), true);
  } finally {
    if (objectURL) {
      URL.revokeObjectURL(objectURL);
      state.objectURLs.delete(objectURL);
    }
    exportPending.delete(key);
    if (trigger.isConnected) {
      trigger.disabled = state.demo;
      trigger.textContent = originalText;
    }
  }
}

// 空间卡片内的「导出我的数据」分组与删除入口，仅 owner 可见完整操作
function buildSpaceDataControls(space) {
  const wrap = el("div", "space-data-actions");
  if (space.role !== "owner") {
    wrap.append(el("p", "export-note", "仅空间所有者可导出"));
    return wrap;
  }
  const group = el("div", "export-group");
  group.append(el("h4", "", "导出我的数据"));
  const actions = el("div", "export-actions");
  const formats = [
    ["JSON", "json"],
    ["Markdown", "markdown"],
    ["HTML", "html"],
  ];
  formats.forEach(([label, format]) => {
    const b = button(
      label,
      "button secondary",
      (event) => exportSpaceData(space, format, event.currentTarget),
    );
    if (state.demo) b.disabled = true;
    actions.append(b);
  });
  group.append(actions);
  if (state.demo) {
    group.append(el("p", "export-note", "演示模式不提供导出"));
  } else {
    group.append(
      el("p", "helper", "导出内容打包为 ZIP 下载，包含该空间内你的工作与生活记录。"),
    );
  }
  wrap.append(group);
  wrap.append(
    button("删除空间", "text-button danger-text", () => confirmDeleteSpace(space), "trash"),
  );
  return wrap;
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
    make(
      "lm1",
      "media",
      "life",
      "周末的散步路线",
      "",
      {
        demoPreview: "landscape",
        mime: "image/demo",
        name: "示例散步照片 · 无实际文件",
        size: 0,
      },
      "active",
      6,
    ),
    make(
      "lm2",
      "media",
      "life",
      "家里做的一顿饭",
      "",
      {
        demoPreview: "landscape",
        mime: "image/demo",
        name: "示例家常菜照片 · 无实际文件",
        size: 0,
      },
      "active",
      9,
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
  state.demoDaily = {};
  state.daily = {
    config: null,
    entries: [],
    digests: {},
    month: "",
    loading: false,
    loadedSpace: null,
    composer: [],
  };
  dailyConfigPromise = null;
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
  // 进入应用即刷新通知铃铛与快捷方式（此后在保存/切空间等动作时顺带刷新，不轮询）
  loadNotifications();
  loadShortcuts();
  if (!state.spaces.length) {
    showCreateSpace();
    render();
    return;
  }
  const preferred =
    state.spaces.find((s) => s.id === sessionStorage.getItem(`zhixu:space:${state.user.id}`)) ||
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
function bedtimePath(panel = "", includeSpace = true) {
  const query = new URLSearchParams();
  if (includeSpace && !state.demo && state.space) query.set("space", state.space.id);
  if (panel === "voices") query.set("panel", panel);
  const search = query.toString();
  return "/bedtime.html" + (search ? `?${search}` : "");
}
function openBedtime(panel = "") {
  if (state.demo || !state.user) {
    authReturnPath = bedtimePath(panel, false);
    history.replaceState(null, "", `/?next=${encodeURIComponent(authReturnPath)}`);
    showAuth();
    updateAuthReturnNote();
    return;
  }
  if (state.space) location.href = bedtimePath(panel);
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
  state.demoDaily = {};
  state.daily = {
    config: null,
    entries: [],
    digests: {},
    month: "",
    loading: false,
    loadedSpace: null,
    composer: [],
  };
  dailyConfigPromise = null;
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
  updateAuthReturnNote();
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
  state.daily.entries = [];
  state.daily.digests = {};
  state.daily.loadedSpace = null;
  state.daily.composer = [];
  state.tagFilter = null;
  state.spaceTags = [];
  state.tagsLoadedSpace = null;
  state.tagsLoading = false;
  state.calendar = { month: "", filter: null };
  state.search = { query: "", results: [], loading: false, request: 0, queried: "", error: "" };
  sessionStorage.setItem(`zhixu:space:${state.user.id}`, id);
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
  if (view === "daily") loadDailyEntries();
}
const areaViews = {
  work: ["overview", "daily", "search", "tasks", "calendar", "notes", "media", "vault", "spaces", "modules"],
  life: ["overview", "daily", "search", "bedtime", "tasks", "calendar", "notes", "media", "spaces", "modules"],
};
function setArea(area) {
  state.area = area;
  state.taskFilter = "all";
  state.mediaFilter = "all";
  state.tagFilter = null;
  state.calendar.filter = null;
  if (!areaViews[area].includes(state.view)) state.view = "overview";
  closeModal();
  render();
}
function render() {
  document.body.classList.toggle("life-area", state.area === "life");
  applyAreaNav();
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
  $("#page-title").textContent = areaName(state.view);
  $("#page-description").textContent = areaDescription(state.view);
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
  const todayDaily = state.daily.entries.filter(
    (entry) => entry.entryDate === dailyDateString(),
  ).length;
  $("#nav-daily-count").textContent = todayDaily;
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
    daily: renderDaily,
    search: renderSearch,
    tasks: renderTasks,
    calendar: renderCalendar,
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
// ---------------------------------------------------------------------------
// 首页「月历 + 当日回顾」：把今日记录、待办、随笔、媒体按天聚合展示。
// 数据来自三个既有来源（daily_entries / items.kind=task / note / media），
// 只做聚合不做迁移，原菜单功能保持不变。
// ---------------------------------------------------------------------------
const homeCalendar = { month: "", day: "" };

// 四类记录：待办、记录（带图或图文）、随笔（纯文字）、媒体。
function recordCategory(item) {
  if (item.kind === "media") return "media";
  if (item.kind === "task") return "task";
  return "note"; // 笔记与随笔同属文字类
}
const CATEGORY_META = {
  task: { label: "待办", icon: "task" },
  note: { label: "随笔", icon: "note" },
  media: { label: "媒体", icon: "folder" },
  daily: { label: "记录", icon: "camera" },
};

// 某一天的全部内容：当日到期的待办 + 当日创建/当日日期的笔记与媒体 + 今日记录。
function collectDayEntries(dateString) {
  const groups = { task: [], note: [], media: [], daily: [] };
  // 接口返回 camelCase 的 createdAt，演示数据同样是 camelCase；
  // 兼容 snake_case，并且绝不把「没有日期」的项目算到今天之外的日子。
  // 服务端接口返回 snake_case 的 created_at；演示数据用 camelCase，
  // 两种命名都要兼容，否则记录会被算成「无日期」而漏掉。
  const createdOf = (item) =>
    String(item.created_at || item.createdAt || "").slice(0, 10);
  for (const item of state.items) {
    if (item.status === "archived") continue;
    const cat = recordCategory(item);
    if (cat === "task") {
      // 待办按计划日期归属；无计划日期再看创建日期。
      const due = item.meta?.due || createdOf(item);
      if (due === dateString) groups.task.push(item);
    } else {
      if (createdOf(item) === dateString) groups[cat].push(item);
    }
  }
  for (const entry of state.daily.entries || []) {
    const d = entry.entryDate || entry.entry_date;
    if (d === dateString) groups.daily.push(entry);
  }
  return groups;
}
// 展示用的短文本：优先用正文/摘要的前几个字，而不是标题。
// 标题只在「转为待办」后才需要，列表与日历里一律显示内容本身。
function snippetOf(item, limit = 12) {
  const raw =
    (item.body || "").trim() ||
    (item.summary || "").trim() ||
    (item.title || "").trim();
  const flat = raw.replace(/\s+/g, " ").trim();
  if (!flat) return "（无内容）";
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

// 一条事项的统一描述：类型 + 短内容，供日历与日程列表共用。
function describeEntry(kind, item, limit = 14) {
  if (kind === "daily")
    return snippetOf({ body: item.body, summary: item.summary }, limit);
  return snippetOf(item, limit);
}

function countDayEntries(dateString) {
  const g = collectDayEntries(dateString);
  return g.task.length + g.note.length + g.media.length + g.daily.length;
}

// 顶部月历：可翻月，有内容的日期显示圆点，点击选中某天。
function renderHomeCalendar(content) {
  if (!homeCalendar.month) homeCalendar.month = dailyDateString().slice(0, 7);
  if (!homeCalendar.day) homeCalendar.day = dailyDateString();
  const monthValue = homeCalendar.month;
  const [year, month] = monthValue.split("-").map(Number);
  const firstDay = new Date(year, month - 1, 1);
  const gridStart = new Date(year, month - 1, 1 - weekdayIndex(firstDay));
  const today = dailyDateString();

  const card = el("section", "card home-calendar");
  const nav = el("div", "calendar-nav");
  const prev = button("", "icon-button", () => {
    homeCalendar.month = shiftMonth(monthValue, -1);
    render();
  }, "chevron");
  prev.setAttribute("aria-label", "上一月");
  prev.querySelector(".icon").style.setProperty("transform", "rotate(180deg)");
  const next = button("", "icon-button", () => {
    homeCalendar.month = shiftMonth(monthValue, 1);
    render();
  }, "chevron");
  next.setAttribute("aria-label", "下一月");
  nav.append(
    prev,
    el("h2", "calendar-title", calendarMonthLabel(monthValue)),
    button("今天", "button secondary calendar-nav-btn", () => {
      homeCalendar.month = today.slice(0, 7);
      homeCalendar.day = today;
      render();
    }),
    next,
  );
  card.append(nav);

  const weekdays = el("div", "home-calendar-weekdays");
  ["一", "二", "三", "四", "五", "六", "日"].forEach((w) =>
    weekdays.append(el("span", "", w)),
  );
  card.append(weekdays);

  const grid = el("div", "home-calendar-grid");
  for (let index = 0; index < 42; index++) {
    const cellDate = new Date(
      gridStart.getFullYear(),
      gridStart.getMonth(),
      gridStart.getDate() + index,
    );
    const dateString = isoDateString(cellDate);
    const inMonth = cellDate.getMonth() === month - 1;
    const isToday = dateString === today;
    const isPicked = dateString === homeCalendar.day;
    const total = countDayEntries(dateString);
    const cell = button(
      "",
      `home-calendar-cell${inMonth ? "" : " muted"}${isToday ? " today" : ""}${
        isPicked ? " picked" : ""
      }`,
      () => {
        homeCalendar.day = dateString;
        render();
      },
    );
    cell.append(el("span", "home-calendar-day", String(cellDate.getDate())));
    if (total > 0) {
      // 格子里直接显示当天第一条内容的前几个字，不再用标题或纯圆点。
      const groups = collectDayEntries(dateString);
      const first = groups.daily[0] || groups.task[0] || groups.note[0] || groups.media[0];
      const kind = groups.daily[0] ? "daily" : groups.task[0] ? "task" : groups.note[0] ? "note" : "media";
      const label = el("span", "home-calendar-preview", describeEntry(kind, first));
      if (kind === "task") label.classList.add("is-task");
      cell.append(label);
      if (total > 1) cell.append(el("span", "home-calendar-more", `+${total - 1}`));
    }
    cell.setAttribute(
      "aria-label",
      `${dateString}${isToday ? "（今天）" : ""}${total ? `，${total} 条内容` : "，暂无内容"}`,
    );
    cell.setAttribute("aria-pressed", String(isPicked));
    grid.append(cell);
  }
  card.append(grid);
  content.append(card);
}

// 当日回顾：按四类分组列出所选日期的全部内容。
function renderDayReview(content) {
  const dateString = homeCalendar.day || dailyDateString();
  if (state.space && state.daily.loadedSpace !== state.space.id && !state.daily.loading) {
    loadDailyEntries();
  }
  const [y, m, d] = dateString.split("-").map(Number);
  const isToday = dateString === dailyDateString();
  const card = el("section", "card day-review");
  const head = el("div", "day-review-head");
  head.append(
    el("h2", "", isToday ? "今天的回顾" : `${m} 月 ${d} 日的回顾`),
    el("span", "day-review-date", `${y}-${m}-${String(d).padStart(2, "0")} ${weekdayCn(dateString)} · ${lunarText(dateString)}`),
  );
  card.append(head);

  const groups = collectDayEntries(dateString);
  const total = groups.task.length + groups.note.length + groups.media.length + groups.daily.length;
  if (!total) {
    emptyState(
      card,
      "calendar",
      isToday ? "今天还没有记录" : "这一天没有内容",
      isToday ? "拍一张、写一句，或加一件待办。" : "点击日历里的其他日期查看。",
      writable() ? "记录今天" : null,
      writable() ? () => navigate("daily") : null,
    );
    content.append(card);
    return;
  }

  const section = (title, iconName, nodes, emptyText) => {
    if (!nodes.length) return;
    const block = el("div", "day-section");
    const heading = el("div", "day-section-head");
    heading.append(svg(iconName, true), el("span", "", `${title} ${nodes.length}`));
    block.append(heading);
    const list = el("div", "day-section-list");
    nodes.forEach((n) => list.append(n));
    block.append(list);
    card.append(block);
  };

  // 待办：可勾选完成
  section(
    "待办",
    "task",
    groups.task.map((item) => {
      const row = el("div", `day-entry day-entry-task${item.status === "done" ? " done" : ""}`);
      const check = button("", `task-check${item.status === "done" ? " checked" : ""}`, async () => {
        if (!writable()) return;
        await saveItem(
          { ...item, status: item.status === "done" ? "active" : "done" },
          item.id,
        );
      });
      check.setAttribute("aria-label", item.status === "done" ? "标记为未完成" : "标记为完成");
      check.disabled = !writable();
      if (item.status === "done") check.append(svg("check"));
      const text = button("", "day-entry-text", () => showEditor("task", item));
      text.type = "button";
      text.append(el("span", "day-entry-title", item.title));
      const meta = [];
      if (item.meta?.priority === "high") meta.push("优先");
      if (item.meta?.due) meta.push(taskTimeLabel(item));
      if (meta.length) text.append(el("small", "", meta.join(" · ")));
      row.append(check, text);
      return row;
    }),
  );

  // 今日记录：带图或文字速记
  section(
    "记录",
    "camera",
    groups.daily.map((entry) => {
      const row = el("div", "day-entry day-entry-daily");
      const img = (entry.images || [])[0];
      if (img) {
        const thumb = el("img", "day-thumb");
        thumb.src = `/api/spaces/${state.space.id}/daily/images/${img.id}`;
        thumb.alt = entry.title || "记录图片";
        thumb.loading = "lazy";
        row.append(thumb);
      } else {
        const ph = el("span", "day-thumb day-thumb-empty");
        ph.append(svg("note", true));
        row.append(ph);
      }
      const text = button("", "day-entry-text", () => showDailyEntry(entry));
      text.type = "button";
      text.append(el("span", "day-entry-title", entry.title || "无标题记录"));
      if (entry.summary) text.append(el("small", "", entry.summary));
      row.append(text);
      return row;
    }),
  );

  // 随笔：纯文字，可选一键转待办
  section(
    "随笔",
    "note",
    groups.note.map((item) => {
      const row = el("div", "day-entry");
      const text = button("", "day-entry-text", () => showEditor("note", item));
      text.type = "button";
      text.append(el("span", "day-entry-title", item.title));
      const body = (item.body || "").replace(/\s+/g, " ").trim();
      if (body) text.append(el("small", "", body.slice(0, 60)));
      row.append(text);
      if (writable()) {
        const promote = button("转为待办", "text-button", async () => {
          await saveItem(
            {
              kind: "task",
              area: item.area,
              title: item.title,
              body: item.body,
              status: "active",
              meta: { ...(item.meta || {}), due: dateString },
            },
            item.id,
          );
          toast("已转为待办");
        });
        row.append(promote);
      }
      return row;
    }),
  );

  // 媒体
  section(
    "媒体",
    "folder",
    groups.media.map((item) => {
      const row = el("div", "day-entry");
      const text = button("", "day-entry-text", () => showMedia(item));
      text.type = "button";
      text.append(el("span", "day-entry-title", item.title));
      row.append(text);
      return row;
    }),
  );

  const foot = el("div", "day-review-foot");
  if (writable()) {
    foot.append(
      button("记录今天", "button primary", () => navigate("daily"), "camera"),
      button("加一件待办", "button soft", () => showEditor("task", null, { due: dateString }), "plus"),
    );
  }
  card.append(foot);
  content.append(card);
}

function renderOverview(content) {
  // 首页结构：状态标签 → 对应列表 → 小日历 → 本月日程 → 快捷入口。
  // 今日记录只归入「本月日程」的今天分组，不再另设重复卡片。
  renderStatusTabs(content);
  renderStatusList(content);
  renderHomeCalendar(content);
  renderMonthAgenda(content);
  if (state.area === "life") renderBedtimeEntry(content);
  renderShortcutBar(content);
}

// 待办状态：待完成 / 进行中 / 已完成（已删除不进入首页列表）。
// 待办只有三种状态：待办（active）、已完成（done）、已删除（dropped）。
const OVERVIEW_STATUS = [
  ["active", "待办"],
  ["done", "已完成"],
  ["dropped", "已删除"],
];
// 历史数据里可能残留 doing（进行中），统一并入待办，避免出现空标签。
function normalizeTaskStatus(status) {
  if (status === "doing") return "active";
  return status || "active";
}
function renderStatusTabs(content) {
  const tasks = currentItems("task");
  const bar = el("div", "status-tabs");
  for (const [key, label] of OVERVIEW_STATUS) {
    const count = tasks.filter((t) => normalizeTaskStatus(t.status) === key).length;
    const active = (state.overviewStatus || "active") === key;
    const tab = button(`${label} ${count}`, `status-tab${active ? " active" : ""}`, () => {
      state.overviewStatus = key;
      render();
    });
    tab.setAttribute("aria-pressed", String(active));
    bar.append(tab);
  }
  content.append(bar);
}
function renderStatusList(content) {
  const key = state.overviewStatus || "active";
  const label = (OVERVIEW_STATUS.find(([k]) => k === key) || ["", "待完成"])[1];
  const tasks = currentItems("task").filter((t) => normalizeTaskStatus(t.status) === key);
  const card = el("section", "card overview-status-list");
  const heading =
    key === "active" ? "待办事项" : `已${label === "已完成" ? "完成" : "删除"}的事项`;
  card.append(
    cardHeading(heading, "task", "全部待办 ↗", () => navigate("tasks"), "我的节奏"),
  );
  const list = el("div", "task-list");
  if (tasks.length) {
    tasks.forEach((item) => list.append(taskRow(item)));
  } else {
    emptyState(
      list,
      "task",
      key === "done" ? "还没有完成的事项" : "这里还是空的",
      key === "done" ? "完成的待办会出现在这里。" : "记下一件要做的事，它会出现在这里。",
      writable() && key === "active" ? "添加第一件待办" : null,
      writable() && key === "active" ? () => showEditor("task") : null,
    );
  }
  card.append(list);
  content.append(card);
}

// 本月日程：从今天往回排到 1 号，可滚动查看更早的内容。
function renderMonthAgenda(content) {
  if (state.space && state.daily.loadedSpace !== state.space.id && !state.daily.loading) {
    loadDailyEntries();
  }
  const today = dailyDateString();
  const [year, month, day] = today.split("-").map(Number);
  const card = el("section", "card month-agenda");
  card.append(
    cardHeading("本月日程", "calendar", "打开日程 ↗", () => navigate("calendar"), "回顾这个月"),
  );
  const list = el("div", "month-agenda-list");
  let rendered = 0;
  // 从今天倒序到 1 号
  for (let d = day; d >= 1; d--) {
    const dateString = `${year}-${String(month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const groups = collectDayEntries(dateString);
    const total = groups.task.length + groups.note.length + groups.media.length + groups.daily.length;
    if (!total) continue;
    rendered += 1;
    const row = el("div", "month-agenda-day");
    const head = el("div", "month-agenda-date");
    head.append(
      el("strong", "", `${month} 月 ${d} 日`),
      el("span", "", `${weekdayCn(dateString)}${d === day ? " · 今天" : ""}`),
      el("em", "month-agenda-count", `${total} 条`),
    );
    row.append(head);
    // 待办优先，其次记录，再随笔与媒体
    const items = el("div", "month-agenda-items");
    // 每条事项是一个独立小事框，展示「内容前几个字」而非标题。
    const makeRow = (kind, item, icon, onClick) => {
      const b = button("", `month-agenda-item month-agenda-${kind}`, onClick);
      b.type = "button";
      if (kind === "task" && normalizeTaskStatus(item.status) === "done")
        b.classList.add("is-done");
      // 带图片的记录直接露出缩略图，一眼能看出记了什么画面。
      const firstImage = kind === "daily" ? (item.images || [])[0] : null;
      const url = firstImage ? dailyImageURL(firstImage) : "";
      if (url) {
        const thumb = el("img", "month-agenda-thumb");
        thumb.src = url;
        thumb.alt = item.summary || "记录图片";
        thumb.loading = "lazy";
        b.append(thumb);
      } else {
        b.append(svg(icon, true));
      }
      const label = el("span", "", describeEntry(kind, item));
      b.append(label);
      if (kind === "daily" && (item.images || []).length > 1)
        b.append(el("em", "month-agenda-imgnum", `${item.images.length} 张`));
      return b;
    };
    groups.task.forEach((item) =>
      items.append(makeRow("task", item, "task", () => showEditor("task", item))),
    );
    groups.daily.forEach((entry) =>
      items.append(makeRow("daily", entry, "camera", () => showDailyEntry(entry))),
    );
    groups.note.forEach((item) =>
      items.append(makeRow("note", item, "note", () => showEditor("note", item))),
    );
    groups.media.forEach((item) =>
      items.append(makeRow("media", item, "folder", () => showMedia(item))),
    );
    row.append(items);
    list.append(row);
  }
  if (!rendered) {
    emptyState(list, "calendar", "本月还没有记录", "从今天开始，记下第一件事。");
  }
  card.append(list);
  content.append(card);
}

// 原统计卡片与「今日记录」卡片单独拆出，供新首页复用在列表下方。
function renderOverviewStats(content) {
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
function renderBedtimeEntry(content) {
  const entry = el("section", "life-bedtime-entry");
  entry.setAttribute("aria-labelledby", "life-bedtime-title");
  const illustration = el("div", "life-bedtime-illustration");
  illustration.setAttribute("aria-hidden", "true");
  illustration.append(el("span", "life-bedtime-moon"), el("span", "life-bedtime-star", "✦"), svg("cloud"));
  const copy = el("div", "life-bedtime-copy");
  const title = el("h2", "", "睡前小故事");
  title.id = "life-bedtime-title";
  copy.append(title, el("p", "", "选年龄、挑一个喜欢的主角，让故事陪你慢慢入眠。"));
  const actions = el("div", "life-bedtime-actions");
  for (const [label, panel, cls] of [["看故事", "", "button bedtime-entry-read"], ["上传我的声音", "voices", "button bedtime-entry-voice"]]) {
    const link = el("a", cls, label);
    link.href = bedtimePath(panel);
    if (state.demo) link.addEventListener("click", (event) => {
      event.preventDefault();
      openBedtime(panel);
    });
    actions.append(link);
  }
  copy.append(actions, el("p", "life-bedtime-caption", state.demo
    ? "登录自己的账户后，可上传声音与保存故事。"
    : "暖光阅读、分段讲解与睡眠定时。故事和声音可以分别选择。"));
  entry.append(copy, illustration);
  content.append(entry);
}
// 卡片上的子任务迷你清单：勾选即 PATCH 整个 meta.subtasks；只读空间禁用
function renderSubtaskMiniList(item) {
  const list = Array.isArray(item.meta?.subtasks) ? item.meta.subtasks : [];
  if (!list.length) return null;
  const { total, done } = subtaskProgress(item);
  const wrap = el("div", "subtask-mini");
  wrap.append(
    el("span", "subtask-mini-progress", `子任务已完成 ${done}/${total}`),
  );
  const items = el("div", "subtask-mini-list");
  list.forEach((sub, index) => {
    const line = el("div", `subtask-mini-row${sub.done ? " done" : ""}`);
    const check = input("checkbox");
    check.checked = sub.done === true;
    check.disabled = !writable();
    check.setAttribute("aria-label", `子任务 ${sub.title} 完成状态`);
    check.addEventListener("change", async () => {
      if (!ensureWrite()) {
        check.checked = sub.done === true;
        return;
      }
      const nextSubtasks = list.map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, done: check.checked } : entry,
      );
      check.disabled = true;
      try {
        await saveItem(
          { ...item, meta: { ...item.meta, subtasks: nextSubtasks } },
          item.id,
        );
      } catch (err) {
        toast(errorMessage(err), true);
        check.checked = sub.done === true;
        check.disabled = false;
      }
    });
    line.append(check, el("span", "subtask-mini-title", sub.title));
    items.append(line);
  });
  wrap.append(items);
  return wrap;
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
        if (item.meta?.recurrence && item.status !== "done") {
          // 滚动后的 due / 完成次数由 saveItem 回写的响应条目渲染，这里仅提示
          toast("已记录本次完成，并安排了下一次到期");
        }
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
  const body = el("div", "task-body");
  const info = button("", "task-info", () => showEditor("task", item));
  info.append(el("strong", "", item.title));
  const meta = item.meta || {};
  const metaParts = [meta.due ? `计划 ${meta.due}` : "不赶时间，一步一步"];
  const rec = meta.recurrence;
  if (rec?.freq) {
    if (rec.exhausted) metaParts.push("重复已结束");
    else {
      metaParts.push(describeRecurrence(rec, meta.due));
      if (Number.isInteger(rec.completed) && rec.completed > 0)
        metaParts.push(`已完成 ${rec.completed} 次`);
    }
  }
  info.append(el("small", "", metaParts.join(" · ")));
  body.append(info);
  const miniList = renderSubtaskMiniList(item);
  if (miniList) body.append(miniList);
  row.append(check, body);
  if (meta.priority === "high" && item.status !== "done")
    row.append(el("span", "tag orange priority-tag", "优先"));
  const tags = itemTags(item);
  if (tags.length) {
    const tagWrap = el("span", "task-tag-wrap");
    tags.slice(0, 3).forEach((tagName) =>
      tagWrap.append(
        el("span", `tag${state.area === "life" ? " orange" : ""}`, tagName),
      ),
    );
    row.append(tagWrap);
  }
  row.append(iconButton("more", "查看待办", () => showEditor("task", item)));
  return row;
}
function noteCard(item) {
  const card = button("", "note-card", () => showEditor("note", item));
  const tags = itemTags(item);
  card.append(
    el("span", "note-label", tags[0] || "随手记"),
    el("h3", "", item.title),
    el("p", "", item.body || "点击查看这条记录"),
  );
  if (tags.length > 1) {
    const more = el("div", "note-tags");
    tags.slice(1, 4).forEach((tagName) => more.append(el("span", "tag", tagName)));
    card.append(more);
  }
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
// 拉取当前空间标签聚合：真实模式走 GET /tags，演示模式从 demoStore 本地聚合
async function loadSpaceTags() {
  if (!state.space || state.tagsLoadedSpace === state.space.id) return;
  if (state.tagsLoading) return;
  state.tagsLoading = true;
  try {
    if (state.demo) {
      const counts = new Map();
      for (const item of state.demoStore[state.space.id] || []) {
        for (const tagName of new Set(itemTags(item)))
          counts.set(tagName, (counts.get(tagName) || 0) + 1);
      }
      state.spaceTags = [...counts.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([name, count]) => ({ name, count }));
    } else {
      const result = await api(`/api/spaces/${state.space.id}/tags`);
      state.spaceTags = Array.isArray(result.tags) ? result.tags : [];
    }
    state.tagsLoadedSpace = state.space.id;
    // 首次加载发生在 render 期间，拉取完成后补一次渲染让筛选条出现
    if (["tasks", "notes"].includes(state.view)) render();
  } catch (err) {
    toast(errorMessage(err), true);
  } finally {
    state.tagsLoading = false;
  }
}
// 标签筛选条：点标签筛选、再点取消；viewer 也可使用（读权限）
function renderTagFilterBar(content) {
  if (!state.spaceTags.length) return;
  const bar = el("div", "tag-filter-bar");
  bar.append(el("span", "tag-filter-label", "标签筛选"));
  const all = button(
    state.tagFilter ? "全部" : "全部 ·",
    state.tagFilter ? "tag-filter-chip" : "tag-filter-chip active",
    () => {
      state.tagFilter = null;
      render();
    },
  );
  all.setAttribute("aria-pressed", String(!state.tagFilter));
  bar.append(all);
  state.spaceTags.forEach(({ name, count }) => {
    const active = state.tagFilter === name;
    const chip = button(
      `${name} ${count}`,
      active ? "tag-filter-chip active" : "tag-filter-chip",
      () => {
        state.tagFilter = active ? null : name;
        render();
      },
    );
    chip.setAttribute("aria-pressed", String(active));
    bar.append(chip);
  });
  content.append(bar);
}
function renderTasks(content) {
  loadSpaceTags();
  const tasks = currentItems("task").filter((item) =>
    itemMatchesTag(item, state.tagFilter),
  );
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
  renderTagFilterBar(content);
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
      state.tagFilter
        ? `没有带「${state.tagFilter}」标签的待办`
        : state.taskFilter === "done"
          ? "完成的事情，会在这里"
          : "从一件想做的事开始",
      state.tagFilter
        ? "换一个标签，或再点一次标签取消筛选。"
        : state.taskFilter === "done"
          ? "完成待办之后，这里会留下你的进展。"
          : "给待办设置标签和计划日期，找到自己的节奏。",
      !state.tagFilter && writable() && state.taskFilter !== "done"
        ? "添加待办"
        : null,
      () => showEditor("task"),
    );
  card.append(list);
  content.append(card);
}
function renderNotes(content) {
  loadSpaceTags();
  toolbar(content, "写一条笔记", () => showEditor("note"));
  renderTagFilterBar(content);
  const grid = el("div", "notes-grid notes-full");
  const notes = currentItems("note").filter((item) =>
    itemMatchesTag(item, state.tagFilter),
  );
  if (notes.length) notes.forEach((item) => grid.append(noteCard(item)));
  else
    emptyState(
      grid,
      "note",
      state.tagFilter ? `没有带「${state.tagFilter}」标签的笔记` : "想法不必完整，先记下来",
      state.tagFilter
        ? "换一个标签，或再点一次标签取消筛选。"
        : "一句话、一段思考、一次语音，都能成为下一步的起点。",
      !state.tagFilter && writable() ? "留下第一条记录" : null,
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
    `每份文件最大 ${MAX_UPLOAD_MB} MB。可以将文件拖到此处，或通过语音录入保存录音。`,
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
    card.append(row, buildSpaceDataControls(space));
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
      "camera",
      "今日记录",
      "拍照或选图快速记下今天，视觉模型识别图片、总结摘要并提出待办。",
      "daily",
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
  if (state.area === "life") {
    const heading = el("div", "module-category-heading");
    heading.append(svg("leaf"), el("h2", "", "生活功能"));
    content.append(heading);
    renderBedtimeEntry(content);
  }
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
  // 模板库卡片：登记基于常用表单固化的可复用模板。
  const templateCard = el("section", "card module-card");
  const templateTop = el("div");
  const templateIcon = el("span", "module-icon");
  templateIcon.append(svg("note"));
  templateTop.append(templateIcon, el("span", "tag", "已可使用"));
  templateCard.append(
    templateTop,
    el("h3", "", "模板库"),
    el("p", "", "站会、周报、读书清单等常用任务与笔记，一键套用。"),
    button("管理模板", "text-button", showTemplateManagement),
  );
  grid.append(templateCard);
  content.append(grid);
  const note = el("div", "module-note");
  note.append(
    svg("puzzle"),
    el(
      "span",
      "",
      "按需添加，保持轻盈。当前版本包含图片速记、记录、资料、协作和密码箱；更多 AI 与日历工具将按实际需求继续接入。",
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
    const store = state.demoStore[sid];
    const index = store.findIndex((item) => item.id === id);
    const previous = index >= 0 ? store[index] : null;
    let savedItem = {
      ...data,
      id: id || `local-${randomUUID()}`,
      space_id: sid,
      created_at: data.created_at || previous?.created_at || new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    // 复刻服务端 apply_recurrence_rollover：仅当本次把状态改成 done 时滚动
    if (
      previous &&
      previous.status !== "done" &&
      savedItem.status === "done" &&
      savedItem.meta?.recurrence
    ) {
      savedItem = rolloverRecurrence(savedItem);
    }
    result = { item: savedItem };
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
    // 标签可能变化，使下次渲染重新聚合 /tags
    state.tagsLoadedSpace = null;
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
  state.tagsLoadedSpace = null;
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
// 标签芯片编辑器：回车或逗号添加、× 删除、最多 8 个、每个 ≤20 字
function buildTagEditor(initialTags) {
  const tags = [];
  for (const tag of initialTags || []) {
    const value = String(tag || "").trim();
    if (value && !tags.includes(value) && tags.length < TAG_LIMIT) tags.push(value);
  }
  const node = el("div", "tag-editor");
  const chips = el("div", "tag-chips");
  const tagInput = input("text", "回车或逗号添加标签，最多 8 个");
  tagInput.setAttribute("aria-label", "添加标签");
  tagInput.maxLength = TAG_MAX_LENGTH;
  function renderChips() {
    chips.replaceChildren();
    tags.forEach((tag, index) => {
      const chip = el("span", "tag-chip");
      chip.append(el("span", "tag-chip-name", tag));
      const remove = button("", "tag-chip-remove", () => {
        tags.splice(index, 1);
        tagInput.disabled = false;
        renderChips();
        tagInput.focus();
      }, "close");
      remove.setAttribute("aria-label", `移除标签 ${tag}`);
      chip.append(remove);
      chips.append(chip);
    });
  }
  function addTag(raw) {
    const value = String(raw || "").trim().replace(/^,+|,+$/g, "");
    if (!value) return;
    if (value.length > TAG_MAX_LENGTH) {
      toast(`单个标签不能超过 ${TAG_MAX_LENGTH} 个字`, true);
      return;
    }
    if (tags.includes(value)) return;
    if (tags.length >= TAG_LIMIT) {
      toast(`最多只能添加 ${TAG_LIMIT} 个标签`, true);
      return;
    }
    tags.push(value);
    tagInput.value = "";
    renderChips();
  }
  tagInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addTag(tagInput.value);
    } else if (event.key === "Backspace" && !tagInput.value && tags.length) {
      tags.pop();
      renderChips();
    }
  });
  tagInput.addEventListener("blur", () => addTag(tagInput.value));
  node.append(chips, tagInput);
  renderChips();
  return {
    node,
    focus: () => tagInput.focus(),
    getTags: () => [...tags],
    setDisabled: (disabled) => {
      tagInput.disabled = disabled;
      $$(".tag-chip-remove", chips).forEach((b) => (b.disabled = disabled));
    },
  };
}

// 重复规则编辑器：仅 task 使用；返回 getRecurrence()，无规则时为 null
function buildRecurrenceEditor(initialRec, getDue) {
  const initial = initialRec && initialRec.freq ? initialRec : null;
  const draft = initial
    ? {
        freq: initial.freq,
        interval: Number.isInteger(initial.interval) ? initial.interval : 1,
        byday: Array.isArray(initial.byday) ? [...initial.byday] : [],
        until: initial.until || "",
        count: Number.isInteger(initial.count) ? initial.count : "",
        endMode: initial.until ? "until" : initial.count ? "count" : "never",
      }
    : {
        freq: "none",
        interval: 1,
        byday: [],
        until: "",
        count: "",
        endMode: "never",
      };
  const node = el("div", "recur-editor");
  const freq = select(RECUR_FREQ_OPTIONS, draft.freq);
  freq.setAttribute("aria-label", "重复频率");
  const intervalWrap = el("div", "recur-field");
  const interval = input("number", "间隔", String(draft.interval));
  interval.setAttribute("aria-label", "重复间隔");
  interval.min = "1";
  interval.step = "1";
  intervalWrap.append(labelField("间隔（次）", interval));
  const weekdayWrap = el("div", "recur-field recur-weekdays");
  weekdayWrap.append(el("span", "recur-label", "重复在星期"));
  const weekdayGroup = el("div", "weekday-toggle-group");
  const weekdayButtons = new Map();
  RECUR_WEEKDAYS.forEach((day) => {
    const toggle = button(RECUR_WEEKDAY_CN[day].slice(1), "weekday-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-pressed", String(draft.byday.includes(day)));
    toggle.setAttribute("aria-label", RECUR_WEEKDAY_CN[day]);
    toggle.addEventListener("click", () => {
      const picked = new Set(draft.byday);
      if (picked.has(day)) picked.delete(day);
      else picked.add(day);
      draft.byday = RECUR_WEEKDAYS.filter((name) => picked.has(name));
      sync();
    });
    weekdayButtons.set(day, toggle);
    weekdayGroup.append(toggle);
  });
  weekdayWrap.append(weekdayGroup);
  const endWrap = el("div", "recur-field recur-end");
  endWrap.append(el("span", "recur-label", "结束条件"));
  const endGroup = el("div", "recur-end-options");
  const endModes = [
    ["never", "永不结束"],
    ["until", "到指定日期"],
    ["count", "固定次数"],
  ];
  const endRadios = [];
  endModes.forEach(([key, text]) => {
    const label = el("label", "recur-radio");
    const radio = input("radio");
    radio.name = "recur-end";
    radio.value = key;
    radio.checked = draft.endMode === key;
    radio.addEventListener("change", () => {
      draft.endMode = key;
      sync();
    });
    label.append(radio, document.createTextNode(text));
    endGroup.append(label);
    endRadios.push(radio);
  });
  const untilWrap = el("div", "recur-end-detail");
  const until = input("date", "", draft.until || "");
  until.setAttribute("aria-label", "重复截止日期");
  untilWrap.append(labelField("直到日期", until));
  const countWrap = el("div", "recur-end-detail");
  const count = input("number", "总次数", String(draft.count || ""));
  count.setAttribute("aria-label", "重复总次数");
  count.min = "1";
  count.step = "1";
  countWrap.append(labelField("共完成几次", count));
  endWrap.append(endGroup, untilWrap, countWrap);
  const caption = el("p", "recur-caption");
  caption.setAttribute("aria-live", "polite");
  node.append(
    labelField("重复", freq),
    intervalWrap,
    weekdayWrap,
    endWrap,
    caption,
  );
  function currentSignature() {
    return JSON.stringify([
      draft.freq,
      draft.interval,
      [...draft.byday].sort(),
      draft.endMode === "until" ? draft.until : "",
      draft.endMode === "count" ? draft.count : "",
    ]);
  }
  const initialSignature = currentSignature();
  function sync() {
    const enabled = draft.freq !== "none";
    intervalWrap.hidden = !enabled;
    weekdayWrap.hidden = !(enabled && draft.freq === "weekly");
    endWrap.hidden = !enabled;
    untilWrap.hidden = draft.endMode !== "until";
    countWrap.hidden = draft.endMode !== "count";
    weekdayButtons.forEach((toggle, day) =>
      toggle.setAttribute("aria-pressed", String(draft.byday.includes(day))),
    );
    endRadios.forEach((radio) => (radio.checked = radio.value === draft.endMode));
    const preview = enabled ? describeRecurrence(buildValue(false), getDue()) : "";
    caption.textContent = preview ? `将按「${preview}」重复` : "";
  }
  function buildValue(keepState) {
    if (draft.freq === "none") return null;
    const value = { freq: draft.freq };
    const intervalValue = Number.parseInt(interval.value, 10);
    if (Number.isInteger(intervalValue) && intervalValue > 1)
      value.interval = intervalValue;
    if (draft.freq === "weekly" && draft.byday.length)
      value.byday = [...draft.byday];
    if (draft.endMode === "until" && until.value) value.until = until.value;
    if (draft.endMode === "count") {
      const countValue = Number.parseInt(count.value, 10);
      if (Number.isInteger(countValue) && countValue >= 1)
        value.count = countValue;
    }
    if (keepState && initial) {
      if (Number.isInteger(initial.completed)) value.completed = initial.completed;
      if (initial.exhausted === true) value.exhausted = true;
    }
    return value;
  }
  freq.addEventListener("change", () => {
    draft.freq = freq.value;
    sync();
  });
  until.addEventListener("change", () => {
    draft.until = until.value;
    sync();
  });
  count.addEventListener("input", () => {
    draft.count = count.value;
    sync();
  });
  interval.addEventListener("input", () => {
    draft.interval = Number.parseInt(interval.value, 10) || 1;
    sync();
  });
  sync();
  return {
    node,
    refresh: sync,
    // 规则字段被修改过就视为新规则，不再继承 completed/exhausted
    getRecurrence: () =>
      buildValue(currentSignature() === initialSignature),
  };
}

// 子任务编辑器：勾选框 + 文本 + 删除，最多 20 项
function buildSubtaskEditor(initialSubtasks) {
  const rows = (Array.isArray(initialSubtasks) ? initialSubtasks : [])
    .filter((sub) => sub && typeof sub.title === "string")
    .slice(0, SUBTASK_LIMIT)
    .map((sub) => ({
      id: /^[a-f0-9]{32}$/.test(sub.id) ? sub.id : makeSubtaskId(),
      title: sub.title.slice(0, 120),
      done: sub.done === true,
    }));
  const node = el("div", "subtask-editor");
  const list = el("div", "subtask-edit-list");
  const add = button("添加子任务", "text-button", () => {
    if (rows.length >= SUBTASK_LIMIT) {
      toast(`最多只能添加 ${SUBTASK_LIMIT} 个子任务`, true);
      return;
    }
    rows.push({ id: makeSubtaskId(), title: "", done: false });
    renderRows(true);
  }, "plus");
  add.setAttribute("aria-label", "添加子任务");
  function renderRows(focusNew) {
    list.replaceChildren();
    rows.forEach((row, index) => {
      const line = el("div", "subtask-edit-row");
      const check = input("checkbox");
      check.checked = row.done;
      check.setAttribute("aria-label", `标记子任务 ${row.title || "新条目"} 完成状态`);
      check.addEventListener("change", () => {
        row.done = check.checked;
      });
      const text = input("text", "子任务内容（1-120 字）", row.title);
      text.maxLength = 120;
      text.setAttribute("aria-label", `子任务 ${index + 1} 内容`);
      text.addEventListener("input", () => {
        row.title = text.value;
      });
      const remove = iconButton("trash", "删除子任务", () => {
        rows.splice(index, 1);
        renderRows();
      });
      line.append(check, text, remove);
      list.append(line);
      if (focusNew && index === rows.length - 1) text.focus();
    });
    add.disabled = rows.length >= SUBTASK_LIMIT;
  }
  node.append(list, add);
  renderRows();
  return {
    node,
    getSubtasks: () =>
      rows
        .filter((row) => row.title.trim())
        .map((row) => ({ id: row.id, title: row.title.trim(), done: row.done })),
    setDisabled: (disabled) => {
      add.disabled = disabled || rows.length >= SUBTASK_LIMIT;
      $$("input,button", list).forEach((control) => (control.disabled = disabled));
    },
  };
}

function showEditor(kind, item, prefill = {}) {
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
        item?.title || prefill.title || "",
      );
      title.required = true;
      title.maxLength = 200;
      const text = el("textarea");
      text.placeholder =
        kind === "task" ? "补充说明、要点或参考，支持 Markdown…" : "一个想法，也值得留下来，支持 Markdown。";
      text.value = item?.body || prefill.body || "";
      text.maxLength = 200000;
      form.append(
        labelField("标题", title),
        labelField(kind === "task" ? "补充说明" : "正文", text),
      );
      // 详情区：笔记始终给 Markdown 预览；任务仅在正文含 Markdown 标记时出现
      const previewWrap = el("div", "markdown-preview-wrap");
      const preview = el("div", "markdown-body");
      previewWrap.append(el("span", "recur-label", "Markdown 预览"), preview);
      previewWrap.hidden = kind === "task" && !hasMarkdownMarkup(text.value);
      renderMarkdownInto(preview, text.value);
      text.addEventListener("input", () => {
        if (kind === "task") previewWrap.hidden = !hasMarkdownMarkup(text.value);
        renderMarkdownInto(preview, text.value);
      });
      form.append(previewWrap);
      const meta = el("div", "editor-meta");
      const area = select(
        [
          ["work", "工作"],
          ["life", "生活"],
        ],
        item?.area || prefill.area || state.area,
      );
      const tagEditor = buildTagEditor(itemTags(item, prefill.meta));
      meta.append(labelField("所属分区", area));
      const tagField = labelField("标签（最多 8 个，每个 ≤20 字）", tagEditor.node);
      tagField.classList.add("editor-meta-wide");
      meta.append(tagField);
      let due, priority, status, recurEditor, subtaskEditor, startTime, endTime;
      if (kind === "task") {
        const prefillMeta = prefill.meta || {};
        due = input("date", "", item?.meta?.due || prefillMeta.due || prefill.due || "");
        priority = select(
          [
            ["normal", "普通"],
            ["high", "优先"],
          ],
          item?.meta?.priority || prefillMeta.priority || "normal",
        );
        status = select(
          [
            ["active", "待办"],
            ["done", "已完成"],
            ["dropped", "已删除"],
          ],
          normalizeTaskStatus(item?.status),
        );
        // 可选时间段：为空表示「全天」，日程视图据此显示时间范围。
        startTime = input("time", "", item?.meta?.startTime || prefillMeta.startTime || "");
        endTime = input("time", "", item?.meta?.endTime || prefillMeta.endTime || "");
        meta.append(
          labelField("计划日期", due),
          labelField("优先级", priority),
          labelField("当前状态", status),
          labelField("开始时间（可选）", startTime),
          labelField("结束时间（可选）", endTime),
        );
        due.addEventListener("change", () => recurEditor?.refresh());
        recurEditor = buildRecurrenceEditor(
          item?.meta?.recurrence || prefillMeta.recurrence,
          () => due.value,
        );
        const recurField = labelField("重复设置", recurEditor.node);
        recurField.classList.add("editor-meta-wide");
        const existingRec = item?.meta?.recurrence || prefillMeta.recurrence;
        if (existingRec?.freq) {
          const progress = el(
            "p",
            "recur-progress",
            existingRec.exhausted
              ? "该重复任务已结束。"
              : `已完成 ${Number.isInteger(existingRec.completed) ? existingRec.completed : 0} 次。`,
          );
          recurEditor.node.append(progress);
        }
        meta.append(recurField);
        subtaskEditor = buildSubtaskEditor(
          item?.meta?.subtasks || prefillMeta.subtasks,
        );
        const subtaskField = labelField("子任务（最多 20 项）", subtaskEditor.node);
        subtaskField.classList.add("editor-meta-wide");
        meta.append(subtaskField);
      }
      form.append(meta);
      const error = el("p", "form-error");
      error.setAttribute("role", "alert");
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
      // 新建记录时提供「保存为模板」，把当前表单固化为可复用模板。
      if (!item && canEdit) {
        buttons.push(
          button(
            "保存为模板",
            "button soft compact template-save",
            async () => {
              const nextMeta = { ...(item?.meta || {}) };
              nextMeta.tags = tagEditor.getTags();
              if (kind === "task") {
                nextMeta.due = due.value;
                nextMeta.priority = priority.value;
                nextMeta.recurrence = recurEditor?.getRecurrence() || undefined;
                const subtasks = subtaskEditor?.getSubtasks() || [];
                if (subtasks.length) nextMeta.subtasks = subtasks;
              }
              await saveCurrentAsTemplate(kind, {
                title: title.value.trim(),
                body: text.value,
                area: area.value,
                meta: nextMeta,
              });
            },
          ),
        );
      }
      form.append(error, actionsRow(...buttons));
      if (!canEdit) {
        $$("input,textarea,select,button", form).forEach(
          (control) => (control.disabled = true),
        );
        tagEditor.setDisabled(true);
        subtaskEditor?.setDisabled(true);
      }
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!canEdit) return;
        save.disabled = true;
        error.textContent = "";
        try {
          const nextMeta = { ...(item?.meta || {}) };
          nextMeta.tags = tagEditor.getTags();
          const fields = {
            kind,
            area: area.value,
            title: title.value.trim(),
            body: text.value,
            status: status?.value || item?.status || "active",
            meta: nextMeta,
          };
          if (kind === "task") {
            nextMeta.due = due.value;
            nextMeta.priority = priority.value;
            // 时间段：两端都为空表示全天；同时给出时要求开始早于结束。
            const startValue = startTime.value;
            const endValue = endTime.value;
            if (startValue && endValue && startValue >= endValue) {
              error.textContent = "结束时间需要晚于开始时间";
              save.disabled = false;
              return;
            }
            if (startValue) nextMeta.startTime = startValue;
            else delete nextMeta.startTime;
            if (endValue) nextMeta.endTime = endValue;
            else delete nextMeta.endTime;
            const recurrence = recurEditor.getRecurrence();
            if (recurrence) nextMeta.recurrence = recurrence;
            else delete nextMeta.recurrence;
            const subtasks = subtaskEditor.getSubtasks();
            if (subtasks.length) nextMeta.subtasks = subtasks;
            else delete nextMeta.subtasks;
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
      ["camera", "拍照记一笔", "拍照或选图，AI 识别总结", () => navigate("daily")],
      ["task", "待办事项", "记录接下来要做的事", () => showEditor("task")],
      ["note", "灵感笔记", "留住此刻的想法", () => showEditor("note")],
      ["mic", "语音录入", "说下来，保存为录音", showVoice],
      ["upload", "上传资料", "图片、视频、音频与文档", chooseFiles],
    ]) {
      const b = button("", "new-option", action);
      b.append(svg(iconName), el("strong", "", title), el("small", "", text));
      options.append(b);
    }
    const fromTemplate = button("", "new-option new-option-template", () =>
      showTemplatePicker(),
    );
    fromTemplate.append(
      svg("note"),
      el("strong", "", "从模板新建"),
      el("small", "", "站会、周报、读书清单…"),
    );
    options.append(fromTemplate);
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
      if (file.size < 1 || file.size > MAX_UPLOAD_MB * 1024 * 1024)
        throw new Error(`「${file.name}」须在 1 字节到 ${MAX_UPLOAD_MB} MB 之间。`);
      let item;
      if (state.demo) {
        const url = URL.createObjectURL(file);
        state.objectURLs.add(url);
        item = {
          id: `local-${randomUUID()}`,
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
        // 禁掉脚本等全部权限；blob:/同源 PDF 需保留同源身份才能被内置查看器读取，
        // 因此只放开 allow-same-origin（不放开 allow-scripts）
        frame.setAttribute("sandbox", "allow-same-origin");
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
            id: `demo-${randomUUID()}`,
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
// 演示模式：在本地 demoStore 与 demoDaily 内跨空间做与后端同名字段的过滤
function demoSearchAllSpaces(term) {
  const keyword = term.toLocaleLowerCase();
  const results = [];
  for (const space of state.spaces) {
    for (const item of state.demoStore[space.id] || []) {
      const haystack = [
        item.title || "",
        item.body || "",
        ...itemTags(item),
      ]
        .join("\n")
        .toLocaleLowerCase();
      if (!haystack.includes(keyword)) continue;
      const snippetSource =
        (item.title || "").toLocaleLowerCase().includes(keyword)
          ? item.title
          : item.body || itemTags(item)[0] || item.title;
      results.push({
        type: "item",
        spaceId: space.id,
        spaceName: space.name,
        area: item.area,
        id: item.id,
        title: item.title,
        snippet: (snippetSource || "").slice(0, 80),
        due: item.meta?.due || null,
        status: item.status,
        updatedAt: item.updated_at || item.created_at,
      });
    }
    for (const entry of state.demoDaily[space.id] || []) {
      const haystack = [entry.title || "", entry.summary || "", entry.body || ""]
        .join("\n")
        .toLocaleLowerCase();
      if (!haystack.includes(keyword)) continue;
      results.push({
        type: "daily",
        spaceId: space.id,
        spaceName: space.name,
        area: entry.area,
        id: entry.id,
        title: entry.title,
        snippet: (entry.body || entry.summary || entry.title || "").slice(0, 80),
        due: null,
        status: entry.status || "active",
        updatedAt: entry.updatedAt || entry.createdAt,
      });
    }
  }
  return results.sort((a, b) =>
    String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")),
  );
}
// 统一入口：⌘K、顶栏搜索按钮与侧边导航都打开同一个跨空间搜索视图
function openSearchView() {
  if (!state.user) return;
  state.view = "search";
  state.search.error = "";
  closeModal();
  closeDrawer(false);
  render();
  window.setTimeout(() => {
    const inputNode = $("#search-view-input");
    if (inputNode && state.view === "search") inputNode.focus();
  }, 60);
}
async function runSearch(term) {
  const keyword = term.trim();
  const token = ++state.search.request;
  state.search.query = keyword;
  if (!keyword) {
    state.search.results = [];
    state.search.queried = "";
    state.search.loading = false;
    state.search.error = "";
    render();
    return;
  }
  state.search.loading = true;
  state.search.error = "";
  render();
  try {
    let results;
    if (state.demo) results = demoSearchAllSpaces(keyword);
    else {
      const result = await api(
        `/api/search?q=${encodeURIComponent(keyword)}`,
      );
      results = Array.isArray(result.results) ? result.results : [];
    }
    if (token !== state.search.request) return;
    state.search.results = results;
    state.search.queried = keyword;
  } catch (err) {
    if (token !== state.search.request) return;
    state.search.results = [];
    state.search.queried = keyword;
    state.search.error = errorMessage(err);
    toast(errorMessage(err), true);
  } finally {
    if (token === state.search.request) state.search.loading = false;
    render();
  }
}
async function openSearchResult(result) {
  if (result.spaceId && result.spaceId !== state.space?.id) {
    const target = state.spaces.find((space) => space.id === result.spaceId);
    if (!target) {
      toast("无法打开该空间的记录", true);
      return;
    }
    await switchSpace(result.spaceId);
  }
  if (result.type === "daily") {
    state.area = result.area === "life" ? "life" : "work";
    navigate("daily");
    return;
  }
  const found = state.items.find((item) => item.id === result.id);
  if (!found) {
    toast("记录可能已被移动或删除", true);
    return;
  }
  showEditor(found.kind, found);
}
function renderSearch(content) {
  const card = el("section", "card search-view-card");
  const searchInput = input("search", "输入关键词，搜索所有你加入的空间");
  searchInput.id = "search-view-input";
  searchInput.value = state.search.query;
  searchInput.setAttribute("aria-label", "跨空间搜索关键词");
  let debounce = 0;
  searchInput.addEventListener("input", () => {
    window.clearTimeout(debounce);
    const value = searchInput.value;
    debounce = window.setTimeout(() => runSearch(value), 300);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      window.clearTimeout(debounce);
      runSearch(searchInput.value);
    }
  });
  card.append(searchInput);
  card.append(
    el(
      "p",
      "helper",
      "覆盖你加入的所有空间内的待办、笔记与今日记录；私密密码箱不参与搜索。",
    ),
  );
  const resultWrap = el("div", "search-view-results");
  const keyword = state.search.query.trim();
  if (!keyword) {
    emptyState(
      resultWrap,
      "search",
      "输入关键词，跨空间找记录",
      "回车或稍等片刻自动搜索，结果会按空间分组展示。",
    );
  } else if (state.search.loading) {
    const row = el("div", "loading-panel");
    row.append(el("span", "spinner"), el("span", "", "正在跨空间搜索…"));
    resultWrap.append(row);
  } else if (state.search.error) {
    const error = el("p", "form-error", state.search.error);
    error.setAttribute("role", "alert");
    resultWrap.append(error);
  } else if (!state.search.results.length) {
    emptyState(
      resultWrap,
      "search",
      `没有找到与「${state.search.queried}」相关的记录`,
      "试试更短或其他关键词。密码箱内容不会出现在搜索结果里。",
    );
  } else {
    const groups = new Map();
    for (const result of state.search.results) {
      if (!groups.has(result.spaceId))
        groups.set(result.spaceId, { name: result.spaceName, list: [] });
      groups.get(result.spaceId).list.push(result);
    }
    for (const [spaceId, group] of groups) {
      const section = el("section", "search-space-group");
      const heading = el("h3", "search-space-title", group.name);
      heading.append(
        el("span", "search-space-count", `${group.list.length} 条`),
      );
      section.append(heading);
      group.list.forEach((result) => {
        const row = button("", "search-result", () => openSearchResult(result));
        const iconName =
          result.type === "daily"
            ? "camera"
            : result.due
              ? "task"
              : "note";
        const info = el("span");
        const metaLine = [
          result.area === "life" ? "生活" : "工作",
          result.type === "daily" ? "今日记录" : "条目",
          result.due ? `计划 ${result.due}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        info.append(
          el("strong", "", result.title),
          el("small", "", metaLine),
          result.snippet ? el("small", "search-snippet", result.snippet) : null,
        );
        row.append(svg(iconName), info, svg("arrow", true));
        section.append(row);
      });
      resultWrap.append(section);
    }
  }
  card.append(resultWrap);
  content.append(card);
}
// ---------------------------------------------------------------------------
// 月历视图：周一到周日的当月月格，重复任务展示未来 3 次到期
// ---------------------------------------------------------------------------
function calendarMonthLabel(monthValue) {
  const [year, month] = monthValue.split("-").map(Number);
  return `${year} 年 ${month} 月`;
}
function shiftMonth(monthValue, delta) {
  const [year, month] = monthValue.split("-").map(Number);
  const date = new Date(year, month - 1 + delta, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}
// ---------------------------------------------------------------------------
// 农历换算：纯前端、零网络。使用 1900–2100 年的压缩编码表
// （每年一个整数，记录大小月与闰月；春节基准日另行给出）。
// ---------------------------------------------------------------------------
const LUNAR_INFO = [
  0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,
  0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,
  0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,
  0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,
  0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,
  0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,
  0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,
  0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,
  0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,
  0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x05ac0, 0x0ab60, 0x096d5, 0x092e0,
  0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,
  0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,
  0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,
  0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,
  0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,
];
const LUNAR_MONTH_CN = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"];
const LUNAR_DAY_CN = [
  "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
  "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
  "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
];
// 每年春节对应的公历日期基准（与 LUNAR_INFO 同年序一一对应）。
const LUNAR_BASE_YEAR = 1900;
const LUNAR_BASE_DATE = Date.UTC(1900, 0, 31);

function lunarLeapMonth(year) {
  return LUNAR_INFO[year - LUNAR_BASE_YEAR] & 0xf;
}
function lunarLeapDays(year) {
  if (!lunarLeapMonth(year)) return 0;
  return LUNAR_INFO[year - LUNAR_BASE_YEAR] & 0x10000 ? 30 : 29;
}
function lunarMonthDays(year, month) {
  return LUNAR_INFO[year - LUNAR_BASE_YEAR] & (0x10000 >> month) ? 30 : 29;
}
function lunarYearDays(year) {
  let sum = 348;
  for (let mask = 0x8000; mask > 0x8; mask >>= 1) {
    if (LUNAR_INFO[year - LUNAR_BASE_YEAR] & mask) sum += 1;
  }
  return sum + lunarLeapDays(year);
}
// 返回形如「农历九月初三」的文本；超出数据范围时返回空串。
function lunarText(dateString) {
  const [y, m, d] = dateString.split("-").map(Number);
  const target = Date.UTC(y, m - 1, d);
  let offset = Math.floor((target - LUNAR_BASE_DATE) / 86400000);
  if (!Number.isFinite(offset) || offset < 0) return "";
  let year = LUNAR_BASE_YEAR;
  const maxYear = LUNAR_BASE_YEAR + LUNAR_INFO.length - 1;
  while (year <= maxYear) {
    const days = lunarYearDays(year);
    if (offset < days) break;
    offset -= days;
    year += 1;
  }
  if (year > maxYear) return "";
  const leap = lunarLeapMonth(year);
  let month = 1;
  let isLeap = false;
  while (month <= 12) {
    const days = isLeap ? lunarLeapDays(year) : lunarMonthDays(year, month);
    if (offset < days) break;
    offset -= days;
    if (leap === month && !isLeap) isLeap = true;
    else {
      isLeap = false;
      month += 1;
    }
  }
  const day = offset + 1;
  if (month < 1 || month > 12 || day < 1 || day > 30) return "";
  return `农历${isLeap ? "闰" : ""}${LUNAR_MONTH_CN[month - 1]}月${LUNAR_DAY_CN[day - 1]}`;
}
function weekdayCn(dateString) {
  const [y, m, d] = dateString.split("-").map(Number);
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][
    new Date(y, m - 1, d).getDay()
  ];
}
function addDays(dateString, delta) {
  const [y, m, d] = dateString.split("-").map(Number);
  const next = new Date(y, m - 1, d + delta);
  return isoDateString(next);
}
// 任务的时间段文案：有开始/结束显示区间，否则「全天」。
function taskTimeLabel(item) {
  const start = item.meta?.startTime;
  const end = item.meta?.endTime;
  if (start && end) return `${start}-${end}`;
  if (start) return `${start} 起`;
  return "全天";
}
// 某一天的日程条目：任务（含重复任务落点）+ 今日记录。
function agendaEntriesFor(dateString) {
  const tasks = [];
  currentItems("task").forEach((item) => {
    if (!item.meta?.due) return;
    if (nextOccurrences(item, 3).includes(dateString)) tasks.push(item);
  });
  tasks.sort((a, b) => {
    const sa = a.meta?.startTime || "99:99";
    const sb = b.meta?.startTime || "99:99";
    return sa.localeCompare(sb);
  });
  const entries = [];
  for (const entry of state.daily.entries || []) {
    // 接口返回 camelCase 的 entryDate；兼容 snake_case 以防内部结构变化。
    const entryDate = entry.entryDate || entry.entry_date;
    if (entryDate === dateString) entries.push(entry);
  }
  return { tasks, daily: entries };
}
function renderAgenda(content) {
  const today = dailyDateString();
  const state_ = state.calendar;
  if (!state_.agendaFrom) state_.agendaFrom = addDays(today, -7);
  if (!state_.agendaDays) state_.agendaDays = 37;
  const from = state_.agendaFrom;
  const days = state_.agendaDays;
  const card = el("section", "card agenda-card");
  const nav = el("div", "calendar-nav");
  nav.append(
    button("更早", "button secondary calendar-nav-btn", () => {
      state_.agendaFrom = addDays(from, -14);
      state_.agendaDays += 14;
      render();
    }),
    el("h2", "calendar-title", "日程"),
    button("回到本周", "button secondary calendar-nav-btn", () => {
      state_.agendaFrom = addDays(today, -7);
      state_.agendaDays = 37;
      render();
    }),
    button("更晚", "button secondary calendar-nav-btn", () => {
      state_.agendaDays += 14;
      render();
    }),
  );
  card.append(nav);
  const list = el("div", "agenda-list");
  let renderedGroups = 0;
  for (let index = 0; index < days; index++) {
    const dateString = addDays(from, index);
    const { tasks, daily } = agendaEntriesFor(dateString);
    if (!tasks.length && !daily.length && dateString !== today) continue;
    renderedGroups += 1;
    const group = el("section", "agenda-group");
    const head = el("div", "agenda-head");
    const [y, m, d] = dateString.split("-").map(Number);
    head.append(
      el("div", "agenda-date", `${m}月${d}日 ${weekdayCn(dateString)}`),
      el("div", "agenda-lunar", lunarText(dateString)),
    );
    if (dateString === today) head.append(el("span", "agenda-today", "今天"));
    group.append(head);
    const body = el("div", "agenda-items");
    if (!tasks.length && !daily.length) {
      body.append(el("p", "agenda-empty", "暂无日程安排"));
      // 今天没有安排也允许快速新建
      if (writable())
        body.append(
          button("新建这一天的待办", "text-button", () =>
            showEditor("task", null, { due: dateString }),
          ),
        );
    }
    tasks.forEach((item) => {
      const row = button("", "agenda-item", () => showEditor("task", item));
      row.type = "button";
      if (item.status === "done") row.classList.add("is-done");
      const time = el("span", "agenda-time", taskTimeLabel(item));
      const title = el("strong", "agenda-title", item.title);
      row.append(time, title);
      if (item.meta?.recurrence?.freq)
        row.append(el("span", "agenda-badge", "重复"));
      body.append(row);
    });
    daily.forEach((entry) => {
      const row = button("", "agenda-item agenda-daily", () => showDailyEntry(entry));
      row.type = "button";
      row.append(
        el("span", "agenda-time", "记录"),
        el("strong", "agenda-title", describeEntry("daily", entry, 80)),
      );
      body.append(row);
    });
    group.append(body);
    list.append(group);
  }
  if (!renderedGroups) {
    emptyState(
      list,
      "calendar",
      "这段时间没有日程",
      "点击「更早」或「更晚」查看其他日期。",
    );
  }
  card.append(list);
  content.append(card);
}
function renderCalendar(content) {
  // 顶部模式切换：日历（月视图）/ 日程（时间线）
  const switcher = el("div", "calendar-mode-switch");
  const mode = state.calendar.mode === "agenda" ? "agenda" : "month";
  for (const [value, label] of [
    ["month", "日历"],
    ["agenda", "日程"],
  ]) {
    const btn = button(label, `calendar-mode-btn${mode === value ? " active" : ""}`, () => {
      state.calendar.mode = value;
      render();
    });
    btn.setAttribute("aria-pressed", String(mode === value));
    switcher.append(btn);
  }
  content.append(switcher);
  if (mode === "agenda") {
    // 日程需要「今日记录」数据；若当前空间尚未加载过，触发一次加载
    // （loadDailyEntries 自带重入守卫，不会递归）。
    if (state.space && state.daily.loadedSpace !== state.space.id && !state.daily.loading) {
      loadDailyEntries();
    }
    renderAgenda(content);
    return;
  }
  if (!state.calendar.month)
    state.calendar.month = dailyDateString().slice(0, 7);
  const monthValue = state.calendar.month;
  const [year, month] = monthValue.split("-").map(Number);
  const firstDay = new Date(year, month - 1, 1);
  const offset = weekdayIndex(firstDay);
  const gridStart = new Date(year, month - 1, 1 - offset);
  const today = dailyDateString();
  // 收集月格内每个日期对应的任务
  const byDate = new Map();
  currentItems("task").forEach((item) => {
    if (!item.meta?.due) return;
    nextOccurrences(item, 3).forEach((dateString) => {
      if (!dateString.startsWith(monthValue)) return;
      if (!byDate.has(dateString)) byDate.set(dateString, []);
      byDate.get(dateString).push(item);
    });
  });
  const card = el("section", "card calendar-card");
  const nav = el("div", "calendar-nav");
  nav.append(
    button("上一月", "button secondary calendar-nav-btn", () => {
      state.calendar.month = shiftMonth(monthValue, -1);
      render();
    }, "arrow"),
    el("h2", "calendar-title", calendarMonthLabel(monthValue)),
    button("今天", "button secondary calendar-nav-btn", () => {
      state.calendar.month = today.slice(0, 7);
      state.calendar.filter = null;
      render();
    }),
    button("下一月", "button secondary calendar-nav-btn", () => {
      state.calendar.month = shiftMonth(monthValue, 1);
      render();
    }, "arrow"),
  );
  // 上一月按钮的箭头反向
  nav.firstChild.querySelector(".icon")?.style.setProperty("transform", "rotate(180deg)");
  card.append(nav);
  const weekdayHeader = el("div", "calendar-weekdays");
  ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].forEach((name) =>
    weekdayHeader.append(el("span", "", name)),
  );
  card.append(weekdayHeader);
  const grid = el("div", "calendar-grid");
  for (let index = 0; index < 42; index++) {
    const cellDate = new Date(
      gridStart.getFullYear(),
      gridStart.getMonth(),
      gridStart.getDate() + index,
    );
    const dateString = isoDateString(cellDate);
    const inMonth = cellDate.getMonth() === month - 1;
    const isToday = dateString === today;
    const isPicked = state.calendar.filter === dateString;
    const cell = el(
      "div",
      `calendar-cell${inMonth ? "" : " muted"}${isToday ? " today" : ""}${
        isPicked ? " picked" : ""
      }`,
    );
    cell.tabIndex = 0;
    cell.setAttribute("role", "button");
    cell.setAttribute(
      "aria-label",
      `${dateString}${isToday ? "（今天）" : ""}，点击新建或筛选任务`,
    );
    const dayNumber = el("span", "calendar-day", String(cellDate.getDate()));
    cell.append(dayNumber);
    const entries = byDate.get(dateString) || [];
    const entryWrap = el("div", "calendar-entries");
    entries.slice(0, 3).forEach((item) => {
      const entry = button("", "calendar-entry", (event) => {
        event.stopPropagation();
        showEditor("task", item);
      });
      if (item.status === "done") entry.classList.add("is-done");
      if (item.meta?.recurrence?.freq) entry.classList.add("is-recurring");
      entry.append(el("span", "calendar-entry-dot"), el("span", "", item.title));
      entryWrap.append(entry);
    });
    if (entries.length > 3)
      entryWrap.append(el("span", "calendar-more", `+${entries.length - 3}`));
    cell.append(entryWrap);
    const activate = () => {
      state.calendar.filter = isPicked ? null : dateString;
      render();
    };
    cell.addEventListener("click", activate);
    cell.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
    grid.append(cell);
  }
  card.append(grid);
  content.append(card);
  if (state.calendar.filter) {
    const detail = el("section", "card calendar-day-detail");
    const head = el("div", "calendar-day-head");
    head.append(
      el("h2", "", `${state.calendar.filter} 的安排`),
      writable()
        ? button("新建这一天的待办", "button primary", () => {
            showEditor("task", null, { due: state.calendar.filter });
          }, "plus")
        : el("span", "readonly-pill", "这个空间为只读"),
    );
    detail.append(head);
    const dayTasks = byDate.get(state.calendar.filter) || [];
    const list = el("div", "task-list");
    if (dayTasks.length) dayTasks.forEach((item) => list.append(taskRow(item)));
    else
      emptyState(
        list,
        "calendar",
        "这一天还没有安排",
        "新建一件待办，或点击其他日期查看。",
        writable() ? "新建待办" : null,
        () => showEditor("task", null, { due: state.calendar.filter }),
      );
    detail.append(list);
    content.append(detail);
  }
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
        button("账号设置", "button soft", showAccountSettings, "settings"),
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

// 数据与账号设置中心：昵称、密码与注销账号
function showAccountSettings() {
  showModal(
    "数据与账号",
    (body) => {
      body.append(
        buildNicknameSection(),
        buildIdentitySection(),
        buildPasswordSection(),
        buildWebhookSection(),
        buildAccountDangerZone(),
      );
    },
    "账号设置",
  );
}

// ---------------------------------------------------------------------------
// 出站提醒（Webhook）：列表 / 添加 / 启停 / 测试 / 轮转 secret / 删除。
// secret 仅在创建与轮转的响应里出现一次，前端用一次性提示条展示。
// ---------------------------------------------------------------------------
function webhookHost(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
function buildWebhookSection() {
  const section = el("section", "account-section webhook-section");
  section.append(el("h3", "", "出站提醒（Webhook）"));
  section.append(
    el(
      "p",
      "helper",
      "任务到期时，除了站内通知，还可以推送到你自己的 Webhook（如 Bark、Server酱、企业微信、飞书的自定义机器人）。仅允许公网 https 地址，内网与保留地址会被拒绝。",
    ),
  );
  if (state.demo) {
    section.append(el("p", "helper", "演示模式不连接账号服务，Webhook 需在登录后配置。"));
    return section;
  }
  const listWrap = el("div", "webhook-list");
  const secretBox = el("div", "webhook-secret");
  secretBox.hidden = true;
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  const showSecret = (secret, note) => {
    secretBox.hidden = false;
    secretBox.replaceChildren();
    const code = el("code", "webhook-secret-value", secret);
    const tip = el(
      "p",
      "helper",
      `${note}这是签名密钥，只显示这一次，请立即复制保存，之后无法再次查看。`,
    );
    const copy = button("复制密钥", "button soft compact", async () => {
      try {
        await navigator.clipboard.writeText(secret);
        toast("密钥已复制");
      } catch {
        toast("复制失败，请手动选择密钥复制", true);
      }
    });
    secretBox.append(code, tip, copy);
  };
  const refresh = async () => {
    listWrap.replaceChildren();
    let hooks;
    try {
      const result = await api("/api/webhooks");
      hooks = Array.isArray(result.webhooks) ? result.webhooks : [];
    } catch (err) {
      error.textContent = errorMessage(err);
      return;
    }
    error.textContent = "";
    if (!hooks.length) {
      listWrap.append(el("p", "helper", "还没有配置 Webhook。"));
      return;
    }
    for (const hook of hooks) {
      const row = el("div", "webhook-row");
      const head = el("div", "webhook-row-head");
      head.append(
        el("strong", "", hook.name || "Webhook"),
        el("small", "", webhookHost(hook.url)),
      );
      const toggle = el("button", `switch${hook.enabled ? " on" : ""}`);
      toggle.type = "button";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", String(!!hook.enabled));
      toggle.setAttribute("aria-label", hook.enabled ? "停用" : "启用");
      toggle.append(el("span"));
      toggle.addEventListener("click", async () => {
        try {
          await api(`/api/webhooks/${hook.id}`, "PATCH", { enabled: !hook.enabled });
          await refresh();
        } catch (err) {
          toast(errorMessage(err), true);
        }
      });
      head.append(toggle);
      const actions = el("div", "webhook-row-actions");
      const testBtn = button("测试", "button soft compact", async () => {
        testBtn.disabled = true;
        try {
          const result = await api(`/api/webhooks/${hook.id}/test`, "POST");
          if (result.ok) toast(`测试成功，对方返回 ${result.status}`);
          else toast(result.error || "测试失败", true);
        } catch (err) {
          toast(errorMessage(err), true);
        } finally {
          testBtn.disabled = false;
        }
      });
      const rotateBtn = button("轮转密钥", "button soft compact", async () => {
        try {
          const result = await api(`/api/webhooks/${hook.id}/rotate-secret`, "POST");
          if (result.webhook?.secret) showSecret(result.webhook.secret, "已生成新的签名密钥。");
        } catch (err) {
          toast(errorMessage(err), true);
        }
      });
      const delBtn = button("删除", "button soft compact danger", () => {
        showModal("删除这个 Webhook？", (b2) => {
          b2.append(el("p", "helper", `「${hook.name || "Webhook"}」删除后不再接收提醒推送。`));
          b2.append(
            actionsRow(
              button("确认删除", "button danger", async () => {
                try {
                  await api(`/api/webhooks/${hook.id}`, "DELETE");
                  closeModal();
                  await refresh();
                  toast("已删除 Webhook");
                } catch (err) {
                  closeModal();
                  toast(errorMessage(err), true);
                }
              }),
            ),
          );
        });
      });
      actions.append(testBtn, rotateBtn, delBtn);
      row.append(head, actions);
      listWrap.append(row);
    }
  };
  // 添加表单
  const form = el("form", "webhook-form");
  const nameInput = input("text", "名称，如 我的 Bark", "");
  nameInput.maxLength = 60;
  const urlInput = input("url", "https://… 的 Webhook 地址", "");
  urlInput.maxLength = 300;
  urlInput.autocapitalize = "none";
  urlInput.spellcheck = false;
  const submit = el("button", "button primary", "添加 Webhook");
  submit.type = "submit";
  form.append(
    labelField("名称", nameInput),
    labelField("地址（公网 https）", urlInput),
    actionsRow(submit),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    try {
      const result = await api("/api/webhooks", "POST", {
        name: nameInput.value.trim(),
        url: urlInput.value.trim(),
        events: ["task.due"],
      });
      nameInput.value = "";
      urlInput.value = "";
      if (result.webhook?.secret) showSecret(result.webhook.secret, "Webhook 已创建。");
      await refresh();
    } catch (err) {
      error.textContent = errorMessage(err);
    } finally {
      submit.disabled = false;
    }
  });
  section.append(secretBox, error, listWrap, form);
  refresh();
  return section;
}

function modalAlertError(node, message) {
  node.textContent = message;
}

// ---------------------------------------------------------------------------
// 任务/笔记模板库：内置模板 + 本人自定义模板。真实模式读 /api/templates，
// 演示模式回退到一套本地常量，保证新建流程在任何模式下可体验。
// ---------------------------------------------------------------------------
function demoTemplateSet() {
  return [
    { id: "builtin-daily-standup", builtin: true, name: "每日站会", kind: "task", area: "work",
      title: "每日站会", body: "## 昨天\n- 已完成的事项\n\n## 今天\n- 待推进的事\n\n## 阻塞\n- 需要协调的点", meta: { tags: ["站会"] } },
    { id: "builtin-weekly-report", builtin: true, name: "周报", kind: "note", area: "work",
      title: "周报", body: "## 本周进展\n\n## 下周计划\n\n## 需要支持", meta: { tags: ["周报"] } },
    { id: "builtin-reading-list", builtin: true, name: "读书清单", kind: "task", area: "life",
      title: "在读清单", body: "", meta: { tags: ["阅读"] } },
    { id: "builtin-travel-prep", builtin: true, name: "旅行准备", kind: "task", area: "life",
      title: "旅行清单", body: "证件、衣物、充电器、药品…", meta: { tags: ["旅行"] } },
  ];
}
async function loadTemplates(force = false) {
  if (state.demo) {
    if (!state.templates.loaded || force) {
      state.templates.list = demoTemplateSet();
      state.templates.loaded = true;
    }
    return;
  }
  if (!state.user || (state.templates.loading && !force)) return;
  state.templates.loading = true;
  const session = state.sessionEpoch;
  try {
    const result = await api("/api/templates");
    if (state.sessionEpoch !== session) return;
    state.templates.list = Array.isArray(result.templates) ? result.templates : [];
    state.templates.loaded = true;
  } catch {
    /* 静默失败，新建流程不受影响，仅模板选择暂不可用 */
  } finally {
    if (state.sessionEpoch === session) state.templates.loading = false;
  }
}
function kindLabel(kind) {
  return { task: "待办", note: "笔记", media: "媒体" }[kind] || kind;
}
function areaLabel(area) {
  return area === "life" ? "生活" : "工作";
}
// 模板项文案：名称 + 类型·分区。注意 el() 的第三个参数只能是文本，
// 所以子元素必须先建容器再 append，不能直接把数组传进去。
function templateOptionText(t) {
  const info = el("div", "template-option-text");
  info.append(
    el("strong", "", t.name),
    el("small", "", `${kindLabel(t.kind)} · ${areaLabel(t.area)}`),
  );
  return info;
}
function showTemplatePicker(kind) {
  showModal("从模板新建", (body) => {
    const wrap = el("div", "template-picker");
    const render = () => {
      wrap.replaceChildren();
      const builtins = state.templates.list.filter((t) => t.builtin);
      const mine = state.templates.list.filter((t) => !t.builtin);
      if (builtins.length) {
        wrap.append(el("h4", "template-group", "内置推荐"));
        builtins.forEach((t) => {
          const row = button("", "template-option", () => {
            closeModal();
            showEditorFromTemplate(t);
          });
          row.type = "button";
          row.append(
            svg(t.kind === "task" ? "task" : "note"),
            templateOptionText(t),
            svg("arrow", true),
          );
          wrap.append(row);
        });
      }
      if (mine.length) {
        wrap.append(el("h4", "template-group", "我的模板"));
        mine.forEach((t) => {
          const row = button("", "template-option", () => {
            closeModal();
            showEditorFromTemplate(t);
          });
          row.type = "button";
          row.append(
            svg(t.kind === "task" ? "task" : "note"),
            templateOptionText(t),
            svg("arrow", true),
          );
          wrap.append(row);
        });
      }
      if (!builtins.length && !mine.length) {
        wrap.append(
          el(
            "p",
            "helper",
            state.templates.loaded
              ? "还没有自定义模板，可在新建后选择「保存为模板」。"
              : "模板加载中…",
          ),
        );
      }
    };
    render();
    body.append(wrap);
    // loadTemplates 是异步的：数据回来后再重绘，避免首次渲染空白。
    loadTemplates().then(() => {
      if (!$("#modal").open) return;
      render();
    });
  }, "模板");
}
function showEditorFromTemplate(t) {
  // 内置模板常量的 area 优先于当前分区，避免把生活模板建到工作区。
  const meta = t.meta || {};
  const targetKind = t.kind || "task";
  showEditor(targetKind, null, {
    title: t.title || "",
    body: t.body || "",
    area: t.area,
    meta,
  });
}
function showTemplateManagement() {
  showModal("模板库", (body) => {
    const wrap = el("div", "template-manage");
    const render = () => {
      wrap.replaceChildren();
      const builtins = state.templates.list.filter((t) => t.builtin);
      const mine = state.templates.list.filter((t) => !t.builtin);
      if (builtins.length)
        wrap.append(el("h4", "template-group", "内置推荐（只读）"));
      builtins.forEach((t) => {
        const row = el("div", "template-row");
        const info = el("div", "template-option-text");
        info.append(
          el("strong", "", t.name),
          el("small", "", `${kindLabel(t.kind)} · ${areaLabel(t.area)}`),
        );
        row.append(
          svg(t.kind === "task" ? "task" : "note"),
          info,
          el("span", "builtin-badge", "内置"),
        );
        wrap.append(row);
      });
      if (mine.length) {
        wrap.append(el("h4", "template-group", "我的模板"));
        mine.forEach((t) => {
          const row = el("div", "template-row");
          const info = el("div", "template-option-text");
          info.append(
            el("strong", "", t.name),
            el("small", "", `${kindLabel(t.kind)} · ${areaLabel(t.area)}`),
          );
          const editBtn = button("编辑", "button soft compact", () =>
            showEditor("task", null, { title: t.title, body: t.body, area: t.area, meta: t.meta }),
          );
          const delBtn = button("删除", "button soft compact danger", () => {
            showModal("删除这个模板？", (b2) => {
              b2.append(el("p", "helper", `「${t.name}」删除后不可恢复，已有的记录不受影响。`));
              b2.append(actionsRow(button("确认删除", "button danger", async () => {
                try {
                  await api(`/api/templates/${t.id}`, "DELETE");
                  closeModal();
                  state.templates.loaded = false;
                  await loadTemplates(true);
                  render();
                  toast("已删除模板");
                } catch (err) {
                  closeModal();
                  toast(errorMessage(err), true);
                }
              })));
            });
          });
          const actions = el("div", "template-row-actions");
          actions.append(editBtn, delBtn);
          row.append(svg(t.kind === "task" ? "task" : "note"), info, actions);
          wrap.append(row);
        });
      } else {
        wrap.append(el("p", "helper", "还没有自定义模板。新建一条记录后，可在编辑器「保存为模板」。"));
      }
    };
    render();
    body.append(wrap);
    // 异步加载完成后再重绘，避免首次渲染只有空态。
    loadTemplates().then(() => {
      if (!$("#modal").open) return;
      render();
    });
  }, "我的模板");
}
async function saveCurrentAsTemplate(kind, fields) {
  if (!kind) return;
  try {
    const result = await api("/api/templates", "POST", {
      name: fields.title?.slice(0, 60) || `${kindLabel(kind)}模板`,
      kind,
      area: fields.area || "work",
      title: fields.title || "",
      body: fields.body || "",
      meta: fields.meta || {},
    });
    state.templates.loaded = false;
    await loadTemplates(true);
    if (result.template?.id) toast("已保存为模板");
    return result.template;
  } catch (err) {
    toast(errorMessage(err), true);
    return null;
  }
}

// ---------------------------------------------------------------------------
// 登录方式：用户名（必填登录名）与手机号（可选），改绑需验证密码。
// ---------------------------------------------------------------------------
function buildIdentitySection() {
  const section = el("section", "account-section");
  section.append(el("h3", "", "登录方式"));
  if (state.demo) {
    section.append(el("p", "helper", "演示模式不连接账号服务，登录方式需在真实账号中设置。"));
    return section;
  }
  const username = state.user?.username || "";
  const phone = state.user?.phone || "";
  section.append(
    el(
      "p",
      "helper",
      `当前用「${username || "未设置"}」登录${phone ? `，手机号 ${phone}` : ""}。用户名与手机号都可以用来登录。`,
    ),
  );
  const form = el("form");
  const nameInput = input("text", "3-30 位字母、数字、下划线", username);
  nameInput.autocomplete = "username";
  nameInput.spellcheck = false;
  nameInput.autocapitalize = "none";
  const phoneInput = input("tel", "11 位手机号，留空表示不设置", phone);
  phoneInput.autocomplete = "tel";
  phoneInput.inputMode = "tel";
  phoneInput.maxLength = 11;
  const password = input("password", "当前登录密码");
  password.autocomplete = "current-password";
  password.required = true;
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  const save = el("button", "button primary", "保存登录方式");
  save.type = "submit";
  form.append(
    labelField("用户名", nameInput),
    labelField("手机号（可选）", phoneInput),
    labelField("验证密码", password),
    error,
    actionsRow(save),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    const nextName = nameInput.value.trim();
    const nextPhone = phoneInput.value.trim();
    if (!nextName) return void (error.textContent = "请填写用户名");
    if (nextPhone && !/^1[3-9]\d{9}$/.test(nextPhone))
      return void (error.textContent = "请输入有效的 11 位手机号");
    if (!password.value) return void (error.textContent = "请输入当前密码");
    const payload = { password: password.value };
    if (nextName !== username) payload.username = nextName;
    if (nextPhone !== phone) payload.phone = nextPhone;
    if (!("username" in payload) && !("phone" in payload))
      return void (error.textContent = "登录方式没有变化");
    save.disabled = true;
    try {
      const result = await api("/api/me/identity", "PATCH", payload);
      state.user = { ...state.user, ...result.identity };
      password.value = "";
      $("#user-status").textContent = state.user.username || "个人账户";
      toast("登录方式已更新");
    } catch (err) {
      error.textContent = errorMessage(err);
    } finally {
      save.disabled = false;
    }
  });
  section.append(form);
  return section;
}

function buildNicknameSection() {
  const section = el("section", "account-section");
  section.append(el("h3", "", "修改昵称"));
  const form = el("form");
  const name = input("text", "怎么称呼你", state.user?.name || "");
  name.maxLength = 80;
  name.autocomplete = "nickname";
  name.required = true;
  if (state.demo) name.disabled = true;
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  const save = el("button", "button primary", "保存昵称");
  save.type = "submit";
  if (state.demo) save.disabled = true;
  form.append(
    labelField("昵称", name),
    error,
    actionsRow(save),
  );
  if (state.demo)
    form.append(el("p", "helper", "演示模式不连接账号服务，昵称与账号资料不会上传。"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const next = name.value.trim();
    modalAlertError(error, "");
    if (!next) {
      modalAlertError(error, "请输入昵称");
      return;
    }
    if (next === state.user.name) {
      modalAlertError(error, "昵称没有变化");
      return;
    }
    save.disabled = true;
    try {
      if (!state.demo) {
        const result = await api("/api/me", "PATCH", { name: next });
        state.user = { ...state.user, ...result.user };
      } else {
        state.user = { ...state.user, name: next };
      }
      if (!$("#modal").open) return;
      $("#user-name").textContent = state.user.name || "我的账户";
      $("#user-avatar").textContent = (state.user.name || "知").slice(0, 1);
      name.value = state.user.name;
      toast("昵称已更新");
    } catch (err) {
      if (!$("#modal").open) return;
      modalAlertError(error, errorMessage(err));
      save.disabled = false;
    }
  });
  section.append(form);
  return section;
}

function buildPasswordSection() {
  const section = el("section", "account-section");
  section.append(el("h3", "", "修改密码"));
  if (state.demo) {
    section.append(
      el("p", "helper", "演示模式不提供账号密码修改，创建账户后可在此更新登录密码。"),
    );
    return section;
  }
  const form = el("form");
  const current = input("password", "当前登录密码");
  current.autocomplete = "current-password";
  current.required = true;
  // 不在 DOM 上设置 minLength：长度规则交给下方统一的中文校验，
  // 避免浏览器原生提示绕过 role=alert 的错误文案
  const next = input("password", `新密码，至少 ${PASSWORD_MIN_LENGTH} 位字符`);
  next.autocomplete = "new-password";
  next.required = true;
  const confirm = input("password", "再次输入新密码");
  confirm.autocomplete = "new-password";
  confirm.required = true;
  const error = el("p", "form-error");
  error.setAttribute("role", "alert");
  const save = el("button", "button primary", "更新密码");
  save.type = "submit";
  form.append(
    labelField("当前密码", current),
    labelField("新密码", next),
    labelField("确认新密码", confirm),
    error,
    actionsRow(save),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    modalAlertError(error, "");
    if (!current.value) {
      modalAlertError(error, "请输入当前密码");
      return;
    }
    if (next.value.length < PASSWORD_MIN_LENGTH) {
      modalAlertError(error, `新密码至少需要 ${PASSWORD_MIN_LENGTH} 位字符`);
      return;
    }
    if (next.value !== confirm.value) {
      modalAlertError(error, "两次输入的新密码不一致");
      return;
    }
    save.disabled = true;
    try {
      // 后端事实契约：密码变更嵌套在 password 对象内，成功后服务端轮换会话
      await api("/api/me", "PATCH", {
        password: {
          currentPassword: current.value,
          newPassword: next.value,
        },
      });
      if (!$("#modal").open) return;
      current.value = "";
      next.value = "";
      confirm.value = "";
      toast("密码已更新，下次登录请使用新密码");
    } catch (err) {
      if (!$("#modal").open) return;
      modalAlertError(error, errorMessage(err));
    } finally {
      if ($("#modal").open) save.disabled = false;
    }
  });
  section.append(form);
  return section;
}

function buildAccountDangerZone() {
  const section = el("section", "account-section danger-zone");
  section.append(el("h3", "", "危险区"));
  section.append(
    el(
      "p",
      "helper",
      "注销账号会删除你的登录身份与名下空间，操作无法恢复。需要保留的数据请先在空间与协作中导出。",
    ),
  );
  if (state.demo) {
    section.append(
      el("p", "helper", "演示模式没有真实账号，直接退出演示即可清空本地样例。"),
    );
    return section;
  }
  section.append(
    button("注销账号", "button danger", confirmDeleteAccount, "trash"),
  );
  return section;
}

// 注销账号二次确认：密码 + 逐字键入确认短语，缺一不可启用红色按钮
function confirmDeleteAccount() {
  showModal(
    "注销账号",
    (body) => {
      body.append(
        el(
          "p",
          "modal-body-note",
          "注销后账号、名下空间与全部记录将被删除且无法恢复，当前登录会话会立即失效。",
        ),
      );
      const form = el("form");
      const password = input("password", "登录密码");
      password.autocomplete = "current-password";
      password.required = true;
      const phrase = input("text", `请输入「${ACCOUNT_DELETE_PHRASE}」确认`);
      phrase.autocomplete = "off";
      phrase.autocapitalize = "none";
      phrase.required = true;
      const error = el("p", "form-error");
      error.setAttribute("role", "alert");
      const confirmBtn = el("button", "button danger", "确认注销账号");
      confirmBtn.type = "submit";
      confirmBtn.disabled = true;
      const sync = () => {
        confirmBtn.disabled =
          !password.value || phrase.value.trim() !== ACCOUNT_DELETE_PHRASE;
      };
      password.addEventListener("input", sync);
      phrase.addEventListener("input", sync);
      form.append(
        labelField("登录密码", password),
        labelField("确认文字", phrase),
        el(
          "p",
          "helper",
          `请逐字键入「${ACCOUNT_DELETE_PHRASE}」六个字，注销按钮才会启用。`,
        ),
        error,
        actionsRow(button("取消", "button secondary", closeModal), confirmBtn),
      );
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        modalAlertError(error, "");
        confirmBtn.disabled = true;
        try {
          await api("/api/me/account", "DELETE", { password: password.value });
          // 成功即会话失效：复用退出登录的本地清理，回到登录页
          for (const url of state.objectURLs) URL.revokeObjectURL(url);
          state.objectURLs.clear();
          state.demo = false;
          state.demoStore = {};
          state.demoVaults = {};
          state.demoDaily = {};
          closeModal();
          showAuth();
          toast("账号已注销");
        } catch (err) {
          if (!$("#modal").open) return;
          modalAlertError(error, errorMessage(err));
          confirmBtn.disabled = false;
          password.focus();
        }
      });
      body.append(form);
    },
    "危险区",
  );
}

// 删除空间二次确认：键入空间全名才能启用删除；409 时原样展示后端说明
function confirmDeleteSpace(space) {
  showModal("删除这个空间？", (body) => {
    body.append(
      el(
        "p",
        "modal-body-note",
        `「${space.name}」删除后无法恢复，空间内的记录、媒体与密码箱副本会一并删除。${
          (space.member_count || 1) > 1
            ? "该空间仍有其他成员，需要先移除全部成员。"
            : ""
        }`,
      ),
    );
    const form = el("form");
    const phrase = input("text", `请输入空间全名「${space.name}」以确认`);
    phrase.autocomplete = "off";
    phrase.autocapitalize = "none";
    phrase.required = true;
    const error = el("p", "form-error");
    error.setAttribute("role", "alert");
    const remove = el("button", "button danger", "确认删除空间");
    remove.type = "submit";
    remove.disabled = true;
    phrase.addEventListener("input", () => {
      remove.disabled = phrase.value !== space.name;
    });
    form.append(
      labelField("空间全名", phrase),
      error,
      actionsRow(button("取消", "button secondary", closeModal), remove),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (phrase.value !== space.name) return;
      modalAlertError(error, "");
      remove.disabled = true;
      try {
        if (state.demo) {
          delete state.demoStore[space.id];
          state.spaces = state.spaces.filter((s) => s.id !== space.id);
        } else {
          await api(`/api/spaces/${space.id}`, "DELETE");
          const fresh = await api("/api/spaces");
          state.spaces = fresh.spaces || fresh;
        }
        closeModal();
        toast("空间已删除");
        if (state.space?.id === space.id) {
          if (state.spaces.length) {
            await switchSpace(state.spaces[0].id);
          } else {
            state.space = null;
            state.items = [];
            render();
            showCreateSpace();
          }
        } else {
          render();
        }
      } catch (err) {
        if (!$("#modal").open) return;
        // 409 等后端中文消息已包含「先移除成员」等行动指引，原样展示
        modalAlertError(error, errorMessage(err));
        remove.disabled = false;
      }
    });
    body.append(form);
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
    // 新建密码箱仅持久化失败时，保留已派生密钥与表单上下文，允许原地重试
    let retryCreate = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.textContent = "";
      const id = state.space.id,
        epoch = state.vault.epoch;
      if (retryCreate) {
        if (
          state.space?.id !== id ||
          state.vault.epoch !== epoch ||
          !state.vault.key
        ) {
          retryCreate = false;
          return;
        }
        save.disabled = true;
        save.textContent = "正在保存…";
        try {
          await persistVault([]);
          if (state.space?.id !== id || state.vault.epoch !== epoch) return;
          retryCreate = false;
          render();
          toast("密码箱已创建");
        } catch (err) {
          if (state.space?.id !== id || state.vault.epoch !== epoch) return;
          error.setAttribute("role", "alert");
          error.textContent = errorMessage(err);
          save.disabled = false;
          save.textContent = "重试创建密码箱";
        }
        return;
      }
      if (!existing && password.value !== confirm.value) {
        error.textContent = "两次输入的密码不同，请重新确认。";
        return;
      }
      const secret = password.value;
      password.value = "";
      if (confirm) confirm.value = "";
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
            if (state.space?.id !== id || state.vault.epoch !== epoch) return;
            // 持久化失败：保留 key、entries 与仅含 salt 的 payload，
            // 不置 null、不清 key，避免与服务端状态分叉并允许原地重试
            retryCreate = true;
            // 密码框已按安全习惯清空，重试走已派生的密钥，需绕过原生必填校验
            save.formNoValidate = true;
            error.setAttribute("role", "alert");
            error.textContent = `${errorMessage(err)} 密码仍保留在本页，可直接重试，无需重新输入。`;
            save.disabled = false;
            save.textContent = "重试创建密码箱";
            return;
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
          id: entry?.id || randomUUID(),
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

// ── 今日记录：图片速记与视觉识别 ──────────────────────────────
function dailyDateString(date = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}
function dailyDayLabel(entryDate) {
  const today = dailyDateString();
  if (entryDate === today) return "今天";
  const yesterday = dailyDateString(new Date(Date.now() - 86400000));
  if (entryDate === yesterday) return "昨天";
  const [year, month, day] = entryDate.split("-").map(Number);
  const weekday = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][
    new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  ];
  return `${month}月${day}日 ${weekday}`;
}
function dailyTime(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
function visionEnabled() {
  return state.daily.config?.vision?.enabled === true && !state.demo;
}
let dailyConfigPromise = null;
async function loadDailyConfig() {
  if (state.daily.config) return state.daily.config;
  const fallback = {
    vision: { enabled: false, model: "" },
    maxImages: 9,
    maxUploadBytes: MAX_UPLOAD_MB * 1024 * 1024,
    imageMimes: ["image/png", "image/jpeg", "image/gif", "image/webp"],
  };
  if (state.demo) {
    state.daily.config = fallback;
    return fallback;
  }
  if (!dailyConfigPromise)
    dailyConfigPromise = api(`/api/spaces/${state.space.id}/daily/config`)
      .then((result) => {
        state.daily.config = result;
        return result;
      })
      .catch(() => {
        state.daily.config = fallback;
        return fallback;
      });
  return dailyConfigPromise;
}
async function loadDailyEntries() {
  if (!state.space) return;
  await loadDailyConfig();
  if (state.demo) {
    if (!state.demoDaily[state.space.id])
      state.demoDaily[state.space.id] = demoDailyEntries();
    state.daily.entries = state.demoDaily[state.space.id];
    state.daily.digests = {};
    state.daily.loadedSpace = state.space.id;
    render();
    return;
  }
  if (state.daily.loadedSpace === state.space.id) {
    render();
    return;
  }
  // The loading flag is also the re-entrancy guard: render() can re-enter this
  // function through the overview card, so without it the calls recurse forever.
  if (state.daily.loading) return;
  state.daily.loading = true;
  render();
  try {
    const month = dailyDateString().slice(0, 7);
    const result = await api(
      `/api/spaces/${state.space.id}/daily/entries?month=${month}`,
    );
    state.daily.entries = result.entries || [];
    state.daily.digests = result.digests || {};
    state.daily.month = result.month || month;
    state.daily.loadedSpace = state.space.id;
  } catch (err) {
    toast(errorMessage(err), true);
  }
  state.daily.loading = false;
  render();
}
function upsertDailyEntry(entry) {
  const index = state.daily.entries.findIndex((item) => item.id === entry.id);
  if (index >= 0) state.daily.entries[index] = entry;
  else state.daily.entries.unshift(entry);
  state.daily.entries.sort(
    (a, b) =>
      b.entryDate.localeCompare(a.entryDate) ||
      b.createdAt.localeCompare(a.createdAt),
  );
}
function currentDailyEntries() {
  return state.daily.entries.filter((entry) => entry.area === state.area);
}
function dailyImageURL(image) {
  if (image?.localURL)
    return state.objectURLs.has(image.localURL) ? image.localURL : "";
  return image?.url || "";
}
function renderDaily(content) {
  if (state.daily.loading) {
    loading(content);
    return;
  }
  if (writable()) content.append(renderDailyComposer());
  const entries = currentDailyEntries();
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.entryDate)) groups.set(entry.entryDate, []);
    groups.get(entry.entryDate).push(entry);
  }
  const timeline = el("div", "daily-timeline");
  if (!entries.length) {
    emptyState(
      timeline,
      "camera",
      state.area === "work" ? "今天忙了什么？拍下来" : "把生活瞬间留下来",
      "拍照或选图即可记录，AI 会识别图片、总结内容并提出待办建议。",
      writable() ? "拍第一张" : null,
      writable() ? () => pickDailyImages(true) : null,
    );
  }
  for (const [date, dayEntries] of groups) {
    const group = el("section", "daily-day-group");
    const header = el("div", "daily-day-header");
    const heading = el("h2", "", dailyDayLabel(date));
    const dateText = el("span", "", date);
    header.append(heading, dateText);
    group.append(header);
    const digest = state.daily.digests[date];
    if (digest) group.append(renderDigestCard(date, digest));
    else if (date === dailyDateString() && visionEnabled() && writable()) {
      const digestAction = el("div", "daily-digest-action");
      digestAction.append(
        button("生成今日小结", "text-button", () =>
          generateDailyDigest(date),
        ),
      );
      group.append(digestAction);
    }
    for (const entry of dayEntries) group.append(renderDailyEntry(entry));
    timeline.append(group);
  }
  content.append(timeline);
  if (entries.length) {
    const note = el("p", "daily-archive-note");
    note.textContent = `仅展示 ${state.daily.month || dailyDateString().slice(0, 7)} 的记录，更早的月份将在后续版本开放。`;
    content.append(note);
  }
}
function renderDigestCard(date, digest) {
  const card = el("article", "card daily-digest-card");
  card.append(
    cardHeading("今日小结", "note", null, null, "AI"),
    el("p", "daily-digest-text", digest),
  );
  if (writable() && visionEnabled()) {
    const foot = el("div", "daily-card-footer");
    foot.append(
      button("重新生成", "text-button", () => generateDailyDigest(date)),
    );
    card.append(foot);
  }
  return card;
}
function renderDailyComposer() {
  const card = el("section", "card daily-composer");
  card.append(
    cardHeading(
      state.area === "work" ? "拍一张，记下今天的工作" : "拍一张，留下此刻",
      "camera",
    ),
  );
  const picker = el("div", "daily-picker-row");
  const cameraButton = button("拍照", "button secondary", () =>
    pickDailyImages(true),
  );
  cameraButton.prepend(svg("camera", true));
  const galleryButton = button("从相册选择", "button secondary", () =>
    pickDailyImages(false),
  );
  galleryButton.prepend(svg("image", true));
  // 语音转文字：用浏览器语音识别把说的话直接写进附言。
  const voiceButton = button("语音转文字", "button secondary", () =>
    startDailyDictation(note, voiceButton),
  );
  voiceButton.prepend(svg("mic", true));
  // 附件：图片以外的文件统一进资料库，记录里留一条引用。
  const attachButton = button("上传附件", "button secondary", () => {
    ensureWrite() && chooseFiles();
  });
  attachButton.prepend(svg("upload", true));
  picker.append(cameraButton, galleryButton, voiceButton, attachButton);
  const strip = el("div", "daily-composer-strip");
  const note = el("textarea", "daily-note");
  note.placeholder = "今天做了什么？写一句就好，也可以语音转文字或上传附件…";
  note.maxLength = 100000;
  note.setAttribute("aria-label", "记录附言");
  const bottom = el("div", "daily-composer-bottom");
  const counter = el("span", "daily-counter", "");
  const submit = button(
    visionEnabled() ? "保存并识别" : "保存记录",
    "button primary",
    () => submitDailyEntry(note, submit),
  );
  bottom.append(counter, submit);
  const hint = el(
    "p",
    "capture-hint",
    visionEnabled()
      ? "原图保存在当前空间，识别在服务端完成，不会替换原图。"
      : "图片识别待连接服务端模型，可先保存图片并手动填写摘要。",
  );
  card.append(picker, strip, note, bottom, hint);
  strip._submit = submit;
  renderComposerStrip(strip, counter);
  card._strip = strip;
  card._counter = counter;
  return card;
}
// 语音转文字：优先用浏览器内置的 Web Speech API（Chrome/Edge 支持，
// 优先中文）。不支持时给出明确提示，不静默失败。
let dictationRecognition = null;
function startDailyDictation(note, trigger) {
  const Recognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    toast("当前浏览器不支持语音转文字，可改用键盘输入或语音录入", true);
    return;
  }
  if (dictationRecognition) {
    try {
      dictationRecognition.stop();
    } catch {
      /* 已停止时忽略 */
    }
    dictationRecognition = null;
    trigger.textContent = "语音转文字";
    return;
  }
  const recognition = new Recognition();
  recognition.lang = "zh-CN";
  recognition.continuous = true;
  recognition.interimResults = false;
  const base = note.value ? `${note.value.replace(/\s*$/, "")} ` : "";
  recognition.onresult = (event) => {
    let text = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) text += event.results[i][0].transcript;
    }
    if (text) note.value = (note.value ? `${note.value} ` : "") + text.trim();
  };
  recognition.onerror = (event) => {
    const message =
      event.error === "not-allowed"
        ? "麦克风权限被拒绝，请在浏览器设置里允许后重试"
        : "语音识别中断，可再试一次";
    toast(message, true);
    stop();
  };
  recognition.onend = () => stop();
  function stop() {
    dictationRecognition = null;
    trigger.textContent = "语音转文字";
    trigger.classList.remove("recording");
  }
  dictationRecognition = recognition;
  trigger.textContent = "停止转写";
  trigger.classList.add("recording");
  try {
    recognition.start();
    toast("已开始转写，说话内容会写入附言");
  } catch {
    stop();
    toast("无法启动语音识别", true);
  }
}

function renderComposerStrip(strip, counter) {
  strip.replaceChildren();
  const items = state.daily.composer;
  for (const item of items) {
    const tile = el("div", `daily-upload ${item.status}`);
    const image = el("img");
    image.src = item.localURL;
    image.alt = item.name;
    tile.append(image);
    const status = el("span", "daily-upload-status");
    if (item.status === "uploading") status.textContent = "上传中…";
    else if (item.status === "queued") status.textContent = "等待上传";
    else if (item.status === "error") {
      status.textContent = item.error || "上传失败";
      // 失败原因需要被辅助技术读出
      status.setAttribute("role", "alert");
    } else status.textContent = "已就绪";
    if (item.status === "error") {
      // 失败瓦片给出明确的重试/移除操作，不再只靠右上角小图标
      const actions = el("div", "daily-upload-actions");
      const retry = button("重试", "text-button", () =>
        uploadDailyImage(item).then(() => refreshComposerStrip()),
      );
      const remove = button(
        "移除",
        "text-button danger-text",
        () => removeComposerImage(item.tempId),
      );
      actions.append(retry, remove);
      tile.append(status, actions);
    } else {
      const remove = iconButton("close", "移除这张图片", () =>
        removeComposerImage(item.tempId),
      );
      remove.classList.add("daily-upload-remove");
      tile.append(remove, status);
    }
    strip.append(tile);
  }
  counter.textContent = items.length
    ? `${items.length}/${state.daily.config?.maxImages || 9} 张`
    : "";
  syncComposerSubmit(strip, items);
}
// 任一瓦片未就绪（排队/上传中/失败）都暂时禁用提交，失败原因与重试入口直接显示在瓦片上
function syncComposerSubmit(strip, items = state.daily.composer) {
  const submit = strip?._submit;
  if (!submit || state.demo) return;
  const blocked = items.some((item) => item.status !== "ready");
  submit.disabled = blocked;
  submit.title = items.some((item) => item.status === "error")
    ? "有图片上传失败，请重试或移除后再保存"
    : "";
}
function refreshComposerStrip() {
  const card = $(".daily-composer");
  if (card?._strip) renderComposerStrip(card._strip, card._counter);
}
function pickDailyImages(capture) {
  if (!ensureWrite()) return;
  const input = $("#daily-file-input");
  if (capture) input.setAttribute("capture", "environment");
  else input.removeAttribute("capture");
  input.value = "";
  input.click();
}
async function onDailyFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  const config = await loadDailyConfig();
  const room = config.maxImages - state.daily.composer.length;
  if (room <= 0) {
    toast(`每条记录最多 ${config.maxImages} 张图片`, true);
    return;
  }
  for (const file of files.slice(0, room)) {
    if (!file.type.startsWith("image/")) {
      toast(`「${file.name}」不是图片，已跳过`, true);
      continue;
    }
    if (file.size < 1 || file.size > config.maxUploadBytes) {
      toast(
        `「${file.name}」须在 ${Math.round(config.maxUploadBytes / 1048576)} MB 以内`,
        true,
      );
      continue;
    }
    const localURL = URL.createObjectURL(file);
    state.objectURLs.add(localURL);
    const item = {
      tempId: randomUUID(),
      file,
      localURL,
      name: file.name,
      size: file.size,
      status: state.demo ? "ready" : "queued",
      uploadId: "",
      error: "",
    };
    state.daily.composer.push(item);
    if (!state.demo) uploadDailyImage(item).then(() => refreshComposerStrip());
  }
  refreshComposerStrip();
}
async function uploadDailyImage(item) {
  item.status = "uploading";
  item.error = "";
  refreshComposerStrip();
  try {
    const data = new FormData();
    data.append("file", item.file, item.name);
    const result = await api(
      `/api/spaces/${state.space.id}/daily/uploads`,
      "POST",
      data,
    );
    item.uploadId = result.upload.id;
    item.status = "ready";
  } catch (err) {
    item.status = "error";
    item.error = errorMessage(err);
  }
}
function removeComposerImage(tempId) {
  const index = state.daily.composer.findIndex(
    (item) => item.tempId === tempId,
  );
  if (index < 0) return;
  const [item] = state.daily.composer.splice(index, 1);
  if (item.localURL) {
    URL.revokeObjectURL(item.localURL);
    state.objectURLs.delete(item.localURL);
  }
  refreshComposerStrip();
}
async function submitDailyEntry(note, submit) {
  if (!ensureWrite()) return;
  const items = state.daily.composer;
  const body = note.value.trim();
  if (!items.length && !body) {
    note.focus();
    toast("先拍一张图片，或写一句记录", true);
    return;
  }
  if (!state.demo && items.some((item) => item.status !== "ready")) {
    toast(
      items.some((item) => item.status === "error")
        ? "有图片上传失败，请重试或移除后再保存"
        : "图片还在上传，请稍等",
      true,
    );
    return;
  }
  submit.disabled = true;
  const sid = state.space.id;
  try {
    let entry;
    if (state.demo) {
      entry = buildDemoDailyEntry(items, body);
      state.demoDaily[sid].unshift(entry);
      state.daily.entries = state.demoDaily[sid];
    } else {
      const result = await api(`/api/spaces/${sid}/daily/entries`, "POST", {
        imageIds: items.map((item) => item.uploadId),
        body,
        area: state.area,
      });
      entry = result.entry;
      upsertDailyEntry(entry);
    }
    for (const item of items) {
      URL.revokeObjectURL(item.localURL);
      state.objectURLs.delete(item.localURL);
    }
    state.daily.composer = [];
    note.value = "";
    render();
    toast(state.demo ? "已保存到本地演示" : "记录已保存");
    if (!state.demo && visionEnabled() && entry.images?.length) {
      await recognizeDailyEntry(entry, { silent: true });
    }
  } catch (err) {
    toast(errorMessage(err), true);
    submit.disabled = false;
  }
}
function buildDemoDailyEntry(items, body) {
  const now = new Date().toISOString();
  const entryId = `local-${randomUUID()}`;
  return {
    id: entryId,
    spaceId: state.space.id,
    area: state.area,
    entryDate: dailyDateString(),
    title: body.split("\n")[0].slice(0, 20) || "今日记录",
    summary: "",
    body,
    status: "active",
    meta: {
      ai: {
        status: "disabled",
        model: "",
        title: "",
        summary: "",
        tags: [],
        suggestions: [],
        recognizedAt: "",
        applied: false,
      },
      convertedSuggestions: [],
      source: "daily",
    },
    createdBy: state.user.id,
    createdAt: now,
    updatedAt: now,
    images: items.map((item, index) => ({
      id: `local-${randomUUID()}`,
      entryId,
      name: item.name,
      mime: item.file.type,
      size: item.size,
      width: 0,
      height: 0,
      sortOrder: index,
      url: "",
      localURL: item.localURL,
      createdAt: now,
    })),
  };
}
function demoDailyEntries() {
  const now = new Date().toISOString();
  const demoMeta = () => ({
    ai: {
      status: "disabled",
      model: "",
      title: "",
      summary: "",
      tags: [],
      suggestions: [],
      recognizedAt: "",
      applied: false,
    },
    convertedSuggestions: [],
    source: "daily",
  });
  const makeDemoEntry = (area, title, body) => ({
    id: `local-demo-${randomUUID()}`,
    spaceId: "demo-personal",
    area,
    entryDate: dailyDateString(),
    title,
    summary: "",
    body,
    status: "active",
    meta: demoMeta(),
    createdBy: "demo-user",
    createdAt: now,
    updatedAt: now,
    images: [],
  });
  return [
    makeDemoEntry(
      "work",
      "演示：拍一张白板开始记录",
      "演示模式下，拍照记录只保存在当前浏览器，不会上传或调用 AI。",
    ),
    makeDemoEntry(
      "life",
      "周末散步时拍下的小路",
      "路边的树影落在墙上，走慢一点才注意到。",
    ),
    makeDemoEntry(
      "life",
      "家里做的一顿晚饭",
      "两菜一汤，认真吃饭也是生活里的小记录。",
    ),
  ];
}
function renderDailyEntry(entry) {
  // 每条记录是一个独立卡片框：框内自上而下是内容、附件、操作。
  // 今日记录不再显示标题，正文即内容。
  const card = el("article", "card daily-entry daily-entry-card");
  const ai = entry.meta?.ai || {};
  const head = el("div", "daily-entry-head");
  const meta = el("div", "daily-entry-meta");
  meta.append(
    el("span", "", dailyTime(entry.createdAt)),
    el("span", "", entry.area === "work" ? "工作" : "生活"),
  );
  head.append(meta);
  card.append(head);
  // 正文与摘要分别渲染，样式和语义保持既有约定。
  if (entry.body?.trim()) {
    const body = el("p", "daily-entry-body");
    body.textContent = entry.body.trim();
    card.append(body);
  }
  if (entry.images?.length) card.append(renderDailyImageGrid(entry));
  if (ai.status === "pending") {
    const pending = el("div", "daily-ai pending");
    pending.append(
      el("span", "daily-skeleton"),
      el("p", "", "正在识别图片，马上生成摘要与待办建议…"),
    );
    card.append(pending);
  } else if (ai.status === "failed") {
    const failed = el("div", "daily-ai failed");
    failed.append(el("p", "", "这次识别没有成功，图片仍已保存。"));
    if (writable() && visionEnabled())
      failed.append(
        button("重新识别", "button secondary", () =>
          recognizeDailyEntry(entry),
        ),
      );
    card.append(failed);
  } else if (ai.status === "done") {
    card.append(renderRecognition(entry, ai));
  } else if (!entry.summary) {
    const disabled = el("div", "daily-ai disabled");
    disabled.append(
      el(
        "p",
        "",
        visionEnabled()
          ? "还没有识别结果。"
          : "图片识别待连接，可先手动填写摘要。",
      ),
    );
    if (writable() && visionEnabled())
      disabled.append(
        button("开始识别", "button secondary", () =>
          recognizeDailyEntry(entry),
        ),
      );
    card.append(disabled);
  }
  // 有摘要就展示；AI 已产出摘要时由识别区块统一呈现，避免重复。
  if (entry.summary && (ai.status !== "done" || entry.summary !== ai.summary))
    card.append(el("p", "daily-summary", entry.summary));
  if (entry.body) card.append(el("p", "daily-body", entry.body));
  if (writable()) card.append(renderDailyEntryActions(entry, ai));
  return card;
}
function renderDailyImageGrid(entry) {
  const grid = el("div", `daily-image-grid count-${Math.min(entry.images.length, 9)}`);
  entry.images.forEach((image, index) => {
    const url = dailyImageURL(image);
    const tile = button("", "daily-image-tile", () =>
      showDailyLightbox(entry.images, index),
    );
    if (url) {
      const img = el("img");
      img.src = url;
      img.alt = image.name || entry.title;
      img.loading = "lazy";
      tile.append(img);
    } else {
      tile.append(svg("image"));
    }
    grid.append(tile);
  });
  return grid;
}
// 日程中的记录直接弹窗查看，保持当前月份、滚动位置和所选日期。
function showDailyEntry(entry) {
  const dateString = entry.entryDate || entry.entry_date || dailyDateString();
  const detail = dateString === dailyDateString() ? "今天的记录" : "当日记录";
  showModal(detail, (body) => {
    const content = el("article", "daily-detail");
    const meta = el("div", "daily-detail-meta");
    meta.append(
      el("span", "", `${dateString} ${dailyTime(entry.createdAt || entry.created_at)}`),
      el("span", "", entry.area === "work" ? "工作" : "生活"),
    );
    content.append(meta);
    const summary = (entry.summary || "").trim();
    const text = (entry.body || "").trim();
    if (summary && summary !== text) content.append(el("p", "daily-detail-summary", summary));
    if (text) content.append(el("p", "daily-detail-body", text));
    if (!summary && !text) content.append(el("p", "daily-detail-body", entry.title || "（无文字内容）"));
    if (entry.images?.length) content.append(renderDailyImageGrid(entry));
    body.append(content);
    body.append(actionsRow(
      ...(writable() ? [button("编辑记录", "button soft", () => editDailyEntry(entry))] : []),
      button("关闭", "button secondary", closeModal),
    ));
  });
}
function renderRecognition(entry, ai) {
  const block = el("div", "daily-ai done");
  if (ai.summary) block.append(el("p", "daily-summary", ai.summary));
  if (ai.tags?.length) {
    const tags = el("div", "daily-tags");
    for (const tag of ai.tags) tags.append(el("span", "daily-tag", `#${tag}`));
    block.append(tags);
  }
  if (ai.suggestions?.length) {
    const suggestions = el("div", "daily-suggestions");
    suggestions.append(el("p", "daily-suggestion-title", "识别到的待办建议"));
    const converted = new Set(entry.meta?.convertedSuggestions || []);
    for (const text of ai.suggestions) {
      const chip = button("", "daily-suggestion", () =>
        convertSuggestion(entry, text),
      );
      chip.append(svg("plus", true), el("span", "", text));
      if (converted.has(text)) {
        chip.disabled = true;
        chip.classList.add("converted");
        chip.replaceChildren(el("span", "", `已加入待办：${text}`));
      }
      suggestions.append(chip);
    }
    block.append(suggestions);
  }
  if (writable() && !ai.applied) {
    block.append(
      button("采用识别的标题与摘要", "text-button", () =>
        applyDailyRecognition(entry),
      ),
    );
  }
  return block;
}
function renderDailyEntryActions(entry, ai) {
  const actions = el("div", "daily-card-footer");
  if (ai.status === "done" && visionEnabled())
    actions.append(
      button("重新识别", "text-button", () => recognizeDailyEntry(entry)),
    );
  // 把一条今日记录变成待办：标题/正文带入，日期取记录当天，
  // 之后就在「待完成 → 进行中 → 已完成」的流程里流转。
  if (writable()) {
    const entryDate = entry.entryDate || entry.entry_date || dailyDateString();
    const already = (state.items || []).some(
      (item) =>
        item.kind === "task" &&
        item.meta?.fromDailyEntry === entry.id,
    );
    const promote = button(
      already ? "已在待办中" : "设为待办",
      "text-button",
      () => {
        if (!ensureWrite() || already) return;
        // 转为待办时才需要标题：给用户一个填写框，默认带上内容开头。
        const suggestion = snippetOf(
          { body: entry.body, summary: entry.summary },
          60,
        );
        showModal("设为待办", (body) => {
          const form = el("form", "editor-main");
          const title = input("text", "这件待办要做什么", suggestion === "（无内容）" ? "" : suggestion);
          title.maxLength = 200;
          title.required = true;
          const due = input("date", "", entryDate);
          const error = el("p", "form-error");
          error.setAttribute("role", "alert");
          const save = el("button", "button primary", "创建待办");
          save.type = "submit";
          form.append(
            labelField("标题", title),
            labelField("计划日期", due),
            error,
            actionsRow(
              button("取消", "button secondary", closeModal),
              save,
            ),
          );
          form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const value = title.value.trim();
            if (!value) {
              error.textContent = "请填写待办标题";
              return;
            }
            save.disabled = true;
            try {
              await saveItem({
                kind: "task",
                area: entry.area || state.area,
                title: value.slice(0, 200),
                body: entry.body || entry.summary || "",
                status: "active",
                meta: { due: due.value || entryDate, fromDailyEntry: entry.id },
              });
              closeModal();
              toast("已设为待办");
            } catch (err) {
              error.textContent = errorMessage(err);
              save.disabled = false;
            }
          });
          body.append(form);
        }, "从今日记录创建");
      },
    );
    promote.disabled = already;
    actions.append(promote);
  }
  actions.append(
    button("编辑", "text-button", () => editDailyEntry(entry)),
    button("删除", "text-button danger-text", () => deleteDailyEntry(entry)),
  );
  return actions;
}
function showDailyLightbox(images, index) {
  let current = index;
  // Escape 已由 <dialog> 的 cancel 事件统一处理（见 modal 监听），这里只绑定
  // 方向键；监听仅在灯箱打开期间生效，关闭时经 modalCleanup 移除，不泄漏到其他视图。
  let onKeyDown = null;
  showModal("图片预览", (body) => {
    const wrap = el("div", "daily-lightbox");
    const img = el("img");
    const counter = el("span", "daily-lightbox-count");
    const renderImage = () => {
      const url = dailyImageURL(images[current]);
      if (url) img.src = url;
      img.alt = images[current]?.name || "记录图片";
      counter.textContent = `${current + 1} / ${images.length}`;
    };
    renderImage();
    wrap.append(img);
    body.append(wrap, counter);
    if (images.length > 1) {
      const goPrev = () => {
        current = (current - 1 + images.length) % images.length;
        renderImage();
      };
      const goNext = () => {
        current = (current + 1) % images.length;
        renderImage();
      };
      const controls = el("div", "daily-lightbox-controls");
      const prev = button("上一张", "button secondary", goPrev);
      const next = button("下一张", "button primary", goNext);
      controls.append(prev, next);
      body.append(controls);
      onKeyDown = (event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          goPrev();
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          goNext();
        }
      };
      document.addEventListener("keydown", onKeyDown);
      modalCleanup = () => document.removeEventListener("keydown", onKeyDown);
    }
  });
}
async function recognizeDailyEntry(entry, { silent = false } = {}) {
  if (!visionEnabled()) {
    toast("图片识别尚未连接，请联系管理员配置服务端密钥", true);
    return;
  }
  entry.meta = entry.meta || {};
  entry.meta.ai = { ...(entry.meta.ai || {}), status: "pending" };
  render();
  try {
    const result = await api(
      `/api/spaces/${state.space.id}/daily/entries/${entry.id}/recognize`,
      "POST",
      {},
    );
    upsertDailyEntry(result.entry);
    render();
    if (!silent) toast("识别完成");
  } catch (err) {
    entry.meta.ai.status = "failed";
    render();
    toast(errorMessage(err), true);
  }
}
async function applyDailyRecognition(entry) {
  if (state.demo) return;
  try {
    const result = await api(
      `/api/spaces/${state.space.id}/daily/entries/${entry.id}`,
      "PATCH",
      { applyAi: true },
    );
    upsertDailyEntry(result.entry);
    render();
    toast("已采用识别结果");
  } catch (err) {
    toast(errorMessage(err), true);
  }
}
async function convertSuggestion(entry, text) {
  if (!ensureWrite()) return;
  try {
    if (!state.demo) {
      await api(`/api/spaces/${state.space.id}/items`, "POST", {
        kind: "task",
        area: entry.area,
        title: text,
        body: "",
        // 前端状态机与编辑器只产出 active/done，待办初始状态必须与之一致
        status: "active",
        meta: { source: "daily-suggestion", entryId: entry.id },
      });
      const converted = [
        ...new Set([...(entry.meta?.convertedSuggestions || []), text]),
      ];
      const result = await api(
        `/api/spaces/${state.space.id}/daily/entries/${entry.id}`,
        "PATCH",
        { convertedSuggestions: converted },
      );
      upsertDailyEntry(result.entry);
    } else {
      entry.meta = entry.meta || {};
      entry.meta.convertedSuggestions = [
        ...new Set([...(entry.meta.convertedSuggestions || []), text]),
      ];
    }
    render();
    toast("已加入待办事项");
  } catch (err) {
    toast(errorMessage(err), true);
  }
}
function editDailyEntry(entry) {
  showModal("编辑这条记录", (body) => {
    const form = el("form", "editor-main");
    // 今日记录不再有标题：标题字段由正文首句自动派生，界面上不出现。
    const summary = el("textarea");
    summary.placeholder = "一句话说明这条记录（可留空）";
    summary.value = entry.summary || "";
    summary.maxLength = 1000;
    const note = el("textarea");
    note.placeholder = "附言";
    note.value = entry.body || "";
    note.maxLength = 100000;
    const area = select(
      [
        ["work", "工作"],
        ["life", "生活"],
      ],
      entry.area,
    );
    form.append(
      labelField("摘要", summary),
      labelField("附言", note),
      labelField("所属分区", area),
    );
    const error = el("p", "form-error");
    const save = el("button", "button primary", "保存修改");
    save.type = "submit";
    form.append(
      error,
      actionsRow(button("取消", "button secondary", closeModal), save),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const content = note.value.trim();
      if (!content && !summary.value.trim() && !(entry.images || []).length) {
        error.textContent = "写点什么，或上传一张图片";
        return;
      }
      save.disabled = true;
      try {
        const payload = {
          // 标题由内容首句自动派生，保持列表与搜索可用。
          title: (content.split(/[。！？\n]/)[0] || content || "处理和附件记录").slice(0, 200),
          summary: summary.value.trim(),
          body: content,
          area: area.value,
        };
        if (state.demo) {
          Object.assign(entry, payload);
          entry.updatedAt = new Date().toISOString();
        } else {
          const result = await api(
            `/api/spaces/${state.space.id}/daily/entries/${entry.id}`,
            "PATCH",
            payload,
          );
          upsertDailyEntry(result.entry);
        }
        closeModal();
        render();
        toast("记录已更新");
      } catch (err) {
        error.textContent = errorMessage(err);
        save.disabled = false;
      }
    });
    body.append(form);
  });
}
function deleteDailyEntry(entry) {
  showModal("删除这条记录？", (body) => {
    body.append(
      el(
        "p",
        "modal-body-note",
        `「${entry.title}」删除后无法恢复，${entry.images?.length ? "对应的原始图片也会删除。" : ""}`,
      ),
    );
    const error = el("p", "form-error");
    const remove = button("确认删除", "button danger", async () => {
      remove.disabled = true;
      try {
        if (!state.demo)
          await api(
            `/api/spaces/${state.space.id}/daily/entries/${entry.id}`,
            "DELETE",
          );
        else
          state.demoDaily[state.space.id] = state.demoDaily[
            state.space.id
          ].filter((item) => item.id !== entry.id);
        state.daily.entries = state.daily.entries.filter(
          (item) => item.id !== entry.id,
        );
        for (const image of entry.images || []) {
          if (image.localURL) {
            URL.revokeObjectURL(image.localURL);
            state.objectURLs.delete(image.localURL);
          }
        }
        closeModal();
        render();
        toast(state.demo ? "已从本地演示移除" : "记录已删除");
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
async function generateDailyDigest(date) {
  if (!visionEnabled()) {
    toast("每日小结尚未连接服务端模型", true);
    return;
  }
  try {
    const result = await api(
      `/api/spaces/${state.space.id}/daily/digest`,
      "POST",
      { date },
    );
    state.daily.digests[date] = result.digest.digest;
    render();
    toast("今日小结已生成");
  } catch (err) {
    toast(errorMessage(err), true);
  }
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
      `录音需要麦克风权限与 HTTPS 连接。音频只在点击保存时上传到当前空间，每份文件最大 ${MAX_UPLOAD_MB} MB。`,
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
          save.disabled = !blob.size || blob.size > MAX_UPLOAD_MB * 1024 * 1024;
          status.textContent = save.disabled
            ? `录音超过 ${MAX_UPLOAD_MB} MB，请缩短后重试`
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
          // 到达上限前 1 MB 自动停止，给编码收尾留出余量，避免录音超限
          if (chunks.reduce((sum, b) => sum + b.size, 0) > (MAX_UPLOAD_MB - 1) * 1024 * 1024)
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
  // 注册时可填手机号；登录时同一输入框接受用户名或手机号。
  $("#phone-label").hidden = !state.register;
  $("#auth-identifier-label").textContent = state.register
    ? "用户名"
    : "用户名或手机号";
  $("#auth-email").placeholder = state.register
    ? "3-30 位字母、数字、下划线"
    : "输入用户名或手机号";
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
    const identifier = $("#auth-email").value.trim();
    const data = { password: $("#auth-password").value };
    if (state.register) {
      if (state.registrationMode === null)
        throw new Error("暂时无法确认注册设置，请刷新页面后重试。");
      data.username = identifier;
      const phone = $("#auth-phone").value.trim();
      if (phone) data.phone = phone;
      data.name = $("#auth-name").value.trim();
      if (state.registrationMode === "invite")
        data.invitationCode = $("#auth-invitation").value.trim();
    } else {
      data.identifier = identifier;
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
    state.area = requestedArea();
    if (finishAuthReturn()) return;
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
$("#search-button").addEventListener("click", openSearchView);
$("#notify-button").addEventListener("click", showNotifications);
$("#profile-button").addEventListener("click", showProfile);
$("#account-settings-button").addEventListener("click", showAccountSettings);
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
$("#daily-file-input").addEventListener("change", (event) =>
  onDailyFiles(event.target.files),
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
    openSearchView();
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
  bindHaptics();
  syncDrawerLayout();
  updateVisualViewport();
  updateDate();
  updateAuthReturnNote();
  await loadConfiguration();
  try {
    const result = await api("/api/me");
    state.user = result.user;
    state.demo = false;
    if (finishAuthReturn()) return;
    await enterApp();
  } catch (err) {
    showAuth();
    if (err.status !== 401) $("#auth-error").textContent = errorMessage(err);
  }
}
init();
