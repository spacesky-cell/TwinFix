"""Agent 产物（结构化输出）。Agent 之间只通过这些对象交换信息。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CodeLocation(BaseModel):
    file: str
    line: int | None = None
    symbol: str | None = None


class Hypothesis(BaseModel):
    id: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    status: Literal["open", "confirmed", "rejected"] = "open"
    evidence_ids: list[str] = Field(default_factory=list)
    children: list[Hypothesis] = Field(default_factory=list)


class DiagnosisResult(BaseModel):
    summary: str
    root_cause: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    suspect_locations: list[CodeLocation] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class ReproResult(BaseModel):
    test_file: str
    test_selector: str  # 例如 "OrderOversellReproTest" 或 "OrderOversellReproTest#concurrentOrders"
    command: str
    failure_output: str = ""
    failing_runs: int = 0
    # 复现需要的其他测试文件（辅助类/夹具）；临时探索用例不要放进来
    test_files: list[str] = Field(default_factory=list)


class RepoPatch(BaseModel):
    diff: str
    files_changed: list[str] = Field(default_factory=list)


class PatchResult(BaseModel):
    diff: str = ""
    files_changed: list[str] = Field(default_factory=list)
    # 附加仓库补丁：仓库名 -> 补丁（主仓库仍用顶层 diff/files_changed）
    repos: dict[str, RepoPatch] = Field(default_factory=dict)
    touches_tests: bool = False
    explanation: str


CheckStatus = Literal["passed", "failed", "skipped"]


class Check(BaseModel):
    name: Literal["build", "repro", "regression", "twin_errors", "test_integrity"]
    status: CheckStatus
    detail: str = ""


class VerificationResult(BaseModel):
    checks: list[Check]
    passed: bool
    commit_sha: str | None = None
    base_sha: str | None = None
    feedback: str | None = None  # 失败时交回 Fixer 的说明


class ReportResult(BaseModel):
    report_ids: list[str]


class Verdict(BaseModel):
    """Agent 主动给出的非常规结论（not_bug / cannot_reproduce / cannot_fix / needs_human）。"""

    reason: str
