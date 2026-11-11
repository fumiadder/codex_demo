// 安全闸门：非高置信匹配一律挂起，由 DSH agent 询问用户后以 decisions 文件回放
import { writeFileSync, readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { paths } from "./paths.mjs";

// 生成待确认清单。pairs: matchChapters 的输出；shareChapters: [{name}]
export function buildPendingGates(courseId, pairs, shareChapters, unmatchedShare = []) {
  const gates = [];
  for (const p of pairs) {
    if (p.confidence === "high") continue;
    gates.push({
      type: "chapter",
      webIndex: p.webIndex,
      webTitle: p.webTitle,
      currentMatch: p.shareName,
      confidence: p.confidence, // medium | ambiguous | none
      score: p.score,
      rivals: p.rivals || [],
      options: [
        ...(p.shareName ? [{ action: "accept", shareName: p.shareName, label: "采用当前匹配：" + p.shareName }] : []),
        ...shareChapters.map((s) => ({ action: "map", shareName: s.name, label: "手动指定 → " + s.name })),
        { action: "skip", shareName: null, label: "跳过本章（网盘暂无对应内容）" },
      ],
    });
  }
  // 网盘里存在但网站目录无对应的文件夹（如加餐/资料包），提示用户决定
  const matchedNames = new Set(pairs.map((p) => p.shareName).filter(Boolean));
  for (const name of unmatchedShare) {
    if (matchedNames.has(name)) continue;
    gates.push({
      type: "orphan-share",
      shareName: name,
      message: "网盘文件夹在网站目录中无对应章节，默认不发布；如需发布请手动指定到某 webIndex",
      options: [
        ...shareChapters.map((s) => ({ action: "map", shareName: s.name, label: "手动指定 → " + s.name })),
        { action: "skip", shareName: null, label: "忽略，不发布" },
      ],
    });
  }
  const file = path.join(paths.stateDir, courseId + ".pending-gates.json");
  writeFileSync(file, JSON.stringify({ courseId, gates, createdAt: new Date().toISOString() }, null, 2));
  return { file, gates };
}

// 读取用户决策（agent 询问后写出的文件）：[{webIndex, action, shareName?}]
export function loadDecisions(courseId, decisionsFile) {
  if (!decisionsFile || !existsSync(decisionsFile)) return null;
  const raw = JSON.parse(readFileSync(decisionsFile, "utf8"));
  const arr = Array.isArray(raw) ? raw : raw.decisions;
  if (!Array.isArray(arr)) throw new Error("决策文件格式应为数组或 {decisions:[...]}");
  validateDecisions(arr);
  return arr;
}

const VALID_ACTIONS = new Set(["accept", "map", "skip"]);

// 纯函数：校验决策结构；validShareNames 给定时，map 的 shareName 必须真实存在，防止拼写错误错配
export function validateDecisions(decisions, validShareNames = null) {
  const seen = new Set();
  for (const [i, d] of decisions.entries()) {
    const where = "决策第 " + (i + 1) + " 条";
    if (!d || typeof d !== "object") throw new Error(where + " 不是对象: " + JSON.stringify(d));
    if (!Number.isFinite(Number(d.webIndex))) throw new Error(where + " 缺少数字 webIndex");
    if (!VALID_ACTIONS.has(d.action)) {
      throw new Error(where + " action 非法（应为 accept|map|skip）: " + d.action);
    }
    if (d.action === "map" && !(typeof d.shareName === "string" && d.shareName.trim())) {
      throw new Error(where + " action=map 必须带非空 shareName");
    }
    const key = Number(d.webIndex);
    if (seen.has(key)) throw new Error("webIndex " + key + " 存在重复决策，每章只能有一条");
    seen.add(key);
    if (d.action === "map" && validShareNames && !validShareNames.includes(d.shareName)) {
      throw new Error(
        where + " 映射的网盘文件夹不存在：「" + d.shareName + "」；可选：" + validShareNames.slice(0, 10).join("、"),
      );
    }
  }
  return true;
}
