"""修复提交的范围：进提交的只有补丁声明的文件与复现用例，Fixer 自验的临时文件不合并。"""

from __future__ import annotations

from twinfix.domain.schemas import ReproResult
from twinfix.orchestrator.steps import _declared_repro_files, _norm_path, _scoped_paths

M = "order/src/main/java/com/acme/order/service/OrderService.java"
T = "order/src/test/java/com/acme/order/service/OrderServiceNoCouponTest.java"
SCRATCH = "order/src/test/java/com/acme/order/service/TempSanityTest.java"
TESTS = ["**/src/test/**"]


def test_scoped_paths_keeps_declared_and_existing_files_only() -> None:
    keep, dropped = _scoped_paths([M, T, SCRATCH], declared={_norm_path(M), T}, tracked={M})
    assert keep == [M, T]
    assert dropped == [SCRATCH]


def test_scoped_paths_commits_unreported_edits_to_existing_files() -> None:
    """Agent 漏报的既有文件改动仍然提交：宁可多带一个改动，也不让修复悄悄少一个文件。"""
    keep, dropped = _scoped_paths([M], declared=set(), tracked={M})
    assert keep == [M] and dropped == []


def test_norm_path_handles_agent_spellings() -> None:
    assert _norm_path("./order/src/Main.java") == "order/src/Main.java"
    assert _norm_path(r"order\src\Main.java") == "order/src/Main.java"
    assert _norm_path("/order/src/Main.java") == "order/src/Main.java"


def test_repro_files_default_to_the_declared_test_file() -> None:
    repro = ReproResult(test_file="orders/tests/test_a.py", test_selector="t", command="pytest")
    assert _declared_repro_files(repro) == ["orders/tests/test_a.py"]


def test_repro_files_include_declared_auxiliary_files_and_normalize() -> None:
    repro = ReproResult(
        test_file="./orders/tests/test_a.py",
        test_selector="t",
        command="pytest",
        test_files=[r"orders\tests\test_b.py", "orders/tests/test_a.py"],
    )
    assert _declared_repro_files(repro) == ["orders/tests/test_a.py", "orders/tests/test_b.py"]


def test_undeclared_temp_test_is_not_committed() -> None:
    """Reproducer/Fixer 留下的临时测试不在 declared 里，_scoped_paths 必须丢弃。"""
    keep, dropped = _scoped_paths([M, T, SCRATCH], declared={_norm_path(M), T}, tracked={M})
    assert keep == [M, T] and dropped == [SCRATCH]
