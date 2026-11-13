"""脱敏：写审计、发往 LLM 之前统一调用。规则保守，宁可多遮不可漏遮。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[PRIVATE_KEY]",
    ),
    ("jwt", re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b"), "[JWT]"),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"), "[TOKEN]"),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[TOKEN]"),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[TOKEN]"),
    (
        "secret_kv",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b(\s*[:=]\s*)(\"?)[^\s\",;]+"),
        r"\1\2\3[REDACTED]",
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[EMAIL]"),
    # 身份证要在银行卡、手机号之前：18 位且末位可为 X
    (
        "cn_id",
        re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
        "[ID_CARD]",
    ),
    ("bank_card", re.compile(r"(?<!\d)(?:62|4\d|5[1-5])\d{14,17}(?!\d)"), "[BANK_CARD]"),
    ("cn_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
]

_SECRET_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)")


class Redactor:
    def __init__(self, extra_patterns: Iterable[str] = ()) -> None:
        self.rules = list(_RULES) + [(f"custom_{i}", re.compile(p), "[REDACTED]") for i, p in enumerate(extra_patterns)]

    def text(self, value: str) -> str:
        for _, pattern, repl in self.rules:
            value = pattern.sub(repl, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEYS.search(k) else self.value(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value


default_redactor = Redactor()
