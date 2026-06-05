// 运行在页面主世界 (page main world),可以访问 window.lib.mtop
(function () {
  if (window.__fapiaoV2_init) return;
  window.__fapiaoV2_init = true;

  window.__fapiaoV2 = {
    records: [],
    done: false,
    error: null,
    progress: '0/?',
    running: false,
    totalPages: null
  };

  function fetchPage(p, sz) {
    return window.lib.mtop.request({
      api: 'mtop.1688.kingsuns.invoice.dataline.service',
      v: '1.0',
      data: {
        serviceId:
          'KsInvoicePurchaserManageMtopService.queryInvoiceApplyRecordListByPage',
        param: JSON.stringify({
          page: p,
          pageSize: sz,
          bizStatusList: [1, 5, 6, 40]
        })
      }
    });
  }

  async function tryPage(p, sz) {
    const delays = [1000, 3000, 8000];
    let lastErr;
    for (let i = 0; i < delays.length; i++) {
      try {
        const r = await fetchPage(p, sz);
        const inner = r && r.data && r.data.data ? r.data.data : r && r.data;
        if (inner && inner.result) return inner;
        throw new Error('no result in response');
      } catch (e) {
        lastErr = e;
        if (i === delays.length - 1) throw e;
        await new Promise((res) => setTimeout(res, delays[i]));
      }
    }
    throw lastErr;
  }

  async function runFetch() {
    if (window.__fapiaoV2.running) return;
    window.__fapiaoV2 = {
      records: [],
      done: false,
      error: null,
      progress: '0/?',
      running: true,
      totalPages: null
    };

    const sz = 20;
    let totalPages = 999;
    try {
      for (let p = 1; p <= totalPages; p++) {
        const inner = await tryPage(p, sz);
        const list = inner.result || [];
        for (const rec of list) {
          const om = rec.orderModel || {};
          const im = rec.invoiceModel || {};
          const titleModel = im.purchaserInvoiceTitleModel || {};
          window.__fapiaoV2.records.push({
            oid: om.idStr || om.id,
            shop: om.sellerCompanyName || om.sellerLoginId || '',
            amount: im.amount || om.sumPayment || 0,
            t: im.gmtCreate || '',
            status: im.bizStatus,
            title: titleModel.title || '',
            taxNo: titleModel.taxpayerIdentify || ''
          });
        }
        if (inner.pagination && inner.pagination.totalNum) {
          totalPages = Math.ceil(parseInt(inner.pagination.totalNum, 10) / sz);
          window.__fapiaoV2.totalPages = totalPages;
        }
        window.__fapiaoV2.progress = p + '/' + totalPages;
        if (list.length < sz) break;
        await new Promise((res) => setTimeout(res, 1500));
      }
      window.__fapiaoV2.done = true;
      window.__fapiaoV2.running = false;
    } catch (e) {
      window.__fapiaoV2.error = (e && e.message) || String(e);
      window.__fapiaoV2.done = true;
      window.__fapiaoV2.running = false;
    }
  }

  function snapshot() {
    return {
      progress: window.__fapiaoV2.progress,
      n: window.__fapiaoV2.records.length,
      done: window.__fapiaoV2.done,
      running: window.__fapiaoV2.running,
      error: window.__fapiaoV2.error,
      totalPages: window.__fapiaoV2.totalPages
    };
  }

  window.addEventListener('message', (ev) => {
    if (ev.source !== window) return;
    const m = ev.data;
    if (!m || m.__fapiao_to !== 'page') return;

    if (m.type === 'CHECK') {
      const existing =
        window.__fapiaoV2.running || window.__fapiaoV2.done
          ? snapshot()
          : null;
      window.postMessage(
        {
          __fapiao_to: 'cs',
          rid: m.rid,
          type: 'CHECK_RESULT',
          url: location.href,
          hasMtop: !!(
            window.lib &&
            window.lib.mtop &&
            typeof window.lib.mtop.request === 'function'
          ),
          hasRealCaptcha: !!document.querySelector(
            '.nc-container,.nc_wrapper,#nocaptcha,#nc_1_wrapper'
          ),
          existing: existing
        },
        '*'
      );
    } else if (m.type === 'RUN') {
      runFetch();
      window.postMessage(
        { __fapiao_to: 'cs', rid: m.rid, type: 'RUN_STARTED' },
        '*'
      );
    } else if (m.type === 'POLL') {
      window.postMessage(
        Object.assign(
          { __fapiao_to: 'cs', rid: m.rid, type: 'POLL_RESULT' },
          snapshot()
        ),
        '*'
      );
    } else if (m.type === 'FETCH_RECORDS') {
      window.postMessage(
        {
          __fapiao_to: 'cs',
          rid: m.rid,
          type: 'RECORDS',
          records: window.__fapiaoV2.records
        },
        '*'
      );
    }
  });

  console.log('[fapiao-v2] page-world ready');
})();
