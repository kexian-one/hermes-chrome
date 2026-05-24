# 09 — 还没决定的事 + 待考察清单

## 用法

本文件列出**做决策 / 接入前必须搞清楚**的问题。建议:

- 按优先级排查 → 决定前不要动后续工程
- 每解决一个就更新此文件,把结论记下来(供后续参考)
- 新增问题随时加,不要让问题悬空

---

## 优先级总览

| 优先级 | 子文件 | 内容 |
|---|---|---|
| P0(影响整体能不能跑) | [09a-p0-decisions.md](09a-p0-decisions.md) | Q1-Q4:扩展架构、多浏览器支持、业务效果、代理选型 |
| P1(影响 Phase 1-2 顺畅度) | [09b-p1-decisions.md](09b-p1-decisions.md) | Q5-Q7, Q15:LLM 实测、Skill 兼容、飞书接入、单机拓扑 |
| P2(Phase 3+,MVP 可先不解决) | [09c-p2-deferred.md](09c-p2-deferred.md) | Q8-Q11:任务队列、Knowledge 格式、Vector DB、Dashboard |
| P3(锦上添花,可推迟) | [09d-p3-future.md](09d-p3-future.md) | Q12-Q14:自动生成 skill、跨平台扩展、团队 ACL |

---

## [P0 优先决策清单 →](09a-p0-decisions.md)

**Q1** `open-claude-in-chrome` 端口可配,但需复制 6 份独立部署  
**Q2** 6 浏览器方案:复制 6 份独立部署(天然冲突)  
**Q3** 业务效果 go/no-go:用户自评,不 gate 工程  
**Q4** 代理供应商:第一版不做 IP 代理

---

## [P1 优先决策清单 →](09b-p1-decisions.md)

**Q5** 非 Claude LLM 实测:不预先 gate,OpenAI 兼容即可  
**Q6** SKILL.md 兼容:直接复用,frontmatter+body 不改  
**Q7** 飞书接入:长连接 + 每机 1 bot,代码已交付  
**Q15** 单机 Agent 拓扑:1 master(LLM-driven) + 6 worker(自建 runtime)

---

## [P2 优先决策清单 →](09c-p2-deferred.md)

**Q8** 任务队列:第一版不做  
**Q9** Knowledge/Memory 格式:待填  
**Q10** Vector DB:待决策  
**Q11** Web Dashboard:永久不做,飞书 bot 替代

---

## [P3 锦上添花清单 →](09d-p3-future.md)

**Q12** Agent 自动生成 skill:Phase 5 后再说  
**Q13** 跨平台扩展:保留可能性  
**Q14** 多人协作 ACL:先不做

---

## 已决定的事(对照表)

为了对比,把已经决定下来的事单列在这里,**不要重复讨论**:

| 决策 | 选择 | 出处 |
|---|---|---|
| 浏览器自动化层 | `open-claude-in-chrome` MCP 扩展 | 00 / 01 |
| 单机浏览器数 | 6 个不同 Chromium 浏览器(非同浏览器多 profile) | 02 |
| LLM provider | 全 OpenAI 兼容,不用 Anthropic Claude | 00 / 05 |
| 模型要求 | 全部支持多模态 | 00 / 06 |
| 滑块处理 | 人工(每机 1 人) | 00 / 02 |
| 跨机通信 | **各机完全独立 + 共享 git(无队列、无消息总线)** | 09 / Q8 |
| **跨机协调** | **永久不做**(无中央 dispatcher、无多机 master 协调) | 09 / Q15 |
| **单机 Agent 拓扑** | **1 master(LLM-driven) + 6 worker(自建 agent runtime)** | 09 / Q15 |
| 单机账号数 | **每台机器 6 账号**(6 浏览器 6 worker 并发) | 09 / Q15 |
| 任务接入 | **Phase 1 本地 Cron;Phase 2 加飞书 bot 长连接(每机 1 个)**;HTTP API / 公网 webhook 不做 | 09 / Q7+Q8 |
| **Dashboard / 远程查询** | **永不做网页 dashboard**,用飞书 bot @ 自然语言查 | 09 / Q7+Q11 |
| **飞书接入方式** | **长连接**(`lark-oapi` SDK),master 主动 outbound,**无公网入口** | 09 / Q7 |
| IP 代理 | **第一版不做**,共享本机公网 IP | 09 / Q4 |
| 业务效果评估 | **用户自评,工程不介入** | 09 / Q3 |
| 经验沉淀 | LLM 定时(每日/周)读日志总结 | 04 |
| Skill 格式 | 复用 Claude Code 的 SKILL.md(frontmatter + markdown) | 04 |
| 长期目标 | 摆脱 Claude Code,自建 Agent runtime | 00 / 04 / Q15 |

---

## 接入前的"决策清单"(checklist)

在动手 Phase 1 前,**请确认以下都已答**:

- [x] Q1: `open-claude-in-chrome` 多端口支持 → **端口可配**,但需复制 6 份独立部署(见 [09a Q1](09a-p0-decisions.md#q1-open-claude-in-chrome-扩展架构--端口是否可配))
- [x] Q2: 选用的 6 个浏览器都能装扩展 → **策略已定**(复制 6 份);具体浏览器选型在 Phase 2 实施时验证
- [x] Q3: 业务效果 go/no-go → **用户自评,不 gate 工程**
- [x] Q4: 代理供应商选定 → **第一版不做**
- [x] Q5: 非 Claude LLM 实测 → **不 gate**,模型 OpenAI 兼容即可,跑不动用户换
- [x] Q6: SKILL.md 格式直接复用 → **可以,frontmatter+body 不改**(已是开放标准,DeepSeek V4 Pro 原生适配)
- [x] Q7: 飞书机器人接入 → **Phase 2 落地**,长连接 + 每机 1 bot + user_id 白名单,无需公网
- [x] Q8: 任务队列选型 → **第一版不做队列**
- [x] Q15: 单机 Agent 拓扑 → **1 master + 6 worker,跨机永不做**

**剩余 gate**:**无**。所有 P0/P1 决策已 unblock,可以进入 Phase 1 开发。

---

## 这份文档的迭代

本目录每次新讨论 / 新决策都应该:

1. 把新决策写到对应章节(00-08)
2. 把还没决定的开放问题加到本文件
3. 把已经决定的从本文件移到"已决定的事"表
4. commit 到 git → 同步给所有 agent / 团队成员
