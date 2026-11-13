"""无外部依赖的单元测试：脱敏、路径规则、指纹、分诊、Java 插件、报告渲染、ULID。"""

from __future__ import annotations

import pytest

from twinfix.domain.enums import Env, ReportType, Severity
from twinfix.gateway import paths
from twinfix.gateway.redact import Redactor
from twinfix.ids import new_id, short_id
from twinfix.orchestrator.reporting import render
from twinfix.profiles.base import TestSelector
from twinfix.profiles.java import JavaProfile, parse_surefire
from twinfix.triage.rules import classify, exception_type, fingerprint

STACK = """java.lang.RuntimeException: wrapper
    at com.acme.order.OrderController.create(OrderController.java:30)
    at org.springframework.web.Servlet.service(Servlet.java:100)
Caused by: java.lang.IllegalStateException: stock < 0
    at com.acme.inventory.StockService$Inner.deduct(StockService.java:42)
    at com.acme.order.OrderService.create(OrderService.java:88)
    ... 12 more
"""


class TestRedactor:
    r = Redactor()

    @pytest.mark.parametrize(
        ("raw", "leak"),
        [
            ("call 13812345678 now", "13812345678"),
            ("id 110101199003078888", "110101199003078888"),
            ("card 6222020200112233445", "6222020200112233445"),
            ("mail bob@acme.com", "bob@acme.com"),
            ("password=hunter2;", "hunter2"),
            ("api_key: abcdef123456", "abcdef123456"),
            ("token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ("Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijk", "eyJhbGciOiJIUzI1"),
            ("key sk-abcdefghijklmnopqrstuv", "sk-abcdefghijklmnopqrstuv"),
        ],
    )
    def test_masks(self, raw: str, leak: str) -> None:
        assert leak not in self.r.text(raw)

    def test_keeps_ordinary_numbers(self) -> None:
        assert self.r.text("order 1001 qty 3 at line 42") == "order 1001 qty 3 at line 42"

    def test_secret_keys_in_dicts(self) -> None:
        out = self.r.value({"Authorization": "Basic xyz", "nested": [{"password": "p"}], "path": "a.java"})
        assert out == {"Authorization": "[REDACTED]", "nested": [{"password": "[REDACTED]"}], "path": "a.java"}

    def test_custom_patterns(self) -> None:
        assert Redactor([r"ACME-\d{4}"]).text("ticket ACME-1234") == "ticket [REDACTED]"


class TestPaths:
    @pytest.mark.parametrize(
        ("path", "pattern", "hit"),
        [
            ("order/src/test/java/A.java", "**/src/test/**", True),
            ("src/test/A.java", "**/src/test/**", True),
            ("src/main/A.java", "**/src/test/**", False),
            ("deploy/x/y.yml", "deploy/**", True),
            ("a/deploy/y.yml", "deploy/**", False),
            ("Jenkinsfile", "Jenkinsfile", True),
            ("app/src/main/resources/application-prod.yml", "**/application-prod.yml", True),
            ("./src/test/../main/A.java", "**/src/test/**", False),
            ("src\\test\\A.java", "**/src/test/**", True),
        ],
    )
    def test_matches(self, path: str, pattern: str, hit: bool) -> None:
        assert paths.matches(path, [pattern]) is hit

    @pytest.mark.parametrize(
        ("path", "bad"), [("../x", True), ("/x", True), ("a/../../x", True), ("D:/x", True), ("a/../b", False)]
    )
    def test_escapes(self, path: str, bad: bool) -> None:
        assert paths.escapes(path) is bad


class TestTriage:
    def test_innermost_exception(self) -> None:
        assert exception_type(STACK) == "java.lang.IllegalStateException"

    def test_fingerprint_stable_across_volatile_titles(self) -> None:
        a = fingerprint(service="order", title="order 1001 failed", stacktrace=STACK)
        b = fingerprint(service="order", title="order 2002 failed", stacktrace=STACK)
        assert a == b

    def test_fingerprint_differs_by_service(self) -> None:
        assert fingerprint(service="a", title="x", stacktrace=STACK) != fingerprint(
            service="b", title="x", stacktrace=STACK
        )

    def test_fingerprint_without_stack_normalizes_numbers(self) -> None:
        assert fingerprint(service="s", title="timeout after 3000ms") == fingerprint(
            service="s", title="timeout after 5000ms"
        )

    def test_explicit_fingerprint_wins(self) -> None:
        assert fingerprint(service="s", title="t", explicit="fp-1") == "fp-1"

    def test_business_exception_is_not_bug(self) -> None:
        d = classify(title="BizException: 余额不足", env=Env.TEST, severity=None, signals=[], config={})
        assert not d.is_bug

    def test_prod_is_at_least_s2(self) -> None:
        d = classify(title="NPE", env=Env.PROD, severity=Severity.S4, signals=[], config={})
        assert d.is_bug and d.severity is Severity.S2

    def test_project_patterns_override(self) -> None:
        d = classify(title="BizException", env=Env.TEST, severity=None, signals=[], config={"not_bug_patterns": []})
        assert d.is_bug


class TestJavaProfile:
    p = JavaProfile(project_packages=("com.acme",))

    def test_parse_stacktrace(self) -> None:
        frames = self.p.parse_stacktrace(STACK)
        assert [f.symbol for f in frames] == [
            "com.acme.order.OrderController.create",
            "org.springframework.web.Servlet.service",
            "com.acme.inventory.StockService$Inner.deduct",
            "com.acme.order.OrderService.create",
        ]
        assert [f.is_project for f in frames] == [True, False, True, True]
        assert frames[2].file == "StockService.java" and frames[2].line == 42

    def test_frame_to_location_handles_inner_class(self) -> None:
        frame = self.p.parse_stacktrace(STACK)[2]
        files = ["inventory/src/main/java/com/acme/inventory/StockService.java", "x/Other.java"]
        loc = self.p.frame_to_location(frame, files)
        assert loc is not None and loc.path == files[0] and loc.line == 42

    def test_detect_and_commands(self) -> None:
        assert self.p.detect(["pom.xml", "README.md"])
        assert not self.p.detect(["package.json"])
        assert self.p.build_command(["mvnw", "pom.xml"], "order")[:1] == ["./mvnw"]
        assert "-pl" in self.p.build_command(["pom.xml"], "order")
        cmd = self.p.test_command(["pom.xml"], TestSelector(classes=["A"], methods=["B#m"]))
        assert "-Dtest=A,B#m" in cmd
        gradle = self.p.test_command(["gradlew"], TestSelector(methods=["B#m"]))
        assert gradle == ["./gradlew", "test", "--tests", "B.m"]

    def test_parse_surefire(self) -> None:
        xml = """<testsuite name="T"><testcase classname="a.T" name="ok"/>
        <testcase classname="a.T" name="bad"><failure message="expected 0 but was -1">trace</failure></testcase>
        <testcase classname="a.T" name="boom"><error message="NPE"/></testcase>
        <testcase classname="a.T" name="skip"><skipped/></testcase></testsuite>"""
        cases = parse_surefire(xml)
        assert [(c.name, c.status) for c in cases] == [
            ("a.T#ok", "passed"),
            ("a.T#bad", "failed"),
            ("a.T#boom", "error"),
            ("a.T#skip", "skipped"),
        ]
        assert cases[1].message == "expected 0 but was -1"

    def test_parse_surefire_tolerates_garbage_output(self) -> None:
        # 并发错乱的测试会把控制字符 / 非法字符引用打进 system-out，报告不再是合法 XML（C017）
        xml = (
            '<testsuite name="T"><testcase classname="a.T" name="race">'
            '<failure message="mismatch">trace</failure>'
            "<system-out>SO20261\x00\x1b[31m&#0;&#x1f;</system-out></testcase>"
            '<testcase classname="a.T" name="ok"/></testsuite>'
        )
        cases = parse_surefire(xml)
        assert [(c.name, c.status) for c in cases] == [("a.T#race", "failed"), ("a.T#ok", "passed")]

    def test_parse_surefire_regex_fallback(self) -> None:
        # 清洗后仍不是合法 XML（例如被截断的 system-out 里有未闭合标签）：退回正则逐个取用例
        xml = (
            '<testsuite name="T"><testcase classname="a.T" name="bad">'
            '<failure message="expected 1 but was 2">x</failure><system-out><unclosed & stuff</system-out>'
            '</testcase><testcase classname="a.T" name="ok"/></testsuite>'
        )
        cases = parse_surefire(xml)
        assert [(c.name, c.status) for c in cases] == [("a.T#bad", "failed"), ("a.T#ok", "passed")]
        assert cases[0].message == "expected 1 but was 2"

    async def test_run_tests_reads_reports_under_target(self) -> None:
        # 报告在 target/ 下；sandbox.search 跳过 target，所以必须靠 find 定位
        from twinfix.adapters.types import CommandResult

        xml = '<testsuite name="T"><testcase classname="a.T" name="bad"><failure message="x"/></testcase></testsuite>'

        class Box:
            async def list_dir(self, ws: object, path: str = ".") -> list[str]:
                return ["pom.xml"]

            async def exec(self, ws: object, cmd: list[str], timeout: int) -> CommandResult:
                out = "./order/target/surefire-reports/TEST-a.T.xml\n" if cmd[0] == "find" and "-path" in cmd else ""
                return CommandResult(exit_code=0, stdout=out, stderr="", duration_ms=1)

            async def search(self, *a: object, **k: object) -> list[tuple[str, int, str]]:
                return []

            async def read_file(self, ws: object, path: str) -> str:
                assert path == "order/target/surefire-reports/TEST-a.T.xml"
                return xml

        report = await self.p.run_tests(Box(), None, TestSelector())  # type: ignore[arg-type]
        assert [c.name for c in report.cases] == ["a.T#bad"]

    def test_template_and_probe(self) -> None:
        assert "{class_name}" in self.p.repro_test_template()
        probe = self.p.runtime_probe()
        assert probe is not None
        assert "thread -b" in probe.allowed_commands
        assert self.p.test_paths() == ["**/src/test/**"]


def test_report_render_contains_key_sections() -> None:
    data = {
        "incident": {
            "id": "I1",
            "title": "超卖",
            "severity": "S2",
            "env": "test",
            "service": "order",
            "target_branch": "dev",
            "fix_branch": "fix/twinfix-ab",
            "closed_reason": None,
            "cost_tokens": 1234,
        },
        "diagnosis": {
            "root_cause": "非原子扣减",
            "confidence": 0.85,
            "suspect_locations": [{"file": "A.java", "line": 3}],
        },
        "patch": {"explanation": "改为条件更新", "diff": "+ atomic"},
        "verification": {"checks": [{"name": "repro", "status": "passed", "detail": "2/2"}], "passed": True},
        "pr_url": "https://github.com/a/b/pull/1",
    }
    r = render(ReportType.FIXED, data)
    assert r.title.startswith("[S2] 已修复")
    for needle in ("## 根因", "非原子扣减", "85%", "```diff", "| repro | ✅ |", "pull/1", "1234 tokens"):
        assert needle in r.content


def test_ulid_is_sortable_and_unique() -> None:
    ids = [new_id() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(len(i) == 26 for i in ids)
    assert len(short_id(ids[0])) == 8


class TestRequiredMergeApprovals:
    """项目配置解析：缺省 / 非法值都要退化为 1，且不允许 0 票就合并。"""

    def test_defaults_and_coercion(self) -> None:
        from twinfix.services import required_merge_approvals

        assert required_merge_approvals(None) == 1
        assert required_merge_approvals({}) == 1
        assert required_merge_approvals({"required_merge_approvals": 3}) == 3
        assert required_merge_approvals({"required_merge_approvals": "2"}) == 2
        assert required_merge_approvals({"required_merge_approvals": 0}) == 1
        assert required_merge_approvals({"required_merge_approvals": "abc"}) == 1


class TestTelnetCleaning:
    """Arthas telnet 客户端输出：CRLF、ANSI 转义、启动横幅、命令回显都要剥掉。"""

    RAW = (
        "\x1b[1;31m  ,---.  \x1b[0m\r\n"
        "\r\n"
        "[arthas@1]$ \r\n"  # 启动后先出现一个空提示符
        "\r\n"
        "[arthas@1]$ thread -b | plaintext\r\n"
        "No most blocking thread found!\r\n"
        "\r\n"
        '\x1b[1;32m"main" Id=1 RUNNABLE\x1b[0m\r\n'
        "    at app//demo.MathGame.main(MathGame.java:17)\r\n"
        "[arthas@1]$ \r\n"
    )

    def test_strips_banner_echo_and_prompts(self) -> None:
        from twinfix.adapters.arthas import clean_telnet as _clean_telnet

        assert _clean_telnet(self.RAW) == (
            'No most blocking thread found!\n\n"main" Id=1 RUNNABLE\n    at app//demo.MathGame.main(MathGame.java:17)'
        )

    def test_without_banner_keeps_payload(self) -> None:
        from twinfix.adapters.arthas import clean_telnet as _clean_telnet

        assert _clean_telnet("java.lang.IllegalArgumentException: x: command not found\r\n") == (
            "java.lang.IllegalArgumentException: x: command not found"
        )


async def test_fake_twin_probe_returns_thread_stack() -> None:
    from twinfix.adapters.twin import FakeTwinProvider
    from twinfix.adapters.types import TwinHandle

    twin = FakeTwinProvider()
    out = await twin.probe(TwinHandle("p", "h", {"gateway": 1}, {}, "abc"), "gateway", "thread -b", 5)
    assert out.startswith("[fake arthas@gateway] thread -b") and '"main"' in out
