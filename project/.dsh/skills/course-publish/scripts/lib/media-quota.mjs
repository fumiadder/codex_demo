// 博客园图片每日配额守卫：免费账号 500 张/天（VIP 1000），超限 newMediaObject 返回 403。
// 滚动/日切后自动恢复；本地按北京日期计数，到限即干净暂停，次日续传，避免把章节误判为失败。
import { promises as fs } from "node:fs";
import path from "node:path";

export const DEFAULT_DAILY_LIMIT = 500;

export class MediaQuotaError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = "MediaQuotaError";
    this.cause = cause;
  }
}

// 识别配额/403 类错误（XML-RPC 仅给 403，Web 通道给中文提示）
export function isQuotaError(e) {
  const msg = String((e && (e.message || e.fault?.faultString)) || e || "");
  return /只能上传|配额|quota|500\s*张|1000\s*张|403|forbidden/i.test(msg);
}

// 北京日期键（博客园为国内服务，按 Asia/Shanghai 切日）
export function dayKey(d = new Date()) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}

export function freshState(limit = DEFAULT_DAILY_LIMIT) {
  return { date: dayKey(), uploaded: 0, blocked: false, limit, lastError: null };
}

// 同日沿用，跨天重置
export function ensureToday(prev, limit = DEFAULT_DAILY_LIMIT) {
  if (prev && prev.date === dayKey()) return { ...freshState(prev.limit || limit), ...prev, date: dayKey() };
  return freshState(limit);
}

export function canUpload(q) {
  return !!q && q.date === dayKey() && !q.blocked && q.uploaded < q.limit;
}

// 持久化配额追踪器
export class QuotaTracker {
  constructor(file, { limit = Number(process.env.CNBLOGS_DAILY_MEDIA_LIMIT) || DEFAULT_DAILY_LIMIT } = {}) {
    this.file = file;
    this.ledgerFile = path.join(path.dirname(file), "media-uploads.jsonl");
    this.limit = limit;
    this.state = freshState(limit);
  }

  async load() {
    try {
      const raw = JSON.parse(await fs.readFile(this.file, "utf8"));
      this.state = ensureToday(raw, this.limit);
    } catch { this.state = freshState(this.limit); }
    return this.state;
  }

  async save() { await fs.writeFile(this.file, JSON.stringify(this.state, null, 2)); }

  canUpload() { return canUpload(this.state); }

  async recordUpload(detail = {}) {
    this.state.uploaded += 1;
    await this.save();
    const entry = { at: new Date().toISOString(), date: dayKey(), status: "uploaded", ...detail };
    try { await fs.appendFile(this.ledgerFile, JSON.stringify(entry) + "\n"); } catch { /* 账本失败不能阻断已成功的上传 */ }
  }

  async markBlocked(err) {
    this.state.blocked = true;
    this.state.lastError = String((err && err.message) || err).slice(0, 200);
    await this.save();
  }
}
