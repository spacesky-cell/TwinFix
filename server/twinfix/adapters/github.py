"""GitHubAppCodeHost：以 GitHub App 身份访问仓库（docs/06-git-ci-flow.md §3）。

- 用 App 私钥签 JWT（RS256，10 分钟）换取安装令牌（1 小时），过期前自动刷新。
- 代码推拉走 git over https，令牌经环境变量注入 http.extraHeader，不进 URL、不进命令行。
- 只允许推 fix/twinfix-*；合并带 sha 参数，头提交变化时 GitHub 返回 409。
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from twinfix.adapters.codehost import assert_fix_branch
from twinfix.adapters.gitutil import config_env, git
from twinfix.adapters.types import CheckResult, IssueRef, MergeResult, PRInfo, PRRef, PRRequest, Workspace


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"github {status}: {message}")
        self.status = status


class GitHubAppCodeHost:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        installation_id: str,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not app_id or not private_key or not installation_id:
            raise ValueError("GitHub App requires app_id, private_key and installation_id")
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.api_url = api_url.rstrip("/")
        self.git_base = (
            "https://github.com" if "api.github.com" in self.api_url else self.api_url.removesuffix("/api/v3")
        )
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            timeout=30,
            transport=transport,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 认证

    def app_jwt(self) -> str:
        now = int(time.time())
        # iat 回拨 60 秒容忍时钟偏差；exp 不超过 10 分钟（GitHub 限制）
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_exp - 300:
                return self._token
            resp = await self._http.post(
                f"/app/installations/{self.installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self.app_jwt()}"},
            )
            data = self._json(resp)
            self._token = str(data["token"])
            # expires_at 形如 2026-01-01T00:00:00Z；保守起见按 55 分钟处理
            self._token_exp = time.time() + 55 * 60
            return self._token

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        token = await self.token()
        resp = await self._http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
        return self._json(resp)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            raise GitHubError(resp.status_code, str(msg)[:500])
        return resp.json() if resp.content else {}

    async def remote(self, repo: str) -> tuple[str, dict[str, str]]:
        """供 WorkspaceManager 使用：仓库地址 + 携带令牌的 git 环境。"""
        token = await self.token()
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        url = f"{self.git_base}/{repo}.git"
        return url, config_env({f"http.{self.git_base}/.extraheader": f"AUTHORIZATION: basic {basic}"})

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
        owner = req.repo.split("/", 1)[0]
        existing = await self._api(
            "GET", f"/repos/{req.repo}/pulls", params={"head": f"{owner}:{req.head}", "state": "open"}
        )
        if existing:
            pr = existing[0]
        else:
            pr = await self._api(
                "POST",
                f"/repos/{req.repo}/pulls",
                json={"title": req.title, "head": req.head, "base": req.base, "body": req.body, "draft": req.draft},
            )
        return PRInfo(number=int(pr["number"]), url=str(pr["html_url"]), head_sha=str(pr["head"]["sha"]))

    async def set_check(self, repo: str, sha: str, check: CheckResult) -> None:
        await self._api(
            "POST",
            f"/repos/{repo}/check-runs",
            json={
                "name": "twinfix/verify" if check.name == "TwinFix" else check.name,
                "head_sha": sha,
                "status": "completed",
                "conclusion": check.conclusion,
                "details_url": check.details_url,
                "output": {"title": f"TwinFix {check.conclusion}", "summary": check.summary[:60000]},
            },
        )

    async def merge_pr(self, repo: str, number: int, method: str, expected_sha: str) -> MergeResult:
        try:
            data = await self._api(
                "PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": method, "sha": expected_sha}
            )
        except GitHubError as exc:
            if exc.status in (405, 409):
                return MergeResult(False, None, str(exc))
            raise
        return MergeResult(bool(data.get("merged")), data.get("sha"), str(data.get("message", "")))

    async def comment(self, target: IssueRef | PRRef, body: str) -> None:
        # GitHub 的 PR 评论与 Issue 评论共用 issues 接口
        await self._api("POST", f"/repos/{target.repo}/issues/{target.number}/comments", json={"body": body})

    async def branch_head(self, repo: str, branch: str) -> str:
        data = await self._api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return str(data["object"]["sha"])

    async def aclose(self) -> None:
        await self._http.aclose()
