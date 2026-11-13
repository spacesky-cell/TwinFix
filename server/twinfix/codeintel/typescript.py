"""用 tree-sitter 解析 TypeScript / JavaScript，产出符号、调用与类型引用。

只解析代码里明确写出的内容：类/接口/类型别名/枚举、方法、字段、函数与箭头函数、
import、继承/实现、参数与返回类型、直接调用。跨文件消歧交给 typescript_index；
动态分派、未标注类型的接收者保持 unresolved，不把猜测伪装成事实。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from twinfix.codeintel.model import Symbol

_TS = Language(tree_sitter_typescript.language_typescript())
_TSX = Language(tree_sitter_typescript.language_tsx())
_JS = Language(tree_sitter_javascript.language())
_LANGUAGES = {".ts": _TS, ".tsx": _TSX, ".js": _JS, ".jsx": _JS, ".mjs": _JS, ".cjs": _JS}

_BUILTIN_TYPES = frozenset(
    {
        "any",
        "bigint",
        "boolean",
        "never",
        "null",
        "number",
        "object",
        "string",
        "symbol",
        "this",
        "undefined",
        "unknown",
        "void",
    }
)
_GLOBAL_TYPES = frozenset(
    {
        "Array",
        "Date",
        "Error",
        "Map",
        "Promise",
        "Record",
        "RegExp",
        "Set",
        "Partial",
        "Required",
        "Readonly",
        "Pick",
        "Omit",
        "Exclude",
        "Extract",
        "NonNullable",
        "ReturnType",
        "Parameters",
    }
)
_CLASS_KINDS = frozenset({"class_declaration", "abstract_class_declaration"})
_FUNCTION_KINDS = frozenset(
    {"function_declaration", "generator_function_declaration", "arrow_function", "function_expression"}
)
_TYPE_KINDS = {
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
}
_SKIP_TYPE_NODES = frozenset({"type_parameters", "type_parameter", "predefined_type", "literal_type"})


@dataclass
class TsImport:
    specifier: str
    imported: str  # 具名导入名 / "*" / "default"
    local: str


@dataclass
class TsCall:
    src: str
    line: int
    name: str
    receiver: str | None = None
    receiver_is_call: bool = False
    is_new: bool = False
    arity: int | None = None
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class TsRef:
    src: str
    line: int
    name: str
    kind: str = "type_ref"


@dataclass
class TsParsed:
    path: str
    module: str
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[TsCall] = field(default_factory=list)
    refs: list[TsRef] = field(default_factory=list)
    imports: list[TsImport] = field(default_factory=list)
    extends: dict[str, list[str]] = field(default_factory=dict)
    implements: dict[str, list[str]] = field(default_factory=dict)
    variables: dict[str, dict[str, str]] = field(default_factory=dict)


def module_name(path: str) -> str:
    """orders/pricing.ts -> orders.pricing；src/orders/index.ts -> orders。"""
    normalized = path.replace("\\", "/").removeprefix("./")
    for suffix in sorted(_LANGUAGES, key=len, reverse=True):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    parts = [p for p in normalized.split("/") if p and p != "."]
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "index":
        parts = parts[:-1]
    return ".".join(parts)


def parse_typescript(path: str, text: str) -> TsParsed:
    data = text.encode("utf-8")
    suffix = _suffix(path)
    tree = Parser(_LANGUAGES.get(suffix, _TS)).parse(data)
    parsed = TsParsed(path=path, module=module_name(path))
    _collect_imports(tree.root_node, data, parsed)
    _collect_definitions(tree.root_node, data, parsed, module=parsed.module, container=None, prefix="")
    _collect_references(tree.root_node, data, parsed)
    return parsed


# ---------------------------------------------------------------- 定义与符号


def _collect_definitions(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    module: str,
    container: str | None,
    prefix: str,
) -> None:
    for child in node.named_children:
        target = _unwrap_export(child)
        if target.type in _TYPE_KINDS:
            _add_type(target, data, parsed, module=module, container=container, prefix=prefix)
        elif target.type in _FUNCTION_KINDS:
            _add_function(target, data, parsed, module=module, container=container, prefix=prefix)
        elif target.type in ("lexical_declaration", "variable_declaration"):
            _add_variables(target, data, parsed, module=module, container=container, prefix=prefix)


def _unwrap_export(node: Node) -> Node:
    while node.type == "export_statement":
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            return node
        node = declaration
    return node


def _add_type(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    module: str,
    container: str | None,
    prefix: str,
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    own_id = f"{container}.{name}" if container else f"{module}.{name}" if module else name
    kind = _TYPE_KINDS[node.type]
    body = node.child_by_field_name("body")
    signature = _text(node, data).splitlines()[0][:160] if _text(node, data) else f"{kind} {name}"
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=own_id,
            kind=kind,
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=signature,
            container=container,
            package=module,
        )
    )
    ignored = _type_params(node, data)
    if node.type in _CLASS_KINDS:
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        if heritage is not None:
            _class_heritage(heritage, data, parsed, own_id, ignored)
    elif node.type == "interface_declaration":
        clause = next((c for c in node.named_children if c.type == "extends_type_clause"), None)
        if clause is not None:
            bases = _heritage_names(clause, data)
            if bases:
                parsed.extends[own_id] = bases
    if body is None:
        return
    if node.type in [*_CLASS_KINDS, "interface_declaration"]:
        _collect_members(body, data, parsed, module=module, class_id=own_id, prefix=prefix)
    elif node.type == "enum_declaration":
        _collect_enum_members(body, data, parsed, enum_id=own_id)


def _collect_members(
    body: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    module: str,
    class_id: str,
    prefix: str,
) -> None:
    ignored = _type_params(body.parent, data) if body.parent is not None else set()
    for child in body.named_children:
        if child.type == "method_definition" or child.type == "method_signature":
            _add_method(child, data, parsed, class_id=class_id, ignored=ignored)
        elif child.type in ("public_field_definition", "property_definition", "field_definition", "property_signature"):
            _add_field(child, data, parsed, class_id=class_id, ignored=ignored)
        elif child.type == "abstract_method_signature":
            _add_method(child, data, parsed, class_id=class_id, ignored=ignored)


def _add_method(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    class_id: str,
    ignored: set[str],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    params = node.child_by_field_name("parameters")
    arity = _arity(params, data)
    is_ctor = name in ("constructor",)
    own_id = f"{class_id}#{name}/{arity}"
    kind = "constructor" if is_ctor else "method"
    body = node.child_by_field_name("body")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=f"{class_id}.{name}",
            kind=kind,
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=class_id,
            package=parsed.module,
            arity=arity,
            return_type=_return_type(node, data),
        )
    )
    _signature_refs(node, data, parsed, own_id, ignored)
    _constructor_properties(node, data, parsed, class_id, ignored)


def _add_field(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    class_id: str,
    ignored: set[str],
) -> None:
    name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
    if name_node is None:
        return
    name = _text(name_node, data)
    if not name or name.startswith(("[", "#")):
        return
    own_id = f"{class_id}#field:{name}"
    type_node = node.child_by_field_name("type")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=f"{class_id}.{name}",
            kind="field",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=class_id,
            package=parsed.module,
            return_type=_type_name(type_node, data),
        )
    )
    if type_node is not None:
        _type_refs(type_node, data, parsed, own_id, _line(node), ignored)


def _collect_enum_members(body: Node, data: bytes, parsed: TsParsed, *, enum_id: str) -> None:
    for child in body.named_children:
        target = child
        if child.type == "enum_assignment":
            name_node = child.child_by_field_name("name")
            target = name_node if name_node is not None else child
        elif child.type == "property_identifier":
            target = child
        else:
            continue
        name = _text(target, data)
        if not name:
            continue
        parsed.symbols.append(
            Symbol(
                id=f"{enum_id}#field:{name}",
                name=name,
                qualified_name=f"{enum_id}.{name}",
                kind="field",
                path=parsed.path,
                start_line=_line(child),
                end_line=_line(child),
                signature=_text(child, data).splitlines()[0][:120],
                container=enum_id,
                package=parsed.module,
            )
        )


def _add_variables(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    module: str,
    container: str | None,
    prefix: str,
) -> None:
    for declarator in node.named_children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier":
            continue
        name = _text(name_node, data)
        if not name:
            continue
        value = declarator.child_by_field_name("value")
        type_node = declarator.child_by_field_name("type")
        if value is not None and value.type in _FUNCTION_KINDS:
            parsed.symbols.append(
                Symbol(
                    id=f"{module}.{name}/{_arity(value.child_by_field_name('parameters'), data)}"
                    if module
                    else f"{name}/{_arity(value.child_by_field_name('parameters'), data)}",
                    name=name,
                    qualified_name=f"{module}.{name}" if module else name,
                    kind="function",
                    path=parsed.path,
                    start_line=_line(declarator),
                    end_line=_line(value) + _span(value) - 1,
                    signature=_text(declarator, data).splitlines()[0][:160],
                    container=None,
                    package=module,
                    arity=_arity(value.child_by_field_name("parameters"), data),
                    return_type=_return_type(value, data),
                )
            )
            src_id = f"{module}.{name}/{_arity(value.child_by_field_name('parameters'), data)}"
            _signature_refs(value, data, parsed, src_id, set())
            continue
        if container:
            own_id = f"{container}#field:{name}"
            kind = "field"
        else:
            own_id = f"{module}:{name}" if module else name
            kind = "variable"
        parsed.symbols.append(
            Symbol(
                id=own_id,
                name=name,
                qualified_name=own_id,
                kind=kind,
                path=parsed.path,
                start_line=_line(declarator),
                end_line=_line(value) + _span(value) - 1 if value is not None else _line(declarator),
                signature=_text(declarator, data).splitlines()[0][:160],
                container=container,
                package=module,
                return_type=_type_name(type_node, data),
            )
        )
        if type_node is not None:
            _type_refs(type_node, data, parsed, own_id, _line(declarator), set())


def _constructor_properties(node: Node, data: bytes, parsed: TsParsed, class_id: str, ignored: set[str]) -> None:
    """`constructor(private readonly repo: Repo)` 同时声明字段。"""
    params = node.child_by_field_name("parameters")
    if params is None:
        return
    for param in params.named_children:
        if param.type not in ("required_parameter", "optional_parameter"):
            continue
        if not any(c.type == "accessibility_modifier" for c in param.children):
            continue
        pattern = param.child_by_field_name("pattern")
        type_node = param.child_by_field_name("type")
        if pattern is None or pattern.type != "identifier":
            continue
        name = _text(pattern, data)
        own_id = f"{class_id}#field:{name}"
        parsed.symbols.append(
            Symbol(
                id=own_id,
                name=name,
                qualified_name=f"{class_id}.{name}",
                kind="field",
                path=parsed.path,
                start_line=_line(param),
                end_line=_line(param),
                signature=_text(param, data)[:160],
                container=class_id,
                package=parsed.module,
                return_type=_type_name(type_node, data),
            )
        )
        if type_node is not None:
            _type_refs(type_node, data, parsed, own_id, _line(param), ignored)


def _add_function(
    node: Node,
    data: bytes,
    parsed: TsParsed,
    *,
    module: str,
    container: str | None,
    prefix: str,
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    params = node.child_by_field_name("parameters")
    arity = _arity(params, data)
    own_id = f"{module}.{name}/{arity}" if module else f"{name}/{arity}"
    body = node.child_by_field_name("body")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=f"{module}.{name}" if module else name,
            kind="function",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=container,
            package=module,
            arity=arity,
            return_type=_return_type(node, data),
        )
    )
    _signature_refs(node, data, parsed, own_id, _type_params(node, data))


def _class_heritage(heritage: Node, data: bytes, parsed: TsParsed, class_id: str, ignored: set[str]) -> None:
    for child in heritage.named_children:
        if child.type == "extends_clause":
            names = _heritage_names(child, data)
            if names:
                parsed.extends[class_id] = names
        elif child.type == "implements_clause":
            names = _heritage_names(child, data)
            if names:
                parsed.implements[class_id] = names


def _heritage_names(clause: Node, data: bytes) -> list[str]:
    names: list[str] = []
    for child in clause.named_children:
        if child.type == "type_arguments":
            continue
        name = _type_name(child, data)
        if name and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------- 引用与调用


def _signature_refs(node: Node, data: bytes, parsed: TsParsed, src: str, ignored: set[str]) -> None:
    params = node.child_by_field_name("parameters")
    variables = parsed.variables.setdefault(src, {})
    if params is not None:
        for param in params.named_children:
            if param.type not in ("required_parameter", "optional_parameter"):
                continue
            pattern = param.child_by_field_name("pattern")
            type_node = param.child_by_field_name("type")
            if type_node is not None:
                _type_refs(type_node, data, parsed, src, _line(param), ignored)
                type_name = _type_name(type_node, data)
                if pattern is not None and pattern.type == "identifier" and type_name:
                    variables[_text(pattern, data)] = type_name
    ret = node.child_by_field_name("return_type")
    if ret is not None:
        _type_refs(ret, data, parsed, src, _line(node), ignored)


def _collect_references(root: Node, data: bytes, parsed: TsParsed) -> None:
    ranges = [
        (s.start_line, s.end_line, s.id) for s in parsed.symbols if s.kind in ("function", "method", "constructor")
    ]
    type_ranges = [(s.start_line, s.end_line, s.id) for s in parsed.symbols if s.is_type]

    def owner(line: int) -> str | None:
        # 类字段初始化里的 new Foo() 归到类，而不是丢掉
        return _innermost(ranges, line) or _innermost(type_ranges, line)

    def walk(node: Node) -> None:
        if node.type in ("call_expression", "new_expression"):
            src = owner(_line(node))
            if src is not None:
                call = _call(node, data, src, parsed.variables.get(src, {}))
                if call is not None:
                    parsed.calls.append(call)
        elif node.type == "variable_declarator":
            src = owner(_line(node))
            if src is not None:
                _local_variable(node, data, parsed, src)
        for child in node.named_children:
            walk(child)

    walk(root)


def _local_variable(node: Node, data: bytes, parsed: TsParsed, src: str) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None or name_node.type != "identifier":
        return
    type_node = node.child_by_field_name("type")
    value = node.child_by_field_name("value")
    name = _text(name_node, data)
    if type_node is not None:
        type_name = _type_name(type_node, data)
        if type_name:
            parsed.variables.setdefault(src, {})[name] = type_name
        _type_refs(type_node, data, parsed, src, _line(node), set())
        return
    if value is not None and value.type == "new_expression":
        constructor = value.child_by_field_name("constructor")
        type_name = _type_name(constructor, data)
        if type_name:
            parsed.variables.setdefault(src, {})[name] = type_name


def _call(node: Node, data: bytes, src: str, variables: dict[str, str]) -> TsCall | None:
    if node.type == "new_expression":
        constructor = node.child_by_field_name("constructor")
        if constructor is None:
            return None
        name = _type_name(constructor, data)
        if not name:
            return None
        return TsCall(
            src=src,
            line=_line(node),
            name=name.rsplit(".", 1)[-1],
            arity=_argument_count(node.child_by_field_name("arguments")),
            is_new=True,
            variables=dict(variables),
        )
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    arity = _argument_count(node.child_by_field_name("arguments"))
    if fn.type == "identifier":
        return TsCall(src=src, line=_line(node), name=_text(fn, data), arity=arity, variables=dict(variables))
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None:
            return None
        name = _text(prop, data)
        receiver: str | None = None
        receiver_is_call = False
        if obj is not None:
            if obj.type in ("identifier", "this", "super"):
                receiver = _text(obj, data)
            elif obj.type == "call_expression":
                inner = obj.child_by_field_name("function")
                if inner is not None and inner.type == "identifier":
                    receiver = _text(inner, data)
                    receiver_is_call = True
            elif obj.type == "new_expression":
                constructor = obj.child_by_field_name("constructor")
                if constructor is not None:
                    receiver = _type_name(constructor, data)
                    receiver_is_call = True
        return TsCall(
            src=src,
            line=_line(node),
            name=name,
            receiver=receiver,
            receiver_is_call=receiver_is_call,
            arity=arity,
            variables=dict(variables),
        )
    return None


def _argument_count(args: Node | None) -> int:
    if args is None:
        return 0
    return len([c for c in args.named_children if c.type != "spread_element"])


def _innermost(ranges: list[tuple[int, int, str]], line: int) -> str | None:
    best: tuple[int, str] | None = None
    for start, end, symbol_id in ranges:
        if start <= line <= end:
            span = end - start
            if best is None or span < best[0]:
                best = (span, symbol_id)
    return best[1] if best is not None else None


# ---------------------------------------------------------------- imports


def _collect_imports(node: Node, data: bytes, parsed: TsParsed) -> None:
    if node.type == "import_statement":
        source = node.child_by_field_name("source")
        specifier = _string_value(source, data)
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if specifier and clause is not None:
            _import_clause(clause, specifier, data, parsed)
    elif node.type == "variable_declarator":
        _require_import(node, data, parsed)
    for child in node.named_children:
        _collect_imports(child, data, parsed)


def _import_clause(clause: Node, specifier: str, data: bytes, parsed: TsParsed) -> None:
    for child in clause.named_children:
        if child.type == "identifier":
            parsed.imports.append(TsImport(specifier=specifier, imported="default", local=_text(child, data)))
        elif child.type == "namespace_import":
            name_node = next((c for c in child.named_children if c.type == "identifier"), None)
            if name_node is not None:
                parsed.imports.append(TsImport(specifier=specifier, imported="*", local=_text(name_node, data)))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type != "import_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                if name_node is None:
                    continue
                imported = _text(name_node, data)
                local = _text(alias, data) if alias is not None else imported
                parsed.imports.append(TsImport(specifier=specifier, imported=imported, local=local))


def _require_import(node: Node, data: bytes, parsed: TsParsed) -> None:
    name_node = node.child_by_field_name("name")
    value = node.child_by_field_name("value")
    if name_node is None or name_node.type != "identifier" or value is None or value.type != "call_expression":
        return
    fn = value.child_by_field_name("function")
    args = value.child_by_field_name("arguments")
    if fn is None or _text(fn, data) != "require" or args is None or not args.named_children:
        return
    specifier = _string_value(args.named_children[0], data)
    if specifier:
        parsed.imports.append(TsImport(specifier=specifier, imported="*", local=_text(name_node, data)))


# ---------------------------------------------------------------- 类型


def _type_refs(node: Node, data: bytes, parsed: TsParsed, src: str, line: int, ignored: set[str]) -> None:
    if node.type in _SKIP_TYPE_NODES:
        return
    if node.type == "type_identifier":
        name = _text(node, data)
        if name and name not in _BUILTIN_TYPES and name not in _GLOBAL_TYPES and name not in ignored:
            parsed.refs.append(TsRef(src=src, line=line, name=name))
        return
    if node.type == "nested_type_identifier":
        name = _text(node, data).replace(" ", "")
        parsed.refs.append(TsRef(src=src, line=line, name=name))
        return
    for child in node.named_children:
        _type_refs(child, data, parsed, src, line, ignored)


def _type_name(node: Node | None, data: bytes) -> str | None:
    if node is None:
        return None
    if node.type in ("type_identifier", "nested_type_identifier", "identifier", "predefined_type"):
        return _text(node, data)
    if node.type == "generic_type":
        name_node = node.child_by_field_name("name")
        return _type_name(name_node, data)
    if node.type in ("union_type", "intersection_type"):
        for child in node.named_children:
            if child.type in ("type_identifier", "nested_type_identifier", "generic_type"):
                return _type_name(child, data)
        return None
    if node.type in ("array_type", "parenthesized_type"):
        return _type_name(node.named_children[0], data) if node.named_children else None
    if node.type == "type_annotation":
        return _type_name(node.named_children[0], data) if node.named_children else None
    return None


def _arity(params: Node | None, data: bytes) -> int:
    if params is None:
        return 0
    if params.type == "identifier":
        return 1
    count = 0
    for child in params.named_children:
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern")
            if pattern is not None and _text(pattern, data) == "this":
                continue
            count += 1
        elif child.type == "rest_pattern":
            continue
    return count


def _type_params(node: Node, data: bytes) -> set[str]:
    params = node.child_by_field_name("type_parameters")
    if params is None:
        return set()
    names: set[str] = set()
    for child in params.named_children:
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            names.add(_text(name_node, data))
    return names


def _return_type(node: Node, data: bytes) -> str | None:
    ret = node.child_by_field_name("return_type")
    return _type_name(ret, data)


# ---------------------------------------------------------------- 工具


def _suffix(path: str) -> str:
    lowered = path.lower()
    for suffix in _LANGUAGES:
        if lowered.endswith(suffix):
            return suffix
    return ".ts"


def _string_value(node: Node | None, data: bytes) -> str:
    if node is None:
        return ""
    if node.type == "string":
        fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return _text(fragment, data) if fragment is not None else _text(node, data).strip("'\"")
    return ""


def _text(node: Node | None, data: bytes) -> str:
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _span(node: Node) -> int:
    return int(node.end_point[0]) - int(node.start_point[0]) + 1


def relative_specifier_target(module: str, specifier: str) -> str | None:
    """把相对 import 解析成模块名（跨文件解析用）；包名返回 None。"""
    if not specifier.startswith("."):
        return None
    base = posixpath.dirname(module.replace(".", "/"))
    target = posixpath.normpath(posixpath.join(base, specifier))
    if target.startswith(".."):
        return None
    target = target.removeprefix("./")
    if target.endswith("/index"):
        target = target[: -len("/index")]
    return target.replace("/", ".")


def base_type_name(type_name: str | None) -> str | None:
    if not type_name:
        return None
    cleaned = re.sub(r"<.*>", "", type_name).strip()
    cleaned = cleaned.replace("[]", "").strip()
    return cleaned.rsplit(".", 1)[-1] or None
