"""运行配置。全部来自环境变量（前缀 TWINFIX_），便于 compose / k8s 注入。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# ES query_string 默认只挑 ERROR 级别；ECS（log.level）与扁平（level）两种字段命名都试，
# 未映射的字段在 ES 里不会报错。项目可用 config.logscan.query 覆盖。
LOGSCAN_QUERY = "(log.level:(ERROR OR FATAL OR SEVERE)) OR (level:(ERROR OR FATAL OR SEVERE))"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TWINFIX_", env_file=".env", extra="ignore")

    # SQLAlchemy 用 psycopg 驱动；LangGraph checkpointer 需要去掉 "+psycopg" 的纯 libpq URL
    # 用 127.0.0.1 而非 localhost：Windows 上 localhost 先解析到 ::1，每次连接白等 5 秒
    database_url: str = "postgresql+psycopg://twinfix:twinfix@127.0.0.1:5432/twinfix"
    redis_url: str = "redis://127.0.0.1:6379/0"
    amqp_url: str = "amqp://twinfix:twinfix@127.0.0.1:5672/twinfix"

    data_dir: Path = Path(".data")
    # 基准测试报告目录（twinfix-arena eval 的输出），控制台评测页读取其中的 *.json
    eval_reports_dir: Path = Path(__file__).resolve().parents[2] / "eval" / "reports"
    # 基准用例目录（arena/cases/<id>/case.yaml），控制台 Arena 页只读展示
    arena_cases_dir: Path = Path(__file__).resolve().parents[2] / "arena" / "cases"
    # demo-shop 源码；空 = arena_cases_dir 的同级 demo-shop
    arena_demo_shop_dir: Path | None = None
    # 评测判分的 Maven：容器内运行时用仓库挂载的 settings（镜像源）与可写的本地仓库
    # （容器里 twinfix 用户读不了 /root/.m2；默认留空 = 用宿主机 ~/.m2）
    eval_maven_settings: Path | None = None
    eval_maven_repo: Path | None = None
    # Go 判分默认用镜像内的 go；宿主机没装 Go 时可指定镜像，由 docker run 执行
    eval_go_image: str = ""
    # TypeScript/JavaScript 判分同理：宿主机没有 Node 22+ 时可指定镜像
    eval_node_image: str = ""
    # C# / .NET 判分同理：宿主机没有 .NET SDK 时可指定镜像
    eval_csharp_image: str = ""
    # API 对自身的地址：控制台发起的评测由独立进程回连 API（与 CLI 评测一致）
    api_self_url: str = "http://127.0.0.1:8000/api"

    # 自身可观测性：GET /api/metrics 输出 Prometheus text format。
    # token 留空时只允许 auth_disabled 的本地开发实例访问；生产应配置随机 token。
    metrics_enabled: bool = True
    metrics_token: SecretStr = SecretStr("")

    # OpenTelemetry trace 导出（Prometheus 指标之外的链路追踪）。
    # 默认关闭；开启后 worker 把 Agent 运行 / 工具调用 / LLM 请求导出到 OTLP/HTTP。
    otel_enabled: bool = False
    otel_endpoint: str = ""  # 收集器基地址或完整 /v1/traces，如 http://127.0.0.1:4318
    otel_service_name: str = "twinfix"
    otel_sample_ratio: float = 1.0

    # 鉴权：生产必须配置 jwt_secret；auth_disabled 仅用于本地开发
    jwt_secret: SecretStr = SecretStr("")
    auth_disabled: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # Agent 实现：stub（内置模拟）或 llm（真实模型）
    agent_mode: str = "stub"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"

    # 编排
    diagnosis_confidence_threshold: float = 0.6
    # 历史判例：诊断时注入同项目相似的已结案事故（关掉可做对照实验）
    precedents_enabled: bool = True
    # 复现是修复的前置：关掉 = 诊断后直接进入修复（对照实验用，验证"先复现"约束的价值）
    repro_required: bool = True
    # 复现稳定性由系统在沙箱里独立重跑确认（关掉 = 采信 Agent 自报的 failing_runs；对照实验用）
    repro_confirm: bool = True
    # 系统侧的测试完整性扫描：补丁删/跳过/弱化测试即判验证失败（对照实验用）
    integrity_check: bool = True
    # 日志行多时给 LLM 模板摘要、原文进证据库（关掉 = 直接回贴原始行；对照实验用）
    log_cluster: bool = True
    default_max_fix_attempts: int = 3
    review_timeout_s: int = 72 * 3600
    lock_ttl_s: int = 60
    step_retry_delay_s: int = 15
    step_max_retries: int = 5

    # 工作状态（DIAGNOSING…MERGING）超过该时长无进展 → NEEDS_HUMAN
    state_timeout_s: int = 3600
    # 控制台地址，用于报告中的链接
    console_url: str = "http://localhost:5173"
    # 模拟 Agent 每个动作之间的停顿，便于在前端观察事件流
    stub_delay_s: float = 0.0

    # 每个 Agent 一次运行的默认预算；可按项目覆盖（project.config.agent，见 agents/params.py）
    budget_max_tokens: int = 2_000_000
    budget_max_tool_calls: int = 120
    budget_max_seconds: int = 2400

    # ---- 适配器选择（fake = 内存实现，测试与本地演示用）
    codehost: str = "fake"  # fake / local / github / gitlab
    sandbox: str = "local"  # local（禁止执行命令）/ docker
    twin_provider: str = "fake"  # fake / compose / k8s
    ci: str = "fake"  # fake / jenkins / gitlab
    notifier: str = "log"  # 逗号分隔：log,email,feishu
    observability: str = "fake"  # fake / stack（Prometheus+ES+SkyWalking，各自独立启用）

    # 可观测后端（observability=stack 时按 URL 是否配置逐项启用，未配的项回退空实现）
    prometheus_url: str = ""  # 如 http://prometheus:9090
    # 部署记录（recent_deploys 工具）：PromQL 模板，{service} 会被替换；空 = 返回空列表
    deploy_query: str = ""
    elasticsearch_url: str = ""  # 如 http://es:9200
    es_logs_index: str = "logs-*"  # 日志所在 index pattern
    es_username: str = ""  # 可选 basic auth
    es_password: SecretStr = SecretStr("")
    skywalking_url: str = ""  # OAP GraphQL 端点，如 http://oap:12800/graphql

    # 定时日志扫描：worker 定期查 ES，把新的 ERROR/异常按指纹聚类后作为 LOG 信号上报。
    # 需要 observability=stack 且配置了 elasticsearch_url；项目可用 config.logscan 覆盖。
    logscan_enabled: bool = False
    logscan_interval_s: int = 300
    logscan_lookback_s: int = 600
    logscan_query: str = LOGSCAN_QUERY
    logscan_limit: int = 200  # 单次 ES 查询返回的行数上限
    logscan_max_signals: int = 10  # 每轮每个项目最多上报的异常簇数

    # local 代码托管：裸仓库所在目录
    git_root: Path = Path(".data/git")

    # GitHub App
    github_app_id: str = ""
    github_private_key: SecretStr = SecretStr("")  # PEM 内容；或用 github_private_key_file
    github_private_key_file: Path | None = None
    github_installation_id: str = ""
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: SecretStr = SecretStr("")

    # GitLab（自建实例把 base_url 指到实例根路径；api_url 空 = <base_url>/api/v4）
    # token 用项目/组/个人 access token，scope 需要 api；仓库推送需要 write_repository
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_api_url: str = ""
    gitlab_token: SecretStr = SecretStr("")
    # Webhook 的 Secret token（GitLab 侧填同一个值，请求头 X-Gitlab-Token）
    gitlab_webhook_secret: SecretStr = SecretStr("")
    # GitLab CI 孪生部署：流水线所在项目（如 group/twinfix-infra）与默认分支
    # 触发令牌留空时复用 gitlab_token；token 需要 api scope
    gitlab_ci_project: str = ""
    gitlab_ci_ref: str = "main"
    gitlab_ci_token: SecretStr = SecretStr("")

    # Alertmanager / Grafana 告警 webhook 的 Bearer token（Alertmanager 侧配 authorization.credentials）
    alert_webhook_secret: SecretStr = SecretStr("")
    # Jenkins 孪生部署回调的 Bearer token（Jenkinsfile 的 CALLBACK_TOKEN）
    jenkins_webhook_secret: SecretStr = SecretStr("")

    # Docker 沙箱：Workspace 是一个长驻容器，代码与依赖缓存都在命名卷里
    docker_host: str | None = None  # 默认用 DOCKER_HOST / 本机 socket
    sandbox_image: str = "maven:3.9.9-eclipse-temurin-21"
    # Python / Go / TypeScript / C# 项目默认沙箱镜像；Java 仍用 sandbox_image。
    # 项目可用 config.sandbox_image 覆盖。
    sandbox_image_python: str = "twinfix-sandbox-python:3.12"
    sandbox_image_go: str = "twinfix-sandbox-go:1.25"
    sandbox_image_typescript: str = "twinfix-sandbox-typescript:22"
    sandbox_image_csharp: str = "twinfix-sandbox-csharp:10.0"
    sandbox_network: str = "twinfix-sandbox"  # 只能访问依赖镜像站的出口网络
    sandbox_maven_settings: Path | None = None  # 挂载为 Maven 全局 settings.xml（镜像源）
    sandbox_dependency_volume: str = "twinfix-m2"  # 只读的依赖缓存（共享）
    sandbox_cpus: float = 2.0
    sandbox_memory_mb: int = 2048

    # Jenkins：只放行 twinfix-twin-* 任务，token 最小权限（docs/05 §凭据）
    jenkins_url: str = ""
    jenkins_username: str = ""
    jenkins_api_token: SecretStr = SecretStr("")
    jenkins_crumb_issuer: bool = True  # Jenkins 默认开 CSRF 防护；关闭则置 False

    # Compose 孪生环境
    twin_compose_file: Path | None = None  # 孪生环境 compose 模板
    twin_port_base: int = 31000
    twin_port_span: int = 10  # 每个孪生环境占用的端口数
    twin_max_concurrent: int = 4
    twin_host: str = "127.0.0.1"  # 孪生环境端口对外的地址（报告与 Agent 请求用）
    # 单个孪生服务容器的资源上限（JVM 按 MaxRAMPercentage 自适应堆）。低配机器可调小，
    # 例如 TWINFIX_TWIN_SERVICE_MEM_LIMIT=512m / TWINFIX_TWIN_SERVICE_CPUS=0.5
    twin_service_mem_limit: str = "768m"
    twin_service_cpus: float = 1.0
    twin_db_mem_limit: str = "256m"

    # Kubernetes 孪生环境（twin_provider=k8s）：镜像由 CI 预构建，Manifest 用 NodePort 暴露服务。
    # node_host 是 worker 可达的节点地址；kubeconfig / context 留空 = 用进程默认凭据。
    k8s_kubectl: str = "kubectl"
    k8s_kubeconfig: Path | None = None
    k8s_context: str = ""
    k8s_node_host: str = ""
    k8s_twin_manifest: Path | None = None  # 空 = deploy/k8s/twin.example.yaml
    k8s_default_image: str = ""
    k8s_rollout_timeout_s: int = 600

    # 飞书自定义机器人：webhook URL 由收件人（channel=feishu 的 address）携带，
    # 这里只有可选的签名校验密钥（机器人后台开启"签名校验"时必须配置）
    feishu_sign_secret: SecretStr = SecretStr("")

    # 邮件
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_sender: str = "twinfix@localhost"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_tls: bool = True

    # LLM
    llm_timeout_s: float = 180
    # 推理模型会先花数千 token 思考再给工具参数，4096 常被截断（finish_reason=length）
    llm_max_tokens: int = 16384
    # 推理模型思考深度（low / medium / high，OpenAI 兼容 reasoning_effort）；空 = 模型默认
    llm_reasoning_effort: str = ""
    # 每百万 token 价格（输入/输出），用于成本统计；0 表示不计费
    llm_price_in: float = 0.0
    llm_price_out: float = 0.0
    # Agent 循环的最大轮数（一次 LLM 调用 = 一轮）
    agent_max_turns: int = 60

    @property
    def demo_shop_dir(self) -> Path:
        return self.arena_demo_shop_dir or self.arena_cases_dir.parent / "demo-shop"

    @property
    def checkpoint_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
