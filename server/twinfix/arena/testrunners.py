"""按用例语言选择测试运行器：隐藏测试安装 + 执行共用同一套接口。

Java 走 Maven，Python 走 pytest。运行器只负责"评测机上的判分"，
真正的用例注入与 Agent 工作区都在别处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from twinfix.arena.hidden import (
    CSharp,
    Go,
    Maven,
    Node,
    Pytest,
    install_csharp_hidden_tests,
    install_go_hidden_tests,
    install_hidden_tests,
    install_node_hidden_tests,
    install_pytest_hidden_tests,
)


@dataclass(frozen=True)
class TestResult:
    passed: bool
    summary: str = ""


class TestRunner(Protocol):
    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]: ...

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult: ...


@dataclass(frozen=True)
class MavenRunner:
    maven: Maven

    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]:
        return install_hidden_tests(repo, module, sources)

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult:
        result = await self.maven.test(repo, module, tests)
        return TestResult(passed=result.passed, summary=result.summary)


@dataclass(frozen=True)
class PytestRunner:
    pytest: Pytest = field(default_factory=Pytest)

    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]:
        return install_pytest_hidden_tests(repo, module, sources)

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult:
        result = await self.pytest.test(repo, module, tests)
        return TestResult(passed=result.passed, summary=result.summary)


@dataclass(frozen=True)
class GoRunner:
    go: Go = field(default_factory=Go)

    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]:
        return install_go_hidden_tests(repo, module, sources)

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult:
        result = await self.go.test(repo, module, tests)
        return TestResult(passed=result.passed, summary=result.summary)


@dataclass(frozen=True)
class NodeRunner:
    node: Node = field(default_factory=Node)

    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]:
        return install_node_hidden_tests(repo, module, sources)

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult:
        result = await self.node.test(repo, module, tests)
        return TestResult(passed=result.passed, summary=result.summary)


@dataclass(frozen=True)
class CSharpRunner:
    csharp: CSharp = field(default_factory=CSharp)

    def install(self, repo: Path, module: str, sources: list[Path]) -> list[str]:
        return install_csharp_hidden_tests(repo, module, sources)

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> TestResult:
        result = await self.csharp.test(repo, module, tests)
        return TestResult(passed=result.passed, summary=result.summary)


def runner_for(
    language: str,
    *,
    maven: Maven,
    pytest: Pytest | None = None,
    go_runner: Go | None = None,
    node_runner: Node | None = None,
    csharp_runner: CSharp | None = None,
) -> TestRunner:
    if language == "python":
        return PytestRunner(pytest or Pytest())
    if language == "go":
        return GoRunner(go_runner or Go())
    if language == "typescript":
        return NodeRunner(node_runner or Node())
    if language == "csharp":
        return CSharpRunner(csharp_runner or CSharp())
    return MavenRunner(maven)
