# 05 — 多 LLM Provider 路由设计

## 前提

用户决策(见 `00-context-and-goals.md` 的硬约束 C2-C3):

- **不用 Anthropic Claude API**
- 用 OpenAI 兼容 API 的国内外 provider:GPT / DeepSeek / Qwen(千问)/ GLM(智谱)/ 等
- 所选模型**全部支持多模态输入**

这个组合 = 架构层面**只需要一个 OpenAI 兼容 adapter**(因为所有 provider 都用同一套 API,只是 `base_url` 和 `api_key` 不同)。

## 架构图

```
┌──────────────────────────────────────────────────┐
│ Agent Core (你的 Python 进程)                    │
│                                                  │
│ ┌──────────────────────────────────────────────┐ │
│ │ LLM Loop                                     │ │
│ │ (内部 canonical = OpenAI 格式,无需翻译)      │ │
│ └────────────────┬─────────────────────────────┘ │
│                  │                               │
│                  ▼                               │
│ ┌──────────────────────────────────────────────┐ │
│ │ Router (选 provider + model)                 │ │
│ │ - 按 task tag 路由                            │ │
│ │ - 失败 fallback 链                            │ │
│ │ - token budget / 限流应对                     │ │
│ └────────────────┬─────────────────────────────┘ │
│                  │                               │
│                  ▼                               │
│ ┌──────────────────────────────────────────────┐ │
│ │ Single OpenAI Adapter                        │ │
│ │ (~150 行 Python,核心 20 行)                  │ │
│ │                                              │ │
│ │ for each provider:                           │ │
│ │   OpenAI(base_url=..., api_key=...)          │ │
│ └────────────────┬─────────────────────────────┘ │
└──────────────────┼───────────────────────────────┘
                   │
                   │ HTTPS (chat.completions)
                   │
        ┌──────────┼──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼
   api.openai  deepseek.   bigmodel.  dashscope.  moonshot.
   .com/v1    com/v1      cn/api/   aliyuncs.   cn/v1
   (GPT)      (DeepSeek)  paas/v4   com/...     (Kimi)
                          (智谱 GLM) (Qwen)
```

## 支持的 Provider 一览

> **注意**:模型名 / 价格 / 端点都可能变化,接入前请到各 provider 官网核实最新信息。本表用于架构设计参考,不是 SLA。

| Provider | base_url | 主要模型(多模态优先) | Tool Calling | 备注 |
|---|---|---|---|---|
| **OpenAI** | `api.openai.com/v1` | `gpt-4o`, `gpt-4.1`, `o3` 等 | ✓ 原生 | 模板基准 |
| **DeepSeek** | `api.deepseek.com/v1` | `deepseek-chat`(V3), `deepseek-reasoner`(R1), 视觉模型(VL) | ✓ | reasoner 含 `reasoning_content` 字段 |
| **Qwen / DashScope** | `dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3-vl-max`, `qwen3-max`, `qwen-vl-plus` | ✓ | 部分 model 要 `enable_thinking=False` |
| **GLM / 智谱** | `open.bigmodel.cn/api/paas/v4/` | `glm-4v-plus`, `glm-4-plus` | ✓ | 偶尔 tool_call schema 转换有坑 |
| **Moonshot / Kimi** | `api.moonshot.cn/v1` | `kimi-k2`, vision 系列 | ✓ | 长上下文优势(256K+) |
| **豆包 / 字节** | `ark.cn-beijing.volces.com/api/v3` | endpoint 形式 ID(如 `ep-xxx`) | ✓ | model id 是部署 endpoint id,不是模型名 |
| **MiniMax** | `api.minimax.chat/v1` | `MiniMax-M1`, vision 系列 | ✓ | 鉴权字段有差异 |
| **阶跃星辰 / Step** | `api.stepfun.com/v1` | `step-1v-32k`, `step-1o-turbo-vision-32k` | ✓ | 国内合规场景 |

## 核心代码骨架

### 单 Adapter

```python
from openai import OpenAI
from dataclasses import dataclass

@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str

class LLMClient:
    """统一所有 OpenAI 兼容 provider"""

    def __init__(self, providers: list[ProviderConfig]):
        self.clients = {
            p.name: OpenAI(base_url=p.base_url, api_key=p.api_key)
            for p in providers
        }

    def chat(
        self,
        provider: str,
        model: str,
        messages: list,
        tools: list | None = None,
        images: list | None = None,  # List[(bytes, detail)]
        **kwargs
    ):
        client = self.clients[provider]

        # 如有图片,注入到最后一条 user message
        if images:
            messages = self._inject_images(messages, images)

        return client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            **kwargs
        )

    def _inject_images(self, messages, images):
        last = messages[-1]
        if last["role"] != "user":
            raise ValueError("images can only attach to user message")

        new_content = [{"type": "text", "text": last["content"]}]
        for img_bytes, detail in images:
            b64 = base64.b64encode(img_bytes).decode()
            new_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{b64}",
                    "detail": detail or "low",  # low / high / auto
                }
            })

        return messages[:-1] + [{"role": "user", "content": new_content}]
```

### Router

```python
@dataclass
class ModelChoice:
    provider: str
    model: str
    fallbacks: list = field(default_factory=list)

class LLMRouter:
    def __init__(self, llm_client: LLMClient, config: dict):
        self.llm = llm_client
        self.rules = config["routing"]["rules"]
        self.default = config["routing"]["default"]

    def call(self, messages, tools, task_tags, images=None):
        choice = self._pick(task_tags)
        chain = [choice] + choice.fallbacks
        last_err = None
        for c in chain:
            try:
                return self.llm.chat(
                    provider=c.provider,
                    model=c.model,
                    messages=messages,
                    tools=tools,
                    images=images,
                )
            except (RateLimitError, APIError, AuthenticationError) as e:
                last_err = e
                continue
        raise RuntimeError(f"All fallbacks failed. Last error: {last_err}")

    def _pick(self, task_tags: set) -> ModelChoice:
        for rule in self.rules:
            if self._matches(rule["match"], task_tags):
                return ModelChoice(
                    provider=rule["use"]["provider"],
                    model=rule["use"]["model"],
                    fallbacks=[ModelChoice(**fb) for fb in rule.get("fallback", [])],
                )
        return ModelChoice(**self.default)
```

## 路由策略(配置文件示例)

```yaml
# config/llm.yaml

providers:
  - name: openai
    base_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}

  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}

  - name: qwen
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: ${DASHSCOPE_API_KEY}

  - name: glm
    base_url: https://open.bigmodel.cn/api/paas/v4/
    api_key: ${GLM_API_KEY}

  - name: moonshot
    base_url: https://api.moonshot.cn/v1
    api_key: ${MOONSHOT_API_KEY}

routing:
  default:
    provider: qwen
    model: qwen3-vl-max

  rules:
    # === 1688 chase: tool 调用为主,vision 偶尔 fallback ===
    - match: { capability: 1688-chase }
      use: { provider: qwen, model: qwen3-max }
      fallback:
        - { provider: qwen, model: qwen3-vl-max }
        - { provider: deepseek, model: deepseek-chat }
        - { provider: openai, model: gpt-4o-mini }

    # === 看图找坐标 / UI 状态判断 ===
    - match: { sub_capability: vision-localization }
      use: { provider: openai, model: gpt-4o }
      fallback:
        - { provider: qwen, model: qwen3-vl-max }
        - { provider: glm, model: glm-4v-plus }

    # === 中文 OCR / 文档识别 ===
    - match: { sub_capability: chinese-ocr }
      use: { provider: qwen, model: qwen3-vl-max }
      fallback:
        - { provider: glm, model: glm-4v-plus }
        - { provider: openai, model: gpt-4o }

    # === 文本总结 / 翻译 / 知识 QA(无需视觉,走最便宜)===
    - match: { capability: [summary, translation, knowledge-qa] }
      use: { provider: deepseek, model: deepseek-chat }
      fallback:
        - { provider: qwen, model: qwen3-max }

    # === 经验沉淀(需要好的归纳能力)===
    - match: { capability: experience-distill }
      use: { provider: deepseek, model: deepseek-reasoner }
      fallback:
        - { provider: openai, model: gpt-4.1 }
        - { provider: qwen, model: qwen3-max }

    # === 长上下文(读大型知识库 / 长聊天记录)===
    - match: { requires_long_context: true }
      use: { provider: moonshot, model: kimi-k2 }
      fallback:
        - { provider: qwen, model: qwen-long }

global:
  on_rate_limit: try_fallback
  on_5xx: retry_3x_exp_backoff
  on_token_budget_exceeded: switch_to_cheaper

  screenshot_policy:
    default_detail: low
    max_images_per_call: 4
```

## Provider 实际差异点(写在适配层的细节)

虽然都叫 "OpenAI 兼容",细节差异要留意:

### DeepSeek

- `deepseek-reasoner`(R1)响应里有额外 `reasoning_content` 字段(OpenAI 标准没有),要单独处理或忽略
- 部分老模型 tool calling 不稳,默认走 `deepseek-chat`(V3.1)即可

### Qwen via DashScope

- 部分模型(如 `qwen3-max` 默认)开启 thinking 模式,响应慢且贵,要传 `enable_thinking=False`
- `qwen3-vl-max` 是当前主力 vision 模型,中文 OCR 优秀

### GLM(智谱)

- 历史上 `tool_choice` 强制工具调用有过 bug,建议只用 `"auto"`,别强制
- 个别响应字段(如 `finish_reason`)偶尔有自定义值(如 `"sensitive"` 表示被审核)

### Moonshot Kimi

- 主打长上下文优势,128K 起步
- 收费方式略不同(按 input/output 分别计价)

### 豆包 / 字节

- 模型 id 不是模型名(如 `doubao-pro`),是 **endpoint id**(如 `ep-20240xxx-xxx`)
- 接入前要在火山引擎控制台为每个 model 创建 endpoint

## 跨 provider 行为不一致的几个 corner case

| 行为 | 不一致表现 | 建议处理 |
|---|---|---|
| `finish_reason` 值 | 大多是 `"stop"` / `"tool_calls"` / `"length"`,国内有时返回 `"sensitive"`(审核拦截) | 把非标准值统一标记为 `"abnormal"`,触发告警 |
| `tool_choice` 强制 | 部分 provider 不严格遵守 `{"type": "function", "function": {"name": "X"}}` | 默认只用 `"auto"`,需强制时加 prompt 提示 |
| 流式响应 chunk 格式 | 大部分对齐,偶尔 `delta.tool_calls` 字段位置不同 | MVP 不用 stream,稳定后再加 |
| 图片 URL 格式 | OpenAI 用 `data:image/png;base64,...`,部分 provider 接受 https URL 但有上传限制 | 统一用 base64 |
| `max_tokens` 含义 | 大部分是 output limit,极少数(早期 GPT-3.5)是 total | 现代模型都不是问题 |
| 限流响应 | 都是 HTTP 429,但 reset 时间字段名不同(`retry-after` vs `x-ratelimit-reset`) | 写一个统一的 retry 装饰器 |

## 工程量

| 模块 | 工时 |
|---|---|
| LLMClient(单 adapter) | 0.5-1 天 |
| Router + 配置加载 | 1 天 |
| Provider 注册 + 各 provider 联调 | 1-2 天 |
| Fallback / 限流处理 | 1 天 |
| Tool calling quirk 兼容(各 provider) | 1-2 天 |
| 视觉路径 | 0.5 天 |
| 测试 + 兼容性回归 | 2-3 天 |
| **小计** | **6-10 天** |

## 几条避坑建议

1. **不要绑死 LangChain**:过度封装,出问题难调
2. **不要用 OpenAI Assistants API**:那是 stateful 私有 API,跨 provider 完全不兼容,要用 chat completions
3. **国内 provider 走 OpenAI compat 端点**:DeepSeek / Qwen / 智谱 / Moonshot 都有,**不要用各家原生 SDK**(那样每加一家都改代码)
4. **MCP 不挑 LLM provider**:Aggregator 暴露的 MCP 工具,LLM 通过你的 Agent runtime 调用,跟谁是 provider 无关
5. **提示词跨模型有差异**:Skill 的 markdown 是模型无关的,但某些 prompt(如经验沉淀)在不同模型上效果会差很多,要 A/B 测
6. **要不要用 LiteLLM**:不推荐。既然你已经全 OpenAI 兼容,自己 ~150 行就搞定,不需要 LiteLLM 那层依赖(它支持多家,但额外抽象)
7. **token 计量自己做**:各 provider 计费小有差异(Anthropic 算 input/output 分开,有些计 total),自己写 accounting 才能准确比成本

## 一图总结

```
LLM 选型:全 OpenAI 兼容(不用 Claude)+ 全多模态
├─ 1 个 Python adapter(150 行)
├─ Router 按 task tag 选 provider+model
├─ Fallback 链应对限流/失败
└─ 配置文件驱动,不改代码加 provider

工程: 6-10 天 / 维护极轻
月成本: 取决于 token 用量,混合路由能省 50-70%
```

## 下一步

- 多模态视觉怎么用 → `06-multimodal-vision.md`
- 工程分阶段实施 → `07-implementation-roadmap.md`
- 成本估算 → `08-tech-stack-and-costs.md`
