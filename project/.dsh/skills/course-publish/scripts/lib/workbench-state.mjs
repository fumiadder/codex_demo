// 工作台状态聚合（纯函数，可单测）：把 chapter-plan / edition8-progress / staging / publish-state 归并成统一章节视图。
import { STAGE } from "./staging.mjs";

// 状态优先级：deleted > published > approved > staged > partial > merged > blocked > pending
export function deriveStatus({ progress, staging, publish }) {
  if (publish && publish.deleted) return "deleted";
  if ((staging && staging.status === STAGE.PUBLISHED) || (publish && publish.published)) return "published";
  if (staging && staging.status === STAGE.APPROVED) return "approved";
  if (staging && staging.status === STAGE.STAGED) return "staged";
  if (staging && staging.status === STAGE.PARTIAL) return "partial";
  if (progress && progress.blocked) return "blocked";
  if (progress && progress.merged) return "merged";
  return "pending";
}

export function buildChapterView(plan, progress, stagingMap, publishState) {
  return plan.chapters.map((c) => {
    const slug = c.slug;
    const stg = (stagingMap && stagingMap[slug]) || null;
    const pub = publishState[slug] || null;
    const prog = progress[slug] || null;
    return {
      slug,
      title: c.titleOverride || c.webTitle,
      track: c.track,
      day: c.day,
      webIndex: c.webIndex,
      status: deriveStatus({ progress: prog, staging: stg, publish: pub }),
      imageCount: stg ? stg.imageCount : null,
      failedImages: stg && Array.isArray(stg.failed) ? stg.failed.length : 0,
      postId: pub ? String(pub.postId || "") : "",
      postUrl: pub && pub.postUrl ? pub.postUrl : "",
      previewPath: stg && stg.previewPath ? stg.previewPath : "",
      stagedAt: stg && stg.stagedAt ? stg.stagedAt : "",
      approvedAt: stg && stg.approvedAt ? stg.approvedAt : "",
    };
  });
}

// 工作台统计徽标
export function summarize(view) {
  const sum = { total: view.length };
  for (const row of view) sum[row.status] = (sum[row.status] || 0) + 1;
  return sum;
}
