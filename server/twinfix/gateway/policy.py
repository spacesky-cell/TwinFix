"""Agent 权限策略：工具白名单 + 最高级别 + 写入范围。这是代码层面的硬约束，与 Prompt 无关。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from twinfix.domain.enums import AgentName, ToolLevel

WriteScope = Literal["none", "tests", "source"]


@dataclass(frozen=True)
class AgentPolicy:
    tools: frozenset[str]
    max_level: ToolLevel
    write_scope: WriteScope = "none"


_READ_CODE = {
    "read_file",
    "list_dir",
    "search_code",
    "search_symbol",
    "find_references",
    "code_outline",
    "git_blame",
    "git_log",
}
_OBSERVE = {"query_metrics", "search_logs", "get_trace", "recent_deploys"}

POLICIES: dict[AgentName, AgentPolicy] = {
    AgentName.DIAGNOSER: AgentPolicy(
        tools=frozenset(_OBSERVE | _READ_CODE | {"parse_stacktrace", "record_hypothesis", "jvm_probe"}),
        max_level=ToolLevel.L1,
    ),
    AgentName.REPRODUCER: AgentPolicy(
        tools=frozenset(_READ_CODE | {"write_file", "build", "run_tests", "twin_request", "twin_logs"}),
        max_level=ToolLevel.L2_SANDBOX,
        write_scope="tests",
    ),
    AgentName.FIXER: AgentPolicy(
        tools=frozenset(_READ_CODE | {"write_file", "build", "run_tests", "workspace_diff"}),
        max_level=ToolLevel.L2_SANDBOX,
        write_scope="source",
    ),
    AgentName.VERIFIER: AgentPolicy(
        tools=frozenset(_READ_CODE | {"build", "run_tests", "workspace_diff", "twin_request", "twin_logs"}),
        max_level=ToolLevel.L2_SANDBOX,
    ),
    AgentName.REPORTER: AgentPolicy(
        tools=frozenset({"send_report", "comment"}),
        max_level=ToolLevel.L2_EXTERNAL,
    ),
    # 编排器：流程中的外部写操作（推分支、开 PR、写 Check、合并）
    AgentName.ORCHESTRATOR: AgentPolicy(
        tools=frozenset({"push_branch", "open_pr", "set_check", "merge_pr", "comment"}),
        max_level=ToolLevel.L3,
    ),
}
