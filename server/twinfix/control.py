"""全局控制面：急停开关。Agent 的每次工具调用前都会检查。"""

from __future__ import annotations

from typing import Protocol

from redis.asyncio import Redis

HALT_KEY = "twinfix:halt"


class ControlPlane(Protocol):
    async def is_halted(self) -> bool: ...
    async def set_halted(self, halted: bool) -> None: ...


class RedisControlPlane:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def is_halted(self) -> bool:
        return bool(await self.redis.exists(HALT_KEY))

    async def set_halted(self, halted: bool) -> None:
        if halted:
            await self.redis.set(HALT_KEY, "1")
        else:
            await self.redis.delete(HALT_KEY)


class MemoryControlPlane:
    def __init__(self) -> None:
        self.halted = False

    async def is_halted(self) -> bool:
        return self.halted

    async def set_halted(self, halted: bool) -> None:
        self.halted = halted
