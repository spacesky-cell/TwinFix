"""Reporter：分发已渲染的报告并回写 Issue / PR。v1 为确定性实现（不调用 LLM）。"""

from __future__ import annotations

from twinfix.agents.base import AgentContext, AgentResult
from twinfix.domain.enums import AgentName
from twinfix.domain.schemas import ReportResult


class Reporter:
    name = AgentName.REPORTER

    async def run(self, ctx: AgentContext) -> AgentResult:
        r = ctx.inputs
        sent = await ctx.tool(
            "send_report",
            report_id=r["report_id"],
            type=r["type"],
            title=r["title"],
            content=r["content"],
            recipients=r.get("recipients", []),
        )
        if not sent.ok:
            raise RuntimeError(f"report delivery failed: {sent.content}")
        summary = f"**TwinFix**：{r['title']}"
        if issue := r.get("github_issue"):
            await ctx.tool("comment", kind="issue", number=issue, body=summary)
        return AgentResult("reported", ReportResult(report_ids=[r["report_id"]]))
