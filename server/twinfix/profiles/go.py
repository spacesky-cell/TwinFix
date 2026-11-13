"""GoProfile：go build / go test、`go test -json` 报告解析、Go panic/测试失败栈解析。

选择器约定（与 Java 的 Class#method、Python 的 path::test 并列）：
  `./orders::TestCreate`  包 + 测试函数
  `example.com/shop/orders::TestCreate`
  `TestCreate`            不限定包，在 ./... 里按名字选
也接受 `./orders#TestCreate`，方便沿用 Java 的书写习惯。
"""

from __future__ import annotations

import json
import re

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import CodeLocation, RuntimeProbe, StackFrame, TestCase, TestReport, TestSelector
from twinfix.profiles.commands import CommandOverrides, run_custom_build, run_custom_tests

_GO_FILE_FRAME = re.compile(r"^\s*(?P<file>[^\s]+?\.go):(?P<line>\d+)(?=[:\s]|$)")
# example.com/shop/orders.(*Service).Create(0xc000...) / main.main() / net/http.(*conn).serve(...)
_GO_SYMBOL_FRAME = re.compile(r"^(?P<symbol>[^\s(]+(?:\([^)]*\))?(?:\.[A-Za-z_]\w*)*)\(")
_TEST_LINE = re.compile(r"^\s*--- (?:FAIL|PASS|SKIP):\s+(?P<name>[^\s(]+)")
_BUILD_FAILED = re.compile(r"^FAIL\s+(?P<package>\S+)\s+\[(?:build failed|setup failed)\]", re.MULTILINE)

_REPRO_TEMPLATE = """\
package {package}

import "testing"

// TwinFix 复现用例：{incident_id}。修复前必须失败，修复后必须通过。
func TestRepro{slug}(t *testing.T) {{
    // given

    // when

    // then
}}
"""


class GoProfile:
    name = "go"

    def __init__(self, project_packages: tuple[str, ...] = (), commands: CommandOverrides | None = None) -> None:
        self.project_packages = project_packages
        self.commands = commands

    def detect(self, root_files: list[str]) -> bool:
        markers = {"go.mod", "go.work", "go.sum"}
        return any(f in markers for f in root_files)

    def build_command(self, module: str | None) -> list[str]:
        # ./... 至少校验整仓可编译；单 main 包构建时 go 会把产物写到当前目录，
        # 这里始终用包模式（多个包 / 非 main 包时 go build 不落盘）。
        return ["go", "build", _package_target(module)]

    def test_command(self, selector: TestSelector) -> list[str]:
        packages, names = self._selection(selector)
        cmd = ["go", "test", "-json", "-count=1"]
        if names:
            cmd += ["-run", _run_pattern(names)]
        return [*cmd, *(packages or ["./..."])]

    async def build(
        self, sandbox: SandboxProvider, ws: Workspace, module: str | None, cwd: str | None = None
    ) -> CommandResult:
        custom = await run_custom_build(self.commands, sandbox, ws, cwd)
        if custom is not None:
            return custom
        target = _package_target(module)
        command = self.build_command(module)
        # 单个 main 包时 `go build` 会把可执行文件写到工作区，污染补丁；
        # 先确认目标里有没有 main 包，有就把产物定向到 /tmp（非 main 包用 -o 目录会报错）。
        listed = await sandbox.exec(
            ws,
            ["go", "list", "-f", '{{if eq .Name "main"}}{{.ImportPath}}{{end}}', target],
            timeout=120,
            cwd=cwd,
        )
        if listed.ok and listed.stdout.strip():
            command = ["go", "build", "-o", "/tmp/twinfix-build/", target]  # noqa: S108 - 容器内路径
        return await sandbox.exec(ws, command, timeout=900, cwd=cwd)

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
        result = await sandbox.exec(ws, self.test_command(selector), timeout=1800, cwd=cwd)
        events = parse_go_test_json(result.stdout)
        if not events:
            events = _parse_go_test_text(result.stdout + "\n" + result.stderr)
        return TestReport(command=result, cases=events)

    def parse_stacktrace(self, text: str) -> list[StackFrame]:
        lines = text.splitlines()
        frames: list[StackFrame] = []
        for index, raw in enumerate(lines):
            match = _GO_FILE_FRAME.match(raw)
            if match is None:
                continue
            path = match.group("file")
            line = int(match.group("line"))
            symbol = ""
            if index > 0:
                previous = lines[index - 1].strip()
                symbol_match = _GO_SYMBOL_FRAME.match(previous)
                if symbol_match is not None and not previous.startswith("created by "):
                    symbol = symbol_match.group("symbol")
            if not symbol:
                symbol = f"{path}:{line}"
            frames.append(
                StackFrame(symbol=symbol, file=path, line=line, is_project=self.is_project_path(path, symbol))
            )
        return frames

    def is_project_path(self, path: str, symbol: str = "") -> bool:
        if (
            self.project_packages
            and symbol
            and any(symbol == p or symbol.startswith(f"{p}.") for p in self.project_packages)
        ):
            return True
        normalized = "/" + path.replace("\\", "/").lstrip("/")
        external = ("/go/pkg/mod/", "/root/go/pkg/mod/", "/usr/local/go/src/", "/usr/lib/go/")
        return not any(marker in normalized for marker in external)

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
        return ["**/*_test.go"]

    def case_matches(self, selector: str, case_name: str) -> bool:
        """选择器与 `go test -json` 用例名（package#TestName，子测试带 /sub）比较。"""
        normalized = selector.replace("::", "#").replace("\\", "/")
        wanted_package, _, wanted_test = normalized.partition("#")
        package, _, test = case_name.partition("#")
        if not wanted_test and _looks_like_test_name(wanted_package):
            # 只给测试函数名（如 TestCreate）：不限定包
            return test == wanted_package or test.startswith(f"{wanted_package}/")
        if wanted_test:
            return test == wanted_test or test.startswith(f"{wanted_test}/")
        if wanted_package and wanted_package not in ("", "./..."):
            # 只给了包（如 ./orders）：包路径按后缀比较
            pkg = wanted_package.removeprefix("./").rstrip("/")
            return package == pkg or package.endswith(f"/{pkg}") or pkg.endswith(package)
        return False

    def source_suffixes(self) -> tuple[str, ...]:
        return (".go",)

    def repro_test_template(self) -> str:
        return _REPRO_TEMPLATE

    def runtime_probe(self) -> RuntimeProbe | None:
        return None

    # ---------------------------------------------------------------- 选择器

    def _selection(self, selector: TestSelector) -> tuple[list[str], list[str]]:
        packages: list[str] = []
        names: list[str] = []
        for item in [*selector.classes, *selector.methods]:
            raw = item.strip()
            if not raw:
                continue
            if "::" in raw:
                package, _, test = raw.partition("::")
                _append_unique(packages, package)
                if test:
                    names.append(test)
            elif "#" in raw:
                package, _, test = raw.partition("#")
                _append_unique(packages, package)
                if test:
                    names.append(test)
            elif _looks_like_test_name(raw):
                # TestFoo/sub 这类子测试名也含 /，不能当成包路径
                names.append(raw)
            else:
                # 包路径：`orders` 归一成 `./orders`，`example.com/x/orders` 保持原样
                _append_unique(packages, _package_target(raw))
        return packages, names


def _looks_like_test_name(name: str) -> bool:
    return name.startswith(("Test", "Benchmark", "Example", "Fuzz"))


def _package_target(module: str | None) -> str:
    """Go 工具接受 `./pkg` 或完整导入路径；裸包名 `orders` 要补成 `./orders`。"""
    target = (module or "./...").strip()
    if target in ("./...", "...") or target.startswith((".", "/")):
        return target
    if re.match(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/", target):
        return target  # example.com/x/orders / github.com/x/y
    return f"./{target}"


def _append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def _run_pattern(names: list[str]) -> str:
    """把测试函数名转成 -run 正则；子测试按 / 分段锚定。"""
    if len(names) == 1 and "/" not in names[0]:
        return f"^{re.escape(names[0])}$"
    plain = [re.escape(name) for name in names if "/" not in name]
    if len(plain) == len(names):
        return "^(?:" + "|".join(plain) + ")$"
    # 混合子测试时按 Go 的层级语义逐段转义，退化为宽松匹配（仍由包/用例名兜底）
    return "|".join("/".join(re.escape(part) for part in name.split("/")) for name in names)


def parse_go_test_json(text: str) -> list[TestCase]:
    """解析 `go test -json` 事件流。

    子测试各自成为 TestCase；包级构建失败（没有 Test 事件）记一条 <build> 错误，
    保证 Verifier 不会把"命令非零退出"误当成全部通过。
    """
    cases: dict[tuple[str, str], TestCase] = {}
    order: list[tuple[str, str]] = []
    outputs: dict[tuple[str, str], list[str]] = {}
    package_outputs: dict[str, list[str]] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        action = event.get("Action")
        package = str(event.get("Package") or "")
        test = str(event.get("Test") or "")
        if action == "output":
            target = (package, test)
            if test:
                outputs.setdefault(target, []).append(str(event.get("Output") or "").rstrip())
            else:
                package_outputs.setdefault(package, []).append(str(event.get("Output") or "").rstrip())
        elif action in ("pass", "fail", "skip") and test:
            key = (package, test)
            status = {"pass": "passed", "fail": "failed", "skip": "skipped"}[str(action)]
            message = "\n".join(outputs.get(key, [])).strip()[-2000:] if status == "failed" else ""
            cases[key] = TestCase(name=f"{package}#{test}" if package else test, status=status, message=message)
            order.append(key)
        elif action == "fail" and package and not test:
            output = "\n".join(package_outputs.get(package, [])).strip()
            # 有测试用例时，包级 fail 只是汇总；没有时才是构建/初始化失败
            if not any(key[0] == package for key in cases):
                key = (package, "<build>")
                cases[key] = TestCase(
                    name=f"{package}#<build>" if package else "<build>",
                    status="error",
                    message=output[-2000:],
                )
                order.append(key)

    return [cases[key] for key in order if key in cases]


def _parse_go_test_text(text: str) -> list[TestCase]:
    """兜底：没有 JSON 事件时按 `--- FAIL: TestX` 与包级构建失败行解析。"""
    cases: dict[str, TestCase] = {}
    order: list[str] = []
    for line in text.splitlines():
        match = _TEST_LINE.match(line)
        if match is not None:
            name = match.group("name")
            status = "failed" if "FAIL" in line else "skipped" if "SKIP" in line else "passed"
            if name not in cases:
                order.append(name)
            cases[name] = TestCase(name=name, status=status)
    for match in _BUILD_FAILED.finditer(text):
        package = match.group("package")
        name = f"{package}#<build>"
        if name not in cases:
            order.append(name)
        cases[name] = TestCase(name=name, status="error", message=text.strip()[-2000:])
    return [cases[key] for key in order]
