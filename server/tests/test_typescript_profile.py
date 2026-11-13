"""TypeScriptProfile 与 TypeScript 代码索引。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from twinfix.adapters.sandbox import LocalSandbox
from twinfix.codeintel import get_index, invalidate
from twinfix.codeintel.typescript_index import build_typescript_index
from twinfix.profiles.base import TestSelector
from twinfix.profiles.factory import get_profile
from twinfix.profiles.junit import parse_junit
from twinfix.profiles.typescript import TypeScriptProfile

FILES = {
    "orders/models.ts": """\
export type CouponKind = "amount" | "percent";

export interface Coupon {
  code: string;
  kind: CouponKind;
  value: number;
  threshold?: number;
}

export interface Pricing {
  discountFor(subtotal: number, coupon: Coupon | null): number;
}

export class OrderLine {
  constructor(public sku: string, public quantity: number, public unitPrice: number) {}
}
""",
    "orders/pricing.ts": """\
import type { Coupon, OrderLine, Pricing } from "./models.ts";

export class PriceCalculator implements Pricing {
  calculate(lines: OrderLine[], coupon: Coupon | null = null): number {
    const subtotal = this.sum(lines);
    return this.discountFor(subtotal, coupon);
  }

  discountFor(subtotal: number, coupon: Coupon | null): number {
    if (coupon === null) {
      return 0;
    }
    const threshold = this.threshold(coupon);
    return subtotal >= threshold ? coupon.value : 0;
  }

  private threshold(coupon: Coupon): number {
    return coupon.kind === "amount" ? Math.max(coupon.threshold ?? 0, coupon.value * 2) : 0;
  }

  private sum(lines: OrderLine[]): number {
    return lines.reduce((total, line) => total + line.unitPrice * line.quantity, 0);
  }
}
""",
    "orders/service.ts": """\
import type { Coupon, OrderLine } from "./models.ts";
import { PriceCalculator } from "./pricing.ts";

export class OrderService {
  private calculator = new PriceCalculator();

  create(lines: OrderLine[], coupon: Coupon | null): number {
    return this.calculator.calculate(lines, coupon);
  }
}
""",
}


def _node_22() -> bool:
    executable = shutil.which("node")
    if executable is None:
        return False
    try:
        out = subprocess.run(  # noqa: S603 - 路径来自 shutil.which，不拼接用户输入
            [executable, "--version"], capture_output=True, text=True, timeout=10
        )
    except OSError:
        return False
    version = out.stdout.strip()
    return version.startswith("v") and version[1:].split(".", 1)[0].isdigit() and int(version[1:].split(".")[0]) >= 22


def test_detect_and_commands() -> None:
    profile = TypeScriptProfile()
    assert profile.detect(["package.json"])
    assert profile.detect(["tsconfig.json"])
    assert not profile.detect(["pom.xml"])
    build = profile.build_command(None)
    assert build[:2] == ["sh", "-c"]
    assert "node --check" in build[2]
    assert "'orders'" in profile.build_command("orders")[2]
    selected = profile.test_command(TestSelector(classes=["orders/tests/pricing.test.ts::test a"]))
    assert "orders/tests/pricing.test.ts" in selected
    assert selected[selected.index("--test-name-pattern") + 1] == r"^test\ a$"
    assert profile.test_command(TestSelector())[-1].startswith("--test-reporter-destination=")
    assert profile.test_paths() == ["**/*.test.ts", "**/*.spec.ts", "**/tests/**", "**/__tests__/**"]
    assert profile.source_suffixes() == (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
    assert profile.runtime_probe() is None
    assert get_profile("ts").name == "typescript"
    assert get_profile("javascript").name == "typescript"
    assert get_profile("nodejs").name == "typescript"


def test_selector_matching() -> None:
    profile = TypeScriptProfile()
    assert profile.case_matches("orders/tests/pricing.test.ts::prices at subtotal", "test#prices at subtotal")
    assert profile.case_matches("prices at subtotal", "test#prices at subtotal")
    assert profile.case_matches("orders/tests/pricing.test.ts", "test#anything")
    assert not profile.case_matches("orders/tests/pricing.test.ts::prices at subtotal", "test#other")


def test_parse_stacktrace_and_frame_location() -> None:
    profile = TypeScriptProfile(("orders",))
    text = (
        "TypeError: Cannot read properties of null (reading 'kind')\n"
        "    at PriceCalculator.threshold (file:///app/orders/pricing.ts:28:18)\n"
        "    at PriceCalculator.discountFor (/app/orders/pricing.ts:16:22)\n"
        "    at /app/orders/app.ts:73:21\n"
        "    at ModuleJob.run (node:internal/modules/esm/module_job:274:25)\n"
        "    at Object.<anonymous> (/app/node_modules/lib/index.js:10:3)\n"
    )
    frames = profile.parse_stacktrace(text)
    assert [frame.symbol for frame in frames] == [
        "PriceCalculator.threshold",
        "PriceCalculator.discountFor",
        "/app/orders/app.ts:73",
        "Object.<anonymous>",
    ]
    assert [frame.is_project for frame in frames] == [True, True, True, False]
    location = profile.frame_to_location(frames[0], ["orders/pricing.ts", "orders/service.ts"])
    assert location is not None and location.path == "orders/pricing.ts" and location.line == 28


def test_parse_node_junit() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testcase name="passes" time="0.1" classname="test"/>
      <testcase name="fails" time="0.1" classname="test" failure="boom">
        <failure type="testCodeFailure" message="boom">trace</failure>
      </testcase>
      <testcase name="skipped" time="0" classname="test"><skipped message="later"/></testcase>
    </testsuites>"""
    cases = {case.name: (case.status, case.message) for case in parse_junit(xml)}
    assert cases["test#passes"] == ("passed", "")
    assert cases["test#fails"][0] == "failed"
    assert "boom" in cases["test#fails"][1]
    assert cases["test#skipped"][0] == "skipped"


def test_typescript_index_symbols_calls_and_interfaces() -> None:
    index = build_typescript_index(FILES)
    ids = set(index.symbols)
    assert {
        "orders.models.Coupon",
        "orders.models.CouponKind",
        "orders.models.Pricing",
        "orders.models.OrderLine",
        "orders.models.OrderLine#constructor/3",
        "orders.models.OrderLine#field:sku",
        "orders.pricing.PriceCalculator",
        "orders.pricing.PriceCalculator#calculate/2",
        "orders.pricing.PriceCalculator#discountFor/2",
        "orders.pricing.PriceCalculator#threshold/1",
        "orders.pricing.PriceCalculator#sum/1",
        "orders.service.OrderService",
        "orders.service.OrderService#create/2",
        "orders.service.OrderService#field:calculator",
    } <= ids

    calculator, candidates = index.resolve_symbol("orders.pricing.PriceCalculator")
    assert calculator is not None and calculator.kind == "class" and not candidates
    calculate = index.symbols["orders.pricing.PriceCalculator#calculate/2"]
    outgoing = {edge.name for edge in index.edges if edge.src == calculate.id and edge.kind == "calls"}
    assert outgoing >= {"sum", "discountFor"}
    discount = index.symbols["orders.pricing.PriceCalculator#discountFor/2"]
    assert {ref.edge.src for ref in index.find_references(index.symbols["orders.models.Coupon"], "types")} >= {
        discount.id,
        calculate.id,
    }
    assert any(
        edge.kind == "implements"
        and edge.src == "orders.pricing.PriceCalculator"
        and edge.dst == "orders.models.Pricing"
        for edge in index.edges
    )

    constructed = index.find_references(calculator, "calls")
    assert {ref.edge.src for ref in constructed} == {"orders.service.OrderService"}

    outline = index.outline("orders/pricing.ts")
    assert [symbol.name for symbol in outline][:2] == ["PriceCalculator", "calculate"]
    assert [symbol.id for symbol in index.search("PriceCalculator", "class")] == ["orders.pricing.PriceCalculator"]


def test_typescript_index_relative_import_and_type_refs() -> None:
    files = {
        "src/pkg/base.ts": "export class Base { run(): void {} }\n",
        "src/pkg/child.ts": """\
import { Base } from "./base.ts";

export class Child extends Base {
  go(): void {
    this.run();
  }
}
""",
    }
    index = build_typescript_index(files)
    base = index.symbols["pkg.base.Base"]
    assert any(edge.kind == "extends" and edge.src == "pkg.child.Child" and edge.dst == base.id for edge in index.edges)
    run = index.symbols["pkg.base.Base#run/0"]
    assert {ref.edge.src for ref in index.find_references(run, "calls")} == {"pkg.child.Child#go/0"}


@pytest.mark.skipif(not _node_22(), reason="needs Node 22+ for TypeScript type stripping")
async def test_run_tests_parses_real_node_junit(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path / "workspaces")
    sandbox.allow_exec = True
    ws = await sandbox.create_workspace("acme/ts", "abc")
    await sandbox.write_file(
        ws,
        "tests/math.test.ts",
        'import assert from "node:assert/strict";\n'
        'import { test } from "node:test";\n'
        'test("ok", () => { assert.equal(1 + 1, 2); });\n'
        'test("bad", () => { assert.equal(1 + 1, 3); });\n',
    )
    try:
        profile = TypeScriptProfile()
        build = await profile.build(sandbox, ws, None)
        assert build.ok, build.stderr
        report = await profile.run_tests(sandbox, ws, TestSelector())
        statuses = {case.name: case.status for case in report.cases}
        assert statuses.get("test#ok") == "passed"
        assert statuses.get("test#bad") == "failed"
        assert [case.name for case in report.failed] == ["test#bad"]
    finally:
        invalidate(ws.id)


async def test_index_cache_is_language_specific_for_typescript(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path / "workspaces")
    ws = await sandbox.create_workspace("acme/ts", "abc")
    await sandbox.write_file(ws, "package.json", '{"name":"x"}\n')
    await sandbox.write_file(ws, "app/service.ts", "export class Service { run(): void {} }\n")
    try:
        ts = await get_index(sandbox, ws, "typescript")
        assert "app.service.Service" in ts.symbols
        java = await get_index(sandbox, ws, "java")
        assert "app.service.Service" not in java.symbols
        assert await get_index(sandbox, ws, "typescript") is ts
    finally:
        invalidate(ws.id)


def test_orchestrator_selects_typescript_profile_and_image() -> None:
    from types import SimpleNamespace

    from twinfix.domain.models import Incident, Project
    from twinfix.orchestrator.steps import Steps
    from twinfix.profiles.java import JavaProfile

    steps = object.__new__(Steps)
    steps.adapters = SimpleNamespace(profile=JavaProfile())
    steps.settings = SimpleNamespace(
        sandbox_image_python="py:3.12",
        sandbox_image_go="go:1.25",
        sandbox_image_typescript="ts:22",
        sandbox_image_csharp="cs:10.0",
    )
    assert steps._profile_of(Project(name="t", repo="acme/ts", language="typescript")).name == "typescript"
    assert steps._profile_for_inc(Incident(meta={"language": "typescript"})).name == "typescript"
    assert steps._sandbox_image(Project(name="t", repo="acme/ts", language="typescript")) == "ts:22"
    assert (
        steps._sandbox_image(Project(name="t", repo="acme/ts", language="typescript", config={"sandbox_image": "x"}))
        == "x"
    )


def test_node_grading_command_and_hidden_install(tmp_path: Path) -> None:
    from twinfix.arena.hidden import Node, install_node_hidden_tests, summarize_node_output

    repo = tmp_path / "repo"
    hidden = tmp_path / "pricing_hidden.test.ts"
    hidden.write_text("test('x', () => {});\n", encoding="utf-8")
    names = install_node_hidden_tests(repo, "orders", [hidden])
    assert names == ["orders/tests/pricing_hidden.test.ts"]
    assert (repo / names[0]).read_text(encoding="utf-8") == "test('x', () => {});\n"

    local = Node(executable="node").command(repo, "orders", ["orders/tests/pricing_hidden.test.ts"])
    assert local[1:] == ["--test", "--test-reporter=spec", "orders/tests/pricing_hidden.test.ts"]
    image = Node(image="twinfix-sandbox-typescript:22").command(repo, "orders", None)
    assert image[:3] == ["docker", "run", "--rm"]
    assert f"{repo.resolve()}:/w" in image and "node" in image and "--test" in image
    summary = summarize_node_output("TAP version 13\n1..2\nnot ok 1 - bad\n  Error: boom\n# fail 1\n")
    assert summary == "not ok 1 - bad\n  Error: boom\n# fail 1"
