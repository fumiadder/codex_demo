// 极简 XML-RPC 客户端（覆盖 string/i4/boolean/base64/struct/array），内置微型 XML 解析器
function escXml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function unesc(s) {
  return String(s)
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&amp;/g, "&");
}

// --- 微型 XML 解析（仅服务于结构固定的 XML-RPC 响应）---
function parseXml(xml) {
  const tokens = xml.match(/<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<!--[\s\S]*?-->|<\/[^>]+>|<[^>]+>|[^<]+/g) || [];
  const root = { tag: "#root", children: [], text: "" };
  const stack = [root];
  for (const tok of tokens) {
    if (tok.startsWith("<?")) continue;
    if (tok.startsWith("<!--")) continue;
    if (tok.startsWith("<![CDATA[")) {
      stack[stack.length - 1].text += tok.slice(9, -3);
      continue;
    }
    if (tok.startsWith("</")) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    if (tok.startsWith("<")) {
      const selfClose = tok.endsWith("/>");
      const tag = tok.slice(1, selfClose ? -2 : -1).trim().split(/\s/)[0];
      const node = { tag, children: [], text: "", attrs: {} };
      stack[stack.length - 1].children.push(node);
      if (!selfClose) stack.push(node);
      continue;
    }
    stack[stack.length - 1].text += tok;
  }
  const byTag = (node, name, acc = []) => {
    for (const c of node.children) {
      if (c.tag === name) acc.push(c);
      byTag(c, name, acc);
    }
    return acc;
  };
  const textOf = (node) => {
    let t = node.text || "";
    for (const c of node.children) {
      if (c.tag === "string" || !c.children.length) t += c.text;
    }
    return unesc(t);
  };
  return { root, byTag, textOf };
}

function encodeValue(v) {
  if (v == null) return "<string></string>";
  if (typeof v === "boolean") return `<boolean>${v ? 1 : 0}</boolean>`;
  if (typeof v === "number") return `<i4>${Math.trunc(v)}</i4>`;
  if (v instanceof Uint8Array || Buffer.isBuffer(v)) {
    return `<base64>${Buffer.from(v).toString("base64")}</base64>`;
  }
  if (Array.isArray(v)) {
    return "<array><data>" + v.map((x) => "<value>" + encodeValue(x) + "</value>").join("") + "</data></array>";
  }
  if (typeof v === "object") {
    const members = Object.entries(v)
      .map(([k, val]) => `<member><name>${escXml(k)}</name><value>${encodeValue(val)}</value></member>`)
      .join("");
    return "<struct>" + members + "</struct>";
  }
  return "<string>" + escXml(v) + "</string>";
}

function decodeValue(valueNode, doc) {
  const typeNode = valueNode.children.find((c) => c.tag !== "#text");
  if (!typeNode) return unesc(valueNode.text || "");
  switch (typeNode.tag) {
    case "string":
      return doc.textOf(typeNode);
    case "boolean":
      return (typeNode.text || "").trim() === "1";
    case "i4":
    case "int":
    case "double":
      return Number((typeNode.text || "0").trim());
    case "base64":
      return Buffer.from((typeNode.text || "").replace(/\s/g, ""), "base64");
    case "dateTime.iso8601":
      return (typeNode.text || "").trim();
    case "array": {
      const data = typeNode.children.find((c) => c.tag === "data");
      return (data?.children || []).filter((c) => c.tag === "value").map((v) => decodeValue(v, doc));
    }
    case "struct": {
      const out = {};
      for (const m of typeNode.children.filter((c) => c.tag === "member")) {
        const nameNode = m.children.find((c) => c.tag === "name");
        const valNode = m.children.find((c) => c.tag === "value");
        out[unesecText(nameNode)] = valNode ? decodeValue(valNode, doc) : null;
      }
      return out;
    }
    default:
      return doc.textOf(typeNode);
  }
}

function unesecText(node) {
  return unesc((node?.text || "").trim());
}

export async function xmlrpcCall(url, method, params, { signal } = {}) {
  const body =
    `<?xml version="1.0"?><methodCall><methodName>${escXml(method)}</methodName><params>` +
    params.map((p) => "<param><value>" + encodeValue(p) + "</value></param>").join("") +
    "</params></methodCall>";
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "text/xml; charset=utf-8", "User-Agent": "course-publish-skill/0.1" },
    body,
    signal,
  });
  const text = await resp.text();
  if (!resp.ok) {
    const detail = text.trim() ? text.slice(0, 300) : "<空响应体>";
    throw new Error("XML-RPC HTTP " + resp.status + " [" + method + "] " + detail);
  }
  const doc = parseXml(text);
  const faultValue = doc.byTag(doc.root, "fault")[0]?.children.find((c) => c.tag === "value");
  if (faultValue) {
    const v = decodeValue(faultValue, doc);
    const err = new Error(`XML-RPC fault ${v.faultCode}: ${v.faultString}`);
    err.fault = v;
    throw err;
  }
  const firstParamValue = doc.byTag(doc.root, "param")[0]?.children.find((c) => c.tag === "value");
  return firstParamValue ? decodeValue(firstParamValue, doc) : null;
}
