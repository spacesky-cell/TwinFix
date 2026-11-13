"""代码智能：符号索引、引用查询与索引缓存。

索引按（工作区, 提交, 语言）缓存；写文件后显式失效。索引是派生数据，只存在进程内。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict

from twinfix.adapters.sandbox import SandboxProvider
from twinfix.adapters.types import Workspace
from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.csharp_index import CSharpCodeIndex, build_csharp_index
from twinfix.codeintel.go_index import GoCodeIndex, build_go_index
from twinfix.codeintel.index import JavaCodeIndex, build_java_index
from twinfix.codeintel.model import Edge, IndexMeta, Reference, Symbol
from twinfix.codeintel.python_index import PythonCodeIndex, build_python_index
from twinfix.codeintel.typescript_index import TypeScriptCodeIndex, build_typescript_index

__all__ = [
    "CSharpCodeIndex",
    "CodeIndex",
    "Edge",
    "GoCodeIndex",
    "IndexMeta",
    "JavaCodeIndex",
    "PythonCodeIndex",
    "Reference",
    "Symbol",
    "TypeScriptCodeIndex",
    "build_csharp_index",
    "build_go_index",
    "build_java_index",
    "build_python_index",
    "build_typescript_index",
    "get_index",
    "invalidate",
]

_MAX_CACHED_INDEXES = 4
_SUFFIXES = {
    "java": (".java",),
    "python": (".py",),
    "go": (".go",),
    "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "csharp": (".cs",),
}
_EXCLUDES = {
    "java": (".git", "target", "node_modules"),
    "python": (".git", "target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"),
    "go": (".git", "target", "node_modules", "vendor", "testdata", ".cache"),
    "typescript": (
        ".git",
        "target",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
    ),
    "csharp": (".git", "bin", "obj", ".vs", "TestResults", "packages"),
}
_cache: OrderedDict[str, CodeIndex] = OrderedDict()
_locks: dict[str, asyncio.Lock] = {}


def _key(ws: Workspace, language: str) -> str:
    return f"{ws.id}:{ws.sha}:{language}"


def _normalize(language: str | None) -> str:
    normalized = (language or "java").lower()
    if normalized in ("python", "py"):
        return "python"
    if normalized in ("go", "golang"):
        return "go"
    if normalized in ("typescript", "ts", "javascript", "js", "node", "nodejs"):
        return "typescript"
    if normalized in ("csharp", "c#", "cs", "dotnet", ".net", "net"):
        return "csharp"
    return "java"


async def get_index(sandbox: SandboxProvider, ws: Workspace, language: str = "java") -> CodeIndex:
    normalized = _normalize(language)
    key = _key(ws, normalized)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
        files, truncated = await sandbox.read_sources(
            ws, suffixes=_SUFFIXES[normalized], exclude_dirs=_EXCLUDES[normalized]
        )
        if normalized == "go":
            # go.mod 决定跨包导入路径（example.com/shop/orders）；read_sources 只收源码后缀
            with contextlib.suppress(FileNotFoundError):
                files = {"go.mod": await sandbox.read_file(ws, "go.mod"), **files}
        if normalized == "python":
            index: CodeIndex = await asyncio.to_thread(build_python_index, files, truncated)
        elif normalized == "go":
            index = await asyncio.to_thread(build_go_index, files, truncated)
        elif normalized == "typescript":
            index = await asyncio.to_thread(build_typescript_index, files, truncated)
        elif normalized == "csharp":
            index = await asyncio.to_thread(build_csharp_index, files, truncated)
        else:
            index = await asyncio.to_thread(build_java_index, files, truncated)
        _cache[key] = index
        while len(_cache) > _MAX_CACHED_INDEXES:
            _cache.popitem(last=False)
        return index


def invalidate(workspace_id: str) -> None:
    for key in [k for k in _cache if k.split(":", 1)[0] == workspace_id]:
        _cache.pop(key, None)
    for key in [k for k in _locks if k.split(":", 1)[0] == workspace_id]:
        _locks.pop(key, None)
