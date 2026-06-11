---
name: fapiao-1688-chase
description: 按 chase_messages_batch*.md 文件,在 1688 旺旺 IM 给每家商家发"催开票"消息(第一句催促 + 第二句订单号列表)。每家流程:订单列表搜该商家的一个订单号 → 真鼠标点旺旺图标 → 新 IM tab 自动开会话 → DOM 写入 + 点发送按钮。需要 `open-claude-in-chrome` MCP 已连接,用户的真实 Chrome 已登录 1688 买家账号。触发场景:用户问"发催单消息"、"按 batch 文件催"、"给商家发开票催单",或直接 /fapiao-1688-chase。
---

# 1688 IM 催开票自动发送

跟 `fapiao-1688`(抓数据 / 生 CSV)配套用 —— 那个产 batch 文件,这个发消息。

## 何时调用

- 用户准备好了 `chase_messages_batch*.md`(参见下面"输入文件格式")并说要发
- 用户问"催单消息怎么发"、"给商家发开票催单"、"按批次发催单"
- 用户直接 /fapiao-1688-chase

## 前置条件

1. `open-claude-in-chrome` MCP 已连接
2. 真实 Chrome 已登录 1688 买家账号
3. 当前工作目录有 `chase_messages_batch*.md`
4. 用户当前以及之前 5-10 分钟内**没大量在 1688 触发滑块** —— 已有警觉时先停一会儿再开始

## 输入文件格式 (chase_messages_batch1.md)

按家分块,每家:店铺名 + 单数 + 金额 + 两句话。示例:

```markdown
## 1. 金华宅一族贸易有限公司 (47 单, ¥2234.40)

**第一句:**
你好，我后台申请的发票，2234.4元这些还没开票，麻烦加急开票，5月30日之前开出来，开出来之后告诉我一下，谢谢。

**第二句:**
这些订单号： 3299805374626531283、3300236005696531283、...

---
```

**关键约束:**
- 跳过 0 元订单的"无门槛红包"等赠品
- 金额 < ¥100 的可以不催(性价比低)
- 自营店铺(店名含"官方供应链" / "1688 跨境官方全球店"等)**也尝试发** —— 部分自营店其实有客服旺旺,实测可发。失败就跟普通店一样自然报错,不要预先跳过

## 关键技术事实(实测)

### 订单列表页(用来"按订单号搜 → 点旺旺图标")

- 入口:`https://work.1688.com/?_path_=buyer2017Base/2017buyerbase_trade/buyList` 内嵌 iframe `trade.1688.com/order/buyer_order_list.htm`(跨域,主 frame 进不去 iframe)
- **顶级页直链**:`https://trade.1688.com/order/buyer_order_list.htm` (会自动 302 到 `https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html?page=1&pageSize=10`),referer 是空白 tab 即可,会正常渲染
- 页面**重度用 Lit Web Components + Shadow DOM**(实测 479 个 shadow root!) —— 普通 `document.querySelectorAll` **不穿透**,要写递归 walk:

```js
function* walk(node) {
  if (!node) return;
  yield node;
  if (node.shadowRoot) yield* walk(node.shadowRoot);
  for (const c of node.childNodes || []) if (c.nodeType === 1) yield* walk(c);
}
```

- **订单关键词**输入框: `Q-INPUT` 自定义元素,内层 `input.quark-input` 的 placeholder = `"商品名称/订单号/下游订单号/运单号/批次号"`
- **卖家**输入框: 另一个 `Q-INPUT`, placeholder = `"卖家登录名/公司/店铺名"`(用公司名或者旺旺号都行)
- **搜索按钮**: `Q-BUTTON` text = "搜索",class 含 `q-button-action`,位置约 (1572, 390),颜色橙色
- **旺旺图标**: `WANG-WANG` 自定义元素 + `div.wangwang-wrapper`,约 14x14 像素,在卖家列每行店铺名左边。每行一个

### Q-INPUT 设值 vs Q-BUTTON 点击 —— **完全不同的难度**

| 操作 | 用 javascript_tool 行吗 | 备注 |
|---|---|---|
| 设 Q-INPUT value | ✅ 行 | `qInput.value=x` + 内层 input 也设 + dispatch `input`/`change` 事件 (composed:true) |
| 点 Q-BUTTON | ❌ 不行 | `.click()` 不触发其内部点击处理器 → **必须用 `mcp__open-claude-in-chrome__computer` 真鼠标 left_click** |
| 点 WANG-WANG 图标 | ❌ 不行 | 同上,真鼠标点 |

`mcp__open-claude-in-chrome__computer` 是 Computer Use API,支持坐标 left_click。**只在 JS 触不动时用**,不是默认。

### IM 聊天页

- 联系卖家直链格式:`https://air.1688.com/app/ocms-fusion-components-1688/def_cbu_web_im/index.html?touid=cnalichn<loginid>&siteid=cnalichn&status=1&orderId=<orderId>#/`
- 点旺旺图标后 1688 页面自己生成这个 URL 并 `window.open(...)` → 新 tab
- 不要自己直接 navigate 这个 URL —— 自己拼**触风控的可能性**比让页面生成更高 (1688 自己生成时 referrer / state 链路是健康的)
- 页面有内嵌 iframe `def_cbu_web_im_core`(**同源,可跨 frame 访问**),里面是聊天界面
- 编辑器:`pre.edit[contenteditable="true"]` 在 iframe 里
- 发送按钮:`button.send-btn` 在 iframe 里
- 发送 走 WebSocket → `wss://wss-cntaobao.dingtalk.com/` → `/r/MessageSend/sendByReceiverScope`(1688 IM 跑在钉钉 IM 底座)

### 字符数限制

- **500 字符硬限制** —— UI 显示 "X / 500",超过的部分会被截掉(尝试过塞 946 字符 → 实际只接受 840 那种异常状态,送不出去)
- 单条 msg #1 (催促语) 约 50-60 字符,稳过
- 单条 msg #2 (订单号列表):订单号 19 位 + 顿号 + "这些订单号： " 前缀
  - 26 单以下:8 + N\*19 + (N-1) ≤ 500 → ~22-23 单一条搞定(注意还要包含分隔符,实测 22 单 = 447 字符 ✓)
  - 多于 22 单:分两段。第一段带"这些订单号： "前缀,第二段不带,直接顿号续上
  - 极少数 30+ 单的大店:可能要分 3 段 —— 实际现在见到的所有店都能 ≤ 2 段

### 风控

- 短时间(几分钟内)**连续打开 ≥ 3 个新 IM 会话**容易触发滑块验证("拖动滑块出现完整的两个房子后就松开")
- 实测的 3 → 第 4 家触发滑块。再发滑块后顺利(说明 1 次过卡 ≈ 重置触发阈值)
- **不要自己模拟拖滑块**(会触发更严风控 / 封号)
- **必须提示用户手动拖**,等用户回复"过了"再继续。可以让用户**刷新该 IM tab**(`location.reload()`)恢复发送

弹窗 / 非风控提示窗的处理见 worker 全局指引(系统提示开头) —— 简单来说:**带 × 关闭按钮的自己点掉,不要打扰用户**。

## 截图原则(读这条,严格执行)

**本 skill 全程几乎不需要截图**。所有元素位置都可以用 JS `walk()` + `getBoundingClientRect()` 精确算出来(本 skill 用 Lit Web Components,JS 可以穿透 shadow root)。截图 + 多模态描述每次成本 ~30 秒,**不必要的截图直接拖慢整个流程**。

### 何时可以截图(只有这两种):
1. **JS 找元素返回 null / 找到多个不能确定哪个对** —— fallback,截图给多模态看,要它告诉你目标位置
2. **滑块拦截(关键词检测到"拖动滑块"/"安全验证")** —— 截图给用户看,让人工拖

### 绝对不要截图的情况:
- ❌ 输完订单号后想"确认输进去了" —— 信任 JS `qInput.value = x` + 事件分发
- ❌ click 搜索按钮后想"确认搜出来了" —— 用 `walk()` 找 `WANG-WANG` 元素数量,有则成功
- ❌ 点旺旺图标后想"确认 IM tab 开了" —— 用 `tabs_context_mcp()` 看 tab 列表
- ❌ 发完一条消息后想"确认发出去了" —— 看 `editor.textContent.length === 0`(刚被清空)
- ❌ 任何"我刚做了 X,看看是不是真生效了"的好奇心截图

如果你违反这些规则截了图,**任务会从 1 分钟拖到 5 分钟**,用户会不高兴。

## 执行步骤

### Step 0: 解析 batch 文件

读 `chase_messages_batch*.md`,提取每家:
- 店铺名 (#编号. 名称 中的"名称"部分)
- 单数 / 金额 (用于校对 / 风险评估,不参与发送)
- **第一句**完整文本
- **第二句**完整文本(去掉 "这些订单号： " 前缀? 不,前缀也是消息一部分)
- 从第二句里提取**第一个订单号**(用于在订单列表里搜) —— 切 "、" 取第一个 19 位数字

### Step 1: 准备订单列表 tab

```
tabs_context_mcp(createIfEmpty=true) → 返回 { availableTabs: [{tabId, title, url}, ...] }
```

如果列表里已经有 `air.1688.com/.../trade-order-list/buyer-order-list.html` 的 tab,**复用它的 tabId**(navigate 重新搜下一家也行)。否则用列表里第一个 tabId,navigate 到目标 URL:

```
navigate(tabId, 'https://trade.1688.com/order/buyer_order_list.htm')
wait 6s   ← 实测 4s 不够,Lit SPA 渲染要 5-6s
```

URL 会自动 302 到 `https://air.1688.com/app/ctf-page/trade-order-list/buyer-order-list.html`,referer 是空白也能正常渲染。

### Step 2: 对每家商家循环

#### 2.1 + 2.2 一次 JS 搞定:塞订单号 + 返回搜索按钮中心坐标

**省 1 步/家** —— 同一次 JS 既设值又算坐标,然后 1 次 computer.left_click。每家流程总步数从 8 降到 7。

**⚠️ 必须严格使用下面这段 JS,逐字复制,不要"简化"或换 `document.querySelector`**:

- ❌ 不要写 `document.querySelector('input[placeholder*="订单号"]')` —— 这种穿不透 shadow root,在 1688 页上**永远返回 null**(Q-INPUT 把真 input 藏在 shadow root 里)。LLM 经常上当 "简化" 成这种写法,然后失败,然后跑去截图,**浪费 30 秒多模态描述**。
- ❌ 不要因为下面的 JS 看起来"长"或"复杂"就用更短的写法替代
- ✅ 直接 copy-paste 下面整段,把 `<order_id>` 替换成实际订单号

**JS 模板(2026-05 dry-run 验证可用,逐字复制,只替换 `<order_id>`)**:

```js
function* walk(node) {
  if (!node) return;
  yield node;
  if (node.shadowRoot) yield* walk(node.shadowRoot);
  for (const c of node.childNodes || []) if (c.nodeType === 1) yield* walk(c);
}
(function() {
  // (a) 找订单关键词输入框
  let qInput = null;
  for (const n of walk(document)) {
    if (n.tagName === 'Q-INPUT' && n.shadowRoot) {
      const inner = n.shadowRoot.querySelector('input');
      if (inner && /商品名称|订单号/.test(inner.placeholder||'')) { qInput = n; break; }
    }
  }
  if (!qInput) return JSON.stringify({ error: 'Q-INPUT not found' });

  // 塞订单号 + 派发 input/change(必须两个都派,某些校验逻辑只听 change)
  const inner = qInput.shadowRoot.querySelector('input');
  qInput.value = '<order_id>';
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  inner.focus();
  setter.call(inner, '<order_id>');
  inner.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  inner.dispatchEvent(new Event('change', { bubbles: true, composed: true }));

  // (b) 找搜索按钮的实际中心坐标(窗口宽度不同,写死会偏)
  let btnCenter = null;
  for (const n of walk(document)) {
    if (n.tagName === 'Q-BUTTON' && /^搜索$/.test((n.textContent||'').trim())) {
      const r = n.getBoundingClientRect();
      if (r.width > 0) {
        btnCenter = { cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2) };
        break;
      }
    }
  }
  return JSON.stringify({ qInputValue: inner.value, btnCenter });
})()
```

**期望返回值**(实测 2026-05-24,Edge 最大化在 ~2560px 显示器):

```json
{"qInputValue":"3293626225672531283","btnCenter":{"cx":2035,"cy":390}}
```

如果 `btnCenter === null` 或返回 `{"error": "Q-INPUT not found"}` → fallback 截图(罕见,通常说明页面没渲染完,先 wait 多几秒再 retry)。

拿到 `btnCenter` **直接 click**(不要截图二次确认):

```
mcp__open-claude-in-chrome__computer(action='left_click', tabId, coordinate=[btnCenter.cx, btnCenter.cy])
wait 3s   ← 等搜索结果渲染
```

#### 2.3 JS 校验搜索结果 + 拿到 WANG-WANG 图标坐标

**JS 模板(dry-run 验证可用)**:

```js
(function() {
  // (a) 校验:商家名是否出现在 DOM(说明搜出了该商家的订单)
  // 用 .includes() 不用 regex —— 商家名常含括号 / 特殊字符,regex 难写对
  const expectedShop = '<expected-shop-name>';   // 例如 "重庆致华电子商务有限公司"
  let sellersFound = 0;
  for (const n of walk(document)) {
    if (n.children && n.children.length === 0
        && (n.textContent||'').includes(expectedShop)) {
      sellersFound++;
    }
  }

  // (b) 找 WANG-WANG 图标精确坐标(所有可见的)
  const hits = [];
  for (const n of walk(document)) {
    if (n.tagName === 'WANG-WANG') {
      const r = n.getBoundingClientRect();
      if (r.width > 0) {
        hits.push({ cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2) });
      }
    }
  }
  return JSON.stringify({
    sellersFound,
    iconCenter: hits[0] || null,
    allIconCount: hits.length,
  });
})()
```

**期望返回值**(搜出 1 家时):

```json
{"sellersFound":1,"iconCenter":{"cx":1579,"cy":685},"allIconCount":1}
```

**判断逻辑**:
- `sellersFound === 0` 且 `allIconCount === 0` → 这家**没搜到订单**(可能订单号错 / 已售后取消),**跳过这家** + 报警
- `sellersFound === 0` 但 `allIconCount > 0` → 搜到了订单但 DOM 里没有店名(渲染没完),**多 wait 2s 重 JS 一次**
- `sellersFound > 0` 且 `iconCenter !== null` → ✅ 正常,继续 click 旺旺

#### 2.4 真鼠标点旺旺图标 + 找新 IM tab

```
mcp__open-claude-in-chrome__computer(action='left_click', tabId=订单列表 tabId, coordinate=[iconCenter.cx, iconCenter.cy])
wait 4s   ← 实测 3s 不够,IM tab 创建 + 初始页面渲染要 4s 才稳

tabs_context_mcp() → 返回 { availableTabs: [...] }
   ↓ 从 availableTabs 里找 URL 含 'def_cbu_web_im' 或 'touid=' 的 tab
   ↓ 这是新开的 IM 会话 tab,记下它的 tabId
```

**期望新 IM tab 的 URL 格式**:

```
https://air.1688.com/app/ocms-fusion-components-1688/def_cbu_web_im/index.html
  ?touid=cnalichn<商家旺旺号>
  &siteid=cnalichn
  &status=1
  &orderId=<订单号>
  #/
```

URL 里 `touid=` 后面的部分是 URL-encode 过的商家名,可以解码出来跟 expectedShop 二次校验。

#### 2.5 IM tab 发消息

切到新 IM tab,等会话加载完(约 5s 让 WebSocket 握手 + 历史消息加载),然后:

```js
new Promise(r => setTimeout(async () => {
  const f = Array.from(document.querySelectorAll('iframe')).find(x => /def_cbu_web_im_core/.test(x.src));
  const d = f.contentDocument;
  const editor = d.querySelector('pre.edit[contenteditable="true"]');
  const sendBtn = d.querySelector('button.send-btn');
  // 滑块检测
  if (/拖动滑块|安全验证/.test(document.body.innerText)) return r({ captcha: true });

  async function send(text) {
    editor.focus();
    d.execCommand('selectAll', false, null);
    d.execCommand('delete', false, null);
    await new Promise(rr => setTimeout(rr, 200));
    d.execCommand('insertText', false, text);
    await new Promise(rr => setTimeout(rr, 400));
    const before = (editor.textContent||'').length;
    sendBtn.click();
    await new Promise(rr => setTimeout(rr, 1800));
    return { len: text.length, before, sentOK: (editor.textContent||'').length === 0 };
  }

  const r1 = await send('<msg1>');
  await new Promise(rr => setTimeout(rr, 4000));   // 节奏 4s
  const r2 = await send('<msg2 part 1>');
  // 如果只 1 段够,跳过 part 2
  let r3 = null;
  if (<has_part2>) {
    await new Promise(rr => setTimeout(rr, 4000));
    r3 = await send('<msg2 part 2>');
  }
  r({ r1, r2, r3 });
}, 5000))
```

#### 2.6 发完即关 IM tab(必做)

每家发完最后一条消息后,**立即关掉该 IM tab**,避免 tab 越积越多(实测 65 家批次跑到 12 家时已经 14 个 tab 在留存,后续会膨胀到不可接受):

```js
// 在该 IM tab 上执行
window.close()
```

`window.close()` 对扩展/脚本打开的 tab 可用(IM tab 都是 wangwang 图标点击触发 window.open 的,可关)。不需要 `mcp__open-claude-in-chrome__close` 之类的工具(也没这个工具)。

唯一不关的 tab:订单列表 tab(`buyer-order-list.html`,要复用搜下一家)和当前正在收回复的会话(用户可能想看)。如果用户要求"保留有回复的 tab",看 tab title 含 `【你有新消息】` 的不关。

#### 2.7 滑块处理(如果命中)

如果 `r.captcha` 为真,或某条 send 返回 `sentOK: false`:
1. 截图给用户看(`computer.screenshot`)
2. **明确告诉用户**:"被滑块拦了,在 tab \<tabId\> 手动拖一下,完了告诉我"
3. 等用户说"过了"
4. `location.reload()` 该 tab(刷新会保留 URL 包括 touid 参数)
5. 等 5s,重发未完成的消息
6. 之后所有家**多加 2-3s 间隔**

### Step 3: 进度展示 + 总结

实时更新 TodoWrite(每家一个 todo,状态 pending → in_progress → completed)。

全部跑完后给个表格:商家 / 单数 / 金额 / 消息数 / 状态(✓ / 滑块过 / 失败)。

提示用户:很多商家会在 5-30 分钟内回复 "在的"/"我们催催" —— 几分钟后可以在 IM tab 看回复。

## 字符切分计算(msg #2 拆段)

```js
function splitOrders(allOrdersStr, prefix = '这些订单号： ') {
  const orders = allOrdersStr.split('、').map(s => s.trim()).filter(Boolean);
  const sep = '、';
  const limit = 480;  // 留 20 字符 buffer
  const parts = [];
  let cur = prefix;
  let count = 0;
  for (const o of orders) {
    const add = (cur === prefix || cur === '' ? '' : sep) + o;
    if ((cur + add).length > limit) {
      parts.push(cur);
      cur = o;  // 续段不带 prefix
    } else {
      cur += add;
    }
  }
  if (cur) parts.push(cur);
  return parts;
}
```

22 单测试:`447 字符,1 段`。47 单:`24+23 拆成两段`。

## 不要做的事

- ❌ 自己拼 IM 直链 navigate(参数化"touid=..."的 URL 看着像异常路径,比让页面生成多一倍风险)—— 走"订单列表 → 点旺旺图标"让页面自己生成 URL
- ❌ 用 javascript_tool `.click()` 点 Q-BUTTON / WANG-WANG —— Lit 自定义元素的点击不响应这个,**真鼠标 computer.left_click 才行**
- ❌ 模拟拖动滑块(触发更严风控)
- ❌ 一次塞一条超长(>500 字符)消息 —— 被截,送不出去,还浪费 1 个发送次数
- ❌ 短时间(2 分钟内)连续打开 ≥ 3 个新 IM 会话 —— 极大概率触滑块
- ❌ 不验证商家名就发消息 —— 万一点错图标发错对方
- ❌ 大批量跑时**不关已发完的 IM tab** —— 65 家批次跑完会留 60+ 个 tab,浏览器内存爆/用户找不到要看的会话。**每家发完 `window.close()` 关掉**
- ❌ **为了"验证刚做的操作有没有生效"截图** —— 每张截图 30s+ 多模态描述,跑 20 家就多花 10 分钟。验证方式见上面"截图原则":验证 type 看 input.value,验证 click 看后续 JS 找到了新元素,验证 IM 发送看 `editor.textContent.length === 0`
- ❌ **每个 step 都截图获取下一步坐标** —— JS `walk()` + `getBoundingClientRect()` 能算出 Q-BUTTON / WANG-WANG 的精确屏幕坐标,比截图多模态快 10 倍且精确。只在 JS 找不到对应元素时(`null` 返回)才 fallback 到截图

## 改造方向(用户可能问)

- 自动生成 chase_messages_batch.md 文件(从 fapiao-1688 的 CSV 推导) —— 可写成新 skill
- 第二天自动追问:`/fapiao-1688-chase --check` 重读 CSV,看哪些已开票,哪些没开 → 对没开的再发一轮
- 多账号轮发:不同浏览器配置多个买家账号,降低单账号风控阈值

## 相关 skill

- `fapiao-1688`:抓"申请中发票"数据,生成 CSV —— 是本 skill 的上游输入源
