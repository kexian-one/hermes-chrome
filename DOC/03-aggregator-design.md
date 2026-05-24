# 03 — 单机 Aggregator 设计

## 问题陈述

一台 PC 跑 **6 个不同的 Chromium 内核浏览器**(Chrome / Edge / Brave / Vivaldi / Opera / 等),每个浏览器装一份 `open-claude-in-chrome` 扩展,每个登录一个 1688 账号。这 6 个扩展各自暴露 MCP 接口(不同端口)。

**LLM 端只能连一个 MCP server**(无论是 Claude Code 还是自建 Agent)。如何把 6 个底层扩展统一为 1 个上层接口?

**答案**: 写一个 **Aggregator MCP Server**,中间人。

> **重要前提**:为什么选 6 个不同浏览器而不是同浏览器 6 profile,见 `02-multi-account-deployment.md`。本章假设这个前提已定。

## Aggregator 架构图

```
┌────────────────────────────────────────────────────────────────┐
│  LLM 端                                                        │
│  (Claude Code 或自建 Agent runtime)                            │
└──────────────────────┬─────────────────────────────────────────┘
                       │ 单一 MCP 连接(stdio 或 WS)
                       │
                       ▼
┌────────────────────────────────────────────────────────────────┐
│  Aggregator MCP Server (Python 进程,本机)                      │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  MCP Server 侧(对 LLM 暴露)                              │ │
│  │  暴露统一工具(每个工具带 account 参数):                  │ │
│  │   - chase_send_one(account, merchant_entry)             │ │
│  │   - browser_navigate(account, tab_id, url)              │ │
│  │   - browser_exec_js(account, tab_id, code)              │ │
│  │   - browser_click(account, tab_id, x, y)                │ │
│  │   - browser_tabs(account)                               │ │
│  │   - account_status() / pause / resume                   │ │
│  │   - knowledge_search(query) / memory_read(scope)        │ │
│  └────────────┬─────────────────────────────────────────────┘ │
│               │                                                │
│               ▼                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  路由表(account → MCP client)                           │ │
│  │   A1 → MCPClient("ws://localhost:17861")                │ │
│  │   A2 → MCPClient("ws://localhost:17862")                │ │
│  │   A3 → MCPClient("ws://localhost:17863")                │ │
│  │   A4 → MCPClient("ws://localhost:17864")                │ │
│  │   A5 → MCPClient("ws://localhost:17865")                │ │
│  │   A6 → MCPClient("ws://localhost:17866")                │ │
│  └────────────┬─────────────────────────────────────────────┘ │
│               │                                                │
│               ▼                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  MCP Client × 6(连各 Chrome 扩展)                       │ │
│  │  + 滑块感知(检测 body 含"拖动滑块" → 暂停该 account)     │ │
│  │  + IM push 通知(微信/钉钉 webhook)                       │ │
│  │  + 失败重试 / Tab 自动 close                             │ │
│  └──────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
                       │
       ┌───────────────┼───────────────┐
       │               │               │
       ▼               ▼               ▼
   :17861          :17862    ...    :17866
   open-claude     open-claude     open-claude
   -in-chrome A1   -in-chrome A2   -in-chrome A6
```

## 关键决策点

### 决策 1: Aggregator 暴露什么粒度的工具?

**低粒度**(透传底层 MCP 工具,加 account 参数):
```
browser_navigate(account, tab_id, url)
browser_exec_js(account, tab_id, code)
browser_click(account, tab_id, x, y)
```

**高粒度**(把业务流程封进单一工具):
```
chase_send_one(account, merchant_entry) 
  → 内部:搜单 + 点旺旺 + 发 msg1 + 发 msg2 + 关 tab + 报告
```

**推荐: 两种都暴露**

理由:
- 低粒度给 LLM 灵活性(应付意外情况:商家页面变了 / 弹意外对话框 / 等)
- 高粒度给"常规批处理"用,LLM 上下文极简(一个 task = 一个 tool call,而不是 7-10 个底层调用)

### 决策 2: 滑块感知放哪一层?

放在 **Aggregator 层**(MCP Client 侧),不在 LLM 层。

理由:
- 滑块是基础设施层问题,LLM 不该关心
- Aggregator 收到底层 MCP response 后扫一下,有滑块就标记 account 状态 = PAUSED
- 上层调用 `chase_send_one(account=A3)` 时,Aggregator 先看 A3 状态,PAUSED 就返回 `{status:"paused", reason:"captcha"}`,不进入实际操作
- LLM 看到 status=paused,LLM 决定:跳过此账号、等下次重试、还是报告人

### 决策 3: Tab 管理放哪一层?

放在 **Aggregator 层**。

每家发完一条/两条消息后:
- Aggregator 自动调 `window.close()` 关 IM tab
- LLM 不需要知道 tab id 这种底层细节

### 决策 4: 工具签名怎么处理 account?

每个工具的第一个 / 命名参数都是 `account`。

```python
@mcp_tool
def chase_send_one(account: str, merchant_entry: dict) -> dict:
    """发一家催单,account 是账号 ID(如 A1, A2, ...)"""
    ...

@mcp_tool
def browser_exec_js(account: str, tab_id: int, code: str) -> dict:
    """在指定 account 的指定 tab 执行 JS"""
    ...
```

LLM 看到的工具列表:
```
mcp__aggregator__chase_send_one
mcp__aggregator__browser_navigate
mcp__aggregator__browser_exec_js
...
```

**一套工具前缀,通过 account 参数路由 — 比让 LLM 看 6 套 prefix 干净得多**。

### 决策 5: 滑块如何检测和恢复?

**检测**: 每次底层 MCP 调用返回后,Aggregator 扫返回内容:
- HTML / innerText 含"拖动滑块" / "安全验证" / "验证码拦截" 字串
- 或:特定 DOM 元素(.nc-container, .nc_wrapper 等)
- 或:页面 title 含"验证"

**响应**:
1. Aggregator 内部 account 状态 = PAUSED_CAPTCHA
2. 给 LLM 调用返回 `{status: "captcha", account: "A3"}`
3. 同时(异步)推送 IM 通知给值守人:"PC1 / 账号 A3 触发滑块,请处理"
4. 后台轮询(2s 一次)该 account 的 tab 检测滑块是否还在
5. 滑块清除后:重置 PAUSED 状态 → status = RUNNING

**超时**:
- 30 分钟无人拖 → 标记账号 FAILED_TIMEOUT
- 把该 account 剩下的任务存盘(下次开机重跑)
- 不阻塞其他账号

### 决策 6: Aggregator 和 Agent runtime 是同一进程吗?

**Phase 1 (Claude Code 阶段)**: 分开
- Claude Code(LLM 端) ↔ Aggregator(MCP server) ↔ open-claude-in-chrome
- Aggregator 是独立 Python 进程

**Phase 2 (自建 Agent 阶段)**: 可以合并
- Agent runtime 直接内嵌 Aggregator 逻辑
- 不再走 MCP server 协议,内部函数调用
- 更高效

**推荐: 起步分开,即使将来合并也能保持代码模块化**。

## 代码骨架

```python
# aggregator/main.py

from mcp.server import Server
from mcp.types import Tool, TextContent
import asyncio
from typing import Dict
from .browser_client import BrowserClient  # 连单个 open-claude-in-chrome
from .high_level import chase_send_one_impl
from .config import load_config
from .push import send_im_push

app = Server("aggregator")
config = load_config()

# 路由表
clients: Dict[str, BrowserClient] = {
    acc.name: BrowserClient(url=acc.mcp_url, account=acc.name)
    for acc in config.accounts
}

# 状态表
account_status: Dict[str, str] = {acc.name: "RUNNING" for acc in config.accounts}


@app.list_tools()
async def list_tools():
    return [
        Tool(name="chase_send_one", description="...", inputSchema=...),
        Tool(name="browser_exec_js", description="...", inputSchema=...),
        Tool(name="browser_click", description="...", inputSchema=...),
        Tool(name="account_status", description="...", inputSchema=...),
        Tool(name="account_resume", description="...", inputSchema=...),
        # ...
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "chase_send_one":
        return await tool_chase_send_one(arguments)
    elif name == "browser_exec_js":
        return await tool_browser_exec_js(arguments)
    # ...


async def tool_chase_send_one(args):
    account = args["account"]
    if account_status[account] != "RUNNING":
        return [TextContent(type="text", text=f"account {account} not running: {account_status[account]}")]
    
    client = clients[account]
    try:
        result = await chase_send_one_impl(client, args["merchant_entry"])
        return [TextContent(type="text", text=json.dumps(result))]
    except CaptchaDetected:
        account_status[account] = "PAUSED_CAPTCHA"
        asyncio.create_task(monitor_captcha_clear(account))
        await send_im_push(f"{config.machine_name} / {account} 触滑块")
        return [TextContent(type="text", text=json.dumps({"status": "captcha", "account": account}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"status": "error", "error": str(e)}))]


async def tool_browser_exec_js(args):
    account = args["account"]
    client = clients[account]
    result = await client.call("javascript_tool", {
        "action": "javascript_exec",
        "tabId": args["tab_id"],
        "text": args["code"],
    })
    # 检测滑块
    if has_captcha(result):
        account_status[account] = "PAUSED_CAPTCHA"
        # ...
    return [TextContent(type="text", text=json.dumps(result))]


async def monitor_captcha_clear(account):
    """后台轮询直到滑块清除"""
    client = clients[account]
    timeout = 30 * 60  # 30 min
    started = time.time()
    while time.time() - started < timeout:
        await asyncio.sleep(2)
        result = await client.call("javascript_tool", {
            "action": "javascript_exec",
            "tabId": 0,  # 假设当前 active tab
            "text": "document.body.innerText.includes('拖动滑块')",
        })
        if not result:  # 滑块已被人拖完
            account_status[account] = "RUNNING"
            return
    # 超时
    account_status[account] = "FAILED_TIMEOUT"


# 启动
if __name__ == "__main__":
    asyncio.run(app.run_stdio_async())
```

## chase_send_one 高层封装(伪代码)

```python
# aggregator/high_level.py

async def chase_send_one_impl(client: BrowserClient, merchant_entry: dict) -> dict:
    """
    单家催单的完整流程,LLM 一次调用搞定。
    
    内部:
    1. 切到订单列表 tab(或开一个)
    2. 设关键词 = merchant_entry.first_order
    3. 真鼠标点搜索按钮
    4. 找旺旺图标坐标
    5. 真鼠标点旺旺图标
    6. 等新 IM tab 出现
    7. 检测会话名匹配(防止点错对方)
    8. 发 msg #1(催促)
    9. 等 4s
    10. 发 msg #2(订单号列表,可能分段)
    11. window.close 关 IM tab
    """
    
    first_order = merchant_entry["orders"][0]
    expected_shop = merchant_entry["shop"]
    msg1 = merchant_entry["msg1"]
    msg2_parts = split_msg2(merchant_entry["orders"], merchant_entry["msg2_prefix"])
    
    # Step 1-5: 找会话
    order_tab = await client.find_or_open_order_list_tab()
    await client.set_keyword_input(order_tab, first_order)
    await client.click_search(order_tab)  # 真鼠标
    await asyncio.sleep(2)
    
    coords = await client.find_wangwang_icon_coords(order_tab)
    if not coords:
        return {"status": "no_match", "shop": expected_shop}
    
    new_tab = await client.click_and_capture_new_tab(order_tab, coords)
    
    # Step 6-7: 验证
    await asyncio.sleep(4)
    actual_shop = await client.read_im_partner(new_tab)
    if not partial_match(actual_shop, expected_shop):
        return {"status": "wrong_partner", "expected": expected_shop, "actual": actual_shop}
    
    # Step 8-10: 发送
    results = []
    
    r1 = await client.send_im_message(new_tab, msg1)
    if not r1["ok"]:
        return {"status": "send_failed", "phase": "msg1", "details": r1}
    results.append({"part": "msg1", "ok": True})
    
    await asyncio.sleep(4)
    
    for i, part in enumerate(msg2_parts):
        r = await client.send_im_message(new_tab, part)
        if not r["ok"]:
            return {"status": "send_failed", "phase": f"msg2_part{i+1}", "details": r}
        results.append({"part": f"msg2_part{i+1}", "ok": True})
        if i < len(msg2_parts) - 1:
            await asyncio.sleep(4)
    
    # Step 11: 清理
    await client.close_tab(new_tab)
    
    return {"status": "sent", "shop": expected_shop, "messages": results}


def split_msg2(orders: list, prefix: str) -> list:
    """把订单号列表切成 ≤480 字符的多段"""
    parts = []
    cur = prefix + " " if prefix else ""
    cur_started = bool(cur)
    
    for o in orders:
        sep = "" if not cur or cur == prefix + " " else "、"
        candidate = cur + sep + o
        if len(candidate) > 480:
            parts.append(cur)
            cur = o
        else:
            cur = candidate
    
    if cur:
        parts.append(cur)
    return parts
```

## 配置文件

```yaml
# config/pc1.yaml

machine_name: "PC1"

accounts:
  - name: A1
    mcp_url: "ws://localhost:17861"
    browser: "chrome"
    browser_exe: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    user_data_dir: "D:\\Profiles\\acct-A1"
    proxy: "http://代理IP_A1:8080"

  - name: A2
    mcp_url: "ws://localhost:17862"
    browser: "edge"
    browser_exe: "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
    user_data_dir: "D:\\Profiles\\acct-A2"
    proxy: "http://代理IP_A2:8080"

  - name: A3
    mcp_url: "ws://localhost:17863"
    browser: "brave"
    browser_exe: "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
    user_data_dir: "D:\\Profiles\\acct-A3"
    proxy: "http://代理IP_A3:8080"

  - name: A4
    mcp_url: "ws://localhost:17864"
    browser: "vivaldi"
    browser_exe: "C:\\Program Files\\Vivaldi\\Application\\vivaldi.exe"
    user_data_dir: "D:\\Profiles\\acct-A4"
    proxy: "http://代理IP_A4:8080"

  # ... A5-A6 用 Opera / 其他 Chromium 浏览器

push:
  type: "feishu"  # 或 "dingtalk" / "wechat-work"
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/XXX"
  message_template: "{machine} / {account} 触发滑块,请处理"

captcha:
  detection_keywords:
    - "拖动滑块"
    - "安全验证"
    - "验证码拦截"
  poll_interval_seconds: 2
  timeout_minutes: 30

throttle:
  inter_send_delay_seconds: 4
  inter_account_delay_seconds: 10  # 同机切账号间隔
  max_concurrent_active_accounts: 3  # 防止 6 账号同时触发滑块潮
```

## open-claude-in-chrome 多端口的现实问题

Aggregator 假设了 "6 个扩展实例各自监听不同端口"。但 `open-claude-in-chrome` 实际能不能这么干**取决于扩展实现**:

| 扩展架构假设 | Aggregator 是否能直接连 | 工作量 |
|---|---|---|
| 端口在 extension options 可配 | ✅ 直接配 6 个不同端口 | 0 |
| 端口写死,但扩展开源 | ✅ fork + 改一行常量 + 6 套打包 | 0.5-1 天 |
| 用 Native Messaging Host + 外部 daemon | △ daemon 需要支持多实例 + Host 注册各 profile 各一份 | 1-2 天 |

**接入前必查**(见 `09-open-questions-and-todos.md` 的考察项):
1. 装一份扩展,看 options / popup 有无端口配置
2. 看 manifest.json 是否有 `nativeMessaging` 权限
3. 看 GitHub repo README 怎么说

## 一图总结

```
                LLM(Claude Code 或自建 Agent)
                          │
                          │ MCP × 1
                          ▼
            ┌──────────────────────────┐
            │  Aggregator MCP Server   │
            │                          │
            │  高层工具(chase_send_one)│
            │  低层工具(browser_*)    │
            │  + 滑块感知 + IM push     │
            │  + Tab 管理               │
            └─────────┬────────────────┘
                      │
                      │ MCP × 6
                      ▼
        ┌────┬────┬────┬────┬────┬────┐
        │A1  │A2  │A3  │A4  │A5  │A6  │  ← open-claude-in-chrome
        │端口 │端口 │端口 │端口 │端口 │端口 │  ← 不同端口/Native Host
        │1786│1786│1786│1786│1786│1786│
        │  1 │  2 │  3 │  4 │  5 │  6 │
        └─┬──┴─┬──┴─┬──┴─┬──┴─┬──┴─┬──┘
          │    │    │    │    │    │
          ▼    ▼    ▼    ▼    ▼    ▼
         Chrome × 6 (各 profile,各账号,各代理 IP)
                      │
                      ▼
                    1688
```

## 下一步

- 了解 LLM 端是什么 → `04-agent-platform.md`(自建 Agent)或 `01-current-state.md`(Claude Code)
- 了解 LLM 怎么选/切 → `05-llm-router-multi-provider.md`
