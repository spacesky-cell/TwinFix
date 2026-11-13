"""多仓库修复端到端：附加仓库改动分仓提交、分别开 PR，并按附加仓库 → 主仓库顺序合并。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from tests.conftest import Harness
from tests.test_orchestrator import open_incident, pending_merge, state_of
from twinfix.adapters.gitutil import git
from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.agents.base import AgentContext, AgentResult
from twinfix.agents.stub import FIX_FILE
from twinfix.container import build_adapters
from twinfix.control import MemoryControlPlane
from twinfix.dispatch import InlineDispatcher
from twinfix.domain.enums import AgentName, IncidentState, UserRole
from twinfix.domain.models import Attempt, Incident, IncidentEvent, Patch
from twinfix.domain.schemas import PatchResult, RepoPatch
from twinfix.events import MemoryPublisher
from twinfix.runtime import build_runtime
from twinfix.services import IncidentService

pytestmark = pytest.mark.integration
S = IncidentState

EXTRA_FILE = "src/main/java/acme/InventoryFix.java"


class _MultiRepoFixer:
    name = AgentName.FIXER

    def __init__(self, *, write_extra: bool = True) -> None:
        self.write_extra = write_extra

    async def run(self, ctx: AgentContext) -> AgentResult:
        await ctx.tool("write_file", path=FIX_FILE, content="class Fixed { /* primary */ }\n")
        if self.write_extra:
            await ctx.tool(
                "write_file", repo="inventory", path=EXTRA_FILE, content="class InventoryFix { /* extra */ }\n"
            )
        return AgentResult(
            "patch_ready",
            PatchResult(
                diff=f"--- a/{FIX_FILE}\n+++ b/{FIX_FILE}\n@@\n-// old\n+// primary fix\n",
                files_changed=[FIX_FILE],
                repos={
                    "inventory": RepoPatch(
                        diff=f"--- a/{EXTRA_FILE}\n+++ b/{EXTRA_FILE}\n@@\n+class InventoryFix {{}}\n",
                        files_changed=[EXTRA_FILE],
                    )
                },
                explanation="跨仓库修复：主仓库调用方 + 附加仓库实现",
            ),
        )


async def _setup(settings: Any, tmp_path: Any, *, write_extra: bool = True) -> tuple[Harness, LocalGitCodeHost, Any]:
    from twinfix.adapters.sandbox import LocalSandbox

    local = settings.model_copy(update={"codehost": "local", "git_root": tmp_path / "git"})
    adapters = build_adapters(local)
    assert isinstance(adapters.sandbox, LocalSandbox)
    adapters.sandbox.allow_exec = True
    host = LocalGitCodeHost(tmp_path / "git")
    src = tmp_path / "seed"
    src.mkdir()
    (src / "README.md").write_bytes(b"primary\n")
    await host.create_from_dir("acme/demo-shop", src)
    extra_src = tmp_path / "inventory-seed"
    (extra_src / "src" / "main" / "java" / "acme").mkdir(parents=True)
    (extra_src / "src" / "main" / "java" / "acme" / "Inventory.java").write_bytes(b"class Inventory {}\n")
    await host.create_from_dir("acme/inventory", extra_src)

    dispatcher = InlineDispatcher()
    rt = await build_runtime(local, publisher=MemoryPublisher(), control=MemoryControlPlane(), adapters=adapters)
    rt.steps.agents[AgentName.FIXER] = _MultiRepoFixer(write_extra=write_extra)  # type: ignore[assignment]
    dispatcher.bind(rt.engine)
    return Harness(rt, IncidentService(rt.store, dispatcher), dispatcher, MemoryPublisher()), host, rt


async def _files_at(host: LocalGitCodeHost, repo: str, sha: str) -> list[str]:
    out = await git("show", "--name-only", "--format=", sha, cwd=host.repo_path(repo))
    return [line for line in out.splitlines() if line]


async def test_multi_repo_fix_commits_and_merges_both_repos(settings: Any, tmp_path: Any) -> None:
    h, host, rt = await _setup(settings, tmp_path)
    try:
        iid = await open_incident(
            h,
            config={
                "repos": [{"name": "inventory", "repo": "acme/inventory", "fixable": True}],
                "service_repos": {"order": ["order", "inventory"]},
            },
        )
        assert await state_of(h, iid) is S.AWAITING_REVIEW

        async with rt.store.read() as s:
            inc = await s.get(Incident, iid)
            attempt = await s.scalar(select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq.desc()))
            assert attempt is not None
            patch = await s.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
        assert inc is not None and inc.fix_branch
        assert patch is not None and "inventory" in patch.repos
        assert patch.repos["inventory"]["files_changed"] == [EXTRA_FILE]
        assert inc.meta["repo_prs"]["inventory"]["pr_number"] > 0

        primary_sha = await host.branch_head("acme/demo-shop", inc.fix_branch)
        assert FIX_FILE in await _files_at(host, "acme/demo-shop", primary_sha)
        extra_sha = await host.branch_head("acme/inventory", inc.fix_branch)
        assert EXTRA_FILE in await _files_at(host, "acme/inventory", extra_sha)

        approval = await pending_merge(h, iid)
        await h.service.decide(approval.id, None, UserRole.MAINTAINER, approve=True, comment=None)

        assert await state_of(h, iid) in {S.MERGED, S.OBSERVING, S.CLOSED}
        for repo, path in (("acme/demo-shop", FIX_FILE), ("acme/inventory", EXTRA_FILE)):
            dev_sha = await host.branch_head(repo, "dev")
            assert path in await _files_at(host, repo, dev_sha)
    finally:
        await rt.close()


async def test_patch_for_read_only_repo_is_dropped(settings: Any, tmp_path: Any) -> None:
    """模型声称改了附加仓库，但该仓库未标 fixable：补丁必须丢弃并留痕，不能提交/开 PR。"""
    h, _host, rt = await _setup(settings, tmp_path, write_extra=False)
    try:
        iid = await open_incident(
            h,
            config={
                "repos": [{"name": "inventory", "repo": "acme/inventory"}],  # 没有 fixable
                "service_repos": {"order": "inventory"},
            },
        )
        assert await state_of(h, iid) is S.AWAITING_REVIEW

        async with rt.store.read() as s:
            inc = await s.get(Incident, iid)
            attempt = await s.scalar(select(Attempt).where(Attempt.incident_id == iid).order_by(Attempt.seq.desc()))
            assert attempt is not None
            patch = await s.scalar(select(Patch).where(Patch.attempt_id == attempt.id))
            events = list(
                (
                    await s.scalars(
                        select(IncidentEvent).where(
                            IncidentEvent.incident_id == iid, IncidentEvent.kind == "fix_repos_ignored"
                        )
                    )
                ).all()
            )
        assert inc is not None and inc.state is S.AWAITING_REVIEW
        assert patch is not None and patch.repos == {}
        assert inc.meta.get("repo_prs") in (None, {})
        assert events and events[0].payload["repos"] == ["inventory"]
    finally:
        await rt.close()
