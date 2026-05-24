> 返回索引: [DOC/09](09-open-questions-and-todos.md)

# 09c — P2 优先(影响 Phase 3+,但 MVP 可先不解决)

### Q8: 任务队列选型 — Redis 自建 vs 云上 vs 其他

**为什么重要**:多机协调要中心队列。

**决策(2026-05-23,用户确认)**:**第一版不做任务队列**。

> "先不去做任务队列,先做最简单的,每一台机器就用来跑不同账号的开发票、催发票"

**MVP 架构(简化版)**:

- 每台 PC = **1 个 master + 6 个 worker = 6 个账号并发**(架构详见 Q15)
- 跨机**完全独立**(**永久决策**,不会变):不连 Redis、不连消息总线、不做任务分发、不做多机 master 协调
- 多账号水平扩展靠"加 PC":N 台 PC × 6 账号 = 6N 账号,各台机独立运行
- 共享 git 仓库保留(代码 / skill / knowledge 同步用)— 单向 pull/push,不走队列
- 单机 master ↔ worker 用 in-process 调用 / 本地 subprocess,**不需要任务队列**

**触发重做的条件**(以后回来再决策):

- 出现"一台机要在 6 worker 之外再加更多并发"需求 → 才需要本机任务队列
- 跨机协调**已明确永不做** — 不在触发条件里

**对其他文档的影响**(已同步):

- [04-agent-platform](04-agent-platform.md):整章降级,Redis 队列 / 跨机协调 / 多机 master 全部归入"不做",顶部已加注解
- "已决定的事"表已更新"跨机通信"等多行(见本文末尾)

### Q9: Knowledge / Memory 的具体格式

**为什么重要**:经验沉淀的产出格式决定后续可用性。

**待查项**:
1. 是否每条 knowledge 用单独 markdown 文件
2. frontmatter 字段约定(topic / source / created / version 等)
3. 如何处理冲突(两台机同时写同一 topic)

**结论**(填):
- [ ] 格式规范:___
- [ ] 冲突策略:___

### Q10: Vector DB 用不用 / 何时上

**为什么重要**:知识库少时 grep 就够,多了要向量检索。

**待决策**:
- knowledge/ 文件数 < ___ 时不上向量
- 达到阈值后选 ___(Qdrant / Chroma / Milvus)

### Q11: Web Dashboard 范围

**决策(2026-05-23,用户确认)**:**永久不做网页控制台**(Phase 1 不做,Phase 2 飞书 bot 替代核心功能后**也没必要做**)。

> "先不做网页控制台"

**为什么没必要做**(Phase 2 起):

[Q7](09b-p1-decisions.md#q7-飞书机器人接入--长连接--自建应用不用公网-webhook) 决策的飞书 bot 直接覆盖了 dashboard 的所有核心需求 — 查状态 / 重启 worker / 看日志 / 看统计,全部用 @ 机器人 + 自然语言搞定,而且移动端原生支持。dashboard 唯一剩下的差异化能力是"图表 / 趋势可视化",这个目前没明确需求,真要时直接接 Grafana / 飞书多维表格,**不需要自建网页**。

**MVP / Phase 1 替代方案(飞书 bot 上线前)**:

- master 主进程的终端 stdout 看实时状态
- `logs/worker-b{n}-*.log` 看每个 worker 的日志
- git commit history 看 knowledge 沉淀进度

**触发回来重做的条件**(以后出现任一条再说):

- 单机日志肉眼盯不过来(罕见,因为 6 worker = 6 个日志文件)
- 扩到 ≥ 2 台机,跨终端切换看烦了
- 业务团队 / 老板要随时看状态(目前没有这个角色)
- 长时间出门 / 睡觉时要远程看

**真要做时,最小范围**(待真触发后再细化):

- [ ] 6 worker 状态表(worker_id / 当前 skill / running|success|failed)
- [ ] 重启某 worker 的按钮
- [ ] **不做**:图表 / 趋势 / 用户登录 / 权限管理(YAGNI)
- [ ] 技术选型:本地 Flask + WebSocket,端口 8080,不接公网
