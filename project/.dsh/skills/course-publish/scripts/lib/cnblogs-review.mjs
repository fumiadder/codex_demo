// 博客园审核/公开可见性的纯函数：供审核巡检脚本与单测共用。
export const REVIEW_LABELS = Object.freeze({
  0: "待审核",
  1: "已通过",
  2: "重审中",
});

export function reviewLabel(value) {
  if (value === null || value === undefined || value === "") return "后台无记录";
  const status = Number(value);
  if (Number.isInteger(status) && status in REVIEW_LABELS) return REVIEW_LABELS[status];
  return Number.isFinite(status) ? "未知状态=" + status : "后台无记录";
}

// backendFound=false 表示后台文章列表里没有这个 id（可能已被删除）。
export function classifyVisibility({ backendFound = true, reviewStatus, publicStatus }) {
  const status = Number(publicStatus);
  const publicOk = Number.isFinite(status) && status >= 200 && status < 300;
  if (!backendFound) {
    return { state: publicOk ? "public-unregistered" : "missing", label: publicOk ? "公开但后台无记录" : "后台无记录", publicOk };
  }
  const review = Number(reviewStatus);
  if (review === 1 && publicOk) return { state: "public", label: "公开", publicOk };
  if (review === 0) return { state: "pending", label: "待审核", publicOk };
  if (review === 2) return { state: "re-review", label: "重审中", publicOk };
  if (!publicOk) return { state: "hidden", label: "状态异常且不可访问", publicOk };
  return { state: "public", label: "公开", publicOk };
}

export function summarizeReviewRows(rows) {
  const summary = { total: 0, public: 0, pending: 0, reReview: 0, hidden: 0, missing: 0 };
  for (const row of rows || []) {
    summary.total++;
    const state = row.state || classifyVisibility(row).state;
    if (state === "public" || state === "public-unregistered") summary.public++;
    else if (state === "pending") summary.pending++;
    else if (state === "re-review") summary.reReview++;
    else if (state === "missing") summary.missing++;
    else summary.hidden++;
  }
  summary.issues = summary.total - summary.public;
  return summary;
}
