# 01 — 当前已有产出与进度

## 已有的 Skill(2 个)

### Skill 1: `fapiao-1688`(抓数据)

**位置**: `C:\Users\<user>\.claude\skills\fapiao-1688\SKILL.md`

**功能**: 抓取 1688 买家账号下"申请中发票"全部订单,按店铺聚合后生成两个 CSV 文件。

**触发场景**: 用户问"哪些发票没开"、"申请中发票"、"1688 待开票"等。

**关键技术决策(写入 skill 的)**:
- 入口必须从 `work.1688.com/?_path_=buyer2017Base/2017buyerbase_trade/buyerMyInvoice` 进 — 不要直接打 air.1688.com 子页 URL(冷启动会触风控)
- mtop API: `mtop.1688.kingsuns.invoice.dataline.service` + `serviceId: KsInvoicePurchaserManageMtopService.queryInvoiceApplyRecordListByPage`
- 申请中状态: `bizStatusList: [1, 5, 6, 40]`
- 金额单位坑:`invoiceModel.amount` 是**分**,要 /100 才是元

**输出文件**:
- `<cwd>/1688_applying_invoices_summary.csv`(店铺汇总,~136 行)
- `<cwd>/1688_applying_invoices_orders.csv`(订单明细,~720 行)
- 均用 UTF-8 BOM 开头确保 Excel 中文不乱码

### Skill 2: `fapiao-1688-chase`(催单)

**位置**: `C:\Users\<user>\.claude\skills\fapiao-1688-chase\SKILL.md`

**功能**: 按 `chase_messages_batch*.md` 文件,在 1688 旺旺给每家商家发"催开票"消息(第一句催促 + 第二句订单号列表)。

**触发场景**: 用户问"发催单消息"、"按 batch 文件催"等。

**关键技术决策(写入 skill 的)**:

| 决策点 | 选择 | 原因 |
|---|---|---|
| 入口 | 订单列表搜订单号 → 点旺旺图标 | 让 1688 自己生成 IM URL,referrer 自然 |
| 搜索按钮点击 | `mcp__open-claude-in-chrome__computer` 真鼠标 | Q-BUTTON(Lit Web Component)对 `.click()` 不响应 |
| 旺旺图标点击 | 真鼠标 + JS 算精确坐标 | 同上,且位置动态(每行一个) |
| Q-INPUT 设值 | JS native setter + dispatch input event | Quark UI 受控,要触 React state |
| IM 编辑器输入 | `document.execCommand('insertText', ...)` | contenteditable 编辑器,触框架 input 事件 |
| 发送 | `button.send-btn` 的 `.click()` | 这个能触发(原生 button) |
| 消息长度限制 | 500 字符硬上限 | UI 截断,超的会被吃掉 |
| 滑块策略 | 不模拟拖动,提示用户手动 | 模拟会触发更严风控 |
| Tab 清理 | 每家发完 `window.close()` | 不然 tab 越积越多 |

## 已抓数据

**位置**: `d:\ai\fapiaoV1\`

| 文件 | 内容 | 状态 |
|---|---|---|
| `1688_applying_invoices_summary.csv` | 136 家店铺汇总(订单数 / 总金额 / 最早申请 / 最晚申请) | ✓ 完整,2026-05-22 抓 |
| `1688_applying_invoices_orders.csv` | 720 条订单明细 | ✓ 完整,同日抓 |
| `chase_messages_batch1.md` | 第一批 5 家催单文案(用户手工筛选) | ✓ |
| `chase_messages_batch2.md` | 第二批 65 家(脚本筛选: 最早申请 > 7 天 + 总金额 > ¥100) | ✓ |

**数据规模**:
- 总未开发票: 720 单 / 136 家店铺 / **¥56,739.42**
- 跳过 1688 自营店铺: ¥6,214.73(92 单) + ¥2,277.78(38 单) = ¥8,492.51,不催
- 满足"7 天 + ¥100"催单条件: **65 家 / 302 单 / ¥27,826.57**

## 催单进度(2026-05-22)

### Batch1(5 家手选,均已完成)

| # | 店铺 | 单数 | 金额 | 已发消息数 | 商家回复 |
|---|---|---:|---:|---:|---|
| 1 | 金华宅一族贸易有限公司 | 47 | ¥2,234.40 | 3 (msg1 用户手发 + msg2 两段我发) | ✓ "我们反馈下" |
| 2 | 义乌市柯松电子商务商行 | 44 | ¥3,783.20 | 3 | ✓ |
| 3 | 洛阳丝路起点商贸有限公司 | 30 | ¥1,183.10 | 3 | ✓ |
| 4 | 湖北达利食品有限公司 | 27 | ¥1,819.30 | 3 (过滑块后) | ✓ |
| 5 | 长沙新润食品有限公司 | 22 | ¥2,105.60 | 2 (订单数刚好 22,1 条够) | ✓ "在的亲亲" |

### Batch2(65 家脚本筛选,进行到 #16)

| # | 店铺 | 状态 |
|---|---|---|
| 1 | 山东玉膳房食品有限公司 | ✓ 已发 |
| 2 | 宿迁市博进商贸有限公司 | ✓ 已发 |
| 3 | 合肥市瑶海区凯岩城食品商行 | ✓ |
| 4 | 合肥长江批发市场万佳富食品商行 | ✓ |
| 5 | 江西樟树市正康医药生物科技有限公司 | ✓ |
| 6 | 合肥乐新电子商务有限公司 | ✓ |
| 7 | 重庆致华电子商务有限公司 | ✓ (滑块一次,过滑块后重发) |
| 8 | 陕西花椒世家科技有限公司 | ✓ |
| 9 | 山东物选聚品供应链管理有限公司 | ✓ |
| 10 | 成都蜀道香食品有限公司 | ✓ |
| 11 | 重庆桦彩电子商务有限公司 | ✓ |
| 12 | 日本叮叮品牌旗舰店 | ✓ |
| 13 | 陕西优之选电子商务有限公司 | ✓ |
| 14 | 汕头市澄海区良荣玩具商行 | ✓ |
| 15 | 重庆立侨伞业有限公司 | ✓ |
| 16 | 福州清晨启航网络科技有限公司 | ✗ **中断**,IM tab 已开(`touid=cnalichn清晨启航ihang`),消息未发 |
| 17-65 | 等等 | ✗ 未开始 |

**Batch2 中断原因**: 用户切去讨论多账号架构,会话内未继续推进。这批的剩余 50 家可以:
- 选项 A:用现有 skill 继续在同会话/新会话跑(每家 ~25-30 秒,~25-30 分钟搞完)
- 选项 B:等新自建 Agent 平台上线后用新平台跑
- 选项 C:已发的 15 家足够验证效果,batch2 剩下的不发了

## 实测发现(写入 skill 但值得单列出来)

### 风控规律

- 一个买家账号短时间(2 分钟内)开 **3-7 个新 IM 会话**会触滑块
- 滑块拖完后阈值会重置,继续可以再开几个
- 总体节奏:每开 5-7 家会有 1 次滑块
- Batch1 + Batch2 实测 21 家中触了 1 次

### 商家回复观察(初步观察,非统计)

- 食品类商家回复较积极(说"我们催催")
- 1688 自营店铺无人回(确认应该跳过)
- 大多数回复在发送后 5-30 分钟内出现
- 主流回复:"在的亲亲" / "好的" / "已收到,会安排" 等

### 技术坑(已克服)

| 现象 | 原因 | 处理 |
|---|---|---|
| Q-BUTTON `.click()` 不触发 | Lit Web Component 不响应 JS click | 用 `mcp__open-claude-in-chrome__computer` 真鼠标 |
| JS 模拟输入 React 不接受 | Ant Design 受控组件 | 用 native setter + dispatch input event |
| 直接 navigate iframe URL | 缺 referrer 触风控 | 走主页 `work.1688.com/?_path_=...` |
| IM tab WebSocket 没握手好就发送失败 | 连接还在建立 | 等 4-5 秒再 send |
| 大消息(>500 字符)被截断 | UI 硬限 | 切片每段 ≤480 字符 |
| 旧 IM tab 不关导致越积越多 | window 没自动关 | 每家发完 `window.close()` |

## 当前架构(单机单账号)

```
Claude Code (用户机)
  │
  │ MCP (stdio)
  ▼
open-claude-in-chrome 扩展
  │
  │ Chrome runtime
  ▼
Chrome (已登录账号 A)
  ├── work.1688.com (主入口)
  ├── air.1688.com/.../trade-order-list (订单列表 tab)
  └── air.1688.com/.../def_cbu_web_im?touid=... (IM tab × N)
```

## 后续接力提示

**如果你接手这个项目,优先做的事:**

1. 跟进 batch1 + batch2 已发 21 家的开票情况(7 天观察期)— 这是后续投入的业务依据
2. 阅读 `02-multi-account-deployment.md` 理解多机多账号怎么布
3. 阅读 `07-implementation-roadmap.md` 看分阶段开发顺序

**如果你只是想先把 batch2 剩下的 50 家发完**:
- 用现有 `fapiao-1688-chase` skill 即可
- 切到 `d:\ai\fapiaoV1\` 工作目录
- 跟 Claude Code 说"继续 batch2 从 #16 开始"
- 准备好手动拖滑块(预计 8-12 次)
