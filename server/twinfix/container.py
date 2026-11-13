"""依赖装配。所有适配器在这里按配置实例化，其余代码只依赖接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from twinfix.adapters.ci import CIProvider, FakeCI
from twinfix.adapters.codehost import CodeHost, FakeCodeHost
from twinfix.adapters.compose_twin import ComposeTwinProvider, TwinTemplate
from twinfix.adapters.docker_sandbox import DockerSandbox
from twinfix.adapters.github import GitHubAppCodeHost
from twinfix.adapters.gitlab import GitLabCI, GitLabCodeHost
from twinfix.adapters.jenkins import JenkinsCI
from twinfix.adapters.k8s_twin import K8sTwinTemplate, KubernetesTwinProvider
from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.adapters.notify import EmailNotifier, FeishuNotifier, LogNotifier, Notifier
from twinfix.adapters.observability import FakeObservability, ObservabilityAdapter, StackObservability
from twinfix.adapters.sandbox import LocalSandbox, SandboxProvider
from twinfix.adapters.twin import FakeTwinProvider, TwinProvider
from twinfix.config import Settings
from twinfix.profiles.base import LanguageProfile
from twinfix.profiles.java import JavaProfile
from twinfix.workspace import RemoteSource, WorkspaceManager


@dataclass
class Adapters:
    observability: ObservabilityAdapter
    codehost: CodeHost
    ci: CIProvider
    twin: TwinProvider
    sandbox: SandboxProvider
    profile: LanguageProfile
    notifiers: list[Notifier] = field(default_factory=list)
    # fake codehost 不支持 remote()（沙箱里没有凭据也拿不到仓库），此时为 None：
    # _ensure_workspace / push_branch 退回直接调用 codehost.clone() / push_branch()。
    workspace_manager: WorkspaceManager | None = None


def build_adapters(settings: Settings) -> Adapters:
    """按配置实例化适配器；默认配置（codehost=fake 等）保持最小可运行行为。"""
    sandbox = _build_sandbox(settings)

    codehost: CodeHost
    source: RemoteSource | None = None  # 同时喂给 WorkspaceManager 与 ComposeTwinProvider
    if settings.codehost == "local":
        local_host = LocalGitCodeHost(settings.git_root)
        codehost = local_host
        source = local_host
    elif settings.codehost == "github":
        github_host = _build_github_host(settings)
        codehost = github_host
        source = github_host
    elif settings.codehost == "gitlab":
        gitlab_host = _build_gitlab_host(settings)
        codehost = gitlab_host
        source = gitlab_host
    elif settings.codehost == "fake":
        codehost = FakeCodeHost()
    else:
        raise ValueError(f"unknown codehost backend: {settings.codehost!r}")

    workspace_manager = WorkspaceManager(sandbox, source, settings.data_dir / "mirrors") if source is not None else None

    twin: TwinProvider
    if settings.twin_provider == "compose":
        if source is None:
            raise ValueError(
                "twin_provider=compose requires codehost=local, codehost=github or codehost=gitlab (fake 无 remote())"
            )
        default_template = _twin_template(settings)
        twin = ComposeTwinProvider(
            template=default_template,
            template_resolver=lambda overrides: resolve_twin_template(overrides, default_template),
            source=source,
            work_dir=settings.data_dir / "twins",
            mirror_dir=settings.data_dir / "mirrors",
            port_base=settings.twin_port_base,
            port_span=settings.twin_port_span,
            max_concurrent=settings.twin_max_concurrent,
            host=settings.twin_host,
            service_mem_limit=settings.twin_service_mem_limit,
            service_cpus=settings.twin_service_cpus,
            db_mem_limit=settings.twin_db_mem_limit,
        )
    elif settings.twin_provider == "fake":
        twin = FakeTwinProvider()
    elif settings.twin_provider == "k8s":
        k8s_template = _k8s_template(settings)
        twin = KubernetesTwinProvider(
            template=k8s_template,
            template_resolver=lambda overrides: resolve_k8s_twin_template(overrides, k8s_template),
            work_dir=settings.data_dir / "twins",
            node_host=settings.k8s_node_host,
            port_base=settings.twin_port_base,
            port_span=settings.twin_port_span,
            max_concurrent=settings.twin_max_concurrent,
            default_image=settings.k8s_default_image,
            kubectl=settings.k8s_kubectl,
            kubeconfig=settings.k8s_kubeconfig,
            context=settings.k8s_context,
            rollout_timeout_s=settings.k8s_rollout_timeout_s,
        )
    else:
        raise ValueError(f"unknown twin_provider backend: {settings.twin_provider!r}")

    return Adapters(
        observability=_build_observability(settings),
        codehost=codehost,
        ci=_build_ci(settings),
        twin=twin,
        sandbox=sandbox,
        profile=JavaProfile(),
        notifiers=_build_notifiers(settings),
        workspace_manager=workspace_manager,
    )


def _build_sandbox(settings: Settings) -> SandboxProvider:
    if settings.sandbox == "docker":
        maven_settings = (
            settings.sandbox_maven_settings.read_text(encoding="utf-8") if settings.sandbox_maven_settings else None
        )
        return DockerSandbox(
            image=settings.sandbox_image,
            network=settings.sandbox_network,
            dependency_volume=settings.sandbox_dependency_volume,
            maven_settings=maven_settings,
            cpus=settings.sandbox_cpus,
            memory_mb=settings.sandbox_memory_mb,
            base_url=settings.docker_host,
        )
    if settings.sandbox == "local":
        return LocalSandbox(settings.data_dir / "workspaces")
    raise ValueError(f"unknown sandbox backend: {settings.sandbox!r}")


def _build_ci(settings: Settings) -> CIProvider:
    if settings.ci == "jenkins":
        token = settings.jenkins_api_token.get_secret_value()
        if not (settings.jenkins_url and settings.jenkins_username and token):
            raise ValueError("ci=jenkins requires jenkins_url, jenkins_username and jenkins_api_token")
        return JenkinsCI(
            base_url=settings.jenkins_url,
            username=settings.jenkins_username,
            api_token=token,
            crumb_issuer=settings.jenkins_crumb_issuer,
        )
    if settings.ci == "gitlab":
        token = settings.gitlab_ci_token.get_secret_value() or settings.gitlab_token.get_secret_value()
        if not (settings.gitlab_ci_project and token):
            raise ValueError("ci=gitlab requires gitlab_ci_project and a gitlab token")
        return GitLabCI(
            base_url=settings.gitlab_base_url,
            api_url=settings.gitlab_api_url,
            project=settings.gitlab_ci_project,
            token=token,
            ref=settings.gitlab_ci_ref,
        )
    if settings.ci == "fake":
        return FakeCI()
    raise ValueError(f"unknown ci backend: {settings.ci!r}")


def _build_github_host(settings: Settings) -> GitHubAppCodeHost:
    private_key = settings.github_private_key.get_secret_value()
    if not private_key and settings.github_private_key_file:
        private_key = settings.github_private_key_file.read_text(encoding="utf-8")
    if not (settings.github_app_id and private_key and settings.github_installation_id):
        raise ValueError(
            "codehost=github requires github_app_id, github_installation_id and "
            "github_private_key (or github_private_key_file)"
        )
    return GitHubAppCodeHost(
        app_id=settings.github_app_id,
        private_key=private_key,
        installation_id=settings.github_installation_id,
        api_url=settings.github_api_url,
    )


def _build_gitlab_host(settings: Settings) -> GitLabCodeHost:
    token = settings.gitlab_token.get_secret_value()
    if not (settings.gitlab_base_url and token):
        raise ValueError("codehost=gitlab requires gitlab_base_url and gitlab_token")
    return GitLabCodeHost(
        base_url=settings.gitlab_base_url,
        api_url=settings.gitlab_api_url,
        token=token,
    )


def resolve_twin_template(overrides: dict[str, Any], default: TwinTemplate) -> TwinTemplate:
    """把 project.config["twin"] 合并到默认模板上，得到该项目自己的孪生模板。

    只允许字段级覆盖：directory / compose_file / services / extra_files。
    相对路径一律相对仓库根解析，和默认模板的定位方式一致（与进程工作目录无关）。
    """
    directory = _repo_path(overrides.get("directory")) or default.directory
    compose_file = str(overrides.get("compose_file") or default.compose_file)
    services = (
        tuple(str(s) for s in overrides["services"]) if overrides.get("services") is not None else default.services
    )
    if not services:
        raise ValueError("twin template needs at least one service")
    extra_files = default.extra_files
    if overrides.get("extra_files") is not None:
        extra_files = tuple(p for p in (_repo_path(f) for f in overrides["extra_files"]) if p is not None)
    return TwinTemplate(directory=directory, compose_file=compose_file, services=services, extra_files=extra_files)


def _repo_path(raw: Any) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (_REPO_ROOT / path).resolve()


def _twin_template(settings: Settings) -> TwinTemplate:
    if settings.twin_compose_file is not None:
        return TwinTemplate(directory=settings.twin_compose_file.parent, compose_file=settings.twin_compose_file.name)
    # 默认模板相对仓库根（server/ 的上一级）定位，与工作目录无关——
    # 进程从任何地方启动都能找到 arena/twin
    # Dockerfile.service 需要 settings.xml（依赖镜像源）在构建上下文里
    return TwinTemplate(
        directory=_REPO_ROOT / "arena" / "twin",
        extra_files=(_REPO_ROOT / "deploy" / "maven" / "settings.xml",),
    )


def resolve_k8s_twin_template(overrides: dict[str, Any], default: K8sTwinTemplate) -> K8sTwinTemplate:
    """项目配置里的 K8s 孪生模板覆盖：directory / manifest_file / services / health_path。"""
    directory = _repo_path(overrides.get("directory")) or default.directory
    manifest_file = str(overrides.get("manifest_file") or default.manifest_file)
    services = (
        tuple(str(s) for s in overrides["services"]) if overrides.get("services") is not None else default.services
    )
    if not services:
        raise ValueError("k8s twin template needs at least one service")
    health_path = str(overrides.get("health_path") or default.health_path)
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    return K8sTwinTemplate(directory=directory, manifest_file=manifest_file, services=services, health_path=health_path)


def _k8s_template(settings: Settings) -> K8sTwinTemplate:
    if settings.k8s_twin_manifest is not None:
        return K8sTwinTemplate(
            directory=settings.k8s_twin_manifest.parent, manifest_file=settings.k8s_twin_manifest.name
        )
    example = _REPO_ROOT / "deploy" / "k8s" / "twin.example.yaml"
    return K8sTwinTemplate(directory=example.parent, manifest_file=example.name)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _build_observability(settings: Settings) -> ObservabilityAdapter:
    if settings.observability == "stack":
        if not (settings.prometheus_url or settings.elasticsearch_url or settings.skywalking_url):
            raise ValueError(
                "observability=stack requires at least one of prometheus_url / elasticsearch_url / skywalking_url"
            )
        return StackObservability(
            prometheus_url=settings.prometheus_url,
            elasticsearch_url=settings.elasticsearch_url,
            es_logs_index=settings.es_logs_index,
            es_username=settings.es_username,
            es_password=settings.es_password.get_secret_value(),
            skywalking_url=settings.skywalking_url,
            deploy_query=settings.deploy_query,
        )
    if settings.observability == "fake":
        return FakeObservability()
    raise ValueError(f"unknown observability backend: {settings.observability!r}")


def _build_notifiers(settings: Settings) -> list[Notifier]:
    notifiers: list[Notifier] = []
    for raw in settings.notifier.split(","):
        channel = raw.strip()
        if not channel:
            continue
        if channel == "log":
            notifiers.append(LogNotifier())
        elif channel == "feishu":
            secret = settings.feishu_sign_secret.get_secret_value()
            notifiers.append(FeishuNotifier(sign_secret=secret or None, console_url=settings.console_url))
        elif channel == "email":
            notifiers.append(
                EmailNotifier(
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    sender=settings.smtp_sender,
                    username=settings.smtp_username or None,
                    password=settings.smtp_password.get_secret_value() or None,
                    use_tls=settings.smtp_tls,
                )
            )
        else:
            raise ValueError(f"unsupported notifier channel: {channel!r}")
    return notifiers
