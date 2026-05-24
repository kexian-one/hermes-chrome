---
name: cleanup-tabs
description: 关闭浏览器里除一个新 about:blank 标签外的所有 tab。常用于定期清理累积的旧 tab、释放浏览器资源、保留登录态。触发场景:用户问"关一下 tab"、"清理 tab"、"关掉所有标签",或直接调用 /cleanup-tabs。
max_idle_minutes: 3
---

# cleanup-tabs

## 何时调用

- 用户问"关一下 tab"、"清理 tab"、"关掉所有标签"
- 浏览器开了一堆 navigation 留下的旧 tab,占内存 / 视觉污染
- 用户直接 `/cleanup-tabs`

## 执行步骤

### Step 1: 看现在有什么工具可用

启动后,你会收到一份 MCP tool 列表。从中找跟"tab 操作"相关的工具,可能的名字:

- `tabs_context_mcp` — 获取当前 tab 信息(肯定有)
- `tabs_create_mcp` — 新建 tab(肯定有)
- 还有 3 个未列出的 tabs/window 相关 — 可能是 `tabs_close_mcp` / `tabs_list_mcp` / `tabs_focus_mcp` 类似的,**先看完整 tool 列表**再决定怎么调

### Step 2: 新建一个 about:blank 作为保留 tab

先 `tabs_create_mcp(url="about:blank")`(或等价工具),拿到新 tab 的 ID。**留这个 tab 不动**,因为浏览器至少要 1 个 tab,否则可能关掉浏览器。

### Step 3: 列出当前所有 tab,关掉除新建的之外

- 如果有 `tabs_close_mcp` / `tabs_remove_mcp` 之类的工具,直接对每个其他 tab ID 调一次
- 如果没有专门关 tab 的 MCP 工具,**fallback**:对 about:blank tab 调 `javascript_tool` 执行 `chrome.runtime.sendMessage(...)` 走扩展层 API(可能不通);或者用 `computer` 工具按 Ctrl+W 关闭(暴力,不可靠)
- 第一选择是 MCP 原生 tabs 工具;实在没有再 fallback

### Step 4: 验证

再 `tabs_context_mcp` 一次,确认只剩 1 个 about:blank。

### Step 5: 写一行 log

`write_file("logs/cleanup-tabs-{worker_id}-{ts}.log", "tabs cleaned, kept=1, closed=N at <ISO>")`

文件名里的 `{worker_id}` 用 `b2` 之类(你跑在哪台 worker 上你自己知道),`<ISO>` 用当前 UTC ISO 时间。

## 注意

- **不要 navigate 到其他 URL**,否则会新增 tab 不是清 tab
- **不要关 about:blank 那个新建的 tab** — 至少要剩 1 个
- 如果发现自己无能为力(没有可用工具),老老实实 LLM finish_reason=stop 报告"没找到关 tab 的工具,需要手动",不要瞎调
- 1688 / 京东 等已登录页面的 tab 关掉**不丢登录**(cookies 在 User Data dir,跟 tab 无关)

## 输出

- 日志文件一行
- 浏览器只剩 1 个 about:blank tab
- LLM 最终回复"已清理 N 个 tab,保留 1 个"
