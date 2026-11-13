"""GoProfile 与 Go 代码索引。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from twinfix.adapters.sandbox import LocalSandbox
from twinfix.codeintel import get_index, invalidate
from twinfix.codeintel.go import base_type_name
from twinfix.codeintel.go_index import build_go_index
from twinfix.profiles.base import TestSelector
from twinfix.profiles.factory import get_profile
from twinfix.profiles.go import GoProfile, parse_go_test_json

FILES = {
    "go.mod": "module example.com/shop\n\ngo 1.23\n",
    "orders/service.go": """\
package orders

import "context"

type Service struct {
    Repo Repository
}

type Repository interface {
    Save(ctx context.Context, sku string) error
}

func NewService(r Repository) *Service {
    return &Service{Repo: r}
}

func (s *Service) Create(ctx context.Context, sku string) error {
    svc := NewService(s.Repo)
    if err := svc.validate(sku); err != nil {
        return err
    }
    return s.Repo.Save(ctx, sku)
}

func (s *Service) validate(sku string) error { return nil }
""",
    "orders/repo.go": """\
package orders

type JdbcRepository struct{}

func (r *JdbcRepository) Save(ctx context.Context, sku string) error { return nil }
""",
    "cmd/main.go": """\
package main

import "example.com/shop/orders"

func main() {
    s := orders.NewService(nil)
    _ = s.Create(nil, "x")
}
""",
}


def test_detect_and_commands() -> None:
    profile = GoProfile()
    assert profile.detect(["go.mod"])
    assert profile.detect(["go.work"])
    assert not profile.detect(["pom.xml"])
    assert profile.build_command(None) == ["go", "build", "./..."]
    assert profile.build_command("./orders")[-1] == "./orders"
    assert profile.build_command("orders")[-1] == "./orders"
    assert profile.build_command("example.com/shop/orders")[-1] == "example.com/shop/orders"
    assert profile.test_command(TestSelector(classes=["orders"]))[-1] == "./orders"
    assert profile.test_command(TestSelector(classes=["./orders::TestCreate"])) == [
        "go",
        "test",
        "-json",
        "-count=1",
        "-run",
        "^TestCreate$",
        "./orders",
    ]
    assert profile.test_command(TestSelector(methods=["TestA", "TestB"]))[-2:] == ["^(?:TestA|TestB)$", "./..."]
    subtest = profile.test_command(TestSelector(methods=["TestCreate/success"]))
    assert subtest[-2:] == ["TestCreate/success", "./..."]
    assert profile.test_paths() == ["**/*_test.go"]
    assert profile.source_suffixes() == (".go",)
    assert profile.runtime_probe() is None
    assert get_profile("go").name == "go"
    assert get_profile("golang").name == "go"
    assert get_profile("kotlin").name == "java"


def test_selector_matching() -> None:
    profile = GoProfile()
    assert profile.case_matches("./orders::TestCreate", "example.com/shop/orders#TestCreate")
    assert profile.case_matches("example.com/shop/orders::TestCreate", "example.com/shop/orders#TestCreate")
    assert profile.case_matches("TestCreate", "example.com/shop/orders#TestCreate/sub")
    assert profile.case_matches("./orders", "example.com/shop/orders#TestCreate")
    assert not profile.case_matches("./orders::TestCreate", "example.com/shop/orders#TestOther")
    assert not profile.case_matches("./inventory", "example.com/shop/orders#TestCreate")


def test_parse_go_test_json() -> None:
    events = [
        {"Action": "run", "Package": "example.com/shop/orders", "Test": "TestCreate"},
        {
            "Action": "output",
            "Package": "example.com/shop/orders",
            "Test": "TestCreate",
            "Output": "    service_test.go:42: got 1, want 2\n",
        },
        {"Action": "fail", "Package": "example.com/shop/orders", "Test": "TestCreate"},
        {"Action": "fail", "Package": "example.com/shop/orders"},
        {"Action": "run", "Package": "example.com/shop/orders", "Test": "TestOK"},
        {"Action": "pass", "Package": "example.com/shop/orders", "Test": "TestOK"},
        {"Action": "skip", "Package": "example.com/shop/orders", "Test": "TestSkip"},
    ]
    cases = {case.name: case for case in parse_go_test_json("\n".join(json.dumps(e) for e in events))}
    assert cases["example.com/shop/orders#TestCreate"].status == "failed"
    assert "got 1, want 2" in cases["example.com/shop/orders#TestCreate"].message
    assert cases["example.com/shop/orders#TestOK"].status == "passed"
    assert cases["example.com/shop/orders#TestSkip"].status == "skipped"


def test_parse_go_test_build_failure() -> None:
    events = [
        {"Action": "output", "Package": "example.com/shop/orders", "Output": "# example.com/shop/orders\n"},
        {"Action": "output", "Package": "example.com/shop/orders", "Output": "./service.go:3: undefined: Foo\n"},
        {"Action": "fail", "Package": "example.com/shop/orders"},
    ]
    cases = parse_go_test_json("\n".join(json.dumps(e) for e in events))
    assert len(cases) == 1
    assert cases[0].name == "example.com/shop/orders#<build>"
    assert cases[0].status == "error"
    assert "undefined: Foo" in cases[0].message


def test_parse_stacktrace_and_frame_location() -> None:
    profile = GoProfile(("example.com/shop",))
    text = (
        "panic: boom\n\n"
        "goroutine 1 [running]:\n"
        "example.com/shop/orders.(*Service).Create(0xc000, {0x0, 0x0})\n"
        "\t/workspace/orders/service.go:42 +0x1d\n"
        "example.com/shop/orders.TestCreate(0xc000)\n"
        "\t/workspace/orders/service_test.go:12 +0x55\n"
        "testing.tRunner(0xc000, 0x4a)\n"
        "\t/usr/local/go/src/testing/testing.go:1595 +0x1b0\n"
    )
    frames = profile.parse_stacktrace(text)
    assert [frame.symbol for frame in frames] == [
        "example.com/shop/orders.(*Service).Create",
        "example.com/shop/orders.TestCreate",
        "testing.tRunner",
    ]
    assert frames[0].is_project is True
    assert frames[2].is_project is False
    location = profile.frame_to_location(frames[0], ["orders/service.go", "orders/repo.go"])
    assert location is not None and location.path == "orders/service.go" and location.line == 42


def test_base_type_name() -> None:
    assert base_type_name("*Service") == "Service"
    assert base_type_name("[]*Service") == "Service"
    assert base_type_name("map[string]Repository") == "Repository"
    assert base_type_name("Box[Repository]") == "Box"
    assert base_type_name("repo.Repository") == "repo.Repository"
    assert base_type_name("func(int) error") == ""


def test_go_index_symbols_calls_and_interfaces() -> None:
    index = build_go_index(FILES)
    assert index.meta.files == 4
    ids = set(index.symbols)
    assert {
        "example.com/shop/orders.Service",
        "example.com/shop/orders.Service#Create/2",
        "example.com/shop/orders.Service#validate/1",
        "example.com/shop/orders.Repository",
        "example.com/shop/orders.Repository#Save/2",
        "example.com/shop/orders.NewService/1",
        "example.com/shop/orders.JdbcRepository#Save/2",
        "example.com/shop/orders.Service#field:Repo",
    } <= ids

    service, candidates = index.resolve_symbol("example.com/shop/orders.Service")
    assert service is not None and service.kind == "class" and not candidates
    create = index.symbols["example.com/shop/orders.Service#Create/2"]
    assert create.container == "example.com/shop/orders.Service"
    assert create.return_type == "error"

    callers = index.find_references(create, "calls")
    assert {ref.edge.src for ref in callers} == {"example.com/shop/cmd.main/0"}

    save = index.symbols["example.com/shop/orders.Repository#Save/2"]
    assert {ref.edge.src for ref in index.find_references(save, "calls")} == {
        "example.com/shop/orders.Service#Create/2"
    }

    constructed = index.find_references(service, "calls")
    assert {ref.edge.src for ref in constructed} == {"example.com/shop/orders.NewService/1"}

    outline = index.outline("orders/service.go")
    assert [symbol.name for symbol in outline][:4] == ["Service", "Repo", "Repository", "Save"]
    assert [symbol.id for symbol in index.search("Service", "class")] == ["example.com/shop/orders.Service"]


def test_go_index_embedded_methods() -> None:
    files = {
        "go.mod": "module example.com/base\n",
        "base.go": """\
package base

type Base struct{}

func (b *Base) Hello() {}

type Child struct {
    Base
}

func (c *Child) Run() {
    c.Hello()
}
""",
    }
    index = build_go_index(files)
    assert any(
        edge.kind == "extends" and edge.src == "example.com/base.Child" and edge.dst == "example.com/base.Base"
        for edge in index.edges
    )
    hello = index.symbols["example.com/base.Base#Hello/0"]
    assert {ref.edge.src for ref in index.find_references(hello, "calls")} == {"example.com/base.Child#Run/0"}


def test_go_index_short_var_composite_literal() -> None:
    files = {
        "go.mod": "module example.com/x\n",
        "x.go": """\
package x

type Service struct{}

func (s *Service) Do() {}

func Run() {
    s := &Service{}
    s.Do()
}
""",
    }
    index = build_go_index(files)
    do = index.symbols["example.com/x.Service#Do/0"]
    assert {ref.edge.src for ref in index.find_references(do, "calls")} == {"example.com/x.Run/0"}


async def test_index_cache_is_language_specific_for_go(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path / "workspaces")
    ws = await sandbox.create_workspace("acme/go", "abc")
    await sandbox.write_file(ws, "go.mod", "module example.com\n")
    await sandbox.write_file(ws, "app/service.go", "package app\n\ntype Service struct{}\n")
    try:
        go = await get_index(sandbox, ws, "go")
        assert "example.com/app.Service" in go.symbols
        java = await get_index(sandbox, ws, "java")
        assert "example.com/app.Service" not in java.symbols
        assert await get_index(sandbox, ws, "go") is go
    finally:
        invalidate(ws.id)


def test_orchestrator_selects_go_profile_and_image() -> None:
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
    assert steps._profile_of(Project(name="g", repo="acme/go", language="go")).name == "go"
    assert steps._profile_for_inc(Incident(meta={"language": "go"})).name == "go"
    assert steps._sandbox_image(Project(name="g", repo="acme/go", language="go")) == "go:1.25"
    assert steps._sandbox_image(Project(name="p", repo="acme/py", language="python")) == "py:3.12"
    assert steps._sandbox_image(Project(name="j", repo="acme/java", language="java")) is None
    assert (
        steps._sandbox_image(Project(name="g", repo="acme/go", language="go", config={"sandbox_image": "custom"}))
        == "custom"
    )
