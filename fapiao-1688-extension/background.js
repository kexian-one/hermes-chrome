// background.js — 淘宝批量开票 v4
// 流程:
//   1. 在 i.taobao.com/my_itaobao/invoice?active=notApply 扫描全部页,
//      收集 [{orderId, shop, date, amount}]
//   2. 每单开 worker tab 到 invoice-ua.taobao.com 申请页:
//      a. 找到 保存按钮 (没找到 → 视为已申请过)
//      b. 若页面有"收货地址"字段 (纸质):
//         点 "展开更多地址" → 等 .e-more-invoice 展开 → 点第一个 .address-box
//      c. 点 保存
//      d. 关 tab
//   3. 结果可下载 CSV (订单号 / 商家 / 金额 / 状态[自动申请|需要人工])

function initState() {
  return {
    phase: 'idle',            // idle | running | done | aborted | error
    listTabId: null,
    queue: [],                // [{ orderId, shop, date, amount }] - 边扫边长
    cursor: 0,
    results: [],              // [{ orderId, shop, amount, taxName, taxId, status, message }]
    abort: false,
    paused: false,            // 暂停标记: 主循环遇到会原地 wait,可恢复
    scanDone: false,
    startedAt: null,
    errorMessage: null,
    scanProgress: { page: 0, found: 0 },
    scanLog: []
  };
}
let state = initState();

function notify() {
  chrome.runtime.sendMessage({ type: 'TB_STATE', state }, () => {
    if (chrome.runtime.lastError) { /* popup closed */ }
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (!msg || !msg.type) return sendResponse({ error: 'no type' });
      switch (msg.type) {
        case 'TB_GET_STATE':
          return sendResponse({ state });

        case 'TB_SCAN_ALL': {
          // 现在 = 开始(扫一页 → 申请这页所有单 → 下一页 → 循环)
          if (state.phase === 'running') return sendResponse({ error: '已在运行' });
          sendResponse({ ok: true });
          runAll(msg.tabId);
          return;
        }

        case 'TB_PAUSE':
          state.paused = true;
          notify();
          return sendResponse({ ok: true });

        case 'TB_RESUME':
          state.paused = false;
          notify();
          return sendResponse({ ok: true });

        case 'TB_STOP':
          state.abort = true;
          state.paused = false;
          return sendResponse({ ok: true });

        case 'TB_RESET':
          if (state.phase === 'running') state.abort = true;
          state = initState();
          notify();
          return sendResponse({ ok: true });

        default:
          return sendResponse({ error: 'unknown type: ' + msg.type });
      }
    } catch (e) {
      sendResponse({ error: String((e && e.message) || e) });
    }
  })();
  return true;
});

// ───────── 主循环: 扫一页 → 申请这页所有单 → 下一页 ─────────

async function runAll(tabId) {
  state = initState();
  state.listTabId = tabId;
  state.phase = 'running';
  state.startedAt = Date.now();
  notify();

  const seen = new Set();
  const MAX_PAGES = 100;

  try {
    for (let p = 1; p <= MAX_PAGES; p++) {
      if (state.abort) break;
      state.scanProgress.page = p;
      notify();

      // ── 1. 扫当前页 ──
      await waitForOrdersPresent(tabId, 8000);
      const r = await execCode(tabId, SCAN_PAGE_CODE);
      let data;
      try { data = JSON.parse(r[0] || '{}'); }
      catch (e) { data = { orders: [], hasNext: false }; }

      const orders = data.orders || [];
      let added = 0;
      for (const o of orders) {
        if (!seen.has(o.orderId)) {
          seen.add(o.orderId);
          state.queue.push({
            orderId: String(o.orderId),
            shop: o.shop || '',
            date: o.date || '',
            amount: o.amount || ''
          });
          added++;
        }
      }
      state.scanProgress.found = state.queue.length;
      state.scanLog.push('第 ' + p + ' 页 · 本页 ' + orders.length + ' · 新增 ' + added);
      notify();

      // ── 2. 把这页新增的单子全部跑完(state.cursor 追到 state.queue.length) ──
      while (state.cursor < state.queue.length && !state.abort) {
        // 暂停检查点 — 每单开始前查
        while (state.paused && !state.abort) await sleep(500);
        if (state.abort) break;

        const o = state.queue[state.cursor];
        const result = await processSingleOrder(o);
        state.results.push(result);
        state.cursor++;
        notify();
        if (!state.abort && state.cursor < state.queue.length) {
          await sleep(2000 + Math.random() * 1500);
        }
      }
      if (state.abort) break;

      // 翻页前也查一次暂停
      while (state.paused && !state.abort) await sleep(500);
      if (state.abort) break;

      // ── 3. 如果有下一页,点过去 ──
      if (!data.hasNext) {
        state.scanLog.push('已到末页');
        break;
      }
      const before = data.firstOrderId || '';
      const cr = await execCode(tabId, CLICK_NEXT_CODE);
      let cObj;
      try { cObj = JSON.parse(cr[0] || '{}'); }
      catch (e) { cObj = { clicked: false }; }
      if (!cObj.clicked) {
        state.scanLog.push('下一页按钮不可点,停止');
        break;
      }
      const changed = await waitForFirstIdChange(tabId, before, 15000);
      if (!changed) {
        state.scanLog.push('翻页后内容未变化(15s),停止');
        break;
      }
      await sleep(600);
    }

    state.scanDone = true;
    state.phase = state.abort ? 'aborted' : 'done';
    notify();
  } catch (e) {
    state.phase = 'error';
    state.errorMessage = String((e && e.message) || e);
    notify();
  }
}

// 处理单个订单: 开 worker tab → 等加载 → 点保存(纸质则先选地址) → 关 tab
async function processSingleOrder(o) {
  let result = {
    orderId: o.orderId,
    shop: o.shop || '',
    amount: o.amount || '',
    taxName: '',
    taxId: '',
    status: 'error',
    message: ''
  };
  let workerTabId = null;
  try {
    const tab = await new Promise((res) =>
      chrome.tabs.create({ url: APPLY_URL(o.orderId), active: false }, res)
    );
    workerTabId = tab.id;
    await waitForComplete(workerTabId, 30000);
    await sleep(1800);
    const r = await runApplyWorker(workerTabId);
    result.status = r.status;
    result.message = r.message;
    result.taxName = r.taxName || '';
    result.taxId = r.taxId || '';
  } catch (e) {
    result.message = String((e && e.message) || e);
  } finally {
    if (workerTabId != null) {
      await new Promise((res) =>
        chrome.tabs.remove(workerTabId, () => {
          if (chrome.runtime.lastError) { /* already closed */ }
          res();
        })
      );
    }
  }
  return result;
}

const SCAN_PAGE_CODE = `
(function() {
  const orders = [];
  let firstOrderId = '';
  const rows = document.querySelectorAll('tr.next-table-group-header');
  for (const row of rows) {
    const idEl = row.querySelector('[class*="group-header-title-number"]');
    if (!idEl) continue;
    const orderId = (idEl.textContent || '').trim();
    if (!/^\\d{10,}$/.test(orderId)) continue;
    if (!firstOrderId) firstOrderId = orderId;

    // 必须本订单 tbody 里有可点的"申请发票"按钮 — 没按钮的(只有订单详情)直接跳过
    const tbody = row.closest('tbody');
    if (!tbody) continue;
    let hasApplyBtn = false;
    const btns = tbody.querySelectorAll('button');
    for (const b of btns) {
      const span = b.querySelector('.next-btn-helper');
      const t = (span ? span.textContent : (b.textContent || '')).trim();
      if (t === '申请发票' && !b.disabled) { hasApplyBtn = true; break; }
    }
    if (!hasApplyBtn) continue;

    let shop = '';
    const logoBox = row.querySelector('[class*="group-header-logo"]');
    if (logoBox) {
      const divs = logoBox.querySelectorAll('div');
      for (const d of divs) {
        const t = (d.textContent || '').trim();
        if (t && !d.querySelector('img')) { shop = t; break; }
      }
    }

    let date = '';
    const timeBox = row.querySelector('[class*="group-header-time"]');
    if (timeBox) {
      const tds = timeBox.querySelectorAll('div');
      if (tds.length >= 2) date = (tds[1].textContent || '').trim();
    }

    // 金额: 同一 tbody 里的 [class*="sum--"] (开票金额)
    let amount = '';
    const sumEl = tbody.querySelector('[class*="sum--"]');
    if (sumEl) amount = (sumEl.textContent || '').trim();

    orders.push({ orderId, shop, date, amount });
  }

  let hasNext = false;
  const nextBtn = document.querySelector('button.next-pagination-item.next-next');
  if (nextBtn) {
    const disabled = nextBtn.disabled ||
      nextBtn.classList.contains('next-btn-disabled') ||
      nextBtn.getAttribute('aria-disabled') === 'true';
    hasNext = !disabled;
  }

  return JSON.stringify({ orders, hasNext, firstOrderId });
})();
`;

const CLICK_NEXT_CODE = `
(function() {
  const btn = document.querySelector('button.next-pagination-item.next-next');
  if (!btn) return JSON.stringify({ clicked: false, reason: 'not-found' });
  if (btn.disabled ||
      btn.classList.contains('next-btn-disabled') ||
      btn.getAttribute('aria-disabled') === 'true') {
    return JSON.stringify({ clicked: false, reason: 'disabled' });
  }
  btn.click();
  return JSON.stringify({ clicked: true });
})();
`;

const FIRST_ID_CODE = `
(function() {
  const idEl = document.querySelector('tr.next-table-group-header [class*="group-header-title-number"]');
  return idEl ? (idEl.textContent || '').trim() : '';
})();
`;

async function waitForFirstIdChange(tabId, before, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await sleep(500);
    try {
      const r = await execCode(tabId, FIRST_ID_CODE);
      const fid = r[0] || '';
      if (fid && fid !== before) return true;
    } catch (e) {}
  }
  return false;
}

async function waitForOrdersPresent(tabId, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await execCode(tabId, FIRST_ID_CODE);
      if (r[0]) return true;
    } catch (e) {}
    await sleep(400);
  }
  return false;
}

// ───────── 批量提交 (worker tab → invoice-ua) ─────────

const APPLY_URL = (oid) =>
  'https://invoice-ua.taobao.com/detail/pc?spm=tbpc.boughtlist.order_op' +
  '#/apply?orderId=' + oid;

async function runQueue() {
  try {
    while (state.cursor < state.queue.length && !state.abort) {
      const o = state.queue[state.cursor];
      let result = {
        orderId: o.orderId,
        shop: o.shop || '',
        amount: o.amount || '',
        taxName: '',
        taxId: '',
        status: 'error',
        message: ''
      };
      let workerTabId = null;
      try {
        const tab = await new Promise((r) =>
          chrome.tabs.create({ url: APPLY_URL(o.orderId), active: false }, r)
        );
        workerTabId = tab.id;
        await waitForComplete(workerTabId, 30000);
        await sleep(1800); // SPA settle
        const r = await runApplyWorker(workerTabId);
        result.status = r.status;
        result.message = r.message;
        result.taxName = r.taxName || '';
        result.taxId = r.taxId || '';
      } catch (e) {
        result.message = String((e && e.message) || e);
      } finally {
        if (workerTabId != null) {
          await new Promise((res) =>
            chrome.tabs.remove(workerTabId, () => {
              if (chrome.runtime.lastError) { /* already closed */ }
              res();
            })
          );
        }
      }
      state.results.push(result);
      state.cursor++;
      notify();

      if (!state.abort && state.cursor < state.queue.length) {
        await sleep(2000 + Math.random() * 1500);
      }
    }
    state.phase = state.abort ? 'aborted' : 'done';
    notify();
  } catch (e) {
    state.phase = 'error';
    state.errorMessage = String((e && e.message) || e);
    notify();
  }
}

function waitForComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    let done = false;
    function onUpdated(id, info) {
      if (id === tabId && info.status === 'complete') finish(resolve);
    }
    function onRemoved(id) {
      if (id === tabId) finish(() => reject(new Error('worker tab closed')));
    }
    function finish(cb) {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
      cb();
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.onRemoved.addListener(onRemoved);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) return;
      if (tab && tab.status === 'complete') finish(resolve);
    });
    setTimeout(() => {
      if (!done) finish(() => reject(new Error('navigation timeout')));
    }, timeoutMs);
  });
}

// 注入到 worker tab 内的脚本,处理"保存"+"收货地址展开第一项"
const APPLY_KICKOFF = `
(function() {
  window.__tbApply = { phase: 'running', status: null, message: '' };

  function visible(el) {
    if (!el || el.disabled) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function findSaveButton() {
    let btns = document.querySelectorAll('button[title="保存"]');
    for (const b of btns) if (visible(b)) return b;
    btns = document.querySelectorAll('button.next-btn-primary');
    for (const b of btns) {
      if ((b.textContent || '').trim() === '保存' && visible(b)) return b;
    }
    return null;
  }

  function hasAddressField() {
    const labels = document.querySelectorAll('.form-label, label');
    for (const l of labels) {
      if ((l.textContent || '').indexOf('收货地址') >= 0) return true;
    }
    return false;
  }

  function getFieldValue(labelText) {
    const forms = document.querySelectorAll('.cloud-form');
    for (const f of forms) {
      const lbl = f.querySelector('.form-label');
      if (!lbl) continue;
      if ((lbl.textContent || '').indexOf(labelText) < 0) continue;
      const input = f.querySelector('.form-item input');
      if (input) return (input.value || '').trim();
    }
    return '';
  }

  (async () => {
    let taxName = '';
    let taxId = '';

    function done(extra) {
      window.__tbApply = Object.assign(
        { phase: 'done', status: '', message: '', taxName: taxName, taxId: taxId },
        extra || {}
      );
    }

    // 1. 等保存按钮出现 (一定会有,等够时间;实测 1-3s,留 25s 防慢网络)
    const start = Date.now();
    let saveBtn = null;
    while (Date.now() - start < 25000) {
      saveBtn = findSaveButton();
      // 顺便沿途采样发票抬头/税号 (字段可能比按钮先渲染好)
      if (!taxName) taxName = getFieldValue('发票抬头');
      if (!taxId) taxId = getFieldValue('纳税人识别号');
      if (saveBtn) break;
      await new Promise((r) => setTimeout(r, 400));
    }
    if (!saveBtn) {
      done({
        status: 'error',
        message: '25s 未渲染出保存按钮,页面加载异常'
      });
      return;
    }

    // 找到按钮后再补一次
    if (!taxName) taxName = getFieldValue('发票抬头');
    if (!taxId) taxId = getFieldValue('纳税人识别号');

    // 2. 检测纸质 (有"收货地址"字段) — 一旦发现纸质,直接跳过不操作
    if (hasAddressField()) {
      done({
        status: 'skipped-paper',
        message: '纸质票,需要手动处理'
      });
      return;
    }

    // 3. 电子发票 — 清旧响应标记 → 点保存 → 等 API 响应
    try {
      document.body.removeAttribute('data-tb-apply-result');
    } catch (e) {}

    try {
      saveBtn.click();
    } catch (e) {
      done({ status: 'error', message: '点击保存抛错: ' + ((e && e.message) || e) });
      return;
    }

    // 4. 轮询 /applyByOrder 接口响应 (从 MAIN world 写到 document.body 属性)
    const start2 = Date.now();
    let apiRaw = null;
    while (Date.now() - start2 < 12000) {
      try {
        const attr = document.body.getAttribute('data-tb-apply-result');
        if (attr) {
          const obj = JSON.parse(attr);
          if (obj && obj.time && obj.time >= start2 - 500) { apiRaw = obj; break; }
        }
      } catch (e) {}
      await new Promise((r) => setTimeout(r, 300));
    }

    if (!apiRaw) {
      done({ status: 'timeout', message: '点保存后 12s 未捕获到 applyByOrder 响应' });
      return;
    }

    const body = apiRaw.body || {};
    if (body.success === true) {
      done({
        status: 'submitted',
        message: '电子 ' + (body.result || '已提交')
      });
    } else if (
      body.errorCode === '1004' ||
      (body.errorMessage && body.errorMessage.indexOf('不需要再次提交') >= 0)
    ) {
      done({
        status: 'already',
        message: body.errorMessage || '已申请过(无修改)'
      });
    } else {
      done({
        status: 'error',
        message: 'API 失败: ' +
          (body.errorMessage || body.errorCode ||
            (JSON.stringify(body).slice(0, 120)))
      });
    }
  })();
})();
`;

const APPLY_POLL = `
(function() {
  try { return JSON.stringify(window.__tbApply || { phase: 'init' }); }
  catch (e) { return '{}'; }
})();
`;

// 装到页面 MAIN world 的网络 hook: 拦截 /user/invoice/pc/applyByOrder 响应,
// 写到 document.body[data-tb-apply-result] 让 isolated world 读到
const NETWORK_HOOK_INSTALL = `
(function() {
  const code = ${JSON.stringify(`
(function() {
  if (window.__tbHookInstalled) return;
  window.__tbHookInstalled = true;
  const TARGET = '/user/invoice/pc/applyByOrder';

  function record(body, httpOk) {
    try {
      document.body.setAttribute('data-tb-apply-result', JSON.stringify({
        ok: !!httpOk, body: body, time: Date.now()
      }));
    } catch (e) {}
  }

  // fetch
  if (window.fetch) {
    const origFetch = window.fetch;
    window.fetch = function() {
      const u = arguments[0];
      const us = typeof u === 'string' ? u : (u && u.url) || '';
      const p = origFetch.apply(this, arguments);
      if (us.indexOf(TARGET) >= 0) {
        p.then(function(r) {
          try {
            r.clone().json().then(
              function(b) { record(b, r.ok); },
              function() { record(null, r.ok); }
            );
          } catch (e) { record(null, r.ok); }
        }).catch(function() { record(null, false); });
      }
      return p;
    };
  }

  // XHR (mtop 大多走 XHR)
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, url) {
    this.__tbUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    const xhr = this;
    if (xhr.__tbUrl && String(xhr.__tbUrl).indexOf(TARGET) >= 0) {
      xhr.addEventListener('load', function() {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) {}
        record(body, xhr.status >= 200 && xhr.status < 300);
      });
      xhr.addEventListener('error', function() { record(null, false); });
      xhr.addEventListener('abort', function() { record(null, false); });
    }
    return origSend.apply(this, arguments);
  };
})();
`)};
  const s = document.createElement('script');
  s.textContent = code;
  (document.head || document.documentElement).appendChild(s);
  s.remove();
})();
`;

async function runApplyWorker(tabId) {
  try {
    // 先装网络 hook 到 MAIN world (拦截 applyByOrder 响应)
    await execCode(tabId, NETWORK_HOOK_INSTALL);
    // 再注入 isolated world 主流程
    await execCode(tabId, APPLY_KICKOFF);
  } catch (e) {
    return { status: 'error', message: '注入失败: ' + e.message, taxName: '', taxId: '' };
  }
  const start = Date.now();
  while (Date.now() - start < 50000 && !state.abort) {
    await sleep(800);
    try {
      const r = await execCode(tabId, APPLY_POLL);
      const obj = JSON.parse(r[0] || '{}');
      if (obj.phase === 'done') {
        return {
          status: obj.status,
          message: obj.message,
          taxName: obj.taxName || '',
          taxId: obj.taxId || ''
        };
      }
    } catch (e) { /* tab navigating */ }
  }
  return { status: 'timeout', message: '处理超时(50s)', taxName: '', taxId: '' };
}

// ───────── 工具 ─────────

function execCode(tabId, code) {
  return new Promise((resolve, reject) => {
    chrome.tabs.executeScript(tabId, { code }, (results) => {
      if (chrome.runtime.lastError) {
        return reject(new Error(chrome.runtime.lastError.message));
      }
      resolve(results || []);
    });
  });
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
