# 第三方组件与许可证说明

本文件是信息性说明，不构成法律意见。TwinFix 本体以 **Apache-2.0** 发布；下列组件由各自的许可证授权，
其版权归各自作者所有。

## 服务端直接依赖

| 组件 | 许可证 |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic / pydantic-settings | MIT |
| SQLAlchemy / Alembic | MIT |
| psycopg / psycopg-pool | LGPL-3.0-only |
| LangGraph / langgraph-checkpoint-postgres | MIT |
| aio-pika | Apache-2.0 |
| redis-py | MIT |
| HTTPX / sse-starlette | BSD-3-Clause |
| PyJWT | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| markdown-it-py | MIT |
| docker-py | Apache-2.0 |
| PyYAML | MIT |
| tree-sitter 及 Java/Python/Go/TypeScript/JavaScript/C# 语法包 | MIT |
| OpenTelemetry SDK / OTLP HTTP Exporter | Apache-2.0 |
| pytest（dev / eval 可选） | MIT |

完整依赖树与精确版本见 `server/uv.lock`。

## 控制台

| 组件 | 许可证 |
|---|---|
| React / React DOM | MIT |
| Vite | MIT |
| TypeScript | Apache-2.0 |
| @vitejs/plugin-react | MIT |
| @types/* | MIT |

完整依赖树见 `web/package-lock.json`。

## 可选的基础设施镜像（不由本仓库分发）

`deploy/compose.dev.yml` 与 `deploy/observability/compose.observability.yml` 只引用镜像，用户在本地拉取：

| 镜像 | 许可证 / 说明 |
|---|---|
| PostgreSQL / pgvector | PostgreSQL License |
| Redis 7.4 | RSALv2 / SSPLv1（非 OSI；建议换 `valkey/valkey:8` 或 Redis 8） |
| RabbitMQ | MPL-2.0 |
| Prometheus | Apache-2.0 |
| Elasticsearch / Filebeat 8.15 | Elastic License 2.0 / SSPLv1（非 OSI，仅本机演示） |
| Apache SkyWalking OAP | Apache-2.0 |
| Jaeger | Apache-2.0 |
| Grafana | AGPL-3.0（独立容器使用） |
| Maven / Temurin / Go / Node / .NET SDK / Python 基础镜像 | 各自上游许可证（Apache-2.0、GPL+CE、BSD-3、MIT、PSF-2.0 等） |

## 基准测试 demo

`arena/demo-shop`、`demo-python`、`demo-go`、`demo-ts`、`demo-cs` 中声明的第三方依赖见各自的
`pom.xml` / `package.json` / `go.mod` / `*.csproj` / 依赖清单；均为公开仓库中的常见开源组件。

## 注意事项

- **psycopg / psycopg-pool 为 LGPL-3.0**：本项目以未修改的库形式使用。若对外分发预构建镜像，
  请保留许可证文本并确保用户可以替换该库。
- **Redis 7.4 不是 OSI 开源许可证**。仅使用源码构建时不受影响；如希望默认栈全部为 OSI 许可证，
  可改用 Valkey 8（BSD-3-Clause）。
- **Elasticsearch / Filebeat 为 source-available（非 OSI）**，只用于本机演示，不属于核心依赖。
- 如果新增依赖或静态资产，请同步更新本文件。
