"""GitLab 适配器：MR / Commit Status / Notes / 分支与 Pipeline 的行为映射。

不依赖真实 GitLab：httpx.MockTransport 模拟 API，断言 URL 编码（嵌套 group 与
带斜杠的分支名）和请求体现在 GitLab 语义上是否正确。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from twinfix.adapters.ci import JobNotAllowedError
from twinfix.adapters.gitlab import GitLabCI, GitLabCodeHost, GitLabError
from twinfix.adapters.types import BuildRef, CheckResult, IssueRef, PRRef, PRRequest, Workspace

BASE = "https://gitlab.test"
REPO = "group/sub/app"
ENCODED_REPO = "group%2Fsub%2Fapp"
WS = Workspace(id="w1", repo=REPO, sha="a" * 40, root=Path("w1"))


def _host(handler, **kw) -> GitLabCodeHost:
    return GitLabCodeHost(
        base_url=BASE,
        token="tok",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def _raw_path(request: httpx.Request) -> str:
    # raw_path 含查询串；路径断言只看 path 部分
    return request.url.raw_path.decode().split("?", 1)[0]


def _json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


class TestAuthAndRemote:
    def test_requires_token(self) -> None:
        with pytest.raises(ValueError, match="requires base_url and token"):
            GitLabCodeHost(base_url=BASE, token="")

    async def test_remote_uses_oauth2_extraheader(self) -> None:
        host = _host(lambda r: httpx.Response(500))
        url, env = await host.remote(REPO)
        assert url == f"{BASE}/{REPO}.git"
        basic = base64.b64encode(b"oauth2:tok").decode()
        assert env["GIT_CONFIG_KEY_0"] == f"http.{BASE}/.extraheader"
        assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
        # token 不出现在 URL 里
        assert "tok" not in url
        await host.aclose()

    async def test_custom_api_url(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"commit": {"id": "abc"}})

        host = _host(handler, api_url=f"{BASE}/custom/api")
        assert await host.branch_head(REPO, "main") == "abc"
        assert seen[0] == f"{BASE}/custom/api/projects/{ENCODED_REPO}/repository/branches/main"
        await host.aclose()


class TestMergeRequests:
    async def test_open_pr_creates_mr_with_encoded_project(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = _raw_path(request)
            if request.method == "GET" and path.endswith("/merge_requests"):
                return httpx.Response(200, json=[])
            if request.method == "POST" and path.endswith("/merge_requests"):
                return httpx.Response(
                    201,
                    json={
                        "iid": 5,
                        "web_url": f"{BASE}/{REPO}/-/merge_requests/5",
                        "sha": "deadbeef",
                    },
                )
            return httpx.Response(404, text=path)

        host = _host(handler)
        info = await host.open_pr(
            PRRequest(repo=REPO, head="fix/twinfix-abc", base="dev", title="fix: 空券", body="body")
        )
        assert (info.number, info.url, info.head_sha) == (5, f"{BASE}/{REPO}/-/merge_requests/5", "deadbeef")
        assert _raw_path(calls[0]) == f"/api/v4/projects/{ENCODED_REPO}/merge_requests"
        params = calls[0].url.params
        assert params["source_branch"] == "fix/twinfix-abc" and params["state"] == "opened"
        body = _json(calls[1])
        assert body["source_branch"] == "fix/twinfix-abc" and body["target_branch"] == "dev"
        assert body["title"] == "fix: 空券" and body["remove_source_branch"] is True
        await host.aclose()

    async def test_open_pr_reuses_existing_open_mr(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json=[{"iid": 9, "web_url": "u", "sha": "s"}])

        host = _host(handler)
        info = await host.open_pr(PRRequest(repo=REPO, head="fix/twinfix-abc", base="dev", title="t", body=""))
        assert info.number == 9 and methods == ["GET"]
        await host.aclose()

    async def test_merge_pr_sends_expected_sha_and_squash(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(_json(request))
            return httpx.Response(200, json={"state": "merged", "merge_commit_sha": "m1"})

        host = _host(handler)
        res = await host.merge_pr(REPO, 5, "squash", "abc")
        assert res.merged and res.sha == "m1"
        assert bodies[0] == {"sha": "abc", "squash": True}
        await host.merge_pr(REPO, 5, "merge", "abc")
        assert bodies[1] == {"sha": "abc", "squash": False}
        await host.aclose()

    @pytest.mark.parametrize("status", [400, 405, 406, 409, 422])
    async def test_merge_conflict_returns_not_merged(self, status: int) -> None:
        host = _host(lambda r: httpx.Response(status, json={"message": "not mergeable"}))
        res = await host.merge_pr(REPO, 5, "squash", "abc")
        assert not res.merged and "not mergeable" in res.message
        await host.aclose()

    async def test_merge_server_error_raises(self) -> None:
        host = _host(lambda r: httpx.Response(500, json={"message": "boom"}))
        with pytest.raises(GitLabError) as exc:
            await host.merge_pr(REPO, 5, "squash", "abc")
        assert exc.value.status == 500
        await host.aclose()


class TestChecksAndComments:
    @pytest.mark.parametrize(
        ("conclusion", "state"),
        [("success", "success"), ("failure", "failed"), ("neutral", "skipped")],
    )
    async def test_set_check_maps_conclusion(self, conclusion: str, state: str) -> None:
        bodies: list[dict] = []
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(_raw_path(request))
            bodies.append(_json(request))
            return httpx.Response(201, json={})

        host = _host(handler)
        await host.set_check(
            REPO,
            "abc123",
            CheckResult("TwinFix", conclusion, "x" * 300, details_url="http://console/1"),  # type: ignore[arg-type]
        )
        assert paths[0] == f"/api/v4/projects/{ENCODED_REPO}/statuses/abc123"
        assert bodies[0]["state"] == state and bodies[0]["name"] == "twinfix/verify"
        assert bodies[0]["target_url"] == "http://console/1"
        assert len(bodies[0]["description"]) == 255
        await host.aclose()

    async def test_comment_issue_and_mr(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(_raw_path(request))
            return httpx.Response(201, json={})

        host = _host(handler)
        await host.comment(IssueRef(REPO, 7), "issue note")
        await host.comment(PRRef(REPO, 5), "mr note")
        assert paths[0] == f"/api/v4/projects/{ENCODED_REPO}/issues/7/notes"
        assert paths[1] == f"/api/v4/projects/{ENCODED_REPO}/merge_requests/5/notes"
        await host.aclose()

    async def test_branch_head_encodes_branch(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(_raw_path(request))
            return httpx.Response(200, json={"commit": {"id": "abc"}})

        host = _host(handler)
        assert await host.branch_head(REPO, "fix/twinfix-abc") == "abc"
        assert paths[0] == (f"/api/v4/projects/{ENCODED_REPO}/repository/branches/fix%2Ftwinfix-abc")
        await host.aclose()


class TestGitLabCI:
    CI_PROJECT = "ci/twin-deploy"
    ENCODED_CI = "ci%2Ftwin-deploy"

    def _ci(self, handler, **kw) -> GitLabCI:
        return GitLabCI(
            base_url=BASE,
            project=self.CI_PROJECT,
            token="tok",
            transport=httpx.MockTransport(handler),
            **kw,
        )

    def test_requires_config(self) -> None:
        with pytest.raises(ValueError, match="requires base_url, project and token"):
            GitLabCI(base_url=BASE, project="", token="tok")

    async def test_rejects_non_twinfix_job(self) -> None:
        ci = self._ci(lambda r: httpx.Response(500))
        with pytest.raises(JobNotAllowedError):
            await ci.trigger("prod-deploy", {})
        await ci.aclose()

    async def test_trigger_posts_pipeline_with_variables(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(201, json={"id": 17, "web_url": "http://ci/17"})

        ci = self._ci(handler)
        ref = await ci.trigger(
            "twinfix-twin-deploy",
            {"REPO": REPO, "SHA": "abc", "PORTS": [31000, 31001], "REF": "release"},
        )
        assert ref.id == "17" and ref.job == "twinfix-twin-deploy"
        path = _raw_path(requests[0])
        assert path == f"/api/v4/projects/{self.ENCODED_CI}/pipeline"
        body = _json(requests[0])
        assert body["ref"] == "release"
        variables = {v["key"]: v["value"] for v in body["variables"]}
        assert variables["TWINFIX_JOB"] == "twinfix-twin-deploy"
        assert variables["REPO"] == REPO and variables["SHA"] == "abc"
        assert json.loads(variables["PORTS"]) == [31000, 31001]
        assert "REF" not in variables
        await ci.aclose()

    @pytest.mark.parametrize(
        ("pipeline", "expected"),
        [
            ("created", "queued"),
            ("pending", "queued"),
            ("running", "running"),
            ("success", "success"),
            ("failed", "failure"),
            ("canceled", "aborted"),
            ("skipped", "aborted"),
            ("something-new", "failure"),
        ],
    )
    async def test_status_mapping(self, pipeline: str, expected: str) -> None:
        ci = self._ci(lambda r: httpx.Response(200, json={"status": pipeline, "web_url": "http://ci/17"}))
        st = await ci.status(BuildRef(job="twinfix-twin-deploy", id="17"))
        assert st.state == expected and st.url == "http://ci/17"
        await ci.aclose()

    async def test_logs_prefers_named_job(self) -> None:
        trace_for: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = _raw_path(request)
            if path.endswith("/jobs"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": 1, "name": "test", "status": "success"},
                        {"id": 2, "name": "twinfix-twin-deploy", "status": "failed"},
                    ],
                )
            if path.endswith("/jobs/2/trace"):
                trace_for.append("2")
                return httpx.Response(200, text="a\nb\nc\nd\n")
            return httpx.Response(404, text=path)

        ci = self._ci(handler)
        text = await ci.logs(BuildRef(job="twinfix-twin-deploy", id="17"), tail=2)
        assert text == "c\nd" and trace_for == ["2"]
        await ci.aclose()

    async def test_logs_without_jobs(self) -> None:
        ci = self._ci(lambda r: httpx.Response(200, json=[]))
        text = await ci.logs(BuildRef(job="twinfix-twin-deploy", id="17"))
        assert "has no jobs yet" in text
        await ci.aclose()
