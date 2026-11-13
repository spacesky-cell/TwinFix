"""用 tree-sitter 解析 Python，产出符号、调用与类型引用的原始记录。

解析只做"代码里明确写了什么"，跨文件消歧交给 python_index。Python 是动态语言，
所以只解析显式注解、类基类、import 与直接调用；不做类型推断，避免把猜测伪装成事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from twinfix.codeintel.model import Symbol

_LANGUAGE = Language(tree_sitter_python.language())
_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "callable",
        "dict",
        "dir",
        "enumerate",
        "eval",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "globals",
        "hasattr",
        "hash",
        "help",
        "hex",
        "id",
        "input",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "locals",
        "map",
        "max",
        "min",
        "next",
        "object",
        "oct",
        "open",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "setattr",
        "slice",
        "sorted",
        "str",
        "sum",
        "super",
        "tuple",
        "type",
        "vars",
        "zip",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "RuntimeError",
        "StopIteration",
        "NotImplementedError",
    }
)


@dataclass
class PyCall:
    src: str
    line: int
    name: str
    receiver: str | None = None
    receiver_is_call: bool = False
    arity: int | None = None


@dataclass
class PyRef:
    src: str
    line: int
    name: str
    kind: str = "type_ref"


@dataclass
class PyParsed:
    path: str
    module: str
    is_package: bool
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[PyCall] = field(default_factory=list)
    refs: list[PyRef] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)  # 本地名 -> 候选全限定名
    wildcards: list[str] = field(default_factory=list)
    class_bases: dict[str, list[str]] = field(default_factory=dict)  # 类 id -> 基类简单名


def module_name(path: str) -> tuple[str, bool]:
    """src/pkg/mod.py -> ("src.pkg.mod", False)；src/pkg/__init__.py -> ("src.pkg", True)。"""
    normalized = path.replace("\\", "/").removeprefix("./")
    if normalized.endswith(".py"):
        normalized = normalized[:-3]
    parts = [p for p in normalized.split("/") if p and p != "."]
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts = parts[:-1]
    # src-layout：src/pkg/mod.py 的导入名是 pkg.mod，路径前缀不参与模块名
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts), is_package


def parse_python(path: str, text: str) -> PyParsed:
    data = text.encode("utf-8")
    # Parser 不是线程安全的：build_python_index 可能在多个工作区线程里并发执行
    tree = Parser(_LANGUAGE).parse(data)
    module, is_package = module_name(path)
    parsed = PyParsed(path=path, module=module, is_package=is_package)
    _collect_imports(tree.root_node, data, parsed)
    _collect_definitions(tree.root_node, data, parsed, module=module, class_id=None, prefix="")
    _collect_references(tree.root_node, data, parsed)
    return parsed


# ---------------------------------------------------------------- 定义与符号


def _collect_definitions(
    node: Node,
    data: bytes,
    parsed: PyParsed,
    *,
    module: str,
    class_id: str | None,
    prefix: str,
) -> None:
    for child in node.named_children:
        target = child
        if child.type == "decorated_definition":
            definition = child.child_by_field_name("definition")
            if definition is not None:
                target = definition
        if target.type == "class_definition":
            _add_class(target, data, parsed, module=module, class_id=class_id, prefix=prefix)
        elif target.type == "function_definition":
            _add_function(target, data, parsed, module=module, class_id=class_id, prefix=prefix)
        elif target.type == "expression_statement":
            _add_assignment(target, data, parsed, module=module, class_id=class_id, prefix=prefix)


def _add_class(node: Node, data: bytes, parsed: PyParsed, *, module: str, class_id: str | None, prefix: str) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    own_id = f"{class_id}.{name}" if class_id else f"{module}.{name}" if module else name
    body = node.child_by_field_name("body")
    start = _line(node)
    end = _line(body) + _span(body) - 1 if body is not None else start
    bases = _class_bases(node, data)
    signature = f"class {name}" + (f"({', '.join(bases)})" if bases else "")
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=own_id,
            kind="class",
            path=parsed.path,
            start_line=_start_line(node),
            end_line=end,
            signature=signature,
            container=class_id,
            package=module,
        )
    )
    if bases:
        parsed.class_bases[own_id] = bases
    if body is not None:
        _collect_definitions(body, data, parsed, module=module, class_id=own_id, prefix=f"{prefix}{name}.")


def _add_function(node: Node, data: bytes, parsed: PyParsed, *, module: str, class_id: str | None, prefix: str) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, data)
    params = node.child_by_field_name("parameters")
    arity = _arity(params, data, method=class_id is not None)
    if class_id:
        own_id = f"{class_id}#{name}/{arity}"
        kind = "method"
        qualified = f"{class_id}.{name}"
        signature = _signature(node, data, is_method=True)
    else:
        own_id = f"{module}.{name}/{arity}" if module else f"{name}/{arity}"
        kind = "function"
        qualified = f"{module}.{name}" if module else name
        signature = _signature(node, data, is_method=False)
    body = node.child_by_field_name("body")
    start_line = _start_line(node)
    end_line = _line(body) + _span(body) - 1 if body is not None else start_line
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=qualified,
            kind=kind,
            path=parsed.path,
            start_line=start_line,
            end_line=end_line,
            signature=signature,
            container=class_id,
            package=module,
            arity=arity,
        )
    )
    # 嵌套函数不单独建符号；其调用仍归属于外层函数，避免索引噪声


def _add_assignment(
    node: Node, data: bytes, parsed: PyParsed, *, module: str, class_id: str | None, prefix: str
) -> None:
    assignment = next((c for c in node.named_children if c.type in ("assignment", "augmented_assignment")), None)
    if assignment is None:
        return
    left = assignment.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return
    name = _text(left, data)
    if class_id:
        own_id = f"{class_id}#field:{name}"
        kind = "field"
        signature = _text(assignment, data).splitlines()[0][:160]
    else:
        own_id = f"{module}:{name}" if module else name
        kind = "variable"
        signature = _text(assignment, data).splitlines()[0][:160]
    parsed.symbols.append(
        Symbol(
            id=own_id,
            name=name,
            qualified_name=own_id,
            kind=kind,
            path=parsed.path,
            start_line=_start_line(assignment),
            end_line=_line(assignment),
            signature=signature,
            container=class_id,
            package=module,
        )
    )


def _class_bases(node: Node, data: bytes) -> list[str]:
    supers = node.child_by_field_name("superclasses")
    if supers is None:
        return []
    names: list[str] = []
    for child in supers.named_children:
        raw = _text(child, data).strip()
        simple = raw.rsplit(".", 1)[-1].strip()
        if simple and simple not in names:
            names.append(simple)
    return names


def _arity(params: Node | None, data: bytes, *, method: bool) -> int:
    if params is None:
        return 0
    count = 0
    for child in params.named_children:
        if child.type in ("identifier", "typed_parameter", "default_parameter", "typed_default_parameter"):
            name = _param_name(child, data)
            if method and count == 0 and name in ("self", "cls"):
                continue
            count += 1
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            continue  # *args / **kwargs 不参与重载消歧
    return count


def _param_name(node: Node, data: bytes) -> str:
    if node.type == "identifier":
        return _text(node, data)
    for field_name in ("name",):
        child = node.child_by_field_name(field_name)
        if child is not None:
            return _text(child, data)
    first = node.named_children[0] if node.named_children else None
    return _text(first, data) if first is not None else ""


def _signature(node: Node, data: bytes, *, is_method: bool) -> str:
    params = node.child_by_field_name("parameters")
    ret = node.child_by_field_name("return_type")
    raw = f"def {_text(node.child_by_field_name('name'), data)}"
    if params is not None:
        raw += _text(params, data).replace("\n", " ")[:180]
    if ret is not None:
        raw += f" -> {_text(ret, data)[:80]}"
    return raw


# ---------------------------------------------------------------- 调用与引用


def _collect_references(root: Node, data: bytes, parsed: PyParsed) -> None:
    for symbol in parsed.symbols:
        if symbol.kind not in ("function", "method"):
            continue
        node = _node_for_symbol(root, symbol)
        if node is None:
            continue
        _signature_refs(node, data, parsed, symbol.id)
        body = node.child_by_field_name("body")
        if body is not None:
            _walk_scope(body, data, parsed, symbol.id)


def _signature_refs(node: Node, data: bytes, parsed: PyParsed, src: str) -> None:
    params = node.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            if param.type in ("typed_parameter", "typed_default_parameter"):
                type_node = param.child_by_field_name("type")
                if type_node is not None:
                    _type_refs(type_node, data, parsed, src, _line(param))
    ret = node.child_by_field_name("return_type")
    if ret is not None:
        _type_refs(ret, data, parsed, src, _line(node))


def _node_for_symbol(root: Node, symbol: Symbol) -> Node | None:
    # tree-sitter 节点不能从 Symbol 反查；按行号+kind 在树里找唯一匹配
    stack = [root]
    while stack:
        node = stack.pop()
        target = node
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition is not None:
                target = definition
        if target.type == "class_definition" and symbol.kind == "class" and _start_line(target) == symbol.start_line:
            return target
        if (
            target.type == "function_definition"
            and symbol.kind in ("function", "method")
            and _start_line(target) == symbol.start_line
        ):
            return target
        stack.extend(node.named_children)
    return None


def _walk_scope(node: Node, data: bytes, parsed: PyParsed, src: str) -> None:
    for child in node.named_children:
        if child.type in ("function_definition", "class_definition", "decorated_definition"):
            continue
        if child.type == "call":
            call = _call(child, data, src)
            if call is not None:
                parsed.calls.append(call)
        if child.type in ("typed_parameter", "typed_default_parameter") or child.type == "assignment":
            type_node = child.child_by_field_name("type")
            if type_node is not None:
                _type_refs(type_node, data, parsed, src, _line(child))
        _walk_scope(child, data, parsed, src)


def _call(node: Node, data: bytes, src: str) -> PyCall | None:
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    args = node.child_by_field_name("arguments")
    arity = 0
    if args is not None:
        arity = len(
            [c for c in args.named_children if c.type not in ("keyword_argument", "list_splat", "dictionary_splat")]
        )
    if fn.type == "identifier":
        name = _text(fn, data)
        if name in _BUILTINS or name == "super":
            return None
        return PyCall(src=src, line=_line(node), name=name, arity=arity)
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        if attr is None:
            return None
        name = _text(attr, data)
        receiver = None
        receiver_is_call = False
        if obj is not None:
            if obj.type == "identifier":
                receiver = _text(obj, data)
            elif obj.type == "call":
                inner = obj.child_by_field_name("function")
                if inner is not None and inner.type == "identifier":
                    receiver = _text(inner, data)
                    receiver_is_call = True
        return PyCall(
            src=src, line=_line(node), name=name, receiver=receiver, receiver_is_call=receiver_is_call, arity=arity
        )
    return None


def _type_refs(node: Node, data: bytes, parsed: PyParsed, src: str, line: int) -> None:
    if node.type == "identifier":
        name = _text(node, data)
        if name not in _BUILTINS:
            parsed.refs.append(PyRef(src=src, line=line, name=name))
        return
    for child in node.named_children:
        _type_refs(child, data, parsed, src, line)


# ---------------------------------------------------------------- imports


def _collect_imports(node: Node, data: bytes, parsed: PyParsed) -> None:
    for child in node.named_children:
        if child.type == "import_statement":
            for item in child.named_children:
                if item.type == "dotted_name":
                    full = _text(item, data)
                    local = full.split(".", 1)[0]
                    parsed.imports.setdefault(local, []).append(full)
                elif item.type == "aliased_import":
                    name_node = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    if name_node is not None and alias is not None:
                        parsed.imports.setdefault(_text(alias, data), []).append(_text(name_node, data))
        elif child.type == "import_from_statement":
            module_node = child.child_by_field_name("module_name")
            base = _import_base(module_node, data, parsed)
            for item in child.named_children:
                if item is module_node:
                    continue
                if item.type == "dotted_name":
                    imported = _text(item, data)
                    local = imported.rsplit(".", 1)[-1]
                    parsed.imports.setdefault(local, []).append(_join(base, imported))
                elif item.type == "aliased_import":
                    name_node = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    if name_node is not None and alias is not None:
                        parsed.imports.setdefault(_text(alias, data), []).append(_join(base, _text(name_node, data)))
                elif item.type == "wildcard_import":
                    parsed.wildcards.append(base)
        elif child.type == "decorated_definition":
            pass
        _collect_imports(child, data, parsed)


def _import_base(module_node: Node | None, data: bytes, parsed: PyParsed) -> str:
    current = parsed.module
    if module_node is None:
        return current
    if module_node.type == "dotted_name":
        return _text(module_node, data)
    if module_node.type == "relative_import":
        prefix = next((c for c in module_node.named_children if c.type == "import_prefix"), None)
        rest = next((c for c in module_node.named_children if c.type == "dotted_name"), None)
        level = len(_text(prefix, data)) if prefix is not None else 1
        package = current.split(".") if parsed.is_package else current.split(".")[:-1]
        package = package[: len(package) - (level - 1)] if level > 1 else package
        base = ".".join(package)
        return _join(base, _text(rest, data)) if rest is not None else base
    return current


def _join(base: str, name: str) -> str:
    return f"{base}.{name}" if base and name else base or name


# ---------------------------------------------------------------- 工具


def _text(node: Node | None, data: bytes) -> str:
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()


def _line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _start_line(node: Node) -> int:
    return int(node.start_point[0]) + 1


def _span(node: Node) -> int:
    return int(node.end_point[0]) - int(node.start_point[0]) + 1
