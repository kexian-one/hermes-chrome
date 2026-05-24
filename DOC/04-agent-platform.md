# 04 — 自建 Agent 平台架构

> **⚠ Scope 降级(2026-05-23,见 [09c / Q8](09c-p2-deferred.md#q8-任务队列选型--redis-自建-vs-云上-vs-其他) 和 [09b / Q15](09b-p1-decisions.md#q15-单机-agent-拓扑--master--worker-架构))**
>
> 实际架构是 **每台机 1 master + 6 worker(单机自治)**,**跨机协调永久不做**(包括 Redis 队列、中央 dispatcher、多机 master 协调,这些全部归入"永不实施")。
> 飞书 / 钉钉接入延后到 Phase 2(NLU 入口),HTTP API 不做。
> 本文档下面描述的 Redis 队列、多机协调架构作为**历史背景保留**,不实施。
> **真正落地的 per-machine 架构见 [09b / Q15](09b-p1-decisions.md#q15-单机-agent-拓扑--master--worker-架构)**。

## 为什么要自建 Agent(而不是继续用 Claude Code)

| 维度 | Claude Code | 自建 Agent |
|---|---|---|
| LLM provider | 锁 Anthropic Claude(或 Cocode 兼容,但有限) | **任意 OpenAI 兼容** ✓ |
| 任务接入 | 用户在 CLI 手输 | **飞书/钉钉/API/Cron** ✓ |
| 知识沉淀 | 文件级 memory,无自动总结 | **每天/周 LLM 自动萃取** ✓ |
| 多任务复用 | 一个 session 一个任务 | **任务队列 + 并发** ✓ |
| 远程 skill | Plugin marketplace(有限) | **自托管 git + hook** ✓ |
| Computer use(看屏点击) | Claude 原生最强 | 各 vision 模型质量不一,需测 |
| 工具调用稳定性 | 极佳 | 看选的 LLM |
| 上手成本 | 0(已经用了) | **5-8 周开发** |

**结论**:Phase 1-2 用 Claude Code 把 1688 催发票场景验证完;Phase 3+ 转自建。Claude Code 是**临时阶段**,长期被替换。

## 平台总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          控制平面 (Control Plane)                    │
│                          可部署在云上 / 中心机 / NAS                  │
│                                                                     │
│  ┌──────────────────┐ ┌─────────────────┐ ┌──────────────────────┐ │
│  │  任务源接入        │ │  任务队列          │ │  Web Dashboard        │ │
│  │  - 飞书机器人      │ │  Redis Streams   │ │  - Agent 状态          │ │
│  │  - 钉钉机器人      │ │  (带消费组)        │ │  - 任务流水            │ │
│  │  - HTTP API       │ │  + tags 路由      │ │  - 触发手工干预         │ │
│  │  - Cron 定时        │ │                  │ │  - 看经验沉淀           │ │
│  │  - CLI / TUI       │ │                  │ │                      │ │
│  └────────┬──────────┘ └──────┬──────────┘ └──────────────────────┘ │
│           │                   │                                     │
│           └───────────────────┴──── 入队                            │
└─────────────────────────────────┼───────────────────────────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                                       │
              ▼                                       ▼
┌──────────────────────────────┐         ┌────────────────────────────┐
│  共享存储 (Data Plane)         │         │  执行平面 (Execution Plane) │
│                              │         │                            │
│  Git Repo:                   │         │  Agent PC #1               │
│   ├── skills/                │ ◄── 读 ── │   - 6 浏览器 / 账号        │
│   ├── knowledge/              │         │                            │
│   ├── memory/                │ ── 写 ─►│  Agent PC #2               │
│   └── prompts/               │         │   - 6 浏览器 / 账号        │
│                              │         │                            │
│  Vector DB(可选):            │ ◄ 检索 ─│  Agent PC #3               │
│   - Qdrant / Chroma         │         │   - 6 浏览器 / 账号        │
│                              │         │                            │
│  对象存储(可选):              │         │  (每台自带 Agent runtime  │
│   - S3 / MinIO / OSS         │         │   + Aggregator + LLM 调用)│
│   - 大文件 / 截图              │         └────────────────────────────┘
└──────────────────────────────┘
```

## 单个 Agent 的内部架构

```
┌──────────────────────────────────────────────────────────────┐
│  Agent (单进程, Python 3.11+)                                │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Task Worker (主循环)                                   │  │
│  │  1. 从队列拉一个属于本机的任务                            │  │
│  │  2. 解析 → 找匹配的 skill                                │  │
│  │  3. 进入 LLM Loop                                       │  │
│  │  4. 完成后写日志,触发经验提取(异步)                      │  │
│  └─────────────────────┬──────────────────────────────────┘  │
│                        │                                     │
│         ┌──────────────┼──────────────┬─────────────────┐    │
│         ▼              ▼              ▼                 ▼    │
│  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐│
│  │ Skill    │  │ Memory /    │  │ LLM Loop │  │ Tool Bridge  ││
│  │ Loader   │  │ Knowledge   │  │ (核心)    │  │              ││
│  │          │  │ Reader      │  │          │  │ - Aggregator ││
│  │ - 扫盘    │  │             │  │ - 拼 prompt│  │   MCP client ││
│  │ - 监听 git│  │ - 注入 ctx  │  │ - 调 LLM  │  │ - HTTP / 文件││
│  │   pull 后 │  │ - 向量检索  │  │   (多 prov)│  │ - 内置工具    ││
│  │   reload  │  │ - 本机 memo │  │ - 工具    │  │              ││
│  └──────────┘  └────────────┘  │   dispatch│  └──────────────┘│
│                                 └──────────┘                  │
│                        │                                     │
│                        ▼                                     │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Experience Extractor (后台,定时跑)                     │  │
│  │  - 每天 / 每周 扫本机最近日志                            │  │
│  │  - LLM 总结模式: "踩了什么坑"、"什么有效"                 │  │
│  │  - 输出 → knowledge/<topic>.md                          │  │
│  │  - git commit + push → 共享给所有 agent                  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Reporter                                               │  │
│  │  - 完成任务 → 飞书反馈                                   │  │
│  │  - 异常告警(账号挂、滑块超时、限流)                       │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

## 核心组件详解

### 1. Task Worker

主循环,从队列拉任务:

```python
async def main_loop():
    while True:
        task = await queue.consume(timeout=30)  # Redis xreadgroup
        if not task:
            continue
        
        try:
            await execute_task(task)
        except Exception as e:
            await reporter.error(task, e)
            await queue.requeue_with_backoff(task)


async def execute_task(task):
    # 1. 找 skill
    skill = skill_loader.match(task.payload, task.skill_hint)
    
    # 2. 拼 prompt
    messages, tools = await prompt_builder.build(skill, task)
    
    # 3. LLM Loop
    result = await llm_loop.run(messages, tools, task_tags=task.tags)
    
    # 4. 写日志
    await logger.write_task_log(task, result)
    
    # 5. 反馈
    await reporter.success(task, result)
```

### 2. Skill Loader

启动时扫盘,运行时支持热加载(git pull 后自动重载):

```python
class SkillLoader:
    def __init__(self, skill_dir):
        self.skill_dir = skill_dir
        self.skills = {}
        self._load_all()
        self._watch_for_changes()
    
    def _load_all(self):
        for skill_md in glob(f"{self.skill_dir}/*/SKILL.md"):
            parsed = self._parse_skill(skill_md)
            self.skills[parsed.name] = parsed
    
    def _parse_skill(self, path):
        """解析 Claude Code 格式的 SKILL.md(frontmatter + markdown body)"""
        with open(path) as f:
            content = f.read()
        # YAML frontmatter
        meta, body = split_frontmatter(content)
        return Skill(
            name=meta["name"],
            description=meta["description"],
            metadata=meta.get("metadata", {}),
            body=body,
        )
    
    def match(self, task_payload, skill_hint=None):
        """根据 task 找匹配的 skill"""
        if skill_hint and skill_hint in self.skills:
            return self.skills[skill_hint]
        # fallback:LLM 决定(把 task description + 所有 skill description 给 LLM)
        return self._llm_select(task_payload)
```

**关键决策**: 复用 **Claude Code 的 SKILL.md 格式**,不发明新格式。这样我们之前写的 `fapiao-1688` 和 `fapiao-1688-chase` 直接能用。

### 3. Memory / Knowledge Reader

#### Memory(本机个性化):

```
memory/
├── PC1/
│   ├── learned-shop-replies.md      ← 这台机的账号常见商家回复
│   ├── account-A1-quirks.md        ← A1 账号特有的怪事
│   └── ...
```

#### Knowledge(全网共享):

```
knowledge/
├── 1688/
│   ├── 风控规律.md
│   ├── 滑块攻略.md
│   ├── 商家应答典型回复.md
│   └── 不同类目商家行为差异.md
├── 飞书/
│   └── 任务格式规范.md
└── 通用/
    └── 经验沉淀方法论.md
```

**注入策略**:
- Skill 加载时把所有 skill body 注入 system prompt(尺寸总和不大,只有几个 skill)
- Knowledge 用**向量检索** top-K 相关 chunk(避免一股脑塞进 prompt 把 token 烧光)
- Memory 用**全量注入**(单机自己的,不大)

### 4. LLM Loop(核心)

```python
class LLMLoop:
    def __init__(self, router):
        self.router = router  # LLMRouter,见 05 章
    
    async def run(self, messages, tools, task_tags, max_iters=30):
        for i in range(max_iters):
            resp = await self.router.call(messages, tools, task_tags)
            
            # 添加到上下文
            messages.append({
                "role": "assistant",
                "content": resp.content,
                "tool_calls": resp.tool_calls,
            })
            
            if resp.finish_reason == "stop":
                return resp
            
            if resp.tool_calls:
                # 派发工具调用
                tool_results = await asyncio.gather(*[
                    self._dispatch_tool(tc) for tc in resp.tool_calls
                ])
                # 工具结果加入消息
                for tc, tr in zip(resp.tool_calls, tool_results):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tr,
                    })
        
        raise MaxIterationsExceeded(messages)
    
    async def _dispatch_tool(self, tool_call):
        return await self.tool_bridge.call(
            tool_call.function.name,
            tool_call.function.arguments,
        )
```

### 5. Tool Bridge

负责把 LLM 的 tool_call 路由到具体的工具实现。**Aggregator MCP 是其中一个 tool 来源**(管浏览器);其他来源:文件操作、HTTP 请求、自定义函数。

```python
class ToolBridge:
    def __init__(self, mcp_clients, builtin_tools):
        self.mcp_clients = mcp_clients  # 多个 MCP server
        self.builtin = builtin_tools     # python 函数
    
    async def call(self, tool_name, arguments):
        # tool_name 例:mcp__aggregator__chase_send_one / builtin__file_read
        if tool_name.startswith("mcp__"):
            _, server_name, fn = tool_name.split("__", 2)
            client = self.mcp_clients[server_name]
            return await client.call(fn, arguments)
        elif tool_name.startswith("builtin__"):
            fn_name = tool_name.removeprefix("builtin__")
            return await self.builtin[fn_name](**arguments)
        else:
            raise UnknownTool(tool_name)
```

### 6. Experience Extractor(关键差异化能力)

每天/每周后台跑一次,扫日志总结经验:

```python
async def extract_experience(time_range="last_24h"):
    # 1. 读最近的 task logs
    logs = await load_logs(time_range)
    
    # 2. 按 capability 分组
    by_capability = group_by(logs, "capability")
    
    # 3. 对每个 capability 跑总结 LLM
    for cap, cap_logs in by_capability.items():
        prompt = build_distill_prompt(cap, cap_logs)
        summary = await llm.call(prompt, model="best_for_summary")
        
        # 4. Merge into knowledge/
        target_file = f"knowledge/{cap}/{date}-extracted.md"
        await merge_knowledge(target_file, summary)
    
    # 5. git commit + push
    await git_commit_and_push(f"Extracted experience {date}")


def build_distill_prompt(capability, logs):
    return f"""
你是经验萃取助手。读以下 {len(logs)} 个 {capability} 任务的日志,抽出:
1. 新发现的规律(如 1688 滑块阈值变化、某类商家的特殊反应等)
2. 失败/错误案例 + 解决方法
3. 商家行为模式(常见拒答、常见回复)
4. 任何能让下次 agent 做更快/更稳的洞察

输出格式:
- 用 ## 分主题
- 每个主题简短的"现象" + "证据"(引用 log 片段)+ "建议下次怎么做"
- 不要写废话总结,直接列洞察

任务日志:
{logs_as_text}
"""
```

**为什么是批量总结而不是每任务都写**:

- 每任务都写会产生 1000+ 条"低密度噪音"(如"商家 X 说了 hi")
- 批量观察才能凝结出**规律**(如"50% 食品类商家在 12 小时内回复")
- 单次 LLM 调用 token 成本更可控

### 7. Reporter

任务结束 → 飞书反馈:

```python
async def report_success(task, result):
    await feishu.send_message(
        chat_id=task.callback.chat_id,
        text=f"✅ {task.description} 完成。结果:{summarize(result)}",
    )

async def report_error(task, error):
    await feishu.send_message(
        chat_id=task.callback.chat_id,
        text=f"❌ {task.description} 失败:{error}。已加入重试队列。",
    )
```

## 飞书集成具体怎么做

### 接收任务

飞书机器人 webhook 收到消息 → 你的接收服务 → 队列。

支持两种任务格式:

**格式 A(自由文本,LLM 解析):**
```
你: @机器人 让 PC2 的 A3 账号催一下湖北达利的发票
```

接收服务用 LLM 把这句话解析成结构化任务:
```json
{
  "id": "tsk-abc123",
  "tags": ["machine:PC2", "account:A3", "capability:1688-chase"],
  "skill_hint": "fapiao-1688-chase",
  "payload": {"merchant": "湖北达利食品有限公司"},
  "callback": {"chat_id": "..."}
}
```

**格式 B(结构化命令):**
```
/chase --account A3 --merchant 湖北达利 --machine PC2
```

直接转结构化任务,无需 LLM。

### 反馈任务

任务完成或失败时,Reporter 把结果发回原飞书会话:

```
✅ PC2/A3 已给"湖北达利食品有限公司"发催单(3 条消息,27 单)。
```

### 飞书机器人配置

- 创建一个飞书企业内部应用 → 给它消息事件权限
- 配置 webhook URL 指向你的接收服务
- 接收服务用 `lark-oapi` Python SDK 解析 + 鉴权

## Knowledge / Memory 的格式

复用 SKILL.md 的 markdown + frontmatter 风格:

```markdown
---
topic: 1688/风控
source: PC1 / 2026-05-22
type: observation
---

## 滑块触发阈值

**现象**: 单账号短时间连续打开 ≥ 4 个新 IM 会话会触发滑块。

**证据**:
- batch1: 5 家中第 4 家触
- batch2: 第 7 家触(因有间隔时间长)
- batch2: 第 16 家触

**建议**:
- 每开一个新 IM tab 间隔至少 30 秒
- 6 账号轮转(每账号开一家后切下一账号)能拉长间隔
```

## 设计的几个关键取舍

| 设计抉择 | 选 A(简单) | 选 B(复杂) | 我们选 |
|---|---|---|---|
| 任务粒度 | 一个任务=一次 LLM loop | 任务可分子任务,agent 自规划 | A 起步 |
| 经验沉淀 | 每天/周总结 | 每任务总结 + 每周大总结 | A |
| 知识检索 | Grep + 文件名匹配 | 向量召回 | A 起步(< 100 文件够),后加 B |
| Agent 间协作 | 各干各的 | 互相 send_message | A |
| Skill 生成 | 人写 push | Agent 写 + 人审核 | A 起步 |
| LLM 上下文 | 全量 skill body | 检索召回相关 skill | A 起步 |

**核心哲学**: **能用文件 + 简单 grep 解决的不上向量库;能用同步 IO 的不上异步队列;一个任务一个 LLM call 解决的不分子任务**。等业务规模真的撑大了再上重型。

## 多机协调

**不引入中央服务**,各机通过共享资源解耦:

| 资源 | 共享方式 |
|---|---|
| skills/ | Git 仓库,各机 git pull |
| knowledge/ | 同上,但有 commit 冲突协调(后到者 rebase) |
| memory/(本机部分) | 仅本机,不共享 |
| 任务队列 | Redis 集群(可放云上 / 中心机) |
| 进度/状态 | 各机本地 SQLite,Reporter 报到飞书 |

任务路由:
- 任务进队列时带 `machine:` 标签或不带
- 各机消费组只拉**带本机 tag 或无 tag**的任务
- 无 tag 任务:谁先抢到谁做

## 一图速览

```
┌─────────────────────────────────────────────────────────────────┐
│                  自建 Agent 平台 - 长期架构                       │
│                                                                 │
│  ┌──────────────┐         ┌─────────────────┐                  │
│  │ 飞书机器人    │ ───────►│  任务接收服务    │                  │
│  │ /钉钉/HTTP   │         │  + Redis 队列    │                  │
│  └──────────────┘         └─────────┬───────┘                  │
│                                     │                          │
│           ┌─────────────────────────┼────────────────────────┐ │
│           │                                                  │ │
│           ▼                         ▼                        ▼ │
│  ┌─────────────┐         ┌─────────────┐         ┌─────────────┐│
│  │ Agent PC#1  │         │ Agent PC#2  │         │ Agent PC#3  ││
│  │             │         │             │         │             ││
│  │ LLM Loop    │         │ LLM Loop    │         │ LLM Loop    ││
│  │ + Skill     │         │ + Skill     │         │ + Skill     ││
│  │   Loader    │         │   Loader    │         │   Loader    ││
│  │ + Aggregator│         │ + Aggregator│         │ + Aggregator││
│  │ + 6 浏览器  │         │ + 6 浏览器  │         │ + 6 浏览器  ││
│  └──────┬──────┘         └──────┬──────┘         └──────┬──────┘│
│         │                       │                       │       │
│         └──── 读/写共享 git ──┴──────────────────────┘       │
│                                     │                          │
│                              ┌──────▼──────┐                   │
│                              │  Git Repo    │                   │
│                              │ skills/      │                   │
│                              │ knowledge/   │                   │
│                              │ memory/      │                   │
│                              └─────────────┘                   │
│                                     ▲                          │
│                                     │                          │
│                              每天 LLM 自动                       │
│                              萃取经验 commit                     │
└─────────────────────────────────────────────────────────────────┘
```

## 下一步

- LLM 选型 + 多 Provider 路由 → `05-llm-router-multi-provider.md`
- 多模态(看截图)能力 → `06-multimodal-vision.md`
- 实施步骤 + 工时 → `07-implementation-roadmap.md`
