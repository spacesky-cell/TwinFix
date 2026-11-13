"""在评测机上运行隐藏测试与模块回归。

只在评测机上执行，不经过工具网关：隐藏测试对 TwinFix 不可见，必须在 Agent 的工作区之外跑。
Java 用 Maven（默认本机 `mvn`；`maven_image` 非空时改用 `docker run` 该镜像），
Python 用 pytest（当前解释器；评测镜像需装好 pytest），Go 用 go test，
TypeScript 用 node --test，C# 用 dotnet run 控制台测试工程。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_TEXT_SUFFIXES = {
    ".java",
    ".xml",
    ".yml",
    ".yaml",
    ".sql",
    ".md",
    ".properties",
    ".py",
    ".toml",
    ".cfg",
    ".ini",
    ".go",
    ".mod",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".cs",
    ".csproj",
    ".sln",
    ".slnx",
}


@dataclass(frozen=True)
class MavenResult:
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class Maven:
    offline: bool = False
    maven_image: str | None = None
    m2_volume: str = "twinfix-m2"
    # 容器内评测：指定 settings.xml（镜像源）与本地仓库；宿主机运行时留空即可
    settings: str | None = None
    repo_local: str | None = None
    timeout_s: int = 900

    def command(self, repo: Path, module: str, tests: list[str] | None) -> list[str]:
        args = ["-B", "-q"]
        if self.offline:
            args.insert(0, "-o")
        if self.settings:
            args += ["-s", self.settings]
        if self.repo_local:
            args += [f"-Dmaven.repo.local={self.repo_local}"]
        args += ["-pl", module, "test"]
        if tests:
            args += [f"-Dtest={','.join(tests)}", "-Dsurefire.failIfNoSpecifiedTests=false"]
        if self.maven_image:
            return [
                "docker", "run", "--rm",
                "-v", f"{repo.resolve()}:/w", "-v", f"{self.m2_volume}:/root/.m2", "-w", "/w",
                self.maven_image, "mvn", *args,
            ]  # fmt: skip
        exe = shutil.which("mvn") or shutil.which("mvn.cmd") or "mvn"
        return [exe, *args]

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> MavenResult:
        cmd = self.command(repo, module, tests)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return MavenResult(passed=False, summary=f"timeout after {self.timeout_s}s")
        out = proc.stdout + proc.stderr
        return MavenResult(passed=proc.returncode == 0, summary=summarize_output(out))


def summarize_output(out: str, keep: int = 12) -> str:
    """保留 surefire 的统计行和失败断言，丢弃 Maven 的帮助链接。"""
    picked = [
        ln.rstrip()
        for ln in out.splitlines()
        if re.search(r"Tests run:|FAIL|expected|but was|Exception|ERROR\] +\S+\.\S+:\d+|COMPILATION", ln)
        and "MojoFailureException" not in ln
    ]
    return "\n".join(picked[-keep:])


def normalize_line_endings(root: Path) -> None:
    """Windows 检出可能是 CRLF，而 patch 是 LF；评测前统一成 LF。"""
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in _TEXT_SUFFIXES:
            data = f.read_bytes()
            if b"\r\n" in data:
                f.write_bytes(data.replace(b"\r\n", b"\n"))


@dataclass(frozen=True)
class PytestResult:
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class Pytest:
    """pytest 判分：在仓库根执行，模块目录与测试选择器都相对仓库根。

    `python` 默认当前解释器：控制台评测子进程与 `twinfix-arena` 用的是同一个 venv，
    容器镜像里通过 `uv sync --extra eval` 装上 pytest 即可。宿主机运行时也可传项目自己的解释器。
    """

    python: str = sys.executable
    timeout_s: int = 900

    def command(self, module: str, tests: list[str] | None) -> list[str]:
        cmd = [self.python, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
        return [*cmd, *(tests or [module])]

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> PytestResult:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        # 显式把仓库根放进 sys.path：测试文件在包目录下时，pytest 的 rootdir 推断不一定停在仓库根
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (os.fspath(repo), env.get("PYTHONPATH"))))
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                self.command(module, tests),
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return PytestResult(passed=False, summary=f"timeout after {self.timeout_s}s")
        out = proc.stdout + proc.stderr
        return PytestResult(passed=proc.returncode == 0, summary=summarize_pytest_output(out))


def summarize_pytest_output(out: str, keep: int = 12) -> str:
    """保留失败用例与统计行，丢弃 pytest 的进度点和空行。"""
    picked = [
        ln.rstrip()
        for ln in out.splitlines()
        if re.search(r"FAILED|ERROR|assert|Traceback|passed|failed|error", ln, re.IGNORECASE) and ln.strip()
    ]
    return "\n".join(picked[-keep:])


@dataclass(frozen=True)
class GoResult:
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class Go:
    """Go 判分：默认用本机 go；image 非空时用 docker run 该镜像（本机无 Go / 容器内评测）。

    tests 里的 `./pkg` 走包选择，`TestName` 走 `-run`；None 表示整仓回归 `./...`。
    """

    executable: str = "go"
    image: str | None = None
    timeout_s: int = 900

    def command(self, repo: Path, module: str, tests: list[str] | None) -> list[str]:
        packages: list[str] = []
        names: list[str] = []
        for item in tests or []:
            if _looks_like_go_test_name(item):
                names.append(item)
            else:
                packages.append(item)
        if tests is None:
            packages = ["./..."]
        elif not packages:
            packages = [f"./{module}" if module else "./..."]
        args = ["test", "-count=1"]
        if names:
            args += ["-run", _go_run_pattern(names)]
        args += packages
        if self.image:
            return [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{repo.resolve()}:/w",
                "-w",
                "/w",
                "-e",
                "GOTOOLCHAIN=local",
                "-e",
                "GOCACHE=/tmp/gocache",
                "-e",
                "GOPATH=/tmp/gopath",
                self.image,
                "go",
                *args,
            ]
        return [shutil.which(self.executable) or self.executable, *args]

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> GoResult:
        cmd = self.command(repo, module, tests)
        env = {**os.environ, "GOTOOLCHAIN": "local"}
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return GoResult(passed=False, summary=f"timeout after {self.timeout_s}s")
        except FileNotFoundError:
            return GoResult(
                passed=False,
                summary=f"Go toolchain not found: {cmd[0]}; install Go or pass --go-image",
            )
        out = proc.stdout + proc.stderr
        return GoResult(passed=proc.returncode == 0, summary=summarize_go_output(out))


def _looks_like_go_test_name(item: str) -> bool:
    return re.match(r"^(Test|Benchmark|Example|Fuzz)", item) is not None


def _go_run_pattern(names: list[str]) -> str:
    if len(names) == 1:
        return "/".join(f"^{re.escape(part)}$" for part in names[0].split("/"))
    return "^(?:" + "|".join(re.escape(name) for name in names) + ")$"


def summarize_go_output(out: str, keep: int = 12) -> str:
    """保留 go test 的失败用例与编译错误，丢弃 JSON 事件与无关输出。"""
    picked = [
        ln.rstrip()
        for ln in out.splitlines()
        if re.search(
            r"FAIL|panic:|\.go:\d+:|undefined:|cannot |expected|got |build failed|test timed out|"
            r"Unable to find image|permission denied|docker:",
            ln,
        )
        and not ln.startswith('{"Action"')
    ]
    return "\n".join(picked[-keep:])


def install_go_hidden_tests(repo: Path, module: str, sources: list[Path]) -> list[str]:
    """把隐藏测试拷到模块包目录（Go 测试必须与被测包同目录），返回包选择器。"""
    packages: list[str] = []
    for src in sources:
        if not src.name.endswith("_test.go"):
            raise ValueError(f"{src}: hidden Go test must end with _test.go")
        dest = repo / module / src.name if module else repo / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        packages.append(f"./{module}" if module else "./...")
    return list(dict.fromkeys(packages))


@dataclass(frozen=True)
class NodeResult:
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class Node:
    """TypeScript / JavaScript 判分：默认用本机 node；image 非空时用 docker run 该镜像。

    tests 是相对仓库根的测试文件（`node --test <files>`）；None 表示模块目录回归。
    沙箱与评测镜像都只需 Node 22+：TypeScript 由内置类型剥离直接执行，不依赖 tsc。
    """

    executable: str = "node"
    image: str | None = None
    timeout_s: int = 900

    def command(self, repo: Path, module: str, tests: list[str] | None) -> list[str]:
        # 不能把目录传给 `node --test`（会被当成模块）；不需要指定时用默认测试发现
        args = ["--test", "--test-reporter=spec"]
        if tests:
            args += tests
        if self.image:
            return [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{repo.resolve()}:/w",
                "-w",
                "/w",
                self.image,
                "node",
                *args,
            ]
        return [shutil.which(self.executable) or self.executable, *args]

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> NodeResult:
        cmd = self.command(repo, module, tests)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return NodeResult(passed=False, summary=f"timeout after {self.timeout_s}s")
        except FileNotFoundError:
            return NodeResult(
                passed=False,
                summary=f"Node.js not found: {cmd[0]}; install Node 22+ or pass --node-image",
            )
        out = proc.stdout + proc.stderr
        return NodeResult(passed=proc.returncode == 0, summary=summarize_node_output(out))


def summarize_node_output(out: str, keep: int = 12) -> str:
    """保留 node --test 的失败用例与断言，丢弃进度点与 TAP 头。"""
    picked = [
        ln.rstrip()
        for ln in out.splitlines()
        if re.search(
            r"not ok|# (?:fail|pass)|Error|assert|expected|actual|SyntaxError|TypeError|ReferenceError|"
            r"Cannot find|ERR_|\.(?:ts|js):\d+",
            ln,
        )
        and not ln.startswith(("TAP version", "1.."))
    ]
    return "\n".join(picked[-keep:])


def install_node_hidden_tests(repo: Path, module: str, sources: list[Path]) -> list[str]:
    """把隐藏测试拷进模块的 tests 目录，返回相对仓库根的测试文件路径。

    Node 的测试发现要求文件名匹配 `*.test.*` / `*.spec.*`；与 pytest 一样放在
    `<module>/tests/`，用 `node --test <file>` 精确执行。
    """
    names: list[str] = []
    for src in sources:
        if not re.search(r"\.(?:test|spec)\.(?:[cm]?[jt]sx?)$", src.name):
            raise ValueError(f"{src}: hidden Node test must match *.test.ts / *.spec.ts")
        dest = repo / module / "tests" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        names.append(f"{module}/tests/{src.name}" if module else f"tests/{src.name}")
    return names


def install_pytest_hidden_tests(repo: Path, module: str, sources: list[Path]) -> list[str]:
    """把隐藏测试拷进模块的 tests 目录，返回相对仓库根的 pytest 选择器。

    Python 的隐藏测试与 Java 不同，不需要 package 声明：直接按文件名放进 `tests/`，
    由 pytest 收集。返回 `orders/tests/test_x_hidden.py` 这样的路径，在仓库根执行。
    """
    names = []
    for src in sources:
        dest = repo / module / "tests" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        names.append(f"{module}/tests/{src.name}")
    return names


@dataclass(frozen=True)
class CSharpResult:
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class CSharp:
    """C# / .NET 判分：默认用本机 dotnet；image 非空时用 docker run 该镜像。

    测试工程约定：目录/工程名以 ``.Tests`` 结尾的控制台工程，工程内自带反射 harness
    （运行 ``Test*`` 静态方法并写 JUnit）。零第三方依赖，离线也能 restore/build。
    tests 是 harness 的过滤参数（隐藏测试类名）；None 表示整仓回归。
    """

    executable: str = "dotnet"
    image: str | None = None
    timeout_s: int = 900

    def command(self, repo: Path, module: str, tests: list[str] | None) -> list[str]:
        project = csharp_test_project(repo, module)
        relative = project.relative_to(repo).as_posix()
        junit = ".twinfix/csharp-tests.xml"
        # 构建产物重定向到仓库外：避免在只读/共享仓库里生成 bin/obj，
        # 也避免 Windows 上 Docker(root) 写出的文件让宿主机无法清理临时目录。
        artifacts = (
            "/tmp/twinfix-dotnet"  # noqa: S108 - 容器内路径，非宿主机临时目录
            if self.image
            else str(Path(tempfile.gettempdir()) / "twinfix-dotnet")
        )
        if self.image:
            args = [
                "run",
                "--artifacts-path",
                artifacts,
                "--project",
                relative,
                "--",
                "--junit",
                f"/w/{junit}",
                *(tests or []),
            ]
            return [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{repo.resolve()}:/w",
                "-w",
                "/w",
                "-e",
                "HOME=/tmp",
                "-e",
                "DOTNET_CLI_TELEMETRY_OPTOUT=1",
                "-e",
                "DOTNET_NOLOGO=1",
                self.image,
                "dotnet",
                *args,
            ]
        args = [
            "run",
            "--artifacts-path",
            artifacts,
            "--project",
            str(project),
            "--",
            "--junit",
            str(repo / junit),
            *(tests or []),
        ]
        return [shutil.which(self.executable) or self.executable, *args]

    async def test(self, repo: Path, module: str, tests: list[str] | None = None) -> CSharpResult:
        (repo / ".twinfix").mkdir(parents=True, exist_ok=True)
        cmd = self.command(repo, module, tests)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            return CSharpResult(passed=False, summary=f"timeout after {self.timeout_s}s")
        except FileNotFoundError:
            return CSharpResult(
                passed=False,
                summary=f"dotnet not found: {cmd[0]}; install .NET SDK or pass --csharp-image",
            )
        out = proc.stdout + proc.stderr
        return CSharpResult(passed=proc.returncode == 0, summary=summarize_csharp_output(out))

    def _env(self) -> dict[str, str]:
        """.NET 首次运行会写 HOME 下的 .dotnet 与 NuGet 迁移目录。

        API 容器里的 twinfix 用户 HOME=/app 不可写，直接跑 dotnet 会报
        `Access to the path '/app/.dotnet' is denied`。判分时单独把 HOME 指到可写的临时目录。
        """
        env = {**os.environ}
        if self.image is None and os.name == "posix":
            home = Path(tempfile.gettempdir()) / "twinfix-dotnet-home"
            home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(home)
            env.setdefault("DOTNET_CLI_HOME", str(home))
        return env


def csharp_test_project(repo: Path, module: str = "") -> Path:
    """按约定找到测试工程：优先 <module>.Tests，其次任意 *.Tests/*.Test 工程。"""
    if module:
        wanted = {f"{module.lower()}.tests", f"{module.lower()}.test"}
        for directory in sorted(p for p in repo.iterdir() if p.is_dir()):
            if directory.name.lower() in wanted:
                projects = sorted(directory.glob("*.csproj"))
                if projects:
                    return projects[0]
    candidates = [p for p in sorted(repo.rglob("*.csproj")) if not ({"bin", "obj"} & set(p.parts))]
    if not candidates:
        raise FileNotFoundError(f"no .csproj under {repo}")
    tests = [p for p in candidates if p.stem.endswith((".Tests", ".Test"))]
    return (tests or candidates)[0]


def summarize_csharp_output(out: str, keep: int = 12) -> str:
    """保留 .NET 的编译错误与失败用例，丢弃 MSBuild 的进度行。"""
    picked = [
        ln.rstrip()
        for ln in out.splitlines()
        if re.search(
            r"error CS|Build FAILED|Unhandled exception|\bFAIL\b|\bFAILED\b|Assert|Expected|Actual|"
            r"MSBUILD : error|dotnet:",
            ln,
        )
        and not ln.strip().startswith(("Determining projects", "Restored ", "  "))
    ]
    return "\n".join(picked[-keep:])


def install_csharp_hidden_tests(repo: Path, module: str, sources: list[Path]) -> list[str]:
    """把隐藏测试拷进测试工程目录（与现有测试一起编译），返回测试类名过滤参数。"""
    target = csharp_test_project(repo, module).parent
    names: list[str] = []
    for src in sources:
        if not src.name.endswith(".cs"):
            raise ValueError(f"{src}: hidden C# test must end with .cs")
        dest = target / src.name
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        match = re.search(r"\b(?:class|record)\s+(\w+)", src.read_text(encoding="utf-8"))
        names.append(match.group(1) if match else src.stem)
    return names


def install_hidden_tests(repo: Path, module: str, sources: list[Path]) -> list[str]:
    """按 package 声明把隐藏测试拷进模块测试目录，返回测试类名（供 -Dtest 使用）。"""
    names = []
    for src in sources:
        text = src.read_text(encoding="utf-8")
        m = re.search(r"^package\s+([\w.]+);", text, re.MULTILINE)
        if not m:
            raise ValueError(f"{src}: no package declaration")
        dest = repo / module / "src" / "test" / "java" / Path(*m.group(1).split(".")) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
        names.append(src.stem)
    return names
