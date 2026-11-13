"""把基准用例注入成一个可用的本地仓库（控制台"注入"按钮与评测运行器共用）。

与 ``ArenaRunner.inject`` 同源：拷贝用例的种子仓库（demo-shop / demo-python ...）→ 统一 LF
→ 建裸仓库 → 在用例分支上应用注入补丁。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from twinfix.adapters.localgit import LocalGitCodeHost
from twinfix.arena.cases import Case
from twinfix.arena.hidden import normalize_line_endings


@dataclass(frozen=True)
class InjectedCase:
    case_id: str
    project: str
    repo: str
    branch: str
    sha: str


async def create_repo_from_case(host: LocalGitCodeHost, case: Case, seed: Path, repo: str) -> str:
    """用种子仓库 + 注入补丁初始化裸仓库，返回用例分支的头提交。"""
    with tempfile.TemporaryDirectory(prefix="twinfix-arena-seed-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp) / "seed"
        shutil.copytree(
            seed,
            work,
            ignore=shutil.ignore_patterns(
                "target",
                ".git",
                ".idea",
                "__pycache__",
                ".pytest_cache",
                ".venv",
                "bin",
                "obj",
                "TestResults",
                ".vs",
            ),
        )
        normalize_line_endings(work)
        await host.create_from_dir(repo, work)
    if case.has_patch:
        return await host.commit_patch(repo, case.inject.branch, case.patch_text(), case.inject.message)
    return await host.branch_head(repo, case.inject.branch)


async def inject_case(
    case: Case,
    seed: Path,
    git_root: Path,
    project: str,
    repo: str,
) -> InjectedCase:
    host = LocalGitCodeHost(git_root)
    sha = await create_repo_from_case(host, case, seed, repo)
    return InjectedCase(case_id=case.id, project=project, repo=repo, branch=case.inject.branch, sha=sha)
