"""多仓库：项目配置解析、附加仓库检出、工具 repo 路由与 webhook 匹配。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from twinfix.adapters.sandbox import LocalSandbox
from twinfix.adapters.types import Workspace
from twinfix.gateway import tools as T
from twinfix.multirepo import involved_specs, repo_for_service, repo_root_prefix, repo_specs


def _project(repo: str = "acme/shop", config: dict | None = None):
    return SimpleNamespace(repo=repo, branch_map={"test": "dev"}, config=config or {})


def test_repo_specs_defaults_to_primary() -> None:
    specs = repo_specs(_project())
    assert [(s.name, s.repo, s.primary) for s in specs] == [("shop", "acme/shop", True)]
    assert repo_root_prefix(specs[0], specs[0]) == ""


def test_repo_specs_from_config_and_dedupes() -> None:
    project = _project(
        config={
            "primary_repo_name": "order",
            "repos": [
                {"name": "inventory", "repo": "acme/inventory", "branch_map": {"test": "main"}},
                {"repo": "acme/payment"},
                {"name": "inventory", "repo": "acme/other"},
                {"name": "dup", "repo": "acme/shop"},
                "not-a-dict",
            ],
        }
    )
    specs = repo_specs(project)
    assert [(s.name, s.repo, s.primary) for s in specs] == [
        ("order", "acme/shop", True),
        ("inventory", "acme/inventory", False),
        ("payment", "acme/payment", False),
    ]
    assert specs[1].branch_map == {"test": "main"}
    assert repo_root_prefix(specs[2], specs[0]) == "_repos/payment"


def test_service_repo_mapping_and_involved_specs() -> None:
    project = _project(
        config={
            "repos": [
                {"name": "inventory", "repo": "acme/inventory"},
                {"name": "payment", "repo": "acme/payment"},
            ],
            "service_repos": {"inventory": "inventory", "pay": "acme/payment"},
        }
    )
    assert repo_for_service(project, "inventory").name == "inventory"
    assert repo_for_service(project, "pay").name == "payment"
    assert repo_for_service(project, "unknown").primary
    assert [s.name for s in involved_specs(project, "inventory")] == ["shop", "inventory"]
    assert [s.name for s in involved_specs(project, None, {"repos": ["payment"]})] == ["shop", "payment"]


def _multi_env(tmp_path: Path) -> tuple[SimpleNamespace, Workspace]:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "A.java").write_text("package p; class A { void a() {} }", encoding="utf-8")
    inv = root / "_repos" / "inventory" / "src"
    inv.mkdir(parents=True)
    (inv / "B.java").write_text("package q; class B { void b() {} }", encoding="utf-8")
    ws = Workspace(id="ws-multi", repo="acme/shop", sha="main", root=root)
    run = SimpleNamespace(
        workspace=ws,
        repo="acme/shop",
        repo_workspaces={
            "shop": {"repo": "acme/shop", "sha": "main", "root": ""},
            "inventory": {"repo": "acme/inventory", "sha": "main", "root": "_repos/inventory"},
        },
    )
    env = SimpleNamespace(run=run, adapters=SimpleNamespace(sandbox=LocalSandbox(tmp_path)))
    return env, ws


def test_tools_route_reads_to_extra_repo(tmp_path: Path) -> None:
    env, _ = _multi_env(tmp_path)
    out = asyncio.run(T.read_file(env, T.ReadArgs(repo="inventory", path="src/B.java")))
    assert "class B" in out.content
    out = asyncio.run(T.read_file(env, T.ReadArgs(repo="shop", path="src/A.java")))
    assert "class A" in out.content
    with pytest.raises(T.ToolError, match="repo not loaded"):
        asyncio.run(T.read_file(env, T.ReadArgs(repo="nope", path="src/A.java")))


def test_tools_filter_symbols_by_repo(tmp_path: Path) -> None:
    env, _ = _multi_env(tmp_path)
    out = asyncio.run(T.search_symbol(env, T.SymbolSearchArgs(query="B", repo="inventory")))
    assert "q.B" in out.content
    out = asyncio.run(T.search_symbol(env, T.SymbolSearchArgs(query="A", repo="inventory")))
    assert "no symbols match" in out.content
    out = asyncio.run(T.search_symbol(env, T.SymbolSearchArgs(query="A")))
    assert "p.A" in out.content
    out = asyncio.run(T.code_outline(env, T.OutlineArgs(repo="inventory", path="src/B.java")))
    assert "inventory:src/B.java" in out.content and "class B" in out.content


def test_search_code_scopes_and_labels_repos(tmp_path: Path) -> None:
    env, _ = _multi_env(tmp_path)
    out = asyncio.run(T.search_code(env, T.SearchArgs(pattern="class", repo="inventory")))
    assert "inventory:src/B.java" in out.content and "src/A.java" not in out.content
    out = asyncio.run(T.search_code(env, T.SearchArgs(pattern="class")))
    assert "src/A.java" in out.content and "inventory:src/B.java" in out.content


def test_git_tools_use_extra_repo_cwd(tmp_path: Path) -> None:
    env, ws = _multi_env(tmp_path)
    sandbox: LocalSandbox = env.adapters.sandbox
    asyncio.run(sandbox.git(ws, ["init", "-q"], cwd="_repos/inventory"))
    asyncio.run(sandbox.git(ws, ["add", "-A"], cwd="_repos/inventory"))
    asyncio.run(sandbox.git(ws, ["commit", "-q", "-m", "inventory initial"], cwd="_repos/inventory"))

    log = asyncio.run(T.git_log(env, T.GitLogArgs(repo="inventory", limit=5)))
    assert "inventory initial" in log.content
    blame = asyncio.run(T.git_blame(env, T.BlameArgs(repo="inventory", path="src/B.java", start_line=1, end_line=1)))
    assert "twinfix[bot]" in blame.content


def test_cross_repo_call_resolution() -> None:
    from twinfix.codeintel.index import build_java_index

    index = build_java_index(
        {
            "src/A.java": (
                "package p;\n"
                "import com.acme.inventory.StockRepository;\n"
                'class A { int x(StockRepository r) { return r.available("sku"); } }\n'
            ),
            "_repos/inventory/src/StockRepository.java": (
                "package com.acme.inventory;\npublic interface StockRepository { int available(String sku); }\n"
            ),
        }
    )
    target = index.symbols["com.acme.inventory.StockRepository#available/1"]
    refs = index.find_references(target, kind="calls")
    assert any(ref.edge.src == "p.A#x/1" and ref.edge.path == "src/A.java" for ref in refs)


def test_validate_config_reports_shape_errors() -> None:
    from twinfix.multirepo import validate_config

    assert validate_config({}) == []
    errors = validate_config(
        {
            "primary_repo_name": "shop",
            "repos": [
                {"repo": "not-owner-name"},
                {"name": "shop", "repo": "acme/inventory"},
                {"name": "dup", "repo": "acme/a"},
                {"name": "dup", "repo": "acme/b", "branch_map": "dev"},
            ],
            "service_repos": {"order": 1},
        }
    )
    joined = "\n".join(errors)
    assert "repo 需为 owner/name" in joined
    assert "与主仓库名冲突" in joined
    assert "name 重复" in joined
    assert "branch_map 必须是对象" in joined
    assert "service_repos['order'] 必须是非空仓库名或仓库名数组" in joined
