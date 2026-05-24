> 返回索引: [DOC/09](09-open-questions-and-todos.md)

# 09b — P1 优先决策(影响 Phase 1-2 顺畅度)

### Q5: 非 Claude LLM 在 chase 场景的实测表现

**决策(2026-05-23,用户确认)**:**不预先 gate**。用户自己根据效果切换,只保证"模型 OpenAI 接口兼容"这一条硬约束。

> "Q5 你不用关心。如果这一个模型不行,我们会自己换其他模型,只要它兼容 OpenAI 接口格式。"

工程上的含义:

- 自建 agent runtime 用 OpenAI 兼容客户端,模型用 env / config 切换
- 不锁特定 provider,**首选 = 用户当前在用的**(可能是 DeepSeek V4 Pro,见 Q6)
- 真跑不动了用户换模型,工程不阻塞

### Q6: 自建 Agent 的 Skill loader 是否完全兼容现有 SKILL.md?

**为什么重要**:我们已写的 `fapiao-1688` 和 `fapiao-1688-chase` 是 Claude Code 格式。自建 Agent 要能直接读。

**结论(2026-05-23 调研)**:**SKILL.md 已经是开放标准,自建 agent 直接复用 = 可行**。下面给细节。

**调研来源**(2026-05-23 抓取):

- [SKILL.md 开放标准规范(agensi.io)](https://www.agensi.io/learn/skill-md-specification-open-standard)
- [OpenAI Codex Skills 文档](https://developers.openai.com/codex/skills)
- [DeepSeek V4 API 文档](https://api-docs.deepseek.com/guides/tool_calls)
- [DeepSeek V4 Tool Calling 指南(Macaron)](https://macaron.im/blog/deepseek-v4-tool-calling)
- 本地两个 skill 文件:`C:\Users\ke785\.claude\skills\fapiao-1688\SKILL.md`、`fapiao-1688-chase\SKILL.md`

**关键事实**:

1. **SKILL.md 是 Anthropic 发起、现已被广泛采用的开放标准**
   - 已采用方:Claude Code、OpenAI Codex、OpenClaw、OpenAI Skills tool、Cursor、Gemini CLI
   - 规范明确写:"Skills are portable across all compatible agents without modification. The SKILL.md file is identical regardless of which agent reads it."
2. **格式必备字段**:`name`(kebab-case 唯一标识)+ `description`(用于语义触发)。其他都是可选(`version` / `author` / `tags` / `agents` / `allowed-tools`)
3. **加载机制全行业一致**(progressive disclosure):agent 启动时只扫各 SKILL.md 的 frontmatter(name + description),根据用户 prompt 与 description 的语义匹配决定是否加载 body 全文
4. **DeepSeek V4 Pro(2026-04-24 发布)对 Claude skills 格式有原生适配**
   - OpenAI 兼容 API ✓
   - 支持 tool calling,parallel 最多 128 个函数 ✓
   - MCPAtlas Public benchmark 73.6 分,**与 Claude Opus 4.6 持平**
   - 官方说有"pre-tuned adapters for Claude Code, OpenCode, OpenClaw, and CodeBuddy" — 意味着 Anthropic 的 skill 调用范式它专门调过

**我们的两个 skill 具体怎么用**:

读了 `fapiao-1688/SKILL.md` 和 `fapiao-1688-chase/SKILL.md` 之后判定:

- **frontmatter(`name` + `description`)**:100% 标准格式,直接复用 ✓
- **body 内容(中文步骤说明、URL endpoint、shadow DOM walking 代码片段、数据结构、错误处理)**:全是自然语言 + JS 代码片段,**任何能读 markdown 的 LLM 都能理解** ✓
- **body 里引用的工具名**(`mcp__open-claude-in-chrome__tabs_context_mcp` / `javascript_tool` 等):这是 Claude Code 的 MCP 工具命名约定(`mcp__<server>__<tool>`),**唯一可能需要小适配的地方**

**唯一要适配的点(微小)**:

自建 agent 调用 MCP server 时怎么给工具命名。两种处理方案:

| 方案 | 自建 agent 工具命名 | SKILL.md 改不改 |
| --- | --- | --- |
| **A. 沿用 Claude Code 命名** | `mcp__<server>__<tool>` | **完全不改**,frontmatter+body 直接复用 |
| B. 自定义命名 | 例如 `oicc.tabs_context_mcp` | 在 loader 里做正则替换 `mcp__open-claude-in-chrome__` → 自定义前缀,SKILL.md 本身仍然不改 |

**推荐:方案 A**(成本为零)。我们的 6 worker 都连同款 `open-claude-in-chrome` MCP server(见 Q1),没必要换命名约定。

**不可移植的字段(明确避开)**:

- `hooks:`(事件触发器)— Claude Code 独有,我们的两个 skill 没用到 ✓
- `allowed-tools:`(权限白名单)— 各 runtime 实现不同,我们的两个 skill 没用到 ✓

**自建 agent runtime 的 Skill loader 最小实现**(Phase 1 工时估计):

1. 扫 `skills/*/SKILL.md`,解析 YAML frontmatter → 拿 name + description
2. 把 `{name, description}` 列表作为 system prompt 的"available skills"块
3. LLM 决定调用某 skill 时,读对应 `SKILL.md` 全文塞进 context
4. 工具调用按 OpenAI function calling 标准走

**工时估计**:**1–2 天**(单文件 Python loader + 集成测试两个 skill)。

**结论汇总**:

- ✅ **直接复用** SKILL.md(frontmatter + body 都不动)
- ✅ **不需要任何改造** — 前提是自建 agent 沿用 `mcp__<server>__<tool>` 工具命名约定
- ✅ **DeepSeek V4 Pro 跑这套完全 OK** — 官方对 Claude skill 格式做过适配训练
- 📌 待 Phase 1 实施时**冒烟测试**:用 DeepSeek V4 Pro + 极简 Python loader + 现成 `fapiao-1688/SKILL.md`,跑一次抓数据,确认无需 prompt rewriting

### Q7: 飞书机器人接入 — 长连接 + 自建应用,不用公网 webhook

**状态(2026-05-23 落地完成)**:✅ **代码已交付**(bot-builder teammate / Sonnet 4.6 / 55 passed + 2 skipped)。剩 user-side 步骤:在飞书开放平台建 4 个自建应用 + 填 config.yaml。建应用步骤见 [README.md "飞书 bot setup"](../README.md#飞书-bot-setup-phase-2)。

**交付物**:

- `agent/bot.py` — `lark-oapi` 长连接客户端
- `agent/nlu.py` — LLM 意图路由(用 reasoning model)
- `agent/intents.py` — 8 个意图实现(query_status / restart_worker / query_logs / query_stats / pause_all / resume_all / help / unknown)
- `agent/worker_state.py` — worker 进程状态追踪(thread-safe)
- `agent/master.py` — 加 `_bot_loop` 跟 `_cron_loop` 并发,`asyncio.TaskGroup`
- `agent/config.py` — `BotConfig` dataclass + 从 config.yaml 加载
- `config.example.yaml` — `bot` section 模板
- 3 个测试文件(test_nlu / test_intents / test_bot_smoke) 共 30 个测试

---

**架构设计**(下面是历史记录,代码已按这个方案实现):

**架构**:**每台机一个独立飞书自建应用机器人**(N 台机 = N 个 bot)。**用长连接事件订阅,不用 webhook**。

> 用户原话:"我总共有 4 台机器,我在飞书中创建 4 个机器人,然后我 @ 每一个机器对应的机器人,问他:现在的任务完成情况怎么样?"

(以下原设计内容保留作为参考)

**~~决策(2026-05-23 升级,用户确认)~~**:**~~Phase 2 落地~~**。同时承担两个角色:
1. 替代 dashboard(替代 Q11 网页控制台需求)
2. 实现 Q15 演进路径里的 NLU 入口

**架构**:**每台机一个独立飞书自建应用机器人**(N 台机 = N 个 bot)。**用长连接事件订阅,不用 webhook**。

> 用户原话:"我总共有 4 台机器,我在飞书中创建 4 个机器人,然后我 @ 每一个机器对应的机器人,问他:现在的任务完成情况怎么样?"

```text
飞书 (云)
   ↑↓ 长连接 (机器 outbound,无需公网入口)
N 个独立 bot:
  bot-pc1 ← @ 进群对话 → master-pc1 (机1)
  bot-pc2 ← @ 进群对话 → master-pc2 (机2)
  ...
```

**关键选择(都已定)**:

| 选择 | 决定 | 理由 |
| --- | --- | --- |
| 接入方式 | **长连接**(`lark-oapi` SDK 主动连飞书拉消息) | 完全去掉公网入口,家用路由器/Tailscale 后面也能用,**Q7 原版"webhook 鉴权"问题不再存在** |
| Bot 数量 | **每台机一个 bot**(N 机 = N bot) | @ 时天然带"机器上下文",NLU 不用再分辨"问哪台机";跟"永不跨机"决策对齐 |
| NLU 实现 | **复用 master 的 LLM 调用通路** | 不引入第二份 LLM 调用栈;`agent/nlu_stub.py` 升级为真实现 |
| 鉴权 | **飞书自建应用 user_id 白名单** | 在飞书后台限定群成员 + 在 master 配置层维护可信 user_id list |
| 公网入口 | **不需要** | 长连接消除这个攻击面 |

**Master agent 要新增的能力**:

- 接 `lark-oapi` 长连接,收到 @ 消息 → 交给 NLU 路由
- NLU 路由(初版意图清单):
  - `查状态` → 列 6 worker 当前 skill / 进度 / 异常
  - `重启 worker N` → master 重启对应 subprocess
  - `跳过当前任务 worker N` → master 发跳过信号
  - `今天的统计` → 读 logs/ + 当日 git commit 摘要
  - `查日志 worker N` → 返回 logs/worker-bN-*.log 最近 50 行
  - `暂停所有` / `恢复所有` → master 改调度状态
- 主动推送(可选,Phase 2.5):worker 出滑块超过 30 秒 → bot 推消息到群

**已知限制(用户接受)**:

- @ 单个 bot **只能查那台机**;"4 台合计今天发了多少催单"得你自己脑补 4 个 bot 回复
- 这是"永不跨机"决策的必然代价,不补救

**工时估算**:

- 4 个飞书自建应用配置(开放平台后台):每个 ≈ 15 min,合计 **1 小时**
- master agent 集成 `lark-oapi` + NLU 路由(代码一次写,N 台机共用):**1-2 天**
- 初版意图清单的查询函数实现:**0.5 天**
- 合计 **约 2-3 天**(Phase 2 范围,代码一次性,部署 N 次)

**对其他文档的影响**:

- [Q11](09c-p2-deferred.md#q11-web-dashboard-范围):dashboard 核心需求被飞书 bot 替代,Q11 进一步降级
- [Q15](#q15-单机-agent-拓扑--master--worker-架构):Phase 2 演进路径里"NLU 入口"具体化为飞书 bot 长连接
- "已决定的事"表:**任务接入**和**远程入口**相关行需要更新

---

### Q15: 单机 Agent 拓扑 — Master / Worker 架构

**为什么重要**:在"不做任务队列、6 浏览器 6 账号并发、最终不用 Claude Code"三条约束下,单机内部 agent 怎么组织决定了后续接聊天工具 / NLU 入口的扩展性。

**决策(2026-05-23)**:**每台机 1 个 Master Agent + 6 个 Worker Agent**,master 是 LLM-driven(为后续 NLU 入口预留),worker 是自建 agent runtime 实例。

**架构图**(per machine):

```text
                ┌────────────────────────────┐
聊天工具消息    │  Master Agent (LLM-driven) │
飞书/钉钉  ───→ │  - NLU: 解析自然语言任务    │
                │  - 路由: 派给哪个 worker    │
cron/schedule ─→│  - 监控: worker 状态/重启  │
                │  - 调度: 优先级/并发控制    │
                └────────────────────────────┘
                       ↓ in-process / subprocess
                ┌───┬───┬───┬───┬───┬───┐
                w1  w2  w3  w4  w5  w6      ← 自建 agent runtime
                ↓   ↓   ↓   ↓   ↓   ↓         跑 fapiao-1688
                b1  b2  b3  b4  b5  b6         + fapiao-1688-chase
                (acct1)(acct2)...(acct6)
```

**关键设计点**:

- **Master 长期是 LLM-driven**(不是 thin Python 编排器),因为 NLU 入口会出现("帮我让账号 5 重跑催单"这类自然语言任务)
- **Worker 不是 Claude Code**,是自建 agent runtime — 符合 [00](00-context-and-goals.md) / [04](04-agent-platform.md) 已定的"摆脱 Claude Code"长期目标
- **Master ↔ Worker 不走队列**:in-process 函数调用或本地 subprocess,Q8 决策"不做任务队列"在单机内仍然成立
- **6 worker 是固定数**(对应 6 浏览器 6 账号),不是动态池;master 不需要"决定要不要新起一个 worker",只需要"派给现成的哪一个"

**Master 的职责清单**:

- 接 cron / schedule:按时触发各 worker 跑 `fapiao-1688` / `fapiao-1688-chase`
- 接 NLU 入口(Phase 2 起):解析飞书/钉钉/CLI 输入的自然语言任务
- 路由决策:任务派给 worker N(账号语义 ↔ 账号编号映射)
- 监控:6 worker 进程存活 / 浏览器 session 可用 / 滑块告警
- 重启:worker 崩溃自动拉起;反复崩溃 escalate 给现场人员
- 状态聚合:把 6 个 worker 的进度 / 日志 / 异常归一,供人查看

**Worker 的职责清单**:

- 占用 1 个浏览器 session(通过对应的 `open-claude-in-chrome` MCP server,见 Q1)
- 接 master 派的任务,跑指定 skill
- 出问题(滑块 / 异常状态 / skill 执行失败)向 master 报告
- 不直接接外部输入,只听 master

**演进路径**(避免 MVP 推倒重来):

| 阶段 | Master 形态 | 触发源 | 备注 |
| --- | --- | --- | --- |
| Phase 1(最早跑通) | LLM agent 框架已搭,只接 cron + 本地 CLI | 不接聊天工具 | NLU 入口"留口子但不接"(`agent/nlu_stub.py`) |
| Phase 2(NLU 入口) | **接飞书 bot 长连接**(每台机一个独立 bot),`nlu_stub` 升级为真实现 | + 自然语言任务(@ bot) | 见 [Q7](#q7-飞书机器人接入--长连接--自建应用不用公网-webhook);**不动 worker**;**无需公网入口** |
| Phase 3+ | 加更复杂调度逻辑(优先级 / 限流 / 失败策略) + 主动推送告警 | 同上 + bot 主动推消息 | 仍然单机自治,**不做跨机** |

**永不会做的事**(防止后续讨论重复绕):

- ❌ 跨机 master 协调
- ❌ 多机共享任务队列
- ❌ 中央 dispatcher
- ❌ 网页 dashboard(飞书 bot 已覆盖核心需求,见 [Q11](09c-p2-deferred.md#q11-web-dashboard-范围))

**对其他文档的影响**:

- 此架构是 [04-agent-platform](04-agent-platform.md) 实际要落地的形态,04 的多机 / Redis / 中央 dashboard 部分**作为历史背景保留**,但不实施
- "已决定的事"表新增"Agent 拓扑"和"跨机协调(永不做)"两行
