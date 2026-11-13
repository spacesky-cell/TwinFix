"""arena/cases/<id>/case.yaml 的类型化模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from twinfix.domain.enums import Env, Severity

ExpectedOutcome = Literal["fixed", "NEEDS_HUMAN", "NOT_BUG", "ALREADY_FIXED", "CANNOT_REPRODUCE"]
Language = Literal["java", "python", "go", "typescript", "csharp"]


class Inject(BaseModel):
    # none：不改代码（运行时/环境类故障、非缺陷），只上报信号考察诊断结论
    type: Literal["patch", "none"]
    file: str = ""
    branch: str = "dev"
    message: str = ""
    services: list[str] = Field(min_length=1)


class TriggerRequest(BaseModel):
    service: str
    method: str = "GET"
    path: str
    body: Any = None
    concurrent: int = 1


class Trigger(BaseModel):
    requests: list[TriggerRequest] = Field(default_factory=list)


class Signal(BaseModel):
    title: str
    service: str | None = None
    env: Env = Env.TEST
    severity: Severity | None = None
    stacktrace: str | None = None
    message: str | None = None


class Expect(BaseModel):
    outcome: ExpectedOutcome
    root_cause_locations: list[str] = Field(default_factory=list)
    root_cause_keywords: list[str] = Field(default_factory=list)


class OnReview(BaseModel):
    """事故停在待评审时的剧本（模拟评审期间的人为动作）。"""

    # 先往目标分支推一个提交（例如别人抢先修好了），再批准合并
    push_patch: str | None = None
    push_message: str = "fix: hotfix by teammate"
    approve: bool = True


class Case(BaseModel):
    id: str
    title: str
    category: str
    description: str = ""
    # 语言决定用哪套测试运行器（Maven / pytest）、语言插件与沙箱镜像
    language: Language = "java"
    # 基线仓库目录名，相对 arena/；Java 用例默认 demo-shop，Python 用例用 demo-python
    seed: str = "demo-shop"
    # 注入项目时一并写入 Project.config（孪生模板、受保护路径、项目包名等）
    project_config: dict[str, Any] = Field(default_factory=dict)
    inject: Inject
    trigger: Trigger = Field(default_factory=Trigger)
    signal: Signal
    expect: Expect
    hidden_tests: list[str] = Field(default_factory=list)
    on_review: OnReview | None = None

    # 用例目录；不来自 yaml
    directory: Path = Field(default=Path(), exclude=True)

    @property
    def module(self) -> str:
        """隐藏测试所在模块：注入的第一个服务。"""
        return self.inject.services[0]

    @property
    def has_patch(self) -> bool:
        return self.inject.type == "patch"

    def patch_text(self) -> str:
        return (self.directory / self.inject.file).read_text(encoding="utf-8")

    def hidden_paths(self) -> list[Path]:
        return [self.directory / h for h in self.hidden_tests]

    def seed_path(self, arena_root: Path, demo_shop: Path | None = None) -> Path:
        """基线仓库目录。demo-shop 允许被部署配置覆盖（TWINFIX_ARENA_DEMO_SHOP_DIR）。"""
        if self.seed == "demo-shop" and demo_shop is not None:
            return demo_shop
        return arena_root / self.seed


def load_case(directory: Path) -> Case:
    raw = yaml.safe_load((directory / "case.yaml").read_text(encoding="utf-8"))
    case = Case.model_validate(raw)
    if case.id != directory.name:
        raise ValueError(f"case id {case.id!r} does not match directory {directory.name!r}")
    case.directory = directory
    if case.has_patch and not (case.inject.file and case.inject.message):
        raise ValueError(f"{case.id}: inject.type=patch requires file and message")
    seed = Path(case.seed)
    if seed.is_absolute() or ".." in seed.parts:
        raise ValueError(f"{case.id}: seed must be a directory name under arena/")
    if case.expect.outcome == "fixed" and not (case.has_patch and case.hidden_tests):
        raise ValueError(f"{case.id}: expect.outcome=fixed requires a patch and hidden tests")
    return case


def load_cases(root: Path, only: set[str] | None = None) -> list[Case]:
    dirs = sorted(d for d in root.iterdir() if (d / "case.yaml").is_file())
    return [load_case(d) for d in dirs if not only or d.name in only]
