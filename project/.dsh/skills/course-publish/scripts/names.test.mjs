import { test } from "node:test";
import assert from "node:assert/strict";
import { normalize, extractIndex, cnToInt, matchChapters, diceCoeff, diffNewChapters } from "./lib/names.mjs";

test("cnToInt", () => {
  assert.equal(cnToInt("一"), 1);
  assert.equal(cnToInt("十"), 10);
  assert.equal(cnToInt("十一"), 11);
  assert.equal(cnToInt("二十三"), 23);
  assert.equal(cnToInt("一百零三"), 103);
  assert.equal(cnToInt("12"), 12);
});

test("normalize strips decorations", () => {
  assert.equal(normalize("第 3 章：Agent 入门（上）【更新】"), "第3章agent入门上");
  assert.equal(normalize("０１－导论"), "01导论");
  assert.equal(normalize("０１－导论"), "01导论".replace("01", "01"));
});

test("extractIndex variants", () => {
  assert.deepEqual(extractIndex("第3章 Agent入门"), { num: 3, sub: null });
  assert.deepEqual(extractIndex("第十二讲 工具调用"), { num: 12, sub: null });
  assert.deepEqual(extractIndex("3-1 环境准备"), { num: 3, sub: 1 });
  assert.deepEqual(extractIndex("03、环境准备"), { num: 3, sub: null });
  assert.deepEqual(extractIndex("05 环境准备"), { num: 5, sub: null });
  assert.deepEqual(extractIndex("导论"), { num: null, sub: null });
});

test("golden: clean ordered match is high", () => {
  const web = [{ index: 1, title: "第1章 课程导论与环境准备" }, { index: 2, title: "第2章 第一个Agent" }, { index: 3, title: "第3章 工具调用实战" }];
  const share = [{ name: "01 课程导论与环境准备" }, { name: "02 第一个Agent" }, { name: "03 工具调用实战" }];
  const { pairs } = matchChapters(web, share);
  assert.equal(pairs[0].confidence, "high");
  assert.equal(pairs[1].confidence, "high");
  assert.equal(pairs[2].confidence, "high");
  assert.equal(pairs[0].shareName, "01 课程导论与环境准备");
});

test("golden: index misalignment must not force match", () => {
  const web = [{ index: 2, title: "第2章 提示工程" }];
  const share = [{ name: "03 提示工程" }]; // 名称一致但序号冲突
  const { pairs } = matchChapters(web, share);
  assert.ok(pairs[0].confidence !== "high", JSON.stringify(pairs[0]));
});

test("golden: totally different names resolve to none", () => {
  const web = [{ index: 1, title: "第1章 RAG 检索增强" }];
  const share = [{ name: "加餐：面试经验分享" }];
  const { pairs } = matchChapters(web, share);
  assert.equal(pairs[0].confidence, "none");
  assert.equal(pairs[0].shareName, null);
});

test("ambiguous similar titles flagged", () => {
  const web = [{ index: 1, title: "第1章 Agent入门" }, { index: 2, title: "第2章 Agent进阶" }];
  const share = [{ name: "Agent入门篇" }, { name: "Agent进阶篇" }];
  const { pairs } = matchChapters(web, share);
  for (const p of pairs) {
    assert.ok(["high", "ambiguous", "medium"].includes(p.confidence), JSON.stringify(p));
  }
  assert.notEqual(pairs[0].shareName, "Agent进阶篇");
});

test("装饰前缀的网盘名仍能提取序号并 high 匹配", () => {
  assert.deepEqual(extractIndex("【更新】01 导论"), { num: 1, sub: null });
  assert.deepEqual(extractIndex("[课件]第3章 RAG"), { num: 3, sub: null });
  const web = [
    { index: 1, title: "第1章 导论" },
    { index: 2, title: "第2章 提示工程" },
  ];
  const share = [{ name: "【更新】01 导论" }, { name: "[课件]02 提示工程实战" }];
  const pairs = matchChapters(web, share).pairs;
  assert.equal(pairs[0].confidence, "high", JSON.stringify(pairs[0]));
  assert.equal(pairs[0].shareName, "【更新】01 导论");
  assert.equal(pairs[1].confidence, "high", JSON.stringify(pairs[1]));
});

test("normalize 去除多个装饰块且保留圆括号内容", () => {
  assert.equal(normalize("【更新】[课件] 第1章 导论（上）"), "第1章导论上");
  assert.equal(normalize("〖资料〗Agent（进阶）"), "agent进阶");
});

test("序号冲突的同名章节必须被压低（不能 high）", () => {
  const web = [{ index: 1, title: "第1章 提示工程" }];
  const share = [{ name: "02 提示工程" }];
  const p = matchChapters(web, share).pairs[0];
  assert.notEqual(p.confidence, "high", "序号不一致时应进闸门: " + JSON.stringify(p));
});

test("diffNewChapters 幂等：重复扫描零新增", () => {
  const prev = [{ fsid: "1", name: "01 导论" }, { fsid: "2", name: "02 提示工程" }];
  const entries = [{ fsid: "1", name: "01 导论" }, { fsid: "2", name: "02 提示工程" }];
  assert.deepEqual(diffNewChapters(prev, entries), []);
});

test("diffNewChapters：只返回真正新增的文件夹", () => {
  const prev = [{ fsid: "1", name: "01 导论" }];
  const entries = [{ fsid: "1", name: "01 导论" }, { fsid: "2", name: "02 提示工程" }, { fsid: "3", name: "03 RAG" }];
  const got = diffNewChapters(prev, entries).map((e) => e.fsid);
  assert.deepEqual(got, ["2", "3"]);
});

test("diffNewChapters：改名（fsid 不变）不算新增，避免重复转存", () => {
  const prev = [{ fsid: "7", name: "03 旧名" }];
  const entries = [{ fsid: "7", name: "03 新名字" }];
  assert.deepEqual(diffNewChapters(prev, entries), []);
});

test("diffNewChapters：重新上传（新 fsid 同名）也不算新增", () => {
  const prev = [{ fsid: "7", name: "01 导论" }];
  const entries = [{ fsid: "99", name: "01 导论" }];
  assert.deepEqual(diffNewChapters(prev, entries), []);
});

test("diffNewChapters：装饰差异不影响名称判重", () => {
  const prev = [{ fsid: "7", name: "01 导论" }];
  const entries = [{ fsid: "88", name: "【更新】01 导论" }];
  assert.deepEqual(diffNewChapters(prev, entries), []);
});
