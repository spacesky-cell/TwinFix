"""通知路由：按级别选收件人、渠道白名单、静默时段（S1 例外）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from twinfix.domain.enums import Severity
from twinfix.notify_routing import route_report

# 2026-10-30 03:00 UTC = 11:00 上海；23:00 上海 = 15:00 UTC
NOON_SH = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)
NIGHT_SH = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


class TestRecipients:
    def test_falls_back_to_report_recipients(self) -> None:
        plan = route_report({"report_recipients": ["email:oncall@x"]}, Severity.S3, NOON_SH)
        assert plan.recipients == ("email:oncall@x",)
        assert not plan.is_suppressed

    def test_severity_specific_overrides_default(self) -> None:
        config = {
            "report_recipients": ["email:oncall@x"],
            "notify": {"severity_recipients": {"S1": ["feishu:https://hook"]}},
        }
        assert route_report(config, Severity.S1, NOON_SH).recipients == ("feishu:https://hook",)
        # 未配置的级别回落到 report_recipients
        assert route_report(config, Severity.S3, NOON_SH).recipients == ("email:oncall@x",)

    def test_wildcard_severity_route(self) -> None:
        config = {
            "report_recipients": ["email:oncall@x"],
            "notify": {"severity_recipients": {"*": ["feishu:https://hook"]}},
        }
        assert route_report(config, Severity.S4, NOON_SH).recipients == ("feishu:https://hook",)

    def test_channel_whitelist_filters_recipients(self) -> None:
        config = {
            "report_recipients": ["email:oncall@x", "feishu:https://hook", "broken"],
            "notify": {"channels": ["feishu"]},
        }
        assert route_report(config, Severity.S2, NOON_SH).recipients == ("feishu:https://hook",)

    def test_no_config_means_no_recipients(self) -> None:
        assert route_report({}, Severity.S2, NOON_SH).recipients == ()


class TestQuietHours:
    QUIET: ClassVar[dict[str, Any]] = {"notify": {"quiet_hours": {"start": "22:00", "end": "08:00"}}}

    def test_silences_low_severity_inside_window(self) -> None:
        plan = route_report({**self.QUIET, "report_recipients": ["email:oncall@x"]}, Severity.S3, NIGHT_SH)
        assert plan.is_suppressed
        assert "静默时段" in (plan.suppressed or "")
        # 收件人仍保留：报告留在控制台，只是不推送
        assert plan.recipients == ("email:oncall@x",)

    def test_s1_always_sends(self) -> None:
        plan = route_report({**self.QUIET, "report_recipients": ["email:oncall@x"]}, Severity.S1, NIGHT_SH)
        assert not plan.is_suppressed

    def test_custom_keep_list(self) -> None:
        config = {
            "report_recipients": ["email:oncall@x"],
            "notify": {"quiet_hours": {"start": "22:00", "end": "08:00", "keep": ["S1", "S2"]}},
        }
        assert not route_report(config, Severity.S2, NIGHT_SH).is_suppressed
        assert route_report(config, Severity.S3, NIGHT_SH).is_suppressed

    def test_outside_window_sends(self) -> None:
        plan = route_report({**self.QUIET, "report_recipients": ["email:oncall@x"]}, Severity.S3, NOON_SH)
        assert not plan.is_suppressed

    def test_window_boundaries(self) -> None:
        # 22:00 上海正好进入静默；08:00 上海正好结束
        assert route_report(self.QUIET, Severity.S3, datetime(2026, 10, 30, 14, 0, tzinfo=UTC)).is_suppressed
        assert not route_report(self.QUIET, Severity.S3, datetime(2026, 10, 30, 0, 0, tzinfo=UTC)).is_suppressed

    def test_non_crossing_window(self) -> None:
        config = {
            "notify": {"quiet_hours": {"start": "12:00", "end": "13:00", "timezone": "UTC"}},
        }
        assert route_report(config, Severity.S3, datetime(2026, 10, 30, 12, 30, tzinfo=UTC)).is_suppressed
        assert not route_report(config, Severity.S3, datetime(2026, 10, 30, 11, 30, tzinfo=UTC)).is_suppressed

    def test_unknown_timezone_is_config_error(self) -> None:
        config = {"notify": {"quiet_hours": {"start": "22:00", "end": "08:00", "timezone": "Mars/Base"}}}
        with pytest.raises(ValueError, match="unknown timezone"):
            route_report(config, Severity.S3, NIGHT_SH)

    def test_bad_time_is_config_error(self) -> None:
        config = {"notify": {"quiet_hours": {"start": "late", "end": "08:00"}}}
        with pytest.raises(ValueError, match="invalid quiet_hours time"):
            route_report(config, Severity.S3, NIGHT_SH)
