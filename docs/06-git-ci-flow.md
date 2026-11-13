# 06 Git 与 CI 流程

## 1. 分支约定（对齐团队现有流程）

| 分支 | 对应 | TwinFix 行为 |
|---|---|---|
| main | 正式环境 | 生产 bug 的修复目标（hotfix），只能人工合并 |
| release | 版本迭代 | 迭代期 bug 的修复目标，只能人工合并 |
| dev | 测试环境 | 测试环境 bug 的修复目标；可配置为人工确认后自动合并 |
| fix/* | bug 修复 | TwinFix 分支为 `fix/twinfix-<短id>`，合并后删除 |
| feature/* | 功能开发 | TwinFix 不触碰 |

环境 → 目标分支映射写在 `project.branch_map`，可改。

合入 main / release 之后的回合并（main → release → dev）沿用团队现有做法，TwinFix 只在报告中提醒，不自动操作。

## 2. 端到端时序

```
1. Signal(env=test, service=order) 到达
2. 查部署记录 → base_commit = 测试环境当前运行的 sha
3. 沙箱 clone repo@base_commit；创建分支 fix/twinfix-7f3a
4. Jenkins 触发 twinfix-twin-deploy(branch=fix/twinfix-7f3a@base_commit, project=twinfix-7f3a, ports=..., data=snapshot-latest)
   → 回调 TwinFix：Twin ready
5. Reproducer 写复现用例 → 在 base_commit 上失败 ✔
6. Fixer 修改 → Verifier：build ✔ repro ✔ regression ✔ twin_errors ✔ test_integrity ✔
7. rebase 到 dev 最新 head（若有新提交）→ 重跑第 6 步 → 推送分支
8. 创建 PR(fix/twinfix-7f3a → dev)，设置 Check `twinfix/verify` = success @sha
9. 发报告（邮件 + 飞书）：根因、diff、测试结果、Twin 地址、合并/驳回入口
10. 开发在 Twin 上人工复测
11a. 点"确认合并" → MERGING：检查 dev head 是否变化 → 变了就回到第 7 步 → 合并（expected_sha 防并发）
11b. 驳回附意见 → 回到 FIXING
12. 合并后销毁 Twin，删除分支，关闭关联 Issue，事故 CLOSED
```

## 3. GitHub 配置

- 以 GitHub App 身份运行（不使用个人 token），安装到目标仓库。
- dev / release / main 开启分支保护：要求 PR、要求 `twinfix/verify` Check 通过（针对 TwinFix PR）、要求最新代码（strict）。
- Check 绑定 head sha；dev 有新提交后旧 Check 不再满足 strict 要求，强制重验。
- Issue 入口：给 Issue 打 `twinfix` 标签 → Webhook → 创建 Signal(source=issue)；进度以评论形式回写，PR 中 `Fixes #<issue>` 自动关联。
- 合并方式默认 squash，提交信息含 Incident 链接。

## 4. Jenkins 配置

提供 `deploy/jenkins/twinfix-twin-deploy.Jenkinsfile` 模板，参数：

| 参数 | 说明 |
|---|---|
| `REPO` / `SHA` | 代码 |
| `TWIN_ID` | compose 项目名 |
| `PORT_BASE` | 端口段起点 |
| `DATA_SNAPSHOT` | 数据快照名 |
| `CALLBACK_URL` / `CALLBACK_TOKEN` | 完成后回调 TwinFix |

流程：checkout → 构建镜像（复用团队现有 Dockerfile）→ 用 `deploy/twin/compose.twin.yml` 覆盖文件启动（注入 `twin` profile、mock 地址、独立库）→ 健康检查 → 回调。销毁任务 `twinfix-twin-destroy`。

不想改 Jenkins 的团队可切换为 `DirectDockerTwinProvider`，由 TwinFix 直接调用孪生宿主机的 Docker API。

## 5. 冲突与边界情况

| 情况 | 处理 |
|---|---|
| 目标分支已修复（在最新 head 上复现用例已通过） | ALREADY_FIXED，关闭 PR，发报告 |
| rebase 文本冲突，冲突块不涉及修复改动的语义（import、格式、相邻行） | Fixer 解决 → 完整重验 |
| rebase 冲突涉及同一方法/同一逻辑 | 停止 → NEEDS_HUMAN，报告附双方 diff 与分析 |
| 合并时 head 又变化 | `expected_sha` 校验失败 → 回到重验 |
| 合并前预检期间目标分支又前进 | 预检完成后、真正合并前再读一次目标 head；变了就重新同步并预检，最多再一轮，仍变化 → NEEDS_HUMAN（`premerge_head_moved` 事件留痕）。附加仓库目标 head 变化 → 直接 NEEDS_HUMAN（附加仓库不自动变基） |
| 人在 PR 上推了新提交 | 视为人工接管：重新验证一次后只做报告，不再自动修改 |
| 同一文件有多个 TwinFix PR | 按 Incident 严重度串行合并，后者合并前自动 rebase 重验 |

## 6. GitLab 配置

TwinFix 通过 `codehost=gitlab` / `ci=gitlab` 接入 GitLab（gitlab.com 与自建实例均支持）：

| 配置 | 说明 |
|---|---|
| `TWINFIX_CODEHOST=gitlab` | 启用 `GitLabCodeHost` |
| `TWINFIX_GITLAB_BASE_URL` | 实例根地址，默认 `https://gitlab.com`（自建如 `https://gitlab.example.com`） |
| `TWINFIX_GITLAB_API_URL` | 空 = `<base_url>/api/v4`；实例挂在子路径时显式指定 |
| `TWINFIX_GITLAB_TOKEN` | 项目 / 组 / 个人 access token，scope：`api`、`write_repository` |
| `TWINFIX_GITLAB_WEBHOOK_SECRET` | 项目 Webhook 的 Secret token（请求头 `X-Gitlab-Token`） |
| `TWINFIX_GITLAB_CI_PROJECT` | `ci=gitlab` 时孪生部署流水线所在项目（`group/project`） |
| `TWINFIX_GITLAB_CI_REF` | 触发流水线的默认分支，默认 `main` |
| `TWINFIX_GITLAB_CI_TOKEN` | CI 触发令牌（`api` scope）；留空复用 `TWINFIX_GITLAB_TOKEN` |

语义映射：

- PR → Merge Request（`iid` 作为 PR 编号）；`Project.repo` 用完整 `path_with_namespace`，支持嵌套 group；
- Check → Commit Status（`success` / `failed`，`neutral` 记为 `skipped`），名称统一 `twinfix/verify`；
- 合并 → Merge MR API，带 `sha` 参数防并发；`merge_method=squash` 映射为 `squash=true`，
  `merge` 映射为 `squash=false`，`rebase` 沿用项目设置；
- 评论 → Issue / MR Notes；
- Issue 入口：项目 Webhook 勾选 Issue events 与 Merge request events，Secret token 与
  `TWINFIX_GITLAB_WEBHOOK_SECRET` 一致；Issue 打上触发标签（`project.config.issue_label`，默认 `twinfix`）即上报；
- 审批：MR 的 `approved` / `approval` 计一票；`unapproved` / `unapproval` 驳回当前合并审批
  （GitLab 不携带附言，驳回后转人工）。

GitLab CI 触发（`CIProvider.trigger`）创建 `TWINFIX_GITLAB_CI_PROJECT` 的流水线，作业名放在
`TWINFIX_JOB` 变量，其余参数逐项作为 CI 变量；流水线状态映射为 queued/running/success/failure/aborted，
日志取同名作业（或首个失败作业）的 trace。

## 7. Kubernetes 孪生（可选）

`twin_provider=k8s` 时用 `kubectl` 在独立 Namespace 中拉起孪生，适合已有 K8s 集群、镜像由 CI
统一构建的团队（K8s 侧不做源码构建）。

| 配置 | 说明 |
|---|---|
| `TWINFIX_TWIN_PROVIDER=k8s` | 启用 `KubernetesTwinProvider` |
| `TWINFIX_K8S_NODE_HOST` | worker 可达的节点地址；孪生 Service 的 NodePort 通过它访问 |
| `TWINFIX_K8S_TWIN_MANIFEST` | 多文档 YAML 模板；空 = `deploy/k8s/twin.example.yaml` |
| `TWINFIX_K8S_DEFAULT_IMAGE` | 项目未配置 `twin.image` 时的默认镜像 |
| `TWINFIX_K8S_KUBECTL` / `TWINFIX_K8S_KUBECONFIG` / `TWINFIX_K8S_CONTEXT` | kubectl 路径与凭据（留空用进程默认） |
| `TWINFIX_K8S_ROLLOUT_TIMEOUT_S` | `kubectl rollout status` 超时，默认 600s |

Manifest 约定：

- 可用占位符：`${NAMESPACE}`、`${TWIN_ID}`、`${INCIDENT_ID}`、`${REPO}`、`${SHA}`、`${DATA_SNAPSHOT}`、
  `${IMAGE}`、`${PORT_<SERVICE>}`，以及项目配置里的标量字段 `${TWIN_<KEY>}`；未知占位符直接报错。
- Deployment 与 Service 同名；Service 必须是 `NodePort`（端口写 `${PORT_ORDER}` 等占位符，或让集群自动分配）。
- 健康检查路径默认 `/actuator/health`，项目可用 `project.config.twin.health_path` 覆盖；
  项目还可用 `twin.directory` / `twin.manifest_file` / `twin.services` 覆盖模板、Manifest 与服务列表。
- 每个孪生一个 Namespace `twinfix-<短id>`，资源带 `dev.twinfix/managed-by=twinfix` 标签；
  事故终态时删除 Namespace，删除失败时保留槽位（避免 NodePort 被下一个孪生复用）；
  worker 并发槽位与 Compose 模式共用 `TWINFIX_TWIN_MAX_CONCURRENT`。
- 镜像必须由流水线按事故 sha 构建并推送；TwinFix 只负责下发 Manifest 与探活，不参与镜像构建。

> 当前仓库没有可用的 K8s 集群，适配器通过注入的 kubectl runner 做单元测试（命令序列、NodePort 解析、
> 槽位回收、失败清理、日志/Arthas 探针）；真实集群联调前请先在测试 Namespace 里跑一次
> `TWINFIX_TWIN_PROVIDER=k8s` 的 happy 用例。
