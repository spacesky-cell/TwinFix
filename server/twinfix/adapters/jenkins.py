"""JenkinsCI：通过 Jenkins REST API 触发/轮询 twinfix-twin-* 任务（docs/06 §4）。

- 只能触发 ``twinfix-twin-*`` 前缀的任务（``assert_allowed_job``，凭据最小权限）。
- 触发走 ``buildWithParameters``；随后轮询队列条目拿到真实 build number，
  状态映射：queued/None → queued，building → running，SUCCESS → success，
  ABORTED → aborted，其余非空结果 → failure。
- 认证用 user + API token 的 Basic Auth；token 不落日志。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from twinfix.adapters.ci import assert_allowed_job
from twinfix.adapters.types import BuildRef, BuildState, BuildStatus


class JenkinsError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"jenkins {status}: {message[:300]}")
        self.status = status


class JenkinsCI:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        api_token: str,
        crumb_issuer: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not username or not api_token:
            raise ValueError("Jenkins CI requires base_url, username and api_token")
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(username, api_token),
            timeout=30,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._crumb_issuer = crumb_issuer
        self._crumb: dict[str, str] | None = None
        self._queue: dict[str, str] = {}  # ref.id -> 队列条目 URL，等 build number 时重查

    # ---------------------------------------------------------------- 内部

    async def _crumb_headers(self) -> dict[str, str]:
        if not self._crumb_issuer:
            return {}
        if self._crumb is None:
            try:
                resp = await self._http.get("/crumbIssuer/api/json")
                if resp.status_code == 200:
                    data = resp.json()
                    self._crumb = {data["crumbRequestField"]: data["crumb"]}
                else:
                    self._crumb = {}  # 未启用 crumb issuer（如 CSRF 关闭）
            except httpx.HTTPError:
                self._crumb = {}
        return self._crumb

    async def _post(self, path: str, **kw: Any) -> httpx.Response:
        headers = {**await self._crumb_headers(), **kw.pop("headers", {})}
        resp = await self._http.post(path, headers=headers, **kw)
        if resp.status_code == 403 and self._crumb_issuer and self._crumb:
            # crumb 过期（Jenkins 会话轮换），刷新一次重试
            self._crumb = None
            headers = {**await self._crumb_headers(), **kw.pop("headers", {})}
            resp = await self._http.post(path, headers=headers, **kw)
        if resp.status_code >= 400:
            raise JenkinsError(resp.status_code, resp.text)
        return resp

    async def _get_json(self, path: str) -> Any:
        resp = await self._http.get(path)
        if resp.status_code >= 400:
            raise JenkinsError(resp.status_code, resp.text)
        return resp.json()

    @staticmethod
    def _job_path(job: str) -> str:
        # 支持 folder/job 嵌套写法：a/b/c -> /job/a/job/b/job/c
        return "".join(f"/job/{part}" for part in job.split("/") if part)

    async def _resolve(self, ref: BuildRef) -> BuildRef:
        """队列条目 -> 真实 build，只查一次。仍在排队则原样返回 queue ref，
        由 status 报 queued / logs 报 not started —— 不能在轮询接口里阻塞等待。"""
        if not ref.id.startswith("queue/"):
            return ref
        queue_url = ref.id.removeprefix("queue/")
        data = await self._get_json(f"{queue_url}/api/json")
        if data.get("cancelled"):
            return BuildRef(job=ref.job, id="cancelled")
        executable = data.get("executable")
        if executable and executable.get("number") is not None:
            return BuildRef(job=ref.job, id=str(executable["number"]))
        return ref

    # ---------------------------------------------------------------- CIProvider 协议

    async def trigger(self, job: str, params: dict[str, Any]) -> BuildRef:
        assert_allowed_job(job)
        resp = await self._post(f"{self._job_path(job)}/buildWithParameters", data=_jsonable(params) or None)
        queue_url = resp.headers.get("Location", "")
        if not queue_url:
            raise JenkinsError(resp.status_code, "buildWithParameters did not return a queue Location")
        # 存相对路径，_resolve 拼 api/json
        rel = queue_url.replace(self.base_url, "").rstrip("/")
        ref = BuildRef(job=job, id=f"queue/{rel}")
        self._queue[job] = ref.id
        return ref

    async def status(self, ref: BuildRef) -> BuildStatus:
        resolved = await self._resolve(ref)
        if resolved.id == "cancelled":
            return BuildStatus(ref=resolved, state="aborted")
        if resolved.id.startswith("queue/"):
            return BuildStatus(ref=ref, state="queued")
        data = await self._get_json(f"{self._job_path(resolved.job)}/{resolved.id}/api/json")
        state: BuildState
        result = data.get("result")
        if data.get("building"):
            state = "running"
        elif result == "SUCCESS":
            state = "success"
        elif result == "ABORTED":
            state = "aborted"
        elif result is None:
            state = "queued"  # building=False 且无 result：刚启动还没标记 building
        else:
            state = "failure"  # FAILURE/UNSTABLE/NOT_BUILT 都视为失败
        return BuildStatus(ref=resolved, state=state, url=data.get("url"))

    async def logs(self, ref: BuildRef, tail: int = 500) -> str:
        resolved = await self._resolve(ref)
        if resolved.id.startswith("queue/") or resolved.id == "cancelled":
            return f"[jenkins] {resolved.job} build not started ({resolved.id})"
        # progressiveText 一次拿全量再截尾部；量大时再换 start= 分页
        resp = await self._http.post(
            f"{self._job_path(resolved.job)}/{resolved.id}/logText/progressiveText",
            params={"start": "0"},
            headers=await self._crumb_headers(),
        )
        if resp.status_code >= 400:
            raise JenkinsError(resp.status_code, resp.text)
        lines = resp.text.splitlines()
        return "\n".join(lines[-tail:])

    async def queue_info(self, job: str) -> Any:
        """调试辅助：最近一次触发任务的队列条目 JSON。"""
        ref_id = self._queue.get(job)
        if ref_id is None or not ref_id.startswith("queue/"):
            return None
        return await self._get_json(f"{ref_id.removeprefix('queue/')}/api/json")

    async def aclose(self) -> None:
        await self._http.aclose()


def _jsonable(params: dict[str, Any]) -> dict[str, str]:
    """Jenkins 参数只收字符串；复杂值序列化为 JSON 文本。"""
    return {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in params.items()}
