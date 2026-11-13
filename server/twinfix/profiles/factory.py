"""按项目语言选择 LanguageProfile。"""

from __future__ import annotations

from typing import Any

from twinfix.profiles.base import LanguageProfile
from twinfix.profiles.commands import CommandOverrides
from twinfix.profiles.csharp import CSharpProfile
from twinfix.profiles.go import GoProfile
from twinfix.profiles.java import JavaProfile
from twinfix.profiles.python import PythonProfile
from twinfix.profiles.typescript import TypeScriptProfile

_ALIASES = {
    "java": "java",
    "kotlin": "java",
    "python": "python",
    "py": "python",
    "go": "go",
    "golang": "go",
    "typescript": "typescript",
    "ts": "typescript",
    "javascript": "typescript",
    "js": "typescript",
    "node": "typescript",
    "nodejs": "typescript",
    "csharp": "csharp",
    "c#": "csharp",
    "cs": "csharp",
    "dotnet": "csharp",
    ".net": "csharp",
    "net": "csharp",
}


def normalize_language(language: str | None) -> str:
    return _ALIASES.get((language or "java").strip().lower(), (language or "java").strip().lower())


def get_profile(
    language: str | None,
    project_packages: tuple[str, ...] = (),
    python_executable: str = "python",
    commands: dict[str, Any] | None = None,
) -> LanguageProfile:
    normalized = normalize_language(language)
    overrides = CommandOverrides.parse(commands)
    if normalized == "java":
        return JavaProfile(project_packages, overrides)
    if normalized == "python":
        return PythonProfile(project_packages, python_executable, overrides)
    if normalized == "go":
        return GoProfile(project_packages, overrides)
    if normalized == "typescript":
        return TypeScriptProfile(project_packages, commands=overrides)
    if normalized == "csharp":
        return CSharpProfile(project_packages, commands=overrides)
    raise ValueError(f"unsupported language {language!r}; supported: java, python, go, typescript, csharp")
