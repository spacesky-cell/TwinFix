"""路径规则：`**` 匹配任意层级目录，`*` 匹配单层内任意字符。"""

from __future__ import annotations

import posixpath
import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def normalize(path: str) -> str:
    """统一为工作区内的相对 posix 路径；`..` 逃逸由沙箱再做一次物理校验。"""
    p = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    return "" if p == "." else p


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    p = normalize(path)
    return any(_compile(pat).match(p) for pat in patterns)


def escapes(path: str) -> bool:
    p = posixpath.normpath(path.replace("\\", "/"))
    return p.startswith("/") or p == ".." or p.startswith("../") or bool(re.match(r"^[A-Za-z]:", p))
