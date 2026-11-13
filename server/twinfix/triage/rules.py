"""分诊规则：指纹、非 bug 过滤、定级。纯函数，规则可由项目配置覆盖。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from twinfix.domain.enums import Env, Severity

DEFAULT_NOT_BUG_PATTERNS = (
    r"\bBizException\b",
    r"\bBusinessException\b",
    r"\bValidationException\b",
    r"\bMethodArgumentNotValidException\b",
)

_EXC_TYPE = re.compile(r"^\s*(?:Caused by:\s*)?([\w$.]+(?:Exception|Error|Throwable))\b", re.M)
_FRAME = re.compile(r"^\s*at\s+([\w.$<>]+)\(", re.M)
# 指纹要对"同一个 bug 的不同次发生"保持稳定：去掉数字、uuid、十六进制等易变部分
_VOLATILE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b0x[0-9a-f]+\b|\d+", re.I)

_SEVERITY_RANK = {Severity.S1: 1, Severity.S2: 2, Severity.S3: 3, Severity.S4: 4}


def exception_type(stacktrace: str | None) -> str | None:
    if not stacktrace:
        return None
    types = _EXC_TYPE.findall(stacktrace)
    # 取最内层 Caused by：那才是根因异常
    return types[-1] if types else None


def fingerprint(
    *,
    service: str | None,
    title: str,
    stacktrace: str | None = None,
    explicit: str | None = None,
    project_packages: tuple[str, ...] = (),
) -> str:
    if explicit:
        return explicit
    parts = [service or "-"]
    exc = exception_type(stacktrace)
    frames = _FRAME.findall(stacktrace or "")
    if project_packages:
        frames = [f for f in frames if f.startswith(project_packages)] or frames
    if exc or frames:
        parts += [exc or "-", *frames[:5]]
    else:
        parts.append(_VOLATILE.sub("#", title.strip().lower()))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class TriageDecision:
    is_bug: bool
    severity: Severity
    reason: str


def classify(
    *,
    title: str,
    env: Env,
    severity: Severity | None,
    signals: list[dict[str, Any]],
    config: dict[str, Any],
) -> TriageDecision:
    patterns = config.get("not_bug_patterns", DEFAULT_NOT_BUG_PATTERNS)
    texts = [title] + [str(s.get("stacktrace") or s.get("message") or "") for s in signals]
    for pattern in patterns:
        if any(re.search(pattern, t) for t in texts):
            return TriageDecision(False, severity or Severity.S4, f"matched not-bug pattern {pattern}")

    sev = severity or Severity.S3
    # 生产环境的问题至少 S2
    if env is Env.PROD and _SEVERITY_RANK[sev] > 2:
        sev = Severity.S2
    return TriageDecision(True, sev, "triaged as bug")
