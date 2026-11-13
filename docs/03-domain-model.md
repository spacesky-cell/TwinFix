# 03 领域模型

## 1. 实体关系

```
Project 1─* Service
Project 1─* Incident 1─* Signal
Incident 1─1 Diagnosis
Incident 1─* Attempt 1─1 Patch
                    1─* Verification
Incident 1─0..1 Twin
Incident 1─* Run 1─* ToolCall
Incident 1─* Evidence
Incident 1─* Report
Incident 1─* Approval
Incident 1─* IncidentEvent      (状态变更流水)
```

## 2. 表结构（v1）

公共字段：`id`（ULID）、`created_at`、`updated_at`。

**project**
| 字段 | 类型 | 说明 |
|---|---|---|
| name | text | 项目名 |
| repo | text | `owner/name` |
| language | text | `java` / `python` / `go` / `typescript` / `csharp`（决定语言插件、沙箱镜像与判分方式） |
| branch_map | jsonb | 环境 → 分支，如 `{"test":"dev","prod":"main","release":"release"}` |
| config | jsonb | 构建/测试命令覆盖、禁止改动路径、通知路由等 |

**service**：`project_id`、`name`、`module_path`、`compose_service`、`health_url`

**signal**
| 字段 | 类型 | 说明 |
|---|---|---|
| source | enum | `alert` `log` `issue` `manual` `im` `frontend` |
| raw | jsonb | 原始载荷 |
| service | text | 归属服务 |
| env | enum | `test` `release` `prod` |
| fingerprint | text | 指纹 |
| trace_id | text | 可空 |
| occurred_at | timestamptz | |
| incident_id | fk | 聚合到的事故 |

**incident**
| 字段 | 类型 | 说明 |
|---|---|---|
| project_id | fk | |
| title | text | |
| state | enum | 见状态机 |
| severity | enum | `S1`–`S4` |
| env | enum | 发现环境 |
| fingerprint | text | 唯一索引（同项目内未关闭的事故） |
| signal_count | int | |
| base_commit | text | bug 出现时实际部署的提交 |
| target_branch | text | 修复要合入的分支 |
| fix_branch | text | `fix/twinfix-<短id>` |
| pr_number | int | |
| github_issue | int | 可空 |
| cost_tokens / cost_amount | int / numeric | 累计成本 |
| assignee_id | fk user | 可空，当前负责人（指针，不改状态机） |
| closed_reason | text | 终止原因 |

**diagnosis**：`incident_id`、`summary`、`root_cause`、`hypotheses`(jsonb，假设树)、`suspect_locations`(jsonb，文件:行)、`confidence`(0–1)

**attempt**：`incident_id`、`seq`、`status`(`running` `passed` `failed` `aborted`)、`feedback`(上一轮验证失败原因)

**patch**：`attempt_id`、`diff`、`files_changed`、`repos`(jsonb：附加仓库名 → `{diff, files_changed, commit_sha, branch, pr_number, pr_url}`)、`touches_tests`(bool)、`commit_sha`、`explanation`

**verification**
| 字段 | 说明 |
|---|---|
| attempt_id | |
| commit_sha | 被验证的提交 |
| base_sha | 目标分支基准提交 |
| checks | jsonb：`[{name:"repro",status,detail},{name:"regression"...},{name:"twin_errors"...},{name:"test_integrity"...},{name:"build"...}]` |
| passed | bool |

**twin**：`incident_id`、`compose_project`、`host`、`ports`(jsonb)、`commit_sha`、`status`(`creating` `ready` `failed` `destroyed`)、`expires_at`、`urls`(jsonb)

**evidence**：`incident_id`、`run_id`、`kind`(`metric` `log` `trace` `thread_dump` `code` `test_output`…)、`summary`、`content_ref`(大内容存对象存储/文件)、`tokens`

**run**：`incident_id`、`agent`、`model`、`status`、`input_tokens`、`output_tokens`、`cost`、`latency_ms`、`error`

**tool_call**：`run_id`、`tool`、`level`、`args`(脱敏后)、`result_ref`、`status`、`latency_ms`、`approval_id`

**approval**：`incident_id`、`action`(`merge` `deploy_twin_prod_data`…)、`payload`、`status`(`pending` `approved` `rejected` `expired`)、`decided_by`、`decided_at`、`token_hash`

合并审批默认 1 人通过；`project.config["required_merge_approvals"] > 1` 时同一审批保持 `pending`，逐票记在 `payload["approvals"]`（`actor` / `comment` / `at`），凑够人数才受理；同一账号重复投票返回冲突。GitHub 的 PR 评审（`pull_request_review` approved / changes_requested）按同样规则计票或驳回。

**report**：`incident_id`、`type`、`channel`、`recipients`、`content`、`sent_at`、`status`

**incident_event**：`incident_id`、`from_state`、`to_state`、`reason`、`actor`(`system`/`agent:<name>`/`user:<id>`)、`payload`

**user**：`name`、`email`、`role`(`admin` `maintainer` `viewer`)、`im_ids`(jsonb)

## 3. 事故状态机

```
            ┌───────────► NOT_BUG ──────────────────────────────┐
            │                                                   │
NEW ──► TRIAGED ──► DIAGNOSING ──► REPRODUCING ──► FIXING ◄──┐    │
                        │              │             │      │    │
                        │              ▼             ▼      │ 失败│
                        │      CANNOT_REPRODUCE  VERIFYING ─┘(≤N)│
                        │                            │           │
                        ▼                            ▼           │
                   NEEDS_HUMAN ◄─────────── AWAITING_REVIEW      │
                        ▲                     │      │           │
                        │      人工驳回/冲突    │      ▼           │
                        └─────────────────────┘   MERGING ──► MERGED ──► OBSERVING ──► CLOSED
                                                                             │
                                                                             │ 同指纹复发
                                                                             ▼
                                                                        NEEDS_HUMAN

 任意非终态 ──► ALREADY_FIXED / CANNOT_FIX / CANCELLED
 观察期也可以人工取消 ──► CANCELLED
```

| 状态 | 进入条件 | 退出 |
|---|---|---|
| NEW | Signal 生成新指纹 | 分诊完成 |
| TRIAGED | 判定为 bug、已定级 | 获取 Incident 锁后进入诊断 |
| DIAGNOSING | Diagnoser 运行 | 置信度 ≥ 阈值 → REPRODUCING；证据不足 → NEEDS_HUMAN |
| REPRODUCING | Twin 创建 + Reproducer 运行 | 得到稳定失败用例（连续 2 次失败）→ FIXING；否则 CANNOT_REPRODUCE |
| FIXING | Fixer 运行 | 产出 Patch → VERIFYING；判定超出能力 → CANNOT_FIX / NEEDS_HUMAN |
| VERIFYING | Verifier 运行 | 通过 → 推送分支、创建 PR → AWAITING_REVIEW；失败且次数未满 → FIXING |
| AWAITING_REVIEW | PR 已创建、报告已发送 | 人点确认 → MERGING；驳回 → NEEDS_HUMAN 或带意见回 FIXING |
| MERGING | rebase 到目标分支 → 重验 → 合并 | 成功 → MERGED；语义冲突 → NEEDS_HUMAN；最新代码已不复现 → ALREADY_FIXED |
| MERGED | 合并成功 | `project.config["observe_s"] > 0` → OBSERVING；否则销毁 Twin → CLOSED |
| OBSERVING | 合并后观察窗口（挂起，等 sweeper 到点 / 复发信号 / 人工取消唤醒） | 期内同指纹新信号 → NEEDS_HUMAN；期满无复发 → CLOSED；人工取消 → CANCELLED |

规则
- 每次转换写 `incident_event`，并通过 SSE 推送前端。
- 终态（NOT_BUG、CANNOT_REPRODUCE、CANNOT_FIX、ALREADY_FIXED、CANCELLED、CLOSED）均触发对应报告。
- 每个状态有超时（可配置），超时 → NEEDS_HUMAN；观察期不受状态超时约束，由 `observe_s` 决定长度。
- 观察期的复发判定只看同指纹新信号的计数，不看人工意见；复发一律转人工复核。
- 同一指纹的新 Signal 进入已有 Incident，只更新计数，不重启流程；已 CLOSED 的指纹再次出现 → 新建 Incident 并标记"复发"。
