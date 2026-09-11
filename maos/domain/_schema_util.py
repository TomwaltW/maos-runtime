"""DDL 片段的加列收口 —— 「探一遍、加一遍、核一遍」这三步只写一次。

## 这个文件为什么存在

p10 跨轨契约 §B.2 让 T116 / T117 / T120 各自复制一份加列助手到自己的模块里
（原话：重复六行，**整合期由主会话去重**，各轨不要为此建共享文件）。落下来的是
两份（T120 那份后来并掉了），而且两份**行为不一样**：`case_pack.py` 那份的
`PRAGMA table_info` 探针带 `try/except` 回落，`compensate.py` 那份不带。
`PRAGMA` 是 SQLite 方言，PG 上没有 —— 于是换到 PG 之后，前者退成 no-op（靠调用方
补一遍），后者当场抛 `SyntaxError: syntax error at or near "PRAGMA"`。T133 收这个口。

## 加列本身不在这里实现

`_dbport.add_column_if_missing()` 早就是这件事的唯一实现（T115 建的，两个后端
各带一条测试），只是一直没有调用方 —— 那两条测试的 docstring 明写「整合期由主会话
改成 import 这一个」。本模块做的是它**上面那一层**：一个片段有好几列，
「这次到底真加了哪几列」要在动手之前问，加完还要核一遍。

## 探针为什么不再需要「回落」

原先 `case_pack.py` 那份的回落是「`PRAGMA` 探不动就 `return`，交给调用方的
SELECT 探针」，而它那段 docstring 特意讲清了一条不显然的理由：

> **回落不是兜底成「当作没有」** —— 那会让每次都去 ALTER 一次、每次都撞
> duplicate column name。

这条理由在这里一个字都不作废，但它现在由**更靠前的一步**满足：
`_dbport.add_column_if_missing()` 的探针是**后端感知**的（SQLite 走
`PRAGMA table_info`，PG 走 `information_schema`），两个后端上都是真探针，
不存在「探不动」这个态，所以也就不需要退成 None 让调用方补。

回落让位给 `_verify`：**探不动就该响，不该静默算过**。T133 实测过回落的代价 ——
把 `case_pack.py` 那份带回落的助手装到 `compensate.py::ensure_ticket_schema()` 上，
它在 PG 上不抛了，六列却一列都没加（那条路径上根本没有调用方的 SELECT 探针补位），
症状要等到下游写 `compensation_record` 时才以 `UndefinedColumn` 冒出来，
而那句报错完全不提加列这件事。
"""
from __future__ import annotations

from typing import Any, Iterable

from maos.domain._dbport import DomainConn, add_column_if_missing

#: `(表, 列, 声明)` 三元组 —— 各轨的片段解析器（`case_pack.fragment_columns()` /
#: `compensate.ticket_columns()`）吐出来的形状。目标形状的唯一来源仍是 .sql 片段
#: 本身，本模块不在代码里另抄清单。
Column = tuple[str, str, str]


class SchemaFragmentError(RuntimeError):
    """片段加列没加上。消息直接给人看，不用翻栈。"""


def has_column(conn: DomainConn, table: str, col: str) -> bool:
    """探「这张表有没有这一列」。一条 `SELECT <col> FROM <table> LIMIT 1`，后端无关。

    口径逐字同 `maos/domain/refund/objects.py::_has_column`（那是冻结面，只读）：
    表名列名都是调用方的字面量、不是外来输入，所以直接拼进 SQL；异常一律当
    「没这列」—— 真是连接坏了，紧随其后的 ALTER 会自己响，不会被这层吞掉。

    **刻意不复用 `_dbport.add_column_if_missing()` 里那套方言探针**：那一套按后端
    分支（`information_schema` / `PRAGMA`），而这里要的恰恰是一条两个后端同形的
    语句 —— 它还顺带探得到 SQLite 的生成列（`PRAGMA table_info` 不列生成列，
    见 `objects._has_column` 的第二条理由）。
    """
    try:
        conn.query(f"SELECT {col} FROM {table} LIMIT 1")
        return True
    except Exception:                                  # noqa: BLE001 —— 探针不该炸
        return False


def apply_columns(conn: DomainConn, columns: Iterable[Column]) -> tuple[str, ...]:
    """把片段声明的列逐条加到库上。幂等，可连跑。返回**本次真加了**的那几列。

    `"<表>.<列>"` 的字符串，顺序同片段。返回值不是装饰：迁移记账、测试与
    `scripts/run_case.py` 的输出都按它判「本次是不是真的动了库」。

    **先探一遍再加**：`add_column_if_missing()` 返回 `None`，所以「这次到底加了
    哪几列」只能在调它之前问。

    **加完再核一遍**：核的只是 `missing` 那几列 —— 本来就有的不必再问。
    核不过就抛，不静默算过；理由见模块 docstring 末段（静默不加列的症状离原因很远）。
    """
    wanted: list[Column] = [(str(t), str(c), str(d)) for t, c, d in columns]
    missing = [(t, c, d) for t, c, d in wanted if not has_column(conn, t, c)]

    for table, col, decl in wanted:
        add_column_if_missing(conn, table, col, decl)

    _verify(conn, missing)
    return tuple(f"{t}.{c}" for t, c, _d in missing)


def _verify(conn: DomainConn, missing: list[Column]) -> None:
    still = [f"{t}.{c}" for t, c, _d in missing if not has_column(conn, t, c)]
    if still:
        raise SchemaFragmentError(
            f"片段加列没加上：{still}（后端 {conn.dialect}）。"
            " 加列助手的探针在这个后端上没起作用 —— 静默算过的话，症状要等到下游"
            " INSERT 报 no such column / UndefinedColumn 才冒出来，而那句报错"
            " 完全不提加列这件事。")


def open_conn(store: Any) -> DomainConn:
    """取这一次调用该用的业务域连接。

    薄薄一层转发，存在的理由是**让调用方不必自己 import `_dbport`**：
    片段加载器关心的是「把这几列加上」，不是「这条连接从哪来」。
    """
    return DomainConn.open(store)
