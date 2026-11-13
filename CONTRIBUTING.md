# 贡献指南

感谢愿意一起改进 TwinFix。这个项目最看重三件事：**Agent 不碰生产**、**结论可复现**、**数字不注水**。提交前请确认改动没有破坏这三点。

## 先读什么

- [`README.md`](README.md)：项目做什么、快速开始、评测数据。
- [`docs/README.md`](docs/README.md)：设计文档地图。
- [`docs/02-architecture.md`](docs/02-architecture.md)：分层、五个 Agent、工具网关。
- [`docs/05-safety.md`](docs/05-safety.md)：权限分级与防作弊，改 Agent/工具前必读。
- [`docs/07-arena-eval.md`](docs/07-arena-eval.md)：基准测试、指标定义、评测方法。

## 环境

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12 | 服务端（uv 管理） |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.11 | 依赖与命令 |
| Node.js | 22 | 控制台；`twinfix-arena validate` 判分 demo-ts（TypeScript 用例） |
| .NET SDK | 10 | `twinfix-arena validate` 判分 demo-cs（C# 用例；无 SDK 时可用 `--csharp-image`） |
| Docker | 任意近期版本 | PostgreSQL / Redis / RabbitMQ、沙箱、孪生环境 |
| JDK + Maven | 21 | `twinfix-arena validate` 编译 demo-shop（Java 用例） |
| pytest | 随 `uv sync` 安装 | `twinfix-arena validate` 判分 demo-python（Python 用例） |
| Go | 1.25（CI 用 setup-go；无 Go 时可用 `--go-image`） | `twinfix-arena validate` 判分 demo-go（Go 用例） |

```bash
# 基础设施
docker compose -f deploy/compose.dev.yml up -d postgres redis rabbitmq

# 服务端
cd server
uv sync
uv run alembic upgrade head
uv run uvicorn twinfix.api.app:create_app --factory --loop twinfix.db:selector_loop --reload
uv run twinfix-worker        # 另一个终端

# 控制台（/api 代理到 127.0.0.1:8000）
cd web && npm install && npm run dev
```

整栈一键启动见 README「快速开始」。本地 LLM 配置放 `server/.env`（已被 gitignore）；不配也能用模拟 Agent（stub）走完整流程。

## 提交前检查

CI 会跑同样的命令，本地先过一遍：

```bash
cd server
uv run ruff check .
uv run ruff format --check .
uv run mypy twinfix
uv run pytest -q

cd ../web
npm run build

cd ../server
uv run twinfix-arena validate        # 改了 arena/、demo-shop 或任一示例服务时必跑
```

- 测试用 PostgreSQL：`TWINFIX_TEST_DATABASE_URL`（CI 里是 `.../twinfix_test`）。
- 没有真实 LLM key 时请用模拟（stub）模式跑测试/冒烟，不要在测试里发真实请求。
- 改了前端交互，附一张截图或 GIF 更好。

## 提交与 PR

- 提交信息用 `type(scope): 中文摘要`，例如 `fix(arena): 补充库存归还链路`。
  `type` 取 `feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `perf` / `style` / `build`。
- 一个 PR 做一件事；行为变化要同步文档（尤其是 `docs/07-arena-eval.md` 的数字和 `docs/08-roadmap.md` 的后续方向）。
- 评测数字必须来自 `eval/reports/` 中可追溯的报告文件，写清楚 run-id、模型与开关状态；**不要把某一轮的最好数字当成稳定结论**。跨轮不稳定的用例要在报告里点名。

## 添加一个故障用例（最常见、也最容易踩坑）

目录：`arena/cases/<Cxxx>/`

```
case.yaml        # 元数据、触发请求、上报信号、期望结论
inject.patch     # 伪装成正常功能合入的 bug
solution.patch   # 参考修复
hidden/          # Agent 看不到的判分测试
```

`case.yaml` 的关键字段：`inject`、`trigger.requests`、`signal`（标题/服务/环境/栈）、
`expect.outcome`（`fixed` / `not_bug` / `already_fixed` / `needs_human` / …）、
`expect.root_cause_locations`、`expect.root_cause_keywords`、`hidden_tests`。

多语言用例额外字段（默认 Java，可省略）：

- `language: python` + `seed: demo-python`：判分改用 pytest，种子仓库取 `arena/demo-python`；
- `language: go` + `seed: demo-go`：判分改用 `go test`，种子仓库取 `arena/demo-go`；
- `language: typescript` + `seed: demo-ts`：判分改用 `node --test`（Node 22+），种子仓库取 `arena/demo-ts`；
- `language: csharp` + `seed: demo-cs`：判分改用 `dotnet run`（.NET SDK 10），种子仓库取 `arena/demo-cs`；
- `project_config`：写入 `Project.config`，例如 `twin.directory`（仓库内的孪生模板）、`project_packages`、
  `protected_paths`。Python 隐藏测试放 `hidden/test_*.py`，判分时拷进 `<module>/tests/`；
  Go 隐藏测试放 `hidden/*_test.go`（与被测包同目录），判分时拷进 `<module>/`；
  TypeScript 隐藏测试放 `hidden/*.test.ts`，判分时拷进 `<module>/tests/`；
  C# 隐藏测试放 `hidden/*.cs`，判分时拷进 `<module>.Tests/` 测试工程，用 `Test*` 静态方法与 `Check.*` 断言。

硬性要求：

1. **注入后项目仍能编译、现有 CI 仍绿**。bug 应该是"看起来合理的业务改动"带来的，而不是让构建失败。
2. **隐藏测试在修复前失败、参考修复后通过**，且测试文件不能被 Agent 在仓库里看到。
3. **`solution.patch` 能从当前 `dev` 分支干净应用**；隐藏测试要紧扣根因，防止"删功能/改断言"式的过度修复。
4. **patch 一律用 `git diff` 生成，不要手写 hunk**：
   ```bash
   # 在一个临时 clone 中改代码后
   git add -A
   git diff --cached > arena/cases/Cxxx/inject.patch
   ```
   `git diff` 不带 `--cached` 会漏掉新增文件；仓库统一 LF（见 `.gitattributes`）。
5. **改动 demo-shop / demo-python / demo-go / demo-ts 等示例服务会波及所有用例 patch 的上下文**。例如给接口加方法可能让它不再满足函数式接口：`InventoryGateway` 加 `release` 后，隐藏测试里的 `(sku, q) -> {}` 两参 lambda 全部失效，需要改成匿名类。改基线后必须全量重跑 `validate`。
6. 新增用例后跑 `uv run twinfix-arena validate Cxxx`，再跑一次全量，确认没有影响其它用例。
   怀疑用例本身抖动时跑 `uv run twinfix-arena validate --repeat 3`：任何一轮成功、又有一轮失败的用例
   会标成 `FLAKY`；先修用例，再拿它去评测。

一个用例的价值在于"**bug 潜伏得合理、判分抓得住根因**"，而不是难倒模型。参考 `C001` 的写法。

## 改动 Agent / 提示词 / 开关

- Agent 只交换结构化产物，不要让一个 Agent 直接改另一个 Agent 的内部状态。
- 所有工具调用必须经过 `gateway`；不要在 Agent 里直接调外部系统或 `subprocess`。
- 可开关的能力（复现确认、完整性扫描、日志聚类、判例检索）用环境变量控制，并同步到 `docs/07-arena-eval.md` §6.1 的开关表；默认值变化要跑全量评测后再合。
- 提示词改动可能显著改变成本与结论，PR 里附上小样本对比（至少覆盖成功、验证失败、NOT_BUG 三类用例）。

## 安全红线

- 不允许新增任何写入生产、绕过工具网关、放宽受保护路径的代码；安全必须在代码里强制，而不是写在 Prompt 里。
- Agent 只能推 `fix/twinfix-*` 分支；合并必须经过人工审批。
- 审计表只追加；不要为方便调试加删除/更新审计记录的接口。
- 隐藏测试、标准答案、`case.yaml` 的 `expect` 不得暴露给 Agent 的上下文。
- 报告中的"已修复"必须来自系统独立验证，不能采信 Agent 自报。

## 报告问题

提 Issue 时请附：复现步骤、`run-id` / 事故 ID、相关日志或事件流片段、期望与实际行为。涉及 LLM 的问题请注明模型与 `reasoning_effort`。安全问题请勿公开利用细节，先私下联系维护者。

## 许可证与安全

- 提交贡献即表示你同意该贡献以 [Apache-2.0](LICENSE) 授权。
- 新增依赖或静态资产前请确认许可证兼容，并同步更新 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- 安全漏洞请按 [`SECURITY.md`](SECURITY.md) 私下报告，不要提交公开 Issue。
