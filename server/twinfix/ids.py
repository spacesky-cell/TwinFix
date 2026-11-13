"""ULID：按时间有序、26 位 Crockford Base32，作为全部实体主键。"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    ts = int(time.time() * 1000)
    rnd = int.from_bytes(os.urandom(10), "big")
    value = (ts << 80) | rnd
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def short_id(ulid: str) -> str:
    """分支名等场景用的短 id：取随机部分末 8 位，避免同毫秒创建的事故冲突。"""
    return ulid[-8:].lower()
