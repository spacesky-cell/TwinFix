"""CSharpProfile：dotnet build / dotnet run 测试工程、JUnit 报告、.NET 栈帧解析。

支持路径（与 TypeScript 的 node --test 同理，刻意保持零第三方依赖）：
测试是仓库里的控制台测试工程（目录或工程名以 ``.Tests`` 结尾），工程内自带一个极简
反射 harness（运行 ``Test*`` 静态方法）并把结果写成 JUnit XML。沙箱只需要 .NET SDK，
不要求项目预先 ``dotnet restore`` 下载 NuGet 包；使用 xUnit/NUnit 的真实项目可换自带
工具链的镜像并提供自己的测试命令（见 ``project.config.sandbox_image``）。

选择器约定：
  ``Shop.Tests.PricingTests::TestNoCoupon``  类 + 测试方法
  ``Shop.Tests.PricingTests``                 该类全部测试
  ``TestNoCoupon``                            按方法名
也接受 ``Class#Method``，沿用 Java 的书写习惯。
"""

from __future__ import annotations

import re
import shlex

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import CodeLocation, RuntimeProbe, StackFrame, TestReport, TestSelector
from twinfix.profiles.commands import CommandOverrides, run_custom_build, run_custom_tests
from twinfix.profiles.junit import parse_junit

_SOURCE_SUFFIXES = (".cs",)
#    at Shop.Orders.PriceCalculator.ThresholdOf(Coupon coupon) in /app/orders/Pricing.cs:line 28
#    at Shop.Orders.OrderService.Create(String customer, IReadOnlyList`1 lines, Coupon coupon)
#    at System.Environment.get_StackTrace()
_FRAME = re.compile(
    r"^\s*at\s+(?P<symbol>[^(]+?)(?:\([^)]*\))?"
    r"(?:\s+in\s+(?P<file>.+?):line\s+(?P<line>\d+))?\s*$"
)
_REPORT = ".twinfix/csharp-tests.xml"

_REPRO_TEMPLATE = """\
namespace Shop.Tests;

using Shop.Orders;

// TwinFix 复现用例：{incident_id}。修复前必须失败，修复后必须通过。
public static class Repro{slug}Tests
{{
    public static void TestReproduces{slug}()
    {{
        // given

        // when

        // then
        Check.Equal(0, 1);
    }}
}}
"""


class CSharpProfile:
    name = "csharp"

    def __init__(
        self,
        project_packages: tuple[str, ...] = (),
        dotnet: str = "dotnet",
        commands: CommandOverrides | None = None,
    ) -> None:
        self.project_packages = project_packages
        self.dotnet = dotnet
        self.commands = commands

    def detect(self, root_files: list[str]) -> bool:
        markers = {
            "global.json",
            "Directory.Build.props",
            "Directory.Build.targets",
            "Directory.Packages.props",
            "nuget.config",
            "NuGet.config",
        }
        return any(f in markers or f.endswith((".csproj", ".sln", ".slnx", ".cs")) for f in root_files)

    def build_command(self, module: str | None) -> list[str]:
        target = (module or ".").strip() or "."
        # 产物放到 /tmp：bin/obj 不进入工作区 diff / 修复提交（沙箱的 /tmp 是可写 tmpfs）
        return [
            self.dotnet,
            "build",
            target,
            "--artifacts-path",
            "/tmp/twinfix-dotnet",  # noqa: S108 - 沙箱容器内路径
            "--nologo",
            "-v:q",
        ]

    def test_command(self, selector: TestSelector) -> list[str]:
        names = _selection(selector)
        # dotnet run 会把工作目录切到测试工程目录，所以 JUnit 路径必须用 $(pwd) 固定到工作区根。
        # 测试工程按约定发现：目录/工程名以 .Tests 结尾（见模块 docstring）。
        script = (
            "set -e; "
            "proj=$(find . -type f -name '*.csproj' "
            "-not -path '*/bin/*' -not -path '*/obj/*' "
            "| grep -Ei '\\.Tests?/.*\\.csproj$|\\.Tests?\\.csproj$' | sort | head -n 1); "
            'if [ -z "$proj" ]; then '
            'echo "no C# test project (*.Tests.csproj) found; add one or set project.config.sandbox_image" >&2; '
            "exit 2; fi; "
            f'exec {shlex.quote(self.dotnet)} run --artifacts-path /tmp/twinfix-dotnet --project "$proj" -- '
            f'--junit "$(pwd)/{_REPORT}"'
        )
        if names:
            script += " " + " ".join(shlex.quote(name) for name in names)
        return ["sh", "-c", script]

    async def build(
        self, sandbox: SandboxProvider, ws: Workspace, module: str | None, cwd: str | None = None
    ) -> CommandResult:
        custom = await run_custom_build(self.commands, sandbox, ws, cwd)
        if custom is not None:
            return custom
        return await sandbox.exec(ws, self.build_command(module), timeout=900, cwd=cwd)

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
        report_path = f"{cwd}/{_REPORT}" if cwd else _REPORT
        # harness 写报告前文件必须可写；.twinfix/ 已被 git 排除，不会污染补丁
        await sandbox.write_file(ws, report_path, "")
        result = await sandbox.exec(ws, self.test_command(selector), timeout=1800, cwd=cwd)
        try:
            xml_text = await sandbox.read_file(ws, report_path)
        except FileNotFoundError:
            return TestReport(command=result, cases=[])
        return TestReport(command=result, cases=parse_junit(xml_text))

    def parse_stacktrace(self, text: str) -> list[StackFrame]:
        frames: list[StackFrame] = []
        for raw in text.splitlines():
            if raw.strip().startswith("--- End of stack trace"):
                continue
            match = _FRAME.match(raw)
            if match is None:
                continue
            path = (match.group("file") or "").strip()
            line = match.group("line")
            symbol = _clean_symbol(match.group("symbol"))
            if not symbol:
                continue
            if not path:
                symbol = symbol or f"<unknown>:{line}"
            frames.append(
                StackFrame(
                    symbol=symbol,
                    file=path or None,
                    line=int(line) if line else None,
                    is_project=self.is_project_path(path, symbol),
                )
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
        external = (
            "/usr/share/dotnet/",
            "/usr/lib/dotnet/",
            "/usr/local/share/dotnet/",
            "/.nuget/packages/",
            "/root/.nuget/packages/",
        )
        return bool(normalized.strip("/")) and not any(marker in normalized for marker in external)

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
        return ["**/*Tests.cs", "**/*Test.cs", "**/*.Tests/**", "**/tests/**"]

    def case_matches(self, selector: str, case_name: str) -> bool:
        """选择器与 harness 的 JUnit 用例名（Namespace.Class#Method）按点号归一后比较。"""
        wanted = selector.strip().replace("::", ".").replace("#", ".")
        name = case_name.replace("#", ".").replace("\\", "/")
        if not wanted:
            return True
        return (
            name == wanted
            or name.endswith(f".{wanted}")
            or f".{wanted}." in f".{name}"
            or name.rsplit(".", 1)[-1] == wanted
        )

    def source_suffixes(self) -> tuple[str, ...]:
        return _SOURCE_SUFFIXES

    def repro_test_template(self) -> str:
        return _REPRO_TEMPLATE

    def runtime_probe(self) -> RuntimeProbe | None:
        return None


def _selection(selector: TestSelector) -> list[str]:
    names: list[str] = []
    for item in [*selector.classes, *selector.methods]:
        raw = item.strip().replace("::", ".").replace("#", ".")
        if raw and raw not in names:
            names.append(raw)
    return names


def _clean_symbol(raw: str) -> str:
    symbol = raw.strip()
    # 泛型方法的显示名带 `1 之类的元数后缀；保留它不影响匹配，但去掉多余空白
    return re.sub(r"\s+", " ", symbol)
