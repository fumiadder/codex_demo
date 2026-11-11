// 章节名称归一化与模糊匹配（安全闸门核心，纯函数可测试）

const CN_NUM = { 零:0, 一:1, 二:2, 两:2, 三:3, 四:4, 五:5, 六:6, 七:7, 八:8, 九:9, 十:10 };

export function cnToInt(s) {
  if (/^\d+$/.test(s)) return parseInt(s, 10);
  if (!s) return null;
  // 十一..九十九；一百零三 等
  if (s.includes("百")) {
    const [bai, rest] = s.split("百");
    const b = bai ? CN_NUM[bai] ?? 1 : 1;
    let n = b * 100;
    if (rest) {
      if (rest[0] === "零") n += CN_NUM[rest[1]] || 0;
      else if (rest.includes("十")) {
        const [s1, s2] = rest.split("十");
        n += (s1 ? CN_NUM[s1] : 1) * 10 + (CN_NUM[s2] || 0);
      } else n += CN_NUM[rest] || 0;
    }
    return n;
  }
  if (s.includes("十")) {
    const [a, b] = s.split("十");
    return (a ? CN_NUM[a] : 1) * 10 + (b ? CN_NUM[b] || 0 : 0);
  }
  return CN_NUM[s] ?? null;
}

// 全角→半角，NFKC，去空白/常见标点/装饰符，小写
export function normalize(s) {
  return (s || "")
    .normalize("NFKC")
    .replace(/[\u3000\s]+/g, "")
    .replace(/[【\[〖［][^】\]〗］]*[】\]〗］]/g, "") // 去掉全部方括号装饰注释（如【更新】[课件]〖资料〗）
    .replace(/[，。、：:；;！!？?·'"“”‘’~～—_＿\.\/\\|•・\-]+/g, "") // 连字符放末尾转义
    .replace(/[（）()]/g, "") // 圆括号只去括号，保留（上/下）等有效内容
    .toLowerCase();
}

// 去掉方括号装饰块（【更新】、[课件] 等），供序号提取前预处理
function stripDecor(s) {
  return (s || "").replace(/[【\[〖［][^】\]〗］]*[】\]〗］]/g, "").trimStart();
}

// 提取序号，如 "第3章", "第3讲", "03 ", "3-1", "3.1"；允许前面有装饰块
export function extractIndex(s) {
  const raw = stripDecor((s || "").trim());
  let m = raw.match(/第\s*([0-9]{1,3}|[零一二两三四五六七八九十百]{1,4})\s*[章节讲课时课篇回关部]/);
  if (m) {
    const n = /^\d+$/.test(m[1]) ? parseInt(m[1], 10) : cnToInt(m[1]);
    if (n != null) return { num: n, sub: null };
  }
  m = raw.match(/^\s*(\d{1,3})\s*[-－.]\s*(\d{1,3})(?![\d.])/);
  if (m) return { num: parseInt(m[1], 10), sub: parseInt(m[2], 10) };
  m = raw.match(/^\s*0*(\d{1,3})\s*[、.．,，\t ]/);
  if (m) return { num: parseInt(m[1], 10), sub: null };
  m = raw.match(/^\s*0*(\d{1,3})\s+\S/);
  if (m) return { num: parseInt(m[1], 10), sub: null };
  return { num: null, sub: null };
}

// 去掉序号前缀与"第x章/讲"等，只留标题主体（归一化后比较用）；先去装饰块
export function stripIndex(s) {
  return stripDecor(s || "")
    .replace(/第\s*[0-9零一二两三四五六七八九十百]{1,4}\s*[章节讲课时课篇回关部]/, "")
    .replace(/^\s*\d{1,3}\s*[-－.]\s*\d{1,3}[、.．,，\t ]?/, "")
    .replace(/^\s*0*\d{1,3}\s*[、.．,，\t ]/, "");
}

export function bigrams(s) {
  const set = new Set();
  for (let i = 0; i < s.length - 1; i++) set.add(s.slice(i, i + 2));
  return [...set];
}

export function diceCoeff(a, b) {
  if (!a || !b) return 0;
  if (a === b) return 1;
  const A = new Set(bigrams(a));
  const B = new Set(bigrams(b));
  let inter = 0;
  for (const g of A) if (B.has(g)) inter++;
  return (2 * inter) / (A.size + B.size);
}

export function levenshtein(a, b) {
  const m = a.length, n = b.length;
  if (!m) return n;
  if (!n) return m;
  const dp = Array.from({ length: m + 1 }, (_, i) => [i, ...Array(n).fill(0)]);
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost);
    }
  }
  return dp[m][n];
}

export function levRatio(a, b) {
  if (!a && !b) return 1;
  const d = levenshtein(a, b);
  return 1 - d / Math.max(a.length, b.length, 1);
}

// 综合相似度 0..1，输出各因子便于审计
export function scorePair(webTitle, shareName) {
  const nw = normalize(stripIndex(webTitle));
  const ns = normalize(stripIndex(shareName));
  const iw = extractIndex(webTitle);
  const is_ = extractIndex(shareName);
  const dice = diceCoeff(nw, ns);
  const lev = levRatio(nw, ns);
  let contain = 0;
  if (nw && ns && (nw.includes(ns) || ns.includes(nw))) {
    contain = Math.min(nw.length, ns.length) / Math.max(nw.length, ns.length);
  }
  let titleSim = Math.max(dice, lev * 0.92, contain);
  // 主体文本过短时（<=3字），要求更严，避免"导论"/"导言"误配
  if (Math.min(nw.length, ns.length) <= 3) {
    titleSim = Math.min(titleSim, lev >= 0.99 ? 1 : dice >= 0.99 ? 1 : titleSim * 0.7);
  }
  let indexAgree = null;
  if (iw.num != null && is_.num != null) {
    if (iw.num === is_.num && (iw.sub ?? null) === (is_.sub ?? null)) indexAgree = true;
    else indexAgree = false;
  }
  let score = titleSim;
  if (indexAgree === true) score = Math.max(score, 0.75 + 0.25 * titleSim);
  if (indexAgree === false) score = Math.min(score, titleSim > 0.95 ? 0.59 : 0.3);
  return { score: Number(score.toFixed(3)), dice: Number(dice.toFixed(3)), lev: Number(lev.toFixed(3)), contain: Number(contain.toFixed(3)), indexAgree, nw, ns };
}

// 阈值（保守，宁可多问不可错发）
export const THRESH = { high: 0.86, medium: 0.62 };

// 增量订阅核心：按 fsid 与归一化名双重判重，返回本次新增条目（纯函数，可测试）
// prevChapters: manifest 中上次记录的 [{fsid,name}]；entries: 本次分享顶层条目 [{fsid,name}]
export function diffNewChapters(prevChapters, entries) {
  const prevFsids = new Set((prevChapters || []).map((c) => String(c.fsid)));
  const prevNames = new Set((prevChapters || []).map((c) => normalize(c.name)));
  return (entries || []).filter(
    (e) => !prevFsids.has(String(e.fsid)) && !prevNames.has(normalize(e.name)),
  );
}

// 一对一匹配。webChapters: [{index,title}], shareChapters: [{name}]
// 返回 { pairs:[{webIndex,webTitle,shareName,score,factors,confidence}], unresolvedWeb, unresolvedShare }
export function matchChapters(webChapters, shareChapters) {
  const candidates = [];
  webChapters.forEach((w, wi) => {
    shareChapters.forEach((s, si) => {
      const f = scorePair(w.title, s.name);
      candidates.push({ wi, si, ...f });
    });
  });
  candidates.sort((a, b) => b.score - a.score);

  const usedW = new Set(), usedS = new Set();
  const chosen = [];
  for (const c of candidates) {
    if (usedW.has(c.wi) || usedS.has(c.si)) continue;
    if (c.score < THRESH.medium) continue;
    chosen.push(c);
    usedW.add(c.wi);
    usedS.add(c.si);
  }

  const pairs = [];
  for (const w of webChapters) {
    const wi = webChapters.indexOf(w);
    const hit = chosen.find((c) => c.wi === wi);
    const confidence = !hit ? "none"
      : hit.score >= THRESH.high ? "high"
      : hit.score >= THRESH.medium ? "medium" : "none";
    // 歧义检测：high 阈值附近是否存在另一个分数接近的网盘候选
    const rivals = candidates
      .filter((c) => c.wi === wi && c.si !== (hit?.si ?? -1) && hit && c.score >= hit.score - 0.12)
      .map((c) => ({ shareName: shareChapters[c.si].name, score: c.score }));
    pairs.push({
      webIndex: w.index ?? wi + 1,
      webTitle: w.title,
      shareName: hit ? shareChapters[hit.si].name : null,
      score: hit ? hit.score : 0,
      confidence: rivals.length && confidence === "high" ? "ambiguous" : confidence,
      rivals,
      factors: hit ? { dice: hit.dice, lev: hit.lev, contain: hit.contain, indexAgree: hit.indexAgree } : null,
    });
  }
  return {
    pairs,
    unresolvedWeb: webChapters.filter((_, i) => !usedW.has(i)).map((w, i) => w),
    unresolvedShare: shareChapters.filter((_, i) => !usedS.has(i)).map((s) => s.name),
  };
}
