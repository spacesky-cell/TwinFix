"""内置工具。每个工具只是薄封装：参数模型 + 调用适配器 + 把结果整理成文本。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from twinfix import codeintel
from twinfix.adapters.sandbox import BASE_TAG
from twinfix.adapters.types import (
    CheckResult,
    HttpRequest,
    IssueRef,
    LogQuery,
    MetricQuery,
    OutgoingReport,
    PRRef,
    PRRequest,
    Recipient,
    TwinHandle,
    Workspace,
)
from twinfix.domain.enums import ApprovalAction, EvidenceKind, ToolLevel
from twinfix.gateway import logcluster
from twinfix.gateway.gateway import ToolError
from twinfix.gateway.registry import Tool, ToolEnv, ToolOutput, ToolRegistry
from twinfix.multirepo import REPO_ROOT_PREFIX
from twinfix.profiles.base import LanguageProfile, TestSelector
from twinfix.profiles.java import JavaProfile

L0, L1, L2S, L2E, L3 = (
    ToolLevel.L0,
    ToolLevel.L1,
    ToolLevel.L2_SANDBOX,
    ToolLevel.L2_EXTERNAL,
    ToolLevel.L3,
)


def _ws(env: ToolEnv) -> Workspace:
    if env.run.workspace is None:
        raise ToolError("no workspace attached to this run")
    return env.run.workspace


def _profile(env: ToolEnv) -> LanguageProfile:
    profile = getattr(env.run, "profile", None) or getattr(env.adapters, "profile", None)
    return profile if profile is not None else JavaProfile()


def _twin(env: ToolEnv) -> TwinHandle:
    if env.run.twin is None:
        raise ToolError("no twin environment attached to this run")
    return env.run.twin


def _build_errors(output: str, keep: int = 40) -> str:
    """Maven 输出里的 [ERROR]/[WARNING] 行（编译错误、未匹配测试等）；没有则给尾部。"""
    lines = [ln for ln in output.splitlines() if ln.startswith(("[ERROR]", "[WARNING]")) and "Help 1" not in ln]
    return "\n".join(lines[:keep]) if lines else output[-3000:]


def _repo(env: ToolEnv) -> str:
    repo = getattr(env.run, "repo", None)
    if repo is None:
        raise ToolError("no repository attached to this run")
    return str(repo)


def _repo_entries(env: ToolEnv) -> dict[str, tuple[str, str]]:
    """仓库名 -> (repo, 工作区内相对根目录)；主仓库的根目录为空字符串。"""
    entries = {
        str(name): (str(info.get("repo") or ""), str(info.get("root") or ""))
        for name, info in getattr(env.run, "repo_workspaces", {}).items()
    }
    primary = getattr(env.run, "repo", None)
    if not entries and primary:
        entries[str(primary)] = (str(primary), "")
    return entries


def _repo_root(env: ToolEnv, repo: str | None) -> str:
    entries = _repo_entries(env)
    if not repo:
        for _, root in entries.values():
            if not root:
                return ""
        return ""
    if repo in entries:
        return entries[repo][1]
    for full, root in entries.values():
        if full == repo or full.endswith(f"/{repo}"):
            return root
    available = ", ".join(f"{name} ({full})" for name, (full, _) in entries.items())
    raise ToolError(f"repo not loaded: {repo!r}; available: {available or '(none)'}")


def _in_repo(env: ToolEnv, repo: str | None, path: str) -> str:
    root = _repo_root(env, repo)
    relative = path.replace("\\", "/").lstrip("/")
    return f"{root}/{relative}" if root else relative


def _label_path(env: ToolEnv, path: str) -> str:
    normalized = path.replace("\\", "/").removeprefix("./")
    for name, (_, root) in _repo_entries(env).items():
        if root and (normalized == root or normalized.startswith(f"{root}/")):
            relative = normalized[len(root) + 1 :] if normalized != root else "."
            return f"{name}:{relative}"
    return normalized


def _label_in_repo(env: ToolEnv, repo: str | None, path: str | None = None) -> str:
    if not path:
        return repo or "primary"
    root = _repo_root(env, repo)
    relative = _strip_root(path, root) if root else path
    return _label_path(env, f"{root}/{relative}" if root else relative)


def _repo_info(env: ToolEnv, repo: str) -> dict[str, str]:
    for name, info in getattr(env.run, "repo_workspaces", {}).items():
        full = str(info.get("repo") or "")
        if name == repo or full == repo or full.endswith(f"/{repo}"):
            return {str(k): str(v) for k, v in info.items()}
    return {}


def _repo_fixable(env: ToolEnv, repo: str | None) -> bool:
    if not repo or not _repo_root(env, repo):
        return True  # 主仓库
    return _repo_info(env, repo).get("fixable") == "1"


def _path_in_root(path: str, root: str) -> bool:
    normalized = path.replace("\\", "/")
    if root:
        return normalized == root or normalized.startswith(f"{root}/")
    return not normalized.startswith("_repos/")


def _strip_root(path: str, root: str) -> str:
    normalized = path.replace("\\", "/").removeprefix("./")
    if root and (normalized == root or normalized.startswith(f"{root}/")):
        return normalized[len(root) + 1 :] if normalized != root else "."
    return normalized


# ---------------- L0 观察 ----------------


class MetricsArgs(BaseModel):
    expr: str = Field(description="PromQL expression")
    minutes: int = Field(60, ge=1, le=7 * 24 * 60)


async def query_metrics(env: ToolEnv, a: MetricsArgs) -> ToolOutput:
    end = datetime.now(UTC)
    series = await env.adapters.observability.query_metrics(
        MetricQuery(a.expr, end - timedelta(minutes=a.minutes), end)
    )
    if not series.points:
        return ToolOutput(content=f"no data for {a.expr}")
    values = [v for _, v in series.points]
    text = (
        f"{a.expr}: {len(values)} points, min={min(values):.4g} max={max(values):.4g} "
        f"last={values[-1]:.4g} first_ts={series.points[0][0].isoformat()}"
    )
    return ToolOutput(content=text, evidence_kind=EvidenceKind.METRIC)


class LogsArgs(BaseModel):
    query: str
    service: str | None = None
    trace_id: str | None = None
    minutes: int = Field(60, ge=1, le=7 * 24 * 60)
    limit: int = Field(100, ge=1, le=500)


async def search_logs(env: ToolEnv, a: LogsArgs) -> ToolOutput:
    end = datetime.now(UTC)
    page = await env.adapters.observability.search_logs(
        LogQuery(a.service, a.query, end - timedelta(minutes=a.minutes), end, a.trace_id, a.limit)
    )
    lines = [f"{ln.ts.isoformat()} {ln.level} [{ln.service}] {ln.message}" for ln in page.lines]
    raw = f"{page.total} hits\n" + "\n".join(lines)
    summary = f"{page.total} log hits for {a.query!r}"
    if not env.log_cluster or len(page.lines) <= _CLUSTER_MIN_LINES:
        return ToolOutput(content=raw, evidence_kind=EvidenceKind.LOG, summary=summary)
    # 行数多时给 LLM 模板摘要（模板 × 次数 + 代表行），原始行进证据库
    clusters = logcluster.cluster([(ln.ts.isoformat(), ln.level, ln.message, ln.service) for ln in page.lines])
    return ToolOutput(
        content=logcluster.render(clusters, page.total),
        evidence_kind=EvidenceKind.LOG,
        summary=f"{summary} in {len(clusters)} templates",
        evidence_content=raw,
    )


_CLUSTER_MIN_LINES = 20


class TraceArgs(BaseModel):
    trace_id: str


async def get_trace(env: ToolEnv, a: TraceArgs) -> ToolOutput:
    trace = await env.adapters.observability.get_trace(a.trace_id)
    lines = [
        f"{s.span_id}<-{s.parent_id} {s.service} {s.operation} {s.duration_ms}ms{' ERROR' if s.error else ''}"
        for s in trace.spans
    ]
    return ToolOutput(content="\n".join(lines) or "trace not found", evidence_kind=EvidenceKind.TRACE)


class DeploysArgs(BaseModel):
    service: str
    hours: int = Field(72, ge=1, le=24 * 30)


async def recent_deploys(env: ToolEnv, a: DeploysArgs) -> ToolOutput:
    since = datetime.now(UTC) - timedelta(hours=a.hours)
    deploys = await env.adapters.observability.recent_deploys(a.service, since)
    text = "\n".join(f"{d.deployed_at.isoformat()} {d.env} {d.sha}" for d in deploys) or "no deploys"
    return ToolOutput(content=text, evidence_kind=EvidenceKind.DEPLOY)


class StackArgs(BaseModel):
    text: str = Field(max_length=200_000)


async def parse_stacktrace(env: ToolEnv, a: StackArgs) -> ToolOutput:
    frames = _profile(env).parse_stacktrace(a.text)
    lines = [f"{'*' if f.is_project else ' '} {f.symbol} ({f.file}:{f.line})" for f in frames]
    return ToolOutput(content="\n".join(lines) or "no frames", data=frames)


class HypothesisArgs(BaseModel):
    id: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    status: Literal["open", "confirmed", "rejected"] = "open"
    evidence_ids: list[str] = Field(default_factory=list)


async def record_hypothesis(env: ToolEnv, a: HypothesisArgs) -> ToolOutput:
    # 不触达外部系统：只把假设写进审计与事件流，前端据此实时绘制假设树
    return ToolOutput(content=f"hypothesis {a.id} recorded ({a.status}, {a.confidence:.2f})", data=a.model_dump())


# ---------------- 代码（工作区内） ----------------


class ReadArgs(BaseModel):
    path: str
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")
    start_line: int = Field(1, ge=1)
    max_lines: int = Field(400, ge=1, le=2000)


async def read_file(env: ToolEnv, a: ReadArgs) -> ToolOutput:
    # 文件不存在是可预期的路径错误，不是工具故障：给 Agent 一句能照着改的提示，别让它只看到内部错误
    path = _in_repo(env, a.repo, a.path)
    try:
        text = await env.adapters.sandbox.read_file(_ws(env), path)
    except FileNotFoundError:
        raise ToolError(f"file not found in workspace: {a.path}（用 list_dir / search_code 确认路径）") from None
    lines = text.splitlines()[a.start_line - 1 : a.start_line - 1 + a.max_lines]
    numbered = "\n".join(f"{a.start_line + i:>5} {ln}" for i, ln in enumerate(lines))
    label = _label_path(env, path)
    return ToolOutput(content=numbered, summary=f"{label} lines {a.start_line}-{a.start_line + len(lines) - 1}")


class ListArgs(BaseModel):
    path: str = "."
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def list_dir(env: ToolEnv, a: ListArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    path = _in_repo(env, a.repo, a.path)
    try:
        entries = await env.adapters.sandbox.list_dir(_ws(env), path)
    except FileNotFoundError:
        raise ToolError(f"directory not found in workspace: {a.path}") from None
    relative = [_strip_root(entry, root) for entry in entries]
    return ToolOutput(content="\n".join(relative))


class SearchArgs(BaseModel):
    pattern: str = Field(min_length=2)
    glob: str = "**/*"
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库 + 所有附加仓库")


async def search_code(env: ToolEnv, a: SearchArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    glob = f"{root}/**/*" if root else a.glob
    hits = await env.adapters.sandbox.search(_ws(env), a.pattern, glob)
    if a.repo and not root:
        # 显式指定主仓库时排除附加仓库目录；不指定才跨整个工作区搜索
        hits = [hit for hit in hits if not hit[0].replace("\\", "/").startswith("_repos/")]
    shown = [(_label_path(env, p), n, line) for p, n, line in hits[:200]]
    text = "\n".join(f"{p}:{n}: {line}" for p, n, line in shown)
    return ToolOutput(content=f"{len(hits)} matches\n{text}", summary=f"{len(hits)} matches for {a.pattern!r}")


class SymbolSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    kind: (
        Literal[
            "class",
            "interface",
            "enum",
            "record",
            "annotation",
            "function",
            "method",
            "constructor",
            "field",
            "enum_constant",
            "constant",
            "variable",
        ]
        | None
    ) = None
    repo: str | None = Field(None, description="限定仓库名；留空 = 所有已加载仓库")
    limit: int = Field(20, ge=1, le=50)


async def search_symbol(env: ToolEnv, a: SymbolSearchArgs) -> ToolOutput:
    index = await codeintel.get_index(env.adapters.sandbox, _ws(env), _profile(env).name)
    root = _repo_root(env, a.repo)
    hits = index.search(a.query, a.kind, a.limit)
    if a.repo:
        hits = [symbol for symbol in hits if _path_in_root(symbol.path, root)]
    meta = index.meta
    head = f"{len(hits)} symbols (indexed {meta.files} files / {meta.symbols} symbols / {meta.edges} edges)"
    if meta.truncated:
        head += "; index truncated (large repo)"
    if not hits:
        return ToolOutput(content=f"{head}\nno symbols match {a.query!r}; try a shorter name or use search_code")
    lines = [f"[{s.kind}] {s.id}  {_label_path(env, s.path)}:{s.start_line}  {s.signature[:180]}" for s in hits]
    return ToolOutput(content=f"{head}\n" + "\n".join(lines), summary=f"{len(hits)} symbols for {a.query!r}")


class ReferencesArgs(BaseModel):
    symbol: str = Field(min_length=1, description="符号全限定名或名字，例如 PriceCalculator#discountFor/2")
    kind: Literal["all", "calls", "types"] = "all"
    repo: str | None = Field(None, description="限定仓库名；留空 = 所有已加载仓库")
    limit: int = Field(40, ge=1, le=100)


async def find_references(env: ToolEnv, a: ReferencesArgs) -> ToolOutput:
    index = await codeintel.get_index(env.adapters.sandbox, _ws(env), _profile(env).name)
    root = _repo_root(env, a.repo)
    symbol, candidates = index.resolve_symbol(a.symbol, path_prefix=root if a.repo else "")
    if a.repo and not root:
        # 显式指定主仓库：排除 _repos/ 下的同名符号
        if symbol is not None and not _path_in_root(symbol.path, root):
            symbol = None
        candidates = [candidate for candidate in candidates if _path_in_root(candidate.path, root)]
        if symbol is None:
            scoped = [
                s
                for s in index.symbols.values()
                if _path_in_root(s.path, root)
                and (s.name == a.symbol or s.qualified_name == a.symbol or s.id == a.symbol)
            ]
            if len(scoped) == 1:
                symbol, candidates = scoped[0], []
            elif scoped:
                candidates = scoped
    if symbol is None:
        if candidates:
            lines = [f"[{s.kind}] {s.id}  {_label_path(env, s.path)}:{s.start_line}" for s in candidates]
            return ToolOutput(content="symbol is ambiguous; narrow it down:\n" + "\n".join(lines))
        raise ToolError(f"symbol not found: {a.symbol!r}（用 search_symbol 先确认名字）")
    refs = index.find_references(symbol, a.kind, a.limit)
    if a.repo:
        refs = [ref for ref in refs if _path_in_root(ref.edge.path, root)]
    if not refs:
        return ToolOutput(content=f"{symbol.kind} {symbol.id} has no references in this repository")
    resolved = sum(1 for r in refs if r.edge.dst)
    lines = []
    for ref in refs:
        source = ref.source.signature if ref.source else ref.edge.src
        state = "" if ref.edge.dst else " (unresolved)"
        lines.append(f"- {ref.edge.kind}{state}  {_label_path(env, ref.edge.path)}:{ref.edge.line}  by {source[:160]}")
    head = (
        f"{symbol.kind} {symbol.id}  {_label_path(env, symbol.path)}:{symbol.start_line}\n"
        f"{len(refs)} references ({resolved} resolved)"
    )
    return ToolOutput(content=f"{head}\n" + "\n".join(lines), summary=f"{len(refs)} references to {symbol.name}")


class OutlineArgs(BaseModel):
    path: str = Field(min_length=1)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def code_outline(env: ToolEnv, a: OutlineArgs) -> ToolOutput:
    index = await codeintel.get_index(env.adapters.sandbox, _ws(env), _profile(env).name)
    path = _in_repo(env, a.repo, a.path)
    items = index.outline(path)
    if not items:
        known = sorted({s.path for s in index.symbols.values() if path.rsplit("/", 1)[-1] in s.path})[:8]
        hint = ("\nfiles with a similar name:\n" + "\n".join(_label_path(env, p) for p in known)) if known else ""
        raise ToolError(
            f"no symbols indexed for {a.path!r}（用 list_dir 确认路径；"
            f"{_profile(env).name} 索引不覆盖的文件请用 read_file）{hint}"
        )
    lines = [f"{s.start_line:>5}-{s.end_line:<5} [{s.kind}] {s.signature[:180]}" for s in items]
    label = _label_path(env, path)
    return ToolOutput(content=f"{label}: {len(items)} symbols\n" + "\n".join(lines), summary=f"outline of {label}")


class BlameArgs(BaseModel):
    path: str = Field(min_length=1)
    start_line: int | None = Field(None, ge=1)
    end_line: int | None = Field(None, ge=1)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def git_blame(env: ToolEnv, a: BlameArgs) -> ToolOutput:
    start = a.start_line or 1
    end = a.end_line or start + 79
    if end < start:
        raise ToolError("end_line must be >= start_line")
    root = _repo_root(env, a.repo)
    path = _strip_root(a.path, root) if root else a.path
    res = await env.adapters.sandbox.git(
        _ws(env),
        ["blame", "--line-porcelain", "-L", f"{start},{end}", "--", path],
        60,
        cwd=root or None,
    )
    if res.exit_code != 0:
        raise ToolError(f"git blame failed: {res.stderr.strip()[:400] or a.path}")
    entries = _parse_blame(res.stdout)
    label = _label_in_repo(env, a.repo, a.path)
    if not entries:
        return ToolOutput(content=f"no blame entries for {label} lines {start}-{end}")
    lines = [f"{line:>5} {sha[:8]} {author:<16} {date}  {text[:160]}" for line, sha, author, date, text in entries]
    return ToolOutput(content="\n".join(lines), summary=f"blame {label} lines {start}-{end}")


class GitLogArgs(BaseModel):
    path: str | None = None
    limit: int = Field(10, ge=1, le=50)
    since: str | None = Field(None, description="git --since, e.g. '30 days ago'")
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def git_log(env: ToolEnv, a: GitLogArgs) -> ToolOutput:
    args = ["log", "--no-decorate", "--date=short", "--pretty=format:%h %ad %an %s", f"-n{a.limit}"]
    root = _repo_root(env, a.repo)
    if a.since:
        args.append(f"--since={a.since}")
    if a.path:
        args.extend(["--", _strip_root(a.path, root) if root else a.path])
    res = await env.adapters.sandbox.git(_ws(env), args, 60, cwd=root or None)
    if res.exit_code != 0:
        raise ToolError(f"git log failed: {res.stderr.strip()[:400]}")
    label = _label_in_repo(env, a.repo, a.path)
    return ToolOutput(content=res.stdout.strip() or "(no commits)", summary=f"git log {label}")


def _parse_blame(output: str) -> list[tuple[int, str, str, str, str]]:
    entries: list[tuple[int, str, str, str, str]] = []
    sha = author = date = ""
    line_no = 0
    for raw in output.splitlines():
        if len(raw) >= 40 and raw[:40].isalnum() and " " in raw:
            parts = raw.split()
            if len(parts) >= 3 and parts[0].isalnum():
                sha = parts[0]
                line_no = int(parts[2]) if parts[2].isdigit() else 0
                author = date = ""
                continue
        if raw.startswith("author ") and not raw.startswith("author-"):
            author = raw.removeprefix("author ").strip()
        elif raw.startswith("author-time "):
            stamp = raw.removeprefix("author-time ").strip()
            date = datetime.fromtimestamp(int(stamp), UTC).strftime("%Y-%m-%d") if stamp.isdigit() else ""
        elif raw.startswith("\t"):
            entries.append((line_no, sha, author, date, raw[1:]))
    return entries


class WriteArgs(BaseModel):
    path: str
    content: str = Field(max_length=500_000)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库。只能写可修复仓库")


async def write_file(env: ToolEnv, a: WriteArgs) -> ToolOutput:
    agent = getattr(getattr(env.run, "agent", None), "value", None)
    if a.repo and agent != "fixer":
        raise PermissionError("only the fixer may write to additional repositories")
    if a.repo and not _repo_fixable(env, a.repo):
        raise PermissionError(f"repo {a.repo!r} is read-only; mark it fixable in project config first")
    ws = _ws(env)
    root = _repo_root(env, a.repo)
    raw = a.path.replace("\\", "/").removeprefix("./")
    # 主仓库根目录下的 _repos/ 是附加仓库挂载点：必须走 repo=... 才能写，避免绕过 fixable 授权
    if not root and (raw == REPO_ROOT_PREFIX or raw.startswith(f"{REPO_ROOT_PREFIX}/")):
        raise PermissionError("additional repositories must be written with repo=<name>")
    path = _in_repo(env, a.repo, a.path)
    await env.adapters.sandbox.write_file(ws, path, a.content)
    codeintel.invalidate(ws.id)
    return ToolOutput(content=f"wrote {_label_path(env, path)} ({len(a.content)} chars)")


class DiffArgs(BaseModel):
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def workspace_diff(env: ToolEnv, a: DiffArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    if not root:
        diff = await env.adapters.sandbox.diff(_ws(env))
    else:
        await env.adapters.sandbox.git(_ws(env), ["add", "-A", "-N", "."], 60, cwd=root)
        res = await env.adapters.sandbox.git(_ws(env), ["diff", "--no-color", BASE_TAG], 60, cwd=root)
        diff = res.stdout if res.ok else ""
    label = a.repo or "primary"
    return ToolOutput(
        content=diff or "(no changes)", evidence_kind=EvidenceKind.CODE, summary=f"workspace diff ({label})"
    )


# ---------------- L2-sandbox 构建/测试/孪生 ----------------


class BuildArgs(BaseModel):
    module: str | None = None
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def build(env: ToolEnv, a: BuildArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    res = await _profile(env).build(env.adapters.sandbox, _ws(env), a.module, cwd=root or None)
    status = "BUILD OK" if res.ok else f"BUILD FAILED (exit {res.exit_code}{', timeout' if res.timed_out else ''})"
    return ToolOutput(
        content=f"{status}\n{res.stdout[-4000:]}\n{res.stderr[-4000:]}",
        data=res,
        evidence_kind=EvidenceKind.TEST_OUTPUT,
        summary=status,
    )


class TestsArgs(BaseModel):
    classes: list[str] = Field(
        default_factory=list,
        description=(
            "Java: 测试类名；Python: 文件或目录路径（pytest 选择器，如 orders/tests/test_x.py）；"
            "C#: Namespace.Class（如 Shop.Tests.PricingTests）"
        ),
    )
    methods: list[str] = Field(
        default_factory=list,
        description="Class#method；Python 用 path::test_name；C# 用 Namespace.Class::TestName",
    )
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def run_tests(env: ToolEnv, a: TestsArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    report = await _profile(env).run_tests(
        env.adapters.sandbox, _ws(env), TestSelector(a.classes, a.methods), cwd=root or None
    )
    failed = report.failed
    head = f"{len(report.cases)} tests, {len(failed)} failed, exit {report.command.exit_code}"
    body = "\n".join(f"FAIL {c.name}: {c.message[:500]}" for c in failed)
    if report.command.exit_code != 0 and not failed:
        # 没有失败用例却非零退出：多半是编译错误或测试没被选中，把构建输出给 Agent 看
        body = _build_errors(report.command.stdout + "\n" + report.command.stderr)
    return ToolOutput(content=f"{head}\n{body}", data=report, evidence_kind=EvidenceKind.TEST_OUTPUT, summary=head)


class TwinRequestArgs(BaseModel):
    service: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = Field(pattern=r"^/")
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, object] | list[object] | str | None = None


async def twin_request(env: ToolEnv, a: TwinRequestArgs) -> ToolOutput:
    twin = _twin(env)
    if a.service not in twin.urls:
        # 给 Agent 可操作的错误（例如把 postgres 当成 HTTP 服务），而不是笼统的内部错误
        raise ToolError(f"unknown twin service {a.service!r}; HTTP services: {sorted(twin.urls)}")
    resp = await env.adapters.twin.exec_request(twin, HttpRequest(a.service, a.method, a.path, a.headers, a.body))
    return ToolOutput(content=f"HTTP {resp.status} in {resp.duration_ms}ms\n{resp.body}", data=resp)


class TwinLogsArgs(BaseModel):
    service: str
    minutes: int = Field(10, ge=1, le=24 * 60)


async def twin_logs(env: ToolEnv, a: TwinLogsArgs) -> ToolOutput:
    since = datetime.now(UTC) - timedelta(minutes=a.minutes)
    text = await env.adapters.twin.logs(_twin(env), a.service, since)
    return ToolOutput(content=text or "(empty)", evidence_kind=EvidenceKind.LOG, summary=f"twin logs {a.service}")


class JvmProbeArgs(BaseModel):
    service: str = Field(description="Twin service name, e.g. order")
    command: str = Field(
        max_length=200,
        description=(
            "Arthas command from the read-only whitelist, e.g. 'thread -n 5', 'thread -b', 'jvm', 'sc -d com.x.Foo'"
        ),
    )
    timeout_s: int = Field(15, ge=1, le=120)


async def jvm_probe(env: ToolEnv, a: JvmProbeArgs) -> ToolOutput:
    """在孪生环境的 JVM 里执行只读 Arthas 命令（线程栈、JVM 状态、反编译、方法追踪）。"""
    profile = _profile(env)
    probe = profile.runtime_probe()
    if probe is None:
        raise ToolError(f"profile {profile.name!r} has no runtime probe; use twin_logs instead")
    command = " ".join(a.command.split())  # 归一化空白，避免 "thread   -b" 绕过白名单
    if command not in probe.allowed_commands:
        # 白名单是精确匹配：允许的命令都在这里，Agent 照抄即可，不要自己拼参数组合
        raise ToolError(f"command {command!r} not allowed; allowed commands: {sorted(probe.allowed_commands)}")
    twin = _twin(env)
    if a.service not in twin.ports:
        raise ToolError(f"unknown twin service {a.service!r}; available: {sorted(twin.ports)}")
    try:
        text = await env.adapters.twin.probe(twin, a.service, command, a.timeout_s)
    except ValueError as exc:  # 未知服务等可预期的调用错误
        raise ToolError(str(exc)) from None
    except RuntimeError as exc:  # 探针没起来 / 命令被服务端拒绝
        raise ToolError(f"{probe.name} probe failed: {exc}") from None
    kind = EvidenceKind.THREAD_DUMP if command.split()[0] == "thread" else EvidenceKind.TOOL_OUTPUT
    return ToolOutput(
        content=text or "(no output)",
        evidence_kind=kind,
        summary=f"{probe.name} {command} on twin service {a.service}",
    )


# ---------------- L2-external 外部写 ----------------


def _target_repo(env: ToolEnv, repo: str | None) -> str:
    if not repo:
        return _repo(env)
    info = _repo_info(env, repo)
    if not info:
        raise ToolError(f"repo not loaded: {repo!r}")
    return str(info.get("repo") or repo)


class PushArgs(BaseModel):
    branch: str = Field(pattern=r"^fix/twinfix-[a-z0-9-]+$")
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def push_branch(env: ToolEnv, a: PushArgs) -> ToolOutput:
    root = _repo_root(env, a.repo)
    if env.adapters.workspace_manager is not None:
        sha = await env.adapters.workspace_manager.push(_ws(env), _target_repo(env, a.repo), a.branch, cwd=root or None)
    elif a.repo:
        raise ToolError("additional repos require a workspace manager (codehost=local or github)")
    else:
        sha = await env.adapters.codehost.push_branch(_ws(env), a.branch)
    return ToolOutput(content=f"pushed {_label_in_repo(env, a.repo)} {a.branch} @ {sha}", data=sha)


class OpenPRArgs(BaseModel):
    head: str = Field(pattern=r"^fix/twinfix-[a-z0-9-]+$")
    base: str
    title: str = Field(max_length=200)
    body: str = Field(max_length=60_000)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def open_pr(env: ToolEnv, a: OpenPRArgs) -> ToolOutput:
    pr = await env.adapters.codehost.open_pr(PRRequest(_target_repo(env, a.repo), a.head, a.base, a.title, a.body))
    return ToolOutput(content=f"opened PR #{pr.number} {pr.url}", data=pr)


class CheckArgs(BaseModel):
    sha: str
    conclusion: Literal["success", "failure", "neutral"]
    summary: str = Field(max_length=60_000)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def set_check(env: ToolEnv, a: CheckArgs) -> ToolOutput:
    await env.adapters.codehost.set_check(
        _target_repo(env, a.repo), a.sha, CheckResult("TwinFix", a.conclusion, a.summary)
    )
    return ToolOutput(content=f"check {a.conclusion} on {a.sha[:10]}")


class CommentArgs(BaseModel):
    kind: Literal["issue", "pr"]
    number: int
    body: str = Field(max_length=60_000)
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def comment(env: ToolEnv, a: CommentArgs) -> ToolOutput:
    repo = _target_repo(env, a.repo)
    target = IssueRef(repo, a.number) if a.kind == "issue" else PRRef(repo, a.number)
    await env.adapters.codehost.comment(target, a.body)
    return ToolOutput(content=f"commented on {a.kind} #{a.number}")


class SendReportArgs(BaseModel):
    report_id: str
    type: str
    title: str
    content: str
    recipients: list[str] = Field(default_factory=list, description="channel:address, e.g. email:a@b.com")


async def send_report(env: ToolEnv, a: SendReportArgs) -> ToolOutput:
    recipients = [Recipient(*r.split(":", 1)) for r in a.recipients if ":" in r]
    report = OutgoingReport(a.report_id, env.run.incident_id, a.type, a.title, a.content)
    results = []
    for notifier in env.adapters.notifiers:
        targeted = [r for r in recipients if r.channel == notifier.channel]
        if notifier.channel != "log" and not targeted:
            continue
        res = await notifier.send(report, targeted)
        results.append((notifier.channel, res))
    failed = [f"{ch}: {r.error}" for ch, r in results if not r.ok]
    if failed:
        raise ToolError("; ".join(failed))
    return ToolOutput(
        content=f"sent via {', '.join(ch for ch, _ in results) or 'none'}", data=[ch for ch, _ in results]
    )


# ---------------- L3 合并 ----------------


class MergeArgs(BaseModel):
    number: int
    expected_sha: str
    method: Literal["merge", "squash", "rebase"] = "squash"
    repo: str | None = Field(None, description="附加仓库名；留空 = 主仓库")


async def merge_pr(env: ToolEnv, a: MergeArgs) -> ToolOutput:
    res = await env.adapters.codehost.merge_pr(_target_repo(env, a.repo), a.number, a.method, a.expected_sha)
    if not res.merged:
        raise ToolError(f"merge rejected: {res.message}")
    return ToolOutput(content=f"merged PR #{a.number} -> {res.sha}", data=res)


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in [
        Tool(
            "query_metrics",
            "Query a Prometheus metric and get a textual summary",
            L0,
            MetricsArgs,
            query_metrics,
            30,
            30,
        ),
        Tool("search_logs", "Search logs by text, service and trace id", L0, LogsArgs, search_logs, 30, 40),
        Tool("get_trace", "Fetch a distributed trace by id", L0, TraceArgs, get_trace, 30, 20),
        Tool("recent_deploys", "List recent deployments of a service", L0, DeploysArgs, recent_deploys, 30, 10),
        Tool(
            "parse_stacktrace",
            "Parse a stack trace into frames; project frames are starred",
            L0,
            StackArgs,
            parse_stacktrace,
            10,
            20,
        ),
        Tool(
            "record_hypothesis",
            "Record or update a root-cause hypothesis",
            L0,
            HypothesisArgs,
            record_hypothesis,
            5,
            60,
        ),
        Tool("read_file", "Read a file in the workspace", L0, ReadArgs, read_file, 15, 200, read_path_args=("path",)),
        Tool(
            "list_dir", "List a directory in the workspace", L0, ListArgs, list_dir, 15, 100, read_path_args=("path",)
        ),
        Tool("search_code", "Search text in workspace files", L0, SearchArgs, search_code, 30, 100),
        Tool(
            "search_symbol",
            "Search code symbols (classes, methods, fields) by name; cheaper than reading files",
            L0,
            SymbolSearchArgs,
            search_symbol,
            30,
            80,
        ),
        Tool(
            "find_references",
            "Find callers and type references of a symbol (resolved and unresolved)",
            L0,
            ReferencesArgs,
            find_references,
            30,
            60,
        ),
        Tool(
            "code_outline",
            "List symbols in a file with line ranges, without reading the whole file",
            L0,
            OutlineArgs,
            code_outline,
            15,
            60,
        ),
        Tool("git_blame", "Show blame (commit, author, date) for a file line range", L0, BlameArgs, git_blame, 30, 30),
        Tool("git_log", "Show recent commit history, optionally for one path", L0, GitLogArgs, git_log, 30, 30),
        Tool(
            "write_file",
            "Create or overwrite a file in the workspace",
            L2S,
            WriteArgs,
            write_file,
            15,
            60,
            2000,
            write_path_args=("path",),
        ),
        Tool(
            "workspace_diff", "Show the current git diff of the workspace", L2S, DiffArgs, workspace_diff, 30, 20, 20000
        ),
        Tool("build", "Compile the project (tests are not run)", L2S, BuildArgs, build, 900, 15),
        Tool("run_tests", "Run all or selected tests", L2S, TestsArgs, run_tests, 1800, 20),
        Tool(
            "twin_request",
            "Send an HTTP request to a service in the twin environment",
            L2S,
            TwinRequestArgs,
            twin_request,
            60,
            100,
        ),
        Tool("twin_logs", "Read recent logs of a twin service", L2S, TwinLogsArgs, twin_logs, 30, 30),
        Tool(
            "jvm_probe",
            "Run a read-only Arthas command inside a twin service JVM "
            "(thread dumps, JVM state, jad/sc/sm, bounded trace/watch); whitelist applies",
            L1,
            JvmProbeArgs,
            jvm_probe,
            130,
            10,
            20000,
        ),
        Tool("push_branch", "Push the workspace to a fix/twinfix-* branch", L2E, PushArgs, push_branch, 120, 5),
        Tool("open_pr", "Open a pull request", L2E, OpenPRArgs, open_pr, 60, 3),
        Tool("set_check", "Set the TwinFix check on a commit", L2E, CheckArgs, set_check, 60, 10),
        Tool("comment", "Comment on an issue or pull request", L2E, CommentArgs, comment, 60, 10),
        Tool("send_report", "Send a report through the configured channels", L2E, SendReportArgs, send_report, 60, 10),
        Tool(
            "merge_pr",
            "Merge a pull request (requires approval)",
            L3,
            MergeArgs,
            merge_pr,
            120,
            2,
            approval=ApprovalAction.MERGE,
        ),
    ]:
        reg.register(tool)
    return reg
