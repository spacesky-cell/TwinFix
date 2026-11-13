# 01 概述

## 1. 要解决的问题

线上或测试环境出 bug 后，开发要经历：看告警 → 翻日志 → 查链路 → 定位代码 → 本地复现 → 修改 → 自测 → 提交 → 部署验证。前半段（诊断）耗时且依赖经验，后半段（修复验证）重复度高。

现有工具的空白：
- AI SRE 类（HolmesGPT、K8sGPT）偏 K8s 基础设施层，只诊断不修代码。
- 自动修复类（Sentry Seer 等）能出 PR，但改动没有在真实运行环境里被验证过。

TwinFix 的定位：**诊断 + 在隔离的孪生环境里真实部署、复现、修复、验证**，交给人的是"已在真实环境跑通的修复"，而不只是一段 diff。

## 2. 目标与非目标

目标
- 支持 Java（Spring Boot，Maven/Gradle）、Python（pytest）、Go（go build/go test）、TypeScript / JavaScript
  （node --test）与 C# / .NET（dotnet build / dotnet run 测试工程）项目；部署形态默认为虚拟机 + Docker Compose，也可选用 Kubernetes；
  基准测试套件包含 Java 的 demo-shop、Python 的 demo-python、Go 的 demo-go、TypeScript 的 demo-ts 与 C# 的 demo-cs。
- 完整闭环：发现 → 分诊 → 诊断 → 复现 → 修复 → 验证 → 报告 → 人工复测 → 合并。
- 对接 GitHub / GitLab（代码 / Issue / PR（MR）/ Check）与 Jenkins / GitLab CI（孪生环境部署）。
- 自带基准测试套件与评测基准，结论可量化。

非目标
- 不直接操作生产环境：不在生产热修复、不改生产配置、不写生产数据库。
- 不把 K8s 作为默认部署形态：K8s 以可选 TwinProvider 提供，需要目标集群。
- 不做通用监控大屏（交给 Grafana）。

## 3. 核心流程

```
bug 信号 ──► 分诊 ──► 诊断 ──► 拉起孪生环境 ──► 复现 ──► 修复 ──► 验证 ──► 报告 ──► 人工复测 ──► 合并
   │          │        │                       │        │        │
   │       非bug/重复   │                    无法复现   无法修复  验证失败(重试≤N)
   │          ▼        ▼                       ▼        ▼        ▼
   └────── 终止并通知（每种终止都有对应报告类型）
```

关键原则
1. 先复现，后修复：没有失败用例就不改代码。
2. 验证独立：修复 Agent 不能自己判定自己通过。
3. 结果绑定提交：验证结论绑定 commit SHA，代码变了就重验。
4. 人是最后一道门：合并到 main / release 必须人工；dev 可在人点确认后自动合并。

## 4. bug 来源

| 来源 | 接入方式 |
|---|---|
| 指标告警 | Alertmanager / Grafana Webhook |
| 日志异常 | worker 定时扫描 ES 中的 ERROR / 未捕获异常，按异常指纹聚成信号 |
| 前端报错 | 前端上报接口错误并携带 traceId（只接收，不做 SDK） |
| 人工提交 | GitHub Issue 打 `twinfix` 标签；Web 控制台手动创建 |
| IM | 飞书 / 钉钉机器人 @ 提交（预留接口，当前实现飞书） |

## 5. 术语

| 术语 | 含义 |
|---|---|
| Signal | 一条原始 bug 信号（一次告警、一条异常日志、一个 Issue） |
| Incident | 分诊后形成的事故，多个 Signal 通过指纹聚合到同一 Incident |
| Fingerprint | 去重指纹：异常类型 + 归一化后的关键栈帧 + 服务名 |
| Twin | 为单个 Incident 临时拉起的孪生环境，修复结束后销毁 |
| Workspace | 沙箱工作区：容器内的代码副本，Agent 在此改代码、编译、跑测试 |
| Repro | 复现产物：能稳定触发 bug 的测试用例或请求脚本 |
| Attempt | 一次修复尝试，一个 Incident 可有多次 Attempt |
| Run | 一次 Agent 执行（含全部工具调用），用于追踪与成本统计 |
| LanguageProfile | 语言插件，封装构建、测试、栈解析、探针等语言相关能力 |
| Arena | 基准测试套件：示例服务 + 故障注入器 + 标准答案 |
