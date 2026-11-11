import { xmlrpcCall } from "./lib/xmlrpc.mjs";

globalThis.fetch = async (url, opts) => {
  if (!opts.body.includes("test.method")) throw new Error("bad method body");
  const xml = [
    '<?xml version="1.0"?><methodResponse><params><param><value><struct>',
    "<member><name>title</name><value><string>第3章 Agent 入门&amp;实战 &lt;上&gt;</string></value></member>",
    "<member><name>categories</name><value><array><data>",
    "<value><string>AI/agent</string></value><value><string>随笔</string></value>",
    "</data></array></value></member>",
    "<member><name>postid</name><value><i4>42</i4></value></member>",
    "<member><name>publish</name><value><boolean>1</boolean></value></member>",
    "</struct></value></param></params></methodResponse>",
  ].join("");
  return new Response(xml, { status: 200, headers: { "content-type": "text/xml" } });
};

const out = await xmlrpcCall("https://x", "test.method", [{ a: "中文&测试", n: 7, b: Buffer.from("img") }]);
console.log(JSON.stringify(out));
if (out.title !== "第3章 Agent 入门&实战 <上>") throw new Error("entity decode fail");
if (out.categories[0] !== "AI/agent" || out.postid !== 42 || out.publish !== true) throw new Error("struct decode fail");
console.log("XMLRPC-SELFTEST-OK");
