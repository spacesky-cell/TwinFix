# 架构决策记录

| # | 决策 | 状态 |
|---|---|---|
| 001 | 修复在每事故临时孪生环境中验证，而非常驻第二条测试线 | 已接受 |
| 002 | 按工具/权限/上下文拆分 5 个 Agent | 已接受 |
| 003 | 权限在工具网关与凭据层强制，不依赖 Prompt | 已接受 |
| 004 | 服务端 Python + LangGraph，Java 用于示例服务与探针 | 已接受 |
| 005 | 语言相关逻辑全部收敛到 LanguageProfile | 已接受 |
| 006 | 编排用 LangGraph + PG checkpoint，任务派发用 RabbitMQ | 已接受 |
| 007 | 验证结果以 GitHub Check 绑定 commit SHA | 已接受 |

## ADR-001 临时孪生环境

背景：需要一个 Agent 能自由部署修复、又不影响测试线的环境。
决策：每个 Incident 按需用 Docker Compose 拉起独立 Twin，数据/MQ/Redis/外部调用隔离，TTL 回收。
理由：常驻第二条线成本翻倍、与测试线漂移、无法并发修多个 bug。
代价：每次拉起需要构建与启动时间（预计数分钟）；需要项目提供可用的 compose 与 `twin` profile。

## ADR-002 五个 Agent

决策：Diagnoser / Reproducer / Fixer / Verifier / Reporter。
理由：诊断只读、修复需写代码、验证必须独立于修复者；各阶段上下文差异大，合并会导致上下文膨胀。
验证：评测中做"单 Agent vs 多 Agent"对照实验，用数据检验该决策。

## ADR-003 权限在模型之外

决策：Agent 仅通过 ToolGateway 调用工具；写权限凭据由适配器持有；危险操作不提供工具。
理由：Prompt 注入与模型失误不可避免，必须保证最坏情况可控。

## ADR-004 语言选型

决策：主服务 Python（LangGraph 生态成熟），Java 用于 demo-shop、Arthas 探针。
理由：Agent 生态以 Python 为主；被诊断对象是 Java，示例服务与探针用 Java 更真实。

## ADR-005 LanguageProfile

决策：构建、测试、栈解析、测试路径、探针都由插件提供。
理由：v1 只做 Java，但后续要支持 Python/Go，核心不应改动。

## ADR-006 编排与派发

决策：状态机用 LangGraph 实现并以 PostgreSQL 保存 checkpoint；每一步由 RabbitMQ 消息驱动 worker 执行。
理由：长流程需要中断（等人审批、等 Jenkins 回调）与崩溃恢复；MQ 提供削峰、重试、死信与水平扩展。

## ADR-007 验证绑定 SHA

决策：Verifier 结果写成 GitHub Check `twinfix/verify` 挂在具体 sha 上，配合分支保护 strict 模式。
理由：目标分支变化后旧结论自动失效，合并前强制重验，复用 GitHub 原生机制而不是自造。
