"""GitLab 适配器：GitLabCodeHost（CodeHost 协议）与 GitLabCI（CIProvider 协议）。

认证用 access token（项目 / 组 / 个人 token 均可）：
- REST API 走 ``PRIVATE-TOKEN`` 头；
- git over https 用 ``http.<base>/.extraheader`` 注入 ``Basic base64(oauth2:<token>)``，
  与 GitHub 实现一致，凭据不进 URL、不进命令行。

语义映射（docs/06-git-ci-flow.md §6）：
- PR → Merge Request（``iid`` 即 PR 编号，嵌套 group 的 ``path_with_namespace`` 原样作为 repo）；
- Check → Commit Status（neutral 记为 skipped）；
- 合并带 ``sha`` 参数，头提交变化时 GitLab 返回 409；
- Issue/PR 评论 → Notes API；
- CI → Pipeline（``trigger`` 创建流水线，``status`` 读 pipeline 状态，``logs`` 取作业 trace）。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from twinfix.adapters.ci import assert_allowed_job
from twinfix.adapters.codehost import assert_fix_branch
from twinfix.adapters.gitutil import config_env, git
from twinfix.adapters.types import (
    BuildRef,
    BuildState,
    BuildStatus,
    CheckResult,
    IssueRef,
    MergeResult,
    PRInfo,
    PRRef,
    PRRequest,
    Workspace,
)


class GitLabError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"gitlab {status}: {message[:500]}")
        self.status = status


def _project_id(repo: str) -> str:
    """GitLab API 的项目 ID 用 URL 编码后的完整 path（支持 group/subgroup/project）。"""
    return quote(repo, safe="")


def _error_message(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    return str(data.get("message") or data.get("error") or data)


class GitLabCodeHost:
    def __init__(
        self,
        *,
        base_url: str = "https://gitlab.com",
        token: str,
        api_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not token:
            raise ValueError("GitLab codehost requires base_url and token")
        self.base_url = base_url.rstrip("/")
        self.api_url = (api_url or f"{self.base_url}/api/v4").rstrip("/")
        self.token = token
        # GitLab 允许自建实例挂在子路径（https://host/gitlab），API 默认是 <base>/api/v4
        self.git_base = self.base_url
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=30,
            transport=transport,
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
        )

    # ---------------------------------------------------------------- 内部

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._http.request(method, path, **kw)
        return self._json(resp)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                message = _error_message(resp.json())
            except ValueError:
                message = resp.text
            raise GitLabError(resp.status_code, message)
        return resp.json() if resp.content else {}

    async def remote(self, repo: str) -> tuple[str, dict[str, str]]:
        """供 WorkspaceManager 使用：仓库地址 + 携带令牌的 git 环境。"""
        basic = base64.b64encode(f"oauth2:{self.token}".encode()).decode()
        url = f"{self.git_base}/{repo}.git"
        return url, config_env({f"http.{self.git_base}/.extraheader": f"AUTHORIZATION: basic {basic}"})

    def _mr_url(self, repo: str, iid: int) -> str:
        return f"{self.base_url}/{repo}/-/merge_requests/{iid}"

    # ---------------------------------------------------------------- CodeHost 协议

    async def clone(self, repo: str, ref: str, dest: Path) -> None:
        url, env = await self.remote(repo)
        await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
        await git("init", "-q", cwd=dest)
        await git("fetch", "-q", "--depth", "50", url, ref, cwd=dest, env=env)
        await git("checkout", "-q", "FETCH_HEAD", cwd=dest)

    async def push_branch(self, ws: Workspace, branch: str) -> str:
        # 实际推送由 WorkspaceManager 经 remote() 完成；这里只做分支规则校验并返回远端头
        assert_fix_branch(branch)
        return await self.branch_head(ws.repo, branch)

    async def open_pr(self, req: PRRequest) -> PRInfo:
        project = _project_id(req.repo)
        existing = await self._api(
            "GET",
            f"/projects/{project}/merge_requests",
            params={
                "source_branch": req.head,
                "target_branch": req.base,
                "state": "opened",
                "per_page": 1,
            },
        )
        if existing:
            mr = existing[0]
        else:
            mr = await self._api(
                "POST",
                f"/projects/{project}/merge_requests",
                json={
                    "source_branch": req.head,
                    "target_branch": req.base,
                    "title": req.title,
                    "description": req.body,
                    "remove_source_branch": True,
                },
            )
        iid = int(mr["iid"])
        return PRInfo(
            number=iid,
            url=str(mr.get("web_url") or self._mr_url(req.repo, iid)),
            head_sha=str(mr.get("sha") or (mr.get("diff_refs") or {}).get("head_sha") or ""),
        )

    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None:
        # GitLab commit status 没有 neutral，用 skipped 表达“不阻塞但不通过”
        state = {"success": "success", "failure": "failed", "neutral": "skipped"}[check.conclusion]
        payload: dict[str, Any] = {
            "state": state,
            "name": "twinfix/verify" if check.name == "TwinFix" else check.name,
            "description": check.summary[:255],
        }
        if check.details_url:
            payload["target_url"] = check.details_url
        await self._api("POST", f"/projects/{_project_id(repo)}/statuses/{sha}", json=payload)

    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult:
        payload: dict[str, Any] = {"sha": expected_sha}
        if method in ("squash", "merge"):
            # GitLab 的合并方式主要由项目设置决定；squash 可按 MR 覆盖，merge 显式关闭 squash
            payload["squash"] = method == "squash"
        try:
            data = await self._api(
                "PUT",
                f"/projects/{_project_id(repo)}/merge_requests/{number}/merge",
                json=payload,
            )
        except GitLabError as exc:
            # 405 不可合并 / 406 分支冲突 / 409 头提交变化 / 400/422 参数或状态不满足
            if exc.status in (400, 405, 406, 409, 422):
                return MergeResult(False, None, str(exc))
            raise
        state = str(data.get("state") or "")
        merge_sha = data.get("merge_commit_sha") or data.get("squash_commit_sha")
        return MergeResult(state == "merged" or bool(merge_sha), merge_sha, state)

    async def comment(self, target: IssueRef | PRRef, body: str) -> None:
        project = _project_id(target.repo)
        kind = "issues" if isinstance(target, IssueRef) else "merge_requests"
        await self._api("POST", f"/projects/{project}/{kind}/{target.number}/notes", json={"body": body})

    async def branch_head(self, repo: str, branch: str) -> str:
        data = await self._api(
            "GET",
            f"/projects/{_project_id(repo)}/repository/branches/{quote(branch, safe='')}",
        )
        return str(data["commit"]["id"])

    async def aclose(self) -> None:
        await self._http.aclose()


# GitLab pipeline 状态 → CIProvider 的 BuildState
_PIPELINE_STATE: dict[str, BuildState] = {
    "created": "queued",
    "waiting_for_resource": "queued",
    "preparing": "queued",
    "pending": "queued",
    "scheduled": "queued",
    "running": "running",
    "success": "success",
    "failed": "failure",
    "canceled": "aborted",
    "cancelling": "aborted",
    "skipped": "aborted",
    "manual": "aborted",
}


class GitLabCI:
    """通过 GitLab Pipeline API 触发/轮询 twinfix-twin-* 作业（docs/06 §6）。

    - 只触发 ``twinfix-twin-*`` 作业名；流水线所在项目由 ``project`` 配置（CI 与代码托管可不同实例）。
    - ``trigger`` 创建流水线，把作业名放进 ``TWINFIX_JOB`` 变量，其余参数逐项作为 CI 变量；
    - ``status`` 映射 pipeline 状态，``logs`` 优先取与作业名同名的 job 的 trace。
    """

    def __init__(
        self,
        *,
        base_url: str = "https://gitlab.com",
        project: str,
        token: str,
        api_url: str = "",
        ref: str = "main",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not project or not token:
            raise ValueError("GitLab CI requires base_url, project and token")
        self.base_url = base_url.rstrip("/")
        self.api_url = (api_url or f"{self.base_url}/api/v4").rstrip("/")
        self.project = _project_id(project)
        self.ref = ref
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=30,
            transport=transport,
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
        )

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                message = _error_message(resp.json())
            except ValueError:
                message = resp.text
            raise GitLabError(resp.status_code, message)
        return resp.json() if resp.content else {}

    async def _text(self, path: str) -> str:
        resp = await self._http.get(path)
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text)
        return resp.text

    async def trigger(self, job: str, params: dict[str, Any]) -> BuildRef:
        assert_allowed_job(job)
        values = dict(params)
        ref = str(values.pop("REF", values.pop("ref", self.ref)))
        variables = {"TWINFIX_JOB": job}
        variables.update({k: v if isinstance(v, str) else json.dumps(v) for k, v in values.items()})
        data = await self._api(
            "POST",
            f"/projects/{self.project}/pipeline",
            json={"ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]},
        )
        return BuildRef(job=job, id=str(data["id"]))

    async def status(self, ref: BuildRef) -> BuildStatus:
        data = await self._api("GET", f"/projects/{self.project}/pipelines/{ref.id}")
        state = _PIPELINE_STATE.get(str(data.get("status") or ""), "failure")
        return BuildStatus(ref=ref, state=state, url=data.get("web_url"))

    async def logs(self, ref: BuildRef, tail: int = 500) -> str:
        jobs = await self._api(
            "GET",
            f"/projects/{self.project}/pipelines/{ref.id}/jobs",
            params={"per_page": 100, "include_retried": "true"},
        )
        if not isinstance(jobs, list) or not jobs:
            return f"[gitlab] {ref.job} pipeline {ref.id} has no jobs yet"
        chosen = next((j for j in jobs if str(j.get("name")) == ref.job and j.get("status") != "failed"), None)
        chosen = chosen or next((j for j in jobs if j.get("status") == "failed"), None) or jobs[0]
        trace = await self._text(f"/projects/{self.project}/jobs/{chosen['id']}/trace")
        lines = trace.splitlines()
        return "\n".join(lines[-tail:])

    async def aclose(self) -> None:
        await self._http.aclose()
