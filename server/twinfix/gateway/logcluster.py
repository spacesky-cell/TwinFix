"""日志模板聚类：把大量原始日志行压成"模板 × 次数 + 代表行"（docs/02 §4 上下文工程）。

做法是 Drain 的简化版：先把易变片段（时间、数字、id、ip、uuid、引号内值）掩成 <*>，
再按"级别 + 词数 + 首词"分桶，桶内按位置相似度 ≥ 阈值合并，不一致的位置泛化为 <*>。
确定性、O(n·桶内模板数)，对几千行足够；结果按次数降序，给 LLM 的是摘要而不是原文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<ts>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:ms|s|%|kb|mb)?\b", re.I), "<num>"),
    (re.compile(r'"[^"]{0,200}"'), '"<str>"'),
]
_WILDCARD = "<*>"
_SIM_THRESHOLD = 0.5


def mask(message: str) -> str:
    out = message.strip()
    for pattern, repl in _MASKS:
        out = pattern.sub(repl, out)
    return out


@dataclass
class Cluster:
    level: str
    tokens: list[str]
    count: int = 0
    sample: str = ""
    first_ts: str = ""
    last_ts: str = ""
    services: set[str] = field(default_factory=set)

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


def _similar(a: list[str], b: list[str]) -> float:
    same = sum(1 for x, y in zip(a, b, strict=True) if x in (y, _WILDCARD))
    return same / len(a)


def cluster(lines: list[tuple[str, str, str, str | None]]) -> list[Cluster]:
    """lines: (ts, level, message, service)。返回按次数降序的模板簇。"""
    buckets: dict[tuple[str, int, str], list[Cluster]] = {}
    for ts, level, message, service in lines:
        tokens = mask(message).split()
        if not tokens:
            continue
        key = (level.upper(), len(tokens), tokens[0])
        group = buckets.setdefault(key, [])
        best = max(group, key=lambda c: _similar(c.tokens, tokens), default=None)
        if best is not None and _similar(best.tokens, tokens) >= _SIM_THRESHOLD:
            best.tokens = [x if x == y else _WILDCARD for x, y in zip(best.tokens, tokens, strict=True)]
        else:
            best = Cluster(level.upper(), tokens, sample=message, first_ts=ts)
            group.append(best)
        best.count += 1
        best.last_ts = max(best.last_ts, ts)
        best.first_ts = min(best.first_ts, ts) if best.first_ts else ts
        if service:
            best.services.add(service)
    out = [c for group in buckets.values() for c in group]
    # 错误级别优先，其次按次数
    rank = {"ERROR": 0, "FATAL": 0, "WARN": 1, "WARNING": 1}
    return sorted(out, key=lambda c: (rank.get(c.level, 2), -c.count))


def render(clusters: list[Cluster], total: int, max_templates: int = 15) -> str:
    shown = clusters[:max_templates]
    head = f"{total} hits → {len(clusters)} templates"
    rows = [
        f"[{c.count}x] {c.level} {c.template}\n    e.g. {c.sample[:300]}  ({c.first_ts} … {c.last_ts}"
        + (f"; {','.join(sorted(c.services))}" if c.services else "")
        + ")"
        for c in shown
    ]
    if len(clusters) > len(shown):
        rest = sum(c.count for c in clusters[len(shown) :])
        rows.append(f"... {len(clusters) - len(shown)} more templates ({rest} lines)")
    return "\n".join([head, *rows])
