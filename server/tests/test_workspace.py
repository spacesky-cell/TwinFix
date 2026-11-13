"""WorkspaceManager 的 bundle 往返：检出 → 修改/提交 → 推送 → 变基，全链路真实 git。

不依赖 Docker 与数据库：沙箱用 LocalSandbox（文件操作即可，git 是系统操作不受
allow_exec 限制），托管方用 LocalGitCodeHost（本机裸仓库，无需凭据）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twinfix.adapters.codehost import BranchNotAllowedError
from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.adapters.sandbox import BASE_TAG, REPRO_TAG, LocalSandbox
from twinfix.workspace import WorkspaceManager

REPO = "acme/shop"

PATCH_DEV = """\
--- a/lib.py
+++ b/lib.py
@@ -1 +1 @@
-lib v1
+lib v2
"""

PATCH_CONFLICT = """\
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print("v1")
+print("dev")
"""


@pytest.fixture
async def env(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    # write_bytes 保持 LF：Windows 上 write_text 会把 \n 翻成 \r\n，导致 unified diff 打不上
    (source / "app.py").write_bytes(b'print("v1")\n')
    (source / "lib.py").write_bytes(b"lib v1\n")

    host = LocalGitCodeHost(tmp_path / "git")
    sha = await host.create_from_dir(REPO, source)
    sandbox = LocalSandbox(tmp_path / "ws")
    mgr = WorkspaceManager(sandbox, host, tmp_path / "mirrors")
    return host, sandbox, mgr, sha


async def _ws(sandbox: LocalSandbox, mgr: WorkspaceManager, sha: str):
    ws = await sandbox.create_workspace(REPO, sha)
    await mgr.checkout(ws, REPO, "dev")
    return ws


class TestCheckout:
    async def test_recreate_wipes_partially_pushed_repo(self, tmp_path: Path) -> None:
        """中断的评测会留下只推了一半分支的裸仓库；重建必须清掉旧 refs，否则推送被拒。"""
        from twinfix.adapters.gitutil import git

        source = tmp_path / "src"
        source.mkdir()
        (source / "app.py").write_bytes(b'print("v1")\n')
        host = LocalGitCodeHost(tmp_path / "git")
        await host.create_from_dir(REPO, source)

        # 模拟旧仓库里已有 main、但 dev 指向别的历史（正是"push 被 fetch first 拒绝"的现场）
        stale = tmp_path / "stale"
        await git("init", "-q", "-b", "dev", str(stale))
        (stale / "other.txt").write_bytes(b"stale\n")
        await git("add", "-A", cwd=stale)
        await git("commit", "-q", "-m", "stale", cwd=stale)
        await git("push", "-q", "--force", str(host.repo_path(REPO)), "dev:dev", cwd=stale)

        sha = await host.create_from_dir(REPO, source)
        assert await git("show", "dev:app.py", cwd=host.repo_path(REPO)) == 'print("v1")'
        assert await git("rev-parse", "dev", cwd=host.repo_path(REPO)) == sha
        assert (host.repo_path(REPO) / "twinfix.json").exists() is False

    async def test_files_tags_and_head(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        assert await mgr.has_tag(ws, BASE_TAG)
        assert await mgr.head(ws) == sha
        assert await sandbox.read_file(ws, "app.py") == 'print("v1")\n'

    async def test_transfer_bundle_is_excluded(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        # .twinfix/ 在 info/exclude 里，changed_files 不应列出传输文件
        assert await mgr.changed_files(ws) == []

    async def test_python_caches_are_excluded(self, env) -> None:
        """pytest 的 __pycache__/.pytest_cache 落在 tests 下，不能被当成复现测试文件提交。"""
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "orders/tests/__pycache__/test_x.pyc", "x")
        await sandbox.write_file(ws, "orders/tests/.pytest_cache/v/cache", "x")
        await sandbox.write_file(ws, "orders/tests/test_x.py", "def test_x() -> None:\n    assert True\n")
        assert await mgr.changed_files(ws) == ["orders/tests/test_x.py"]

    async def test_unknown_ref_fails(self, env) -> None:
        from twinfix.adapters.gitutil import GitError

        _, sandbox, mgr, sha = env
        ws = await sandbox.create_workspace(REPO, sha)
        with pytest.raises(GitError):
            await mgr.checkout(ws, REPO, "no-such-branch")


class TestCommitAndReset:
    async def test_clean_tree_commits_nothing(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        assert await mgr.commit_all(ws, "noop") is None

    async def test_commit_and_tag(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        new_sha = await mgr.commit_all(ws, "fix", tag=REPRO_TAG)
        assert new_sha is not None and new_sha != sha
        assert await mgr.head(ws) == new_sha
        assert await mgr.has_tag(ws, REPRO_TAG)
        # BASE_TAG 仍指向起点
        assert await mgr.changed_files(ws) == ["app.py"]

    async def test_tag_on_clean_tree_still_applies(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        assert await mgr.commit_all(ws, "noop", tag=REPRO_TAG) is None
        assert await mgr.has_tag(ws, REPRO_TAG)

    async def test_reset_restores_base(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("broken")\n')
        await sandbox.write_file(ws, "junk.txt", "junk\n")
        await mgr.commit_all(ws, "wip")
        await mgr.reset_to(ws, BASE_TAG)
        assert await mgr.head(ws) == sha
        assert await mgr.commit_all(ws, "noop") is None
        assert await sandbox.read_file(ws, "app.py") == 'print("v1")\n'

    async def test_diff_shows_change(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        diff = await mgr.diff(ws)
        assert '-print("v1")' in diff and '+print("v2")' in diff


class TestScopedCommit:
    async def test_only_listed_paths_are_committed(self, env) -> None:
        """Fixer 自验新建的临时文件不进提交，但仍留在工作区。"""
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        await sandbox.write_file(ws, "TempSanityTest.py", "scratch\n")

        new_sha = await mgr.commit_paths(ws, ["app.py"], "fix")

        assert new_sha is not None and new_sha != sha
        res = await sandbox.git(ws, ["show", "--name-only", "--format=", "HEAD"], 30)
        assert res.stdout.split() == ["app.py"]
        # 临时文件既不进提交，也不被删除：工作区原样保留，留给后续轮次或人工处理
        status = await sandbox.git(ws, ["status", "--porcelain"], 30)
        assert status.stdout.split() == ["??", "TempSanityTest.py"]
        assert await sandbox.read_file(ws, "TempSanityTest.py") == "scratch\n"

    async def test_nothing_to_commit_returns_none(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        assert await mgr.commit_paths(ws, [], "fix") is None
        assert await mgr.head(ws) == sha
        # 既有文件的改动也不会被顺手提交：范围之外的一律不动
        assert await mgr.commit_paths(ws, ["lib.py"], "fix") is None

    async def test_tracked_at_separates_new_files_from_edits(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        await sandbox.write_file(ws, "helper.py", "helper\n")
        tracked = await mgr.tracked_at(ws)
        assert tracked == {"app.py", "lib.py"}


class TestPush:
    async def test_rejects_non_fix_branch(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        await mgr.commit_all(ws, "fix")
        with pytest.raises(BranchNotAllowedError):
            await mgr.push(ws, REPO, "main")

    async def test_pushed_branch_matches_workspace_head(self, env) -> None:
        host, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("v2")\n')
        head = await mgr.commit_all(ws, "fix")
        pushed = await mgr.push(ws, REPO, "fix/twinfix-1")
        assert pushed == head
        assert await host.branch_head(REPO, "fix/twinfix-1") == head


class TestRebase:
    async def test_noop_when_branch_unchanged(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        res = await mgr.rebase_onto(ws, REPO, "dev")
        assert res.ok and res.new_base == sha and res.conflicts == []
        assert "unchanged" in res.message

    async def test_clean_rebase_moves_base_tag(self, env) -> None:
        host, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("fix")\n')
        await mgr.commit_all(ws, "fix")
        dev_head = await host.commit_patch(REPO, "dev", PATCH_DEV, "feat: bump lib")

        res = await mgr.rebase_onto(ws, REPO, "dev")
        assert res.ok and res.new_base == dev_head and res.conflicts == []
        # 修复提交仍然在新基座之上，文件同时包含两侧改动
        assert await sandbox.read_file(ws, "app.py") == 'print("fix")\n'
        assert await sandbox.read_file(ws, "lib.py") == "lib v2\n"
        # BASE_TAG 已移动，changed_files 只剩修复那一处
        assert await mgr.changed_files(ws) == ["app.py"]

    async def test_conflict_aborts_and_reports_files(self, env) -> None:
        host, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "app.py", 'print("ws")\n')
        ws_head = await mgr.commit_all(ws, "fix")
        await host.commit_patch(REPO, "dev", PATCH_CONFLICT, "feat: change same line")

        res = await mgr.rebase_onto(ws, REPO, "dev")
        assert not res.ok and res.conflicts == ["app.py"]
        # 冲突后已中止变基，工作区回到修复提交
        assert await mgr.head(ws) == ws_head


class TestStageTarget:
    async def test_stages_target_with_repro_only_and_restores(self, env) -> None:
        host, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        await sandbox.write_file(ws, "test_repro.py", "assert True\n")
        await sandbox.write_file(ws, "app.py", 'print("fix")\n')
        fix_head = await mgr.commit_all(ws, "repro + fix")
        dev_head = await host.commit_patch(REPO, "dev", PATCH_DEV, "teammate hotfix")

        staged = await mgr.stage_target_with(ws, REPO, "dev", ["test_repro.py"])
        assert staged == (dev_head, fix_head)
        # 目标分支内容 + 复现用例，但不含修复
        assert await sandbox.read_file(ws, "lib.py") == "lib v2\n"
        assert await sandbox.read_file(ws, "app.py") == 'print("v1")\n'
        assert await sandbox.read_file(ws, "test_repro.py") == "assert True\n"

        await mgr.restore(ws, fix_head)
        assert await mgr.head(ws) == fix_head
        assert await sandbox.read_file(ws, "app.py") == 'print("fix")\n'

    async def test_noop_when_target_unchanged(self, env) -> None:
        _, sandbox, mgr, sha = env
        ws = await _ws(sandbox, mgr, sha)
        assert await mgr.stage_target_with(ws, REPO, "dev", ["app.py"]) is None


class TestExtraRepo:
    async def test_checkout_into_secondary_repo(self, env, tmp_path: Path) -> None:
        host, sandbox, mgr, _ = env
        source = tmp_path / "inventory-src"
        source.mkdir()
        (source / "stock.py").write_bytes(b"stock v1\n")
        inv_sha = await host.create_from_dir("acme/inventory", source)

        ws = await _ws(sandbox, mgr, _)

        extra_sha = await mgr.checkout_into(ws, "acme/inventory", "dev", "_repos/inventory")

        assert extra_sha == inv_sha
        assert await sandbox.read_file(ws, "_repos/inventory/stock.py") == "stock v1\n"
        # 主仓库工作区不受影响，附加仓库目录也不会出现在主仓库 diff 里
        assert await sandbox.read_file(ws, "app.py") == 'print("v1")\n'
        assert await mgr.changed_files(ws) == []
        res = await sandbox.git(ws, ["rev-parse", "HEAD"], 30, cwd="_repos/inventory")
        assert res.stdout.strip() == inv_sha

    async def test_commit_and_push_secondary_repo(self, env, tmp_path: Path) -> None:
        from twinfix.adapters.gitutil import git

        host, sandbox, mgr, _ = env
        source = tmp_path / "inventory-src"
        source.mkdir()
        (source / "stock.py").write_bytes(b"stock v1\n")
        inv_sha = await host.create_from_dir("acme/inventory", source)
        ws = await _ws(sandbox, mgr, _)
        await mgr.checkout_into(ws, "acme/inventory", "dev", "_repos/inventory")
        await sandbox.write_file(ws, "_repos/inventory/stock.py", "stock v2\n")

        # cwd 只作用于附加仓库：主仓库工作区保持干净，diff 也不串仓库
        assert await mgr.changed_files(ws, cwd="_repos/inventory") == ["stock.py"]
        assert "stock v2" in await mgr.diff(ws, cwd="_repos/inventory")
        assert await mgr.changed_files(ws) == []

        sha = await mgr.commit_paths(ws, ["stock.py"], "fix inventory", cwd="_repos/inventory")
        assert sha is not None and sha != inv_sha
        pushed = await mgr.push(ws, "acme/inventory", "fix/twinfix-multi", cwd="_repos/inventory")
        assert pushed == sha
        assert await host.branch_head("acme/inventory", "fix/twinfix-multi") == sha
        show = await git("show", "--name-only", "--format=", sha, cwd=host.repo_path("acme/inventory"))
        assert "stock.py" in show
