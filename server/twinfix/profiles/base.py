"""语言插件接口。核心代码不出现任何语言专用逻辑，全部经由 LanguageProfile。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace


@dataclass(frozen=True)
class StackFrame:
    symbol: str  # 例如 com.acme.order.OrderService.create
    file: str | None
    line: int | None
    is_project: bool = False


@dataclass(frozen=True)
class CodeLocation:
    path: str
    line: int | None
    symbol: str | None = None


@dataclass(frozen=True)
class TestSelector:
    __test__ = False  # 不是 pytest 测试类

    classes: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)  # "Class#method"

    @property
    def is_all(self) -> bool:
        return not self.classes and not self.methods


@dataclass(frozen=True)
class TestCase:
    name: str
    status: str  # passed / failed / error / skipped
    message: str = ""


@dataclass(frozen=True)
class TestReport:
    command: CommandResult
    cases: list[TestCase]

    @property
    def failed(self) -> list[TestCase]:
        return [c for c in self.cases if c.status in ("failed", "error")]


class RuntimeProbe(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def allowed_commands(self) -> frozenset[str]: ...


class LanguageProfile(Protocol):
    name: str

    def detect(self, root_files: list[str]) -> bool: ...
    async def build(
        self, sandbox: SandboxProvider, ws: Workspace, module: str | None, cwd: str | None = None
    ) -> CommandResult: ...
    async def run_tests(
        self,
        sandbox: SandboxProvider,
        ws: Workspace,
        selector: TestSelector,
        cwd: str | None = None,
    ) -> TestReport: ...
    def parse_stacktrace(self, text: str) -> list[StackFrame]: ...
    def frame_to_location(self, frame: StackFrame, files: list[str]) -> CodeLocation | None: ...
    def test_paths(self) -> list[str]: ...
    def case_matches(self, selector: str, case_name: str) -> bool: ...
    def source_suffixes(self) -> tuple[str, ...]: ...
    def repro_test_template(self) -> str: ...
    def runtime_probe(self) -> RuntimeProbe | None: ...
