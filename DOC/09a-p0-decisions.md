> 返回索引: [DOC/09](09-open-questions-and-todos.md)

# 09a — P0 优先决策(影响整体能不能跑)

### Q1: `open-claude-in-chrome` 扩展架构 — 端口是否可配?

**为什么重要**:Phase 2 要在单机跑 6 个不同浏览器,每个浏览器扩展用不同端口。如果扩展端口写死,需要 fork 改源码。

**调研来源**:[noemica-io/open-claude-in-chrome](https://github.com/noemica-io/open-claude-in-chrome)(README + `install.sh` + `host/native-host.js`, 2026-05-23 抓取)

**结论**:

- **架构**:**Native Messaging + 本地 daemon**(三段式)
  - `Manifest V3 扩展` ←(Chrome native messaging,stdio)→ `host/native-host.js` ←(TCP localhost:port)→ `host/mcp-server.js`(Node.js)
  - 不是纯 WebSocket-in-extension
- **端口可配**:**是**,通过 `~/.config/open-claude-in-chrome/config.json` 的 `{ "port": <n> }` 字段
  - 默认 18765
  - **没有** env var 或 CLI flag,只能改 config 文件
- **多 session 模式**(原生默认):同一份扩展跑多个 Claude Code session 时,**第一个 session 是 "primary",独占 TCP 端口**,后续 session 作为 client 连到 primary 多路复用
  - **这与我们 6 浏览器各自独立 agent 的诉求相反** — 默认设计假设一个 MCP server 多路复用所有 client
- **install.sh 多浏览器支持**:`./install.sh <chrome-id> <brave-id> <vivaldi-id> ...`,把所有扩展 ID 写入**同一份** manifest 的 `allowed_origins`,所有浏览器共享**同一个** native host 名(`com.anthropic.open_claude_in_chrome`),指向**同一个** MCP server(同一端口)
  - 也就是说**原生的多浏览器支持是"一对多",不是"一对一独立"**
- **install.sh 不支持 Windows**:macOS/Linux 通过文件路径注册 native host,Windows 必须改注册表(`HKCU\Software\Google\Chrome\NativeMessagingHosts\<host-name>`,值为 manifest json 的路径)。**装到 Windows 需要自己写 .reg 或 PowerShell 脚本**
- **不支持的浏览器**:Vivaldi / Opera 不在 install.sh 默认路径里,要查它们的 NativeMessagingHosts 目录另写注册

**对 Phase 2(6 浏览器 6 独立 agent)的影响**:

我们**不能**直接用上游的"一份扩展 + 一个 daemon + 多 session"模式,要的是**6 个完全独立的实例**(因为 6 个 agent 进程要并发,各自专属一个浏览器)。需要 **6 份"插件 + daemon"独立部署**:

1. 把 `open-claude-in-chrome/` 项目复制 6 份(`open-claude-in-chrome-b1` ~ `-b6`)
2. 每份的 `config.json` 写不同端口(18765, 18766, ..., 18770)
3. 每份的 native host 名改成不同(`com.anthropic.open_claude_in_chrome.b1` ~ `.b6`),避免 manifest 冲突
4. 每个浏览器(Chrome / Edge / Brave / Vivaldi / Opera / 第六个)只注册 1 个对应的 native host
5. 每个 Claude Code 实例 `claude mcp add` 指向自己那份 `mcp-server.js`

**改源码工作量估计**:**0 人时(不改源码)** + **2-4 人时(写多实例部署脚本 + Windows 注册表批量注入)**

参考 issue:[claude-chromium-native-messaging](https://github.com/stolot0mt0m/claude-chromium-native-messaging)(已经做了 Brave / Arc / Vivaldi / Edge / Genspark 多浏览器适配,可借鉴)

### Q2: 6 个不同浏览器在同一台 PC 同时跑,能不能装同一份扩展?

**为什么重要**:不同浏览器对 Chrome Web Store 扩展支持度不同。如果 Brave / Vivaldi / Opera 装不了 `open-claude-in-chrome`,要换其他浏览器或重做扩展支持。

**决策(2026-05-23,用户确认)**:

> **能装同一份就装同一份;装不了同一份(端口冲突、native host 名冲突、manifest 冲突等)就把每个插件的文件夹复制一份出来,独立部署。**

结合 Q1 的调研结论:**6 浏览器场景下必然冲突**(端口默认共享 + native host 名共享 + primary/client 多路复用模型不符合需求),因此实际方案是**复制 6 份独立部署**。

**实施步骤(Phase 2 用)**:

1. 把 `open-claude-in-chrome/` 复制 6 份:`oicc-b1` ~ `oicc-b6`(对应 6 个浏览器)
2. 每份独立的:
   - `config.json` → 不同端口(18765–18770)
   - native host 名 → `com.anthropic.open_claude_in_chrome.b{1..6}`
   - 扩展 ID(每个浏览器加载未打包扩展时生成的 ID 都不同,天然分开)
3. 各浏览器只注册自己那份的 native host(macOS/Linux 改 manifest 路径,Windows 写注册表 `HKCU\Software\<browser>\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b{n}`)
4. Phase 2 启动前在测试机跑一次冒烟:6 个 Chromium 全开 + 6 个 MCP server 跑、检查端口冲突 / 内存 / CPU
5. 内存 / CPU 不达标时降级方案:**减浏览器数量(从 6 → 4)** 而非换架构

**还要验的事**(Phase 2 实施时):

- [ ] Vivaldi / Opera 的 NativeMessagingHosts 目录路径(Linux/macOS/Windows 三平台)
- [ ] 6 浏览器同开内存基线(中端 i5 + 16GB,每个 Chromium 静默状态 ≈ 200-400MB,6 个就是 1.2-2.4GB,加 6 个 Node.js MCP server 估 ≈ 600MB,合计 ≈ 2-3GB,16GB 应该够)
- [ ] 第六个浏览器选什么(Chrome / Edge / Brave / Vivaldi / Opera 之外)— 候选:Arc / Yandex / 360 极速浏览器(国内反风控可能加分)

### Q3: 业务效果 — 催单后开票率是否真的提升?

**为什么重要**:这是整个 roadmap 的 go/no-go gate。Phase 0 必跑。

**决策(2026-05-23,用户确认)**:**这块用户自己评估,本工程不介入**。Phase 1 启动不再由这条数据 gate,以用户口头确认为准。

> "这属于我们这边考虑的,你不用去管这一块"

(Batch1 + Batch2 已发 21 家的开票率追踪用户自行处理,不在 agent / 文档迭代范围内。)

### Q4: 代理供应商选择

**为什么重要**:代理是刚需,选错了又贵又被识别。

**决策(2026-05-23,用户确认)**:**第一版不做 IP 代理**。

> "在第一个版本先不做 IP 代理"

第一版直接用单机本地公网 IP 跑(每台机器一个出口 IP),先把流程跑通,**等 1688 出现风控信号(滑块爆增、IP 限速、账号异常登录)再回来选代理**。

**这意味着**:

- Phase 1-2 不需要在每个浏览器配置 SOCKS / HTTP proxy
- 单台机器上的多浏览器**共享同一公网 IP**(用户接受这个风险)
- 触发风控的早期指标后续要在 [04-agent-platform](04-agent-platform.md) 或日志侧加监控

延后但仍需关注:见 P2 章节(条件触发后再回来评估代理)。
