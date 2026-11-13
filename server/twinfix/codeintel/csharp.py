"""用 tree-sitter 解析 C#，产出符号、调用与类型引用。

只解析代码里明确写出的内容：类/接口/结构体/记录/枚举/委托、方法、构造器、属性、
字段与枚举成员、using（含 alias/static/global）、继承与接口实现、参数/返回/字段类型、
直接调用与对象创建。跨文件消歧交给 csharp_index；动态分派、未标注类型的接收者保持
unresolved，不把猜测伪装成事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_c_sharp
from tree_sitter import Language, Node, Parser

from twinfix.codeintel.model import Symbol

_CS = Language(tree_sitter_c_sharp.language())

_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "struct_declaration": "struct",
    "record_declaration": "record",
    "delegate_declaration": "delegate",
}
_METHOD_KINDS = frozenset({"method_declaration", "constructor_declaration", "destructor_declaration"})
_FIELD_KINDS = frozenset({"field_declaration", "event_field_declaration"})
_BUILTIN_TYPES = frozenset(
    {
        "bool",
        "byte",
        "char",
        "decimal",
        "double",
        "dynamic",
        "float",
        "int",
        "long",
        "nint",
        "nuint",
        "object",
        "sbyte",
        "short",
        "string",
        "uint",
        "ulong",
        "ushort",
        "void",
    }
)


@dataclass
class CsUsing:
    target: str
    alias: str | None = None
    is_static: bool = False
    is_global: bool = False


@dataclass
class CsCall:
    src: str
    line: int
    name: str
    receiver: str | None = None
    receiver_is_call: bool = False
    is_new: bool = False
    arity: int | None = None
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class CsRef:
    src: str
    line: int
    name: str
    kind: str = "type_ref"


@dataclass
class CsParsed:
    path: str
    namespace: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[CsCall] = field(default_factory=list)
    refs: list[CsRef] = field(default_factory=list)
    usings: list[CsUsing] = field(default_factory=list)
    bases: dict[str, list[str]] = field(default_factory=dict)
    variables: dict[str, dict[str, str]] = field(default_factory=dict)


def parse_csharp(path: str, text: str) -> CsParsed:
    data = text.encode("utf-8")
    tree = Parser(_CS).parse(data)
    parsed = CsParsed(path=path)
    _collect_declarations(tree.root_node, data, parsed, namespace="", container=None)
    _collect_references(tree.root_node, data, parsed)
    return parsed


# ---------------------------------------------------------------- 声明与符号


def _collect_declarations(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str | None,
) -> None:
    current = namespace
    for child in node.named_children:
        if child.type == "using_directive":
            _add_using(child, data, parsed)
        elif child.type == "file_scoped_namespace_declaration":
            name = _text(child.child_by_field_name("name"), data)
            if name:
                current = _join_namespace(namespace, name)
                parsed.namespace = current
        elif child.type == "namespace_declaration":
            name = _text(child.child_by_field_name("name"), data)
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_declarations(
                    body,
                    data,
                    parsed,
                    namespace=_join_namespace(current, name),
                    container=None,
                )
        elif child.type in _TYPE_KINDS:
            _add_type(child, data, parsed, namespace=current, container=container)


def _add_type(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str | None,
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    if not name:
        return
    own_id = f"{container}.{name}" if container else f"{namespace}.{name}" if namespace else name
    kind = _TYPE_KINDS[node.type]
    if node.type == "record_declaration" and re.match(r"^(?:[\w\s]*\brecord\s+struct)\b", _text(node, data)):
        kind = "struct"
    body = node.child_by_field_name("body")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=own_id,
            kind=kind,
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=container,
            package=namespace,
        )
    )
    ignored = _type_parameters(node, data)
    bases = _base_names(node, data)
    if bases:
        parsed.bases[own_id] = bases
    params = _parameters(node)
    if node.type == "record_declaration" and params is not None:
        _record_parameters(params, data, parsed, type_id=own_id, ignored=ignored)
    if node.type == "delegate_declaration":
        _signature_refs(node, data, parsed, own_id, ignored, returns_field="type")
    if body is None:
        return
    if node.type == "enum_declaration":
        _collect_enum_members(body, data, parsed, enum_id=own_id)
    else:
        for child in body.named_children:
            if child.type in (_METHOD_KINDS | _FIELD_KINDS) or child.type in {
                "property_declaration",
                "indexer_declaration",
                "event_declaration",
            }:
                _add_member(
                    child,
                    data,
                    parsed,
                    namespace=namespace,
                    container=own_id,
                    ignored=ignored,
                )
            elif child.type in _TYPE_KINDS:
                _add_type(child, data, parsed, namespace=namespace, container=own_id)


def _add_member(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str,
    ignored: set[str],
) -> None:
    if node.type in _METHOD_KINDS:
        _add_method(node, data, parsed, namespace=namespace, container=container, ignored=ignored)
    elif node.type in _FIELD_KINDS:
        _add_fields(node, data, parsed, namespace=namespace, container=container, ignored=ignored)
    elif node.type in ("property_declaration", "indexer_declaration", "event_declaration"):
        _add_property(node, data, parsed, namespace=namespace, container=container, ignored=ignored)


def _add_method(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str,
    ignored: set[str],
) -> None:
    name_node = node.child_by_field_name("name")
    params = _parameters(node)
    arity = _arity(params)
    if name_node is not None:
        name = _text(name_node, data)
        qualified = f"{container}.{name}"
    elif node.type == "destructor_declaration":
        name = f"~{container.rsplit('.', 1)[-1]}"
        qualified = f"{container}.{name}"
    else:
        name = container.rsplit(".", 1)[-1]
        qualified = f"{container}.{name}"
    is_ctor = node.type == "constructor_declaration"
    own_id = f"{container}#{name}/{arity}"
    body = node.child_by_field_name("body")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=qualified,
            kind="constructor" if is_ctor else "method",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=container,
            package=namespace,
            arity=arity,
            return_type=None if is_ctor else _type_name(node.child_by_field_name("returns"), data),
        )
    )
    ignored = ignored | _type_parameters(node, data)
    _signature_refs(node, data, parsed, own_id, ignored, returns_field="returns")


def _add_fields(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str,
    ignored: set[str],
) -> None:
    declaration = next((c for c in node.named_children if c.type == "variable_declaration"), None)
    if declaration is None:
        return
    type_node = declaration.child_by_field_name("type")
    type_name = _type_name(type_node, data)
    for declarator in declaration.named_children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, data)
        if not name:
            continue
        own_id = f"{container}#field:{name}"
        parsed.symbols.append(
            Symbol(
                id=own_id,
                name=name,
                qualified_name=f"{container}.{name}",
                kind="field",
                path=parsed.path,
                start_line=_line(declarator),
                end_line=_line(declarator),
                signature=_text(declarator, data).splitlines()[0][:160],
                container=container,
                package=namespace,
                return_type=type_name,
            )
        )
        if type_node is not None:
            _type_refs(type_node, data, parsed, own_id, _line(declarator), ignored)


def _add_property(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    *,
    namespace: str,
    container: str,
    ignored: set[str],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    if not name:
        return
    own_id = f"{container}#field:{name}"
    type_node = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    if body is None:
        body = node.child_by_field_name("accessors")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=f"{container}.{name}",
            kind="field",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(body) + _span(body) - 1 if body is not None else _line(node),
            signature=_text(node, data).splitlines()[0][:160],
            container=container,
            package=namespace,
            return_type=_type_name(type_node, data),
        )
    )
    if type_node is not None:
        _type_refs(type_node, data, parsed, own_id, _line(node), ignored)


def _collect_enum_members(body: Node, data: bytes, parsed: CsParsed, *, enum_id: str) -> None:
    for child in body.named_children:
        if child.type != "enum_member_declaration":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, data)
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
                package=parsed.namespace,
            )
        )


def _record_parameters(params: Node, data: bytes, parsed: CsParsed, *, type_id: str, ignored: set[str]) -> None:
    """record 的主构造参数同时是位置属性；按字段建符号，便于引用查询。"""
    for param in params.named_children:
        if param.type != "parameter":
            continue
        name_node = param.child_by_field_name("name")
        type_node = param.child_by_field_name("type")
        if name_node is None:
            continue
        name = _text(name_node, data)
        if not name:
            continue
        own_id = f"{type_id}#field:{name}"
        parsed.symbols.append(
            Symbol(
                id=own_id,
                name=name,
                qualified_name=f"{type_id}.{name}",
                kind="field",
                path=parsed.path,
                start_line=_line(param),
                end_line=_line(param),
                signature=_text(param, data)[:160],
                container=type_id,
                package=parsed.namespace,
                return_type=_type_name(type_node, data),
            )
        )
        if type_node is not None:
            _type_refs(type_node, data, parsed, own_id, _line(param), ignored)


def _signature_refs(
    node: Node,
    data: bytes,
    parsed: CsParsed,
    src: str,
    ignored: set[str],
    *,
    returns_field: str,
) -> None:
    variables = parsed.variables.setdefault(src, {})
    params = _parameters(node)
    if params is not None:
        for param in params.named_children:
            if param.type != "parameter":
                continue
            type_node = param.child_by_field_name("type")
            name_node = param.child_by_field_name("name")
            if type_node is not None:
                _type_refs(type_node, data, parsed, src, _line(param), ignored)
                type_name = _type_name(type_node, data)
                if name_node is not None and type_name:
                    variables[_text(name_node, data)] = type_name
    ret = node.child_by_field_name(returns_field)
    if ret is not None:
        _type_refs(ret, data, parsed, src, _line(node), ignored)


# ---------------------------------------------------------------- 引用与调用


def _collect_references(root: Node, data: bytes, parsed: CsParsed) -> None:
    ranges = [
        (s.start_line, s.end_line, s.id) for s in parsed.symbols if s.kind in ("function", "method", "constructor")
    ]
    type_ranges = [(s.start_line, s.end_line, s.id) for s in parsed.symbols if s.is_type]

    field_types: dict[str, dict[str, str]] = {}
    for symbol in parsed.symbols:
        if symbol.kind == "field" and symbol.container and symbol.return_type:
            field_types.setdefault(symbol.container, {})[symbol.name] = symbol.return_type

    def variables_for(src: str) -> dict[str, str]:
        source = next((s for s in parsed.symbols if s.id == src), None)
        container = source.container if source is not None else None
        # 方法体内直接用字段接收者（this 省略）时，用字段声明类型补全
        return {**(field_types.get(container or "", {})), **parsed.variables.get(src, {})}

    def owner(line: int) -> str | None:
        return _innermost(ranges, line) or _innermost(type_ranges, line)

    def walk(node: Node) -> None:
        if node.type == "invocation_expression":
            src = owner(_line(node))
            if src is not None:
                call = _call(node, data, src, variables_for(src))
                if call is not None:
                    parsed.calls.append(call)
        elif node.type == "object_creation_expression":
            src = owner(_line(node))
            if src is not None:
                call = _construction(node, data, src, variables_for(src))
                if call is not None:
                    parsed.calls.append(call)
        elif node.type == "variable_declaration":
            src = owner(_line(node))
            if src is not None:
                _local_variables(node, data, parsed, src)
        for child in node.named_children:
            walk(child)

    walk(root)


def _local_variables(node: Node, data: bytes, parsed: CsParsed, src: str) -> None:
    type_node = node.child_by_field_name("type")
    declared = _type_name(type_node, data)
    for declarator in node.named_children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, data)
        type_name = declared
        if type_node is not None and type_node.type in ("implicit_type", "var"):
            type_name = None
        if type_name is None:
            for child in declarator.named_children:
                if child.type == "object_creation_expression":
                    type_name = _type_name(child.child_by_field_name("type"), data)
                    break
        if type_name:
            parsed.variables.setdefault(src, {})[name] = type_name


def _call(node: Node, data: bytes, src: str, variables: dict[str, str]) -> CsCall | None:
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    arity = _argument_count(node.child_by_field_name("arguments"))
    if fn.type == "identifier":
        return CsCall(src=src, line=_line(node), name=_text(fn, data), arity=arity, variables=dict(variables))
    if fn.type == "generic_name":
        return CsCall(
            src=src,
            line=_line(node),
            name=_generic_name(fn, data),
            arity=arity,
            variables=dict(variables),
        )
    if fn.type != "member_access_expression":
        return None
    name_node = fn.child_by_field_name("name")
    obj = fn.child_by_field_name("expression")
    if name_node is None:
        return None
    name = _text(name_node, data)
    if name_node.type == "generic_name":
        name = _generic_name(name_node, data)
    receiver: str | None = None
    receiver_is_call = False
    if obj is not None:
        if obj.type in ("this", "base"):
            receiver = obj.type
        elif obj.type == "identifier":
            receiver = _text(obj, data)
        elif obj.type == "object_creation_expression":
            receiver = _type_name(obj.child_by_field_name("type"), data)
            receiver_is_call = True
        elif obj.type == "invocation_expression":
            inner = obj.child_by_field_name("function")
            if inner is not None and inner.type == "identifier":
                receiver = _text(inner, data)
                receiver_is_call = True
        elif obj.type in ("qualified_name", "generic_name", "predefined_type"):
            receiver = _type_name(obj, data)
    return CsCall(
        src=src,
        line=_line(node),
        name=name,
        receiver=receiver,
        receiver_is_call=receiver_is_call,
        arity=arity,
        variables=dict(variables),
    )


def _construction(node: Node, data: bytes, src: str, variables: dict[str, str]) -> CsCall | None:
    type_node = node.child_by_field_name("type")
    name = _type_name(type_node, data)
    if not name:
        return None
    return CsCall(
        src=src,
        line=_line(node),
        name=name.rsplit(".", 1)[-1],
        arity=_argument_count(node.child_by_field_name("arguments")),
        is_new=True,
        variables=dict(variables),
    )


def _argument_count(args: Node | None) -> int:
    if args is None:
        return 0
    return len([c for c in args.named_children if c.type == "argument"])


def _innermost(ranges: list[tuple[int, int, str]], line: int) -> str | None:
    best: tuple[int, str] | None = None
    for start, end, symbol_id in ranges:
        if start <= line <= end:
            span = end - start
            if best is None or span < best[0]:
                best = (span, symbol_id)
    return best[1] if best is not None else None


# ---------------------------------------------------------------- using / 类型


def _add_using(node: Node, data: bytes, parsed: CsParsed) -> None:
    text = _text(node, data)
    body = re.sub(r"^(?:global\s+)?using\s+", "", text).rstrip(";").strip()
    is_static = body.startswith("static ")
    if is_static:
        body = body[len("static ") :].strip()
    alias: str | None = None
    if "=" in body:
        alias, _, body = body.partition("=")
        alias = alias.strip()
        body = body.strip()
    if not body:
        return
    parsed.usings.append(
        CsUsing(
            target=body,
            alias=alias,
            is_static=is_static,
            is_global=text.startswith("global "),
        )
    )


def _base_names(node: Node, data: bytes) -> list[str]:
    base_list = next((c for c in node.named_children if c.type == "base_list"), None)
    if base_list is None:
        return []
    names: list[str] = []
    for child in base_list.named_children:
        if child.type == "primary_constructor_base_type":
            name = _type_name(child.child_by_field_name("type"), data)
        else:
            name = _type_name(child, data)
        if name and name not in names:
            names.append(name)
    return names


def _type_refs(node: Node, data: bytes, parsed: CsParsed, src: str, line: int, ignored: set[str]) -> None:
    if node.type in ("predefined_type", "implicit_type"):
        return
    if node.type == "identifier":
        name = _text(node, data)
        if name and name not in ignored and name not in _BUILTIN_TYPES:
            parsed.refs.append(CsRef(src=src, line=line, name=name))
        return
    if node.type in ("qualified_name", "alias_qualified_name"):
        name = _text(node, data)
        if name and name not in ignored and name not in _BUILTIN_TYPES:
            parsed.refs.append(CsRef(src=src, line=line, name=name))
        return
    if node.type == "generic_name":
        name = _generic_name(node, data)
        if name and name not in ignored and name not in _BUILTIN_TYPES:
            parsed.refs.append(CsRef(src=src, line=line, name=name))
        for child in node.named_children:
            if child.type == "type_argument_list":
                _type_refs(child, data, parsed, src, line, ignored)
        return
    for child in node.named_children:
        _type_refs(child, data, parsed, src, line, ignored)


def _type_name(node: Node | None, data: bytes) -> str | None:
    if node is None:
        return None
    if node.type in ("identifier", "predefined_type"):
        return _text(node, data)
    if node.type == "qualified_name":
        return _text(node, data)
    if node.type == "generic_name":
        return _generic_name(node, data)
    if node.type in ("nullable_type", "array_type", "pointer_type"):
        return _type_name(node.child_by_field_name("type") or _first_named(node), data)
    return None


def _generic_name(node: Node, data: bytes) -> str:
    for child in node.named_children:
        if child.type == "identifier":
            return _text(child, data)
    return _text(node, data).split("<", 1)[0]


def _type_parameters(node: Node, data: bytes) -> set[str]:
    params = next((c for c in node.named_children if c.type == "type_parameter_list"), None)
    if params is None:
        return set()
    names: set[str] = set()
    for child in params.named_children:
        if child.type != "type_parameter":
            continue
        name_node = child.child_by_field_name("name")
        names.add(_text(name_node if name_node is not None else child, data))
    return names


def base_type_name(type_name: str | None) -> str | None:
    if not type_name:
        return None
    cleaned = re.sub(r"<.*>", "", type_name).strip()
    cleaned = cleaned.replace("?", "").replace("[]", "").strip()
    return cleaned.rsplit(".", 1)[-1] or None


# ---------------------------------------------------------------- 工具


def _parameters(node: Node) -> Node | None:
    """方法用 parameters 字段；record / delegate 的 parameter_list 是直接子节点。"""
    params = node.child_by_field_name("parameters")
    if params is not None:
        return params
    return next((c for c in node.named_children if c.type == "parameter_list"), None)


def _arity(params: Node | None) -> int:
    if params is None:
        return 0
    return len([c for c in params.named_children if c.type == "parameter"])


def _first_named(node: Node) -> Node | None:
    return node.named_children[0] if node.named_children else None


def _join_namespace(prefix: str, name: str) -> str:
    name = name.strip().lstrip(".")
    if not prefix:
        return name
    if name.startswith(prefix + "."):
        return name
    return f"{prefix}.{name}"


def _text(node: Node | None, data: bytes) -> str:
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _span(node: Node) -> int:
    return int(node.end_point[0]) - int(node.start_point[0]) + 1
