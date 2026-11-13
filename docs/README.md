# TwinFix 设计文档

> AI agent that diagnoses and fixes production bugs in an isolated twin environment, then hands a verified PR to humans.
> 发现 bug → 诊断 → 孪生环境复现与修复 → 自测 → 人工复测 → 合并。

状态：v0.1 alpha（Apache-2.0），已在基准测试套件上跑通完整闭环；评测方法与数据见 [07-arena-eval.md](07-arena-eval.md)。运行时支持 Java（Maven/Gradle）、Python（pytest）、Go（go build / go test）、TypeScript / JavaScript（node --test）与 C# / .NET（dotnet build / dotnet run）；基准测试套件包含 Java 的 C001–C024、Python 的 P001–P004、Go 的 G001–G004、TypeScript 的 T001–T004 与 C# 的 S001–S004。英文项目概览见 [README.en.md](../README.en.md)。

| 文档 | 内容 |
|---|---|
| [01-overview.md](01-overview.md) | 目标、范围、核心流程、术语 |
| [02-architecture.md](02-architecture.md) | 分层架构、模块职责、技术栈、部署形态、仓库结构 |
| [03-domain-model.md](03-domain-model.md) | 核心实体、数据库表、事故状态机 |
| [04-interfaces.md](04-interfaces.md) | 适配器 / 插件 / Agent / 对外 API 契约 |
| [05-safety.md](05-safety.md) | 权限分级、工具网关、孪生环境隔离、防作弊 |
| [06-git-ci-flow.md](06-git-ci-flow.md) | 分支策略、GitHub App、Jenkins、合并与冲突 |
| [07-arena-eval.md](07-arena-eval.md) | 基准测试、用例清单、评测方法与结果 |
| [08-roadmap.md](08-roadmap.md) | 后续方向与已知限制 |
| [adr/](adr/) | 架构决策记录 |
