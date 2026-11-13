"""索引查询的公共实现：搜索、消歧、引用查找。语言解析器只需产出 Symbol/Edge。"""

from __future__ import annotations

from twinfix.codeintel.model import Edge, IndexMeta, Reference, Symbol

_MAX_CANDIDATES = 20


class CodeIndex:
    def __init__(self, symbols: dict[str, Symbol], edges: list[Edge], meta: IndexMeta) -> None:
        self.symbols = symbols
        self.edges = edges
        self.meta = meta
        self._by_path: dict[str, list[Symbol]] = {}
        for symbol in symbols.values():
            self._by_path.setdefault(symbol.path, []).append(symbol)
        for items in self._by_path.values():
            items.sort(key=lambda s: (s.start_line, s.end_line))
        self._refs: dict[str, list[Edge]] = {}
        for edge in edges:
            self._refs.setdefault(edge.dst or "", []).append(edge)

    def search(self, query: str, kind: str | None = None, limit: int = 20) -> list[Symbol]:
        needle = query.lower().strip()
        if not needle:
            return []
        scored: list[tuple[int, int, Symbol]] = []
        for symbol in self.symbols.values():
            if kind and symbol.kind != kind:
                continue
            name = symbol.name.lower()
            qualified = symbol.qualified_name.lower()
            if name == needle:
                rank = 0
            elif name.startswith(needle):
                rank = 1
            elif needle in name:
                rank = 2
            elif needle in qualified or needle in symbol.id.lower():
                rank = 3
            else:
                continue
            scored.append((rank, len(symbol.name), symbol))
        scored.sort(key=lambda item: (item[0], item[1], item[2].qualified_name))
        return [item[2] for item in scored[: max(1, limit)]]

    def outline(self, path: str) -> list[Symbol]:
        return self._by_path.get(path.removeprefix("./"), [])

    def resolve_symbol(self, text: str, path_prefix: str = "") -> tuple[Symbol | None, list[Symbol]]:
        query = text.strip()
        if not query:
            return None, []

        def in_scope(symbol: Symbol) -> bool:
            return not path_prefix or symbol.path == path_prefix or symbol.path.startswith(f"{path_prefix}/")

        if query in self.symbols and in_scope(self.symbols[query]):
            return self.symbols[query], []
        by_qualified = [s for s in self.symbols.values() if s.qualified_name == query and in_scope(s)]
        if len(by_qualified) == 1:
            return by_qualified[0], []
        by_name = [s for s in self.symbols.values() if s.name == query and in_scope(s)]
        if len(by_name) == 1:
            return by_name[0], []
        type_matches = [s for s in by_name if s.is_type]
        if len(type_matches) == 1:
            return type_matches[0], []
        suffix = [
            s
            for s in self.symbols.values()
            if in_scope(s) and (s.qualified_name.endswith(f".{query}") or s.id.endswith(query))
        ]
        if len(suffix) == 1:
            return suffix[0], []
        candidates = by_name or suffix
        if not candidates:
            candidates = [s for s in self.symbols.values() if in_scope(s) and query.lower() in s.name.lower()]
        candidates = sorted(candidates, key=lambda s: (s.path, s.start_line))[:_MAX_CANDIDATES]
        return None, candidates

    def find_references(self, symbol: Symbol, kind: str = "all", limit: int = 40) -> list[Reference]:
        wanted: set[str] | None
        if kind == "calls":
            wanted = {"calls", "constructs"}
        elif kind == "types":
            wanted = {"type_ref", "constructs", "extends", "implements"}
        else:
            wanted = None
        edges: list[Edge] = []
        for edge in self._refs.get(symbol.id, []):
            if edge.src != symbol.id and (wanted is None or edge.kind in wanted):
                edges.append(edge)
        for edge in self.edges:
            if edge.dst is not None or edge.src == symbol.id:
                continue
            if edge.name != symbol.name:
                continue
            if symbol.arity is not None and edge.arity is not None and edge.arity != symbol.arity:
                continue
            if wanted is None or edge.kind in wanted:
                edges.append(edge)
        unique: dict[tuple[str, int, str], Edge] = {}
        for edge in edges:
            unique.setdefault((edge.src, edge.line, edge.kind), edge)
        result = [Reference(edge=e, source=self.symbols.get(e.src)) for e in unique.values()]
        result.sort(key=lambda r: (r.edge.path, r.edge.line, r.edge.kind))
        return result[: max(1, limit)]
