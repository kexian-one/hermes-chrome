---
name: fapiao-1688
description: 抓取 1688 买家"申请中发票"全部订单(已申请但商家还没开),按店铺聚合后生成两个 CSV 报告(店铺汇总 + 订单明细)写到本次任务的输出目录(由 master 通过派任务的 IM 通道发回),并在对话里展示 Top 5。需要 open-claude-in-chrome MCP server 已连接,且用户的真实 Chrome 已登录 1688 买家账号。触发场景:用户问"哪些发票没开"、"催开票"、"申请中发票"、"1688 待开票",或直接调用 /fapiao-1688。
---

# 1688 申请中发票汇总

## 何时调用

- 用户问"哪些发票没开"、"催开票"、"1688 申请中发票"、"待开票订单"
- 用户想统计 1688 买家身份下待开发票的金额/店铺分布
- 用户直接调用 `/fapiao-1688`

## 前置条件

1. `mcp__open-claude-in-chrome__*` 工具可用(`open-claude-in-chrome` MCP server 已连接)
2. 用户的真实 Chrome 已登录 1688 买家账号(扩展接管已登录的浏览器,无需另行登录)
3. 当前工作目录可写

## 关键事实(实测确认)

**入口**:`https://work.1688.com/?_path_=buyer2017Base/2017buyerbase_trade/buyerMyInvoice`(1688 买家工作台 → "我的发票" 路由)。这个页面**内嵌**了 `air.1688.com/app/ctf-page/invoice/buyer-invoice-list.html` 这个 iframe,但**主 frame 已带 `window.lib.mtop`**,不用进 iframe。

**绝对不要**冷启动直接 navigate 到 air.1688.com 那个 iframe URL — 缺 referrer / 路由轨迹,风控指纹异常,容易触滑块。参见 memory: `feedback_no_direct_iframe_url`

**数据接口:**
- mtop api: `mtop.1688.kingsuns.invoice.dataline.service`
- 内部 service: `KsInvoicePurchaserManageMtopService.queryInvoiceApplyRecordListByPage`
- `sign` 参数动态计算,**不要手搓**,直接用 `window.lib.mtop.request`
- 主 frame(work.1688.com)的 `lib.mtop` 直接调通这个 air.1688.com 域的服务(已实测)

**数据结构:**
- `r.data.data.result[]` (数组,注意多一层 `.data`)
- `r.data.data.pagination.totalNum` (总数,字符串型)
- 每条:
  - 店铺名:`orderModel.sellerCompanyName`
  - 订单号:`orderModel.idStr`(用 `idStr`,不要 `id` — 长数字会丢精度)
  - **金额:`invoiceModel.amount`,单位【分】,/100 才是元**
  - 申请时间:`invoiceModel.gmtCreate`
  - 状态:`invoiceModel.bizStatus`(数字)

**bizStatusList `[1, 5, 6, 40]` = "申请中"** 这个 tab(含待审核、审核中、开票中、部分开票)。

## open-claude-in-chrome 用法要点

- `javascript_tool` **直接返回最后一个表达式的值**(不用 `document.title` 转接)
- `text` 里**不要写 `return`**(只是评估表达式,不是函数体)
- IIFE `(function(){...})()` 没显式 return 时返回 `undefined` — 副作用照常生效,只是没回值
- 单次响应字符串上限约 **50K 字符**;超了会被 spill 到磁盘文件,返回的报错里给路径,需要 Python 读+`json.loads()` 解码(文件里是 JSON-encoded string)
- 用前先 `tabs_context_mcp(createIfEmpty=true)` 拿 tabId

## 执行步骤

### Step 1: 拿 tab ID

```
mcp__open-claude-in-chrome__tabs_context_mcp(createIfEmpty=true)
→ 记下 tabId
```

### Step 2: 导航 + 验证

```
navigate(tabId, 'https://work.1688.com/?_path_=buyer2017Base/2017buyerbase_trade/buyerMyInvoice')
```

等 4 秒让内嵌 iframe / mtop 客户端就绪,验证:

```js
new Promise(r => setTimeout(() => r({
  url: location.href,
  hasMtop: !!(window.lib && window.lib.mtop && typeof window.lib.mtop.request === 'function'),
  hasRealCaptcha: !!document.querySelector('.nc-container,.nc_wrapper,#nocaptcha,#nc_1_wrapper'),
  iframeHasAir: Array.from(document.querySelectorAll('iframe')).some(f => /air\.1688\.com.*buyer-invoice/.test(f.src || ''))
}), 4000))
```

期望:`hasMtop:true`、`hasRealCaptcha:false`、`iframeHasAir:true`。

如果 `hasRealCaptcha:true` → 提示用户手动拖滑块,等回复后继续。**不要尝试模拟拖动**(触发更严风控)。

### Step 3: 探测一次,确认能拿到数据 + 总数

跑一次 page=1,pageSize=2 试水(同时拿到 `totalNum` 估算总页数):

```js
window.__probe = null;
window.lib.mtop.request({
  api: 'mtop.1688.kingsuns.invoice.dataline.service',
  v: '1.0',
  data: {
    serviceId: 'KsInvoicePurchaserManageMtopService.queryInvoiceApplyRecordListByPage',
    param: JSON.stringify({ page: 1, pageSize: 2, bizStatusList: [1, 5, 6, 40] })
  }
}).then(r => {
  const inner = r && r.data && r.data.data ? r.data.data : (r && r.data);
  window.__probe = {
    ok: true,
    totalNum: inner && inner.pagination ? inner.pagination.totalNum : null,
    retCodes: r ? r.ret : null
  };
}).catch(e => { window.__probe = { ok: false, error: (e && e.message) || JSON.stringify(e).slice(0, 300) }; });
'probe-fired'
```

```js
new Promise(r => setTimeout(() => r(window.__probe), 4000))
```

期望 `ok:true`,`retCodes:['SUCCESS::调用成功']`,记下 `totalNum`。

### Step 4: 启动后台抓取

```js
(function() {
  window.__allRecords = [];
  window.__fetchDone = false;
  window.__fetchProgress = '0/?';
  window.__fetchError = null;

  function fetchPage(p, sz) {
    return window.lib.mtop.request({
      api: 'mtop.1688.kingsuns.invoice.dataline.service',
      v: '1.0',
      data: {
        serviceId: 'KsInvoicePurchaserManageMtopService.queryInvoiceApplyRecordListByPage',
        param: JSON.stringify({ page: p, pageSize: sz, bizStatusList: [1, 5, 6, 40] })
      }
    });
  }

  async function tryPage(p, sz) {
    const delays = [1000, 3000, 8000];
    for (let i = 0; i < delays.length; i++) {
      try {
        const r = await fetchPage(p, sz);
        const inner = r.data && r.data.data ? r.data.data : r.data;
        if (inner && inner.result) return inner;
        throw new Error('no result in response');
      } catch (e) {
        if (i === delays.length - 1) throw e;
        await new Promise(r => setTimeout(r, delays[i]));
      }
    }
  }

  async function run() {
    const sz = 20;
    let totalPages = 999;
    try {
      for (let p = 1; p <= totalPages; p++) {
        const inner = await tryPage(p, sz);
        const list = inner.result || [];
        for (const rec of list) {
          const om = rec.orderModel || {};
          const im = rec.invoiceModel || {};
          window.__allRecords.push({
            oid: om.idStr || om.id,
            shop: om.sellerCompanyName || om.sellerLoginId || '',
            amount: im.amount || om.sumPayment || 0,
            t: im.gmtCreate || '',
            status: im.bizStatus
          });
        }
        if (inner.pagination && inner.pagination.totalNum) {
          totalPages = Math.ceil(parseInt(inner.pagination.totalNum) / sz);
        }
        window.__fetchProgress = p + '/' + totalPages + ' (n=' + window.__allRecords.length + ')';
        if (list.length < sz) break;
        await new Promise(r => setTimeout(r, 1500));
      }
      window.__fetchDone = true;
      window.__fetchProgress = 'DONE:' + window.__allRecords.length;
    } catch (e) {
      window.__fetchError = (e && e.message) ? e.message : JSON.stringify(e).slice(0, 200);
      window.__fetchProgress = 'ERR@' + window.__fetchProgress;
    }
  }
  run();
})()
```

返回 `undefined` 是预期的(IIFE 没 return),副作用已生效。

### Step 5: 轮询进度

`javascript_tool` 单次有 60s 超时,所以轮询窗口 ≤ 30s:

```js
new Promise(r => setTimeout(() => r({
  progress: window.__fetchProgress,
  done: window.__fetchDone,
  error: window.__fetchError,
  n: (window.__allRecords || []).length
}), 30000))
```

看到 `done:true` 进下一步,看到 `error` 非空报错。

**实测耗时:** 720 单 / 36 页约 **90-120 秒**(每页 1.5s sleep + 1-2s 请求)。

### Step 6: 聚合 + 拼 CSV + 一次性返回(浏览器侧)

**关键改造**:聚合完直接 `return` 一个大对象,把 summary CSV、orders CSV、stats、top 20 一次拿全。这样 LLM 不用再分多次往返调 `javascript_tool` 取数 —— **每次 LLM 调用都有断流风险,合并能显著降低后期任务失败率**。

```js
(function() {
  const recs = window.__allRecords || [];
  const byShop = {};
  for (const r of recs) {
    const k = r.shop || '(未知店铺)';
    if (!byShop[k]) byShop[k] = { count: 0, total: 0, earliest: '', latest: '' };
    byShop[k].count++;
    byShop[k].total += (parseFloat(r.amount) || 0) / 100;
    if (!byShop[k].earliest || r.t < byShop[k].earliest) byShop[k].earliest = r.t;
    if (!byShop[k].latest  || r.t > byShop[k].latest)  byShop[k].latest  = r.t;
  }
  const shops = Object.keys(byShop).map(k => ({ shop: k, ...byShop[k] }));
  shops.sort((a, b) => b.count - a.count);
  window.__shopAgg = shops;

  let summaryCsv = '﻿店铺名,订单数,总金额,最早申请,最晚申请\n';
  for (const s of shops) {
    const safe = (s.shop || '').replace(/"/g, '""');
    summaryCsv += '"' + safe + '",' + s.count + ',' + s.total.toFixed(2) + ',' + (s.earliest||'') + ',' + (s.latest||'') + '\n';
  }
  window.__summaryCsv = summaryCsv;

  let ordersCsv = '﻿订单号,店铺,金额(元),申请时间,状态\n';
  for (const r of recs) {
    const amtYuan = ((parseFloat(r.amount) || 0) / 100).toFixed(2);
    ordersCsv += '"' + r.oid + '","' + (r.shop||'').replace(/"/g, '""') + '",' + amtYuan + ',' + r.t + ',' + r.status + '\n';
  }
  window.__ordersCsv = ordersCsv;

  // 一次性 return 全部数据,LLM 直接拿去 write_file + 展示 — 不用再调 javascript_tool 来回取
  return {
    summaryCsv: summaryCsv,
    ordersCsv: ordersCsv,
    stats: {
      shops: shops.length,
      recs: recs.length,
      sumLen: summaryCsv.length,
      ordLen: ordersCsv.length,
      grandTotal: shops.reduce((a, s) => a + s.total, 0).toFixed(2)
    },
    top5: shops.slice(0, 5)
  };
})()
```

**注意**:这个 IIFE **有 `return`**,跟 SKILL.md 总则里"text 不要写 return"的禁忌不冲突 —— 那个禁忌指的是直接在 `text` 顶层写 `return foo`(语法错);IIFE 内部 `return` 是合法 JS。`javascript_tool` 把 IIFE 的返回值原样吐回。

如果 ordersCsv 太大(50KB+) 触发 spill-to-disk,把 return 改成只含 `summaryCsv + stats + top5`,orders 单独从 `window.__ordersCsv` 取(分块或降级)。

### Step 7: 直接 write_file 写到任务输出目录

Step 6 的 IIFE 返回值已经包含 `summaryCsv` 和 `ordersCsv` 两个字符串 —— **不需要再调 `javascript_tool`**,直接用 builtin `write_file`:

```
write_file(path="1688_applying_invoices_summary.csv", content=<Step 6 返回的 summaryCsv>)
write_file(path="1688_applying_invoices_orders.csv",  content=<Step 6 返回的 ordersCsv>)
```

**关键 — 用相对路径只填文件名,不要 `<cwd>/...` 也不要绝对路径**。builtin `write_file` 会自动解析到当前任务的输出目录(`outputs/<worker_id>-<timestamp>/`),master 任务完成后扫描该目录并把文件通过派任务的 IM 通道(飞书等)发回用户。如果你写**绝对路径**到项目根,**master 扫不到,用户拿不到文件**。

**如果 Step 6 的 IIFE 返回 ordersCsv 时被 spill 到磁盘**(报错 "exceeds maximum allowed tokens"):
1. 只调一次 `javascript_tool` 单独取 `window.__ordersCsv` 字符串(可能再次 spill,继续看下一步)
2. 若仍 spill,降级:**只写 summary,跳过 orders**。任务部分成功,用户也能拿到主报告
3. 不要用 `a.click()` 浏览器下载 —— 用户偏好 write_file,见 `feedback_no_browser_download`

### Step 8: 在对话展示 Top 5(短输出,避免 LLM 末段断流)

Step 6 的 IIFE 返回值已经包含 `top5`(店铺数最多的 5 家),**不需要再调 `javascript_tool`**。直接用这 5 条生成一个**简短** Markdown 表格,5 列:`# / 店铺 / 单数 / 金额(¥) / 最早申请`,不写备注、不写长说明。例如:

```
| # | 店铺 | 单数 | 金额(¥) | 最早申请 |
|---|---|---|---|---|
| 1 | 1688选品中心官方供应链 | 92 | 6214.73 | 2026-04-02 |
| 2 | 金华宅一族贸易有限公司 | 47 | 2234.40 | 2026-05-06 |
| ... |
```

末尾附一行总数提示:`共 N 家店铺,M 单待开,总金额 ¥X。完整 CSV 已写到 outputs/ 目录,通过飞书自动发回。`

**不要让 LLM 输出更长的内容**(每多生成 1k token,流截断概率显著上升;DeepSeek-v4-flash 的流式响应在多 tool 长对话末端尤其不稳)。

### Step 9: 完成 — 不再补充建议

任务到 Step 8 就**结束**,不要在对话里继续生成"催单建议 / 哪些可以放弃 / 优先级"这类长文 —— 那些预设知识已经在本 SKILL.md 的"备注语义"里,LLM 重复输出只会拉长响应、增加断流风险。

如果用户后续追问("哪些可以放弃催?"、"建议先催谁?"),那是**新一轮对话**的事,届时 LLM 可以从 worker 完成后回写的状态拿数据再答。

## 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `hasRealCaptcha:false` 但 `[class*=slider]` 命中 | `memeber-slider` 等 carousel 误判 | 只查 `.nc-container,.nc_wrapper,#nocaptcha,#nc_1_wrapper` |
| `FAIL_SYS_HSF_ASYNC_TIMEOUT` | 后端微服务超时 | 降 pageSize,加重试(已内置 1/3/8s 三档) |
| `result=[]` 但 `total>0` | 错把 `r.data.result` 当 `r.data.data.result` | 多剥一层 `.data` |
| 金额显示 ¥6,214,730 | 错把"分"当"元" | /100 |
| 0 元订单 | 赠品/满赠拆单 | 不用催 |
| 返回值超 ~50K 字符报错 | MCP 响应大小上限 | spill 到磁盘了,Python json.loads 读 |
| `text` 写了 `return` 报语法错 | `javascript_tool` 评估的是表达式不是函数体 | 删 return,只留表达式 |

## 不要做的事

- ❌ 模拟拖动滑块
- ❌ 用 `a.click()` 浏览器下载 CSV(见 `feedback_no_browser_download`)
- ❌ 冷启动直接 navigate 到 air.1688.com 子页 URL(见 `feedback_no_direct_iframe_url`)
- ❌ 在 `javascript_tool` 的 text 里写 `return`
- ❌ 凭印象列价格/竞品(见 `feedback_verify_third_party_products`)
- ❌ 把 `fapiaoV1` 目录名当成"用户在做发票项目"的证据(见 `feedback_no_folder_name_assumptions`)

## 改造方向

- 抓"已开具发票":改 `bizStatusList`(取值待确认,可在前端先 hook XHR 看 tab 切换时的请求参数)
- 按 PO 号过滤:`param` 加 `buyerOrderId`
- **自动催商家**:已实现,见 sibling skill `fapiao-1688-chase` — 输入 batch.md 文件,自动按家发催单消息(订单列表搜单 → 真鼠标点旺旺图标 → DOM 写入 + 发送)

## 输出文件

**用相对路径调 `write_file`**(自动落到 `outputs/<worker_id>-<timestamp>/`,master 检测后通过 IM 通道发回给用户):

- `1688_applying_invoices_summary.csv`(店铺汇总,实测 137 行 = 1 header + 136 店铺)
- `1688_applying_invoices_orders.csv`(订单明细,实测 721 行 = 1 header + 720 单)

CSV 用 UTF-8 BOM(`﻿` 字面值)开头确保 Excel 中文不乱码。
