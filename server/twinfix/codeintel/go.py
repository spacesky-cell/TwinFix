"""用 tree-sitter 解析 Go，产出符号、调用与类型引用的原始记录。

只做单文件语法级抽取，跨文件消歧交给 go_index。Go 没有编译期类型信息时，
只解析显式的参数/字段/变量类型与 import 别名；动态分派保持未解析，避免把猜测伪装成事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_go
from tree_sitter import Language, Node, Parser

from twinfix.codeintel.model import Symbol

_LANGUAGE = Language(tree_sitter_go.language())

_BUILTIN_TYPES = frozenset(
    {
        "any",
        "bool",
        "byte",
        "comparable",
        "complex64",
        "complex128",
        "error",
        "float32",
        "float64",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "rune",
        "string",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
    }
)
_BUILTIN_FUNCS = frozenset(
    {
        "append",
        "cap",
        "clear",
        "close",
        "complex",
        "copy",
        "delete",
        "imag",
        "len",
        "make",
        "max",
        "min",
        "new",
        "panic",
        "print",
        "println",
        "real",
        "recover",
    }
)
_PARAMETER_TYPES = frozenset({"parameter_declaration", "variadic_parameter_declaration"})


@dataclass(frozen=True)
class GoCall:
    src: str
    line: int
    name: str
    arity: int
    receiver: str | None = None
    receiver_is_call: bool = False
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GoRef:
    src: str
    line: int
    name: str
    kind: str = "type_ref"  # type_ref / constructs


@dataclass
class GoParsed:
    path: str
    package_name: str
    package_path: str
    module: str
    imports: dict[str, str] = field(default_factory=dict)
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[GoCall] = field(default_factory=list)
    refs: list[GoRef] = field(default_factory=list)
    embeds: list[tuple[str, str]] = field(default_factory=list)  # 类型 id -> 内嵌类型名


def parse_go(path: str, text: str, module: str = "") -> GoParsed:
    data = text.encode("utf-8")
    # Parser 不是线程安全的：build_go_index 可能在多个工作区线程里并发执行
    tree = Parser(_LANGUAGE).parse(data)
    root = tree.root_node
    package_name = _package_name(root, data)
    parsed = GoParsed(
        path=path,
        package_name=package_name,
        package_path=_package_path(path, module, package_name),
        module=module,
        imports=_imports(root, data),
    )
    for child in root.named_children:
        if child.type == "function_declaration":
            _add_function(child, data, parsed)
        elif child.type == "method_declaration":
            _add_method(child, data, parsed)
        elif child.type == "type_declaration":
            for spec in child.named_children:
                if spec.type == "type_spec":
                    _add_type(spec, data, parsed)
        elif child.type in ("const_declaration", "var_declaration"):
            _add_values(child, data, parsed)
    return parsed


# ---------------------------------------------------------------- 顶层声明


def _add_type(spec: Node, data: bytes, parsed: GoParsed) -> None:
    name_node = spec.child_by_field_name("name")
    type_node = spec.child_by_field_name("type")
    if name_node is None or type_node is None:
        return
    name = _text(name_node, data)
    type_id = _qualify(parsed.package_path, name)
    kind = "interface" if type_node.type == "interface_type" else "class"
    parsed.symbols.append(
        Symbol(
            id=type_id,
            name=name,
            qualified_name=type_id,
            kind=kind,
            path=parsed.path,
            start_line=_line(spec),
            end_line=_line(type_node),
            signature=_signature(spec, data),
            package=parsed.package_path,
        )
    )
    if type_node.type == "struct_type":
        _add_struct_fields(type_node, type_id, data, parsed)
    elif type_node.type == "interface_type":
        _add_interface_members(type_node, type_id, data, parsed)
    else:
        for ref in _type_refs(type_node, data):
            parsed.refs.append(GoRef(src=type_id, line=_line(type_node), name=ref))


def _add_struct_fields(type_node: Node, type_id: str, data: bytes, parsed: GoParsed) -> None:
    fields = next((c for c in type_node.named_children if c.type == "field_declaration_list"), None)
    if fields is None:
        return
    for decl in fields.named_children:
        if decl.type != "field_declaration":
            continue
        field_type = decl.child_by_field_name("type")
        if field_type is None:
            continue
        type_text = _type_text(field_type, data)
        names = [_text(n, data) for n in decl.children_by_field_name("name")]
        if not names:
            # 内嵌字段（Base / *Base / pkg.Base）提升方法和字段；记为 extends
            for ref in _type_refs(field_type, data):
                parsed.embeds.append((type_id, ref))
            continue
        for name in names:
            field_id = f"{type_id}#field:{name}"
            parsed.symbols.append(
                Symbol(
                    id=field_id,
                    name=name,
                    qualified_name=f"{type_id}.{name}",
                    kind="field",
                    path=parsed.path,
                    start_line=_line(decl),
                    end_line=_line(field_type),
                    signature=_signature(decl, data),
                    container=type_id,
                    package=parsed.package_path,
                    return_type=type_text,
                )
            )
            for ref in _type_refs(field_type, data):
                parsed.refs.append(GoRef(src=field_id, line=_line(decl), name=ref))


def _add_interface_members(type_node: Node, type_id: str, data: bytes, parsed: GoParsed) -> None:
    for child in type_node.named_children:
        if child.type == "method_elem":
            name = _text(child.child_by_field_name("name"), data)
            params = child.child_by_field_name("parameters")
            arity = _arity(params)
            method_id = f"{type_id}#{name}/{arity}"
            parsed.symbols.append(
                Symbol(
                    id=method_id,
                    name=name,
                    qualified_name=f"{type_id}.{name}",
                    kind="method",
                    path=parsed.path,
                    start_line=_line(child),
                    end_line=_line(child),
                    signature=_signature(child, data),
                    container=type_id,
                    package=parsed.package_path,
                    arity=arity,
                    return_type=_result_type(child, data),
                )
            )
            for ref in _parameter_refs(params, data):
                parsed.refs.append(GoRef(src=method_id, line=_line(child), name=ref))
            for ref in _result_refs(child, data):
                parsed.refs.append(GoRef(src=method_id, line=_line(child), name=ref))
        elif child.type == "type_elem":
            for ref in _type_refs(child, data):
                parsed.embeds.append((type_id, ref))


def _add_function(node: Node, data: bytes, parsed: GoParsed) -> None:
    name = _text(node.child_by_field_name("name"), data)
    params = node.child_by_field_name("parameters")
    arity = _arity(params)
    func_id = _qualify(parsed.package_path, f"{name}/{arity}")
    parsed.symbols.append(
        Symbol(
            id=func_id,
            name=name,
            qualified_name=_qualify(parsed.package_path, name),
            kind="function",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(node.child_by_field_name("body") or node),
            signature=_signature(node, data),
            package=parsed.package_path,
            arity=arity,
            return_type=_result_type(node, data),
        )
    )
    for ref in _parameter_refs(params, data):
        parsed.refs.append(GoRef(src=func_id, line=_line(node), name=ref))
    for ref in _result_refs(node, data):
        parsed.refs.append(GoRef(src=func_id, line=_line(node), name=ref))
    body = node.child_by_field_name("body")
    if body is not None:
        _walk_body(body, data, parsed, func_id, _parameter_vars(params, data))


def _add_method(node: Node, data: bytes, parsed: GoParsed) -> None:
    receiver = node.child_by_field_name("receiver")
    receiver_type, receiver_name = _receiver(receiver, data)
    if not receiver_type:
        return
    name = _text(node.child_by_field_name("name"), data)
    params = node.child_by_field_name("parameters")
    arity = _arity(params)
    container = _qualify(parsed.package_path, receiver_type)
    method_id = f"{container}#{name}/{arity}"
    parsed.symbols.append(
        Symbol(
            id=method_id,
            name=name,
            qualified_name=f"{container}.{name}",
            kind="method",
            path=parsed.path,
            start_line=_line(node),
            end_line=_line(node.child_by_field_name("body") or node),
            signature=_signature(node, data),
            container=container,
            package=parsed.package_path,
            arity=arity,
            return_type=_result_type(node, data),
        )
    )
    for ref in _parameter_refs(params, data):
        parsed.refs.append(GoRef(src=method_id, line=_line(node), name=ref))
    for ref in _result_refs(node, data):
        parsed.refs.append(GoRef(src=method_id, line=_line(node), name=ref))
    variables = _parameter_vars(params, data)
    if receiver_name:
        variables[receiver_name] = receiver_type
    body = node.child_by_field_name("body")
    if body is not None:
        _walk_body(body, data, parsed, method_id, variables)


def _add_values(decl: Node, data: bytes, parsed: GoParsed) -> None:
    kind = "constant" if decl.type == "const_declaration" else "variable"
    specs: list[Node] = []
    for child in decl.named_children:
        if child.type in ("const_spec", "var_spec"):
            specs.append(child)
        elif child.type in ("const_spec_list", "var_spec_list"):
            specs.extend(c for c in child.named_children if c.type in ("const_spec", "var_spec"))
    for spec in specs:
        type_node = spec.child_by_field_name("type")
        type_text = _type_text(type_node, data) if type_node is not None else ""
        for name_node in spec.children_by_field_name("name"):
            name = _text(name_node, data)
            own_id = _qualify(parsed.package_path, name)
            parsed.symbols.append(
                Symbol(
                    id=own_id,
                    name=name,
                    qualified_name=own_id,
                    kind=kind,
                    path=parsed.path,
                    start_line=_line(spec),
                    end_line=_line(spec),
                    signature=_signature(spec, data),
                    package=parsed.package_path,
                    return_type=type_text or None,
                )
            )
            if type_node is not None:
                for ref in _type_refs(type_node, data):
                    parsed.refs.append(GoRef(src=own_id, line=_line(spec), name=ref))


# ---------------------------------------------------------------- 函数体与调用


def _walk_body(node: Node, data: bytes, parsed: GoParsed, src: str, variables: dict[str, str]) -> None:
    for child in node.named_children:
        if child.type == "func_literal":
            body = child.child_by_field_name("body")
            if body is not None:
                inner = dict(variables)
                inner.update(_parameter_vars(child.child_by_field_name("parameters"), data))
                _walk_body(body, data, parsed, src, inner)
            continue
        if child.type == "short_var_declaration":
            _record_short_var(child, data, variables)
        elif child.type == "var_declaration":
            _record_var_decl(child, data, variables)
        if child.type == "call_expression":
            _record_call(child, data, parsed, src, variables)
        elif child.type == "composite_literal":
            _record_composite(child, data, parsed, src)
        _walk_body(child, data, parsed, src, variables)


def _record_short_var(node: Node, data: bytes, variables: dict[str, str]) -> None:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None:
        return
    names = [_text(n, data) for n in left.named_children]
    values = right.named_children
    if len(names) != 1 or len(values) != 1:
        return
    inferred = _infer_type(values[0], data)
    if inferred:
        variables[names[0]] = inferred


def _record_var_decl(node: Node, data: bytes, variables: dict[str, str]) -> None:
    specs: list[Node] = []
    for child in node.named_children:
        if child.type == "var_spec":
            specs.append(child)
        elif child.type == "var_spec_list":
            specs.extend(c for c in child.named_children if c.type == "var_spec")
    for spec in specs:
        names = [_text(n, data) for n in spec.children_by_field_name("name")]
        type_node = spec.child_by_field_name("type")
        if type_node is not None:
            type_text = _type_text(type_node, data)
            for name in names:
                variables[name] = type_text
            continue
        value = spec.child_by_field_name("value")
        if value is None or not value.named_children:
            continue
        inferred = _infer_type(value.named_children[0], data)
        if inferred:
            for name in names:
                variables[name] = inferred


def _infer_type(node: Node | None, data: bytes) -> str:
    node = _unwrap(node)
    if node is None:
        return ""
    if node.type == "composite_literal":
        return _construct_type(node, data)
    if node.type == "unary_expression":
        operand = node.child_by_field_name("operand")
        return _construct_type(operand, data) if operand is not None else ""
    if node.type == "call_expression":
        function = _unwrap(node.child_by_field_name("function"))
        if function is None:
            return ""
        name = _text(function, data)
        if name == "new":
            args = node.child_by_field_name("arguments")
            if args is not None and args.named_children:
                return _type_text(args.named_children[0], data)
            return ""
        return f"@{name}" if name else ""
    if node.type in ("type_identifier", "qualified_type"):
        return _text(node, data)
    return ""


def _record_call(node: Node, data: bytes, parsed: GoParsed, src: str, variables: dict[str, str]) -> None:
    function = _unwrap(node.child_by_field_name("function"))
    args = node.child_by_field_name("arguments")
    arity = len(args.named_children) if args is not None else 0
    if function is None:
        return
    if function.type == "identifier":
        name = _text(function, data)
        if name in _BUILTIN_FUNCS:
            if name == "new" and args is not None and args.named_children:
                for ref in _construct_refs(args.named_children[0], data):
                    parsed.refs.append(GoRef(src=src, line=_line(node), name=ref, kind="constructs"))
            return
        parsed.calls.append(GoCall(src=src, line=_line(node), name=name, arity=arity, variables=dict(variables)))
    elif function.type == "selector_expression":
        name = _text(function.child_by_field_name("field"), data)
        receiver, is_call = _receiver_text(function.child_by_field_name("operand"), data)
        if name and receiver:
            parsed.calls.append(
                GoCall(
                    src=src,
                    line=_line(node),
                    name=name,
                    arity=arity,
                    receiver=receiver,
                    receiver_is_call=is_call,
                    variables=dict(variables),
                )
            )


def _record_composite(node: Node, data: bytes, parsed: GoParsed, src: str) -> None:
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return
    for ref in _construct_refs(type_node, data):
        parsed.refs.append(GoRef(src=src, line=_line(node), name=ref, kind="constructs"))


def _receiver_text(node: Node | None, data: bytes) -> tuple[str, bool]:
    node = _unwrap(node)
    if node is None:
        return "", False
    if node.type == "identifier":
        return _text(node, data), False
    if node.type == "selector_expression":
        return _text(node, data), False
    if node.type == "call_expression":
        function = _unwrap(node.child_by_field_name("function"))
        return (_text(function, data), True) if function is not None else ("", False)
    return "", False


def _unwrap(node: Node | None) -> Node | None:
    while node is not None:
        if node.type == "parenthesized_expression":
            inner = next(iter(node.named_children), None)
            if inner is None or inner.type == "unary_expression":
                return None
            node = inner
            continue
        if node.type == "index_expression":
            # 泛型实例化 Map[int](...)：取被实例化的函数
            inner = node.child_by_field_name("operand")
            if inner is None:
                return None
            node = inner
            continue
        return node
    return None


# ---------------------------------------------------------------- 类型与变量


def _parameter_vars(params: Node | None, data: bytes) -> dict[str, str]:
    variables: dict[str, str] = {}
    if params is None:
        return variables
    for param in params.named_children:
        if param.type not in _PARAMETER_TYPES:
            continue
        type_text = _type_text(param.child_by_field_name("type"), data)
        for name_node in param.children_by_field_name("name"):
            variables[_text(name_node, data)] = type_text
    return variables


def _receiver(node: Node | None, data: bytes) -> tuple[str, str]:
    if node is None:
        return "", ""
    param = next((c for c in node.named_children if c.type in _PARAMETER_TYPES), None)
    if param is None:
        return "", ""
    receiver = base_type_name(_type_text(param.child_by_field_name("type"), data))
    return receiver, _text(param.child_by_field_name("name"), data)


def _arity(params: Node | None) -> int:
    if params is None:
        return 0
    count = 0
    for param in params.named_children:
        if param.type not in _PARAMETER_TYPES:
            continue
        names = list(param.children_by_field_name("name"))
        count += len(names) if names else 1
    return count


def _result_type(node: Node, data: bytes) -> str | None:
    result = node.child_by_field_name("result")
    if result is None:
        return None
    if result.type == "parameter_list":
        params = [c for c in result.named_children if c.type in _PARAMETER_TYPES]
        if len(params) != 1:
            return None
        return _type_text(params[0].child_by_field_name("type"), data) or None
    return _type_text(result, data) or None


def _parameter_refs(params: Node | None, data: bytes) -> list[str]:
    refs: list[str] = []
    if params is None:
        return refs
    for param in params.named_children:
        if param.type in _PARAMETER_TYPES:
            refs += _type_refs(param.child_by_field_name("type"), data)
    return refs


def _result_refs(node: Node, data: bytes) -> list[str]:
    result = node.child_by_field_name("result")
    return _type_refs(result, data) if result is not None else []


def _type_refs(node: Node | None, data: bytes) -> list[str]:
    """提取类型节点里可解析的命名类型；内建类型、匿名结构体不算引用。"""
    if node is None:
        return []
    if node.type == "type_identifier":
        name = _text(node, data)
        return [] if name in _BUILTIN_TYPES else [name]
    if node.type == "qualified_type":
        return [_text(node, data)]
    if node.type == "generic_type":
        refs: list[str] = []
        base = node.child_by_field_name("type")
        if base is not None:
            refs += _type_refs(base, data)
        args = node.child_by_field_name("type_arguments")
        if args is not None:
            refs += _type_refs(args, data)
        return refs
    if node.type in ("interface_type", "struct_type", "type_parameter_list"):
        return []
    refs = []
    for child in node.named_children:
        refs += _type_refs(child, data)
    return refs


def _construct_type(node: Node | None, data: bytes) -> str:
    refs = _construct_refs(node, data)
    return refs[0] if refs else ""


def _construct_refs(node: Node | None, data: bytes) -> list[str]:
    node = _unwrap(node)
    if node is None:
        return []
    if node.type == "composite_literal":
        node = node.child_by_field_name("type")
        if node is None:
            return []
    if node.type == "generic_type":
        node = node.child_by_field_name("type")
        if node is None:
            return []
    if node.type in ("type_identifier", "qualified_type"):
        return [_text(node, data)]
    return []


def base_type_name(text: str) -> str:
    """把 `*pkg.Type`、`[]Type`、`map[K]V`、`Box[T]` 归一成可解析的类型名。"""
    raw = text.strip()
    while raw:
        if raw.startswith("*"):
            raw = raw[1:].lstrip()
        elif raw.startswith("[]"):
            raw = raw[2:].lstrip()
        elif raw.startswith("..."):
            raw = raw[3:].lstrip()
        elif re.match(r"^\[\d+\]", raw):
            raw = raw[raw.index("]") + 1 :].lstrip()
        else:
            break
    if raw.startswith("map["):
        depth = 0
        for index, char in enumerate(raw):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return base_type_name(raw[index + 1 :])
        return ""
    for prefix in ("chan ", "<-chan ", "chan<- "):
        if raw.startswith(prefix):
            return base_type_name(raw[len(prefix) :])
    if raw.startswith(("func(", "interface{", "struct{")):
        return ""
    if "[" in raw:
        raw = raw.split("[", 1)[0]
    return raw.split()[0].strip() if raw else ""


# ---------------------------------------------------------------- 工具


def _package_name(root: Node, data: bytes) -> str:
    clause = next((c for c in root.named_children if c.type == "package_clause"), None)
    if clause is None or not clause.named_children:
        return ""
    return _text(clause.named_children[0], data)


def _package_path(path: str, module: str, package_name: str) -> str:
    normalized = path.replace("\\", "/").removeprefix("./")
    directory = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    if module:
        module = module.rstrip("/")
        return f"{module}/{directory}" if directory else module
    return directory or package_name


def _imports(root: Node, data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        specs: list[Node] = []
        for node in child.named_children:
            if node.type == "import_spec_list":
                specs.extend(n for n in node.named_children if n.type == "import_spec")
            elif node.type == "import_spec":
                specs.append(node)
        for spec in specs:
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            raw = _text(path_node, data).strip('"`')
            alias = _text(spec.child_by_field_name("name"), data)
            if alias in ("_", "."):
                continue
            result[alias or raw.rsplit("/", 1)[-1]] = raw
    return result


def _qualify(package_path: str, name: str) -> str:
    return f"{package_path}.{name}" if package_path else name


def _signature(node: Node, data: bytes) -> str:
    raw = _text(node, data).split("{", 1)[0]
    return " ".join(raw.split())[:200]


def _type_text(node: Node | None, data: bytes) -> str:
    return " ".join(_text(node, data).split())[:120] if node is not None else ""


def _text(node: Node | None, data: bytes) -> str:
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1
