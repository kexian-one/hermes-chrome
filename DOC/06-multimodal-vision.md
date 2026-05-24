# 06 — 多模态(视觉)能力利用

## 前提

用户决策(见 `00-context-and-goals.md` 硬约束 C3):**所选 LLM 全部支持多模态输入**。

这意味着架构层面我们可以**默认假设视觉可用**,不需要按"是否需要视觉"分支路由模型。但**实际使用要克制**,因为 image token 比 text token 贵。

## 视觉在我们这个系统里的作用

### 主要场景 1: GUI 自动化(chase 等)的兜底

**主路径**(不需要视觉):
1. JS 找元素 (`document.querySelector` / `getBoundingClientRect`)
2. JS 返回坐标
3. 真鼠标点击该坐标

**视觉兜底路径**(主路径失败时):
1. 主路径返回"找不到元素"(DOM 结构变了)
2. Aggregator 截图当前页面
3. 让 LLM 看图找按钮/图标位置
4. 用 LLM 返回的坐标点击

这条兜底大大提升对 **1688 改版 / UI 变更的鲁棒性**。

### 主要场景 2: 滑块识别(强化检测)

**纯文本检测**(快但脆):
- `document.body.innerText.includes('拖动滑块')`
- 局限:换种文案可能漏检

**视觉检测**(慢但稳):
- 截图 → LLM 看图 → 判断"这是什么类型的验证"
- 能识别多种滑块变体(拖出小房子 / 拼图 / 选字 / 旋转 / 等)

**推荐策略**:**主要靠文本检测,视觉作为可选的复核**(节省 token)。

### 主要场景 3: 商家回复理解

商家可能发:
- 文字 → 文本就够
- **截图**(回复发票样张、订单截图、聊天截图等)→ 需要视觉
- 表情包 / 图片消息 → 需要视觉
- 语音(转成文字后) → 文本

视觉让 Agent 能直接理解商家发的截图,**不需要 OCR 中间环节**。

### 主要场景 4: PDF / 票据核验

商家开出发票后会发 PDF。视觉能力:
- 直接读 PDF 截图,提取税号、抬头、金额
- 比对订单原始信息
- 自动核验 → 入账

### 主要场景 5: 未来其他任务

- 看后台报表截图汇总数据
- 看商品图分类 / 判断质量
- 看物流截图判断状态
- 看其他 UI 截图做决策

## Image Token 成本(关键)

**视觉不是免费的**,要算账。

OpenAI / 国内 provider 的图片 token 计算大致(各家略不同,**接入前到官网核**):

| 图片大小 | detail | 估算 token | 用途建议 |
|---|---|---|---|
| 1080p+ | high | 1500-2000 | 看精细文本 / 复杂图表 |
| 1080p+ | low | 85-200 | 大部分场景 |
| 512×512 | low | 150 | icon / 局部区域 |
| 256×256 | low | 100 | 小图标 |

**经验法则**:能用 `detail=low` 解决的不用 `high`。视觉任务能预处理 crop 到目标区域,token 大幅降低。

## 设计原则

### 原则 1: 默认不附图

LLM Loop 默认**只用文本**。需要图的场景在 Aggregator 层显式触发。

### 原则 2: 截图策略由 Aggregator 决定

不是让 LLM 自己决定"我要看图"(LLM 习惯性总想看更多信息,会浪费 token),而是 Aggregator 按规则附:

```yaml
# config/screenshot_policy.yaml
auto_attach:
  # 真鼠标点击之前自动附一张(LLM 能看清要点的位置)
  - tool: computer.left_click
    before_call: true
    detail: low

  # 真鼠标点击之后附(验证有没有点对)
  - tool: computer.left_click
    after_call: true
    detail: low

  # 检测到可能滑块时附
  - on_event: captcha_suspected
    detail: high

  # 主路径 JS 找元素失败时附(进入 vision 兜底)
  - on_event: element_not_found
    detail: high
```

### 原则 3: Token 预算控制

每个任务有 token 上限,超了报警:

```python
class TokenBudget:
    def __init__(self, max_total, max_images):
        self.max_total = max_total
        self.max_images = max_images
        self.used = 0
        self.image_count = 0

    def can_attach_image(self) -> bool:
        return self.image_count < self.max_images

    def record(self, tokens, is_image=False):
        self.used += tokens
        if is_image:
            self.image_count += 1
        if self.used > self.max_total:
            raise TokenBudgetExceeded()
```

### 原则 4: 模型间视觉质量差异要测

虽然都支持视觉输入,**质量不同**:

| 任务类型 | GPT-4o | Qwen3-VL-Max | GLM-4V-Plus | 备注 |
|---|---|---|---|---|
| 看截图找按钮坐标 | 优 | 良 | 良 | 起步 GPT-4o,贵就切 Qwen |
| 看商家头像/产品图分类 | 优 | 优 | 良 | 都行 |
| 看 PDF 发票提取信息 | 优 | 优 | 优 | 都行 |
| 看 UI 截图描述 | 优 | 优 | 良 | 都行 |
| 看复杂图表 / 数据可视化 | 优 | 良 | 中 | 重要任务用 GPT |
| 看自然图片(实物 / 风景) | 优 | 优 | 良 | 都行 |
| **中文文字识别** | 良 | **优** | 优 | 中文场景 Qwen / GLM 反而胜 |

(以上是基于公开 benchmark 的相对评分,**接入后建议自己跑内部 benchmark**)

**Benchmark 建议**:
- 收集 20-30 张你**真实场景**的截图(1688 订单页 / IM 聊天 / 发票 / 等)
- 同一 prompt(如"找出搜索按钮位置"),各模型跑一遍
- 人工打分,写到 `knowledge/vision-bench.md`
- 据此更新 router 配置

## 视觉调用代码示例

```python
# 在 chase 流程里,JS 找元素失败的兜底
async def find_wangwang_icon_with_vision(client, tab_id):
    # 截图
    screenshot_bytes = await client.screenshot(tab_id)

    # 调 LLM 看图
    prompt = "下面是 1688 订单列表页面截图。找到第一个订单行里 '旺旺聊天' 图标(通常是橙色或蓝色聊天气泡,在卖家名前面)的中心坐标。返回 JSON: {x, y}"
    response = await llm_router.call(
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        task_tags={"sub_capability": "vision-localization"},
        images=[(screenshot_bytes, "low")],  # low detail 够找按钮
    )

    # 解析坐标
    coords = json.loads(extract_json(response.content))
    return coords["x"], coords["y"]
```

## 在 Aggregator MCP 层做视觉感知

把"自动附截图"和"视觉兜底"放在 Aggregator,**LLM 端无感知**:

```python
class AggregatorWithVision:
    def __init__(self, mcp_clients, policy):
        ...

    async def computer_left_click(self, account, tab_id, x, y):
        # 按 policy 决定是否截图
        screenshot_before = None
        if self.policy.attach_before("computer.left_click"):
            screenshot_before = await self._screenshot(account, tab_id)

        # 真实点击
        result = await self.mcp_clients[account].call(
            "computer", action="left_click", tabId=tab_id, coordinate=[x, y]
        )

        # 后置截图
        screenshot_after = None
        if self.policy.attach_after("computer.left_click"):
            screenshot_after = await self._screenshot(account, tab_id)

        return {
            "result": result,
            "screenshots": {
                "before": screenshot_before,
                "after": screenshot_after,
            },
            # 这些截图在 LLM Loop 里会被附到下一条消息
        }
```

## 一图速览

```
┌──────────────────────────────────────────────────┐
│ 全多模态前提下的视觉使用策略                       │
│                                                  │
│ 主路径:                                          │
│  - 文本 + JS DOM 操作(99% 场景)                 │
│  - 无 image token 成本                           │
│                                                  │
│ 视觉兜底:                                        │
│  - JS 找不到元素 → 截图 → LLM 视觉定位          │
│  - 滑块/异常 → 截图 → LLM 判别类型              │
│  - 商家发图 / PDF → 视觉理解                     │
│                                                  │
│ 控制层:                                          │
│  - Aggregator 按 policy 自动附截图              │
│  - Token budget 强制上限                         │
│  - 同 task 不超 4 张图(默认)                    │
│  - detail=low 默认,high 显式指定                │
│                                                  │
│ 模型选型:                                        │
│  - 起步:Qwen3-VL-Max(中文场景性价比高)        │
│  - 高质量任务:GPT-4o                             │
│  - 国内合规:GLM-4V-Plus                          │
│  - 实测建议每个 provider 跑 benchmark            │
└──────────────────────────────────────────────────┘
```

## 下一步

- 实施步骤 → `07-implementation-roadmap.md`
- 成本估算(含 token 预算) → `08-tech-stack-and-costs.md`
