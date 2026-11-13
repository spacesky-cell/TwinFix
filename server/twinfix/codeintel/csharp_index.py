"""C# 跨文件符号解析：namespace/using、同类与基类调用、继承与接口实现、类型引用。"""

from __future__ import annotations

import re
import time

from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.csharp import CsCall, CsParsed, CsUsing, base_type_name, parse_csharp
from twinfix.codeintel.model import Edge, IndexMeta, Symbol


class CSharpCodeIndex(CodeIndex):
    pass


def build_csharp_index(files: dict[str, str], truncated: bool = False) -> CSharpCodeIndex:
    started = time.monotonic()
    parsed = [parse_csharp(path, text) for path, text in sorted(files.items())]
    symbols: dict[str, Symbol] = {}
    for item in parsed:
        for symbol in item.symbols:
            symbols.setdefault(symbol.id, symbol)

    types = {s.id: s for s in symbols.values() if s.is_type}
    by_qualified: dict[str, list[Symbol]] = {}
    by_name: dict[str, list[Symbol]] = {}
    methods_by_type: dict[str, list[Symbol]] = {}
    for symbol in symbols.values():
        by_qualified.setdefault(symbol.qualified_name, []).append(symbol)
        by_name.setdefault(symbol.name, []).append(symbol)
        if symbol.kind in ("method", "constructor") and symbol.container:
            methods_by_type.setdefault(symbol.container, []).append(symbol)
    namespace_by_path = {item.path: _file_namespace(item) for item in parsed}
    usings_by_path = {item.path: item.usings for item in parsed}
    resolver = _CsResolver(types, by_qualified, by_name, methods_by_type, usings_by_path, namespace_by_path)

    edges: list[Edge] = []
    for item in parsed:
        for type_id, bases in item.bases.items():
            for base in bases:
                target = resolver.resolve_type(base, item.path, container=None)
                if target is None or target.id == type_id:
                    continue
                line = symbols[type_id].start_line if type_id in symbols else 1
                kind = "implements" if target.kind == "interface" else "extends"
                edges.append(Edge(type_id, target.id, kind, item.path, line, target.name))
                if kind == "extends":
                    resolver.parent_by_type.setdefault(type_id, []).append(target.id)

    for item in parsed:
        for ref in item.refs:
            source = symbols.get(ref.src)
            container = source.container if source is not None else None
            target = resolver.resolve_type(ref.name, item.path, container=container)
            if target is None or target.id == ref.src:
                continue
            edges.append(Edge(ref.src, target.id, ref.kind, item.path, ref.line, target.name))
        for call in item.calls:
            edge = resolver.resolve_call(call, item.path, symbols)
            if edge is not None:
                edges.append(edge)

    deduped: dict[tuple[str, str | None, str, int], Edge] = {}
    for edge in edges:
        deduped.setdefault((edge.src, edge.dst, edge.kind, edge.line), edge)
    ordered = sorted(deduped.values(), key=lambda e: (e.path, e.line, e.src, e.kind))
    meta = IndexMeta(
        files=len(files),
        symbols=len(symbols),
        edges=len(ordered),
        truncated=truncated,
        build_ms=int((time.monotonic() - started) * 1000),
    )
    return CSharpCodeIndex(symbols, ordered, meta)


def _file_namespace(parsed: CsParsed) -> str:
    if parsed.namespace:
        return parsed.namespace
    for symbol in parsed.symbols:
        if symbol.package:
            return symbol.package
    return ""


class _CsResolver:
    def __init__(
        self,
        types: dict[str, Symbol],
        by_qualified: dict[str, list[Symbol]],
        by_name: dict[str, list[Symbol]],
        methods_by_type: dict[str, list[Symbol]],
        usings_by_path: dict[str, list[CsUsing]],
        namespace_by_path: dict[str, str],
    ) -> None:
        self.types = types
        self.by_qualified = by_qualified
        self.by_name = by_name
        self.methods_by_type = methods_by_type
        self.usings_by_path = usings_by_path
        self.namespace_by_path = namespace_by_path
        self.parent_by_type: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 类型

    def resolve_type(self, name: str | None, path: str, *, container: str | None) -> Symbol | None:
        cleaned = _clean_type_name(name)
        if not cleaned:
            return None
        if cleaned in self.types:
            return self.types[cleaned]
        namespace = self.namespace_by_path.get(path, "")
        candidates: list[str] = []
        if container:
            candidates.append(f"{container}.{cleaned}")
        if namespace:
            candidates.append(f"{namespace}.{cleaned}")
        for using in self.usings_by_path.get(path, []):
            if using.is_static:
                continue
            if using.alias:
                if cleaned == using.alias:
                    candidates.append(using.target)
                elif cleaned.startswith(f"{using.alias}."):
                    candidates.append(f"{using.target}.{cleaned[len(using.alias) + 1 :]}")
            else:
                candidates.append(f"{using.target}.{cleaned}")
        for candidate in candidates:
            if candidate in self.types:
                return self.types[candidate]
        if "." in cleaned:
            suffix = [s for s in self.types.values() if s.qualified_name.endswith(f".{cleaned}")]
            return suffix[0] if len(suffix) == 1 else None
        matches = [s for s in self.types.values() if s.name == cleaned]
        return matches[0] if len(matches) == 1 else None

    # ---------------------------------------------------------------- 调用

    def resolve_call(self, call: CsCall, path: str, symbols: dict[str, Symbol]) -> Edge | None:
        source = symbols.get(call.src)
        container = source.container if source is not None else None
        if call.is_new:
            target = self.resolve_type(call.name, path, container=container)
            return Edge(
                call.src,
                target.id if target is not None else None,
                "constructs",
                path,
                call.line,
                target.name if target is not None else call.name,
                call.arity,
            )
        receiver = call.receiver
        if receiver:
            if receiver in ("this", "base") and container:
                method = self._method(
                    container,
                    call.name,
                    call.arity,
                    include_parents=True,
                    parents_only=receiver == "base",
                )
                return Edge(
                    call.src,
                    method.id if method is not None else None,
                    "calls",
                    path,
                    call.line,
                    method.name if method is not None else call.name,
                    call.arity,
                    receiver,
                )
            receiver_type = call.variables.get(receiver)
            if receiver_type is None and (call.receiver_is_call or receiver[:1].isupper()):
                receiver_type = receiver
            target = self.resolve_type(receiver_type, path, container=container) if receiver_type else None
            if target is not None:
                method = self._method(target.id, call.name, call.arity)
                if method is not None:
                    return Edge(call.src, method.id, "calls", path, call.line, method.name, call.arity, receiver)
            return Edge(call.src, None, "calls", path, call.line, call.name, call.arity, receiver)
        if container:
            method = self._method(container, call.name, call.arity, include_parents=True)
            if method is not None:
                return Edge(call.src, method.id, "calls", path, call.line, method.name, call.arity)
        matches = [
            s
            for s in self.by_name.get(call.name, [])
            if s.kind in ("function", "method") and (call.arity is None or s.arity == call.arity)
        ]
        if len(matches) == 1:
            return Edge(call.src, matches[0].id, "calls", path, call.line, matches[0].name, matches[0].arity)
        return Edge(call.src, None, "calls", path, call.line, call.name, call.arity)

    def _method(
        self,
        type_id: str,
        name: str,
        arity: int | None,
        *,
        include_parents: bool = False,
        parents_only: bool = False,
    ) -> Symbol | None:
        seen: set[str] = set()
        # this / 无接收者：先查自身再走父类；base：只查父类
        queue = list(self.parent_by_type.get(type_id, [])) if (include_parents or parents_only) else [type_id]
        if include_parents and not parents_only and type_id not in queue:
            queue.append(type_id)
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for method in self.methods_by_type.get(current, []):
                if method.name == name and (arity is None or method.arity == arity):
                    return method
            queue.extend(self.parent_by_type.get(current, []))
        return None


def _clean_type_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip().removeprefix("global::").strip()
    while re.search(r"<[^<>]*>", cleaned):
        cleaned = re.sub(r"<[^<>]*>", "", cleaned)
    cleaned = cleaned.replace("?", "").replace("[]", "").replace("()", "").strip()
    cleaned = cleaned.rstrip("*").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def base_name(type_name: str | None) -> str | None:
    return base_type_name(type_name)
