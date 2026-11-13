"""项目级自定义构建/测试命令。

真实仓库往往有自己的一套工具链（tsc、jest、vitest、dotnet test + xUnit/NUnit……）。
语言插件内置命令面向零依赖示例服务；生产项目可通过 ``project.config.commands`` 覆盖：

    "commands": {
      "build": "npm ci --no-audit --no-fund && npm run build",
      "test": "npx jest --ci --json --outputFile=.twinfix/jest.json",
      "test_selector": "npx jest --ci --runTestsByPath {selector}",
      "report": ".twinfix/jest.json",
      "report_format": "jest-json"
    }

约定
- 命令通过 ``sh -c`` 执行；镜像里需要自带对应工具链（用 ``project.config.sandbox_image``）。
- ``{selector}`` / ``{classes}`` / ``{methods}`` 只在给了 ``test_selector`` 且选择器非空时替换。
- ``report`` 相对工作区（或 ``cwd``）；支持 junit / trx / jest-json，缺省按 JUnit 解析。
- 配置非法时退回默认行为，不让一个坏配置拖垮整条事故链。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from twinfix.profiles.base import TestCase, TestReport, TestSelector
from twinfix.profiles.junit import parse_junit

if TYPE_CHECKING:
    from twinfix.adapters.sandbox import SandboxProvider
    from twinfix.adapters.types import CommandResult, Workspace

REPORT_FORMATS = frozenset({"junit", "trx", "jest-json"})
_DEFAULT_TIMEOUTS = {"build_timeout_s": 900, "test_timeout_s": 1800}


@dataclass(frozen=True)
class CommandOverrides:
    build: str | None = None
    test: str | None = None
    test_selector: str | None = None
    report: str | None = None
    report_format: str = "junit"
    build_timeout_s: int = 900
    test_timeout_s: int = 1800

    @property
    def active(self) -> bool:
        return bool(self.build or self.test)

    @classmethod
    def parse(cls, raw: Any) -> CommandOverrides | None:
        if not isinstance(raw, dict):
            return None

        def text(key: str) -> str | None:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        def timeout(key: str) -> int:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float | str):
                return _DEFAULT_TIMEOUTS[key]
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                return _DEFAULT_TIMEOUTS[key]
            return min(max(seconds, 60), 4 * 3600)

        fmt = text("report_format") or "junit"
        overrides = cls(
            build=text("build"),
            test=text("test"),
            test_selector=text("test_selector"),
            report=text("report"),
            report_format=fmt if fmt in REPORT_FORMATS else "junit",
            build_timeout_s=timeout("build_timeout_s"),
            test_timeout_s=timeout("test_timeout_s"),
        )
        return overrides if overrides.active else None


def selector_text(selector: TestSelector) -> str:
    return " ".join([*selector.classes, *selector.methods])


async def run_custom_build(
    commands: CommandOverrides | None,
    sandbox: SandboxProvider,
    ws: Workspace,
    cwd: str | None = None,
) -> CommandResult | None:
    if commands is None or not commands.build:
        return None
    return await sandbox.exec(ws, ["sh", "-c", commands.build], timeout=commands.build_timeout_s, cwd=cwd)


async def run_custom_tests(
    commands: CommandOverrides | None,
    sandbox: SandboxProvider,
    ws: Workspace,
    selector: TestSelector,
    cwd: str | None = None,
) -> TestReport | None:
    if commands is None or not commands.test:
        return None
    command = commands.test
    if not selector.is_all and commands.test_selector:
        command = (
            commands.test_selector.replace("{selector}", selector_text(selector))
            .replace("{classes}", " ".join(selector.classes))
            .replace("{methods}", " ".join(selector.methods))
        )
    report = commands.report or ".twinfix/custom-tests.xml"
    report_path = f"{cwd}/{report}" if cwd else report
    # 清掉旧报告：命令失败/没产出报告时不能把上一次结果算进来
    await sandbox.write_file(ws, report_path, "")
    result = await sandbox.exec(ws, ["sh", "-c", command], timeout=commands.test_timeout_s, cwd=cwd)
    try:
        text = await sandbox.read_file(ws, report_path)
    except FileNotFoundError:
        return TestReport(command=result, cases=[])
    return TestReport(command=result, cases=parse_report(text, commands.report_format))


def parse_report(text: str, report_format: str) -> list[TestCase]:
    text = text.lstrip("﻿")  # dotnet test / jest 的报告可能带 UTF-8 BOM
    if report_format == "trx":
        return parse_trx(text)
    if report_format == "jest-json":
        return parse_jest_json(text)
    return parse_junit(text)


def parse_trx(xml_text: str) -> list[TestCase]:
    """解析 dotnet test 的 TRX；损坏的报告返回空列表，不让工具整体报错。"""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return []
    cases: list[TestCase] = []
    for node in root.iter("UnitTestResult"):
        name = node.get("testName") or ""
        if not name:
            continue
        outcome = (node.get("outcome") or "").lower()
        status = {
            "passed": "passed",
            "failed": "failed",
            "passedbutrunaborted": "passed",
            "skipped": "skipped",
            "notexecuted": "skipped",
            "inconclusive": "skipped",
            "error": "error",
            "timeout": "error",
            "aborted": "error",
        }.get(outcome, "error")
        message = ""
        for msg in node.iter("Message"):
            message = (msg.text or "").strip()[:2000]
            if message:
                break
        cases.append(TestCase(name=name, status=status, message=message))
    return cases


def parse_jest_json(text: str) -> list[TestCase]:
    """解析 jest --json；vitest 的 JSON reporter 结构兼容。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    cases: list[TestCase] = []
    for file in data.get("testResults") or []:
        if not isinstance(file, dict):
            continue
        for assertion in file.get("assertionResults") or []:
            if not isinstance(assertion, dict):
                continue
            name = assertion.get("fullName") or assertion.get("title") or ""
            if not name:
                continue
            status = {
                "passed": "passed",
                "failed": "failed",
                "pending": "skipped",
                "todo": "skipped",
                "disabled": "skipped",
            }.get(str(assertion.get("status")), "error")
            messages = assertion.get("failureMessages") or []
            message = "\n".join(str(m) for m in messages)[:2000]
            cases.append(TestCase(name=name, status=status, message=message))
    return cases
