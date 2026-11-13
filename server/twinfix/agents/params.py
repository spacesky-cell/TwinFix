"""Agent 运行参数：全局默认（环境变量 / Settings）+ 项目级覆盖（project.config.agent）+ 按 Agent 覆盖。

project.config 形如：
    {"agent": {
        "max_tokens": 2000000, "max_tool_calls": 120, "max_seconds": 2400,
        "model": "deepseek-v4-1-flash", "reasoning_effort": "low", "max_turns": 60,
        "per_agent": {"reproducer": {"max_tool_calls": 150}}
    }}
未配置的字段沿用全局默认；数值带上下限，防止误配成 0 或天文数字。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field

from twinfix.config import Settings
from twinfix.domain.enums import AgentName

REASONING_EFFORTS = ("", "low", "medium", "high")


class AgentOverrides(BaseModel):
    """可调参数（API 校验用）。None = 不覆盖。"""

    max_tokens: int | None = Field(None, ge=10_000, le=20_000_000)
    max_tool_calls: int | None = Field(None, ge=5, le=1_000)
    max_seconds: int | None = Field(None, ge=60, le=4 * 3600)
    max_turns: int | None = Field(None, ge=5, le=500)
    model: str | None = Field(None, min_length=1, max_length=200)
    reasoning_effort: str | None = Field(None, pattern=r"^(|low|medium|high)$")


class AgentConfig(AgentOverrides):
    per_agent: dict[AgentName, AgentOverrides] = Field(default_factory=dict)


@dataclass(frozen=True)
class AgentParams:
    max_tokens: int
    max_tool_calls: int
    max_seconds: int
    max_turns: int
    model: str
    reasoning_effort: str


def defaults(settings: Settings) -> AgentParams:
    return AgentParams(
        max_tokens=settings.budget_max_tokens,
        max_tool_calls=settings.budget_max_tool_calls,
        max_seconds=settings.budget_max_seconds,
        max_turns=settings.agent_max_turns,
        model=settings.llm_model,
        reasoning_effort=settings.llm_reasoning_effort,
    )


def _apply(base: AgentParams, o: AgentOverrides) -> AgentParams:
    fields = AgentOverrides.model_fields.keys()
    return replace(base, **{k: v for k, v in o.model_dump(include=set(fields)).items() if v is not None})


def resolve(settings: Settings, project_config: dict[str, Any], agent: AgentName) -> AgentParams:
    raw = project_config.get("agent") or {}
    try:
        cfg = AgentConfig.model_validate(raw)
    except ValueError:
        # 库里的配置由 API 校验后写入；历史脏数据不应让事故整体失败，退回默认
        cfg = AgentConfig()
    params = _apply(defaults(settings), cfg)
    if agent in cfg.per_agent:
        params = _apply(params, cfg.per_agent[agent])
    return params
