// 运行在 isolated world: 桥接 popup ↔ 页面主世界 injected.js
(function () {
  if (window.__fapiao_cs_loaded) return;
  window.__fapiao_cs_loaded = true;

  // 把 injected.js 作为 <script> 标签注入主世界
  const s = document.createElement('script');
  s.src = chrome.runtime.getURL('injected.js');
  s.onload = function () { s.remove(); };
  (document.head || document.documentElement).appendChild(s);

  let nextRid = 1;
  const pending = {};

  window.addEventListener('message', (ev) => {
    if (ev.source !== window) return;
    const m = ev.data;
    if (!m || m.__fapiao_to !== 'cs') return;
    const cb = pending[m.rid];
    if (cb) {
      delete pending[m.rid];
      cb(m);
    }
  });

  function callPage(payload, timeoutMs) {
    return new Promise((resolve) => {
      const rid = nextRid++;
      pending[rid] = resolve;
      window.postMessage(
        Object.assign({ __fapiao_to: 'page', rid: rid }, payload),
        '*'
      );
      setTimeout(() => {
        if (pending[rid]) {
          delete pending[rid];
          resolve({ error: 'timeout' });
        }
      }, timeoutMs || 30000);
    });
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    callPage(msg, msg.timeoutMs).then((r) => {
      try { sendResponse(r); } catch (e) { /* popup closed */ }
    });
    return true; // 异步响应
  });
})();
