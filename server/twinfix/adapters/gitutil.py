"""git 子进程封装。所有调用都显式传 argv、固定身份与配置，不依赖宿主机的 git 全局配置。"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

BOT_NAME = "twinfix[bot]"
BOT_EMAIL = "twinfix-bot@users.noreply.local"

# 固定的 git 环境：不读全局/系统配置，不弹交互
_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": BOT_NAME,
    "GIT_AUTHOR_EMAIL": BOT_EMAIL,
    "GIT_COMMITTER_NAME": BOT_NAME,
    "GIT_COMMITTER_EMAIL": BOT_EMAIL,
}

# hooks 是仓库作者的代码，不在 TwinFix 进程里执行；换行原样保留
_FLAGS = ["-c", f"core.hooksPath={os.devnull}", "-c", "core.autocrlf=false", "-c", "advice.detachedHead=false"]


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {**os.environ, **_ENV, **(extra or {})}


def config_env(entries: dict[str, str]) -> dict[str, str]:
    """把 git 配置项通过环境变量传入（GIT_CONFIG_COUNT），凭据不出现在命令行参数里。"""
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for i, (k, v) in enumerate(entries.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def git_argv(args: list[str]) -> list[str]:
    return ["git", *_FLAGS, *args]


class GitError(RuntimeError):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args[:3])} failed ({code}): {stderr.strip()[:2000]}")
        self.code = code
        self.stderr = stderr


def _run(
    args: list[str], cwd: Path | None, check: bool, input_bytes: bytes | None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(  # noqa: S603 - argv list, no shell
        git_argv(args),
        cwd=cwd,
        env=git_env(env),
        capture_output=True,
        input=input_bytes,
        timeout=300,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc


async def git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    data = input_text.encode("utf-8") if input_text is not None else None
    proc = await asyncio.to_thread(_run, list(args), cwd, check, data, env)
    return proc.stdout.decode("utf-8", "replace").strip()


async def git_bytes(*args: str, cwd: Path | None = None) -> bytes:
    proc = await asyncio.to_thread(_run, list(args), cwd, True, None)
    return proc.stdout


async def git_ok(*args: str, cwd: Path | None = None) -> bool:
    proc = await asyncio.to_thread(_run, list(args), cwd, False, None)
    return proc.returncode == 0
