"""Arthas 只读探测：compose 与 K8s 孪生共用的常量与输出清洗。

孪生镜像以 premain（``-javaagent``）挂载 Arthas，telnet 只绑容器内 127.0.0.1:3658；
TwinFix 在容器/ Pod 内执行一条与白名单逐字相等的命令，输出里去掉启动横幅、命令回显与提示符。
"""

from __future__ import annotations

import re

ARTHAS_CLIENT_JAR = "/opt/arthas/arthas-client.jar"
ARTHAS_TELNET_PORT = 3658

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ECHO = re.compile(r"^\[arthas@\d+\]\$ \S")  # 回显行有命令；空提示符（启动横幅后那条）不算
_PROMPT = re.compile(r"^\[arthas@\d+\]\$ ?$")


def telnet_command(command: str, timeout_s: int) -> list[str]:
    """容器内执行 Arthas telnet 客户端的 argv（不含 docker exec / kubectl exec 前缀）。"""
    return [
        "java",
        "-cp",
        ARTHAS_CLIENT_JAR,
        "com.taobao.arthas.client.TelnetConsole",
        "-t",
        str(timeout_s * 1000),
        "-c",
        command,
        "127.0.0.1",
        str(ARTHAS_TELNET_PORT),
    ]


def clean_telnet(text: str) -> str:
    """telnet 客户端输出里有 CRLF、ANSI 转义、启动横幅与命令回显；只留命令结果。"""
    lines = _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    start = next((i + 1 for i, ln in enumerate(lines) if _ECHO.match(ln)), 0)
    return "\n".join(ln for ln in lines[start:] if not _PROMPT.match(ln)).strip("\n")


def check_probe_output(returncode: int, output: str, command: str) -> str:
    """统一的失败判定：命令不存在时 Arthas 客户端仍可能返回 0，不能只看退出码。"""
    if returncode != 0 or "Connect to telnet server error" in output:
        raise RuntimeError(f"arthas probe failed (exit {returncode}): {output[-2000:]}")
    if "command not found" in output:
        raise RuntimeError(f"arthas rejected command {command!r}: {output[-1000:]}")
    return output
