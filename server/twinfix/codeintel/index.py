"""Java 跨文件符号解析。

解析策略是刻意保守的：只有在仓库内能唯一确定时才建立"已解析"边，否则保留未解析记录，
让调用方知道"这里有个引用，但可能来自外部依赖或存在歧义"。这样不会把猜测伪装成事实。
"""

from __future__ import annotations

import time
from collections import deque

from twinfix.codeintel.base import CodeIndex
from twinfix.codeintel.java import ParsedFile, RawCall, parse_java, simple_type_names
from twinfix.codeintel.model import Edge, ImportSet, IndexMeta, Symbol


class JavaCodeIndex(CodeIndex):
    pass


def build_java_index(files: dict[str, str], truncated: bool = False) -> JavaCodeIndex:
    started = time.monotonic()
    parsed = [parse_java(path, text) for path, text in sorted(files.items())]
    symbols: dict[str, Symbol] = {}
    for item in parsed:
        for symbol in item.symbols:
            symbols.setdefault(symbol.id, symbol)

    types_by_fqn = {s.id: s for s in symbols.values() if s.is_type}
    types_by_simple: dict[str, list[str]] = {}
    for fqn in types_by_fqn:
        types_by_simple.setdefault(fqn.rsplit(".", 1)[-1], []).append(fqn)
    methods_by_type: dict[str, list[Symbol]] = {}
    for symbol in symbols.values():
        if symbol.kind in ("method", "constructor") and symbol.container:
            methods_by_type.setdefault(symbol.container, []).append(symbol)

    imports = {item.path: item.imports for item in parsed}
    path_by_symbol = {symbol.id: symbol.path for symbol in symbols.values()}
    package_by_symbol = {symbol.id: symbol.package for symbol in symbols.values()}
    resolver = _Resolver(types_by_fqn, types_by_simple, imports, path_by_symbol, package_by_symbol)
    supertypes: dict[str, list[str]] = {}
    edges: list[Edge] = []

    def add_edge(edge: Edge) -> None:
        edges.append(edge)

    for item in parsed:
        for ref in item.refs:
            names = simple_type_names(ref.type_name)
            if ref.kind == "constructs" and names:
                names = names[:1]
            for simple in names:
                target = resolver.resolve_type(simple, ref.src)
                if target is None or target.id == ref.src:
                    continue
                add_edge(
                    Edge(
                        src=ref.src,
                        dst=target.id,
                        kind=ref.kind,
                        path=item.path,
                        line=ref.line,
                        name=target.name,
                    )
                )
                if ref.kind in ("extends", "implements"):
                    supertypes.setdefault(ref.src, []).append(target.id)

    for item in parsed:
        for call in item.calls:
            edge = _resolve_call(call, item, symbols, methods_by_type, supertypes, resolver)
            if edge is not None:
                add_edge(edge)

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
    return JavaCodeIndex(symbols, ordered, meta)


class _Resolver:
    def __init__(
        self,
        types_by_fqn: dict[str, Symbol],
        types_by_simple: dict[str, list[str]],
        imports_by_path: dict[str, ImportSet],
        path_by_symbol: dict[str, str],
        package_by_symbol: dict[str, str],
    ) -> None:
        self.types_by_fqn = types_by_fqn
        self.types_by_simple = types_by_simple
        self.imports_by_path = imports_by_path
        self.path_by_symbol = path_by_symbol
        self.package_by_symbol = package_by_symbol

    def resolve_type(self, name: str, src: str) -> Symbol | None:
        path = self.path_by_symbol.get(src)
        source = self.imports_by_path.get(path or "")
        package = self.package_by_symbol.get(src, "")
        simple = name.rsplit(".", 1)[-1]
        if "." in name:
            if name in self.types_by_fqn:
                return self.types_by_fqn[name]
            matches = [fqn for fqn in self.types_by_fqn if fqn.endswith(f".{name}")]
            if len(matches) == 1:
                return self.types_by_fqn[matches[0]]
        imported = source.types.get(simple) if source else None
        if imported and imported in self.types_by_fqn:
            return self.types_by_fqn[imported]
        candidate = f"{package}.{simple}" if package else simple
        if candidate in self.types_by_fqn:
            return self.types_by_fqn[candidate]
        for prefix in source.wildcards if source else ():
            candidate = f"{prefix}.{simple}"
            if candidate in self.types_by_fqn:
                return self.types_by_fqn[candidate]
        matches = self.types_by_simple.get(simple, [])
        return self.types_by_fqn[matches[0]] if len(matches) == 1 else None


def _resolve_call(
    call: RawCall,
    item: ParsedFile,
    symbols: dict[str, Symbol],
    methods_by_type: dict[str, list[Symbol]],
    supertypes: dict[str, list[str]],
    resolver: _Resolver,
) -> Edge | None:
    src = symbols.get(call.src)
    if src is None:
        return None
    receiver_type_name = call.receiver_type
    receiver = call.receiver
    if receiver_type_name is None and receiver:
        if "." in receiver:
            last = receiver.rsplit(".", 1)[-1]
            receiver_type_name = call.variables.get(last)
        elif receiver in call.variables:
            receiver_type_name = call.variables[receiver]
    if receiver_type_name is None and receiver and receiver[:1].isupper():
        receiver_type_name = receiver
    target_type = resolver.resolve_type(receiver_type_name, call.src) if receiver_type_name else None
    if target_type is None and receiver_type_name:
        type_names = simple_type_names(receiver_type_name)
        target_type = resolver.resolve_type(type_names[0], call.src) if type_names else None
    enclosing = src.container
    if receiver in ("this", None) and enclosing:
        target_type = symbols.get(enclosing, target_type)
    candidates = _candidates(call, target_type, enclosing, methods_by_type, supertypes, symbols)
    dst = candidates[0].id if len(candidates) == 1 else None
    return Edge(
        src=call.src,
        dst=dst,
        kind="calls",
        path=item.path,
        line=call.line,
        name=call.name,
        arity=call.arity,
        receiver=receiver,
        receiver_type=receiver_type_name,
    )


def _candidates(
    call: RawCall,
    target_type: Symbol | None,
    enclosing: str | None,
    methods_by_type: dict[str, list[Symbol]],
    supertypes: dict[str, list[str]],
    symbols: dict[str, Symbol],
) -> list[Symbol]:
    if call.name == "<init>":
        # 构造调用只能落到被 new 的类型上；目标类型不在仓库内时保持未解析，
        # 不能退回外围类，否则 `new NoSuchElementException(...)` 会被误记成外层类的构造调用
        if target_type is None:
            return []
        return [s for s in methods_by_type.get(target_type.id, []) if s.kind == "constructor" and s.arity == call.arity]
    if target_type is not None:
        found = _methods_in_chain(target_type.id, call.name, methods_by_type, supertypes)
        exact = [s for s in found if s.arity == call.arity]
        return exact if exact else found
    if enclosing:
        found = _methods_in_chain(enclosing, call.name, methods_by_type, supertypes)
        exact = [s for s in found if s.arity == call.arity]
        if len(exact) == 1:
            return exact
    repo_wide = [s for s in symbols.values() if s.kind == "method" and s.name == call.name and s.arity == call.arity]
    return repo_wide


def _methods_in_chain(
    type_id: str,
    name: str,
    methods_by_type: dict[str, list[Symbol]],
    supertypes: dict[str, list[str]],
) -> list[Symbol]:
    # 先看类型自身：自身有同名方法时，父类同名方法不应参与重载解析，否则永远"有歧义"
    own = [s for s in methods_by_type.get(type_id, []) if s.name == name]
    if own:
        return own
    found: list[Symbol] = []
    seen: set[str] = {type_id}
    queue = deque(supertypes.get(type_id, []))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        found.extend(s for s in methods_by_type.get(current, []) if s.name == name)
        queue.extend(supertypes.get(current, []))
    return found
