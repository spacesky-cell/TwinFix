"""代码索引的数据模型。

索引只回答"代码里有什么、谁引用了谁"，不做语义编译。符号 id 的约定：
  类型       com.acme.Foo
  方法/构造  com.acme.Foo#bar/2（重载按参数个数区分）
  字段       com.acme.Foo#field:count
"""

from __future__ import annotations

from dataclasses import dataclass, field

TYPE_KINDS = frozenset({"class", "interface", "enum", "record", "struct", "delegate", "annotation", "type"})


@dataclass(frozen=True)
class Symbol:
    id: str
    name: str
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str
    container: str | None = None
    package: str = ""
    arity: int | None = None
    return_type: str | None = None
    params: tuple[str, ...] = ()

    @property
    def is_type(self) -> bool:
        return self.kind in TYPE_KINDS

    def location(self) -> str:
        return f"{self.path}:{self.start_line}"


@dataclass(frozen=True)
class Edge:
    src: str  # 发起引用的符号 id
    dst: str | None  # 解析到的符号 id；None 表示仓库内未解析（可能是外部依赖或歧义）
    kind: str  # calls / constructs / type_ref / extends / implements
    path: str
    line: int
    name: str
    arity: int | None = None
    receiver: str | None = None
    receiver_type: str | None = None


@dataclass(frozen=True)
class ImportSet:
    types: dict[str, str] = field(default_factory=dict)  # 简单名 -> 全限定名
    statics: dict[str, str] = field(default_factory=dict)  # 成员名 -> 所有者全限定名
    wildcards: tuple[str, ...] = ()  # import a.b.* 的前缀


@dataclass(frozen=True)
class Reference:
    edge: Edge
    source: Symbol | None  # 发起引用的符号；文件级引用时为 None


@dataclass(frozen=True)
class IndexMeta:
    files: int
    symbols: int
    edges: int
    truncated: bool
    build_ms: int
