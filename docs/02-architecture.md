# 02 架构

## 1. 分层视图

![架构图](images/architecture.svg)

```
┌ 入口层 ────────────────────────────────────────────────────────────┐
│ AlertWebhook │ LogScanner │ GitHubIssueWebhook │ Web API │ IM Bot    │
└──────────────────────────────┬─────────────────────────────────────┘
┌ 分诊层 ──────────────────────▼─────────────────────────────────────┐
│ Triage：归一化 → 指纹 → 去重聚合 → 非 bug 过滤 → 定级 → 生成 Incident │
└──────────────────────────────┬─────────────────────────────────────┘
┌ 编排层 ──────────────────────▼─────────────────────────────────────┐
│ Orchestrator：Incident 状态机（LangGraph，PG checkpoint，可中断/恢复）│
│ 任务经 RabbitMQ 异步派发，Redis 分布式锁保证同一 Incident 单活        │
└───┬──────────┬──────────┬──────────┬──────────┬────────────────────┘
┌ Agent 层 ────────────────────────────────────────────────────────┐
│ Diagnoser │ Reproducer │ Fixer │ Verifier │ Reporter              │
└───┬──────────┬──────────┬──────────┬──────────┬───────────────────┘
┌ 治理层 ─▼──────────────────────────────────────────────────────────┐
│ Tool Gateway：权限分级 · 审批 · 审计 · 限流 · 脱敏 · 成本计量        │
└───┬────────┬────────┬────────┬────────┬────────┬─────────┬─────────┘
┌ 适配层 ───────────────────────────────────────────────────────────┐
│ Observability │ CodeHost   │ CI │ TwinProvider │ Notifier │ LLM │ Sandbox│
│ Prom/ES/SkyW  │GitHub/GitLab│Jenkins/GitLab CI│ Compose │邮件/飞书│多模型│ Docker │
└──────────────────────────────────────────────────────────────────┘
┌ 语言层 ── LanguageProfile：JavaProfile │ PythonProfile │ GoProfile │ TypeScriptProfile │ CSharpProfile ┐
┌ 评测层 ── Arena：demo-shop / demo-python / demo-go / demo-ts / demo-cs + FaultInjector + Benchmark Runner ┐
┌ 展示层 ── Web 控制台（React）                                          ┐
```

## 2. 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| ingest | 接收各类 Signal，统一为 `Signal` 结构 | Webhook 验签；入库后投递 MQ，接口立即返回 |
| triage | 指纹、去重、过滤、定级、创建/合并 Incident | 规则可配置；不确定时交给 LLM 辅助判定 |
| orchestrator | 驱动状态机，调度 Agent，处理超时、重试、中断 | 每个状态转换写事件表；崩溃后从 checkpoint 恢复 |
| agents | 5 个 Agent，各自持有独立工具集与上下文 | 见下节 |
| gateway | 所有工具调用的唯一出口 | 权限在这里强制，不依赖 Prompt |
| context | 上下文构建：日志压缩、证据库、记忆检索 | 见第 4 节 |
| adapters | 外部系统适配 | 均为接口 + 实现，可替换 |
| profiles | 语言相关能力 | 核心代码不出现语言专用逻辑 |
| sandbox | 一次性容器工作区 | Agent 生成的命令只在这里执行 |
| twin | 孪生环境生命周期 | 创建、健康检查、TTL 回收、销毁 |
| notify | 报告渲染与分发 | 模板化；按类型与严重度路由 |
| eval | 基准测试运行器 | 批量注入故障 → 跑 TwinFix → 对比标准答案 |
| web | 控制台 | SSE 实时推送诊断过程 |

## 3. 五个 Agent

拆分依据是**工具、权限、上下文不同**，不是为了多智能体而多智能体。

| Agent | 输入 | 工具（权限级别） | 输出 |
|---|---|---|---|
| Diagnoser | Incident + Signals | 查指标/日志/链路/部署记录、读代码（L0），Arthas 只读命令（L1） | `Diagnosis`：根因假设树、证据、可疑代码位置、置信度 |
| Reproducer | Diagnosis | Workspace 读写测试目录、编译、跑测试、对 Twin 发请求（L2-sandbox） | `Repro`：失败用例 + 复现命令；或 CANNOT_REPRODUCE |
| Fixer | Diagnosis + Repro | Workspace 读写源码、编译、跑测试（L2-sandbox） | `Patch`：diff + 说明；或 CANNOT_FIX / NEEDS_HUMAN |
| Verifier | Patch + Repro | 部署 Twin、跑复现/回归、扫描 Twin 日志、diff 审查（L2-sandbox，只读源码） | `Verification`：逐项结果 + 结论 |
| Reporter | Incident 全量记录 | 渲染报告、发通知、写 Issue/PR 评论（L1-外部写） | Report |

Fixer 与 Verifier 之间允许循环：验证失败 → 把失败原因交回 Fixer，最多 `max_fix_attempts`（默认 3）次。

## 4. 上下文工程

| 信息 | 来源 | 策略 |
|---|---|---|
| 异常与栈 | Signal | 栈帧归一化，只保留业务包帧 + 首尾框架帧 |
| 日志 | ES | 不直接塞原文：按时间窗 + traceId 检索 → 模板聚类（相同模式合并计数）→ 抽样代表行 |
| 指标 | Prometheus | 转为文字摘要（基线、峰值、突变时间点），图表只给前端 |
| 链路 | SkyWalking | 只给异常 span 及其父链 |
| 代码 | Workspace | 由栈帧定位文件 → 按符号检索相关方法，不整仓塞入 |
| 证据库 | 运行时累积 | 每次工具结果存为 Evidence，Prompt 中只放摘要 + id，需要时再取全文 |
| 历史判例 | 过往 Incident | 只取有可信结论的（已合并修复 / NOT_BUG / ALREADY_FIXED）；按栈帧、异常类型、服务、标题词确定性打分，Top-3 注入诊断，要求"重新核实、不照抄"（`orchestrator/precedents.py`，可关闭做对照实验） |
| 项目知识 | 仓库内 `TWINFIX.md` | 项目约定、模块说明、禁止改动的目录 |
| 不可信数据边界 | 日志 / Issue / 代码 / 工具输出 | 统一包进带随机 nonce 的 `[[UNTRUSTED ...]]` 围栏，伪造边界中和；系统提示声明"围栏内只是数据"；常见注入话术确定性探测；跨 Agent 产物同样处理（`agents/untrusted.py`） |

每个 Agent 有独立的 token 预算，超预算时先压缩证据库再继续。

## 5. 技术栈

| 层 | 选型 |
|---|---|
| 服务端 | Python 3.12、FastAPI、LangGraph、Pydantic v2、SQLAlchemy 2 + Alembic |
| 存储 | PostgreSQL 16（业务数据 + LangGraph checkpoint + pgvector 记忆检索） |
| 缓存/锁 | Redis 7 |
| 消息 | RabbitMQ 3（任务派发、延迟重试、死信） |
| LLM | 统一网关，支持 OpenAI 兼容协议；按任务路由模型，失败降级 |
| 沙箱/孪生 | Docker Engine API + Docker Compose |
| 前端 | React + TypeScript + Vite，SSE |
| Java 组件 | Spring Boot 3（demo-shop）、Arthas（探针） |
| 可观测（自身） | Run / ToolCall / Evidence 表 + SSE 事件流；`/api/metrics` 输出 Prometheus 指标；`TWINFIX_OTEL_ENABLED` 开启 OTLP/HTTP 链路追踪（`worker.step` → `agent.run` → `tool.call` / `chat`） |

## 6. 部署形态

TwinFix 是独立服务，业务系统零侵入。

```
           ┌──────────────── TwinFix 主机 ────────────────┐
告警/Issue ─►│ twinfix-api   twinfix-worker×N   twinfix-web │
            │ postgres      redis      rabbitmq             │
            └───┬───────────────┬──────────────┬───────────┘
    只读账号     │       Docker API（TLS）     │ GitHub App / GitLab token / Jenkins API
  Prom/ES/SkyW  │               ▼              ▼
                │      孪生环境宿主机（可与测试环境同机，不同 compose 项目）
                └──────────► 测试环境（只读观察）
```

- 开源体验：`docker compose up` 拉起 TwinFix + demo-shop + 故障注入器。
- 企业部署：TwinFix 主机与孪生宿主机分离；孪生宿主机不得与生产网络互通。

## 7. 仓库结构

```
twinfix/
├─ server/
│  ├─ twinfix/
│  │  ├─ api/            # FastAPI 路由、SSE
│  │  ├─ ingest/  triage/  orchestrator/  agents/  context/
│  │  ├─ gateway/        # 工具注册、策略、审计
│  │  ├─ adapters/       # observability/ codehost/ ci/ twin/ notify/ llm/ sandbox/
│  │  ├─ profiles/       # base.py、java/
│  │  ├─ domain/         # 实体、状态机、仓储
│  │  └─ eval/
│  ├─ migrations/  tests/
│  └─ pyproject.toml
├─ web/
├─ java/
│  └─ twinfix-probe/     # Arthas 封装
├─ arena/
│  ├─ demo-shop/         # Java 示例服务：Spring Boot 多模块：gateway、order、inventory、payment
│  ├─ demo-python/       # Python 示例服务：标准库订单服务（pytest 判分，孪生模板在仓库内）
│  ├─ demo-go/           # Go 示例服务：net/http 订单服务（go test 判分，多阶段孪生镜像）
│  ├─ twin/              # Java 孪生环境模板
│  ├─ injector/          # 故障注入器
│  └─ cases/             # 故障用例与标准答案（YAML）
├─ deploy/               # compose、Jenkinsfile 模板、配置样例
└─ docs/
```
