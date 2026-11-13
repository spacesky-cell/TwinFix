# 05 安全设计

原则：安全不依赖 AI 判断正确，而是保证它判断错了也闯不了大祸。所有约束在工具网关和基础设施层强制，Prompt 只是辅助。

## 1. 权限分级

| 级别 | 范围 | 示例 | 规则 |
|---|---|---|---|
| L0 只读观察 | 测试/生产的监控数据 | 查指标、搜日志、看链路、读部署记录、读代码 | 自动执行；限频 |
| L1 有开销诊断 | 测试环境运行时 | Arthas `thread`/`jvm`/`sc`/`jad` 与限定次数的 `trace`/`watch`（工具 `jvm_probe`，命令白名单逐字匹配） | 自动执行但限次数/时长；只打到 TwinFix 自建的孪生容器，不触真实环境；对生产默认关闭，开启需审批 |
| L2-sandbox | 沙箱工作区与孪生环境 | 改代码、编译、跑测试、部署/请求 Twin | 自动执行；仅限 TwinFix 自建资源 |
| L2-external | 外部协作系统写入 | 推送 `fix/twinfix-*` 分支、创建 PR、评论 Issue、发通知 | 自动执行；GitHub App 权限只到这一层 |
| L3 合并 | 合并 PR | 合入 dev | 需人工点确认（或项目配置为"验证通过后自动合并到 dev"） |
| L3 合并 | 合并 PR | 合入 release / main | 必须人工，且由人在 GitHub 上操作或在控制台二次确认 |
| 禁止 | — | 生产部署、生产配置/数据库写入、任意 shell 到真实环境、删除分支以外的仓库操作 | 系统中不存在对应工具 |

L3 的“人工确认”可由项目配置 `required_merge_approvals` 提高到 2–5 人：同一事故需要相应数量的不同账号分别投票，
同一账号重复投票直接冲突；GitHub 上的 PR `approved` 评审按账号计票，`changes_requested` 按驳回处理。

## 2. 工具网关

- Agent 只能看到并调用分配给它的工具清单；未注册的工具无法调用。
- 每个工具声明：级别、参数 schema、超时、频率、是否需审批、结果最大长度。
- 沙箱命令：只在 Workspace 容器内执行；容器无宿主机挂载、无 Docker socket、无生产网络、CPU/内存/时长受限；出网只允许包仓库（Maven 镜像）与 LLM 网关。
- 文件写入：路径必须在 Workspace 内；命中项目配置的 `protected_paths`（如 `**/application-prod.yml`、`deploy/**`、`.github/**`、`Jenkinsfile`）直接拒绝。
- 跨仓库写入：附加仓库默认只读，只有 `config.repos[].fixable: true` 才允许 Fixer 写入；`write_file(repo=...)`
  只能落在该仓库的 `_repos/<name>` 根内，其他 Agent 一律拒绝。推送/PR/Check/合并都按 `repo` 路由，
  分支保护同样要求 `fix/twinfix-*`，不会把附加仓库改动混进主仓库提交。
- 全量审计：参数（脱敏后）、结果引用、耗时、Agent、模型、审批人，追加写，不可修改。
- 控制台发起的评测（`POST /api/eval/runs`）由 API 拉起独立子进程执行固定的 `twinfix-arena eval` 命令：
  参数来自 HTTP 请求但经过枚举/长度校验，不接受任意命令行；子进程继承 API 环境变量，日志落 `data_dir/eval_runs`，
  同一时间只允许一个任务。评测要求 `agent_mode=llm` + `codehost=local` + 共享 `git_root`，不会写真实仓库。

## 3. 凭据隔离

| 凭据 | 持有者 | 权限 |
|---|---|---|
| 监控只读账号 | adapters | 只读 |
| GitHub App | codehost 适配器 | contents:write（仅允许推 `fix/twinfix-*`，由分支规则保证）、pull_requests:write、checks:write、issues:write |
| GitLab access token | codehost 适配器 | `api` + `write_repository`（同样只推 `fix/twinfix-*`）、MR / Commit Status / Notes 权限 |
| GitLab Webhook token | API 的 `/webhooks/gitlab` | 仅能上报 Issue 事件与同步 MR 审批 |
| Jenkins token | ci 适配器 | 仅能触发 `twinfix-twin-*` 任务 |
| GitLab CI token | ci 适配器 | 仅能创建/读取配置项目（`gitlab_ci_project`）的流水线 |
| Metrics token | Prometheus 抓取 `/api/metrics` | 只读运行指标，不含源码、Issue 正文与密钥 |
| OTel 导出端点 | worker → OTLP/HTTP 收集器 | 仅 span 元数据（事故 / Run / 工具名与状态 / 模型与 token 用量），不含 Prompt、源码、工具参数与结果 |
| Jenkins 回调 token | API 的 `/webhooks/jenkins` | 仅能更新指定 `compose_project` 的孪生状态 |
| Docker API | twin/sandbox 适配器 | 仅孪生宿主机，TLS 证书认证 |

凭据不进入 Prompt，不暴露给沙箱容器。

## 4. 孪生环境隔离

| 资源 | 隔离方式 |
|---|---|
| 进程/网络 | Compose：独立 compose 项目名 `twinfix-<id>` 与独立网络；端口从可配置端口段分配。K8s：每个孪生独立 Namespace（同名 `twinfix-<id>`），Service 以 NodePort 暴露给 worker |
| 数据库 | 从测试库快照恢复的独立库（或预置种子数据），不与测试线共用 |
| MQ | 独立 vhost / topic 前缀 |
| Redis | 独立实例或独立 db index + key 前缀 |
| 定时任务 | 通过 Spring profile `twin` 关闭（项目需提供，或注入 `spring.task.scheduling.enabled=false` 等配置） |
| 外部调用 | 短信、支付、邮件、第三方 HTTP 指向 mock 服务（WireMock） |
| 资源上限 | 每个孪生服务容器默认 1 CPU / 768MB、数据库 256MB / 0.5 CPU（`TWINFIX_TWIN_SERVICE_CPUS`、`TWINFIX_TWIN_SERVICE_MEM_LIMIT`、`TWINFIX_TWIN_DB_MEM_LIMIT`）；JVM 以 `MaxRAMPercentage=70` 按容器上限自适应堆。沙箱工作区默认 2 CPU / 2GB（`TWINFIX_SANDBOX_CPUS`、`TWINFIX_SANDBOX_MEMORY_MB`），pids 512。K8s 的资源上限写在 Manifest 的 requests/limits 里 |
| 生命周期 | 事故走到终态即销毁；没走到的按 TTL（默认 24h）由 worker 巡检回收，创建卡死超过 1h 的孪生同样回收；端口段按槽位分配，全局并发上限。K8s 销毁 = 删除整个 Namespace（`--wait=false`），资源带 `dev.twinfix/managed-by=twinfix` 标签便于集群侧兜底 GC |

容量估算：空闲基础设施约 0.8–1GB；一次事故 ≈ 沙箱 2GB + 孪生（db 256MB + 两个服务各 768MB 上限）≈ 3.5–4GB；
四并发约 12–14GB。2C/2G 只能跑 stub 演示；个人单机使用建议 4C/8G，并设 `TWINFIX_TWIN_MAX_CONCURRENT=1`。
内存紧张时可把孪生服务上限降到 `512m`、CPU 降到 `0.5`（启动失败或健康检查超时就调回）。

生产数据永不进入孪生环境；生产 bug 用测试数据 + 复现脚本在 Twin 中复现。

## 5. 防 AI 作弊

- 不允许删除、跳过（如 JUnit `@Disabled`、pytest `skip`/`xfail`、Go `t.Skip`、Node `test.skip`/`skip: true`、
  C# `[Fact(Skip=...)]` / `Assert.Ignore`）或弱化已有测试断言（含 C# harness 的 `Check.*`）。
  由系统对修复 diff 做确定性扫描（`orchestrator/integrity.py`），
  覆盖 Verifier 自报的 `test_integrity` 检查：删测试文件 / 删测试方法 / 删断言 / 新增跳过 → 验证失败并交回 Fixer。
- 修复 diff 中新增吞异常的 `catch`、按测试夹具数据特判（硬编码 SKU/用户）时标记为可疑，写入 `test_integrity` 详情与报告，供人评审。
- 复现用例必须在修复前的代码上失败：Reproducer 跑出一次失败即可提交，编排器在沙箱里独立重跑 2 次确认稳定，不采信自报次数。
- 验证由独立的 Verifier 完成，不复用 Fixer 的上下文。
- 提交范围受控：进提交的只有补丁声明的文件（`files_changed`）与复现用例，外加既有受控文件的改动；
  Fixer 自验时新建的临时文件（例如临时用例）留在工作区、不随修复合并，并在事故事件里记 `fix_files_scoped` 留痕。
- 跨仓库补丁逐仓库执行同一套完整性扫描（删测试、跳过、吞异常），提交范围也按各仓库的 `files_changed` 独立限定；
  附加仓库 PR 先合并、主仓库最后合并，已合并仓库记入 `incident.meta.merged_repos`，重放不会重复合并。

## 6. Prompt 注入与数据安全

原则：指令与数据分离是纵深防御，真正的权限边界仍在工具网关（ADR-003）。
实现见 `server/twinfix/agents/untrusted.py`。

- **统一围栏**：事故标题、服务标签、日志、堆栈、Issue 正文/标签/链接、代码与注释、构建/测试输出、
  trace、HTTP 响应在进入模型前，统一包进 `[[UNTRUSTED id=<本次运行随机 nonce> source=...]]` 与
  `[[/UNTRUSTED id=<同一 nonce>]]` 之间。系统消息、任务说明与明确标注的人工指导是唯一可信指令来源，
  围栏内文字一律按数据读。
- **伪造边界中和**：内容中出现的围栏标记（含大小写与空格变体）在 `[[` 后插入零宽空格；内容里若恰好
  出现本次 nonce，也会被拆开，无法提前闭合围栏。NUL 等控制字符会被清除，换行统一为 `\n`。
- **工具输出同样处理**：`read_file` / `search_logs` / `get_trace` / 测试与构建输出等回贴模型前统一围栏；
  拒绝、超时、审批等系统消息也围栏，但不做注入扫描（拒绝原因里出现工具名是正常现象）。
- **确定性注入探测**：命中"忽略之前指令 / 角色切换 / 泄露系统提示词 / 调用工具 / 外发数据 / 聊天模板标记"
  等模式时，在围栏外追加一条系统安全提示，并记 warning 日志；不阻断正常分析，也不改变权限。
- **跨 Agent 一致**：diagnosis / repro / patch 等上游产物与历史判例传给下游时同样按不可信数据围栏，
  避免注入文本借一次模型转述变成下一次运行的"指令"。
- **权限不依赖模型**：即使模型被骗，也只能调用其工具清单内、且网关放行的操作；受保护路径、只推修复分支、
  合并审批与追加审计仍由代码强制。
- **脱敏**：发往 LLM 前按规则遮手机号、身份证、银行卡、邮箱、token/密钥模式；事故信号原文与工具结果
  使用同一个 `Redactor`（`gateway/redact.py`）。
- **数据不出内网**：支持接入内网部署的模型。

## 7. 人机协同

- 人可以随时：中途纠正方向（`/guide`）、取消、驳回 PR 并附意见（意见回传 Fixer）。
  驳回时可选退回阶段：`patch`（根因成立，从修复重做）或 `root_cause`（从诊断重来），避免一句话把两类问题混在一起。
- 全局急停：一个开关暂停所有 Agent 的新工具调用。
- 审批与合并操作要求登录与角色，邮件链接只跳转不执行。
- 观察期：合并后默认进入观察窗口（`observe_s`），期内同指纹复发自动转人工复核；人工可以随时取消观察并收尾。
- 通知路由：按级别给收件人、限制渠道白名单、设置静默时段；静默期内只即时发送 S1，其余报告落库并标记 suppressed，
  不会静默丢报告。
- 只读角色（viewer）：控制台隐藏写操作与 Arena/评测入口；可指派账号列表、账号管理与项目接入修改只对维护者/管理员开放，
  但负责人名字随事故详情返回，只读账号也能看到“谁在处理”。
