"""项目多仓库配置。

`Project.repo` 仍是主仓库（修复、孪生、PR 都落在它上面），附加仓库通过项目配置声明：

    {
      "repos": [
        {"name": "inventory", "repo": "acme/inventory", "branch_map": {"test": "dev"}},
        {"name": "payment", "repo": "acme/payment"}
      ],
      "service_repos": {"inventory": "inventory", "payment": "payment"}
    }

附加仓库检出到工作区的 `_repos/<name>` 下，用于跨服务诊断与代码检索；
只有显式标记 `fixable: true` 的仓库才允许 Fixer 修改、分仓提交并开 PR，默认只读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from twinfix.domain.models import Project

REPO_ROOT_PREFIX = "_repos"
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


@dataclass(frozen=True)
class RepoSpec:
    name: str
    repo: str
    branch_map: dict[str, str] = field(default_factory=dict)
    primary: bool = False
    fixable: bool = False


def repo_slug(repo: str) -> str:
    slug = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return slug or "primary"


def repo_specs(project: Project) -> list[RepoSpec]:
    config: dict[str, Any] = project.config or {}
    primary_name = str(config.get("primary_repo_name") or repo_slug(project.repo))
    specs = [RepoSpec(primary_name, project.repo, dict(project.branch_map or {}), True, True)]
    seen = {primary_name, project.repo}
    raw = config.get("repos")
    if not isinstance(raw, list):
        return specs
    for item in raw:
        if not isinstance(item, dict):
            continue
        repo = str(item.get("repo") or "").strip()
        if not repo or repo in seen:
            continue
        name = str(item.get("name") or repo_slug(repo)).strip()
        if not name or name in seen:
            continue
        raw_branch_map = item.get("branch_map")
        branch_map = {str(k): str(v) for k, v in raw_branch_map.items()} if isinstance(raw_branch_map, dict) else {}
        fixable = item.get("fixable") is True
        specs.append(RepoSpec(name, repo, branch_map, False, fixable))
        seen.add(name)
        seen.add(repo)
    return specs


def repos_for_service(project: Project, service: str | None) -> list[RepoSpec]:
    specs = repo_specs(project)
    config: dict[str, Any] = project.config or {}
    mapping = config.get("service_repos")
    target = mapping.get(service) if service and isinstance(mapping, dict) else None
    targets = [target] if isinstance(target, str) else target if isinstance(target, list) else []
    selected: list[RepoSpec] = []
    for item in targets:
        for spec in specs:
            if (spec.name == item or spec.repo == item) and spec not in selected:
                selected.append(spec)
    return selected


def repo_for_service(project: Project, service: str | None) -> RepoSpec:
    selected = repos_for_service(project, service)
    return selected[0] if selected else repo_specs(project)[0]


def involved_specs(project: Project, service: str | None, meta: dict[str, Any] | None = None) -> list[RepoSpec]:
    """本次事故需要加载的仓库：主仓库 + 服务所属仓库 + 项目常驻仓库 + 事故 meta 声明的仓库。"""
    specs = repo_specs(project)
    wanted = [specs[0]]
    by_name = {spec.name: spec for spec in specs}
    by_repo = {spec.repo: spec for spec in specs}

    def add(spec: RepoSpec) -> None:
        if spec not in wanted:
            wanted.append(spec)

    for spec in repos_for_service(project, service):
        add(spec)
    load_repos = (project.config or {}).get("load_repos")
    for item in load_repos if isinstance(load_repos, list) else []:
        found = by_name.get(str(item)) or by_repo.get(str(item))
        if found is not None:
            add(found)
    for item in (meta or {}).get("repos") or []:
        found = by_name.get(str(item)) or by_repo.get(str(item))
        if found is not None:
            add(found)
    return wanted


def repo_root_prefix(spec: RepoSpec, primary: RepoSpec) -> str:
    return "" if spec.primary else f"{REPO_ROOT_PREFIX}/{spec.name}"


def repo_names(project: Project) -> list[str]:
    return [spec.name for spec in repo_specs(project)]


def validate_config(config: dict[str, Any]) -> list[str]:
    """校验 `repos` / `service_repos` / `primary_repo_name` 的形状；返回可读错误列表。"""
    errors: list[str] = []
    primary_name = config.get("primary_repo_name")
    if primary_name is not None and (not isinstance(primary_name, str) or not primary_name.strip()):
        errors.append("primary_repo_name 必须是非空字符串")
    raw_repos = config.get("repos")
    if raw_repos is not None:
        if not isinstance(raw_repos, list):
            errors.append("repos 必须是数组")
        else:
            names: set[str] = set()
            repos: set[str] = set()
            for index, item in enumerate(raw_repos):
                if not isinstance(item, dict):
                    errors.append(f"repos[{index}] 必须是对象")
                    continue
                repo = str(item.get("repo") or "").strip()
                name = str(item.get("name") or repo_slug(repo)).strip()
                if not _REPO_RE.match(repo):
                    errors.append(f"repos[{index}].repo 需为 owner/name")
                if name in names:
                    errors.append(f"repos[{index}].name 重复：{name}")
                if repo in repos:
                    errors.append(f"repos[{index}].repo 重复：{repo}")
                if isinstance(primary_name, str) and primary_name.strip() == name:
                    errors.append(f"repos[{index}].name 与主仓库名冲突：{name}")
                names.add(name)
                repos.add(repo)
                branch_map = item.get("branch_map")
                if branch_map is not None and not isinstance(branch_map, dict):
                    errors.append(f"repos[{index}].branch_map 必须是对象")
                if "fixable" in item and not isinstance(item["fixable"], bool):
                    errors.append(f"repos[{index}].fixable 必须是布尔值")
    raw_mapping = config.get("service_repos")
    if raw_mapping is not None:
        if not isinstance(raw_mapping, dict):
            errors.append("service_repos 必须是对象")
        else:
            for service, value in raw_mapping.items():
                values = [value] if isinstance(value, str) else value if isinstance(value, list) else None
                if values is None or any(not isinstance(v, str) or not v.strip() for v in values):
                    errors.append(f"service_repos[{service!r}] 必须是非空仓库名或仓库名数组")
    return errors
