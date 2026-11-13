"""基准测试命令行。

twinfix-arena validate [C001 C003 ...]            # 用例自检（无需 TwinFix 服务）
twinfix-arena eval --api http://127.0.0.1:8000/api --git-root .data/git [C001 ...]
    # 端到端评测：需要以 codehost=local 运行、与这里共享 git_root 的 TwinFix
    # 报告写到 eval/reports/<date>-<run>.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from twinfix.arena.cases import Case, load_cases
from twinfix.arena.grading import render_comparison, render_report, report_json
from twinfix.arena.hidden import CSharp, Go, Maven, Node, Pytest
from twinfix.arena.runner import ArenaRunner, RunConfig, TwinFixAPI
from twinfix.arena.validate import RepeatResult, Validation, validate_case

REPO_ROOT = Path(__file__).resolve().parents[3]
ARENA = REPO_ROOT / "arena"

# 快速回归套件：从 24 例里挑跨轮稳定、覆盖主要结论类型的用例。
# fast  约 45–60 分钟（3 个修复 + NEEDS_HUMAN + NOT_BUG），用于日常改动/PR 前；
# core  约 75–90 分钟，补上 ALREADY_FIXED 与缓存类，用于 nightly。
# 跨轮稳定性不足的用例（C003/C004/C007/C010/C016）不放进套件。
SUITES: dict[str, tuple[str, ...]] = {
    "fast": ("C001", "C002", "C011", "C014", "C018", "C024"),
    "core": ("C001", "C002", "C009", "C011", "C012", "C014", "C018", "C024"),
    # Python 用例：修复 ×2（异常/可变默认参数）+ NOT_BUG + NEEDS_HUMAN；比 Java 套件短，适合改 Python 侧时回归
    "python": ("P001", "P002", "P003", "P004"),
    # Go 用例：修复 ×2（nil pointer / 状态泄漏）+ NOT_BUG + NEEDS_HUMAN
    "go": ("G001", "G002", "G003", "G004"),
    # TypeScript 用例：修复 ×2（空值解引用 / 状态泄漏）+ NOT_BUG + NEEDS_HUMAN
    "typescript": ("T001", "T002", "T003", "T004"),
    # C# 用例：修复 ×2（空引用 / 状态泄漏）+ NOT_BUG + NEEDS_HUMAN
    "csharp": ("S001", "S002", "S003", "S004"),
}


def _maven(args: argparse.Namespace) -> Maven:
    return Maven(
        offline=args.offline,
        maven_image=args.maven_image or None,
        settings=args.maven_settings or None,
        repo_local=args.maven_repo or None,
    )


def _pytest(args: argparse.Namespace) -> Pytest:
    return Pytest(python=args.python or sys.executable)


def _go(args: argparse.Namespace) -> Go:
    return Go(executable=args.go or "go", image=args.go_image or None)


def _node(args: argparse.Namespace) -> Node:
    return Node(executable=args.node or "node", image=args.node_image or None)


def _csharp(args: argparse.Namespace) -> CSharp:
    return CSharp(executable=args.dotnet or "dotnet", image=args.csharp_image or None)


def _print_checks(v: Validation, indent: str) -> None:
    for name, passed, detail in v.checks:
        print(f"{indent}[{'x' if passed else ' '}] {name}")
        if not passed and detail:
            print(indent + "    " + detail.replace("\n", "\n" + indent + "    "))


def _print_result(case: Case, result: RepeatResult, repeat: int) -> None:
    if repeat == 1:
        print(f"{'PASS' if result.ok else 'FAIL'} {case.id}  {case.title}")
        _print_checks(result.runs[0], "   ")
        return
    print(f"{result.status} {case.id}  {case.title}  ({result.passed_runs}/{repeat} runs)")
    for i, run in enumerate(result.runs, 1):
        if run.ok and result.status == "PASS":
            continue
        print(f"   run {i}/{repeat}: {'PASS' if run.ok else 'FAIL'}")
        if not run.ok:
            _print_checks(run, "     ")


async def _validate(args: argparse.Namespace) -> int:
    cases = load_cases(ARENA / "cases", set(args.cases) or None)
    maven = _maven(args)
    pytest_runner = _pytest(args)
    go_runner = _go(args)
    node_runner = _node(args)
    csharp_runner = _csharp(args)
    repeat = max(1, args.repeat)
    failed = flaky = 0
    for case in cases:
        runs = [
            await validate_case(case, ARENA, maven, pytest_runner, go_runner, node_runner, csharp_runner)
            for _ in range(repeat)
        ]
        result = RepeatResult(case.id, runs)
        _print_result(case, result, repeat)
        failed += not result.ok
        flaky += result.status == "FLAKY"
    summary = f"\n{len(cases) - failed}/{len(cases)} cases valid"
    if flaky:
        summary += f", {flaky} flaky"
    print(summary)
    return 1 if failed else 0


async def _eval(args: argparse.Namespace) -> int:
    selected = set(args.cases) if args.cases else (set(SUITES[args.suite]) if args.suite else None)
    cases = load_cases(ARENA / "cases", selected)
    run_id = args.run_id or (f"{args.suite}-" if args.suite else "") + datetime.now(UTC).strftime("%H%M%S")
    cfg = RunConfig(
        arena_root=ARENA,
        git_root=Path(args.git_root),
        run_id=run_id,
        timeout_s=args.timeout,
        cleanup=not args.keep,
        model=args.model,
        effort=args.effort,
    )
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    # trust_env=False：本机 HTTP_PROXY 不应拦截对 127.0.0.1 的访问
    async with httpx.AsyncClient(base_url=args.api, headers=headers, timeout=30, trust_env=False) as client:
        scores = await ArenaRunner(
            TwinFixAPI(client),
            _maven(args),
            cfg,
            pytest=_pytest(args),
            go_runner=_go(args),
            node_runner=_node(args),
            csharp_runner=_csharp(args),
        ).run(cases)

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    meta = {
        "运行": run_id,
        "日期": date,
        "Agent": args.label or os.environ.get("TWINFIX_AGENT_MODE", "unknown"),
        "用例": ", ".join(c.id for c in cases),
    }
    if args.model:
        meta["模型"] = args.model
    if args.effort:
        meta["推理强度"] = args.effort
    if args.suite:
        meta["套件"] = args.suite
    title = f"TwinFix 基准测试评测 {date}"
    report = render_report(title, scores, meta)
    out = Path(args.out) if args.out else REPO_ROOT / "eval" / "reports" / f"{date}-{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    data = report_json(title, scores, meta)
    out.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    print(f"report: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twinfix-arena")
    ap.add_argument("--offline", action="store_true", help="Maven -o（依赖已在本地仓库）")
    ap.add_argument("--maven-image", default="", help="用 docker 运行该 Maven 镜像，而不是本机 mvn")
    ap.add_argument("--maven-settings", default="", help="Maven -s：指定 settings.xml（镜像源）")
    ap.add_argument("--maven-repo", default="", help="Maven -Dmaven.repo.local：指定本地仓库目录")
    ap.add_argument("--python", default="", help="Python 判分用的解释器（默认当前解释器；需装 pytest）")
    ap.add_argument("--go", default="", help="Go 判分用的可执行文件（默认 PATH 里的 go）")
    ap.add_argument(
        "--go-image",
        default="",
        help="用 docker run 该镜像执行 go test（本机没有 Go 时用，例如 twinfix-sandbox-go:1.25）",
    )
    ap.add_argument("--node", default="", help="TypeScript/JavaScript 判分用的 node（默认 PATH 里的 node）")
    ap.add_argument(
        "--node-image",
        default="",
        help="用 docker run 该镜像执行 node --test（本机没有 Node 时用，例如 twinfix-sandbox-typescript:22）",
    )
    ap.add_argument("--dotnet", default="", help="C# 判分用的 dotnet（默认 PATH 里的 dotnet）")
    ap.add_argument(
        "--csharp-image",
        default="",
        help="用 docker run 该镜像执行 dotnet run（本机没有 .NET SDK 时用，例如 twinfix-sandbox-csharp:10.0）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="校验用例：bug 潜伏、隐藏测试有效、参考修复可通过")
    v.add_argument("cases", nargs="*")
    v.add_argument(
        "-n",
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="每个用例重复自检 N 次；结果不一致的用例标记为 FLAKY（默认 1）",
    )

    e = sub.add_parser("eval", help="端到端评测")
    e.add_argument("cases", nargs="*")
    e.add_argument("--api", default="http://127.0.0.1:8000/api")
    e.add_argument("--token", default=os.environ.get("TWINFIX_TOKEN", ""))
    e.add_argument("--git-root", default=os.environ.get("TWINFIX_GIT_ROOT", ".data/git"))
    e.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help="单个用例等待收敛的秒数（对齐状态超时；low 推理 1800 通常够，high 推理建议 3600）",
    )
    e.add_argument("--run-id", default="")
    e.add_argument("--label", default="", help="报告里标注的 Agent/模型名")
    e.add_argument("--model", default="", help="本次评测使用的模型（写入各用例项目的 agent-config，空 = 全局默认）")
    e.add_argument(
        "--effort",
        default="",
        choices=["", "low", "medium", "high"],
        help="本次评测的推理强度（写入各用例项目的 agent-config，空 = 全局默认）",
    )
    e.add_argument("--out", default="")
    e.add_argument("--keep", action="store_true", help="不取消停在评审/人工的事故")
    e.add_argument(
        "--suite",
        choices=sorted(SUITES),
        default="",
        help="按预设用例集评测（fast / core / python / go / typescript / csharp）",
    )

    c = sub.add_parser("compare", help="并排对比多份评测 JSON（模型对比 / 对照实验）")
    c.add_argument("reports", nargs="+", help="eval/reports/*.json")
    c.add_argument("--out", default="")

    g = sub.add_parser("gate", help="对评测报告做回归门禁（任一阈值不满足则退出码非 0）")
    g.add_argument("report", help="eval/reports/*.json")
    g.add_argument("--min-outcome", type=float, default=0.83, help="结论正确率下限（默认 5/6）")
    g.add_argument("--min-fix-rate", type=float, default=0.0, help="修复率下限")
    g.add_argument("--max-false-fix", type=float, default=0.0, help="误修率上限")
    g.add_argument("--max-needs-human", type=float, default=1.0, help="人工介入率上限")
    g.add_argument("--max-avg-duration", type=float, default=0.0, help="平均耗时上限秒数（0 = 不检查）")

    args = ap.parse_args(argv)
    if args.cmd == "eval" and args.suite and args.cases:
        ap.error("--suite 与显式用例不能同时使用")
    if args.cmd == "compare":
        return _compare(args)
    if args.cmd == "gate":
        return _gate(args)
    handler = _validate if args.cmd == "validate" else _eval
    return asyncio.run(handler(args))


def _gate(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    summary = report.get("summary") or {}
    checks = [
        ("结论正确率", float(summary.get("outcome_accuracy", 0.0)), ">=", args.min_outcome),
        ("修复率", float(summary.get("fix_rate", 0.0)), ">=", args.min_fix_rate),
        ("误修率", float(summary.get("false_fix_rate", 0.0)), "<=", args.max_false_fix),
        ("人工介入率", float(summary.get("needs_human_rate", 0.0)), "<=", args.max_needs_human),
    ]
    if args.max_avg_duration > 0:
        checks.append(("平均耗时(s)", float(summary.get("avg_duration_s", 0.0)), "<=", args.max_avg_duration))
    failed = 0
    print(f"report: {args.report}  cases: {summary.get('total', 0)}")
    for name, actual, op, expected in checks:
        ok = actual >= expected if op == ">=" else actual <= expected
        failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual:.3f} {op} {expected:.3f}")
    print("gate: PASS" if not failed else f"gate: FAIL ({failed} threshold(s))")
    return 1 if failed else 0


def _compare(args: argparse.Namespace) -> int:
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]
    text = "# TwinFix 评测对比\n\n" + render_comparison(reports)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
