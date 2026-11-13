"""JavaProfile v1：Maven/Gradle 构建、Surefire 报告解析、Java 栈解析。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import CodeLocation, RuntimeProbe, StackFrame, TestCase, TestReport, TestSelector
from twinfix.profiles.commands import CommandOverrides, run_custom_build, run_custom_tests

_FRAME = re.compile(r"^\s*at\s+(?:[\w.$/@]+/)?(?P<symbol>[\w.$<>]+)\((?P<loc>[^)]*)\)")
_LOC = re.compile(r"^(?P<file>[\w$]+\.(?:java|kt|groovy)):(?P<line>\d+)$")

_ARTHAS_COMMANDS = frozenset(
    {"thread", "thread -b", "jvm", "memory", "dashboard -n 1", "sc", "sm", "jad", "trace", "watch"}
)

_REPRO_TEMPLATE = """\
package {package};

import org.junit.jupiter.api.Test;
import static org.assertj.core.api.Assertions.assertThat;

/** TwinFix 复现用例：{incident_id}。修复前必须失败，修复后必须通过。 */
class {class_name} {{

    @Test
    void reproduces_{slug}() {{
        // given

        // when

        // then
    }}
}}
"""


@dataclass(frozen=True)
class ArthasProbe:
    name: str = "arthas"
    allowed_commands: frozenset[str] = _ARTHAS_COMMANDS


class JavaProfile:
    name = "java"

    def __init__(self, project_packages: tuple[str, ...] = (), commands: CommandOverrides | None = None) -> None:
        self.project_packages = project_packages
        self.commands = commands

    def detect(self, root_files: list[str]) -> bool:
        markers = {"pom.xml", "build.gradle", "build.gradle.kts", "mvnw", "gradlew"}
        return any(f in markers for f in root_files)

    def build_tool(self, root_files: list[str]) -> str:
        if "mvnw" in root_files:
            return "./mvnw"
        if "pom.xml" in root_files:
            return "mvn"
        return "./gradlew" if "gradlew" in root_files else "gradle"

    def build_command(self, root_files: list[str], module: str | None) -> list[str]:
        tool = self.build_tool(root_files)
        if "mvn" in tool:
            cmd = [tool, "-q", "-B", "-DskipTests", "compile", "test-compile"]
            return [*cmd, "-pl", module, "-am"] if module else cmd
        task = f":{module}:testClasses" if module else "testClasses"
        return [tool, "-q", task]

    def test_command(self, root_files: list[str], selector: TestSelector) -> list[str]:
        tool = self.build_tool(root_files)
        if "mvn" in tool:
            cmd = [tool, "-B", "test", "-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false"]
            if not selector.is_all:
                cmd.append("-Dtest=" + ",".join([*selector.classes, *selector.methods]))
            return cmd
        cmd = [tool, "test"]
        for t in [*selector.classes, *selector.methods]:
            cmd += ["--tests", t.replace("#", ".")]
        return cmd

    async def build(
        self, sandbox: SandboxProvider, ws: Workspace, module: str | None, cwd: str | None = None
    ) -> CommandResult:
        custom = await run_custom_build(self.commands, sandbox, ws, cwd)
        if custom is not None:
            return custom
        files = await sandbox.list_dir(ws, cwd or ".")
        if cwd:
            return await sandbox.exec(ws, self.build_command(files, module), timeout=900, cwd=cwd)
        return await sandbox.exec(ws, self.build_command(files, module), timeout=900)

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
        files = await sandbox.list_dir(ws, cwd or ".")
        # 清掉上一次的报告，否则旧结果会混进本次统计
        clean_cmd = ["find", ".", "-type", "d", "-name", "surefire-reports", "-prune", "-exec", "rm", "-rf", "{}", "+"]
        if cwd:
            await sandbox.exec(ws, clean_cmd, 60, cwd=cwd)
            result = await sandbox.exec(ws, self.test_command(files, selector), timeout=1800, cwd=cwd)
        else:
            await sandbox.exec(ws, clean_cmd, 60)
            result = await sandbox.exec(ws, self.test_command(files, selector), timeout=1800)
        # 报告在 target/ 下，而 sandbox.search 会跳过构建产物目录，所以直接 find
        find_cmd = ["find", ".", "-path", "*/surefire-reports/TEST-*.xml", "-type", "f"]
        listed = await sandbox.exec(ws, find_cmd, 60, cwd=cwd) if cwd else await sandbox.exec(ws, find_cmd, 60)
        reports = sorted({p.strip().removeprefix("./") for p in listed.stdout.splitlines() if p.strip()})
        cases: list[TestCase] = []
        for path in reports:
            cases += parse_surefire(await sandbox.read_file(ws, f"{cwd}/{path}" if cwd else path))
        return TestReport(command=result, cases=cases)

    def parse_stacktrace(self, text: str) -> list[StackFrame]:
        frames: list[StackFrame] = []
        for raw in text.splitlines():
            m = _FRAME.match(raw)
            if not m:
                continue  # 异常行、Caused by、"... N more" 都不是帧
            symbol, loc = m.group("symbol"), m.group("loc")
            lm = _LOC.match(loc)
            frames.append(
                StackFrame(
                    symbol=symbol,
                    file=lm.group("file") if lm else None,
                    line=int(lm.group("line")) if lm else None,
                    is_project=self.is_project_symbol(symbol),
                )
            )
        return frames

    def is_project_symbol(self, symbol: str) -> bool:
        return any(symbol.startswith(p + ".") for p in self.project_packages)

    def frame_to_location(self, frame: StackFrame, files: list[str]) -> CodeLocation | None:
        if frame.file is None:
            return None
        # com.acme.order.OrderService$Inner.create → com/acme/order/OrderService.java
        cls = frame.symbol.rsplit(".", 1)[0].split("$", 1)[0]
        expected = cls.rsplit(".", 1)[0].replace(".", "/") + "/" + frame.file
        for f in files:
            if f.endswith(expected):
                return CodeLocation(path=f, line=frame.line, symbol=frame.symbol)
        return None

    def test_paths(self) -> list[str]:
        return ["**/src/test/**"]

    def case_matches(self, selector: str, case_name: str) -> bool:
        cls, _, method = selector.partition("#")
        simple = cls.rsplit(".", 1)[-1]
        return simple in case_name and (not method or method in case_name)

    def source_suffixes(self) -> tuple[str, ...]:
        return (".java",)

    def repro_test_template(self) -> str:
        return _REPRO_TEMPLATE

    def runtime_probe(self) -> RuntimeProbe | None:
        return ArthasProbe()


# XML 1.0 不允许的字符：测试打印的乱码（并发错乱、二进制、ANSI 转义）会被 Surefire 原样写进报告
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_XML_ILLEGAL_REF = re.compile(r"&#(?:x0*(?:[0-8bcef]|1[0-9a-f])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));", re.IGNORECASE)
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse_surefire(xml_text: str) -> list[TestCase]:
    """解析 Surefire 报告。报告不是合法 XML 时清洗后重试，仍失败则按正则提取，绝不让工具整体报错。"""
    cleaned = _XML_ILLEGAL_REF.sub("", _XML_ILLEGAL.sub("", xml_text))
    try:
        root = ET.fromstring(cleaned)  # noqa: S314
    except ET.ParseError:
        return _parse_surefire_lenient(cleaned)
    cases = []
    for tc in root.iter("testcase"):
        name = f"{tc.get('classname', '')}#{tc.get('name', '')}"
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            node = tc.find(tag)
            if node is not None:
                status = "failed" if tag == "failure" else tag
                message = node.get("message") or (node.text or "").strip()[:2000]
                break
        cases.append(TestCase(name=name, status=status, message=message))
    return cases


def _parse_surefire_lenient(text: str) -> list[TestCase]:
    """兜底：逐个 <testcase> 用正则取出名字与结果（system-out 里的残缺标签不影响）。"""
    cases = []
    for attrs, _, body in _TESTCASE.findall(text):
        a = dict(_ATTR.findall(attrs))
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            m = re.search(rf"<{tag}\b([^>]*)", body or "")
            if m:
                status = "failed" if tag == "failure" else tag
                message = dict(_ATTR.findall(m.group(1))).get("message", "")[:2000]
                break
        cases.append(TestCase(name=f"{a.get('classname', '')}#{a.get('name', '')}", status=status, message=message))
    return cases
