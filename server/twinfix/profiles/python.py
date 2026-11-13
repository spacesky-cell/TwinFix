"""PythonProfile：pytest 构建/测试、JUnit 报告解析、Python traceback 解析。"""

from __future__ import annotations

import re

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import CodeLocation, RuntimeProbe, StackFrame, TestReport, TestSelector
from twinfix.profiles.commands import CommandOverrides, run_custom_build, run_custom_tests
from twinfix.profiles.junit import parse_junit

_FILE_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<symbol>.+?)\s*$')
_PYTEST_FRAME = re.compile(r"^(?P<file>[\w./\\-]+\.py):(?P<line>\d+): in (?P<symbol>[\w.<>]+)")

_REPRO_TEMPLATE = '''\
"""TwinFix 复现用例：{incident_id}。修复前必须失败，修复后必须通过。"""


def test_reproduces_{slug}():
    # given

    # when

    # then
    assert ...
'''


class PythonProfile:
    name = "python"

    def __init__(
        self,
        project_packages: tuple[str, ...] = (),
        interpreter: str = "python",
        commands: CommandOverrides | None = None,
    ) -> None:
        self.project_packages = project_packages
        # 沙箱里通常是 python；本机测试可传 venv 解释器。项目可用 config.python_executable 覆盖。
        self.interpreter = interpreter
        self.commands = commands

    def detect(self, root_files: list[str]) -> bool:
        markers = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "poetry.lock"}
        return any(f in markers for f in root_files)

    def build_command(self, module: str | None) -> list[str]:
        # 沙箱里没有安装项目依赖时，至少做一次全量语法检查；真正的依赖测试交给 run_tests
        return [self.interpreter, "-m", "compileall", "-q", module or "."]

    def test_command(self, selector: TestSelector) -> list[str]:
        cmd = [
            self.interpreter,
            "-B",  # 不写 __pycache__：复现目录里的缓存不应污染工作区/补丁
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "--junitxml=.twinfix/pytest.xml",
        ]
        selected = [*selector.classes, *(m.replace("#", "::") for m in selector.methods)]
        return [*cmd, *selected]

    async def build(
        self, sandbox: SandboxProvider, ws: Workspace, module: str | None, cwd: str | None = None
    ) -> CommandResult:
        custom = await run_custom_build(self.commands, sandbox, ws, cwd)
        if custom is not None:
            return custom
        return await sandbox.exec(ws, self.build_command(module), timeout=600, cwd=cwd)

    async def run_tests(
        self,
        sandbox: SandboxProvider,
        ws: Workspace,
        selector: TestSelector,
        cwd: str | None = None,
    ) -> TestReport:
        custom = await run_custom_tests(self.commands, sandbox, ws, selector, cwd)
        if custom is not None:
            return custom
        report_path = f"{cwd}/.twinfix/pytest.xml" if cwd else ".twinfix/pytest.xml"
        # junitxml 的父目录必须存在；.twinfix 已被 git 排除，不会污染补丁
        await sandbox.write_file(ws, report_path, "")
        result = await sandbox.exec(ws, self.test_command(selector), timeout=1800, cwd=cwd)
        try:
            xml_text = await sandbox.read_file(ws, report_path)
        except FileNotFoundError:
            return TestReport(command=result, cases=[])
        return TestReport(command=result, cases=parse_pytest_junit(xml_text))

    def parse_stacktrace(self, text: str) -> list[StackFrame]:
        frames: list[StackFrame] = []
        for raw in text.splitlines():
            m = _FILE_FRAME.match(raw) or _PYTEST_FRAME.match(raw)
            if not m:
                continue
            path = m.group("file")
            symbol = m.group("symbol").strip()
            frames.append(
                StackFrame(
                    symbol=f"{path}:{symbol}" if symbol else path,
                    file=path,
                    line=int(m.group("line")),
                    is_project=self.is_project_path(path),
                )
            )
        return frames

    def is_project_path(self, path: str) -> bool:
        normalized = f"/{path.replace('\\', '/').lstrip('/')}"
        if self.project_packages:
            return any(f"/{p.replace('.', '/')}/" in normalized for p in self.project_packages)
        return "/site-packages/" not in normalized and "/lib/python" not in normalized

    def frame_to_location(self, frame: StackFrame, files: list[str]) -> CodeLocation | None:
        if frame.file is None:
            return None
        target = frame.file.replace("\\", "/")
        candidates = [f for f in files if target.endswith(f) or f.endswith(target.lstrip("/"))]
        if not candidates:
            basename = target.rsplit("/", 1)[-1]
            candidates = [f for f in files if f.rsplit("/", 1)[-1] == basename]
        if not candidates:
            return None
        path = max(candidates, key=len)
        return CodeLocation(path=path, line=frame.line, symbol=frame.symbol)

    def test_paths(self) -> list[str]:
        return ["**/tests/**", "**/test_*.py", "**/*_test.py"]

    def case_matches(self, selector: str, case_name: str) -> bool:
        """pytest selector（path::test）与 JUnit 用例名（module#test）按点号归一后比较。"""
        normalized_selector = selector.replace("\\", "/").replace("::", ".")
        normalized_selector = re.sub(r"\.py(?=\.|$)", "", normalized_selector)
        normalized_selector = normalized_selector.replace("/", ".").lstrip(".")
        normalized_case = case_name.replace("::", ".").replace("#", ".")
        return normalized_selector in normalized_case

    def source_suffixes(self) -> tuple[str, ...]:
        return (".py",)

    def repro_test_template(self) -> str:
        return _REPRO_TEMPLATE

    def runtime_probe(self) -> RuntimeProbe | None:
        return None


# 兼容旧导入：pytest 与 Node 共用同一份 JUnit 解析
parse_pytest_junit = parse_junit
