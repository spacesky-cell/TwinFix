"""防作弊检查（docs/05-safety.md §5）：由系统对修复 diff 做确定性扫描，不采信 Agent 自报。

- 违规（violation）：删除 / 跳过 / 弱化已有测试 → 验证失败，交回 Fixer。
- 可疑（suspicious）：新增吞异常的 catch、按测试数据特判等 → 不阻断，在验证结果与报告里标出供人评审。

复现用例由 Reproducer 写入，不在 Fixer 的 diff 里；Fixer 对测试目录的任何删改都按"已有测试"处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from twinfix.gateway import paths

# 跳过 / 禁用测试的写法（JUnit 4/5、AssertJ/Assumptions、pytest/unittest、Go testing、Node test runner）
_SKIP = re.compile(
    r"@Disabled\b|@Ignore\b|\bassumeTrue\s*\(\s*false\s*\)|\bassumeFalse\s*\(\s*true\s*\)|\bAssumptions\.abort\b"
    r"|@pytest\.mark\.skip\b|@pytest\.mark\.skipif\b|@unittest\.skip\b|@unittest\.skipIf\b"
    r"|pytest\.skip\s*\(|pytest\.xfail\s*\(|self\.skipTest\s*\("
    r"|\bt\.(?:Skip|Skipf|SkipNow)\s*\("
    r"|(?:\btest|\bit|\bdescribe)\.(?:skip|todo)\s*\(|\bxit\s*\(|\bxdescribe\s*\(|\bskip\s*:\s*true\b"
    r"|\bAssert\.Ignore\s*\(|\[(?:Fact|Theory)\s*\(\s*Skip\s*="
)
_ASSERT = re.compile(
    r"\bassert\w*\s*\(|\bassertThat\w*\s*\(|\bverify\s*\(|\bfail\s*\(|\.is\w+\(|\.has\w+\(|\.contains\w*\("
    r"|^\s*assert\b|^\s*expect\s*\(|pytest\.raises\s*\(|\bassert\.\w+\s*\("
    r"|\bt\.(?:Fatal|Fatalf|Error|Errorf)\s*\(|\brequire\.\w+\s*\("
    r"|\bCheck\.\w+\s*\("
)
_TEST_ANNOTATION = re.compile(
    r"@(Test|ParameterizedTest|RepeatedTest)\b|^\s*(?:async\s+)?def\s+test_\w+\s*\("
    r"|^\s*func\s+Test\w+\s*\(|^\s*(?:test|it)\s*\("
    r"|^\s*public\s+static\s+(?:async\s+)?(?:void|Task)\s+Test\w+\s*\("
)
# 空 catch 或 catch 内只有注释 / 日志（Java 带类型、TS/JS 单绑定或带类型注解、C# 也允许无绑定）
_CATCH = re.compile(r"\bcatch\s*(?:\(\s*[\w.|:<>\[\],\s]*\))?\s*\{")
_LOG_ONLY = re.compile(
    r"^\s*(//.*|/\*.*\*/|log\.\w+\(.*\);?|logger\.\w+\(.*\);?|console\.\w+\(.*\);?"
    r"|Console\.\w+\(.*\);?|[\w.]+\.Log\w*\(.*\);?|e\.printStackTrace\(\);|)\s*$"
)
# Python 的 except 块：下一行是 pass / 注释 / 纯日志（print/logging）视为吞异常
_PY_EXCEPT = re.compile(r"^\s*except\b[^:]*:\s*$")
_PY_LOG_ONLY = re.compile(r"^\s*(#.*|pass\s*|print\(.*\)|logger\.\w+\(.*\)|logging\.\w+\(.*\)|)$")
# 按测试夹具数据特判（硬编码 SKU / 用户 / 订单号走特殊分支）
_SPECIAL_CASE = re.compile(r"\bif\s*\(.*(?:\"SKU-\d+\"|\"u-[\w-]+\"|\"test\w*\").*\)", re.IGNORECASE)


@dataclass
class IntegrityReport:
    violations: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def detail(self) -> str:
        parts = [f"违规: {v}" for v in self.violations] + [f"可疑: {s}" for s in self.suspicious]
        return "; ".join(parts) or "no existing tests modified; no suspicious patterns"


@dataclass
class _FileDiff:
    path: str
    deleted_file: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _parse(diff: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            cur = _FileDiff(m.group(2) if m else line)
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("deleted file mode"):
            cur.deleted_file = True
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _count(pattern: re.Pattern[str], lines: list[str]) -> int:
    return sum(len(pattern.findall(ln)) for ln in lines)


def _swallowing_catches(added: list[str]) -> int:
    """新增的 catch 块里只有空行 / 注释 / 日志（不 throw、不 return、不处理）。"""
    n = 0
    for i, ln in enumerate(added):
        if not _CATCH.search(ln):
            continue
        rest = ln[_CATCH.search(ln).end() :]  # type: ignore[union-attr]
        body: list[str] = [rest] if rest.strip() else []
        for nxt in added[i + 1 : i + 8]:
            if nxt.strip().startswith("}"):
                break
            body.append(nxt)
        else:
            continue  # 块没在新增行里闭合，无法判断
        if all(_LOG_ONLY.match(b) for b in body):
            n += 1
    return n


def _swallowing_excepts(added: list[str]) -> int:
    """Python：except 块第一条语句是 pass / 注释 / 纯日志，视为吞异常。"""
    n = 0
    for i, ln in enumerate(added):
        if not _PY_EXCEPT.match(ln):
            continue
        body = next((nxt for nxt in added[i + 1 : i + 5] if nxt.strip()), "")
        if body and _PY_LOG_ONLY.match(body):
            n += 1
    return n


def check_patch(diff: str, test_globs: list[str]) -> IntegrityReport:
    rep = IntegrityReport()
    for f in _parse(diff):
        if paths.matches(f.path, test_globs):
            if f.deleted_file:
                rep.violations.append(f"删除了测试文件 {f.path}")
                continue
            if _count(_SKIP, f.added) > _count(_SKIP, f.removed):
                rep.violations.append(f"{f.path} 新增了跳过/禁用测试")
            if _count(_TEST_ANNOTATION, f.removed) > _count(_TEST_ANNOTATION, f.added):
                rep.violations.append(f"{f.path} 删除了测试方法")
            if _count(_ASSERT, f.removed) > _count(_ASSERT, f.added):
                rep.violations.append(f"{f.path} 删减了断言")
            elif f.removed and f.added:
                rep.suspicious.append(f"{f.path} 修改了已有测试")
            continue
        if _swallowing_catches(f.added):
            rep.suspicious.append(f"{f.path} 新增了吞掉异常的 catch")
        if _swallowing_excepts(f.added):
            rep.suspicious.append(f"{f.path} 新增了吞掉异常的 except")
        if any(_SPECIAL_CASE.search(ln) for ln in f.added):
            rep.suspicious.append(f"{f.path} 疑似按测试数据特判")
    return rep
