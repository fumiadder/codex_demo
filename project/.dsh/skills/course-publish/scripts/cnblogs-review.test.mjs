import test from "node:test";
import assert from "node:assert/strict";
import { classifyVisibility, reviewLabel, summarizeReviewRows } from "./lib/cnblogs-review.mjs";

test("审核状态标签覆盖待审、通过、重审与缺失", () => {
  assert.equal(reviewLabel(0), "待审核");
  assert.equal(reviewLabel(1), "已通过");
  assert.equal(reviewLabel(2), "重审中");
  assert.equal(reviewLabel(null), "后台无记录");
});

test("已通过且公开页 200 才算公开", () => {
  assert.deepEqual(classifyVisibility({ reviewStatus: 1, publicStatus: 200 }), { state: "public", label: "公开", publicOk: true });
  assert.equal(classifyVisibility({ reviewStatus: 1, publicStatus: 404 }).state, "hidden");
});

test("待审与重审不论 HTTP 结果都不能算公开", () => {
  assert.equal(classifyVisibility({ reviewStatus: 0, publicStatus: 404 }).state, "pending");
  assert.equal(classifyVisibility({ reviewStatus: 2, publicStatus: 404 }).state, "re-review");
  assert.equal(classifyVisibility({ reviewStatus: 0, publicStatus: 200 }).publicOk, true);
  assert.notEqual(classifyVisibility({ reviewStatus: 0, publicStatus: 200 }).state, "public");
});

test("后台缺失但公开页可访问单独标记，不与已删除混同", () => {
  assert.equal(classifyVisibility({ backendFound: false, publicStatus: 404 }).state, "missing");
  assert.equal(classifyVisibility({ backendFound: false, publicStatus: 200 }).state, "public-unregistered");
});

test("汇总把审核中、重审、隐藏和缺失都计入 issues", () => {
  const rows = [
    { state: "public" },
    { state: "pending" },
    { state: "re-review" },
    { state: "hidden" },
    { state: "missing" },
  ];
  assert.deepEqual(summarizeReviewRows(rows), { total: 5, public: 1, pending: 1, reReview: 1, hidden: 1, missing: 1, issues: 4 });
});
