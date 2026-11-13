"""工具注册表。每个工具声明：级别、参数 schema、超时、频率、审批、结果长度、写路径参数。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from twinfix.domain.enums import ApprovalAction, EvidenceKind, ToolLevel

if TYPE_CHECKING:
    from twinfix.container import Adapters
    from twinfix.tracing import RunHandle


@dataclass(frozen=True)
class ToolEnv:
    """工具执行时能拿到的全部依赖。凭据只在适配器内部，不经过这里流向 Agent。"""

    run: RunHandle
    adapters: Adapters
    # 日志多行时是否用模板摘要代替原始行（对照实验：关闭后 search_logs 直接回贴全文）
    log_cluster: bool = True


@dataclass
class ToolOutput:
    content: str
    data: Any = None  # 结构化结果，给调用方代码用；不进入审计与 Prompt
    evidence_kind: EvidenceKind | None = None
    summary: str | None = None
    # 存入证据库的全文（content 是给 LLM 的压缩视图时使用，例如聚类后的日志）；None = 与 content 相同
    evidence_content: str | None = None


Handler = Callable[[ToolEnv, Any], Awaitable[ToolOutput]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    level: ToolLevel
    args_model: type[BaseModel]
    handler: Handler
    timeout_s: float = 60
    max_calls_per_run: int = 50
    max_result_chars: int = 8000
    # 需要事先批准的动作；None 表示自动执行
    approval: ApprovalAction | None = None
    # 参数中代表"写入路径"的字段名，网关据此做工作区与受保护路径检查
    write_path_args: tuple[str, ...] = ()
    read_path_args: tuple[str, ...] = ()

    def json_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self.tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.tools
