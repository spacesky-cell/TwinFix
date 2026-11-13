"""Prometheus 指标端点（GET /api/metrics）。

指标从 PostgreSQL 实时聚合，因此 API 与 worker 两个进程暴露的是同一份全局状态，
不需要在每个进程里维护计数器。输出为 Prometheus text exposition format（version 0.0.4）。

鉴权（docs/05-safety.md §3）：
- 配置 ``TWINFIX_METRICS_TOKEN`` 时要求 ``Authorization: Bearer <token>`` 或 ``X-TwinFix-Token``；
- 未配置 token 且开启了账号鉴权时返回 401，避免运行数据暴露给未登录访问者；
- ``TWINFIX_METRICS_ENABLED=false`` 时端点返回 404。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from twinfix.api.webhooks import verify_token
from twinfix.config import Settings
from twinfix.db import Store
from twinfix.domain.enums import TERMINAL_STATES, WAITING_STATES, RunStatus
from twinfix.domain.models import Approval, Incident, Project, Report, Run, Signal, ToolCall, Twin, utcnow

router = APIRouter(prefix="/api")

_STARTED_AT = time.time()
try:  # 与 FastAPI 的版本号保持一致；源码直接运行时回退到 pyproject 的版本
    _VERSION = version("twinfix")
except PackageNotFoundError:  # pragma: no cover - 仅未安装时触发
    _VERSION = "0.1.0"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format(value: float | int) -> str:
    return str(int(value)) if isinstance(value, int) else f"{value:.6g}"


def sample(name: str, value: float | int, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {_format(value)}"
    rendered = ",".join(f'{key}="{_escape(str(labels[key]))}"' for key in sorted(labels))
    return f"{name}{{{rendered}}} {_format(value)}"


def _metric(
    lines: list[str],
    name: str,
    help_text: str,
    samples: list[tuple[dict[str, str] | None, float | int]],
    kind: str = "gauge",
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        lines.append(sample(name, value, labels))


def _age_seconds(moment: datetime | None, now: datetime) -> float:
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment).total_seconds())


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


async def collect_metrics(store: Store, settings: Settings) -> str:
    """从数据库聚合指标；查询失败由调用方转为 503，避免 Prometheus 把旧值当新值。"""
    now = utcnow()
    stale_before = now - timedelta(seconds=settings.state_timeout_s)
    async with store.read() as s:
        incident_rows = (await s.execute(select(Incident.state, func.count()).group_by(Incident.state))).all()
        oldest_rows = (
            await s.execute(
                select(Incident.state, func.min(Incident.state_entered_at))
                .where(Incident.state.notin_(TERMINAL_STATES))
                .group_by(Incident.state)
            )
        ).all()
        stuck = await s.scalar(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.state.notin_(TERMINAL_STATES),
                Incident.state.notin_(WAITING_STATES),
                Incident.state_entered_at < stale_before,
            )
        )
        twin_rows = (await s.execute(select(Twin.status, func.count()).group_by(Twin.status))).all()
        run_rows = (await s.execute(select(Run.status, func.count()).group_by(Run.status))).all()
        active_runs = await s.scalar(select(func.count()).select_from(Run).where(Run.status == RunStatus.RUNNING))
        token_rows = (
            await s.execute(
                select(
                    Run.agent,
                    func.coalesce(func.sum(Run.input_tokens + Run.output_tokens), 0),
                    func.coalesce(func.sum(Run.cost), 0),
                ).group_by(Run.agent)
            )
        ).all()
        tool_rows = (
            await s.execute(
                select(
                    ToolCall.tool,
                    ToolCall.status,
                    func.count(),
                    func.coalesce(func.sum(ToolCall.latency_ms), 0),
                ).group_by(ToolCall.tool, ToolCall.status)
            )
        ).all()
        approval_rows = (
            await s.execute(
                select(Approval.action, Approval.status, func.count()).group_by(Approval.action, Approval.status)
            )
        ).all()
        report_rows = (await s.execute(select(Report.status, func.count()).group_by(Report.status))).all()
        signal_rows = (await s.execute(select(Signal.source, func.count()).group_by(Signal.source))).all()
        projects = await s.scalar(select(func.count()).select_from(Project))

    lines: list[str] = []
    _metric(lines, "twinfix_up", "TwinFix API is up.", [(None, 1)])
    _metric(
        lines,
        "twinfix_info",
        "Build information.",
        [({"version": _VERSION}, 1)],
    )
    _metric(
        lines,
        "twinfix_process_start_time_seconds",
        "Start time of the API process since the Unix epoch.",
        [(None, _STARTED_AT)],
    )
    _metric(lines, "twinfix_projects", "Number of configured projects.", [(None, int(projects or 0))])

    _metric(
        lines,
        "twinfix_incidents",
        "Number of incidents by state.",
        [({"state": _enum_value(state)}, int(count)) for state, count in incident_rows],
    )
    _metric(
        lines,
        "twinfix_incident_oldest_age_seconds",
        "Age in seconds of the oldest non-terminal incident in each state.",
        [({"state": _enum_value(state)}, _age_seconds(oldest, now)) for state, oldest in oldest_rows],
    )
    _metric(
        lines,
        "twinfix_incidents_stuck",
        "Non-terminal incidents without progress longer than the state timeout.",
        [(None, int(stuck or 0))],
    )
    _metric(
        lines,
        "twinfix_twins",
        "Number of twin environments by status.",
        [({"status": _enum_value(state)}, int(count)) for state, count in twin_rows],
    )
    _metric(
        lines,
        "twinfix_runs",
        "Number of agent runs by status.",
        [({"status": _enum_value(state)}, int(count)) for state, count in run_rows],
    )
    _metric(lines, "twinfix_active_runs", "Agent runs currently running.", [(None, int(active_runs or 0))])
    _metric(
        lines,
        "twinfix_llm_tokens_total",
        "Cumulative LLM tokens (input + output) by agent.",
        [({"agent": _enum_value(agent)}, int(tokens or 0)) for agent, tokens, _ in token_rows],
        kind="counter",
    )
    _metric(
        lines,
        "twinfix_llm_cost_total",
        "Cumulative LLM cost by agent (configured price per million tokens).",
        [({"agent": _enum_value(agent)}, float(cost or 0)) for agent, _, cost in token_rows],
        kind="counter",
    )
    _metric(
        lines,
        "twinfix_tool_calls_total",
        "Cumulative tool calls by tool and status.",
        [({"tool": str(tool), "status": _enum_value(state)}, int(count)) for tool, state, count, _ in tool_rows],
        kind="counter",
    )
    # 同一工具在多个状态下各有一行，按工具汇总后再输出，避免重复 label set
    latency_by_tool: dict[str, float] = {}
    for tool, _state, _count, sum_ms in tool_rows:
        latency_by_tool[str(tool)] = latency_by_tool.get(str(tool), 0.0) + sum_ms / 1000.0
    _metric(
        lines,
        "twinfix_tool_latency_seconds_total",
        "Cumulative tool call latency in seconds by tool.",
        [({"tool": tool}, latency) for tool, latency in latency_by_tool.items()],
        kind="counter",
    )
    _metric(
        lines,
        "twinfix_approvals",
        "Number of approvals by action and status.",
        [
            ({"action": _enum_value(action), "status": _enum_value(state)}, int(count))
            for action, state, count in approval_rows
        ],
    )
    _metric(
        lines,
        "twinfix_reports",
        "Number of reports by delivery status.",
        [({"status": _enum_value(state)}, int(count)) for state, count in report_rows],
    )
    _metric(
        lines,
        "twinfix_signals_total",
        "Cumulative ingested signals by source.",
        [({"source": _enum_value(source)}, int(count)) for source, count in signal_rows],
        kind="counter",
    )
    return "\n".join(lines) + "\n"


def _authorized(settings: Settings, authorization: str, token_header: str) -> bool:
    secret = settings.metrics_token.get_secret_value()
    if secret:
        return verify_token(secret, authorization, token_header)
    # 没配 metrics token 时只允许完全关闭鉴权的本地开发实例
    return settings.auth_disabled


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(
    request: Request,
    authorization: str = Header(""),
    x_twinfix_token: str = Header(""),
) -> PlainTextResponse:
    settings: Settings = request.app.state.settings
    if not settings.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "metrics are disabled")
    if not _authorized(settings, authorization, x_twinfix_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "metrics requires TWINFIX_METRICS_TOKEN")
    try:
        text = await collect_metrics(request.app.state.store, settings)
    except Exception as exc:  # 数据库不可用时让抓取失败，而不是返回空/旧指标
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"metrics unavailable: {exc}") from exc
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")
