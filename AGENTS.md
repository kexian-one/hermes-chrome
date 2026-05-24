# AGENTS.md — teammate 协作契约

> 这份文档是 `all-in-ai` team 所有 teammate 共用的契约。开始任何工作前必读。

## 项目背景(30 秒读完)

- 业务:1688 自动开票 + 催发票 MVP
- 完整架构 / 决策见 [DOC/](DOC/) 目录,**特别是** [DOC/09-open-questions-and-todos.md](DOC/09-open-questions-and-todos.md) 的 Q1/Q6/Q8/Q15
- 当前阶段:Phase 1(自建 agent runtime 最小可跑版)
- 长期目标:摆脱 Claude Code,自建 agent 平台,OpenAI 兼容模型(默认 DeepSeek V4 Pro)

## 顶层架构(per machine)

```text
Master Agent (LLM-driven, NLU 入口 stub)
       ↓ subprocess
   ┌───┬───┬───┬───┬───┬───┐
   w1  w2  w3  w4  w5  w6        ← worker = 自建 agent runtime
   ↓   ↓   ↓   ↓   ↓   ↓
   b1  b2  b3  b4  b5  b6        ← 6 浏览器(Chrome/Edge/Brave/Vivaldi/Opera/...)
                                    每个连一个独立的 open-claude-in-chrome MCP server
                                    端口 18765 ~ 18770
```

**单机自治,跨机不协调**(永久决策,见 Q15)。

## 文件布局(所有 teammate 必须遵守)

```text
d:\ai\all-in-ai\
├── AGENTS.md              (本文件,契约)
├── DOC/                   (规划文档,不要动)
├── skills/                (复制自 ~/.claude/skills/ 的两个 SKILL.md,只读)
│   ├── fapiao-1688/SKILL.md
│   └── fapiao-1688-chase/SKILL.md
├── agent/                 (Python 包,worker + master 共用代码)
│   ├── __init__.py
│   ├── config.py
│   ├── skill_loader.py
│   ├── llm_client.py
│   ├── mcp_client.py
│   ├── worker.py
│   └── master.py
├── deploy/                (部署脚本,主要 PowerShell)
│   ├── README.md
│   ├── clone-oicc.ps1
│   ├── register-native-host.ps1
│   └── config.template.json
├── tests/                 (pytest 测试)
│   ├── test_skill_loader.py
│   ├── test_worker_smoke.py
│   └── test_master_smoke.py
├── pyproject.toml
└── README.md              (代码层 README,区别于 DOC/README.md)
```

**写新文件前先看清楚自己负责的范围**,不要越界改别 teammate 的文件。

## 技术栈(硬约束)

- **Python 3.11+**(用 `match`、类型注解、`asyncio.TaskGroup` 等现代特性)
- **LLM 调用**:`openai` SDK,base_url + model + key 从 `config.yaml` 加载(双模型:multimodal + reasoning,见下方"组件契约")
- **MCP 客户端**:`mcp` Python SDK(`pip install mcp`)— 连 `open-claude-in-chrome` 的 mcp-server.js(TCP)
- **测试**:`pytest` + `pytest-asyncio`
- **YAML 解析**:`pyyaml`
- **配置**:环境变量优先,`agent/config.py` 提供 typed dataclass 包装
- **不要引入** SQLAlchemy / FastAPI / Celery / Redis 等"重量级"依赖。MVP 不需要

## 组件契约(API 边界,改这些要先告知 team lead)

### `agent/config.py`

LLM 设置走 **双模型** + **配置文件**(`config.yaml`,gitignored)。**不读环境变量**(除了 `ALL_IN_AI_CONFIG` 用来 override 配置文件路径)。

```python
@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str

@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str               # "b1" ~ "b6"
    mcp_port: int                # 18765 + (b_index - 1)
    llm_multimodal: LLMSettings  # 视觉/截图/图片输入用
    llm_reasoning: LLMSettings   # 纯文本 tool-calling(worker 默认走这个)
    skills_dir: Path             # 默认 ./skills
    log_dir: Path                # 默认 ./logs

@dataclass(frozen=True)
class MasterConfig:
    workers: list[WorkerConfig]   # 6 个
    cron_schedule: str            # 默认 "0 9,15 * * *" — 早 9 点 + 下午 3 点各跑一次
    log_dir: Path
```

**双模型路由约定**:

- Worker 跑 `fapiao-1688` / `fapiao-1688-chase` 等纯文本 + tool calling 流程 → **用 `llm_reasoning`**
- 任何需要看图的场景(滑块截图分析、飞书 bot 收到的图片、debug 截图等) → **用 `llm_multimodal`**
- 决定用哪个 model 是**调用点的责任**,不是 config 的责任 — 同一份 config 同时持有两个 client 凭据

**config.yaml 形状**(见 [config.example.yaml](config.example.yaml)):

```yaml
llm:
  multimodal:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen3-vl-max
    api_key: sk-xxx
  reasoning:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen3.6-plus
    api_key: sk-xxx
```

### `agent/skill_loader.py`

```python
@dataclass(frozen=True)
class Skill:
    name: str                 # frontmatter.name
    description: str          # frontmatter.description
    body: str                 # markdown 正文(不含 frontmatter)
    path: Path                # SKILL.md 绝对路径

class SkillRegistry:
    def __init__(self, skills_dir: Path): ...
    def list_skills(self) -> list[Skill]:        # 只含 name+description(progressive disclosure 浅层)
    def load_full(self, name: str) -> Skill:     # 触发时读 body 全文
```

### `agent/llm_client.py`

```python
class LLMClient:
    """OpenAI 兼容客户端的薄包装,默认指向 DeepSeek。"""
    def __init__(self, base_url: str, api_key: str, model: str): ...
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        """返回 .text / .tool_calls / .finish_reason"""
```

### `agent/mcp_client.py`

```python
class OpenClaudeInChromeClient:
    """连 open-claude-in-chrome 的 mcp-server.js(TCP localhost:port)。"""
    def __init__(self, port: int): ...
    async def __aenter__(self): ...
    async def __aexit__(self, *exc): ...
    async def list_tools(self) -> list[Tool]: ...
    async def call_tool(self, name: str, arguments: dict) -> ToolResult: ...
```

### `agent/worker.py`(主入口之一)

```bash
python -m agent.worker --worker-id b1 --skill fapiao-1688 [--port 18765]
```

退出码:0 = 成功;1 = skill 执行失败;2 = MCP 连不上;3 = LLM 错误;4 = 配置错误。

### `agent/master.py`(主入口之二)

```bash
python -m agent.master              # 起 cron poll + bot 长连接(同时)
python -m agent.master --once       # 跑一次"是否有定时任务在 1 分钟窗口内到期",到了就 fire
python -m agent.master --dry-run    # 跟 --once 一起用看会派发什么,但不真的 spawn
```

**Schedule-driven 模型**:master 每 60 秒读 `state/schedule.yaml`,找 cron 表达式在当前轮询窗口里命中的条目并 spawn 对应 worker。没有"固定 9 点 + 15 点跑所有 worker"硬编码;每个派发都是 `state/schedule.yaml` 里一条显式 entry。

**Worker → 账号映射不在 framework**。`b1..b6` 是匿名插槽,谁登哪个账号是用户脑子里 / 物理便签上的事。bot 命令和定时任务都直接说 `b2`,用户实现知道 b2 是什么。

**飞书 bot intent 入口**:`agent/nlu.py` 用 reasoning LLM 把消息分类成 12 个 intent,`agent/intents.py` 派发到具体 handler。Cron 触发不走 NLU(直接 schedule entry → spawn);只有 bot 文字消息走 NLU。

### Deploy 脚本契约(`deploy/`)

- `clone-oicc.ps1 -Count 6`:克隆 [noemica-io/open-claude-in-chrome](https://github.com/noemica-io/open-claude-in-chrome) 6 份到 `deploy/oicc-b1` ~ `deploy/oicc-b6`,各自的 `config.json` 写不同端口(18765-18770)
- `register-native-host.ps1 -BrowserList Chrome,Edge,Brave,Vivaldi,Opera,...`:为每个浏览器写 `HKCU\Software\<vendor>\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b<n>`,指向对应的 native host manifest 路径

## 策略 vs 机制 — 谁决定卡住时怎么办

worker 跑到**阻塞事件**(滑块 / 验证码 / 账号异常 / 1688 风控提示等)时,**怎么应对不是 framework 的一刀切规则**。Framework 只提供**机制**,**策略**由每个 SKILL.md 自己写明:

- **Framework 机制**(worker.py / master.py / bot 提供):
  - 检测阻塞(LLM 看 tool 返回 + 状态)
  - 暂停 worker 任务
  - 通知人(打日志 / 飞书 bot 推消息)
  - 让人通过 bot 命令"跳过 / 重试 / 继续"

- **Skill 策略**(SKILL.md body 自己写):
  - 默认 = 暂停 + 通知人,等待人工(对应 [DOC/00](DOC/00-context-and-goals.md) 决策"滑块 = 人工")
  - 可选 = 个别 skill 声明"简单几何滑块尝试用 multimodal 模型"
  - 可选 = "跳过这家继续下一家"

**严禁**:在 worker.py / master.py 里写"遇滑块自动调 vision 模型"或任何 framework-level 的阻塞处理逻辑。**不是所有滑块都指望视觉模型滑动**;不同账号/不同 skill 应对方式不同,只能写在 skill 里。

**同样原则适用于:浏览器 tab 清理**

- Framework 提供机制:MCP 工具里有 `tabs_context_mcp` / `tabs_create_mcp` 等
- 策略由 skill 决定:`fapiao-1688` 跑完可以关 tab,`fapiao-1688-chase` 复用旺旺 tab **不关**
- **不要在 worker.py / master.py 写"任务完成自动关所有 tab"** — 不同 skill 复用 tab 的方式不同,框架不应该一刀切

## 编码规范

- 类型注解全用(`from __future__ import annotations`)
- 只在系统边界(用户输入、外部 API)校验;内部代码相信类型
- **不写多余注释** — 全局 CLAUDE.md 已经强调"默认无注释",名字够清楚就够了
- **不写文档字符串里的"参数说明"** — 类型签名已经说了
- 错误处理只在能真正恢复的地方加;否则让它崩,日志记下
- 不写 try-except 兜底底层异常

## 跑通验证

每个 teammate 完工前**必须**让自己负责的代码至少跑一次,**不能光写不跑**:

- worker-builder:写 `tests/test_worker_smoke.py`,**Mock 掉 LLM 和 MCP** 验证 wiring。如果有真 DeepSeek key 在 env,再加 `tests/test_worker_e2e.py`(@pytest.mark.skipif 没 key 就跳过)
- master-builder:写 `tests/test_master_smoke.py`,subprocess Mock 6 个假 worker,验证调度和监控
- deploy-builder:在 powershell 里**至少 dry-run** 一次脚本(可以加 `-WhatIf` 或 `-DryRun` 参数),验证不会写错路径

## 完工后

完工后:

1. 用 `TaskUpdate` 把你的任务标 completed
2. `SendMessage` 通知 team-lead,**简短**说:写了什么文件、怎么跑、跑的结果是什么
3. 不要等其他 teammate,你可以直接 idle

## 协作通讯

- 有阻塞 → `SendMessage` to `team-lead`
- 发现别 teammate 的接口需要改 → `SendMessage` to `team-lead` **协调**,不要自己改别人的代码
- 不需要主动联系其他 teammate(都通过 team-lead 转发)

## 不要做的事

- 不要碰 `DOC/` 目录的任何文件
- 不要碰 `~/.claude/skills/` 下的原始 SKILL.md(只读,本项目复制了一份在 `skills/`)
- 不要装重型依赖(SQLAlchemy / FastAPI / Celery / Redis)
- 不要为了"扩展性"写 abstract base class / dependency injection framework,简单的就好
- 不要给 master / worker 加任务队列(Q8 决策:不做)
- 不要做跨机协调(Q15 决策:永不做)
- 不要"完善"现有 SKILL.md 内容
