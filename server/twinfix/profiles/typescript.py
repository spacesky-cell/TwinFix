"""TypeScriptProfile：Node 内置类型剥离 + node:test、JUnit 报告、Node 栈解析。

不假设项目安装了 tsc / jest / vitest：沙箱只带 Node 工具链，`.ts` 由 Node 22+ 的类型剥离直接执行，
测试跑 `node --test`。项目若需要真实的编译步骤，可用 ``project.config.sandbox_image`` 换成自带
构建工具链的镜像，并在项目里提供自己的命令。
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import CodeLocation, RuntimeProbe, StackFrame, TestReport, TestSelector
from twinfix.profiles.commands import CommandOverrides, run_custom_build, run_custom_tests
from twinfix.profiles.junit import parse_junit

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
#     at PriceCalculator.discountFor (/app/orders/pricing.ts:22:15)
#     at async run (file:///app/orders/service.ts:31:9)
#     at /app/orders/app.ts:69:9
_FRAME = re.compile(
    r"^\s*at\s+(?:(?P<symbol>.+?)\s+\()?"
    r"(?P<file>(?:file://)?[^()\s]+?\.(?:ts|tsx|js|jsx|mjs|cjs))"
    r":(?P<line>\d+):(?P<col>\d+)\)?\s*$"
)
_TEST_SUFFIX = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")

_REPRO_TEMPLATE = """\
import assert from "node:assert/strict";
import { test } from "node:test";
// TwinFix 复现用例：{incident_id}。修复前必须失败，修复后必须通过。

test("reproduces {slug}", () => {{
  // given

  // when

  // then
  assert.ok(...);
}});
"""


class TypeScriptProfile:
    name = "typescript"

    def __init__(
        self,
        project_packages: tuple[str, ...] = (),
        node: str = "node",
        commands: CommandOverrides | None = None,
    ) -> None:
        self.project_packages = project_packages
        self.node = node
        self.commands = commands

    def detect(self, root_files: list[str]) -> bool:
        markers = {
            "package.json",
            "tsconfig.json",
            "tsconfig.base.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        }
        return any(f in markers for f in root_files)

    def build_command(self, module: str | None) -> list[str]:
        # Node 的类型剥离只检查单个文件；find + xargs 逐文件做语法检查，
        # 不引入 tsc，也不要求项目预先 npm install。.tsx / .jsx 需要 JSX 转换，跳过。
        target = _module_target(module)
        script = (
            "set -e; "
            f"find {_shell_quote(target)} -type f "
            "\\( -name '*.ts' -o -name '*.js' -o -name '*.mjs' -o -name '*.cjs' \\) "
            "-not -path '*/node_modules/*' -print0 "
            "| xargs -0 -r -n 1 node --check"
        )
        return ["sh", "-c", script]

    def test_command(self, selector: TestSelector) -> list[str]:
        files, names = _selection(selector)
        destination = ".twinfix/node-tests.xml"
        # Node 要求 reporter 与 destination 一一对应；只写 JUnit，失败信息在用例 message 里
        cmd = [
            self.node,
            "--test",
            "--test-reporter=junit",
            f"--test-reporter-destination={destination}",
        ]
        if names:
            cmd += ["--test-name-pattern", _name_pattern(names)]
        return [*cmd, *files]

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
        report_path = f"{cwd}/.twinfix/node-tests.xml" if cwd else ".twinfix/node-tests.xml"
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
            match = _FRAME.match(raw)
            if match is None:
                continue
            path = _clean_path(match.group("file"))
            symbol = _clean_symbol(match.group("symbol"))
            if not symbol:
                symbol = f"{path}:{match.group('line')}"
            frames.append(
                StackFrame(
                    symbol=symbol,
                    file=path,
                    line=int(match.group("line")),
                    is_project=self.is_project_path(path),
                )
            )
        return frames

    def is_project_path(self, path: str) -> bool:
        normalized = "/" + path.replace("\\", "/").lstrip("/")
        if self.project_packages and any(f"/{p.replace('.', '/')}/" in normalized for p in self.project_packages):
            return True
        external = ("/node_modules/", "/usr/local/lib/node_modules/", "/usr/lib/node_modules/")
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
        return ["**/*.test.ts", "**/*.spec.ts", "**/tests/**", "**/__tests__/**"]

    def case_matches(self, selector: str, case_name: str) -> bool:
        """选择器可以是 `path.test.ts::test name`、单独文件，或只给测试名。

        Node 的 JUnit classname 只是 "test"（不含文件路径），因此只按 `::` 后的测试名比较；
        只给文件时无法从报告反查，按"不否定"处理，交给 run_tests 的退出码兜底。
        """
        raw = selector.strip()
        if "::" in raw:
            wanted = raw.split("::", 1)[1].strip()
        elif re.search(r"\.(?:[cm]?[jt]sx?)$", raw):
            return True
        else:
            wanted = raw
        if not wanted:
            return True
        normalized = wanted.replace("#", ".").lower()
        return normalized in case_name.lower()

    def source_suffixes(self) -> tuple[str, ...]:
        return _SOURCE_SUFFIXES

    def repro_test_template(self) -> str:
        return _REPRO_TEMPLATE

    def runtime_probe(self) -> RuntimeProbe | None:
        return None


def _module_target(module: str | None) -> str:
    target = (module or ".").strip()
    return target or "."


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _selection(selector: TestSelector) -> tuple[list[str], list[str]]:
    files: list[str] = []
    names: list[str] = []
    for item in [*selector.classes, *selector.methods]:
        raw = item.strip()
        if not raw:
            continue
        if "::" in raw:
            path, _, name = raw.partition("::")
            _append_unique(files, path)
            if name.strip():
                names.append(name.strip())
        elif "#" in raw and re.search(r"\.(?:[cm]?[jt]sx?)$", raw.partition("#")[0]):
            path, _, name = raw.partition("#")
            _append_unique(files, path)
            if name.strip():
                names.append(name.strip())
        elif re.search(r"\.(?:[cm]?[jt]sx?)$", raw):
            _append_unique(files, raw)
        else:
            names.append(raw)
    return files, names


def _name_pattern(names: list[str]) -> str:
    if len(names) == 1:
        return f"^{re.escape(names[0])}$"
    return "^(?:" + "|".join(re.escape(name) for name in names) + ")$"


def _append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def _clean_symbol(symbol: str | None) -> str:
    if not symbol:
        return ""
    cleaned = symbol.strip()
    for prefix in ("async ", "new "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned


def _clean_path(raw: str) -> str:
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        return unquote(parsed.path)
    return raw


def is_test_file(path: str) -> bool:
    return bool(_TEST_SUFFIX.search(path))
