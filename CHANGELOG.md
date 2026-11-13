# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构，版本号采用 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0-alpha] - 2026-11-13

首个公开 alpha 版本。

### 新增

- 事故闭环：Webhook / 日志扫描 / Issue / 手动 / REST 接入，分诊、诊断、孪生环境复现、修复、独立验证、人工审批与合并。
- 四个 LLM Agent + 确定性 Reporter；LangGraph 状态机、RabbitMQ 派发、Redis 单活锁与 PostgreSQL checkpoint。
- 工具网关：风险分级授权、审批、审计、脱敏、预算与测试完整性扫描。
- 不可信数据围栏与 Prompt 注入探测。
- 代码智能：tree-sitter 符号索引、引用与调用查询、git 历史。
- 五种语言 Profile：Java、Python、Go、TypeScript / JavaScript、C# / .NET。
- 多仓库诊断与跨仓库修复。
- GitHub / GitLab 代码托管、Jenkins / GitLab CI、Compose / Kubernetes 孪生提供方。
- 40 例基准测试套件与 `twinfix-arena validate / eval / gate / compare`。
- Prometheus 指标与可选 OpenTelemetry 链路追踪。
- React 控制台：事故、审批、Arena、评测、运行与设置。

### 安全

- Apache-2.0 许可证、SECURITY.md、CODE_OF_CONDUCT.md 与第三方许可证说明。

### 已知限制

- 默认面向本地单机演示，关闭鉴权并只绑定 `127.0.0.1`。
- GitLab 与 Kubernetes 适配器需要在目标环境中联调。
- 全量评测需要模型配额与 Docker 环境。
