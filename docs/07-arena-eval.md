# 07 基准测试与评测

## 1. 目标

1. 让任何人 `docker compose up` 后，能亲眼看到 TwinFix 发现并修好一个 bug。
2. 提供带标准答案的故障用例库，把"效果好不好"变成数字。

## 2. 示例服务

### 2.1 demo-shop（Java）

Spring Boot 3 多模块电商系统，故意保持真实的复杂度：

| 服务 | 依赖 |
|---|---|
| gateway | Spring Cloud Gateway |
| order | PostgreSQL（MyBatis-Plus）、Redis、RabbitMQ 生产者 |
| inventory | PostgreSQL、Redis（库存缓存）、RabbitMQ 消费者 |
| payment | 调用 mock 第三方支付（WireMock） |

配套：Prometheus + Grafana、Elasticsearch + Filebeat、SkyWalking、k6 流量脚本。

### 2.2 demo-python（Python）

`arena/demo-python` 是一个纯标准库的订单服务，用来验证 PythonProfile 与 Python 代码智能的端到端链路：

- `orders/`：`OrderService` / `PriceCalculator` / `Cart` / `Inventory`，`orders/app.py` 是标准库 HTTP 服务；
- 无第三方运行时依赖，孪生镜像基于 `python:3.12-slim` 构建，可离线完成；
- 孪生模板在仓库内（`twin/`），由用例的 `project_config.twin.directory` 指定；
- 判分用 pytest：隐藏测试拷进 `<module>/tests/`，在仓库根执行。

### 2.3 demo-go（Go）

`arena/demo-go` 是一个纯标准库的 Go 订单服务，用来验证 GoProfile、Go 代码智能与孪生构建：

- `orders/`：`OrderService` / `PriceCalculator` / `Cart` / `Inventory`，`orders/app.go` 是 `net/http` 服务，
  `cmd/orders/main.go` 是入口，金额统一用整数分（`Money int64`）；
- 无第三方依赖：`go build ./...` 与 `go test ./...` 可离线执行；
- 孪生镜像多阶段构建（`golang:1.25-bookworm` 编译 → `scratch` 运行），健康检查用二进制自带的
  `-healthcheck`，不依赖 shell/curl；模板在仓库内 `twin/`；
- 判分用 `go test`：隐藏测试是 `hidden/*_test.go`，拷进 `<module>/` 后按包执行；
- 本机没有 Go 时可用 `--go-image twinfix-sandbox-go:1.25` 通过 docker 判分。

### 2.4 demo-ts（TypeScript）

`arena/demo-ts` 是一个纯标准库的 TypeScript 订单服务，用来验证 TypeScriptProfile、TS 代码智能与孪生构建：

- `orders/`：`OrderService` / `PriceCalculator` / `Cart` / `Inventory`，`orders/app.ts` 是 `node:http` 服务；
  金额统一用整数分（`number`）；
- Node 22+ 直接用类型剥离执行 `.ts`，没有 `tsc`、没有第三方依赖、不需要 `npm install`；
- 孪生镜像基于 `node:22-bookworm-slim`，健康检查用 Node 自带的 `fetch`，模板在仓库内 `twin/`；
- 判分用 `node --test`：隐藏测试是 `hidden/*.test.ts`，拷进 `<module>/tests/` 后执行；
  本机没有 Node 22+ 时可用 `--node-image twinfix-sandbox-typescript:22` 通过 docker 判分。

### 2.5 demo-cs（C# / .NET）

`arena/demo-cs` 是一个纯标准库的 C# 订单服务，用来验证 CSharpProfile、C# 代码智能与孪生构建：

- `orders/`：`OrderService` / `PriceCalculator` / `Cart` / `Inventory`，`orders/App.cs` 是
  `System.Net.HttpListener` 服务，金额统一用 `decimal` 分；
- `orders.Tests/`：控制台测试工程 + 极简反射 harness（执行 `public static void Test*`，写 JUnit XML），
  无 xUnit/NUnit/MSTest 依赖，离线可直接 `dotnet run`；
- 孪生镜像多阶段构建（`mcr.microsoft.com/dotnet/sdk:10.0` 编译 → `dotnet/runtime:10.0` 运行），
  健康检查用应用自带的 `--healthcheck`，模板在仓库内 `twin/`；
- 判分用 `dotnet run --project <module>.Tests`：隐藏测试是 `hidden/*.cs`，拷进测试工程目录后编译执行；
  本机没有 .NET SDK 时可用 `--csharp-image twinfix-sandbox-csharp:10.0` 通过 docker 判分。

## 3. 故障注入方式

| 类型 | 方式 |
|---|---|
| 代码缺陷 | 每个用例是一个 git patch，注入 = 在示例服务仓库上应用 patch 并部署；标准答案 = 反向 patch 的关键位置 |
| 运行时/环境 | 注入器调用 API：改配置、限流、kill 消费者、塞大 key、调慢依赖（Toxiproxy） |

TwinFix 的核心是"修代码"，当前用例以代码缺陷为主，运行时类用于考察诊断能力（期望结论为 NEEDS_HUMAN 或给出运维建议，而非改代码）。

## 4. 故障清单（40 例）

当前共 40 例：Java C001–C024、Python P001–P004、Go G001–G004、TypeScript T001–T004、C# S001–S004。下表是 Java 的 24 例（C001–C024），其他语言的用例见 §4.1–§4.4。

| # | 类别 | 故障 | 期望结果 |
|---|---|---|---|
| 1 | 空指针 | 优惠券为空时计算订单金额 NPE | 修复 |
| 2 | 边界 | 分页参数 page=0 越界 | 修复 |
| 3 | 精度 | 金额用 double 累加导致对账差 1 分 | 修复 |
| 4 | 并发 | 库存扣减先查后改，超卖 | 修复（乐观锁/原子扣减） |
| 5 | 事务 | `@Transactional` 自调用失效，部分写入 | 修复 |
| 6 | 事务 | 异常被 catch 吞掉，事务未回滚 | 修复 |
| 7 | 连接池 | 手动获取连接未关闭，连接池耗尽 | 修复 |
| 8 | 慢 SQL | 查询缺少索引条件导致全表扫描 | 修复（改查询）+ 建议加索引 |
| 9 | N+1 | 订单列表循环查明细 | 修复 |
| 10 | 缓存 | 更新 DB 后未删缓存，读到旧库存 | 修复 |
| 11 | 缓存 | 缓存穿透：不存在的 key 打穿 DB | 修复（空值缓存） |
| 12 | 缓存 | 序列化不兼容导致反序列化异常 | 修复 |
| 13 | MQ | 消费者异常未处理，消息无限重投 | 修复（重试上限 + 死信） |
| 14 | MQ | 消费非幂等，重复消息重复扣库存 | 修复 |
| 15 | 线程池 | 无界队列线程池导致内存上涨 | 修复 |
| 16 | 死锁 | 两把锁加锁顺序相反 | 修复 |
| 17 | 内存 | 静态 Map 缓存只增不减 | 修复 |
| 18 | 时间 | 时区错误导致订单跨天统计错误 | 修复 |
| 19 | 序列化 | Jackson 日期格式不一致，接口 400 | 修复 |
| 20 | 超时 | 调用支付无超时，线程被占满 | 修复（超时 + 降级） |
| 21 | 配置 | 环境配置缺失键导致启动失败 | 诊断 → NEEDS_HUMAN（配置受保护） |
| 22 | 依赖 | 下游服务宕机 | 诊断 → NOT_BUG/运维建议 |
| 23 | 业务异常 | 余额不足的正常 BizException 被大量上报 | NOT_BUG |
| 24 | 已修复 | 注入后 dev 上已被人修掉 | ALREADY_FIXED |

### 4.1 Python 用例

| # | 类别 | 故障 | 期望结果 |
|---|---|---|---|
| P001 | 空指针 | 无券下单 `AttributeError`：判空写在解引用之后 | 修复 |
| P002 | 共享状态 | `Cart` 可变默认参数导致连续订单串行累加 | 修复 |
| P003 | 业务异常 | 库存不足的正常 409 被当成故障上报 | NOT_BUG |
| P004 | 配置 | 必填环境变量 `ORDERS_INVENTORY_TOKEN` 未下发，启动失败 | NEEDS_HUMAN（配置受保护） |

### 4.2 Go 用例

| # | 类别 | 故障 | 期望结果 |
|---|---|---|---|
| G001 | 空指针 | 无券下单 nil pointer panic：判空写在解引用之后 | 修复 |
| G002 | 共享状态 | `OrderService` 缓存最近优惠券，第二笔订单沿用上一笔的优惠 | 修复 |
| G003 | 业务异常 | 库存不足的正常 409 被当成故障上报 | NOT_BUG |
| G004 | 配置 | 必填环境变量 `ORDERS_INVENTORY_TOKEN` 未下发，启动 panic | NEEDS_HUMAN（配置受保护） |

### 4.3 TypeScript 用例

| # | 类别 | 故障 | 期望结果 |
|---|---|---|---|
| T001 | 空指针 | 无券下单 `TypeError`：判空写在解引用之后 | 修复 |
| T002 | 共享状态 | `OrderService` 缓存最近优惠券，第二笔订单沿用上一笔的优惠 | 修复 |
| T003 | 业务异常 | 库存不足的正常 409 被当成故障上报 | NOT_BUG |
| T004 | 配置 | 必填环境变量 `ORDERS_INVENTORY_TOKEN` 未下发，启动失败 | NEEDS_HUMAN（配置受保护） |

### 4.4 C# 用例

| # | 类别 | 故障 | 期望结果 |
|---|---|---|---|
| S001 | 空指针 | 无券下单 `NullReferenceException`：判空写在解引用之后 | 修复 |
| S002 | 共享状态 | `OrderService` 缓存最近优惠券，第二笔订单沿用上一笔的优惠 | 修复 |
| S003 | 业务异常 | 库存不足的正常 409 被当成故障上报 | NOT_BUG |
| S004 | 配置 | 必填环境变量 `ORDERS_INVENTORY_TOKEN` 未下发，启动失败 | NEEDS_HUMAN（配置受保护） |

用例格式 `arena/cases/<id>/case.yaml`：

```yaml
id: C004
title: 库存扣减超卖
inject: { type: patch, file: inject.patch, services: [inventory] }
trigger: { k6: oversell.js }
expect:
  outcome: fixed
  root_cause_locations: ["inventory/.../StockService.java#deduct"]
  root_cause_keywords: ["并发", "超卖", "原子"]
hidden_tests: [hidden/StockConcurrencyTest.java]   # TwinFix 看不到，用于判分
```

多语言字段（都可省略，默认 Java）：

- `language: java | python | go | typescript | csharp`：决定判分运行器
  （Maven / pytest / go test / node --test / dotnet run）、语言插件与沙箱镜像；
- `seed: demo-shop`：基线仓库目录，相对 `arena/`；Python / Go / TypeScript / C# 用例分别为
  `demo-python` / `demo-go` / `demo-ts` / `demo-cs`；
- `project_config`：注入时写入 `Project.config`，例如 `twin.directory`（孪生模板）、
  `project_packages`（代码索引的包名）、`protected_paths`（受保护文件）。

Python 隐藏测试直接放在 `hidden/test_*.py`，判分时拷到 `<module>/tests/` 后用 pytest 选择器执行；
Go 隐藏测试放在 `hidden/*_test.go`，判分时拷到 `<module>/`（必须与被测包同目录）后执行 `go test`；
TypeScript 隐藏测试放在 `hidden/*.test.ts`，判分时拷到 `<module>/tests/` 后用 `node --test` 执行；
C# 隐藏测试放在 `hidden/*.cs`，判分时拷到 `<module>.Tests/` 测试工程后随工程一起编译执行。

`hidden_tests` 是防止"只让自己写的复现用例通过"的关键：判分用的测试 TwinFix 无法读取。

每个用例目录还带一份 `solution.patch`（参考修复，基于注入后的代码）。`twinfix-arena validate` 在干净副本上逐条校验，CI 的 `arena` 任务每次提交都跑：

1. `inject.patch` 能应用，且注入服务自己的测试仍全绿（bug 潜伏，CI 拦不住）；
2. 隐藏测试在注入后的代码上失败（真的能抓住 bug）；
3. 叠加 `solution.patch` 后隐藏测试与模块全量测试通过（用例可解，判分不会冤枉正确修复）。

制作 patch 的方式：在种子仓库（demo-shop / demo-python / demo-go / demo-ts / demo-cs）的临时 git 副本里改代码，`git diff` 导出；不要手写 hunk。需要新代码面的 bug（分页、缓存等）由 patch 同时引入"功能 + bug"，模拟一次真实的功能合入。

### 4.5 评测运行

```bash
cd server
uv run twinfix-arena validate                      # 用例自检，不需要 TwinFix 服务
uv run twinfix-arena validate --repeat 3           # 稳定性自检：同一用例跑 3 次，结果不一致标记 FLAKY
uv run twinfix-arena eval --api http://127.0.0.1:8000/api --git-root .data/git --label gpt-4o-mini
uv run twinfix-arena eval c001 c002 --effort low --model claude-sonnet-5-5   # 只跑指定用例，覆盖模型/推理强度
uv run twinfix-arena eval --suite python --label python-fast                  # Python 四例回归
uv run twinfix-arena --go-image twinfix-sandbox-go:1.25 eval --suite go       # Go 四例（本机无 Go 时用 docker 判分）
uv run twinfix-arena eval --suite typescript --label ts-fast                  # TypeScript 四例回归
uv run twinfix-arena --node-image twinfix-sandbox-typescript:22 eval --suite typescript  # 本机无 Node 22+ 时
uv run twinfix-arena eval --suite csharp --label cs-fast                      # C# 四例回归
uv run twinfix-arena --csharp-image twinfix-sandbox-csharp:10.0 eval --suite csharp  # 本机无 .NET SDK 时
```

也可以在控制台"评测"页发起：`POST /api/eval/runs` 用同一套 CLI 参数拉起独立子进程，状态、耗时和日志在页面上可见，
结束后报告自动出现在对比表里。同一时间只允许一个任务；不满足 `agent_mode=llm` / `codehost=local` / 种子仓库
或用例 id 非法时直接 409。`--effort` 把 `reasoning_effort` 写进各用例项目的 agent-config，空 = 沿用全局默认，
报告 meta 里会记下本次推理强度，便于横向对比。

控制台发起的评测在 API 容器里判分，所以 `server/Dockerfile` 自带 JDK 21 + Maven（demo-shop 编译目标是
Java 21）、Go 1.25 工具链、Node 22 与 .NET SDK 10，并通过 `uv sync --extra eval` 安装 pytest（Python 用例判分用）；`deploy/compose.dev.yml`
通过 `TWINFIX_EVAL_MAVEN_SETTINGS` / `TWINFIX_EVAL_MAVEN_REPO` 把镜像源 settings 和可写的本地仓库
（`/data/m2/repository`）传进去。发起前会按所选用例的语言检查 `mvn` / pytest / `go` / `node` 是否存在，
缺依赖直接 409，避免每个用例跑完全流程后才在判分阶段报 ERROR。宿主机直接跑 CLI 时这两个变量留空即可，
`--python` 可指定判分解释器（默认当前解释器），`--go-image` / `--node-image` 可在没有 Go / Node 的宿主机上
用 docker 镜像判分（控制台可用 `TWINFIX_EVAL_GO_IMAGE` / `TWINFIX_EVAL_NODE_IMAGE` 配置）。

真实闭环需要的本机准备（宿主机运行 api/worker，配置见 `deploy/.env.example` 末尾）：

```bash
docker compose -f deploy/compose.dev.yml up -d postgres redis rabbitmq
# 预热沙箱的只读依赖卷（沙箱默认无网络，离线构建）
docker run --rm -v "$PWD/arena/demo-shop:/src:ro" -v twinfix-m2:/deps -v "$PWD/deploy/maven/settings.xml:/s.xml:ro" \
  maven:3.9.9-eclipse-temurin-21 bash -c 'cp -r /src /w && cd /w && mvn -B -q -s /s.xml -Dmaven.repo.local=/deps/repository package dependency:go-offline && chmod -R a+rX /deps'
cd server && uv run alembic upgrade head
uv run uvicorn twinfix.api.app:create_app --factory --loop twinfix.db:selector_loop --port 8000 &
uv run twinfix-worker &
```

`eval` 对每个用例：在共享 `git_root` 下新建裸仓库 `arena/<case>-<run>`（main/dev 两条分支），把故障注入 dev，注册独立项目，按 `signal` 上报告警；轮询直到事故到达评审/人工/终态；取修复分支（或最后一次补丁）装入隐藏测试判分，再取消停留的事故。TwinFix 需以 `codehost=local` 运行并与运行器共享同一个 `git_root`。报告写到 `eval/reports/<date>-<run>.md`。

评测项目注册时带 `config.observe_s=0`：判分看的是"修复是否合并进分支"，不需要合并后再盯观察期；否则每个走到合并的用例都要多等一轮观察期，还会占着孪生名额。合并之后的 `OBSERVING` / `CLOSED` 同样算"修复交付"（`grading._FIXED_STATES`），状态机继续往后走不影响判对。

`--timeout` 是单个用例等待收敛的秒数，默认 **3600**（对齐 `state_timeout_s`）。low 推理 1800 一般够，high 推理建议 3600；超时用例的备注会提示调大。等待期间每 60s 打一行 `状态 / 已耗时 / token`，日志重定向到文件也能看出卡在哪一步。单个 Run 的 token 在**每次 LLM 调用后增量落库**，所以被超时/取消的用例也不会在报告里显示 0 token。

## 5. 评测指标

| 指标 | 定义 |
|---|---|
| 结论准确率 | 最终 outcome 与期望一致的比例 |
| 根因命中率 Top-1 / Top-3 | 诊断给出的可疑位置命中标准答案 |
| 复现率 | 能产出稳定失败用例的比例 |
| 修复率 | 隐藏测试 + 回归全部通过的比例 |
| 误修率 | 修复"通过验证"但隐藏测试失败的比例（最重要的安全指标） |
| 端到端耗时 | 信号到 AWAITING_REVIEW；超时用例按实际等待时长计入（报告里不会显示 "-"） |
| 单事故成本 | token 与金额 |
| 人工介入率 | 进入 NEEDS_HUMAN 的比例 |

## 6. 评测方法

评测在独立项目上端到端运行：每个用例注入到示例服务的一个临时仓库，TwinFix 从告警开始走完整链路，最后由隐藏测试判分。同一份代码、同一份用例集、同一套沙箱条件下，可以横向比较模型或运行时开关。

### 6.1 对照实验开关

以下开关通过环境变量或 `server/.env` 配置，**重启 worker 生效**（API 不需要重启）。关闭某个开关后，可以观察对应机制对结论准确率、误修率与成本的影响。

| 变量 | 关掉之后 | 观察点 |
|---|---|---|
| `TWINFIX_REPRO_REQUIRED=false` | REPRODUCING 不跑 Reproducer，诊断后直接修复；Verifier 改为对照诊断根因复核 | “没有复现就不能修复”这条约束的作用 |
| `TWINFIX_REPRO_CONFIRM=false` | 采信 Agent 自报的 `failing_runs`，系统不在沙箱里重跑 | 系统侧复现确认能挡住多少“假复现” |
| `TWINFIX_INTEGRITY_CHECK=false` | 补丁删/跳过/弱化测试不再判验证失败 | 完整性扫描对误修率的影响 |
| `TWINFIX_PRECEDENTS_ENABLED=false` | 诊断不注入同项目历史判例 | 判例检索的收益 |
| `TWINFIX_LOG_CLUSTER=false` | `search_logs` 行数多时不再给模板摘要，直接回贴原始行 | 日志模板聚类节省的 token |

“先复现”是状态机的硬约束（`DIAGNOSING → FIXING` 非法，见 `domain/state_machine.py`）。`repro_required=false` 只改变复现阶段做什么（照常创建工作区与孪生环境，记录 `repro_skipped` 事件），不改变状态图。

对比多份报告时用 `twinfix-arena compare`，它只比较各报告共有的用例，用例集不同也不会失真：

```bash
cd server
uv run twinfix-arena compare ../eval/reports/a.json ../eval/reports/b.json
```

模型对比通过 `--model` 写入各用例项目的 agent-config，API / worker 不需要重启；只有模型不同、其余条件一致时，数字才可比。

### 6.2 快速回归套件

全量 Java 用例一轮耗时较长，不适合每次改动前运行。`twinfix-arena` 从用例库中固定了六个套件（`server/twinfix/arena/cli.py` 的 `SUITES`）：

| 套件 | 用例 | 覆盖 |
|---|---|---|
| `fast` | C001、C002、C011、C014、C018、C024 | 修复 ×3（NPE / 分页 / Integer 比较）、NEEDS_HUMAN、MQ 幂等、NOT_BUG |
| `core` | `fast` + C009、C012 | 再补 ALREADY_FIXED 与缓存失效 |
| `python` | P001–P004 | 修复 ×2、NOT_BUG、NEEDS_HUMAN |
| `go` | G001–G004 | 修复 ×2、NOT_BUG、NEEDS_HUMAN |
| `typescript` | T001–T004 | 修复 ×2、NOT_BUG、NEEDS_HUMAN |
| `csharp` | S001–S004 | 修复 ×2、NOT_BUG、NEEDS_HUMAN |

跨轮稳定性不足的用例（C003、C004、C007、C010、C016）不放进套件；C009 稳定但单例耗时较长，只放在 `core`。套件结果是回归信号，不代表全量评测水平。

```bash
cd server
uv run twinfix-arena eval --suite fast --label "fast-$(date +%F)" --timeout 3600
uv run twinfix-arena gate eval/reports/<report>.json
uv run twinfix-arena eval --suite python
uv run twinfix-arena eval --suite go          # 本机无 Go 时加 --go-image
uv run twinfix-arena eval --suite typescript  # 本机无 Node 22+ 时加 --node-image
uv run twinfix-arena eval --suite csharp      # 本机无 .NET SDK 时加 --csharp-image
```

`gate` 默认阈值：结论准确率 ≥ 0.83、误修率 = 0、人工介入率 ≤ 1.0；`--max-avg-duration` 可加平均耗时上限。阈值可按套件与模型调整，但误修率默认必须为 0：误修比漏修更危险，回归时应先拦住。

控制台 **评测** 页提供与 `--suite` 等价的预设按钮。GitHub 托管 runner 没有孪生环境与 LLM Key，仓库 CI 只跑 `twinfix-arena validate`；需要完整回归时，在具备 Docker、LLM 与 Maven 缓存的机器或 self-hosted runner 上运行。

### 6.3 语言套件参考结果

以下为各语言 4 例套件的单轮参考结果。报告文件与运行参数保存在 `eval/reports/`，可以用 `twinfix-arena gate` 重新校验。

#### Python（P001–P004）

| 指标 | 值 |
|---|---|
| 结论准确率 | 4/4（100%） |
| 根因命中率 Top-1 / Top-3 | 100% / 100% |
| 复现率 | 100%（两个修复用例） |
| 修复率（隐藏测试 + 回归） | 100% |
| 误修率 | 0% |
| 人工介入率 | 25%（P004，期望 NEEDS_HUMAN） |
| 平均耗时 / token | 750s / 1.05M |

#### Go（G001–G004）

| 指标 | 值 |
|---|---|
| 结论准确率 | 4/4（100%） |
| 根因命中率 Top-1 / Top-3 | 100% / 100%（3 个判分用例） |
| 复现率 | 100%（两个修复用例） |
| 修复率（隐藏测试 + 回归） | 100% |
| 误修率 | 0% |
| 人工介入率 | 25%（G004，期望 NEEDS_HUMAN） |
| 平均耗时 / token | 1236s / 854K |

#### TypeScript（T001–T004）

| 指标 | 值 |
|---|---|
| 结论准确率 | 4/4（100%） |
| 根因命中率 Top-1 / Top-3 | 100% / 100%（3 个判分用例） |
| 复现率 | 100%（两个修复用例） |
| 修复率（隐藏测试 + 回归） | 100% |
| 误修率 | 0% |
| 人工介入率 | 25%（T004，期望 NEEDS_HUMAN） |
| 平均耗时 / token | 552s / 708K |

#### C#（S001–S004）

| 指标 | 值 |
|---|---|
| 结论准确率 | 4/4（100%） |
| 根因命中率 Top-1 / Top-3 | 100% / 100%（3 个判分用例） |
| 复现率 | 100%（两个修复用例） |
| 修复率（隐藏测试 + 回归） | 100% |
| 误修率 | 0% |
| 人工介入率 | 25%（S004，期望 NEEDS_HUMAN） |
| 平均耗时 / token | 604s / 884K |

Java 24 例的历史评测报告同样保存在 `eval/reports/`；由于部分用例跨轮稳定性不足，结果以报告中的逐例数据为准。
