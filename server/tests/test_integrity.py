"""防作弊检查：对修复 diff 的确定性扫描（docs/05-safety.md §5）。"""

from __future__ import annotations

from types import SimpleNamespace

from twinfix.config import Settings
from twinfix.domain.schemas import Check, VerificationResult
from twinfix.orchestrator.integrity import check_patch
from twinfix.orchestrator.steps import Steps, _apply_integrity

TESTS = ["**/src/test/**"]
T = "order/src/test/java/com/acme/order/service/OrderServiceTest.java"
M = "order/src/main/java/com/acme/order/service/OrderService.java"
PY_TESTS = ["**/tests/**", "**/test_*.py", "**/*_test.py"]
PT = "orders/tests/test_pricing.py"
PM = "orders/pricing.py"
TS_TESTS = ["**/*.test.ts", "**/*.spec.ts", "**/tests/**", "**/__tests__/**"]
TT = "orders/tests/pricing.test.ts"
TM = "orders/pricing.ts"
GO_TESTS = ["**/*_test.go"]
GT = "orders/pricing_test.go"


def _diff(path: str, removed: list[str], added: list[str], *, deleted: bool = False) -> str:
    head = [f"diff --git a/{path} b/{path}"]
    if deleted:
        head.append("deleted file mode 100644")
    head += [f"--- a/{path}", f"+++ b/{path}", "@@ -1,3 +1,3 @@"]
    return "\n".join(head + [f"-{r}" for r in removed] + [f"+{a}" for a in added]) + "\n"


def test_clean_source_fix_passes() -> None:
    rep = check_patch(_diff(M, ["  return a.doubleValue();"], ["  return a.setScale(2, HALF_UP);"]), TESTS)
    assert rep.ok and not rep.suspicious


def test_deleting_test_file_is_violation() -> None:
    rep = check_patch(_diff(T, ["class OrderServiceTest {}"], [], deleted=True), TESTS)
    assert not rep.ok and "删除了测试文件" in rep.violations[0]


def test_disabling_test_is_violation() -> None:
    rep = check_patch(_diff(T, ["    @Test"], ["    @Disabled", "    @Test"]), TESTS)
    assert not rep.ok and any("跳过" in v for v in rep.violations)
    rep = check_patch(_diff(T, [], ["        assumeTrue(false);"]), TESTS)
    assert not rep.ok


def test_removing_assertions_or_test_methods_is_violation() -> None:
    rep = check_patch(_diff(T, ['        assertThat(total).isEqualTo("269.70");'], ["        // flaky"]), TESTS)
    assert any("断言" in v for v in rep.violations)
    rep = check_patch(_diff(T, ["    @Test", "    void a() {}"], []), TESTS)
    assert any("测试方法" in v for v in rep.violations)


def test_editing_test_without_weakening_is_only_suspicious() -> None:
    rep = check_patch(
        _diff(T, ['        assertThat(x).isEqualTo("1");'], ['        assertThat(x).isEqualTo("2");']), TESTS
    )
    assert rep.ok and rep.suspicious


def test_swallowed_catch_and_fixture_special_case_are_suspicious() -> None:
    added = [
        "        try {",
        "            ledger.write(id);",
        "        } catch (RuntimeException e) {",
        '            log.warn("ignored {}", id);',
        "        }",
        '        if (sku.equals("SKU-3")) return;',
    ]
    rep = check_patch(_diff(M, [], added), TESTS)
    assert rep.ok
    assert any("catch" in s for s in rep.suspicious) and any("特判" in s for s in rep.suspicious)


def test_catch_that_rethrows_is_not_flagged() -> None:
    added = ["        } catch (SQLException e) {", "            throw new IllegalStateException(e);", "        }"]
    assert not check_patch(_diff(M, [], added), TESTS).suspicious


def test_python_disabled_test_is_violation() -> None:
    rep = check_patch(
        _diff(
            PT,
            ["def test_no_coupon() -> None:", "    assert total == 10"],
            ["@pytest.mark.skip", "def test_no_coupon() -> None:", "    assert total == 10"],
        ),
        PY_TESTS,
    )
    assert not rep.ok and any("跳过" in v for v in rep.violations)


def test_python_removing_test_or_assertions_is_violation() -> None:
    rep = check_patch(_diff(PT, ["def test_a() -> None:", "    assert x == 1"], []), PY_TESTS)
    assert any("测试方法" in v for v in rep.violations)
    rep = check_patch(_diff(PT, ["    assert order.total == 10"], ["    pass"]), PY_TESTS)
    assert any("断言" in v for v in rep.violations)


def test_python_swallowed_except_is_suspicious() -> None:
    rep = check_patch(_diff(PM, [], ["    except ValueError:", "        pass"]), PY_TESTS)
    assert rep.ok and any("except" in s for s in rep.suspicious)


def test_typescript_disabled_test_is_violation() -> None:
    rep = check_patch(_diff(TT, [], ['test.skip("a", () => {});']), TS_TESTS)
    assert not rep.ok and any("跳过" in v for v in rep.violations)
    rep = check_patch(_diff(TT, [], ['test("a", { skip: true }, () => {});']), TS_TESTS)
    assert not rep.ok and any("跳过" in v for v in rep.violations)
    rep = check_patch(_diff(TT, [], ['it.todo("a");']), TS_TESTS)
    assert not rep.ok


def test_typescript_removing_test_or_assertions_is_violation() -> None:
    rep = check_patch(_diff(TT, ['test("a", () => {', "  assert.equal(x, 1);", "});"], []), TS_TESTS)
    assert any("测试方法" in v for v in rep.violations)
    rep = check_patch(_diff(TT, ["  assert.equal(order.total, 10);"], ["  // flaky"]), TS_TESTS)
    assert any("断言" in v for v in rep.violations)


def test_typescript_swallowed_catch_is_suspicious() -> None:
    added = ["    try {", "        run();", "    } catch (error) {", "        console.warn(error);", "    }"]
    rep = check_patch(_diff(TM, [], added), TS_TESTS)
    assert rep.ok and any("catch" in s for s in rep.suspicious)


def test_go_disabled_test_is_violation() -> None:
    rep = check_patch(_diff(GT, [], ['\tt.Skip("flaky")']), GO_TESTS)
    assert not rep.ok and any("跳过" in v for v in rep.violations)


def test_go_removing_test_or_assertions_is_violation() -> None:
    rep = check_patch(_diff(GT, ["func TestA(t *testing.T) {", '\tt.Fatal("x")', "}"], []), GO_TESTS)
    assert any("测试方法" in v for v in rep.violations)
    rep = check_patch(_diff(GT, ['\tt.Fatalf("x")'], ["\t// flaky"]), GO_TESTS)
    assert any("断言" in v for v in rep.violations)


def test_apply_integrity_overrides_verifier() -> None:
    ver = VerificationResult(
        checks=[Check(name="test_integrity", status="passed", detail="agent says ok")], passed=True
    )
    bad = {"diff": _diff(T, ["class X {}"], [], deleted=True)}
    out = _apply_integrity(ver, bad, TESTS)
    assert not out.passed and out.feedback and "完整性" in out.feedback
    assert [c.status for c in out.checks if c.name == "test_integrity"] == ["failed"]

    good = _apply_integrity(ver, {"diff": _diff(M, ["a"], ["b"])}, TESTS)
    assert good.passed and [c.name for c in good.checks].count("test_integrity") == 1


class _Profile:
    def test_paths(self) -> list[str]:
        return TESTS


def _steps_for(settings: Settings) -> Steps:
    """只测 _check_integrity：绕开 __init__（它需要数据库与网关），只填这个方法用到的两个属性。"""
    steps = object.__new__(Steps)
    steps.settings = settings
    steps.adapters = SimpleNamespace(profile=_Profile())
    return steps


def test_check_integrity_switch_disables_scan() -> None:
    """integrity_check=false（对照实验）：系统不覆盖 Verifier 的结论，补丁原样透传。"""
    ver = VerificationResult(checks=[Check(name="test_integrity", status="passed", detail="agent")], passed=True)
    bad = {"diff": _diff(T, ["class X {}"], [], deleted=True)}

    assert _steps_for(Settings(integrity_check=True))._check_integrity(ver, bad).passed is False
    untouched = _steps_for(Settings(integrity_check=False))._check_integrity(ver, bad)
    assert untouched is ver and untouched.passed
