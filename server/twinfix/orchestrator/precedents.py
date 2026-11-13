"""历史判例：检索同项目里相似的、已有结论的过往事故，作为"参考资料"注入诊断（docs/02 §上下文）。

- 只取有可信结论的事故：修复已合并（CLOSED 且有补丁）、或明确判定为 NOT_BUG / ALREADY_FIXED；
  失败 / 取消 / 不能复现的过程性结论不当判例，避免把错误经验传下去。
- 相似度是确定性的词法打分（异常类型、项目栈帧、服务、标题词），可解释、可复现，不依赖向量库；
  评测时也不会因检索随机性影响结果。
- 判例只是线索：提示词明确要求 Agent 用当前代码重新核实，不能直接照抄结论。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from twinfix.db import Store
from twinfix.domain.enums import IncidentState
from twinfix.domain.models import Attempt, Diagnosis, Incident, Patch, Signal
from twinfix.triage.rules import exception_type

_FRAME = re.compile(r"^\s*at\s+([\w.$<>]+)\(", re.M)
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}|[一-鿿]{2,}", re.I)
_STOP = frozenset({"the", "and", "for", "with", "api", "http", "post", "get", "error", "exception", "java"})

# 可当判例的结论
_VERDICTS = (IncidentState.CLOSED, IncidentState.NOT_BUG, IncidentState.ALREADY_FIXED)
_MIN_SCORE = 0.25
_CANDIDATES = 200


@dataclass(frozen=True)
class Features:
    service: str | None
    exception: str | None
    frames: frozenset[str]
    words: frozenset[str]


def features(title: str, service: str | None, texts: list[str]) -> Features:
    blob = "\n".join(t for t in texts if t)
    frames = frozenset(_FRAME.findall(blob)[:20])
    words = frozenset(w.lower() for w in _WORD.findall(f"{title}\n{blob}") if w.lower() not in _STOP)
    return Features(service, exception_type(blob), frames, words)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def similarity(a: Features, b: Features) -> float:
    """0..1。栈帧与异常类型权重最高（同一段代码出同一种错）；标题/正文词兜底无栈的告警。"""
    score = 0.4 * _jaccard(a.frames, b.frames) + 0.35 * _jaccard(a.words, b.words)
    if a.exception and a.exception == b.exception:
        score += 0.15
    if a.service and a.service == b.service:
        score += 0.1
    return round(score, 3)


async def _signal_texts(store: Store, incident_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {i: [] for i in incident_ids}
    if not incident_ids:
        return out
    async with store.read() as s:
        rows = (await s.scalars(select(Signal).where(Signal.incident_id.in_(incident_ids)))).all()
    for r in rows:
        if r.incident_id is not None:
            out[r.incident_id] += [str(r.raw.get(k) or "") for k in ("message", "stacktrace")]
    return out


async def find_precedents(
    store: Store, inc: Incident, signals: list[dict[str, Any]], limit: int = 3
) -> list[dict[str, Any]]:
    async with store.read() as s:
        past = (
            await s.scalars(
                select(Incident)
                .where(
                    Incident.project_id == inc.project_id,
                    Incident.id != inc.id,
                    Incident.state.in_(_VERDICTS),
                )
                .order_by(Incident.created_at.desc())
                .limit(_CANDIDATES)
            )
        ).all()
    if not past:
        return []
    me = features(inc.title, inc.service, [str(sg.get(k) or "") for sg in signals for k in ("message", "stacktrace")])
    texts = await _signal_texts(store, [p.id for p in past])
    scored = sorted(
        ((similarity(me, features(p.title, p.service, texts[p.id])), p) for p in past),
        key=lambda x: x[0],
        reverse=True,
    )
    top = [(sc, p) for sc, p in scored if sc >= _MIN_SCORE][:limit]
    if not top:
        return []

    ids = [p.id for _, p in top]
    async with store.read() as s:
        diags = {d.incident_id: d for d in (await s.scalars(select(Diagnosis).where(Diagnosis.incident_id.in_(ids))))}
        patches = (
            await s.execute(
                select(Attempt.incident_id, Patch.files_changed, Patch.explanation)
                .join(Patch, Patch.attempt_id == Attempt.id)
                .where(Attempt.incident_id.in_(ids))
                .order_by(Attempt.seq.desc())
            )
        ).all()
    fix: dict[str, dict[str, Any]] = {}
    for incident_id, files, explanation in patches:
        fix.setdefault(incident_id, {"files_changed": files, "explanation": (explanation or "")[:500]})

    out = []
    for sc, p in top:
        if p.state is IncidentState.CLOSED and p.id not in fix:
            continue  # CLOSED 但没有补丁（人工直接关）不算修复判例
        d = diags.get(p.id)
        out.append(
            {
                "incident_id": p.id,
                "similarity": sc,
                "title": p.title,
                "verdict": "fixed" if p.state is IncidentState.CLOSED else p.state.value,
                "closed_reason": (p.closed_reason or "")[:300],
                "root_cause": (d.root_cause if d else "")[:600],
                "suspect_locations": (d.suspect_locations if d else [])[:3],
                "fix": fix.get(p.id),
            }
        )
    return out
