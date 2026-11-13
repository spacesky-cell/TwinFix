"""报告渲染：一份 Markdown 源，各渠道（控制台/邮件/飞书/GitHub）自行渲染。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from twinfix.domain.enums import IncidentState, ReportType

TERMINAL_REPORT: dict[IncidentState, ReportType] = {
    IncidentState.CLOSED: ReportType.MERGED,
    IncidentState.NOT_BUG: ReportType.NOT_BUG,
    IncidentState.CANNOT_REPRODUCE: ReportType.CANNOT_REPRODUCE,
    IncidentState.CANNOT_FIX: ReportType.CANNOT_FIX,
    IncidentState.ALREADY_FIXED: ReportType.ALREADY_FIXED,
    IncidentState.CANCELLED: ReportType.CANCELLED,
}

_HEADLINE: dict[ReportType, str] = {
    ReportType.FIXED: "已修复，等待复测与合并",
    ReportType.MERGED: "修复已合并",
    ReportType.CANNOT_REPRODUCE: "无法复现",
    ReportType.CANNOT_FIX: "无法自动修复",
    ReportType.NEEDS_HUMAN: "需要人工介入",
    ReportType.NOT_BUG: "判定为非缺陷",
    ReportType.ALREADY_FIXED: "目标分支已修复",
    ReportType.CANCELLED: "已取消",
}


@dataclass(frozen=True)
class RenderedReport:
    title: str
    content: str


def render(report_type: ReportType, data: dict[str, Any]) -> RenderedReport:
    inc = data["incident"]
    title = f"[{inc['severity']}] {_HEADLINE[report_type]}：{inc['title']}"
    lines = [
        f"# {_HEADLINE[report_type]}",
        "",
        f"- 事故：`{inc['id']}`　环境：`{inc['env']}`　服务：`{inc.get('service') or '-'}`",
        f"- 目标分支：`{inc.get('target_branch') or '-'}`　修复分支：`{inc.get('fix_branch') or '-'}`",
    ]
    if inc.get("closed_reason") or data.get("reason"):
        lines.append(f"- 原因：{data.get('reason') or inc['closed_reason']}")
    if url := data.get("console_url"):
        lines.append(f"- 控制台：{url}")

    if diag := data.get("diagnosis"):
        lines += ["", "## 根因", "", diag.get("root_cause") or diag.get("summary", ""), ""]
        lines.append(f"置信度：{diag.get('confidence', 0):.0%}")
        if locs := diag.get("suspect_locations"):
            lines.append("可疑位置：" + "、".join(f"`{loc['file']}:{loc.get('line') or ''}`" for loc in locs))

    if patch := data.get("patch"):
        lines += ["", "## 修复", "", patch.get("explanation", ""), "", "```diff", patch.get("diff", "").strip(), "```"]
        for name, repo_patch in (patch.get("repos") or {}).items():
            if not isinstance(repo_patch, dict):
                continue
            lines += [
                "",
                f"### 附加仓库 `{name}`",
                "",
                "```diff",
                str(repo_patch.get("diff", "")).strip(),
                "```",
            ]

    if ver := data.get("verification"):
        lines += ["", "## 验证", "", "| 检查 | 结果 | 说明 |", "|---|---|---|"]
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭"}
        for c in ver.get("checks", []):
            lines.append(f"| {c['name']} | {icon.get(c['status'], c['status'])} | {c.get('detail', '')} |")

    if twin := data.get("twin"):
        urls = "、".join(f"[{k}]({v})" for k, v in (twin.get("urls") or {}).items())
        lines += ["", f"孪生环境：`{twin['compose_project']}` {urls}"]

    if pr := data.get("pr_url"):
        lines += ["", f"PR：{pr}"]
    for name, info in (data.get("repo_prs") or {}).items():
        if isinstance(info, dict) and info.get("pr_url"):
            lines.append(f"附加仓库 `{name}` PR：{info['pr_url']}")

    lines += ["", f"成本：{inc.get('cost_tokens', 0)} tokens"]
    if report_type is ReportType.MERGED:
        lines += ["", "> 若修复合入的是 main / release，请按团队流程回合并到其余分支。"]
    return RenderedReport(title=title, content="\n".join(lines).strip() + "\n")
