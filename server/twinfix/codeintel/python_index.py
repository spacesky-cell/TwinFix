"""Python 跨文件符号解析：imports、同类/同模块调用、基类继承与显式类型注解。"""

from __future__ import annotations

import time

from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.model import Edge, IndexMeta, Symbol
from twinfix.codeintel.python import PyCall, parse_python

_MAX_CANDIDATES = 20


class PythonCodeIndex(CodeIndex):
    pass


def build_python_index(files: dict[str, str], truncated: bool = False) -> PythonCodeIndex:
    started = time.monotonic()
    parsed = [parse_python(path, text) for path, text in sorted(files.items())]
    symbols: dict[str, Symbol] = {}
    for item in parsed:
        for symbol in item.symbols:
            symbols.setdefault(symbol.id, symbol)

    classes = {s.id: s for s in symbols.values() if s.is_type}
    by_qualified: dict[str, list[Symbol]] = {}
    by_name: dict[str, list[Symbol]] = {}
    methods_by_class: dict[str, list[Symbol]] = {}
    functions_by_module: dict[str, list[Symbol]] = {}
    module_by_path = {item.path: item.module for item in parsed}
    imports_by_path = {item.path: item.imports for item in parsed}
    wildcards_by_path = {item.path: item.wildcards for item in parsed}
    for symbol in symbols.values():
        by_qualified.setdefault(symbol.qualified_name, []).append(symbol)
        by_name.setdefault(symbol.name, []).append(symbol)
        if symbol.kind == "method" and symbol.container:
            methods_by_class.setdefault(symbol.container, []).append(symbol)
        elif symbol.kind == "function":
            functions_by_module.setdefault(symbol.package, []).append(symbol)

    parent_by_class: dict[str, list[str]] = {}
    resolver = _PyResolver(
        classes,
        by_qualified,
        by_name,
        methods_by_class,
        functions_by_module,
        imports_by_path,
        wildcards_by_path,
        module_by_path,
    )
    edges: list[Edge] = []
    for item in parsed:
        for class_id, bases in item.class_bases.items():
            for base in bases:
                target = resolver.resolve_type(base, item.path, container=None)
                if target is not None and target.id != class_id:
                    edges.append(
                        Edge(class_id, target.id, "extends", item.path, _class_line(symbols[class_id]), target.name)
                    )
                    parent_by_class.setdefault(class_id, []).append(target.id)
    resolver.parent_by_class = parent_by_class

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
    return PythonCodeIndex(symbols, ordered, meta)


class _PyResolver:
    def __init__(
        self,
        classes: dict[str, Symbol],
        by_qualified: dict[str, list[Symbol]],
        by_name: dict[str, list[Symbol]],
        methods_by_class: dict[str, list[Symbol]],
        functions_by_module: dict[str, list[Symbol]],
        imports_by_path: dict[str, dict[str, list[str]]],
        wildcards_by_path: dict[str, list[str]],
        module_by_path: dict[str, str],
    ) -> None:
        self.classes = classes
        self.by_qualified = by_qualified
        self.by_name = by_name
        self.methods_by_class = methods_by_class
        self.functions_by_module = functions_by_module
        self.imports_by_path = imports_by_path
        self.wildcards_by_path = wildcards_by_path
        self.module_by_path = module_by_path
        self.parent_by_class: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 类型

    def resolve_type(self, name: str, path: str, *, container: str | None) -> Symbol | None:
        name = name.strip()
        if not name:
            return None
        if "." in name:
            direct = self._class_by_qualified(name)
            if direct is not None:
                return direct
            root, rest = name.split(".", 1)
            for candidate in self.imports_by_path.get(path, {}).get(root, []):
                found = self._class_by_qualified(f"{candidate}.{rest}")
                if found is not None:
                    return found
            suffix = [c for c in self.classes.values() if c.qualified_name.endswith(f".{name}")]
            return suffix[0] if len(suffix) == 1 else None
        if container:
            nested = self.classes.get(f"{container}.{name}")
            if nested is not None:
                return nested
        for candidate in self.imports_by_path.get(path, {}).get(name, []):
            found = self._class_by_qualified(candidate)
            if found is not None:
                return found
        module = self.module_by_path.get(path, "")
        same_module = self.classes.get(f"{module}.{name}" if module else name)
        if same_module is not None:
            return same_module
        matches = [c for c in self.classes.values() if c.name == name]
        return matches[0] if len(matches) == 1 else None

    def _class_by_qualified(self, qualified: str) -> Symbol | None:
        for symbol in self.by_qualified.get(qualified, []):
            if symbol.is_type:
                return symbol
        for items in self.by_qualified.values():
            for item in items:
                if item.is_type and item.qualified_name.endswith(f".{qualified}"):
                    return item
        return None

    # ---------------------------------------------------------------- 调用

    def resolve_call(self, call: PyCall, path: str, symbols: dict[str, Symbol]) -> Edge | None:
        source = symbols.get(call.src)
        container = source.container if source is not None else None
        module = self.module_by_path.get(path, "")
        receiver = call.receiver

        if receiver:
            if receiver in ("self", "cls") and container:
                method = self._method(container, call.name, call.arity)
                if method is not None:
                    return Edge(call.src, method.id, "calls", path, call.line, method.name, method.arity, receiver)
            target = self._resolve_receiver(receiver, path, container)
            if target is not None and target.is_type:
                method = self._method(target.id, call.name, call.arity)
                if method is not None:
                    return Edge(call.src, method.id, "calls", path, call.line, method.name, method.arity, receiver)
            for candidate in self.imports_by_path.get(path, {}).get(receiver, []):
                function = self._function_in_module(candidate, call.name, call.arity)
                if function is not None:
                    return Edge(
                        call.src, function.id, "calls", path, call.line, function.name, function.arity, receiver
                    )
            return Edge(call.src, None, "calls", path, call.line, call.name, call.arity, receiver)

        if container:
            method = self._method(container, call.name, call.arity)
            if method is not None:
                return Edge(call.src, method.id, "calls", path, call.line, method.name, method.arity)
        function = self._function_in_module(module, call.name, call.arity)
        if function is not None:
            return Edge(call.src, function.id, "calls", path, call.line, function.name, function.arity)
        for candidate in self.imports_by_path.get(path, {}).get(call.name, []):
            symbol = self._symbol_for_import(candidate, call.arity)
            if symbol is not None:
                kind = "constructs" if symbol.is_type else "calls"
                return Edge(call.src, symbol.id, kind, path, call.line, symbol.name, symbol.arity)
        for prefix in self.wildcards_by_path.get(path, []):
            symbol = self._symbol_for_import(f"{prefix}.{call.name}", call.arity)
            if symbol is not None:
                kind = "constructs" if symbol.is_type else "calls"
                return Edge(call.src, symbol.id, kind, path, call.line, symbol.name, symbol.arity)
        if call.name[:1].isupper():
            cls = self.resolve_type(call.name, path, container=container)
            if cls is not None:
                return Edge(call.src, cls.id, "constructs", path, call.line, cls.name)
        matches = [
            s for s in self.by_name.get(call.name, []) if s.kind in ("function", "method") and s.arity == call.arity
        ]
        if len(matches) == 1:
            return Edge(call.src, matches[0].id, "calls", path, call.line, matches[0].name, matches[0].arity)
        kind = "constructs" if call.name[:1].isupper() else "calls"
        return Edge(call.src, None, kind, path, call.line, call.name, call.arity)

    def _resolve_receiver(self, receiver: str, path: str, container: str | None) -> Symbol | None:
        if container:
            nested = self.classes.get(f"{container}.{receiver}")
            if nested is not None:
                return nested
        return self.resolve_type(receiver, path, container=container)

    def _method(self, class_id: str, name: str, arity: int | None) -> Symbol | None:
        seen: set[str] = set()
        queue = [class_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for method in self.methods_by_class.get(current, []):
                if method.name == name and (arity is None or method.arity == arity):
                    return method
            queue.extend(self.parent_by_class.get(current, []))
        # 参数个数对不上时（默认参数、*args），名字唯一也可以接受
        matches = [m for m in self.methods_by_class.get(class_id, []) if m.name == name]
        return matches[0] if len(matches) == 1 else None

    def _function_in_module(self, module: str, name: str, arity: int | None) -> Symbol | None:
        matches = [f for f in self.functions_by_module.get(module, []) if f.name == name]
        exact = [f for f in matches if arity is None or f.arity == arity]
        if len(exact) == 1:
            return exact[0]
        return matches[0] if len(matches) == 1 else None

    def _symbol_for_import(self, qualified: str, arity: int | None) -> Symbol | None:
        candidates = self.by_qualified.get(qualified, [])
        for symbol in candidates:
            if symbol.is_type:
                return symbol
            if symbol.kind in ("function", "method") and (arity is None or symbol.arity == arity):
                return symbol
        matches = [s for s in self.by_name.get(qualified.rsplit(".", 1)[-1], []) if s.qualified_name == qualified]
        if len(matches) == 1:
            return matches[0]
        return None


def _class_line(symbol: Symbol) -> int:
    return symbol.start_line
