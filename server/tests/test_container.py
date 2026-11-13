"""build_adapters() 的分支装配：每个 backend 配置项映射到正确实现，非法组合快速报错。

不依赖数据库与外部服务：codehost=github 的凭据校验在建立网络连接之前完成，
sandbox=docker 需要本机 Docker，单独标记为 integration。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from twinfix.adapters.ci import FakeCI
from twinfix.adapters.codehost import FakeCodeHost
from twinfix.adapters.compose_twin import ComposeTwinProvider, TwinTemplate
from twinfix.adapters.github import GitHubAppCodeHost
from twinfix.adapters.gitlab import GitLabCI, GitLabCodeHost
from twinfix.adapters.jenkins import JenkinsCI
from twinfix.adapters.k8s_twin import K8sTwinTemplate, KubernetesTwinProvider
from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.adapters.notify import EmailNotifier, FeishuNotifier, LogNotifier
from twinfix.adapters.sandbox import LocalSandbox
from twinfix.adapters.twin import FakeTwinProvider
from twinfix.adapters.types import TwinSpec
from twinfix.config import Settings
from twinfix.container import _REPO_ROOT, build_adapters, resolve_k8s_twin_template, resolve_twin_template


class _UnusedSource:
    """resolve 阶段不触发 RemoteSource；只为满足构造函数类型。"""

    async def remote(self, repo: str):  # pragma: no cover - 不会被调用
        raise AssertionError("not used")


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(data_dir=tmp_path / "data", **kwargs)


class TestCodehost:
    def test_fake_has_no_workspace_manager(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path))
        assert isinstance(adapters.codehost, FakeCodeHost)
        assert isinstance(adapters.sandbox, LocalSandbox)
        assert isinstance(adapters.twin, FakeTwinProvider)
        # fake 无 remote()，push/clone 退回 codehost 直接实现
        assert adapters.workspace_manager is None

    def test_local_gets_workspace_manager(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, codehost="local", git_root=tmp_path / "git"))
        assert isinstance(adapters.codehost, LocalGitCodeHost)
        assert adapters.workspace_manager is not None

    def test_github_requires_credentials(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="codehost=github requires"):
            build_adapters(_settings(tmp_path, codehost="github"))

    def test_github_from_private_key_file(self, tmp_path) -> None:
        key_file = tmp_path / "key.pem"
        key_file.write_text("PEM", encoding="utf-8")
        adapters = build_adapters(
            _settings(
                tmp_path,
                codehost="github",
                github_app_id="1",
                github_private_key_file=key_file,
                github_installation_id="2",
            )
        )
        assert isinstance(adapters.codehost, GitHubAppCodeHost)
        assert adapters.workspace_manager is not None

    def test_github_inline_key(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                codehost="github",
                github_app_id="1",
                github_private_key=SecretStr("PEM"),
                github_installation_id="2",
            )
        )
        assert isinstance(adapters.codehost, GitHubAppCodeHost)

    def test_gitlab_requires_token(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="codehost=gitlab requires"):
            build_adapters(_settings(tmp_path, codehost="gitlab"))

    def test_gitlab_backend(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                codehost="gitlab",
                gitlab_base_url="https://gitlab.test",
                gitlab_token=SecretStr("glpat-token"),
            )
        )
        assert isinstance(adapters.codehost, GitLabCodeHost)
        assert adapters.workspace_manager is not None

    def test_unknown_codehost(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unknown codehost backend: 'bogus'"):
            build_adapters(_settings(tmp_path, codehost="bogus"))


class TestSandbox:
    def test_unknown_sandbox(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unknown sandbox backend: 'bogus'"):
            build_adapters(_settings(tmp_path, sandbox="bogus"))

    @pytest.mark.integration
    def test_docker_sandbox(self, tmp_path) -> None:
        from twinfix.adapters.docker_sandbox import DockerSandbox

        try:
            adapters = build_adapters(_settings(tmp_path, sandbox="docker"))
        except Exception as exc:  # docker daemon 不可用时跳过而非失败
            pytest.skip(f"docker unavailable: {exc}")
        assert isinstance(adapters.sandbox, DockerSandbox)


class TestCI:
    def test_default_is_fake(self, tmp_path) -> None:
        assert isinstance(build_adapters(_settings(tmp_path)).ci, FakeCI)

    def test_jenkins_requires_credentials(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="ci=jenkins requires"):
            build_adapters(_settings(tmp_path, ci="jenkins"))

    def test_jenkins_backend(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                ci="jenkins",
                jenkins_url="http://jenkins.test",
                jenkins_username="bot",
                jenkins_api_token=SecretStr("tok"),
            )
        )
        assert isinstance(adapters.ci, JenkinsCI)
        assert adapters.ci.base_url == "http://jenkins.test"

    def test_gitlab_requires_project_and_token(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="ci=gitlab requires"):
            build_adapters(_settings(tmp_path, ci="gitlab"))

    def test_gitlab_ci_backend(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                ci="gitlab",
                gitlab_token=SecretStr("glpat-token"),
                gitlab_ci_project="ci/twin-deploy",
            )
        )
        assert isinstance(adapters.ci, GitLabCI)
        assert adapters.ci.project == "ci%2Ftwin-deploy"

    def test_unknown_ci(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unknown ci backend: 'bogus'"):
            build_adapters(_settings(tmp_path, ci="bogus"))


class TestTwin:
    def test_compose_requires_real_codehost(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="requires codehost=local"):
            build_adapters(_settings(tmp_path, twin_provider="compose", codehost="fake"))

    def test_compose_with_local_codehost(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(tmp_path, twin_provider="compose", codehost="local", git_root=tmp_path / "git")
        )
        assert isinstance(adapters.twin, ComposeTwinProvider)

    def test_compose_resource_limits_reach_provider(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                twin_provider="compose",
                codehost="local",
                git_root=tmp_path / "git",
                twin_service_mem_limit="512m",
                twin_service_cpus=0.5,
                twin_db_mem_limit="192m",
            )
        )
        assert isinstance(adapters.twin, ComposeTwinProvider)
        assert adapters.twin.service_mem_limit == "512m"
        assert adapters.twin.service_cpus == 0.5
        assert adapters.twin.db_mem_limit == "192m"

    def test_compose_template_from_file(self, tmp_path) -> None:
        compose = tmp_path / "my-twin.yml"
        compose.write_text("services: {}", encoding="utf-8")
        adapters = build_adapters(
            _settings(
                tmp_path,
                twin_provider="compose",
                codehost="local",
                git_root=tmp_path / "git",
                twin_compose_file=compose,
            )
        )
        assert isinstance(adapters.twin, ComposeTwinProvider)
        assert adapters.twin.template.directory == compose.parent
        assert adapters.twin.template.compose_file == "my-twin.yml"

    def test_unknown_twin_provider(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unknown twin_provider backend: 'bogus'"):
            build_adapters(_settings(tmp_path, twin_provider="bogus"))

    def test_k8s_requires_node_host(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="TWINFIX_K8S_NODE_HOST"):
            build_adapters(_settings(tmp_path, twin_provider="k8s"))

    def test_k8s_backend(self, tmp_path) -> None:
        manifest = tmp_path / "twin.yaml"
        manifest.write_text("kind: Service\n", encoding="utf-8")
        adapters = build_adapters(
            _settings(
                tmp_path,
                twin_provider="k8s",
                k8s_node_host="node1",
                k8s_twin_manifest=manifest,
                k8s_context="kind-test",
                k8s_kubeconfig=tmp_path / "kubeconfig",
                k8s_default_image="registry/twin:test",
            )
        )
        assert isinstance(adapters.twin, KubernetesTwinProvider)
        assert adapters.twin.template.directory == manifest.parent
        assert adapters.twin.template.manifest_file == "twin.yaml"
        assert adapters.twin.node_host == "node1"
        assert adapters.twin.default_image == "registry/twin:test"

    def test_k8s_default_template_is_shipped_example(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, twin_provider="k8s", k8s_node_host="node1"))
        assert isinstance(adapters.twin, KubernetesTwinProvider)
        assert adapters.twin.template.manifest_file == "twin.example.yaml"
        assert adapters.twin.template.directory == _REPO_ROOT / "deploy" / "k8s"


class TestTwinTemplateOverrides:
    """项目配置的项目级孪生模板：只覆盖给出的字段，相对路径相对仓库根。"""

    def test_k8s_template_overrides(self, tmp_path) -> None:
        default = K8sTwinTemplate(directory=tmp_path / "twin", services=("order",))
        resolved = resolve_k8s_twin_template(
            {"manifest_file": "custom.yaml", "services": ["api"], "health_path": "healthz"}, default
        )
        assert resolved.directory == default.directory
        assert resolved.manifest_file == "custom.yaml"
        assert resolved.services == ("api",)
        assert resolved.health_path == "/healthz"

    def _default(self, tmp_path) -> TwinTemplate:
        return TwinTemplate(
            directory=tmp_path / "twin",
            compose_file="compose.twin.yml",
            services=("order", "inventory"),
            extra_files=(tmp_path / "settings.xml",),
        )

    def test_empty_overrides_keep_default(self, tmp_path) -> None:
        default = self._default(tmp_path)
        assert resolve_twin_template({}, default) == default

    def test_partial_override_keeps_other_fields(self, tmp_path) -> None:
        default = self._default(tmp_path)
        resolved = resolve_twin_template({"services": ["billing"]}, default)
        assert resolved.services == ("billing",)
        assert resolved.directory == default.directory
        assert resolved.compose_file == "compose.twin.yml"
        assert resolved.extra_files == default.extra_files

    def test_relative_paths_resolve_from_repo_root(self, tmp_path) -> None:
        default = self._default(tmp_path)
        resolved = resolve_twin_template(
            {"directory": "arena/billing", "compose_file": "billing.twin.yml", "extra_files": ["deploy/x.xml"]},
            default,
        )
        assert resolved.directory == (_REPO_ROOT / "arena" / "billing").resolve()
        assert resolved.compose_file == "billing.twin.yml"
        assert resolved.extra_files == ((_REPO_ROOT / "deploy" / "x.xml").resolve(),)

    def test_explicit_empty_extra_files_clears(self, tmp_path) -> None:
        resolved = resolve_twin_template({"extra_files": []}, self._default(tmp_path))
        assert resolved.extra_files == ()

    def test_rejects_empty_services(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="at least one service"):
            resolve_twin_template({"services": []}, self._default(tmp_path))

    def test_provider_uses_project_template_only_when_given(self, tmp_path) -> None:
        default = self._default(tmp_path)
        project_template = TwinTemplate(directory=tmp_path / "other", services=("billing",))
        provider = ComposeTwinProvider(
            template=default,
            template_resolver=lambda overrides: project_template,
            source=_UnusedSource(),
            work_dir=tmp_path / "twins",
            mirror_dir=tmp_path / "mirrors",
            port_base=18000,
            port_span=10,
            max_concurrent=2,
        )
        empty = TwinSpec("i1", "org/repo", "HEAD")
        assert provider._template_for(empty) is default
        with_project = TwinSpec("i1", "org/repo", "HEAD", template={"services": ["billing"]})
        assert provider._template_for(with_project) is project_template

    def test_provider_resolves_through_container_resolver(self, tmp_path) -> None:
        default = self._default(tmp_path)
        provider = ComposeTwinProvider(
            template=default,
            template_resolver=lambda overrides: resolve_twin_template(overrides, default),
            source=_UnusedSource(),
            work_dir=tmp_path / "twins",
            mirror_dir=tmp_path / "mirrors",
            port_base=18000,
            port_span=10,
            max_concurrent=2,
        )
        spec = TwinSpec("i1", "org/repo", "HEAD", template={"services": ["billing", "ledger"]})
        assert provider._template_for(spec).services == ("billing", "ledger")


class TestNotifiers:
    def test_log_and_email_in_order(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, notifier="log, email"))
        assert [n.channel for n in adapters.notifiers] == ["log", "email"]
        assert isinstance(adapters.notifiers[0], LogNotifier)
        assert isinstance(adapters.notifiers[1], EmailNotifier)

    def test_email_uses_smtp_settings(self, tmp_path) -> None:
        adapters = build_adapters(
            _settings(
                tmp_path,
                notifier="email",
                smtp_host="smtp.test",
                smtp_port=2525,
                smtp_sender="bot@test",
                smtp_username="u",
                smtp_password=SecretStr("p"),
                smtp_tls=False,
            )
        )
        mail = adapters.notifiers[0]
        assert isinstance(mail, EmailNotifier)
        assert (mail.host, mail.port, mail.sender) == ("smtp.test", 2525, "bot@test")
        assert mail.username == "u" and mail.password == "p" and mail.use_tls is False

    def test_empty_channels_are_skipped(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, notifier="log,,"))
        assert [n.channel for n in adapters.notifiers] == ["log"]

    def test_feishu_channel(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, notifier="feishu"))
        feishu = adapters.notifiers[0]
        assert isinstance(feishu, FeishuNotifier) and feishu.channel == "feishu"
        assert feishu._sign_secret is None

    def test_feishu_with_sign_secret(self, tmp_path) -> None:
        adapters = build_adapters(_settings(tmp_path, notifier="feishu", feishu_sign_secret=SecretStr("sec")))
        assert isinstance(adapters.notifiers[0], FeishuNotifier)
        assert adapters.notifiers[0]._sign_secret == "sec"

    def test_unknown_channel(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="unsupported notifier channel: 'wechat'"):
            build_adapters(_settings(tmp_path, notifier="wechat"))


class TestTwinSlots:
    def test_claim_slot_reaps_projects_without_containers(self, tmp_path, monkeypatch) -> None:
        provider = ComposeTwinProvider(
            template=TwinTemplate(
                directory=tmp_path / "twin",
                compose_file="compose.twin.yml",
                services=("order",),
            ),
            source=_UnusedSource(),
            work_dir=tmp_path / "twins",
            mirror_dir=tmp_path / "mirrors",
            port_base=18000,
            port_span=10,
            max_concurrent=2,
        )
        work = provider.work_dir
        for slot, owner in enumerate(("twinfix-a", "twinfix-b")):
            (work / owner).mkdir(parents=True)
            (work / owner / "slot").write_text(str(slot))
        monkeypatch.setattr(
            ComposeTwinProvider,
            "_project_exists",
            staticmethod(lambda name: False),
        )
        claimed = provider._claim_slot("twinfix-new")
        assert claimed == 0
        assert (work / "twinfix-new" / "slot").read_text() == "0"
        assert not (work / "twinfix-a").exists()
