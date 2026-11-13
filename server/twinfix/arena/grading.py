"""判分：把一次事故的最终详情 + 隐藏测试结果映射成 docs/07 §5 的指标。纯函数，便于单测。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from twinfix.arena.cases import Case

# 期望 "fixed" 时，这些状态都算 Agent 交付了经验证的修复。
# OBSERVING / CLOSED 是合并之后的延续：修复已经进了目标分支，判分不能因为它继续往后走就算错
_FIXED_STATES = {"AWAITING_REVIEW", "MERGING", "MERGED", "OBSERVING", "CLOSED"}


@dataclass(frozen=True)
class HiddenTestResult:
    ran: bool
    passed: bool
    regression_passed: bool = False
    output: str = ""


@dataclass
class CaseScore:
    case_id: str
    category: str
    expected: str
    final_state: str
    outcome_correct: bool
    root_cause_top1: bool = False
    root_cause_top3: bool = False
    root_cause_graded: bool = False  # 用例有标准根因位置才判 Top-k（NOT_BUG 等诊断类可能没有）
    keyword_hits: float = 0.0
    reproduced: bool = False
    verified: bool = False
    hidden_passed: bool | None = None  # None = 没有补丁可判
    false_fix: bool = False
    needs_human: bool = False
    duration_s: float | None = None
    tokens: int = 0
    cost: float = 0.0
    incident_id: str = ""
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def fix_success(self) -> bool:
        return self.hidden_passed is True and self.verified


def outcome_matches(expected: str, state: str) -> bool:
    if expected == "fixed":
        return state in _FIXED_STATES
    return expected == state


def location_matches(expected: str, loc: dict[str, Any]) -> bool:
    """expected 形如 "order/src/.../OrderService.java#confirmPaid"；文件按后缀比较，方法名可选。"""
    path, _, symbol = expected.partition("#")
    file = str(loc.get("file") or "").replace("\\", "/").lstrip("./")
    if not file:
        return False
    path = path.replace("\\", "/")
    if not (path.endswith(file) or file.endswith(path)):
        return False
    if not symbol:
        return True
    return symbol in str(loc.get("symbol") or "")


def root_cause_rank(expected: list[str], locations: list[dict[str, Any]]) -> int | None:
    """第一个命中任一标准位置的可疑位置的名次（1 起）；未命中为 None。"""
    for i, loc in enumerate(locations, start=1):
        if any(location_matches(e, loc) for e in expected):
            return i
    return None


def keyword_ratio(keywords: list[str], text: str) -> float:
    if not keywords:
        return 1.0
    low = text.lower()
    return sum(1 for k in keywords if k.lower() in low) / len(keywords)


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return None


def score(case: Case, detail: dict[str, Any], hidden: HiddenTestResult | None) -> CaseScore:
    state = str(detail.get("state", ""))
    s = CaseScore(
        case_id=case.id,
        category=case.category,
        expected=case.expect.outcome,
        final_state=state,
        outcome_correct=outcome_matches(case.expect.outcome, state),
        needs_human=state == "NEEDS_HUMAN",
        tokens=int(detail.get("cost_tokens") or 0),
        cost=float(detail.get("cost_amount") or 0),
        incident_id=str(detail.get("id", "")),
    )

    diag = detail.get("diagnosis") or {}
    if case.expect.root_cause_locations:
        rank = root_cause_rank(case.expect.root_cause_locations, list(diag.get("suspect_locations") or []))
        s.root_cause_graded = True
        s.root_cause_top1 = rank == 1
        s.root_cause_top3 = rank is not None and rank <= 3
    text = " ".join(str(diag.get(k) or "") for k in ("summary", "root_cause"))
    s.keyword_hits = keyword_ratio(case.expect.root_cause_keywords, text) if diag else 0.0

    runs = list(detail.get("runs") or [])
    s.reproduced = any(r.get("agent") == "reproducer" and r.get("outcome") == "reproduced" for r in runs)
    s.verified = any(v.get("passed") for a in detail.get("attempts") or [] for v in a.get("verifications") or [])

    if hidden is not None and hidden.ran:
        s.hidden_passed = hidden.passed and hidden.regression_passed
        # 误修：自己的验证通过、交付给人评审，但隐藏测试不过——最重要的安全指标
        s.false_fix = s.verified and state in _FIXED_STATES and not s.hidden_passed
        if not hidden.passed:
            s.notes.append("hidden tests failed")
        elif not hidden.regression_passed:
            s.notes.append("module regression failed")

    start, end = _ts(detail.get("created_at")), _ts(detail.get("state_entered_at"))
    if start and end and state in _FIXED_STATES | {"NEEDS_HUMAN", "NOT_BUG", "ALREADY_FIXED", "CANNOT_REPRODUCE"}:
        s.duration_s = max(0.0, (end - start).total_seconds())
    return s


@dataclass(frozen=True)
class Summary:
    total: int
    outcome_accuracy: float
    top1: float
    top3: float
    repro_rate: float
    fix_rate: float
    false_fix_rate: float
    needs_human_rate: float
    avg_duration_s: float | None
    avg_tokens: float
    avg_cost: float


def _ratio(n: int, d: int) -> float:
    return n / d if d else 0.0


def summarize(scores: list[CaseScore]) -> Summary:
    n = len(scores)
    with_loc = [s for s in scores if s.expected == "fixed"]
    fix_cases = [s for s in scores if s.expected == "fixed"]
    delivered = [s for s in scores if s.hidden_passed is not None]
    durations = [s.duration_s for s in scores if s.duration_s is not None]
    return Summary(
        total=n,
        outcome_accuracy=_ratio(sum(s.outcome_correct for s in scores), n),
        top1=_ratio(sum(s.root_cause_top1 for s in with_loc), len(with_loc)),
        top3=_ratio(sum(s.root_cause_top3 for s in with_loc), len(with_loc)),
        repro_rate=_ratio(sum(s.reproduced for s in fix_cases), len(fix_cases)),
        fix_rate=_ratio(sum(s.fix_success for s in fix_cases), len(fix_cases)),
        false_fix_rate=_ratio(sum(s.false_fix for s in delivered), len(delivered)),
        needs_human_rate=_ratio(sum(s.needs_human for s in scores), n),
        avg_duration_s=sum(durations) / len(durations) if durations else None,
        avg_tokens=_ratio(sum(s.tokens for s in scores), n),
        avg_cost=sum(s.cost for s in scores) / n if n else 0.0,
    )


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def render_report(title: str, scores: list[CaseScore], meta: dict[str, str]) -> str:
    sm = summarize(scores)
    lines = [f"# {title}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    lines += [
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 用例数 | {sm.total} |",
        f"| 结论正确率 | {_pct(sm.outcome_accuracy)} |",
        f"| 根因定位 Top-1 / Top-3 | {_pct(sm.top1)} / {_pct(sm.top3)} |",
        f"| 复现成功率 | {_pct(sm.repro_rate)} |",
        f"| 修复成功率（隐藏测试 + 回归） | {_pct(sm.fix_rate)} |",
        f"| 误修率 | {_pct(sm.false_fix_rate)} |",
        f"| 人工介入率 | {_pct(sm.needs_human_rate)} |",
        f"| 平均端到端耗时 | {f'{sm.avg_duration_s:.0f}s' if sm.avg_duration_s is not None else '-'} |",
        f"| 平均 token / 成本 | {sm.avg_tokens:.0f} / {sm.avg_cost:.4f} |",
        "",
        "## 明细",
        "",
        "| 用例 | 类别 | 期望 | 实际 | 结论 | 根因 | 复现 | 隐藏测试 | 误修 | 耗时 | token | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def mark(b: bool | None) -> str:
        return "-" if b is None else ("✅" if b else "❌")

    for s in scores:
        if not s.root_cause_graded:
            rc = "-"
        else:
            rc = "Top-1" if s.root_cause_top1 else ("Top-3" if s.root_cause_top3 else "❌")
        note = "; ".join([*s.notes, *([s.error] if s.error else [])]) or ""
        dur = f"{s.duration_s:.0f}s" if s.duration_s is not None else "-"
        lines.append(
            f"| {s.case_id} | {s.category} | {s.expected} | {s.final_state} | {mark(s.outcome_correct)} | {rc} "
            f"| {mark(s.reproduced)} | {mark(s.hidden_passed)} | {'⚠️' if s.false_fix else ''} | {dur} "
            f"| {s.tokens} | {note.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def report_json(title: str, scores: list[CaseScore], meta: dict[str, str]) -> dict[str, Any]:
    """机器可读的评测结果：控制台评测页与对比实验读取它（与 Markdown 报告同源）。"""
    return {
        "title": title,
        "meta": meta,
        "summary": asdict(summarize(scores)),
        "cases": [{**asdict(s), "fix_success": s.fix_success} for s in scores],
    }


_COMPARE_ROWS: list[tuple[str, str, bool]] = [
    # (summary 字段, 显示名, 是否百分比)
    ("outcome_accuracy", "结论正确率", True),
    ("top1", "根因 Top-1", True),
    ("repro_rate", "复现率", True),
    ("fix_rate", "修复率", True),
    ("false_fix_rate", "误修率", True),
    ("needs_human_rate", "人工介入率", True),
    ("avg_duration_s", "平均耗时 (s)", False),
    ("avg_tokens", "平均 token", False),
]


def render_comparison(reports: list[dict[str, Any]]) -> str:
    """多份评测 JSON 并排对比（模型对比 / 对照实验）。只比较所有报告共有的用例，避免用例集不同导致的偏差。"""
    common = set.intersection(*({c["case_id"] for c in r["cases"]} for r in reports)) if reports else set()
    cols = [r["meta"].get("Agent") or r["meta"].get("运行", "?") for r in reports]
    sums = []
    for r in reports:
        cases = [c for c in r["cases"] if c["case_id"] in common]
        scores = [CaseScore(**{k: v for k, v in c.items() if k in CaseScore.__dataclass_fields__}) for c in cases]
        sums.append(asdict(summarize(scores)))

    def fmt(v: float | None, pct: bool) -> str:
        if v is None:
            return "-"
        return _pct(v) if pct else f"{v:.0f}"

    lines = [f"共同用例 {len(common)} 个：{', '.join(sorted(common))}", "", "| 指标 | " + " | ".join(cols) + " |"]
    lines.append("|---|" + "---|" * len(cols))
    for key, label, pct in _COMPARE_ROWS:
        lines.append(f"| {label} | " + " | ".join(fmt(s[key], pct) for s in sums) + " |")
    lines += ["", "| 用例 | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for cid in sorted(common):
        cells = []
        for r in reports:
            c = next(x for x in r["cases"] if x["case_id"] == cid)
            mark = "✅" if c["outcome_correct"] else "❌"
            cells.append(f"{mark} {c['final_state']}" + (" ⚠️误修" if c.get("false_fix") else ""))
        lines.append(f"| {cid} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
