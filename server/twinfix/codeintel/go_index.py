"""Go 跨文件符号解析：import 别名、同包函数、接收者/字段类型、接口嵌入。

与 Java/Python 索引一样刻意保守：只有仓库内能唯一确定时才建立已解析边，
其余保留 `dst=None`（可能是标准库、外部模块或动态分派）。
"""

from __future__ import annotations

import re
import time

from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.go import GoCall, base_type_name, parse_go
from twinfix.codeintel.model import Edge, IndexMeta, Symbol

_MODULE = re.compile(r"^module\s+(\S+)", re.MULTILINE)


class GoCodeIndex(CodeIndex):
    pass


def build_go_index(files: dict[str, str], truncated: bool = False) -> GoCodeIndex:
    started = time.monotonic()
    module = _module_path(files)
    parsed = [parse_go(path, text, module) for path, text in sorted(files.items())]
    symbols: dict[str, Symbol] = {}
    for item in parsed:
        for symbol in item.symbols:
            symbols.setdefault(symbol.id, symbol)

    types_by_qname = {s.id: s for s in symbols.values() if s.is_type}
    types_by_simple: dict[str, list[str]] = {}
    for qname, symbol in types_by_qname.items():
        types_by_simple.setdefault(symbol.name, []).append(qname)
    functions_by_pkg: dict[str, list[Symbol]] = {}
    methods_by_type: dict[str, list[Symbol]] = {}
    fields_by_type: dict[tuple[str, str], Symbol] = {}
    for symbol in symbols.values():
        if symbol.kind == "function":
            functions_by_pkg.setdefault(symbol.package, []).append(symbol)
        elif symbol.kind == "method" and symbol.container:
            methods_by_type.setdefault(symbol.container, []).append(symbol)
        elif symbol.kind == "field" and symbol.container:
            fields_by_type.setdefault((symbol.container, symbol.name), symbol)

    package_by_path = {item.path: item.package_path for item in parsed}
    imports_by_path = {item.path: item.imports for item in parsed}
    resolver = _GoResolver(
        types_by_qname,
        types_by_simple,
        functions_by_pkg,
        methods_by_type,
        fields_by_type,
        imports_by_path,
        package_by_path,
    )
    edges: list[Edge] = []

    # 先解析内嵌关系，方法/字段查找需要沿嵌入链上溯
    for item in parsed:
        for src, name in item.embeds:
            target = resolver.resolve_type(name, item.path, src=src)
            if target is None or target.id == src:
                continue
            edges.append(Edge(src, target.id, "extends", item.path, _line(symbols, src), target.name))
            resolver.embeds_by_type.setdefault(src, []).append(target.id)

    for item in parsed:
        for ref in item.refs:
            target = resolver.resolve_type(ref.name, item.path, src=ref.src)
            if target is None or target.id == ref.src:
                continue
            kind = "constructs" if ref.kind == "constructs" else "type_ref"
            edges.append(Edge(ref.src, target.id, kind, item.path, ref.line, target.name))
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
    return GoCodeIndex(symbols, ordered, meta)


def _module_path(files: dict[str, str]) -> str:
    candidates = [path for path in files if path == "go.mod" or path.endswith("/go.mod")]
    if not candidates:
        return ""
    path = min(candidates, key=lambda item: (item.count("/"), len(item)))
    match = _MODULE.search(files.get(path, ""))
    return match.group(1).strip('"`') if match else ""


class _GoResolver:
    def __init__(
        self,
        types_by_qname: dict[str, Symbol],
        types_by_simple: dict[str, list[str]],
        functions_by_pkg: dict[str, list[Symbol]],
        methods_by_type: dict[str, list[Symbol]],
        fields_by_type: dict[tuple[str, str], Symbol],
        imports_by_path: dict[str, dict[str, str]],
        package_by_path: dict[str, str],
    ) -> None:
        self.types_by_qname = types_by_qname
        self.types_by_simple = types_by_simple
        self.functions_by_pkg = functions_by_pkg
        self.methods_by_type = methods_by_type
        self.fields_by_type = fields_by_type
        self.imports_by_path = imports_by_path
        self.package_by_path = package_by_path
        self.embeds_by_type: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 类型

    def resolve_type(self, name: str, path: str, src: str | None = None) -> Symbol | None:
        qualified = base_type_name(name)
        if not qualified:
            return None
        direct = self.types_by_qname.get(qualified)
        if direct is not None:
            return direct
        if "." in qualified:
            alias, _, rest = qualified.partition(".")
            imported = self.imports_by_path.get(path, {}).get(alias)
            if imported:
                target = self.types_by_qname.get(f"{imported}.{rest}")
                if target is not None:
                    return target
        package = self.package_by_path.get(path, "")
        candidate = f"{package}.{qualified}" if package else qualified
        target = self.types_by_qname.get(candidate)
        if target is not None:
            return target
        matches = self.types_by_simple.get(qualified, [])
        return self.types_by_qname[matches[0]] if len(matches) == 1 else None

    # ---------------------------------------------------------------- 调用

    def resolve_call(self, call: GoCall, path: str, symbols: dict[str, Symbol]) -> Edge | None:
        if call.src not in symbols:
            return None
        imports = self.imports_by_path.get(path, {})
        package = self.package_by_path.get(path, "")
        receiver = call.receiver or ""
        receiver_type = ""
        target: Symbol | None = None

        if receiver:
            if not call.receiver_is_call and receiver in imports:
                imported = imports[receiver]
                receiver_type = imported
                target = self._function_in_pkg(imported, call.name, call.arity)
                if target is None:
                    target = self._type_in_pkg(imported, call.name)
            elif call.receiver_is_call:
                receiver_type = self._function_return(receiver, path)
                target_type = self.resolve_type(receiver_type, path) if receiver_type else None
                if target_type is not None:
                    target = self._method(target_type.id, call.name, call.arity)
            else:
                receiver_type = self._resolve_var_chain(receiver, path, call.variables)
                target_type = self.resolve_type(receiver_type, path) if receiver_type else None
                if target_type is not None:
                    target = self._method(target_type.id, call.name, call.arity)
        else:
            target = self._function_in_pkg(package, call.name, call.arity)
            if target is None:
                target = self._type_in_pkg(package, call.name)

        if target is not None:
            kind = "constructs" if target.is_type else "calls"
            name = target.name
        else:
            kind = "constructs" if not receiver and call.name[:1].isupper() else "calls"
            name = call.name
        return Edge(
            src=call.src,
            dst=target.id if target is not None else None,
            kind=kind,
            path=path,
            line=call.line,
            name=name,
            arity=call.arity,
            receiver=receiver or None,
            receiver_type=receiver_type or None,
        )

    def _resolve_var_chain(self, receiver: str, path: str, variables: dict[str, str]) -> str:
        parts = [part for part in receiver.split(".") if part]
        if not parts:
            return ""
        current = variables.get(parts[0], "")
        if current.startswith("@"):
            # `x := NewService(...)` 推函数返回值；`x := Service(y)` 是类型转换，直接当类型
            current = self._function_return(current[1:], path) or base_type_name(current[1:])
        for part in parts[1:]:
            if not current:
                return ""
            type_symbol = self.resolve_type(current, path)
            if type_symbol is None:
                return ""
            field = self._field(type_symbol.id, part)
            if field is None:
                return ""
            current = field.return_type or ""
        return current

    def _function_return(self, name: str, path: str) -> str:
        qualifier, _, function_name = name.rpartition(".")
        if qualifier:
            imported = self.imports_by_path.get(path, {}).get(qualifier)
            if not imported:
                return ""
            target = self._function_by_name(imported, function_name)
        else:
            target = self._function_by_name(self.package_by_path.get(path, ""), name)
        return target.return_type or "" if target is not None else ""

    def _function_in_pkg(self, package: str, name: str, arity: int | None) -> Symbol | None:
        matches = [symbol for symbol in self.functions_by_pkg.get(package, []) if symbol.name == name]
        exact = [symbol for symbol in matches if arity is None or symbol.arity == arity]
        if len(exact) == 1:
            return exact[0]
        return exact[0] if exact else (matches[0] if len(matches) == 1 else None)

    def _function_by_name(self, package: str, name: str) -> Symbol | None:
        matches = [symbol for symbol in self.functions_by_pkg.get(package, []) if symbol.name == name]
        return matches[0] if len(matches) == 1 else None

    def _type_in_pkg(self, package: str, name: str) -> Symbol | None:
        return self.types_by_qname.get(f"{package}.{name}" if package else name)

    def _method(self, type_id: str, name: str, arity: int | None) -> Symbol | None:
        own = [symbol for symbol in self.methods_by_type.get(type_id, []) if symbol.name == name]
        found = _pick(own, arity)
        if found is not None:
            return found
        seen = {type_id}
        queue = list(self.embeds_by_type.get(type_id, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            found = _pick([s for s in self.methods_by_type.get(current, []) if s.name == name], arity)
            if found is not None:
                return found
            queue.extend(self.embeds_by_type.get(current, []))
        return None

    def _field(self, type_id: str, name: str) -> Symbol | None:
        direct = self.fields_by_type.get((type_id, name))
        if direct is not None:
            return direct
        seen = {type_id}
        queue = list(self.embeds_by_type.get(type_id, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            found = self.fields_by_type.get((current, name))
            if found is not None:
                return found
            queue.extend(self.embeds_by_type.get(current, []))
        return None


def _pick(candidates: list[Symbol], arity: int | None) -> Symbol | None:
    if not candidates:
        return None
    exact = [symbol for symbol in candidates if arity is None or symbol.arity == arity]
    if exact:
        return exact[0]
    return candidates[0] if len(candidates) == 1 else None


def _line(symbols: dict[str, Symbol], symbol_id: str) -> int:
    symbol = symbols.get(symbol_id)
    return symbol.start_line if symbol is not None else 0
