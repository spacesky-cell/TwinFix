"""历史判例：相似度打分与检索（只取有可信结论的过往事故）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.conftest import Harness
from twinfix.domain.enums import IncidentState, SignalSource
from twinfix.domain.models import Attempt, Diagnosis, Incident, IncidentEvent, Patch
from twinfix.orchestrator.precedents import features, find_precedents, similarity
from twinfix.services import SignalIn

NPE = """java.lang.NullPointerException: coupon is null
    at com.acme.order.domain.PriceCalculator.calculate(PriceCalculator.java:21)
    at com.acme.order.service.OrderService.create(OrderService.java:57)
"""
OVERSELL = """java.lang.IllegalStateException: stock < 0
    at com.acme.inventory.service.StockService.reserve(StockService.java:30)
"""


class TestSimilarity:
    def test_same_stack_scores_high_unrelated_low(self) -> None:
        a = features("下单 500 优惠券为空", "order", [NPE])
        b = features("不用券下单报错", "order", [NPE.replace("21", "23")])
        c = features("库存变负", "inventory", [OVERSELL])
        assert similarity(a, b) > 0.6
        assert similarity(a, c) < 0.25

    def test_words_carry_stackless_alerts(self) -> None:
        a = features("GET /api/orders?customer= P99 4.8s", "order", ["order_line N+1 slow query"])
        b = features("GET /api/orders?customer= slow", "order", ["order_line slow query"])
        assert similarity(a, b) >= 0.25


async def _closed_incident(h: Harness, project_name: str, state: IncidentState, *, with_patch: bool) -> str:
    h.dispatcher.auto = False
    res = await h.service.ingest(
        SignalIn(
            project=project_name,
            source=SignalSource.MANUAL,
            title="优惠券为空下单 NPE",
            service="order",
            stacktrace=NPE,
        )
    )
    async with h.rt.store.tx() as tx:
        inc = await tx.session.get(Incident, res.incident_id)
        assert inc is not None
        inc.state = state
        inc.closed_reason = "merged"
        tx.session.add(
            Diagnosis(
                incident_id=inc.id,
                summary="NPE",
                root_cause="coupon 为空时未判空",
                confidence=0.9,
                suspect_locations=[{"file": "order/src/main/java/com/acme/order/domain/PriceCalculator.java"}],
            )
        )
        if with_patch:
            att = Attempt(incident_id=inc.id, seq=1)
            tx.session.add(att)
            await tx.session.flush()
            tx.session.add(
                Patch(
                    attempt_id=att.id,
                    diff="d",
                    files_changed=["PriceCalculator.java"],
                    touches_tests=False,
                    explanation="coupon 判空",
                )
            )
    return res.incident_id


@pytest.mark.integration
async def test_finds_fixed_precedent_and_skips_unverified(harness: Harness) -> None:
    project = await harness.project()
    fixed = await _closed_incident(harness, project.name, IncidentState.CLOSED, with_patch=True)
    await _closed_incident(harness, project.name, IncidentState.CANNOT_REPRODUCE, with_patch=False)
    await _closed_incident(harness, project.name, IncidentState.CLOSED, with_patch=False)  # 人工关闭，无补丁

    res = await harness.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title="结算页 500：无券订单",
            service="order",
            stacktrace=NPE.replace("21", "24"),
        )
    )
    async with harness.rt.store.read() as s:
        inc = await s.get(Incident, res.incident_id)
    assert inc is not None
    found = await find_precedents(harness.rt.store, inc, [{"stacktrace": NPE.replace("21", "24")}])
    assert [p["incident_id"] for p in found] == [fixed]
    assert found[0]["verdict"] == "fixed" and found[0]["fix"]["files_changed"] == ["PriceCalculator.java"]
    assert "判空" in found[0]["root_cause"]


@pytest.mark.integration
async def test_diagnosis_step_injects_precedents(harness: Harness) -> None:
    project = await harness.project()
    fixed = await _closed_incident(harness, project.name, IncidentState.CLOSED, with_patch=True)
    harness.dispatcher.auto = True
    res = await harness.service.ingest(
        SignalIn(
            project=project.name,
            source=SignalSource.MANUAL,
            title="无券下单 NPE",
            service="order",
            stacktrace=NPE,
            meta={"scenario": "happy"},
        )
    )
    async with harness.rt.store.read() as s:
        ev = await s.scalar(
            select(IncidentEvent).where(
                IncidentEvent.incident_id == res.incident_id, IncidentEvent.kind == "precedents"
            )
        )
    assert ev is not None and ev.payload["ids"] == [fixed]
