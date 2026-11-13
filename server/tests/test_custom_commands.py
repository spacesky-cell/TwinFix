"""项目级自定义 build/test 命令：覆盖内置命令、选择器替换与 JUnit/TRX/Jest-JSON 解析。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twinfix.adapters.types import CommandResult, Workspace
from twinfix.profiles.base import TestSelector
from twinfix.profiles.commands import CommandOverrides, parse_jest_json, parse_report, parse_trx
from twinfix.profiles.csharp import CSharpProfile
from twinfix.profiles.factory import get_profile
from twinfix.profiles.go import GoProfile
from twinfix.profiles.java import JavaProfile
from twinfix.profiles.python import PythonProfile
from twinfix.profiles.typescript import TypeScriptProfile

WS = Workspace(id="w1", repo="acme/demo", sha="abc", root=Path("."))

JUNIT = """<?xml version="1.0"?>
<testsuite>
  <testcase classname="demo.PricingTest" name="noCoupon"/>
</testsuite>
"""

TRX = """<?xml version="1.0"?>
<TestRun>
  <Results>
    <UnitTestResult testName="Shop.Tests.PricingTests.TestNoCoupon" outcome="Passed" />
    <UnitTestResult testName="Shop.Tests.PricingTests.TestCoupon" outcome="Failed">
      <Output><ErrorInfo><Message>expected 1 but was 2</Message></ErrorInfo></Output>
    </UnitTestResult>
    <UnitTestResult testName="Shop.Tests.PricingTests.TestSkipped" outcome="NotExecuted" />
  </Results>
</TestRun>
"""

JEST = json.dumps(
    {
        "success": False,
        "testResults": [
            {
                "name": "/app/tests/pricing.test.ts",
                "assertionResults": [
                    {"fullName": "Pricing no coupon", "status": "passed"},
                    {"fullName": "Pricing coupon", "status": "failed", "failureMessages": ["boom"]},
                    {"fullName": "Pricing todo", "status": "pending"},
                ],
            }
        ],
    }
)


class FakeSandbox:
    def __init__(self, report: str = "", *, missing_report: bool = False) -> None:
        self.report = report
        self.missing_report = missing_report
        self.commands: list[tuple[list[str], int, str | None]] = []
        self.written: list[tuple[str, str]] = []

    async def exec(self, ws: Workspace, cmd: list[str], timeout: int, cwd: str | None = None) -> CommandResult:
        self.commands.append((cmd, timeout, cwd))
        return CommandResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    async def read_file(self, ws: Workspace, path: str) -> str:
        if self.missing_report:
            raise FileNotFoundError(path)
        return self.report

    async def write_file(self, ws: Workspace, path: str, content: str) -> None:
        self.written.append((path, content))


class TestParsing:
    def test_parse_overrides_accepts_valid_config(self) -> None:
        parsed = CommandOverrides.parse(
            {"build": "npm run build", "test": "npx vitest run", "report": ".twinfix/x.xml"}
        )
        assert parsed is not None
        assert parsed.build == "npm run build"
        assert parsed.test == "npx vitest run"
        assert parsed.report == ".twinfix/x.xml"
        assert parsed.report_format == "junit"

    @pytest.mark.parametrize("raw", [None, "x", 1, {"build": "  "}, {}])
    def test_parse_overrides_ignores_invalid_or_empty(self, raw: Any) -> None:
        assert CommandOverrides.parse(raw) is None

    def test_parse_overrides_clamps_and_defaults(self) -> None:
        parsed = CommandOverrides.parse(
            {"test": "x", "report_format": "unknown", "build_timeout_s": 1, "test_timeout_s": 99999}
        )
        assert parsed is not None
        assert parsed.report_format == "junit"
        assert parsed.build_timeout_s == 60
        assert parsed.test_timeout_s == 4 * 3600

    def test_parse_trx(self) -> None:
        cases = {c.name: c for c in parse_trx(TRX)}
        assert cases["Shop.Tests.PricingTests.TestNoCoupon"].status == "passed"
        assert cases["Shop.Tests.PricingTests.TestCoupon"].status == "failed"
        assert "expected 1 but was 2" in cases["Shop.Tests.PricingTests.TestCoupon"].message
        assert cases["Shop.Tests.PricingTests.TestSkipped"].status == "skipped"

    def test_parse_jest_json(self) -> None:
        cases = {c.name: c for c in parse_jest_json(JEST)}
        assert cases["Pricing no coupon"].status == "passed"
        assert cases["Pricing coupon"].status == "failed"
        assert "boom" in cases["Pricing coupon"].message
        assert cases["Pricing todo"].status == "skipped"

    def test_parse_report_strips_utf8_bom(self) -> None:
        assert parse_report("﻿" + TRX, "trx")[0].name == "Shop.Tests.PricingTests.TestNoCoupon"
        assert parse_report("﻿" + JEST, "jest-json")[0].name == "Pricing no coupon"

    def test_parse_report_dispatch(self) -> None:
        assert parse_report(JUNIT, "junit")[0].name == "demo.PricingTest#noCoupon"
        assert parse_report(TRX, "trx")[0].name.startswith("Shop.Tests")
        assert parse_report(JEST, "jest-json")[0].name == "Pricing no coupon"
        assert parse_report("not xml", "trx") == []


class TestProfiles:
    @pytest.mark.parametrize(
        "profile",
        [
            JavaProfile(commands=CommandOverrides(build="echo build")),
            PythonProfile(commands=CommandOverrides(build="echo build")),
            GoProfile(commands=CommandOverrides(build="echo build")),
            TypeScriptProfile(commands=CommandOverrides(build="echo build")),
            CSharpProfile(commands=CommandOverrides(build="echo build")),
        ],
    )
    async def test_custom_build_overrides_native_command(self, profile: Any) -> None:
        sandbox = FakeSandbox()
        result = await profile.build(sandbox, WS, None)  # type: ignore[arg-type]
        assert result.ok
        assert sandbox.commands == [(["sh", "-c", "echo build"], 900, None)]

    async def test_custom_tests_replaces_selector_and_parses_report(self) -> None:
        commands = CommandOverrides(
            test="runner all",
            test_selector="runner {classes} {methods}",
            report=".twinfix/report.xml",
        )
        profile = TypeScriptProfile(commands=commands)
        sandbox = FakeSandbox(JUNIT)

        report = await profile.run_tests(
            sandbox,
            WS,
            TestSelector(classes=["demo.PricingTest"], methods=["PricingTest#noCoupon"]),  # type: ignore[arg-type]
        )

        assert sandbox.commands[0][0] == ["sh", "-c", "runner demo.PricingTest PricingTest#noCoupon"]
        assert sandbox.written[0] == (".twinfix/report.xml", "")
        assert [c.name for c in report.cases] == ["demo.PricingTest#noCoupon"]

    async def test_custom_tests_without_report_returns_empty_cases(self) -> None:
        profile = CSharpProfile(commands=CommandOverrides(test="dotnet test", report=".twinfix/t.trx"))
        sandbox = FakeSandbox(missing_report=True)
        report = await profile.run_tests(sandbox, WS, TestSelector())  # type: ignore[arg-type]
        assert report.cases == []
        assert sandbox.commands[0][0] == ["sh", "-c", "dotnet test"]

    def test_factory_passes_commands(self) -> None:
        profile = get_profile("typescript", (), commands={"test": "npx jest", "report_format": "jest-json"})
        assert isinstance(profile, TypeScriptProfile)
        assert profile.commands is not None
        assert profile.commands.test == "npx jest"
        assert profile.commands.report_format == "jest-json"

    def test_factory_without_commands_keeps_native_profile(self) -> None:
        profile = get_profile("java")
        assert isinstance(profile, JavaProfile)
        assert profile.commands is None
