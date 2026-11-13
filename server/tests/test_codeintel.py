"""代码智能：Java 符号解析、跨文件引用、沙箱批量读取与工具输出。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from twinfix.adapters.sandbox import LocalSandbox
from twinfix.adapters.types import Workspace
from twinfix.codeintel import get_index, invalidate
from twinfix.codeintel.index import build_java_index
from twinfix.codeintel.java import simple_type_names
from twinfix.gateway import tools as T

FILES = {
    "src/main/java/com/acme/OrderService.java": """
package com.acme;

import java.util.List;

public class OrderService extends BaseService implements Runnable {
    private final PriceCalculator calculator;

    public OrderService(PriceCalculator calculator) {
        this.calculator = calculator;
    }

    public int pay(List<OrderLine> lines) {
        int total = calculator.calculate(lines, null);
        this.validate(total);
        helper();
        return total;
    }

    private void validate(int total) {
        if (total < 0) {
            throw new IllegalArgumentException("negative");
        }
    }

    private void helper() {
        validate(0);
    }

    public void run() {
        helper();
    }
}
""",
    "src/main/java/com/acme/BaseService.java": """
package com.acme;

public abstract class BaseService {
    protected void validate(int total) {
    }
}
""",
    "src/main/java/com/acme/PriceCalculator.java": """
package com.acme;

import java.util.List;

public class PriceCalculator {
    public int calculate(List<OrderLine> lines, Object coupon) {
        return lines.size();
    }
}
""",
    "src/main/java/com/acme/OrderLine.java": """
package com.acme;

public record OrderLine(int quantity, String sku) {
}
""",
}


@pytest.fixture
def index():
    return build_java_index(FILES)


def test_symbols_and_outline(index) -> None:
    assert index.meta.files == 4
    service, candidates = index.resolve_symbol("OrderService")
    assert service is not None and service.kind == "class" and not candidates
    assert service.id == "com.acme.OrderService"

    pay = index.symbols["com.acme.OrderService#pay/1"]
    assert pay.start_line >= 1
    assert pay.signature.startswith("public int pay")

    field = index.symbols["com.acme.OrderService#field:calculator"]
    assert field.return_type == "PriceCalculator"
    assert field.kind == "field"

    outline = index.outline("src/main/java/com/acme/OrderService.java")
    assert [s.name for s in outline][:3] == ["OrderService", "calculator", "OrderService"]
    assert any(s.kind == "method" and s.name == "pay" for s in outline)


def test_record_accessors_are_indexed(index) -> None:
    quantity = index.symbols["com.acme.OrderLine#quantity/0"]
    assert quantity.kind == "method"
    assert quantity.return_type == "int"
    assert quantity.signature == "public int quantity()"


def test_call_resolution(index) -> None:
    calculate = index.symbols["com.acme.PriceCalculator#calculate/2"]
    callers = index.find_references(calculate, kind="calls")
    assert len(callers) == 1
    assert callers[0].edge.src == "com.acme.OrderService#pay/1"
    assert callers[0].edge.dst == calculate.id

    validate = index.symbols["com.acme.OrderService#validate/1"]
    refs = index.find_references(validate, kind="calls")
    assert {r.edge.src for r in refs} == {
        "com.acme.OrderService#pay/1",
        "com.acme.OrderService#helper/0",
    }
    assert all(r.edge.dst == validate.id for r in refs)


def test_inheritance_edges(index) -> None:
    service = index.symbols["com.acme.OrderService"]
    extends = [e for e in index.edges if e.src == service.id and e.kind == "extends"]
    assert [(e.dst, e.name) for e in extends] == [("com.acme.BaseService", "BaseService")]
    # Runnable 不在仓库内，不应伪造解析结果
    assert not [e for e in index.edges if e.src == service.id and e.kind == "implements"]


def test_search_and_ambiguity(index) -> None:
    hits = index.search("validate")
    assert {s.id for s in hits} >= {
        "com.acme.OrderService#validate/1",
        "com.acme.BaseService#validate/1",
    }
    assert index.search("OrderService", kind="class")
    # 按方法过滤时，类名命中只通过 qualified_name 带出该类的方法
    assert all(s.kind == "method" for s in index.search("OrderService", kind="method"))

    symbol, candidates = index.resolve_symbol("validate")
    assert symbol is None
    assert {s.id for s in candidates} == {
        "com.acme.BaseService#validate/1",
        "com.acme.OrderService#validate/1",
    }


def test_simple_type_names() -> None:
    assert simple_type_names("Map<String, com.acme.Foo[]>") == ["Map", "String", "Foo"]
    assert simple_type_names("int") == []


def test_read_sources_filters_and_caps(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "A.java").write_text("class A {}", encoding="utf-8")
    (root / "src" / "B.py").write_text("print(1)", encoding="utf-8")
    (root / "target").mkdir()
    (root / "target" / "C.java").write_text("class C {}", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "D.java").write_text("class D {}", encoding="utf-8")

    sandbox = LocalSandbox(tmp_path)
    ws = Workspace(id="ws-sources", repo="acme/demo", sha="main", root=root)
    files, truncated = asyncio.run(sandbox.read_sources(ws))
    assert list(files) == ["src/A.java"]
    assert truncated is False

    files, truncated = asyncio.run(sandbox.read_sources(ws, max_files=0))
    assert files == {} and truncated is True

    # 主仓库优先：附加仓库不能挤掉主仓库的名额
    extra = root / "_repos" / "inventory" / "E.java"
    extra.parent.mkdir(parents=True)
    extra.write_text("class E {}", encoding="utf-8")
    files, truncated = asyncio.run(sandbox.read_sources(ws, max_files=2))
    assert list(files) == ["src/A.java", "_repos/inventory/E.java"] and truncated is False


def test_index_cache_is_invalidated(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "A.java").write_text("package p; class A { void a() {} }", encoding="utf-8")
    sandbox = LocalSandbox(tmp_path)
    ws = Workspace(id="ws-cache", repo="acme/demo", sha="main", root=root)

    first = asyncio.run(get_index(sandbox, ws))
    assert "p.A#a/0" in first.symbols
    assert asyncio.run(get_index(sandbox, ws)) is first

    (root / "A.java").write_text("package p; class A { void b() {} }", encoding="utf-8")
    invalidate(ws.id)
    second = asyncio.run(get_index(sandbox, ws))
    assert "p.A#b/0" in second.symbols


def _env(tmp_path: Path, root: Path) -> tuple[SimpleNamespace, Workspace]:
    sandbox = LocalSandbox(tmp_path)
    ws = Workspace(id="ws-tools", repo="acme/demo", sha="main", root=root)
    env = SimpleNamespace(run=SimpleNamespace(workspace=ws), adapters=SimpleNamespace(sandbox=sandbox))
    return env, ws


def test_tools_search_outline_and_references(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    for path, text in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    env, _ = _env(tmp_path, root)

    out = asyncio.run(T.search_symbol(env, T.SymbolSearchArgs(query="calculate")))
    assert "PriceCalculator#calculate/2" in out.content

    out = asyncio.run(T.code_outline(env, T.OutlineArgs(path="src/main/java/com/acme/OrderService.java")))
    assert "OrderService" in out.content and "pay" in out.content

    out = asyncio.run(T.find_references(env, T.ReferencesArgs(symbol="com.acme.OrderService#validate/1")))
    assert "2 references (2 resolved)" in out.content

    out = asyncio.run(T.find_references(env, T.ReferencesArgs(symbol="validate")))
    assert "ambiguous" in out.content


def test_git_tools(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "A.java").write_text("package p;\nclass A { void a() {} }\n", encoding="utf-8")
    sandbox = LocalSandbox(tmp_path)
    ws = Workspace(id="ws-git", repo="acme/demo", sha="main", root=root)
    asyncio.run(sandbox.git(ws, ["init", "-q"]))
    asyncio.run(sandbox.git(ws, ["add", "-A"]))
    asyncio.run(sandbox.git(ws, ["commit", "-q", "-m", "initial"]))
    env = SimpleNamespace(run=SimpleNamespace(workspace=ws), adapters=SimpleNamespace(sandbox=sandbox))

    log = asyncio.run(T.git_log(env, T.GitLogArgs(limit=5)))
    assert "initial" in log.content

    blame = asyncio.run(T.git_blame(env, T.BlameArgs(path="A.java", start_line=1, end_line=2)))
    assert "twinfix[bot]" in blame.content
    assert "class A" in blame.content


def test_docker_tar_source_parser() -> None:
    import io
    import tarfile

    from twinfix.adapters.docker_sandbox import _read_tar_sources

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text in (("src/A.java", "class A {}"), ("src/B.py", "print(1)"), ("src/C.java", "class C {}")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    files, truncated = _read_tar_sources(buf.getvalue(), (".java",), max_files=10, max_bytes=1024)
    assert files == {"src/A.java": "class A {}", "src/C.java": "class C {}"}
    assert truncated is False

    files, truncated = _read_tar_sources(buf.getvalue(), (".java",), max_files=1, max_bytes=1024)
    assert len(files) == 1 and truncated is True


def test_constructor_calls_are_resolved() -> None:
    index = build_java_index({"A.java": "package p; class A { A(int x) {} } class B { void b() { new A(1); } }"})
    ctor = index.symbols["p.A#<init>/1"]
    refs = index.find_references(ctor, kind="calls")
    assert len(refs) == 1
    assert refs[0].edge.src == "p.B#b/0"
    assert refs[0].edge.dst == "p.A#<init>/1"
