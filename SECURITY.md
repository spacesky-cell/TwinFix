# 安全策略

## 支持版本

当前开发版本为 `0.1.x`。安全修复只保证进入最新 `main`，不维护旧版本分支。

## 报告漏洞

请通过 GitHub 的 **Security → Report a vulnerability**（Security Advisories）私下报告，**不要开公开 Issue**。

报告中请尽量包含：

- 受影响的版本 / commit
- 复现步骤或最小 PoC
- 影响范围（沙箱逃逸、凭据泄露、权限绕过、提示词注入绕过、拒绝服务等）
- 你认为的修复方向（可选）

我们会在 7 天内确认收到，并在修复后与你协调披露时间。

## 安全边界与部署前提

- 默认 `TWINFIX_AUTH_DISABLED=true` **只用于本机开发**；所有 compose 端口绑定 `127.0.0.1`。
  对外暴露前必须设置 `TWINFIX_AUTH_DISABLED=false`、≥32 字节的 `TWINFIX_JWT_SECRET`，并创建管理员账号。
- 不要把 `deploy/.env`、`server/.env`、GitHub App 私钥、GitLab/Jenkins token、LLM API Key 提交到仓库或 Issue。
- 可观测栈会挂载 Docker socket、关闭 Elasticsearch 安全特性，**只用于本机演示**，不要对外暴露。
- 生产凭据不要注入沙箱或孪生环境；沙箱不应挂载宿主机目录或 Docker socket。
- 所有日志、Issue、代码与工具输出进入模型前都按不可信数据处理；发现围栏/注入探测可被绕过，请按漏洞报告。

## 不在范围内

- 依赖组件自身的已知漏洞（请同时报告给上游项目）
- 需要宿主机 root 或物理访问的攻击
- 在未启用鉴权的情况下直接把服务暴露到公网（这属于错误部署，不是产品漏洞）
- 社会工程、钓鱼或对维护者个人的攻击

## 安全相关配置

| 配置 | 说明 |
|---|---|
| `TWINFIX_AUTH_DISABLED` | 仅本地开发可设为 `true` |
| `TWINFIX_JWT_SECRET` | 开启鉴权时必填，≥32 字节随机值 |
| `TWINFIX_METRICS_TOKEN` | 对外提供 `/api/metrics` 时配置 |
| `TWINFIX_OTEL_*` | 追踪导出，仅发送标识与用量，不含 Prompt/源码/工具参数 |
| `deploy/observability/` | 仅本机演示；包含 Docker socket 与非 OSI 组件 |
