"""定时日志扫描：按异常聚类成信号，窗口重叠时归并到已有事故。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.adapters.types import LogLine, LogPage, LogQuery
from twinfix.config import Settings
from twinfix.domain.enums import SignalSource
from twinfix.domain.models import Incident, Project
from twinfix.logscan import LogScanner, cluster_lines, extract_stacktrace

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)

NPE = (
    "java.lang.NullPointerException: coupon is null\n"
    "    at com.acme.order.PriceCalculator.calculate(PriceCalculator.java:21)\n"
    "    at com.acme.order.OrderService.quote(OrderService.java:88)"
)


class TestPure:
    def test_extract_stacktrace_keeps_header_and_frames(self) -> None:
        text = f"2026-10-25 10:00:00 ERROR [order] 下单失败\n{NPE}\n接下来是无关输出"
        stack = extract_stacktrace(text)
        assert stack is not None
        assert "NullPointerException" in stack.splitlines()[0]
        assert "PriceCalculator.java:21" in stack
        assert "无关输出" not in stack

    def test_extract_stacktrace_without_exception(self) -> None:
        assert extract_stacktrace("nothing useful here") is None
        assert extract_stacktrace("    at com.acme.X.run(X.java:1)") is not None

    def test_clusters_by_fingerprint_and_dedupes_lines(self) -> None:
        lines = [
            LogLine(NOW, "ERROR", f"first hit\n{NPE}", "order"),
            LogLine(NOW + timedelta(seconds=1), "ERROR", f"another hit\n{NPE}", "order"),
            LogLine(NOW + timedelta(seconds=2), "ERROR", "ledger write failed for order 1001", "order"),
            LogLine(NOW + timedelta(seconds=3), "ERROR", "ledger write failed for order 1002", "order"),
            LogLine(NOW + timedelta(seconds=4), "ERROR", "ledger write failed for order 1001", "order"),
        ]
        signals = cluster_lines(lines, project="demo", packages=("com.acme",))
        assert len(signals) == 2
        assert all(s.source is SignalSource.LOG for s in signals)

        npe = next(s for s in signals if "NullPointerException" in s.title)
        assert npe.title == "java.lang.NullPointerException: coupon is null"
        assert npe.fingerprint.startswith("logscan:")
        assert npe.stacktrace == NPE
        # 两次发生共用堆栈 → 同一个信号；每条样本整段保留
        assert npe.raw["count"] == 2
        assert "first hit" in (npe.message or "") and "another hit" in (npe.message or "")

        # 没有堆栈时按去掉易变数字的标题聚类 → 1001/1002 归到一起，同一行只留一条
        ledger = next(s for s in signals if "ledger" in s.title)
        assert ledger.raw["count"] == 3
        assert len(ledger.message.splitlines()) == 2
        assert ledger.stacktrace is None

    def test_caps(self) -> None:
        many = [
            LogLine(NOW, "ERROR", f"boom {i}\njava.lang.RuntimeException\n    at com.acme.M{i}.run(M{i}.java:1)", "x")
            for i in range(8)
        ]
        assert len(cluster_lines(many, project="demo", max_signals=3)) == 3

        same = [LogLine(NOW, "ERROR", f"attempt {i}\n{NPE}", "order") for i in range(5)]
        sig = cluster_lines(same, project="demo", max_lines=2)[0]
        # 最多保留 2 条样本，但计数仍是全部 5 次
        assert "attempt 0" in (sig.message or "") and "attempt 1" in (sig.message or "")
        assert "attempt 2" not in (sig.message or "")
        assert sig.raw["count"] == 5


class FakeObservability:
    """只实现扫描用到的 search_logs。"""

    def __init__(self, lines: list[LogLine] | None = None, fail: tuple[str, ...] = ()) -> None:
        self.lines = list(lines or [])
        self.fail = set(fail)
        self.queries: list[LogQuery] = []

    async def search_logs(self, query: LogQuery) -> LogPage:
        self.queries.append(query)
        if query.service in self.fail:
            raise RuntimeError("elasticsearch is down")
        return LogPage(lines=list(self.lines), total=len(self.lines))


async def _disable_others(harness: Harness) -> None:
    """同一会话里其他测试留下的项目也会被扫；全部关掉，保证统计只算本用例。"""
    async with harness.rt.store.tx() as tx:
        for project in (await tx.session.scalars(select(Project))).all():
            project.config = {**project.config, "logscan": {"enabled": False}}


def _scanner(harness: Harness, settings: Settings, fake: FakeObservability) -> LogScanner:
    return LogScanner(harness.rt.store, fake, harness.service, settings)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_scan_creates_then_merges(harness: Harness, settings: Settings) -> None:
    harness.dispatcher.auto = False
    await _disable_others(harness)
    project = await harness.project(logscan={"query": ""})
    fake = FakeObservability(
        [
            LogLine(NOW - timedelta(seconds=30), "ERROR", f"下单失败\n{NPE}", "order"),
            LogLine(NOW - timedelta(seconds=10), "ERROR", "ledger write failed for order 1001", "order"),
            LogLine(NOW - timedelta(seconds=5), "INFO", "health check ok", "order"),
        ]
    )
    scanner = _scanner(harness, settings, fake)

    first = await scanner.scan_once(now=NOW)
    assert first == {"projects": 1, "created": 2, "merged": 0, "ignored": 0, "errors": 0}
    # INFO 且不含异常的行被过滤掉
    assert len(fake.queries) == 1

    async with harness.rt.store.read() as s:
        incidents = (await s.scalars(select(Incident).where(Incident.project_id == project.id))).all()
    assert len(incidents) == 2
    assert {i.service for i in incidents} == {"order"}
    ids = {i.id for i in incidents}

    # 窗口重叠/重复扫描：同指纹归并到未关闭事故，不新建
    second = await scanner.scan_once(now=NOW)
    assert second["created"] == 0 and second["merged"] == 2
    async with harness.rt.store.read() as s:
        again = (await s.scalars(select(Incident).where(Incident.project_id == project.id))).all()
    assert {i.id for i in again} == ids
    assert all(i.signal_count == 2 for i in again)


@pytest.mark.integration
async def test_per_project_service_list_and_failures(harness: Harness, settings: Settings) -> None:
    harness.dispatcher.auto = False
    await _disable_others(harness)
    boom = await harness.project(logscan={"query": "", "services": ["boom"]})
    ok = await harness.project(logscan={"query": "", "services": ["order", "cart"]})
    fake = FakeObservability([LogLine(NOW, "ERROR", "boom happened", "boom")], fail=("boom",))
    scanner = _scanner(harness, settings, fake)

    stats = await scanner.scan_once(now=NOW)
    # 一个项目炸了不影响另一个
    assert stats["projects"] == 2 and stats["errors"] == 1 and stats["created"] == 1
    # services 列表逐个查询
    assert {q.service for q in fake.queries} == {"boom", "order", "cart"}
    async with harness.rt.store.read() as s:
        assert await s.scalar(select(Incident).where(Incident.project_id == boom.id)) is None
        assert await s.scalar(select(Incident).where(Incident.project_id == ok.id)) is not None


@pytest.mark.integration
async def test_disabled_and_invalid_config(harness: Harness, settings: Settings) -> None:
    harness.dispatcher.auto = False
    await _disable_others(harness)
    await harness.project(logscan={"enabled": False})
    assert (await _scanner(harness, settings, FakeObservability()).scan_once(now=NOW))["projects"] == 0

    # 配置写坏（lookback_s 低于下限）退回全局默认，不中断扫描
    broken = await harness.project(logscan={"lookback_s": 5})
    fake = FakeObservability([LogLine(NOW, "ERROR", "still scanned", "order")])
    stats = await _scanner(harness, settings, fake).scan_once(now=NOW)
    assert stats["projects"] == 1 and stats["created"] == 1
    query = fake.queries[0]
    assert (query.end - query.start).total_seconds() == settings.logscan_lookback_s
    async with harness.rt.store.read() as s:
        assert await s.scalar(select(Incident).where(Incident.project_id == broken.id)) is not None
