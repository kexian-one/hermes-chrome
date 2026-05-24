# all-in-ai

1688 invoice automation worker agent runtime.

## Requirements

- Python 3.11+
- `open-claude-in-chrome` MCP server running on TCP localhost (default port 18765)
- An OpenAI-compatible LLM API key (Qwen, DeepSeek, GLM, etc.)

## Install

```bash
pip install -e ".[dev]"
```

## Configure

Configuration lives in `config.yaml` at the project root (gitignored). Copy the template:

```bash
copy config.example.yaml config.yaml   # PowerShell
# or: cp config.example.yaml config.yaml
```

Then fill in your keys. The system uses **two distinct LLM models**:

| Role | Used for | Default |
| --- | --- | --- |
| `llm.multimodal` | Anything that needs vision (screenshots, slider images, 飞书 image attachments) | `qwen3-vl-max` |
| `llm.reasoning` | Pure text / tool-calling agentic loop (worker default for fapiao skills) | `qwen3.6-plus` |

Same key, same `base_url`, different `model` strings is fine — both default to Alibaba DashScope's OpenAI-compatible endpoint.

You can point `ALL_IN_AI_CONFIG=/path/to/other.yaml` if you need a non-default config location.

## Run a worker

```bash
python -m agent.worker --worker-id b1 --skill fapiao-1688
```

Optional: override the MCP port:

```bash
python -m agent.worker --worker-id b1 --skill fapiao-1688 --port 18765
```

## Run the master (schedule loop + optional 飞书 bot)

```bash
python -m agent.master           # cron polls every 60s + bot loop (if bot.enabled)
python -m agent.master --once    # fire any schedule entries due in the next minute, exit
python -m agent.master --dry-run # print what would fire without spawning workers
```

The master is **schedule-driven**: it reads `state/schedule.yaml` and fires entries whose cron matches the polling window. There is no hardcoded "run all 6 workers at 9am" — every dispatch is an explicit entry in the schedule file.

You add entries either:

- **Via the 飞书 bot**: `@bot 每天 16:00 让 b2 跑 fapiao-1688` (NLU parses + writes to `state/schedule.yaml`)
- **By hand**: edit `state/schedule.yaml` directly. Schema in [state/schedule.example.yaml](state/schedule.example.yaml).

## Worker → account mapping (intentionally external)

The framework does **NOT** track which platform/account is logged into which `b{1..6}` slot. That mapping lives in **your head** (or a sticky note on the machine). When you @ the bot, you say "b2 跑 fapiao-1688" — you implicitly know what b2 is configured to do. This way:

- Adding 京东 / 淘宝 / 拼多多 platforms = write a new `SKILL.md`, no config change
- Stopping an account = stop sending bot commands to that worker
- The framework stays small and platform-agnostic

## 飞书 bot setup (Phase 2)

Each machine runs its own dedicated 飞书 bot. Per-machine setup:

1. Go to <https://open.feishu.cn/app> → "创建企业自建应用" → fill name/description
2. "凭证与基础信息" → copy **App ID** and **App Secret** into `config.yaml` under `bot.app_id` / `bot.app_secret`
3. "添加应用能力" → 添加 "机器人"
4. "权限管理" → grant `im:message` (receive) and `im:message:send_as_bot` (send)
5. "事件订阅" → enable **"长连接模式"** (no public URL needed — `lark-oapi` connects outbound)
6. "版本管理与发布" → publish the app
7. Invite the bot to a 飞书 group; collect the `open_id` (ou_xxx) of authorized users and put into `bot.authorized_user_ids`
8. Repeat for each machine (4 machines = 4 separate 自建应用 with distinct App ID/Secret)

Bot intents (12 total):

| Category | Examples |
| --- | --- |
| 查询 | `查状态` `看 b3 日志` `今天的统计` `看定时任务` `/help` |
| 立即执行 | `b2 现在跑 fapiao-1688` `重启 b3` |
| 定时管理 | `每天 16:00 让 b2 跑 fapiao-1688` `删掉 #3` |
| 全局控制 | `暂停所有` `继续` |

## Tests

**Unit / smoke (always run, all mocked):**

```bash
python -m pytest tests/ -v
```

23 tests pass with no setup. 2 MCP connectivity tests skip without a running MCP server; 2 DeepSeek smoke tests skip without `DEEPSEEK_API_KEY`.

**Connectivity (before first real e2e — recommended):**

Before running `python -m agent.master --once` for real, validate the two external dependencies independently:

```bash
# 1. LLM API works + tool calling works (reads config.yaml)
python -m pytest tests/test_llm_smoke.py -v -s

# 2. MCP server reachable (start one oicc-b1 first via deploy/, then:)
python -m pytest tests/test_mcp_connectivity.py -v -s

# Optional: target a specific port other than 18765
$env:MCP_TEST_PORT="18766"; python -m pytest tests/test_mcp_connectivity.py -v -s
```

Running these *first* isolates "LLM works" and "MCP works" from "full worker pipeline works" — if either skips/fails you know exactly which dep to fix.

## Known caveat — tool name prefix

Skills under `skills/` reference tools as `mcp__open-claude-in-chrome__<name>` (Claude Code's naming convention). The raw MCP server may expose bare names like `<name>`. If real e2e shows the LLM trying to call `mcp__...` names and getting "tool not found", the fix is either:

- Strip the `mcp__server__` prefix in `agent/worker.py` before calling MCP, OR
- Map MCP-returned tool names → prefixed names so the LLM sees the SKILL.md names.

This is intentionally not pre-emptively fixed — first real e2e will tell us if it's a real problem (Qwen 3.6 Plus may be smart enough to use whatever names the tools list shows, ignoring the SKILL.md prefix).

## Architecture

```text
python -m agent.worker --worker-id b1 --skill fapiao-1688
  1. SkillRegistry.load_full("fapiao-1688") -> reads SKILL.md body
  2. OpenClaudeInChromeClient(port=18765) -> TCP connect to mcp-server.js
  3. LLMClient(reasoning model from config.yaml) -> OpenAI-compatible chat with tool_calls
  4. Agentic loop (max 30 iterations):
     - LLM returns tool_calls -> execute via MCP -> feed results back
     - LLM finish_reason="stop" -> exit 0
  (Vision-needed paths route to multimodal model; not used by fapiao skills today.)
```

Exit codes: `0` success, `1` skill execution failure, `2` MCP connect error, `3` LLM error, `4` config/skill not found.
