/* 知序工作台 Service Worker：仅用于让站点可安装到手机桌面。
   不缓存任何私密 API、密码箱明文或上传内容，只处理离线兜底页与角标消息。 */
const OFFLINE_VERSION = "zhixu-offline-v1";

self.addEventListener("install", (event) => {
  // 立即接管，避免首装后仍受旧版本控制。
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // 不做任何缓存代理：私密数据必须始终走网络，防止离线留存。
  // 仅当导航请求失败时返回一个极简离线提示页。
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(
      () =>
        new Response(
          `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
           <title>知序 · 离线</title>
           <body style="margin:0;font-family:-apple-system,'PingFang SC',sans-serif;background:#f7f8f5;color:#24382f;
                       display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center">
           <div><h1 style="font-size:20px;font-weight:600">暂时连不上工作台</h1>
           <p style="color:#879089;font-size:14px">检查网络后再试；私密内容不会保存在本机。</p></div></body>`,
          { headers: { "Content-Type": "text/html; charset=utf-8" } },
        ),
    ),
  );
});

// 允许页面通过 SW 更新桌面角标（部分平台只认 SW 侧设置）
// 收到服务器推送：显示系统通知（锁屏/下拉栏可见），点击回到工作台。
self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload = { title: "知序", body: "" };
  try { payload = { ...payload, ...event.data.json() }; } catch { payload.body = event.data.text(); }
  event.waitUntil(
    self.registration.showNotification(payload.title || "知序", {
      body: payload.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      data: payload.data || {},
      tag: payload.data?.itemId || "zhixu",
      renotify: true,
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      if (clients.length) return clients[0].focus();
      return self.clients.openWindow("/");
    }),
  );
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "badge" && "setAppBadge" in self.registration) {
    if (data.count > 0) self.registration.setAppBadge(data.count);
    else self.registration.clearAppBadge();
  }
});
