"""TypeScript / JavaScript 跨文件符号解析：相对 import、同类/同模块调用、继承与类型引用。"""

from __future__ import annotations

import time

from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.model import Edge, IndexMeta, Symbol
from twinfix.codeintel.typescript import (
    TsCall,
    TsImport,
    base_type_name,
    parse_typescript,
    relative_specifier_target,
)

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class TypeScriptCodeIndex(CodeIndex):
    pass


def build_typescript_index(files: dict[str, str], truncated: bool = False) -> TypeScriptCodeIndex:
    started = time.monotonic()
    parsed = [parse_typescript(path, text) for path, text in sorted(files.items())]
    symbols: dict[str, Symbol] = {}
    for item in parsed:
        for symbol in item.symbols:
            symbols.setdefault(symbol.id, symbol)

    types = {s.id: s for s in symbols.values() if s.is_type}
    by_qualified: dict[str, list[Symbol]] = {}
    by_name: dict[str, list[Symbol]] = {}
    methods_by_class: dict[str, list[Symbol]] = {}
    functions_by_module: dict[str, list[Symbol]] = {}
    module_by_path = {item.path: item.module for item in parsed}
    path_by_module = {item.module: item.path for item in parsed}
    imports_by_path = {item.path: item.imports for item in parsed}
    for symbol in symbols.values():
        by_qualified.setdefault(symbol.qualified_name, []).append(symbol)
        by_name.setdefault(symbol.name, []).append(symbol)
        if symbol.kind in ("method", "constructor") and symbol.container:
            methods_by_class.setdefault(symbol.container, []).append(symbol)
        elif symbol.kind == "function":
            functions_by_module.setdefault(symbol.package, []).append(symbol)

    resolver = _TsResolver(
        types,
        by_qualified,
        by_name,
        methods_by_class,
        functions_by_module,
        imports_by_path,
        module_by_path,
        path_by_module,
    )
    edges: list[Edge] = []
    for item in parsed:
        for class_id, bases in item.extends.items():
            for base in bases:
                target = resolver.resolve_type(base, item.path, container=None)
                if target is not None and target.id != class_id:
                    line = symbols[class_id].start_line if class_id in symbols else 1
                    edges.append(Edge(class_id, target.id, "extends", item.path, line, target.name))
                    resolver.parent_by_type.setdefault(class_id, []).append(target.id)
        for class_id, interfaces in item.implements.items():
            for interface in interfaces:
                target = resolver.resolve_type(interface, item.path, container=None)
                if target is not None and target.id != class_id:
                    line = symbols[class_id].start_line if class_id in symbols else 1
                    edges.append(Edge(class_id, target.id, "implements", item.path, line, target.name))

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
    return TypeScriptCodeIndex(symbols, ordered, meta)


class _TsResolver:
    def __init__(
        self,
        types: dict[str, Symbol],
        by_qualified: dict[str, list[Symbol]],
        by_name: dict[str, list[Symbol]],
        methods_by_class: dict[str, list[Symbol]],
        functions_by_module: dict[str, list[Symbol]],
        imports_by_path: dict[str, list[TsImport]],
        module_by_path: dict[str, str],
        path_by_module: dict[str, str],
    ) -> None:
        self.types = types
        self.by_qualified = by_qualified
        self.by_name = by_name
        self.methods_by_class = methods_by_class
        self.functions_by_module = functions_by_module
        self.imports_by_path = imports_by_path
        self.module_by_path = module_by_path
        self.path_by_module = path_by_module
        self.parent_by_type: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 模块

    def resolve_module(self, specifier: str, path: str) -> str | None:
        module = self.module_by_path.get(path, "")
        target = relative_specifier_target(module, specifier)
        if target is None:
            return None
        for suffix in _SOURCE_SUFFIXES:
            if target.endswith(suffix):
                target = target[: -len(suffix)]
                break
        if target in self.path_by_module:
            return target
        with_index = f"{target}.index"
        return with_index if with_index in self.path_by_module else None

    # ---------------------------------------------------------------- 类型

    def resolve_type(self, name: str | None, path: str, *, container: str | None) -> Symbol | None:
        if not name:
            return None
        cleaned = name.strip().removeprefix("typeof ").strip()
        if not cleaned:
            return None
        if "." in cleaned:
            if cleaned in self.types:
                return self.types[cleaned]
            root, rest = cleaned.split(".", 1)
            for symbol in self._imported_symbols(root, path, rest):
                if symbol.is_type:
                    return symbol
            suffix = [s for s in self.types.values() if s.qualified_name.endswith(f".{cleaned}")]
            return suffix[0] if len(suffix) == 1 else None
        if container:
            nested = self.types.get(f"{container}.{cleaned}")
            if nested is not None:
                return nested
        module = self.module_by_path.get(path, "")
        same_module = self.types.get(f"{module}.{cleaned}" if module else cleaned)
        if same_module is not None:
            return same_module
        for symbol in self._imported_symbols(cleaned, path, ""):
            if symbol.is_type:
                return symbol
        matches = [s for s in self.types.values() if s.name == cleaned]
        return matches[0] if len(matches) == 1 else None

    def _imported_symbols(self, local: str, path: str, rest: str) -> list[Symbol]:
        found: list[Symbol] = []
        for item in self.imports_by_path.get(path, []):
            if item.local != local:
                continue
            module = self.resolve_module(item.specifier, path)
            if module is None:
                continue
            if item.imported == "*":
                if rest:
                    symbol = self.by_qualified.get(f"{module}.{rest}")
                    found.extend(symbol or [])
            elif item.imported == "default":
                candidate = self.types.get(f"{module}.{local}")
                if candidate is not None and not rest:
                    found.append(candidate)
                elif rest:
                    found.extend(self.by_qualified.get(f"{module}.{rest}", []))
            else:
                target = f"{module}.{item.imported}"
                if rest:
                    target = f"{target}.{rest}"
                found.extend(self.by_qualified.get(target, []))
        return found

    # ---------------------------------------------------------------- 调用

    def resolve_call(self, call: TsCall, path: str, symbols: dict[str, Symbol]) -> Edge | None:
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
            if receiver in ("this", "super") and container:
                method = self._method(container, call.name, call.arity, include_super=receiver == "super")
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
            receiver_type = call.variables.get(receiver) or (receiver if receiver[:1].isupper() else None)
            if receiver_type is None and call.receiver_is_call:
                receiver_type = receiver
            target = self.resolve_type(receiver_type, path, container=container) if receiver_type else None
            if target is not None:
                method = self._method(target.id, call.name, call.arity)
                if method is not None:
                    return Edge(call.src, method.id, "calls", path, call.line, method.name, call.arity, receiver)
            function = self._imported_function(receiver, call.name, call.arity, path)
            if function is not None:
                return Edge(call.src, function.id, "calls", path, call.line, function.name, function.arity, receiver)
            return Edge(call.src, None, "calls", path, call.line, call.name, call.arity, receiver)
        module = self.module_by_path.get(path, "")
        function = self._function_in_module(module, call.name, call.arity)
        if function is not None:
            return Edge(call.src, function.id, "calls", path, call.line, function.name, function.arity)
        imported = self._imported_function("", call.name, call.arity, path)
        if imported is not None:
            return Edge(call.src, imported.id, "calls", path, call.line, imported.name, imported.arity)
        matches = [
            s for s in self.by_name.get(call.name, []) if s.kind in ("function", "method") and s.arity == call.arity
        ]
        if len(matches) == 1:
            return Edge(call.src, matches[0].id, "calls", path, call.line, matches[0].name, matches[0].arity)
        return Edge(call.src, None, "calls", path, call.line, call.name, call.arity)

    def _method(self, class_id: str, name: str, arity: int | None, *, include_super: bool = False) -> Symbol | None:
        seen: set[str] = set()
        queue = list(self.parent_by_type.get(class_id, [])) if include_super else [class_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for method in self.methods_by_class.get(current, []):
                if method.name == name and (arity is None or method.arity == arity):
                    return method
            queue.extend(self.parent_by_type.get(current, []))
        matches = [m for m in self.methods_by_class.get(class_id, []) if m.name == name]
        return matches[0] if len(matches) == 1 else None

    def _function_in_module(self, module: str, name: str, arity: int | None) -> Symbol | None:
        matches = [f for f in self.functions_by_module.get(module, []) if f.name == name]
        exact = [f for f in matches if arity is None or f.arity == arity]
        if len(exact) == 1:
            return exact[0]
        return matches[0] if len(matches) == 1 else None

    def _imported_function(self, namespace: str, name: str, arity: int | None, path: str) -> Symbol | None:
        for item in self.imports_by_path.get(path, []):
            if namespace and item.local != namespace:
                continue
            if not namespace and item.imported in ("*", "default"):
                continue
            module = self.resolve_module(item.specifier, path)
            if module is None:
                continue
            if namespace:
                function = self._function_in_module(module, name, arity)
                if function is not None:
                    return function
                continue
            symbol = self.by_qualified.get(f"{module}.{item.imported}")
            for candidate in symbol or []:
                if candidate.kind in ("function", "method") and candidate.arity == arity:
                    return candidate
        return None


def base_name(type_name: str | None) -> str | None:
    return base_type_name(type_name)
