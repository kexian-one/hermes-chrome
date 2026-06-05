// popup runs in extension context with chrome.* APIs available
const INVOICE_URL =
  'https://work.1688.com/?_path_=buyer2017Base/2017buyerbase_trade/buyerMyInvoice';

const el = (id) => document.getElementById(id);
const show = (id, on) => { el(id).style.display = on ? '' : 'none'; };
const setText = (id, t) => { el(id).textContent = t; };

let currentTabId = null;
let allRecords = null;
let allShops = null;
let pollTimer = null;

function getCurrentTab() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      resolve(tabs && tabs[0]);
    });
  });
}

function sendToTab(tabId, msg) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, msg, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ __noReceiver: true, error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || {});
      }
    });
  });
}

function setStatus(text, kind) {
  const s = el('status');
  s.textContent = text;
  s.className = 'status status-' + (kind || 'info');
  show('status', true);
}

async function init() {
  const tab = await getCurrentTab();
  currentTabId = tab && tab.id;

  if (!tab || !tab.url) {
    setStatus('无法读取当前 tab', 'err');
    return;
  }

  if (!/^https:\/\/work\.1688\.com/.test(tab.url)) {
    setStatus('当前 tab 不是 1688 工作台。请先打开发票页。', 'warn');
    show('btnOpen', true);
    return;
  }

  await refreshCheck();
}

async function refreshCheck() {
  show('btnRefresh', false);
  show('btnRun', false);
  show('btnOpen', false);
  show('error', false);
  show('captcha', false);
  show('results', false);

  setStatus('检查页面状态…', 'info');

  const resp = await sendToTab(currentTabId, { type: 'CHECK' });
  if (resp.__noReceiver) {
    setStatus('content script 还没就绪,请先刷新 1688 页面后重新打开本插件。', 'warn');
    show('btnRefresh', true);
    return;
  }
  if (resp.error) {
    setStatus('页面状态检查失败: ' + resp.error, 'err');
    show('btnRefresh', true);
    return;
  }

  if (resp.hasRealCaptcha) {
    show('captcha', true);
    show('btnRefresh', true);
    setStatus('请先在页面上手动通过滑块验证', 'warn');
    return;
  }
  if (!resp.hasMtop) {
    setStatus('页面未就绪 (window.lib.mtop 不存在),请确认已进入"我的发票"页。', 'warn');
    show('btnOpen', true);
    show('btnRefresh', true);
    return;
  }

  // 页面就绪。看是否有正在跑/已跑完的任务
  if (resp.existing) {
    setStatus('✓ 已就绪 — 检测到先前抓取', 'ok');
    if (resp.existing.running) {
      startPolling();
    } else if (resp.existing.done) {
      if (resp.existing.error) {
        show('error', true);
        el('error').textContent = '上次抓取失败: ' + resp.existing.error;
        show('btnRun', true);
      } else {
        await loadResults();
      }
    } else {
      show('btnRun', true);
    }
  } else {
    setStatus('✓ 已就绪', 'ok');
    show('btnRun', true);
  }
}

async function openInvoicePage() {
  await new Promise((r) =>
    chrome.tabs.update(currentTabId, { url: INVOICE_URL }, () => r())
  );
  setStatus('已导航。等页面加载完后重新点击插件图标。', 'info');
  show('btnOpen', false);
  show('btnRefresh', true);
}

async function startRun() {
  show('btnRun', false);
  show('error', false);
  show('progressBox', true);
  setText('progressText', '启动抓取…');
  el('progressFill').style.width = '2%';

  const resp = await sendToTab(currentTabId, { type: 'RUN' });
  if (resp.error || resp.__noReceiver) {
    show('progressBox', false);
    show('error', true);
    el('error').textContent = '启动失败: ' + (resp.error || resp.__noReceiver);
    show('btnRun', true);
    return;
  }

  startPolling();
}

function startPolling() {
  show('progressBox', true);
  setText('progressText', '抓取中…');
  show('btnRun', false);

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 2000);
  poll();
}

async function poll() {
  const r = await sendToTab(currentTabId, { type: 'POLL' });
  if (r.__noReceiver) return;

  let pct = 5;
  if (r.totalPages && r.progress) {
    const cur = parseInt(String(r.progress).split('/')[0], 10);
    if (!isNaN(cur)) pct = Math.min(99, Math.round((cur / r.totalPages) * 100));
  }
  el('progressFill').style.width = pct + '%';
  setText(
    'progressText',
    '进度: ' + (r.progress || '?') + ' · 已抓 ' + (r.n || 0) + ' 单'
  );

  if (r.error) {
    clearInterval(pollTimer);
    pollTimer = null;
    show('progressBox', false);
    show('error', true);
    el('error').textContent = '抓取出错: ' + r.error;
    show('btnRun', true);
    return;
  }

  if (r.done) {
    clearInterval(pollTimer);
    pollTimer = null;
    el('progressFill').style.width = '100%';
    setText('progressText', '完成 · 共 ' + r.n + ' 单');
    await loadResults();
  }
}

async function loadResults() {
  const r = await sendToTab(
    currentTabId,
    { type: 'FETCH_RECORDS', timeoutMs: 60000 }
  );
  if (r.error || r.__noReceiver) {
    show('error', true);
    el('error').textContent = '读取记录失败: ' + (r.error || r.__noReceiver);
    return;
  }
  allRecords = r.records || [];
  renderResults();
}

function renderResults() {
  show('progressBox', false);
  show('results', true);

  const byShop = {};
  for (const rec of allRecords) {
    const k = rec.shop || '(未知店铺)';
    if (!byShop[k]) byShop[k] = { count: 0, total: 0, earliest: '', latest: '' };
    byShop[k].count++;
    byShop[k].total += (parseFloat(rec.amount) || 0) / 100;
    if (!byShop[k].earliest || rec.t < byShop[k].earliest)
      byShop[k].earliest = rec.t;
    if (!byShop[k].latest || rec.t > byShop[k].latest)
      byShop[k].latest = rec.t;
  }
  allShops = Object.keys(byShop).map((k) =>
    Object.assign({ shop: k }, byShop[k])
  );
  allShops.sort((a, b) => b.count - a.count);

  const grandTotal = allShops.reduce((a, s) => a + s.total, 0).toFixed(2);
  el('stats').innerHTML =
    '共 <b>' + allShops.length + '</b> 家店铺,' +
    '<b>' + allRecords.length + '</b> 单待开,' +
    '总金额 <b>¥' + grandTotal + '</b>';

  const top = allShops.slice(0, 5);
  let html =
    '<thead><tr><th>#</th><th>店铺</th>' +
    '<th class="num">单数</th><th class="num">金额(¥)</th>' +
    '<th>最早申请</th></tr></thead><tbody>';
  top.forEach((s, i) => {
    html +=
      '<tr><td>' + (i + 1) + '</td>' +
      '<td>' + escapeHtml(s.shop) + '</td>' +
      '<td class="num">' + s.count + '</td>' +
      '<td class="num">' + s.total.toFixed(2) + '</td>' +
      '<td>' + (s.earliest || '').slice(0, 10) + '</td></tr>';
  });
  html += '</tbody>';
  el('topTable').innerHTML = html;
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function buildShopTitleCsv(records) {
  // 按 (店铺, 发票抬头) 聚合 — 同店多抬头拆多行
  const byKey = new Map();
  for (const rec of records) {
    const shop = rec.shop || '(未知店铺)';
    const title = rec.title || '(无抬头)';
    const taxNo = rec.taxNo || '';
    const k = shop + '\x1f' + title;  // 用不会出现在文本里的分隔符
    if (!byKey.has(k)) byKey.set(k, {
      shop, title, taxNo,
      count: 0, total: 0, earliest: '', latest: ''
    });
    const g = byKey.get(k);
    g.count++;
    g.total += (parseFloat(rec.amount) || 0) / 100;
    if (!g.earliest || rec.t < g.earliest) g.earliest = rec.t;
    if (!g.latest || rec.t > g.latest) g.latest = rec.t;
    if (!g.taxNo && taxNo) g.taxNo = taxNo;
  }
  const rows = Array.from(byKey.values()).sort((a, b) => {
    if (a.shop !== b.shop) return a.shop < b.shop ? -1 : 1;
    return b.count - a.count;
  });

  let csv = '﻿店铺名,发票抬头,纳税人识别号,订单数,总金额,最早申请,最晚申请\n';
  for (const r of rows) {
    const safeShop = (r.shop || '').replace(/"/g, '""');
    const safeTitle = (r.title || '').replace(/"/g, '""');
    const safeTax = (r.taxNo || '').replace(/"/g, '""');
    csv +=
      '"' + safeShop + '","' + safeTitle + '","' + safeTax + '",' +
      r.count + ',' + r.total.toFixed(2) + ',' +
      (r.earliest || '') + ',' + (r.latest || '') + '\n';
  }
  return csv;
}

function buildOrdersCsv(records) {
  let csv = '﻿订单号,店铺,金额(元),发票抬头,纳税人识别号,申请时间,状态\n';
  for (const r of records) {
    const amt = ((parseFloat(r.amount) || 0) / 100).toFixed(2);
    const safeShop = (r.shop || '').replace(/"/g, '""');
    const safeTitle = (r.title || '').replace(/"/g, '""');
    const safeTax = (r.taxNo || '').replace(/"/g, '""');
    // 订单号用 ="..." 公式形式,逼 Excel 当文本处理,
    // 否则 19 位订单号会被解析成科学计数法,第 16 位往后丢精度
    const oid = String(r.oid || '');
    csv +=
      '="' + oid + '","' + safeShop + '",' + amt +
      ',"' + safeTitle + '","' + safeTax + '",' +
      (r.t || '') + ',' + (r.status == null ? '' : r.status) + '\n';
  }
  return csv;
}

function downloadCsv(filename, content) {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  chrome.downloads.download(
    { url: url, filename: filename, saveAs: false },
    () => {
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
  );
}

el('btnOpen').addEventListener('click', openInvoicePage);
el('btnRun').addEventListener('click', startRun);
el('btnRerun').addEventListener('click', startRun);
el('btnRefresh').addEventListener('click', refreshCheck);
el('btnDownloadOrders').addEventListener('click', () => {
  if (allRecords) downloadCsv('1688_applying_invoices_orders.csv',
    buildOrdersCsv(allRecords));
});
el('btnDownloadShopTitle').addEventListener('click', () => {
  if (allRecords) downloadCsv('1688_applying_invoices_shop_title.csv',
    buildShopTitleCsv(allRecords));
});

/* ═══════════════════ Tab 切换 ═══════════════════ */

let activeTab = 'ali';

function switchTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.panel').forEach((p) => {
    p.style.display = p.dataset.panel === name ? '' : 'none';
  });
  if (name === 'ali' && !aliInited) {
    aliInited = true;
    init();
  } else if (name === 'tb') {
    tbInit();
  }
}

let aliInited = false;
document.querySelectorAll('.tab').forEach((t) => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});

/* ═══════════════════ 淘宝批量开票 ═══════════════════ */

const TB_LIST_URL =
  'https://i.taobao.com/my_itaobao/invoice?active=notApply';

let tbTabId = null;

function bgSend(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ error: chrome.runtime.lastError.message });
      } else resolve(resp || {});
    });
  });
}

async function tbInit() {
  const tab = await getCurrentTab();
  tbTabId = tab && tab.id;

  if (!tab || !tab.url) {
    tbSetStatus('无法读取当前 tab', 'err');
    return;
  }
  const onListPage = /^https:\/\/i\.taobao\.com\/my_itaobao\/invoice/.test(tab.url);
  tbOnListPage = onListPage;

  if (!onListPage) {
    tbSetStatus('当前 tab 不是"我的发票 → 未申请"页面。先点下方按钮跳过去再扫描。', 'warn');
    show('tbBtnOpen', true);
  } else {
    tbSetStatus('✓ 已就绪 — 我的发票页 tab 已找到', 'ok');
    show('tbBtnOpen', false);
  }

  // 拿背景状态,渲染 (即使不在列表页,也能看到之前扫的结果 + 下载 CSV + 继续提交)
  const resp = await bgSend({ type: 'TB_GET_STATE' });
  if (resp && resp.state) tbRender(resp.state);
}

let tbOnListPage = false;

function tbSetStatus(text, kind) {
  const s = el('tbStatus');
  s.textContent = text;
  s.className = 'status status-' + (kind || 'info');
  show('tbStatus', true);
}

async function tbOpenList() {
  await new Promise((r) => chrome.tabs.update(tbTabId, { url: TB_LIST_URL }, () => r()));
  tbSetStatus('已导航。等页面加载完成后重新点击插件图标。', 'info');
  show('tbBtnOpen', false);
}

async function tbStartScan() {
  show('tbError', false);
  if (!tbOnListPage) {
    showTbError('当前 tab 不是 我的发票 页面,请先打开');
    return;
  }
  const r = await bgSend({ type: 'TB_SCAN_ALL', tabId: tbTabId });
  if (r.error) showTbError('启动扫描失败: ' + r.error);
}

async function tbPause() {
  await bgSend({ type: 'TB_PAUSE' });
}

async function tbResume() {
  await bgSend({ type: 'TB_RESUME' });
}

async function tbStop() {
  await bgSend({ type: 'TB_STOP' });
}

async function tbReset() {
  await bgSend({ type: 'TB_RESET' });
}

function showTbError(text) {
  el('tbError').textContent = text;
  show('tbError', true);
}

const TB_STATUS_LABEL = {
  submitted: '已提交',
  'skipped-paper': '跳过(纸质)',
  already: '已申请过',
  captcha: '滑块验证',
  error: '错误',
  timeout: '超时'
};

function tbRender(s) {
  if (activeTab !== 'tb') return;

  const canScan = tbOnListPage;
  const hasResults = s.results && s.results.length > 0;
  const hasQueue = s.queue && s.queue.length > 0;

  // 下载按钮 + 主体统计:有数据就显示
  show('tbBtnDownload', hasQueue);
  show('tbEntityBox', hasResults);
  if (hasResults) renderEntityStats(s);

  // ─── running: 扫一页申请一页 ───
  if (s.phase === 'running') {
    show('tbBtnScan', false);
    show('tbBtnPause', !s.paused);
    show('tbBtnResume', !!s.paused);
    show('tbBtnStop', true);
    show('tbBtnReset', false);

    show('tbScanBox', true);
    show('tbQueueBox', hasQueue);
    show('tbRunBox', hasQueue);
    show('tbResultBox', false);

    const pg = s.scanProgress || { page: 0, found: 0 };
    setText('tbScanText',
      (s.paused ? '⏸ 已暂停 · ' : '运行中 · ') +
      '第 ' + pg.page + ' 页 · 已扫到 ' + pg.found + ' 单 · 已处理 ' + s.cursor);
    el('tbScanFill').style.width = Math.min(95, pg.page * 6) + '%';
    el('tbScanLog').textContent = (s.scanLog || []).slice(-8).join('\n');

    if (hasQueue) {
      renderQueuePreview(s);
      const total = s.queue.length;
      const cur = s.cursor;
      setText('tbRunText',
        (s.paused ? '⏸ ' : '') +
        '已处理 ' + cur + '/' + total +
        (s.queue[cur] ? ' · 当前 ' + s.queue[cur].orderId : ''));
      el('tbRunFill').style.width = total ? Math.round((cur / total) * 100) + '%' : '0%';
    }
    return;
  }

  // ─── done / aborted ───
  if (s.phase === 'done' || s.phase === 'aborted') {
    show('tbBtnScan', canScan);
    show('tbBtnPause', false);
    show('tbBtnResume', false);
    show('tbBtnStop', false);
    show('tbBtnReset', true);
    show('tbScanBox', false);
    show('tbQueueBox', false);
    show('tbRunBox', false);
    show('tbResultBox', true);
    renderResultTable(s);
    return;
  }

  // ─── error ───
  if (s.phase === 'error') {
    showTbError('错误: ' + (s.errorMessage || '未知'));
    show('tbBtnReset', true);
    show('tbBtnPause', false);
    show('tbBtnResume', false);
    show('tbBtnStop', false);
    return;
  }

  // ─── idle ───
  show('tbBtnScan', canScan);
  show('tbBtnPause', false);
  show('tbBtnResume', false);
  show('tbBtnStop', false);
  show('tbBtnReset', false);
  show('tbScanBox', false);
  show('tbQueueBox', false);
  show('tbRunBox', false);
  show('tbResultBox', false);
}

function renderEntityStats(s) {
  // 按 taxName 分组 (空的归 "(未知主体)"), 统计 submitted 的单数和金额
  // 剔除 status='already' — 这些进开票页就没有保存按钮 (之前已申请过),
  // 既不算我们提交的,也不算需人工
  const groups = new Map();
  for (const r of s.results) {
    if (r.status === 'already') continue;

    const key = (r.taxName || '').trim() || '(未知主体)';
    if (!groups.has(key)) groups.set(key, {
      taxName: key, taxId: r.taxId || '',
      submitted: 0, manual: 0, amount: 0
    });
    const g = groups.get(key);
    if (!g.taxId && r.taxId) g.taxId = r.taxId;
    if (r.status === 'submitted') {
      g.submitted++;
      g.amount += parseFloat(r.amount || '0') || 0;
    } else {
      g.manual++;
    }
  }
  const rows = Array.from(groups.values()).sort((a, b) => b.amount - a.amount);

  let html =
    '<thead><tr><th>主体</th><th class="num">已申请</th>' +
    '<th class="num">金额(¥)</th><th class="num">需人工</th></tr></thead><tbody>';
  for (const g of rows) {
    html +=
      '<tr><td>' + escapeHtml(g.taxName.slice(0, 18)) +
      (g.taxId ? '<br><span style="color:#888;font-size:11px">' + escapeHtml(g.taxId) + '</span>' : '') +
      '</td>' +
      '<td class="num">' + g.submitted + '</td>' +
      '<td class="num">' + g.amount.toFixed(2) + '</td>' +
      '<td class="num">' + (g.manual || '') + '</td>' +
      '</tr>';
  }
  html += '</tbody>';
  el('tbEntityTable').innerHTML = html;
}

function renderQueuePreview(s) {
  el('tbQueueStats').innerHTML =
    '已扫到 <b>' + s.queue.length + '</b> 单可开票' +
    (s.scanLog && s.scanLog.length
      ? ' · 共扫描 <b>' + Math.max(1, s.scanProgress.page) + '</b> 页'
      : '');
  const top = s.queue.slice(0, 10);
  let html =
    '<thead><tr><th>#</th><th>日期</th><th>店铺</th><th>订单号</th></tr></thead><tbody>';
  top.forEach((o, i) => {
    html +=
      '<tr><td>' + (i + 1) + '</td>' +
      '<td>' + escapeHtml(o.date || '') + '</td>' +
      '<td>' + escapeHtml((o.shop || '').slice(0, 14)) + '</td>' +
      '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px">' +
      escapeHtml(o.orderId) + '</td></tr>';
  });
  if (s.queue.length > 10) {
    html += '<tr><td colspan="4" style="color:#888">... 还有 ' +
            (s.queue.length - 10) + ' 单</td></tr>';
  }
  html += '</tbody>';
  el('tbQueueTable').innerHTML = html;
}

function renderResultTable(s) {
  const buckets = {
    submitted: 0, 'skipped-paper': 0, already: 0,
    error: 0, timeout: 0, captcha: 0
  };
  for (const r of s.results) {
    buckets[r.status] = (buckets[r.status] || 0) + 1;
  }
  el('tbResultStats').innerHTML =
    '处理完毕: <b>' + s.results.length + '</b> 单 · ' +
    '<span class="status-pill pill-submitted">已提交 ' + (buckets.submitted || 0) + '</span> ' +
    '<span class="status-pill pill-skipped-paper">跳过(纸质) ' + (buckets['skipped-paper'] || 0) + '</span>' +
    (buckets.already ? ' <span class="status-pill pill-already">已申请过 ' + buckets.already + '</span>' : '') +
    (buckets.captcha ? ' <span class="status-pill pill-captcha">滑块 ' + buckets.captcha + '</span>' : '') +
    (buckets.error ? ' <span class="status-pill pill-error">错误 ' + buckets.error + '</span>' : '') +
    (buckets.timeout ? ' <span class="status-pill pill-timeout">超时 ' + buckets.timeout + '</span>' : '');

  let html =
    '<thead><tr><th>#</th><th>订单号</th><th>状态</th><th>说明</th></tr></thead><tbody>';
  s.results.forEach((r, i) => {
    const cls = 'pill-' + (r.status || 'error');
    const label = TB_STATUS_LABEL[r.status] || r.status;
    html +=
      '<tr><td>' + (i + 1) + '</td>' +
      '<td style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px">' +
      escapeHtml(r.orderId) + '</td>' +
      '<td><span class="status-pill ' + cls + '">' + escapeHtml(label) + '</span></td>' +
      '<td style="color:#595e66;font-size:11px">' + escapeHtml(r.message || '') + '</td></tr>';
  });
  html += '</tbody>';
  el('tbResultTable').innerHTML = html;
}

/* ---- CSV 下载 ---- */

function tbStatusToCN(status) {
  if (status === 'submitted') return '自动申请';
  if (!status) return '(待处理)';
  return '需要人工';
}

function buildTbCsv(s) {
  let csv = '﻿订单号,商家,金额(元),发票抬头,纳税人识别号,状态,说明\n';
  const resultByOid = new Map();
  for (const r of s.results) resultByOid.set(r.orderId, r);
  for (const o of s.queue) {
    const oid = String(o.orderId);
    const r = resultByOid.get(oid) || {};
    const shop = (o.shop || '').replace(/"/g, '""');
    const amount = o.amount || '';
    const taxName = (r.taxName || '').replace(/"/g, '""');
    const taxId = (r.taxId || '').replace(/"/g, '""');
    const statusCN = tbStatusToCN(r.status);
    const msg = (r.message || '').replace(/"/g, '""');
    // 订单号用 ="..." 防 Excel 科学计数法
    csv += '="' + oid + '","' + shop + '",' + amount +
           ',"' + taxName + '","' + taxId + '","' +
           statusCN + '","' + msg + '"\n';
  }
  return csv;
}

function tbDownloadCsv() {
  // 重新读后台状态,确保拿到最新
  bgSend({ type: 'TB_GET_STATE' }).then((resp) => {
    if (!resp || !resp.state) return;
    const csv = buildTbCsv(resp.state);
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    downloadCsv('taobao_invoice_apply_' + stamp + '.csv', csv);
  });
}

// 监听后台状态变更
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === 'TB_STATE') tbRender(msg.state);
});

// TB 面板按钮
el('tbBtnOpen').addEventListener('click', tbOpenList);
el('tbBtnScan').addEventListener('click', tbStartScan);
el('tbBtnPause').addEventListener('click', tbPause);
el('tbBtnResume').addEventListener('click', tbResume);
el('tbBtnStop').addEventListener('click', tbStop);
el('tbBtnReset').addEventListener('click', tbReset);
el('tbBtnDownload').addEventListener('click', tbDownloadCsv);

// 初始化:默认 1688 tab
aliInited = true;
init();
