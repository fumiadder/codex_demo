# 百度网盘接口与页面备忘（探针驱动，失效时以 baidu-probe 快照为准）

## 自有网盘
- `GET /api/list?dir=<urlencode>&order=time&desc=1`：列目录，errno=0 且 list[] 含 fs_id/server_filename/isdir。
- `POST /api/create?a=commit&bdstoken=<t>`：body `path=<dir>&isdir=1&block_list=[]` 建目录。
- `GET /api/filemetas?fsidlist=[id]&dlink=1&target=[id]`：取 dlink 下载地址（302 到 CDN，需带 cookie 与 netdisk UA）。

## 分享页
- `/share/verify`：提取码校验；URL 带 pwd 时页面自动完成。
- `/share/list?shareid=&uk=&...`：分享内容列表（顶层条目的 path=/）。
- `POST /share/transfer?shareid=&from=<uk>&bdstoken=&seid=`：body `fsidlist=[... ]&path=<目标目录>`；errno=0 成功，-140 风控。seid 从 share/list XHR 的 URL 参数或页面 yunData.SEID 提取（缺失时自动降级 DOM 转存）。
- bdstoken 来源：`window.locals.get('bdstoken')` / yunData.MYBDSTOKEN / 页面 HTML 正则。

## DOM 兜底选择器（探针校准）
- 行：`.file-item` / `.share-list .list-item` / `tr.file-item`，名称：`.file-name .text, .filename`。
- 提取码：`.pickpw_input` + `.pickpw-btn`；转存：文案“保存到”的按钮 + 弹窗目录树 + “确定/保存到这里”。

## 风控信号
body 文案出现 验证码/安全验证/滑块 → GateError(captcha)；transfer errno=-140 → GateError(risk)。
