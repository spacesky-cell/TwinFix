"""定时扫描 Elasticsearch 日志，把新的 ERROR / 异常聚类后作为信号上报（docs/01 §入口）。

worker 巡检循环里按 ``logscan_interval_s`` 调 ``scan_once``；``TWINFIX_LOGSCAN_ENABLED=false``
时完全不启动。每个项目默认都扫，可用 ``project.config["logscan"]`` 覆盖：

    {"enabled": false, "query": "level:ERROR", "services": ["order"],
     "env": "prod", "severity": "S2", "lookback_s": 600, "max_signals": 5, "max_lines": 20}

不保存水位：窗口重叠时同一个异常只会让未关闭事故的 ``signal_count`` 增加（ingest 按指纹归并），
不会制造重复事故。不同项目的日志默认不隔离，项目配置 ``services`` 用于只扫自己的服务。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from twinfix.adapters.types import LogLine, LogPage, LogQuery
from twinfix.config import LOGSCAN_QUERY, Settings
from twinfix.domain.enums import Env, Severity, SignalSource
from twinfix.domain.models import Project
from twinfix.services import ConflictError, IncidentService, NotFoundError, SignalIn
from twinfix.triage.rules import fingerprint as triage_fingerprint

if TYPE_CHECKING:
    from twinfix.adapters.observability import ObservabilityAdapter
    from twinfix.db import Store

log = logging.getLogger("twinfix.logscan")

_ERROR_LEVELS = frozenset({"error", "fatal", "severe", "critical", "panic", "emerg"})
_EXC_LINE = re.compile(r"^\s*(?:Caused by:\s*)?([\w$.]+(?:Exception|Error|Throwable))\b(.*)$")
_FRAME_LINE = re.compile(r"^\s*at\s+[\w.$<>]+\(")
_ERROR_HINT = re.compile(r"\b(ERROR|FATAL|SEVERE|CRITICAL|PANIC)\b|Exception|Error|Caused by:", re.IGNORECASE)


class LogScanConfig(BaseModel):
    """项目级扫描配置；不填的字段沿用全局默认。"""

    enabled: bool = True
    query: str = ""
    services: list[str] = Field(default_factory=list)
    env: Env = Env.TEST
    severity: Severity = Severity.S3
    lookback_s: int | None = Field(None, ge=30, le=24 * 3600)
    max_signals: int | None = Field(None, ge=1, le=50)
    max_lines: int = Field(20, ge=1, le=200)


@dataclass
class _Cluster:
    service: str | None
    title: str
    stacktrace: str | None
    fingerprint: str
    lines: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None
    hits: int = 0  # 命中条数（高频日志会刷屏，signal 里只留去重后的样本）

    @property
    def count(self) -> int:
        return self.hits


class LogScanner:
    def __init__(
        self,
        store: Store,
        observability: ObservabilityAdapter,
        service: IncidentService,
        settings: Settings,
    ) -> None:
        self.store = store
        self.observability = observability
        self.service = service
        self.settings = settings

    def _config(self, project: Project) -> LogScanConfig:
        defaults = {
            "query": self.settings.logscan_query or LOGSCAN_QUERY,
            "lookback_s": self.settings.logscan_lookback_s,
            "max_signals": self.settings.logscan_max_signals,
        }
        raw = project.config.get("logscan") or {}
        try:
            cfg = LogScanConfig.model_validate({**defaults, **raw})
        except (ValidationError, TypeError, ValueError):
            # 配置写错不能让整个扫描停摆：退回默认值并留一条日志
            log.warning("project %s has invalid logscan config; using defaults", project.name, exc_info=True)
            cfg = LogScanConfig.model_validate(defaults)
        return LogScanConfig.model_validate(
            {
                **cfg.model_dump(),
                "lookback_s": cfg.lookback_s or self.settings.logscan_lookback_s,
                "max_signals": cfg.max_signals or self.settings.logscan_max_signals,
            }
        )

    async def scan_once(self, now: datetime | None = None) -> dict[str, int]:
        """扫一轮所有启用的项目；返回新建/归并/忽略的信号数。"""
        async with self.store.read() as s:
            projects = list((await s.scalars(select(Project).order_by(Project.name))).all())
        stats = {"projects": 0, "created": 0, "merged": 0, "ignored": 0, "errors": 0}
        now = now or datetime.now(UTC)
        for project in projects:
            cfg = self._config(project)
            if not cfg.enabled:
                continue
            stats["projects"] += 1
            try:
                got = await self._scan_project(project, cfg, now)
            except Exception:
                # 单个项目（ES 抖动、query 写错）失败不影响其他项目，下一轮再试
                stats["errors"] += 1
                log.exception("log scan failed for project %s", project.name)
                continue
            for key in ("created", "merged", "ignored"):
                stats[key] += got[key]
        if stats["created"] or stats["merged"]:
            log.info(
                "log scan: %s projects, %s new, %s merged, %s ignored",
                stats["projects"],
                stats["created"],
                stats["merged"],
                stats["ignored"],
            )
        return stats

    async def _scan_project(self, project: Project, cfg: LogScanConfig, now: datetime) -> dict[str, int]:
        start = now - timedelta(seconds=cfg.lookback_s or self.settings.logscan_lookback_s)
        pages: list[LogPage] = []
        if cfg.services:
            for name in cfg.services:
                pages.append(
                    await self.observability.search_logs(
                        LogQuery(service=name, query=cfg.query, start=start, end=now, limit=self.settings.logscan_limit)
                    )
                )
        else:
            pages.append(
                await self.observability.search_logs(
                    LogQuery(service=None, query=cfg.query, start=start, end=now, limit=self.settings.logscan_limit)
                )
            )
        lines = [line for page in pages for line in page.lines if _is_error_line(line)]
        stats = {"created": 0, "merged": 0, "ignored": 0}
        packages = tuple(project.config.get("project_packages", ()))
        for sig in cluster_lines(
            lines,
            project=project.name,
            packages=packages,
            env=cfg.env,
            severity=cfg.severity,
            max_signals=cfg.max_signals or self.settings.logscan_max_signals,
            max_lines=cfg.max_lines,
        ):
            try:
                res = await self.service.ingest(sig)
            except (NotFoundError, ConflictError):
                stats["ignored"] += 1
                continue
            stats["created" if res.created else "merged"] += 1
        return stats

    async def scan_forever(self, interval_s: float | None = None) -> None:
        interval = float(interval_s or self.settings.logscan_interval_s)
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("log scan round failed")
            await asyncio.sleep(interval)


def cluster_lines(
    lines: list[LogLine],
    *,
    project: str,
    packages: tuple[str, ...] = (),
    env: Env = Env.TEST,
    severity: Severity = Severity.S3,
    max_signals: int = 10,
    max_lines: int = 20,
) -> list[SignalIn]:
    """按异常指纹聚类，每个簇产出一个 LOG 信号（最多 max_signals 个）。"""
    clusters: dict[str, _Cluster] = {}
    for line in sorted(lines, key=lambda x: x.ts):
        text = (line.message or "").strip()
        if not text:
            continue
        stack = extract_stacktrace(text)
        title = _title(text, stack)
        fp = "logscan:" + triage_fingerprint(
            service=line.service, title=title, stacktrace=stack, project_packages=packages
        )
        cluster = clusters.get(fp)
        if cluster is None:
            cluster = clusters[fp] = _Cluster(
                service=line.service, title=title, stacktrace=stack, fingerprint=fp, occurred_at=line.ts
            )
        cluster.hits += 1
        # 同一行只留一条（高频日志会刷屏，但计数仍保留）
        if len(cluster.lines) < max_lines and text not in cluster.lines:
            cluster.lines.append(text)
        if cluster.stacktrace is None and stack:
            cluster.stacktrace = stack

    out: list[SignalIn] = []
    for cluster in list(clusters.values())[:max_signals]:
        body = "\n".join(cluster.lines)
        if len(body) > 100_000:
            body = body[:100_000]
        out.append(
            SignalIn(
                project=project,
                source=SignalSource.LOG,
                title=cluster.title[:500],
                service=cluster.service,
                env=env,
                severity=severity,
                message=body,
                stacktrace=cluster.stacktrace,
                fingerprint=cluster.fingerprint,
                occurred_at=cluster.occurred_at,
                raw={"count": cluster.count, "scanner": "es-logscan"},
            )
        )
    return out


def _is_error_line(line: LogLine) -> bool:
    level = (line.level or "").strip().lower()
    text = line.message or ""
    if level:
        if level in _ERROR_LEVELS:
            return True
        # 配了宽泛 query（如 *）时本地兜底：level 明确不是错误就别上报
        return bool(_FRAME_LINE.search(text) or _EXC_LINE.search(text))
    return bool(_ERROR_HINT.search(text) or _FRAME_LINE.search(text))


def extract_stacktrace(text: str, keep: int = 80) -> str | None:
    """从一条日志里截出异常头 + 堆栈（到连续帧结束为止）。"""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _EXC_LINE.match(line):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if _FRAME_LINE.match(line):
                start = max(0, i - 1)
                break
    if start is None:
        return None
    end = start
    for i in range(start, len(lines)):
        line = lines[i]
        if i == start or _FRAME_LINE.match(line) or _EXC_LINE.match(line) or line.lstrip().startswith(("...", "\tat ")):
            end = i + 1
        else:
            break
    return "\n".join(lines[start:end][:keep]).strip() or None


def _title(text: str, stack: str | None) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "log error")
    if stack:
        m = _EXC_LINE.match(stack.splitlines()[0])
        if m:
            exc, rest = m.group(1), m.group(2).strip().lstrip(":").strip()
            return f"{exc}{(': ' + rest) if rest else ''}"[:300]
    return first[:300]


__all__ = ["LogScanConfig", "LogScanner", "cluster_lines", "extract_stacktrace"]
