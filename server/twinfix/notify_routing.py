"""报告通知路由：按事故严重级别、渠道白名单与静默时段决定发给谁、是否暂不发送。

项目配置（project.config["notify"]）的形状：

```json
{
  "severity_recipients": {"S1": ["feishu:https://..."], "*": ["email:oncall@example.com"]},
  "channels": ["feishu", "email"],
  "quiet_hours": {"start": "22:00", "end": "08:00", "keep": ["S1"], "timezone": "Asia/Shanghai"}
}
```

- severity_recipients：按级别给收件人，`"*"` 作兜底；都没有时用 config["report_recipients"]。
- channels：只允许这些渠道（收件人按 `channel:address` 的前缀过滤）。
- quiet_hours：静默时段内，不在 keep 里的级别不发送（保留报告，标记为 suppressed），
  S1 默认始终发送——事故越严重越不能因为用户睡觉而失联。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from twinfix.domain.enums import Severity

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_QUIET_KEEP = ("S1",)


@dataclass(frozen=True)
class NotifyPlan:
    recipients: tuple[str, ...] = ()
    # 非空表示本次不发送；内容是留痕/展示用的原因
    suppressed: str | None = None

    @property
    def is_suppressed(self) -> bool:
        return self.suppressed is not None


def route_report(config: dict[str, Any], severity: Severity, now: datetime) -> NotifyPlan:
    notify = config.get("notify") or {}
    recipients = _recipients(config, notify, severity)
    channels = notify.get("channels")
    if channels:
        allowed = {str(c) for c in channels}
        recipients = [r for r in recipients if _channel_of(r) in allowed]
    quiet = notify.get("quiet_hours")
    kept = {str(v) for v in (quiet.get("keep") or DEFAULT_QUIET_KEEP)} if quiet else set()
    if quiet and severity.value not in kept and _in_quiet_hours(now, quiet):
        window = f"{quiet.get('start', '')}-{quiet.get('end', '')}"
        return NotifyPlan(tuple(recipients), suppressed=f"静默时段 {window}，仅 S1 即时发送")
    return NotifyPlan(tuple(recipients))


def _recipients(config: dict[str, Any], notify: dict[str, Any], severity: Severity) -> list[str]:
    per_severity = notify.get("severity_recipients") or {}
    chosen = per_severity.get(severity.value) or per_severity.get("*")
    if chosen is None:
        chosen = config.get("report_recipients") or []
    return [str(r) for r in chosen]


def _channel_of(recipient: str) -> str:
    return recipient.split(":", 1)[0] if ":" in recipient else ""


def _in_quiet_hours(now: datetime, quiet: dict[str, Any]) -> bool:
    start = _parse_hhmm(quiet.get("start"))
    end = _parse_hhmm(quiet.get("end"))
    zone = _zone(quiet.get("timezone"))
    local: time = now.astimezone(zone).time()
    if start == end:
        return False
    if start < end:
        return start <= local < end
    # 跨零点：22:00-08:00
    return local >= start or local < end


def _parse_hhmm(raw: Any) -> time:
    text = str(raw or "")
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError as exc:
        raise ValueError(f"invalid quiet_hours time {text!r}, expected HH:MM") from exc


def _zone(raw: Any) -> ZoneInfo:
    name = str(raw or DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc
