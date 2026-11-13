"""PythonProfile 与 Python 代码索引。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from twinfix.adapters.sandbox import LocalSandbox
from twinfix.codeintel import get_index, invalidate
from twinfix.codeintel.python_index import build_python_index
from twinfix.profiles.base import TestSelector
from twinfix.profiles.factory import get_profile
from twinfix.profiles.python import PythonProfile, parse_pytest_junit


def test_detect_and_commands() -> None:
    profile = PythonProfile()
    assert profile.detect(["pyproject.toml"])
    assert profile.detect(["requirements.txt"])
    assert not profile.detect(["pom.xml"])
    assert profile.test_command(TestSelector(classes=["tests/test_x.py::test_a"]))[-1] == "tests/test_x.py::test_a"
    assert profile.test_command(TestSelector(methods=["tests/test_x.py#test_a"]))[-1] == "tests/test_x.py::test_a"
    assert profile.build_command(None)[-1] == "."
    assert profile.build_command("service")[-1] == "service"
    assert profile.runtime_probe() is None
    assert get_profile("py").name == "python"
    assert get_profile("kotlin").name == "java"


def test_selector_matching() -> None:
    profile = PythonProfile()
    assert profile.case_matches("tests/test_x.py::test_a", "tests.test_x#test_a")
    assert profile.case_matches("tests/test_x.py", "tests.test_x#test_b")
    assert not profile.case_matches("tests/test_y.py::test_a", "tests.test_x#test_a")
    assert not profile.case_matches("tests/test_x.py::test_a", "tests.test_x#test_b")


def test_parse_pytest_junit() -> None:
    xml = """<testsuite>
      <testcase classname="tests.test_x" name="test_a"><failure message="boom">trace</failure></testcase>
      <testcase classname="tests.test_x" name="test_b"/>
      <testcase classname="tests.test_x" name="test_c"><skipped message="skip me"/></testcase>
      <testcase classname="tests.test_x" name="test_d"><error message="err"/></testcase>
    </testsuite>"""
    cases = {c.name: (c.status, c.message) for c in parse_pytest_junit(xml)}
    assert cases["tests.test_x#test_a"] == ("failed", "boom")
    assert cases["tests.test_x#test_b"] == ("passed", "")
    assert cases["tests.test_x#test_c"][0] == "skipped"
    assert cases["tests.test_x#test_d"][0] == "error"
    # 损坏的 XML 也不能让工具整体报错
    assert parse_pytest_junit("<testsuite><testcase") == []


def test_parse_stacktrace_and_frame_location() -> None:
    profile = PythonProfile(("app",))
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/pkg/service.py", line 12, in execute\n'
        "    helper()\n"
        "tests/test_service.py:3: in test_execute\n"
        "    execute()\n"
    )
    frames = profile.parse_stacktrace(text)
    assert [f.symbol for f in frames] == ["/app/pkg/service.py:execute", "tests/test_service.py:test_execute"]
    assert frames[0].is_project is True
    location = profile.frame_to_location(frames[0], ["pkg/service.py", "pkg/other.py"])
    assert location is not None and location.path == "pkg/service.py" and location.line == 12


def test_python_index_symbols_calls_and_inheritance() -> None:
    files = {
        "app/models.py": """\
class Base:
    def run(self, x: int) -> str:
        return str(x)

class Thing(Base):
    def go(self, y):
        return self.run(y)
""",
        "app/service.py": """\
from app.models import Thing

def helper(x: int) -> str:
    return Thing().go(x)

class Service:
    def __init__(self):
        self.thing = Thing()

    def execute(self, n):
        return helper(n)
""",
        "app/main.py": """\
from app.service import Service

def main():
    return Service().execute(3)
""",
    }
    index = build_python_index(files)
    ids = set(index.symbols)
    assert {"app.models.Base", "app.models.Base#run/1", "app.models.Thing#go/1"} <= ids
    assert {"app.service.helper/1", "app.service.Service#execute/1", "app.main.main/0"} <= ids

    base_run, _ = index.resolve_symbol("app.models.Base#run/1")
    assert base_run is not None
    refs = index.find_references(base_run, "calls")
    assert any(ref.edge.src == "app.models.Thing#go/1" for ref in refs)

    thing, _ = index.resolve_symbol("app.models.Thing")
    assert thing is not None
    constructs = index.find_references(thing, "calls")
    assert {ref.edge.src for ref in constructs} >= {"app.service.helper/1", "app.service.Service#__init__/0"}

    outline = index.outline("app/models.py")
    assert [s.name for s in outline] == ["Base", "run", "Thing", "go"]
    assert [s.id for s in index.search("Thing", "class")] == ["app.models.Thing"]


def test_python_index_src_layout() -> None:
    files = {
        "src/pkg/__init__.py": "",
        "src/pkg/mod.py": "class Thing:\n    def go(self):\n        pass\n",
        "tests/test_use.py": "from pkg.mod import Thing\n\ndef test_go():\n    Thing().go()\n",
    }
    index = build_python_index(files)
    thing, _ = index.resolve_symbol("pkg.mod.Thing")
    assert thing is not None
    assert thing.path == "src/pkg/mod.py"
    method, _ = index.resolve_symbol("pkg.mod.Thing#go/0")
    assert method is not None and method.path == "src/pkg/mod.py"


def test_python_index_relative_import_and_type_ref() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/base.py": "class Base:\n    pass\n",
        "pkg/sub/mod.py": """\
from ..base import Base

def take(x: Base) -> Base:
    return x
""",
    }
    index = build_python_index(files)
    refs = [e for e in index.edges if e.src == "pkg.sub.mod.take/1"]
    assert {(e.kind, e.dst) for e in refs} == {("type_ref", "pkg.base.Base")}
    child = build_python_index(
        {
            "pkg/base.py": "class Base:\n    pass\n",
            "pkg/sub/mod.py": "from ..base import Base\n\nclass Child(Base):\n    pass\n",
        }
    )
    assert any(e.kind == "extends" and e.src == "pkg.sub.mod.Child" and e.dst == "pkg.base.Base" for e in child.edges)


async def test_run_tests_parses_real_pytest_junit(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path / "workspaces")
    sandbox.allow_exec = True
    ws = await sandbox.create_workspace("acme/py", "abc")
    await sandbox.write_file(
        ws,
        "tests/test_math.py",
        "def test_ok():\n    assert 1 + 1 == 2\n\n\ndef test_fail():\n    assert 1 + 1 == 3\n",
    )
    profile = PythonProfile(interpreter=sys.executable)
    build = await profile.build(sandbox, ws, None)
    assert build.ok, build.stderr
    report = await profile.run_tests(sandbox, ws, TestSelector())
    statuses = {case.name: case.status for case in report.cases}
    assert statuses.get("tests.test_math#test_ok") == "passed"
    assert statuses.get("tests.test_math#test_fail") == "failed"
    assert [case.name for case in report.failed] == ["tests.test_math#test_fail"]


async def test_index_cache_is_language_specific(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path / "workspaces")
    ws = await sandbox.create_workspace("acme/mixed", "abc")
    await sandbox.write_file(ws, "src/A.java", "class A { void m() {} }\n")
    await sandbox.write_file(ws, "src/a.py", "class A:\n    def m(self):\n        pass\n")
    try:
        java = await get_index(sandbox, ws, "java")
        python = await get_index(sandbox, ws, "python")
        assert java is not python
        assert any(s.id.endswith("#m/0") for s in java.symbols.values())
        assert any(s.id == "a.A#m/0" for s in python.symbols.values())
        assert await get_index(sandbox, ws, "python") is python
    finally:
        invalidate(ws.id)


def test_orchestrator_selects_profile_by_project_language() -> None:
    from types import SimpleNamespace

    from twinfix.domain.models import Incident, Project
    from twinfix.orchestrator.steps import Steps
    from twinfix.profiles.java import JavaProfile

    steps = object.__new__(Steps)
    steps.adapters = SimpleNamespace(profile=JavaProfile())
    assert steps._profile_of(Project(name="p", repo="acme/py", language="python")).name == "python"
    assert steps._profile_of(Project(name="j", repo="acme/java", language="java")).name == "java"
    assert steps._profile_for_inc(Incident(meta={"language": "python"})).name == "python"
    assert steps._profile_for_inc(Incident(meta={})).name == "java"


def test_python_index_is_sync_build() -> None:
    # build_python_index 是纯函数，可被 asyncio.to_thread 调用
    index = asyncio.run(asyncio.to_thread(build_python_index, {"a.py": "def f():\n    pass\n"}))
    assert "a.f/0" in index.symbols
