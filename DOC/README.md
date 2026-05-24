# All-in-AI — 自建多账号 Agent 平台设计文档

本目录是 2026-05-22 与 Claude 协作讨论后整理的**多账号 Agent 平台**设计稿。从"1688 发票催单"这个具体场景出发,推演到"自建可扩展的通用 Agent 平台"的完整方案。

## 写作场景与已知约束

- 起步业务:**1688 发票催单**(已有可工作的 Claude Code skill 跑过 batch1 + batch2 部分,见 `01-current-state.md`)
- 规模:**20+ 个 1688 买家账号**,分散到 **3-4 台 PC**,每台 6 账号(每台有人值守)
- 长期目标:**摆脱 Claude Code 依赖**,自建 Agent 平台,支持飞书/钉钉接任务、知识沉淀、多任务复用
- LLM 选型:**全 OpenAI 兼容 API**,不用 Claude(用 GPT / DeepSeek / Qwen / GLM 等)
- 模型要求:**全部支持多模态输入**
- 浏览器自动化:**必须用 `open-claude-in-chrome`**(开源 MCP 扩展,Playwright 已验证被 1688 风控)
- 滑块处理:**人工**(每机一人,微信/钉钉 push 提醒)

## 文档地图

按"由近及远 / 由实到虚"排列:

| # | 文件 | 内容 | 何时读 |
|---|---|---|---|
| 00 | [00-context-and-goals.md](00-context-and-goals.md) | 业务背景、目标、设计约束 | 想了解"为什么这么设计" |
| 01 | [01-current-state.md](01-current-state.md) | 已有的 skill、batch 文件、进度 | 想继续/接手当前工作 |
| 02 | [02-multi-account-deployment.md](02-multi-account-deployment.md) | 多机 × 多账号物理部署架构 | 想了解硬件/账号怎么布 |
| 03 | [03-aggregator-design.md](03-aggregator-design.md) | 单机 Aggregator(管 6 浏览器)设计 | 想了解软件如何统一 6 个扩展 |
| 04 | [04-agent-platform.md](04-agent-platform.md) | 自建 Agent 平台总架构(摆脱 Claude Code) | 想了解长期目标架构 |
| 05 | [05-llm-router-multi-provider.md](05-llm-router-multi-provider.md) | 多 LLM Provider 路由设计 | 想了解 LLM 怎么选/切 |
| 06 | [06-multimodal-vision.md](06-multimodal-vision.md) | 多模态(视觉)能力利用 | 想了解什么时候用截图 |
| 07 | [07-implementation-roadmap.md](07-implementation-roadmap.md) | 分阶段实施路线 + 工时 | 想了解开发顺序 |
| 08 | [08-tech-stack-and-costs.md](08-tech-stack-and-costs.md) | 技术栈选择 + 运营成本估算 | 想算钱 + 选技术 |
| 09 | [09-open-questions-and-todos.md](09-open-questions-and-todos.md) | 还没决定的事 + 待考察清单(索引 + 已决定表) | 想知道下一步要做什么决策 |
| 09a | [09a-p0-decisions.md](09a-p0-decisions.md) | P0 优先决策:Q1-Q4(扩展架构、多浏览器、代理) | Phase 2 实施前必读 |
| 09b | [09b-p1-decisions.md](09b-p1-decisions.md) | P1 优先决策:Q5-Q7, Q15(LLM、Skill、飞书、单机拓扑) | Phase 1-2 顺畅度 |
| 09c | [09c-p2-deferred.md](09c-p2-deferred.md) | P2 延后:Q8-Q11(任务队列、Knowledge 格式、Dashboard) | Phase 3+ 再看 |
| 09d | [09d-p3-future.md](09d-p3-future.md) | P3 锦上添花:Q12-Q14(自动 skill、跨平台、团队 ACL) | 远期参考 |
| 10 | [10-deployment-guide.md](10-deployment-guide.md) | 全新电脑从零部署 + 浏览器扩展装 + 飞书 bot 配置 + 启动 | 想动手部署时必读 |

## 快速读法(按角色)

**如果你是即将动手的工程师**:
1. 读 `00`(理解背景)
2. 读 `01`(看已有产出)
3. 跳到 `07`(实施路线)
4. 按 phase 读相关章节

**如果你是项目负责人 / 决策者**:
1. 读 `00`(背景)
2. 读 `07`(路线)+ `08`(成本)
3. 读 `09`(未决问题)

**如果你想理解整个系统的设计哲学**:
1. 按 00 → 02 → 03 → 04 → 05 → 06 顺序读
2. 这是从"具体场景"到"抽象平台"的演进过程

## 关键设计决策(本目录所有文档的共同前提)

| 决策点 | 选择 | 决策依据 |
|---|---|---|
| 浏览器自动化层 | `open-claude-in-chrome` MCP 扩展 | Playwright 实测被 1688 风控,扩展+真 Chrome 风控认账 |
| 浏览器实例 | 多 Chrome profile / 多浏览器内核(Chrome+Edge+Brave) | 不同指纹降低跨账号关联风险 |
| LLM provider | 全 OpenAI 兼容(不用 Anthropic) | 单 adapter 即可覆盖所有目标 provider |
| 模型多模态 | 选型时只选支持多模态的模型 | 给截图辅助决策保留可能性 |
| 滑块处理 | 人工 | 打码服务有合规与封号风险,人工更稳 |
| 跨机协调 | 各机独立,通过共享 git 仓库同步 skill+knowledge | 无需中心服务,降低运维 |
| 任务接入 | 飞书/钉钉机器人 → Redis 队列 | 利用 IM 已有触达 |
| 经验沉淀 | LLM 定时(每日/周)读日志总结 → 写知识库 → git commit | 自动化但有人工 review 入口 |

## 状态

文档生成于 **2026-05-22**,基于当天与 Claude (Anthropic Claude Opus 4.7) 协作设计的内容。

- 业务侧验证:**未完成**(batch1 5 家 + batch2 16 家已发,商家已有回复但开票率未完整跟踪)
- 技术侧验证:**未开始**(本文档之后才动手)
- 决策侧:见 `09-open-questions-and-todos.md`

## 相关产出(不在本目录)

- 1688 fapiao 抓数据 + 催单 skill:`C:\Users\<user>\.claude\skills\fapiao-1688\` + `fapiao-1688-chase\`
- 已抓数据 CSV:`d:\ai\fapiaoV1\1688_applying_invoices_summary.csv` + `..._orders.csv`
- 催单 batch 文件:`d:\ai\fapiaoV1\chase_messages_batch1.md` + `_batch2.md`
