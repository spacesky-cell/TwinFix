# 04 接口契约

以 Python Protocol 描述，实际实现为 `abc.ABC` 或 `typing.Protocol`。所有方法均为 async。

## 1. 语言插件 LanguageProfile

```python
class LanguageProfile(Protocol):
    name: str                                    # "java" / "python" / "go" / "typescript" / "csharp"

    def detect(self, root_files: list[str]) -> bool: ...
    async def build(self, sandbox: SandboxProvider, ws: Workspace, module: str | None,
                    cwd: str | None = None) -> CommandResult: ...
    async def run_tests(self, sandbox: SandboxProvider, ws: Workspace,
                        selector: TestSelector, cwd: str | None = None) -> TestReport: ...
    def parse_stacktrace(self, text: str) -> list[StackFrame]: ...
    def frame_to_location(self, frame: StackFrame, files: list[str]) -> CodeLocation | None: ...
    def test_paths(self) -> list[str]: ...        # 用于识别"改了测试"
    def case_matches(self, selector: str, case_name: str) -> bool: ...
    def source_suffixes(self) -> tuple[str, ...]: ...  # 代码索引读取哪些源文件
    def repro_test_template(self) -> str: ...     # 复现用例模板
    def runtime_probe(self) -> RuntimeProbe | None: ...
```

`Project.language`（`java` / `python` / `go` / `typescript` / `csharp`）决定每次事故使用的插件：编排器在 `RunHandle.profile` 上携带选中的插件，
工具网关的构建/测试/栈解析以及代码索引都读它；多仓库附加仓库同样使用项目语言。未设置或与默认插件同名时
退回 `adapters.profile`，保持旧配置行为不变。

JavaProfile
- 构建：识别 `mvnw`/`pom.xml` 或 `gradlew`/`build.gradle`；默认 `-q -DskipTests` 编译，测试单独执行。
- 测试：`TestSelector` 支持全部 / 指定类 / 指定方法；解析 Surefire XML 输出为 `TestReport`。
- 栈解析：识别 `Caused by`、`... N more`；按项目包名前缀区分业务帧。
- 测试路径：`src/test/**`。
- 探针：`ArthasProbe`，命令白名单 `thread`、`thread -b`、`jvm`、`memory`、`dashboard -n 1`、`sc`、`sm`、`jad`、`trace`（限次数、限时长）、`watch`（同上）。
  访问方式：孪生镜像以 premain（`-javaagent`）挂载 Arthas，telnet 只绑容器内 127.0.0.1:3658；TwinFix 经 `docker exec` 用 `arthas-client` 执行一条命令（`TwinProvider.probe`），Agent 只能通过 `jvm_probe` 工具触达，且命令必须与白名单逐字相等（空白先归一化）。JRE 没有 `jdk.attach`，所以不能 attach 现成进程，只能 premain。

PythonProfile
- 构建：识别 `pyproject.toml` / `setup.py` / `requirements.txt` 等；`python -m compileall` 做语法检查。
- 测试：`python -m pytest`，选择器为 `tests/test_x.py::test_name`；写 `.twinfix/pytest.xml`（git 排除）并解析 JUnit。
- 栈解析：识别 `File "...", line N, in func` 与 pytest 的 `path.py:N: in func`；按 `project_packages` 区分业务帧。
- 测试路径：`**/tests/**`、`**/test_*.py`、`**/*_test.py`；无 Arthas 探针。
- 代码索引：tree-sitter Python 解析 class/function/method/变量、import、继承、显式类型注解与调用；
  `search_symbol` / `find_references` / `code_outline` 与 Java 共用同一套工具。
- 沙箱镜像：`deploy/sandbox/python.Dockerfile`（Python 3.12 + pytest）；项目依赖建议预装进派生镜像。
  可用 `project.config.python_executable` 覆盖解释器（默认 `python`）。

GoProfile
- 构建：识别 `go.mod` / `go.work`；`go build ./...`（或指定包）做编译检查，构建产物不落工作区。
- 测试：`go test -json -count=1`；选择器为 `./pkg::TestName` / 导入路径::TestName / 纯 `TestName`（也接受 `#`）。
  解析 `go test -json` 事件（子测试单独成例，包级构建失败记 `<build>` 错误），无 JSON 时回退 `--- FAIL:` 文本。
- 栈解析：识别 Go panic 的 `file.go:line +0x...` 帧与测试失败行；按 `project_packages`（模块/包路径）区分业务帧，
  标准库（`/usr/local/go/src`）与模块缓存（`/go/pkg/mod`）标记为非业务帧。
- 测试路径：`**/*_test.go`；无运行时探针。
- 代码索引：tree-sitter Go 解析 struct / interface / 其他类型、函数、方法（指针接收者归一）、字段、常量、变量；
  解析 import 别名、内嵌（`extends`）、参数/返回值/变量类型与调用；跨包解析保守处理，接口动态实现与歧义调用保留 unresolved。
- 沙箱镜像：`deploy/sandbox/go.Dockerfile`（Go 1.25，`GOTOOLCHAIN=local`，缓存写 `/tmp`）；
  可用 `project.config.sandbox_image` 覆盖语言默认镜像。

TypeScriptProfile（同时覆盖 JavaScript）
- 构建：识别 `package.json` / `tsconfig.json` / 各包管理器锁文件；不引入 `tsc`，用 `node --check` 逐文件做语法检查
  （Node 22+ 原生类型剥离；`.tsx` / `.jsx` 需要 JSX 转换，不在检查范围）。
- 测试：`node --test` + `--test-reporter=junit`，报告写 `.twinfix/node-tests.xml` 并复用 JUnit 解析；
  选择器为 `tests/x.test.ts::test name`（也接受单文件或只给测试名），多个名字映射为 `--test-name-pattern`。
- 栈解析：识别 `at Symbol (file:///app/orders/pricing.ts:22:15)` 与无符号帧；去掉 `file://` 前缀与 `async`/`new` 前缀，
  按 `project_packages` 与 `node_modules` 区分业务帧。
- 测试路径：`**/*.test.ts`、`**/*.spec.ts`、`**/tests/**`、`**/__tests__/**`；无运行时探针。
- 代码索引：tree-sitter TypeScript / JavaScript 解析 class / interface / type / enum / function / method / 字段 / 变量；
  相对 import（含 `index` 目录）、继承、`implements`、类型注解与构造/方法调用；跨文件解析保守处理，动态分派保留 unresolved。
- 沙箱镜像：`deploy/sandbox/typescript.Dockerfile`（Node 22，`node --test` 开箱可用）；
  可用 `project.config.sandbox_image` 覆盖语言默认镜像。

CSharpProfile
- 构建：识别 `*.csproj` / `*.sln` / `*.slnx` / `global.json` / `Directory.Build.props` / `nuget.config`；
  `dotnet build` 编译检查，产物用 `--artifacts-path /tmp/...` 重定向，不落工作区。
- 测试：约定测试是目录/工程名以 `.Tests` 结尾的控制台工程，工程内自带极简反射 harness（执行
  `public static void Test*` 并写 JUnit XML），`dotnet run` 执行；选择器为 `Namespace.Class::TestName`
  （也接受 `Class#Method`、类名或方法名）。零第三方依赖，离线可直接 restore/build。
- 栈解析：识别 `at Namespace.Type.Method(...) in /app/...cs:line N`（含无文件帧）；
  按 `project_packages`（命名空间）与 dotnet/NuGet 路径区分业务帧。
- 测试路径：`**/*Tests.cs`、`**/*Test.cs`、`**/*.Tests/**`、`**/tests/**`；无运行时探针。
- 代码索引：tree-sitter C# 解析 class / interface / struct / record / enum / delegate、方法、构造器、属性、
  字段、枚举成员；namespace/using（alias/static/global）、继承与接口实现、参数/返回/字段类型与调用；
  跨文件解析保守处理，动态分派保留 unresolved。
- 沙箱镜像：`deploy/sandbox/csharp.Dockerfile`（.NET SDK 10，NuGet 缓存写 `/tmp`）；
  可用 `project.config.sandbox_image` 覆盖语言默认镜像。原生支持仓库内的零依赖测试 harness；
  xUnit/NUnit 项目可用下面的自定义命令 + TRX 报告接入。

### 项目级自定义构建 / 测试命令

所有 Profile 都支持用 `project.config.commands` 覆盖内置命令（命令经 `sh -c` 执行，镜像需自带工具链）：

```json
{
  "commands": {
    "build": "npm ci --no-audit --no-fund && npm run build",
    "test": "npx vitest run --reporter=junit --outputFile=.twinfix/junit.xml",
    "test_selector": "npx vitest run --reporter=junit --outputFile=.twinfix/junit.xml {selector}",
    "report": ".twinfix/junit.xml",
    "report_format": "junit"
  }
}
```

| 字段 | 说明 |
|---|---|
| `build` / `test` | 完整命令；给了就替代该语言的默认构建/测试命令 |
| `test_selector` | 可选；选择器非空时用它替代 `test`，支持 `{selector}` / `{classes}` / `{methods}` 占位符 |
| `report` | JUnit/TRX/Jest-JSON 报告路径（相对工作区或 `cwd`）；缺省 `.twinfix/custom-tests.xml` |
| `report_format` | `junit`（默认）/ `trx`（dotnet test）/ `jest-json`（jest --json，vitest JSON 兼容） |
| `build_timeout_s` / `test_timeout_s` | 60–14400 秒，默认 900 / 1800 |

常见接入：
- TypeScript + jest：`test = "npx jest --ci --json --outputFile=.twinfix/jest.json"`，`report_format = "jest-json"`；
- TypeScript + tsc：`build = "npx tsc --noEmit"`；
- C# + xUnit/NUnit：`test = "dotnet test --logger 'trx;LogFileName=test.trx' --results-directory .twinfix"`，
  `report = ".twinfix/test.trx"`，`report_format = "trx"`；
- Java / Python / Go 也可用同一机制接 Gradle/Maven 之外的工具链。

配置非法（类型不对、命令为空、格式未知）时退回内置命令，不会让坏配置拖垮事故链。

## 2. 适配器

```python
class ObservabilityAdapter(Protocol):
    async def query_metrics(self, q: MetricQuery) -> MetricSeries: ...
    async def search_logs(self, q: LogQuery) -> LogPage: ...
    async def get_trace(self, trace_id: str) -> Trace: ...
    async def recent_deploys(self, service: str, since: datetime) -> list[Deploy]: ...

class CodeHost(Protocol):
    async def clone(self, repo: str, ref: str, dest: Path) -> None: ...
    async def push_branch(self, ws: Workspace, branch: str) -> str: ...      # 返回 sha
    async def open_pr(self, req: PRRequest) -> PRInfo: ...
    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None: ...
    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult: ...
    async def comment(self, target: IssueRef | PRRef, body: str) -> None: ...
    async def branch_head(self, repo: str, branch: str) -> str: ...

class CIProvider(Protocol):
    async def trigger(self, job: str, params: dict) -> BuildRef: ...
    async def status(self, ref: BuildRef) -> BuildStatus: ...
    async def logs(self, ref: BuildRef, tail: int = 500) -> str: ...

class TwinProvider(Protocol):
    async def create(self, spec: TwinSpec) -> Twin: ...       # 分支/sha、数据快照、端口段、TTL
    async def wait_ready(self, twin: Twin, timeout: int) -> bool: ...
    async def exec_request(self, twin: Twin, req: HttpRequest) -> HttpResponse: ...
    async def logs(self, twin: Twin, service: str, since: datetime) -> str: ...
    async def probe(self, twin: Twin, service: str, command: str, timeout_s: int) -> str: ...
    async def destroy(self, twin: Twin) -> None: ...

class SandboxProvider(Protocol):
    async def create_workspace(self, repo: str, sha: str, image: str) -> Workspace: ...
    async def exec(self, ws: Workspace, cmd: list[str], timeout: int) -> CommandResult: ...
    async def read_file / write_file / list_dir / search(...)
    async def read_sources(self, ws, suffixes, exclude_dirs, max_files, max_bytes) -> (files, truncated)
    async def diff(self, ws: Workspace) -> str: ...
    async def destroy(self, ws: Workspace) -> None: ...

class Notifier(Protocol):
    async def send(self, report: Report, recipients: list[Recipient]) -> SendResult: ...

class LLMClient(Protocol):
    async def chat(self, req: ChatRequest) -> ChatResponse: ...   # 含 tool calling、流式
```

v1 实现：`PrometheusAdapter`、`ElasticsearchLogAdapter`、`SkyWalkingAdapter`、`GitHubAppCodeHost`、
`GitLabCodeHost`、`JenkinsCI`、`GitLabCI`、`ComposeTwinProvider`（Jenkins 部署或直连 Docker API 二选一，可配置）、
`KubernetesTwinProvider`（kubectl + 独立 Namespace + NodePort，镜像需 CI 预构建）、
`DockerSandbox`、`EmailNotifier`、`FeishuNotifier`、`OpenAICompatibleLLM`。

`recent_deploys` 的数据源由 `TWINFIX_DEPLOY_QUERY` 配置（PromQL range query，`{service}` 会替换）：
`deploy_timestamp_seconds` 直接用样本值作为部署时间，`deploy_info` 用窗口内首个采样点近似；
未配置时返回空列表，诊断 Agent 仍可正常工作。

## 3. Agent 契约

```python
class Agent(Protocol):
    name: str
    tools: list[str]          # 允许调用的工具名，由网关强制
    async def run(self, ctx: AgentContext) -> AgentResult: ...

@dataclass
class AgentContext:
    incident: Incident
    evidence: EvidenceStore
    gateway: ToolGateway       # 唯一的工具入口
    budget: Budget             # token / 时间 / 工具调用次数
    feedback: str | None       # 上一轮失败原因

@dataclass
class AgentResult:
    outcome: str               # 例如 "diagnosed" / "cannot_reproduce" / "patch_ready"
    payload: BaseModel         # Diagnosis / Repro / Patch / Verification
    next_hint: str | None
```

外部文本（事故标题、signal 原文、Issue 正文、代码/注释、工具结果、上游 Agent 产物）进入模型前，
由 `agents/untrusted.py` 统一包进带随机 nonce 的 `[[UNTRUSTED ...]]` 围栏；系统提示声明围栏内只是数据，
不是指令。`AgentContext.feedback` 作为修正线索单独标注，其中引用的外部文本仍按数据处理。

## 4. 工具网关

```python
class ToolGateway:
    async def call(self, run: Run, tool: str, args: dict) -> ToolResult: ...
```

执行顺序：校验 Agent 是否被授权该工具 → 参数 schema 校验 → 策略检查（级别、路径、命令白名单、频率）→ 需审批则挂起并创建 Approval → 脱敏参数写审计 → 执行 → 结果截断/存为 Evidence → 记录耗时与成本。

## 5. 对外 HTTP API（v1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 用户名/邮箱 + 密码换 JWT（12h） |
| GET | `/api/auth/me` | 当前用户与角色；开启鉴权且未登录返回 401 |
| GET | `/api/metrics` | Prometheus 运行指标；`Authorization: Bearer $TWINFIX_METRICS_TOKEN`（token 为空时仅 `auth_disabled` 实例可访问） |
| GET | `/api/settings/overview` | 只读全局配置：Agent 默认值、运行边界、质量守卫与运行时适配器（不含密钥） |
| POST | `/api/webhooks/alertmanager`、`/api/webhooks/grafana` | 告警接入（同一实现） |
| POST | `/api/webhooks/github` | Issue / PR / Check 事件（验签） |
| POST | `/api/webhooks/gitlab` | Issue Hook / MR 审批（`X-Gitlab-Token`） |
| POST | `/api/webhooks/jenkins` | 孪生部署回调 |
| POST | `/api/signals` | 通用信号接入（前端报错、自定义） |
| GET | `/api/incidents` | 列表：state/severity/service 多值筛选、`q` 搜索、`limit`/`offset` 分页，返回 `{items,total}` |
| GET | `/api/incidents/stats` | 全工作区统计（总数/进行中/等待人工/高优先级/已结束与服务清单），不受列表筛选影响 |
| GET | `/api/incidents/{id}` | 详情（含诊断、尝试、验证、Twin） |
| GET | `/api/incidents/{id}/stream` | SSE：状态变更、工具调用、假设树更新 |
| POST | `/api/incidents` | 手动创建 |
| POST | `/api/incidents/{id}/guide` | 人工中途纠正方向（注入提示） |
| POST | `/api/incidents/{id}/cancel` | 取消；观察期中同样生效（唤醒观察期节点 → CANCELLED） |
| POST | `/api/incidents/{id}/assignee` | 指派/取消指派负责人（maintainer+；终态 409） |
| GET | `/api/incidents/{id}/similar` | 同项目历史判例（只取已合并 / NOT_BUG / ALREADY_FIXED） |
| POST | `/api/approvals/{id}/decide` | 批准/驳回（需登录 + 角色）；驳回可带 `reason_kind`：`patch`（回修复）或 `root_cause`（回诊断） |
| GET | `/api/runs/{id}` | 单次 Agent 执行及工具调用明细（控制台“运行轨迹”抽屉） |
| GET | `/api/stats/runtime` | 运行态：各状态事故数、进行中运行、待审批、孪生占用、卡住事故与边界值 |
| GET | `/api/stats/cost?days=7` | 用量：token 总量、按 Agent、按日、token 消耗最高的事故（金额字段保留，未配单价时为 0） |
| GET | `/api/reports/{id}/download` | 报告 Markdown 下载（`Content-Disposition` 带类型与事故 id） |
| POST | `/api/arena/inject` | Arena 注入故障 |
| POST | `/api/eval/runs` | 发起评测 |
| GET | `/api/eval/runs/{id}` | 评测结果 |
| CRUD | `/api/projects`、`/api/users` | 配置管理 |
| PATCH | `/api/projects/{name}` | 项目接入配置：`repo` / `branch_map` / `config`（`config` 整体替换） |
| POST | `/api/projects/{name}/check` | 接入自检（只读探测）：仓库与分支映射、代码托管、工作区来源、孪生提供方与模板、通知渠道 |
| GET | `/api/users/assignable` | 可指派账号 `{id,name,role}`（maintainer+；不含邮箱） |

告警入口接受 Alertmanager / Grafana 的 `alerts[]` 载荷：只处理 `firing`，按 `labels` 提取
`alertname` / `service` / `env` / `severity`（`critical→S1`、`error→S2`、`warning→S3`、`info→S4`，
也接受 `S1`–`S4`），`annotations.summary` 作标题、`description` 作正文、`stacktrace` 作堆栈。
项目取 `labels.twinfix_project` 或 `labels.project`；全库只有一个项目时可省略。
重复投递按 `fingerprint`（没有时用 label 集哈希）归并到同一事故。
鉴权用 `Authorization: Bearer $TWINFIX_ALERT_WEBHOOK_SECRET`（或 `X-TwinFix-Token`），未配置密钥一律 401。

GitLab 项目 Webhook（`POST /api/webhooks/gitlab`）用 `X-Gitlab-Token`（`TWINFIX_GITLAB_WEBHOOK_SECRET`）鉴权：
Issue Hook 在 Issue 带触发标签（open/reopen，或标签变化的 update）时上报信号，项目按
`project.path_with_namespace` 与 `Project.repo` 匹配（支持嵌套 group）；Merge Request Hook 的
`approved` / `approval` 计审批票，`unapproved` / `unapproval` 驳回当前合并审批（无附言时转人工）。同一 Issue 按
`gitlab-issue:<path>#<iid>` 归并到同一事故。

Jenkins 孪生部署回调（`POST /api/webhooks/jenkins`）接收
`{"twin": "<compose 项目名>", "status": "ready"|"failed", "sha": "...", "build": "...", "urls": {...}}`，
用 `Authorization: Bearer $TWINFIX_JENKINS_WEBHOOK_SECRET` 鉴权；按 `Twin.compose_project` 更新状态与 URL，
并记一条 `twin_ready` / `twin_failed` 事件。重复回调幂等（返回 `updated=false`），未知孪生 404。

账号管理（`/api/users`，仅 admin）：`GET` 列表、`POST` 创建（重名/重复邮箱 409）、`PATCH` 改角色/邮箱/密码、
`DELETE` 删除。不能把自己降级或删除；审批记录里出现过的账号（`approval.decided_by`）也不能删，只能改角色。

项目配置（`PATCH /api/projects/{name}`，admin）只改给出的字段，`config` 是整体替换，控制台提交合并后的完整对象。
控制台“设置 → 项目接入”只动它认识的键：`budget_per_incident_tokens`、`required_merge_approvals`、
`report_recipients`、`notify.quiet_hours`、`twin.services/directory/compose_file`；其余键（`severity_recipients`、
`data_snapshot`、`observe_s`、`agent` 等）原样保留。
通知路由形状见 `twinfix/notify_routing.py`：`notify.severity_recipients`（按 `S1`–`S4` 或 `*` 兜底）、
`notify.channels`（渠道白名单）、`notify.quiet_hours`（`start`/`end`/`keep`，默认只放行 S1）。

多仓库：`Project.repo` 仍是主仓库，附加仓库写在 `config.repos`：

```json
{
  "primary_repo_name": "order",
  "repos": [
    {"name": "inventory", "repo": "acme/inventory", "branch_map": {"test": "dev"}, "fixable": true},
    {"name": "payment", "repo": "acme/payment"}
  ],
  "service_repos": {"order": ["order", "inventory"], "inventory": "inventory", "payment": "payment"}
}
```

事故按 `incident.service` 经 `service_repos` 选中服务所属仓库（值可以是仓库名或仓库名数组），连同主仓库一起检出；
`incident.meta.repos` 也可显式追加。附加仓库检出到工作区 `_repos/<name>`，代码类工具（`read_file`、`list_dir`、
`search_code`、`search_symbol`、`find_references`、`code_outline`、`git_blame`、`git_log`）支持 `repo` 参数；
不传默认主仓库。附加仓库默认只读，只有 `fixable: true` 的仓库才允许 Fixer 通过 `write_file(repo=...)` 修改。

修复按仓库拆分：补丁的 `repos` 保存各附加仓库的 diff 与 `files_changed`，编排器逐仓库限定提交范围、分别推
`fix/twinfix-*` 分支并各开一个 PR（主仓库 PR 仍是事故关联 PR）。`build`、`run_tests`、`workspace_diff`、
`push_branch`、`open_pr`、`set_check`、`comment`、`merge_pr` 都接受 `repo` 参数；`repo` 留空表示主仓库。
合并顺序固定为附加仓库 → 主仓库：主仓库 PR 是整套修复的提交点，附加仓库全成功后才合主仓库；若附加仓库已合并而
主仓库失败，事故转 `NEEDS_HUMAN`，已合并仓库记在 `incident.meta.merged_repos`，重试或重放不会重复合并。
孪生环境仍只从主仓库构建，附加仓库改动不进入孪生部署；跨仓库修复的验证以各仓库单测/构建 + 主仓库孪生为准。
GitHub PR 评审 webhook 仍只关联主仓库 PR；附加仓库 PR 通过控制台审批同一张合并审批票。

GitHub Issue webhook 会按主仓库或任一附加仓库匹配项目；项目自检（`GET /api/projects/{name}/check`）新增
`extra_repos` 一项，逐个探测附加仓库分支头。`POST /api/projects` 与 `PATCH /api/projects/{name}` 会校验
`repos` / `service_repos` / `primary_repo_name` 的形状（repo 必须是 `owner/name`、名称不重复、
`fixable` 必须是布尔值、映射值必须是非空字符串或字符串数组），非法配置返回 422。

审批语义：`required_merge_approvals > 1` 时 `POST /api/approvals/{id}/decide` 只记账不立即合并，
响应里的 `payload.approvals` 是已投票列表；同一账号重复投票 409。GitHub 的 PR 评审同样计票，
`changes_requested` 按驳回处理（可带意见回 FIXING）。

Arena 注入（`POST /api/arena/inject`，maintainer+）把 `arena/cases/<id>` 的故障注入成一个新项目的本地仓库，
默认随即上报一条信号并建事故：`{"case_id": "C001", "project": "arena-demo", "trigger": true}` →
`{"case_id","project","repo","branch","sha","incident_id","created"}`。需要 `TWINFIX_CODEHOST=local` 且与 worker
共享 `TWINFIX_GIT_ROOT`；未知用例 404、项目已存在 409。

评测任务（`POST /api/eval/runs`，maintainer+）：`{"cases": [...], "label": "", "model": "", "effort": "low|medium|high", "timeout_s": 3600}`
→ 202 + 任务对象（`id`/`status`）。API 以独立子进程执行 `twinfix-arena eval`（与手工命令行同一路径），
状态与日志落 `data_dir/eval_runs/`，结束后报告写到 `eval_reports_dir/<run-id>.md|.json`。
`GET /api/eval/runs` 列任务、`GET /api/eval/runs/{id}` 返回 `{"run": ..., "report": ...}`。
同一时间只允许一个任务（409）；缺少 `agent_mode=llm`、`codehost=local`、用例所需的种子仓库（demo-shop /
demo-python）或未知用例时直接 409。

鉴权：Web 用 JWT（本地账号，scrypt 密码哈希，预留 OIDC）；账号用 `twinfix-user create|passwd|list` 管理；
`TWINFIX_AUTH_DISABLED=true` 时以本地管理员身份放行（仅开发）。写操作要求 maintainer/admin 角色。
Webhook 用各自签名/令牌；邮件中的操作链接为短期签名 URL，只跳转到控制台，不直接执行。

## 6. MQ 拓扑

| 队列 | 生产者 | 消费者 | 说明 |
|---|---|---|---|
| `signals.ingest` | ingest | triage | |
| `incidents.step` | orchestrator | worker | 消息体只含 `incident_id` + `step`，worker 从 DB 读状态 |
| `incidents.step.retry` | worker | — | TTL 延迟重试，过期回到 `incidents.step` |
| `notify.send` | reporter | notifier | |
| `*.dlq` | — | 人工排查 | 超过重试次数 |

消费幂等：以 `(incident_id, step, attempt_seq)` 为幂等键存 Redis；处理前抢 `lock:incident:{id}`。
