"""用 tree-sitter 解析 Java，产出与语言无关的符号/引用原始记录。

这里只做单文件语法级抽取，跨文件的类型与方法解析在 index.py 里完成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from twinfix.codeintel.model import ImportSet, Symbol

_LANGUAGE = Language(tree_sitter_java.language())

TYPE_NODES: dict[str, str] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
METHOD_NODES = frozenset({"method_declaration", "constructor_declaration"})
_PRIMITIVES = frozenset({"boolean", "byte", "char", "double", "float", "int", "long", "short", "void", "var"})
_ANNOTATION_HEAD = re.compile(r"^(?:@[\w.]+(?:\([^)]*\))?\s+)+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


@dataclass(frozen=True)
class RawCall:
    src: str
    name: str
    arity: int
    receiver: str | None
    receiver_type: str | None
    line: int
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RawRef:
    src: str
    type_name: str
    kind: str  # constructs / type_ref / extends / implements
    line: int


@dataclass
class ParsedFile:
    path: str
    package: str
    imports: ImportSet
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    refs: list[RawRef] = field(default_factory=list)


def parse_java(path: str, text: str) -> ParsedFile:
    source = text.encode("utf-8")
    tree = Parser(_LANGUAGE).parse(source)
    parsed = ParsedFile(path=path, package=_package(tree.root_node, source), imports=_imports(tree.root_node, source))
    _process_body(tree.root_node, [], parsed, source)
    return parsed


def _process_body(node: Node, containers: list[tuple[str, str]], parsed: ParsedFile, source: bytes) -> None:
    for child in node.named_children:
        if child.type in TYPE_NODES:
            _process_type(child, containers, parsed, source)
        elif child.type in METHOD_NODES and containers:
            _process_callable(child, containers, parsed, source, {})


def _process_type(node: Node, containers: list[tuple[str, str]], parsed: ParsedFile, source: bytes) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, source)
    chain = [c[0] for c in containers] + [name]
    fqn = ".".join([parsed.package, *chain]) if parsed.package else ".".join(chain)
    symbol = Symbol(
        id=fqn,
        name=name,
        qualified_name=fqn,
        kind=TYPE_NODES[node.type],
        path=parsed.path,
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=_signature(node, source),
        container=containers[-1][1] if containers else None,
        package=parsed.package,
    )
    parsed.symbols.append(symbol)
    _super_refs(node, symbol, parsed, source)
    if node.type == "record_declaration":
        _record_accessors(node, symbol, parsed, source)

    body = _body(node)
    if body is None:
        return
    field_types: dict[str, str] = {}
    for child in body.named_children:
        if child.type == "field_declaration":
            type_name = _decl_type(child, source)
            if type_name:
                for field_name in _declarator_names(child, source):
                    field_types[field_name] = type_name

    for child in body.named_children:
        if child.type == "field_declaration":
            _process_field(child, symbol, parsed, source, field_types)
        elif child.type in METHOD_NODES:
            _process_callable(child, [*containers, (name, fqn)], parsed, source, field_types)
        elif child.type in TYPE_NODES:
            _process_type(child, [*containers, (name, fqn)], parsed, source)
        elif child.type == "enum_constant":
            _process_enum_constant(child, symbol, parsed, source)


def _record_accessors(node: Node, owner: Symbol, parsed: ParsedFile, source: bytes) -> None:
    """record 的分量会生成同名访问器；不索引它，`req.quantity()` 这类调用永远解析不到。"""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return
    for param in params_node.named_children:
        if param.type not in ("formal_parameter", "spread_parameter"):
            continue
        type_name = _field_text(param, "type", source)
        name = _field_text(param, "name", source)
        if not type_name or not name:
            continue
        parsed.symbols.append(
            Symbol(
                id=f"{owner.id}#{name}/0",
                name=name,
                qualified_name=f"{owner.qualified_name}.{name}",
                kind="method",
                path=parsed.path,
                start_line=param.start_point.row + 1,
                end_line=param.end_point.row + 1,
                signature=f"public {type_name} {name}()",
                container=owner.qualified_name,
                package=parsed.package,
                arity=0,
                return_type=type_name,
            )
        )
        parsed.refs.append(RawRef(f"{owner.id}#{name}/0", type_name, "type_ref", param.start_point.row + 1))


def _process_field(node: Node, owner: Symbol, parsed: ParsedFile, source: bytes, owner_fields: dict[str, str]) -> None:
    type_name = _decl_type(node, source)
    for declarator in node.named_children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None:
            continue
        name = _text(name_node, source)
        symbol = Symbol(
            id=f"{owner.id}#field:{name}",
            name=name,
            qualified_name=f"{owner.qualified_name}.{name}",
            kind="field",
            path=parsed.path,
            start_line=declarator.start_point.row + 1,
            end_line=declarator.end_point.row + 1,
            signature=_signature(node, source),
            container=owner.qualified_name,
            package=parsed.package,
            return_type=type_name,
        )
        parsed.symbols.append(symbol)
        if type_name:
            parsed.refs.append(RawRef(symbol.id, type_name, "type_ref", symbol.start_line))
        value = declarator.child_by_field_name("value")
        if value is not None:
            variables = dict(owner_fields)
            _collect_calls(value, symbol.id, variables, parsed, source)


def _process_enum_constant(node: Node, owner: Symbol, parsed: ParsedFile, source: bytes) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, source)
    parsed.symbols.append(
        Symbol(
            id=f"{owner.id}#enum:{name}",
            name=name,
            qualified_name=f"{owner.qualified_name}.{name}",
            kind="enum_constant",
            path=parsed.path,
            start_line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
            signature=_text(node, source).split("\n")[0][:200],
            container=owner.qualified_name,
            package=parsed.package,
        )
    )


def _process_callable(
    node: Node,
    containers: list[tuple[str, str]],
    parsed: ParsedFile,
    source: bytes,
    class_fields: dict[str, str],
) -> None:
    container = containers[-1][1]
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, source)
    is_ctor = node.type == "constructor_declaration"
    params_node = node.child_by_field_name("parameters")
    params = [
        c
        for c in (params_node.named_children if params_node else [])
        if c.type in ("formal_parameter", "spread_parameter")
    ]
    arity = len(params)
    return_type = None if is_ctor else _field_text(node, "type", source)
    symbol_id = f"{container}#{'<init>' if is_ctor else name}/{arity}"
    signature = _signature(node, source)
    symbol = Symbol(
        id=symbol_id,
        name=name,
        qualified_name=f"{container}.{name}",
        kind="constructor" if is_ctor else "method",
        path=parsed.path,
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=signature,
        container=container,
        package=parsed.package,
        arity=arity,
        return_type=return_type,
        params=tuple(_param_text(p, source) for p in params),
    )
    parsed.symbols.append(symbol)
    if return_type:
        parsed.refs.append(RawRef(symbol.id, return_type, "type_ref", symbol.start_line))
    variables = dict(class_fields)
    for param in params:
        ptype = _field_text(param, "type", source)
        pname = _field_text(param, "name", source)
        if ptype and pname:
            variables[pname] = ptype
            parsed.refs.append(RawRef(symbol.id, ptype, "type_ref", param.start_point.row + 1))
    body = node.child_by_field_name("body")
    if body is not None:
        _collect_local_vars(body, variables, parsed, symbol.id, source)
        _collect_calls(body, symbol.id, variables, parsed, source)


def _collect_local_vars(node: Node, variables: dict[str, str], parsed: ParsedFile, src: str, source: bytes) -> None:
    if node.type in TYPE_NODES:
        return
    if node.type == "local_variable_declaration":
        type_name = _decl_type(node, source)
        if type_name:
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is not None:
                    variables[_text(name_node, source)] = type_name
            parsed.refs.append(RawRef(src, type_name, "type_ref", node.start_point.row + 1))
    elif node.type == "enhanced_for_statement":
        type_name = _field_text(node, "type", source)
        name_node = node.child_by_field_name("name")
        if type_name and name_node is not None:
            variables[_text(name_node, source)] = type_name
            parsed.refs.append(RawRef(src, type_name, "type_ref", node.start_point.row + 1))
    for child in node.named_children:
        _collect_local_vars(child, variables, parsed, src, source)


def _collect_calls(node: Node, src: str, variables: dict[str, str], parsed: ParsedFile, source: bytes) -> None:
    if node.type in TYPE_NODES:
        return
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = _text(name_node, source)
            obj = node.child_by_field_name("object")
            receiver = _text(obj, source) if obj is not None else None
            receiver_type = None
            if receiver is not None and obj is not None and obj.type == "identifier":
                receiver_type = variables.get(receiver)
            args = node.child_by_field_name("arguments")
            arity = len(args.named_children) if args is not None else 0
            parsed.calls.append(
                RawCall(
                    src=src,
                    name=name,
                    arity=arity,
                    receiver=receiver,
                    receiver_type=receiver_type,
                    line=node.start_point.row + 1,
                    variables=dict(variables),
                )
            )
    elif node.type == "object_creation_expression":
        type_name = _field_text(node, "type", source)
        if type_name:
            line = node.start_point.row + 1
            parsed.refs.append(RawRef(src, type_name, "constructs", line))
            args = node.child_by_field_name("arguments")
            parsed.calls.append(
                RawCall(
                    src=src,
                    name="<init>",
                    arity=len(args.named_children) if args is not None else 0,
                    receiver=type_name,
                    receiver_type=type_name,
                    line=line,
                    variables=dict(variables),
                )
            )
    elif node.type == "catch_type":
        parsed.refs.append(RawRef(src, _text(node, source), "type_ref", node.start_point.row + 1))
    elif node.type == "instanceof_expression":
        type_name = _field_text(node, "right", source)
        if type_name:
            parsed.refs.append(RawRef(src, type_name, "type_ref", node.start_point.row + 1))
    elif node.type == "cast_expression":
        type_name = _field_text(node, "type", source)
        if type_name:
            parsed.refs.append(RawRef(src, type_name, "type_ref", node.start_point.row + 1))
    for child in node.named_children:
        _collect_calls(child, src, variables, parsed, source)


def _super_refs(node: Node, symbol: Symbol, parsed: ParsedFile, source: bytes) -> None:
    for child in node.named_children:
        if child.type == "superclass":
            if child.named_children:
                parsed.refs.append(
                    RawRef(symbol.id, _text(child.named_children[-1], source), "extends", child.start_point.row + 1)
                )
        elif child.type == "super_interfaces":
            for type_node in child.named_children:
                if type_node.type == "type_list":
                    for item in type_node.named_children:
                        parsed.refs.append(
                            RawRef(symbol.id, _text(item, source), "implements", item.start_point.row + 1)
                        )
                else:
                    parsed.refs.append(
                        RawRef(symbol.id, _text(type_node, source), "implements", type_node.start_point.row + 1)
                    )


def _body(node: Node) -> Node | None:
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.named_children:
        if child.type in ("class_body", "interface_body", "enum_body", "record_declaration_body"):
            return child
    return None


def _decl_type(node: Node, source: bytes) -> str | None:
    return _field_text(node, "type", source)


def _declarator_names(node: Node, source: bytes) -> list[str]:
    names = []
    for child in node.named_children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node, source))
    return names


def _param_text(node: Node, source: bytes) -> str:
    return " ".join(_text(node, source).split())


def _package(root: Node, source: bytes) -> str:
    for child in root.named_children:
        if child.type == "package_declaration":
            return _text(child, source).removeprefix("package").removesuffix(";").strip()
    return ""


def _imports(root: Node, source: bytes) -> ImportSet:
    types: dict[str, str] = {}
    statics: dict[str, str] = {}
    wildcards: list[str] = []
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        raw = _text(child, source).removeprefix("import").removesuffix(";").strip()
        static = raw.startswith("static ")
        path = raw.removeprefix("static ").strip()
        if path.endswith(".*"):
            wildcards.append(path[:-2])
        elif static:
            owner, _, member = path.rpartition(".")
            statics[member] = owner
        else:
            types[path.rsplit(".", 1)[-1]] = path
    return ImportSet(types=types, statics=statics, wildcards=tuple(wildcards))


def _field_text(node: Node, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return _text(child, source) if child is not None else None


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature(node: Node, source: bytes) -> str:
    raw = _text(node, source).split("{", 1)[0]
    head = " ".join(raw.split())
    head = _ANNOTATION_HEAD.sub("", head)
    return head[:300]


def simple_type_names(type_text: str) -> list[str]:
    """把 `Map<String, com.acme.Foo[]>` 拆成可用于解析的简单类型名。"""
    names: list[str] = []
    for match in _IDENT.findall(type_text):
        simple = match.rsplit(".", 1)[-1]
        if simple and simple not in _PRIMITIVES and simple not in names:
            names.append(simple)
    return names
