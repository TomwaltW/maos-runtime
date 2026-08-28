"""StorePort 的 SQLite 适配器 —— 组合现有的核心 Store，不继承、不重写。

手册禁区原文：「不许改现有 store.py 的任何方法签名。SQLiteStore 是适配器，不是
重写。」所以本模块只做两件事：把核心 Store 的连接与锁包成 StorePort 的五个方法；
再给退款域留一个公开的事务入口。核心 Store 的方法签名与表 DDL 一个字未动。

## 私有属性的唯一出口

本适配器确实读了核心 Store 的 `_conn` / `_lock` 两个私有属性 —— 这是**有意的收口**，
不是又开一条旁路：全仓库只此一处读它们，其余调用方一律走 StorePort。退款域的
`objects.py` 现在也各读一处（`docs/BACKLOG.md` 的 `## task-R1` 第 3 条记着），
换成走这里的 `execute` / `query` / `transaction` 之后那两处就能删干净。本轨不改
`objects.py`（不在独占文件里），只把能替代它的入口先摆好，怎么换见 DECISIONS。

适配器自己的属性**故意不叫** `_conn` / `_lock`：叫了的话，把本适配器实例传给
`objects.py::_conn()` 会被它 `getattr` 认下并直接在连接上 `commit()` —— 那会在
`transaction()` 块里提前提交，「settled 与回执同事务」当场破且没有症状。改名之后
传错对象会立刻抛 TypeError，把一次静默破坏换成一声脆响。

## 检索两条通道的口径（W-3 的检索器照这条写）

全文 `fts_search(table, field, q, limit)`：

- 走 SQLite FTS5，索引表是**约定命名的影子表** `<table>_fts`，由建表方自己创建
  与维护。适配器不建也不同步 —— 它不知道该同步哪些列，替人做主只会做错。
- 影子表必须有一个 `id UNINDEXED` 列存源表主键，其余列名与源表被索引字段同名：

      CREATE VIRTUAL TABLE kb_doc_fts USING fts5(
          id UNINDEXED, title, body, tokenize='trigram');

- **中文必须 `tokenize='trigram'`**：缺省的 unicode61 把一整串汉字切成一个 token，
  「退款政策超时未到账」整条是一个词，查「退款政策」一条都命不中，而且不报错。
  实测见本轨回执。
- **trigram 的查询串必须 ≥3 字符**：更短的查询切不出任何 trigram，FTS5 老老实实
  返回空集，不报错。所以中文两字词（「退款」「超时」）在 trigram 表上恒空 ——
  这是 SQLite 的既定行为，适配器不替它兜底（兜了就是本层自造一套语义），但检索器
  必须知道，否则会把「查询太短」误读成「库里没有」。
- 影子表不存在 → 抛 `LookupError` 并把上面那条 DDL 写进报错，**不回落 LIKE**：
  静默降级的症状是「全文检索看起来通了，召回却一直是空」。
- `q` 按空白切词，每个词转义成 FTS5 短语，词间用 FTS5 缺省的 AND（全都要命中）。
  这样任意用户输入都不会撞出 `fts5: syntax error`（`"` `*` `NEAR(` `-` 都实测过），
  代价是 FTS5 的 OR / NEAR 等算子透不过去。要算子就另开方法，别动 F-2 那五个签名。
- 分数取 `-bm25()`，越大越相关，降序。注意 bm25 的 IDF 在「词出现在过半文档里」
  时会归零，此时同批结果分数都是 0.0、退化成按 id 排序 —— 这是 BM25 本身的性质，
  不是排序坏了。

向量 `vector_search(table, field, vec, limit)`：

- 纯 Python 余弦，零依赖。手册口径是数据量 <1000 条够用；再大谈 pgvector（P5）。
- 源表 `<field>` 列存 JSON 数组文本（TEXT 或同内容的 BLOB），主键列名固定 `id`。
- 维度对不上、或解析不出数组 → 抛 `ValueError` 并点名是哪一行，**不跳过**：跳过
  等于把「半个库检索不到」变成一个没有症状的 bug，正是铁律 8 要防的那类假象。
- 分数是余弦相似度 [-1, 1]，越大越相关，降序；同分按 id 升序，保证结果可复现。
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import sqlite3
from typing import Any, Iterator

#: `dialect()` 的返回值，F-2 只认 "sqlite" | "postgres" 两个字面量。
DIALECT = "sqlite"

#: 影子表命名约定：源表 `kb_doc` 的 FTS5 索引表叫 `kb_doc_fts`。
FTS_SUFFIX = "_fts"

#: 表名 / 列名要拼进 SQL（标识符没法用占位符绑定），所以拼之前先卡死形状。
#: 参数一律走 `?`，一个都不拼 —— 这两条合起来才算「不拼 SQL」。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(kind: str, name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(
            f"非法的{kind}名 {name!r}：只允许字母、数字、下划线，且不以数字开头。"
            " 标识符是拼进 SQL 的，这里不卡形状就等于开了一条注入路径。"
        )
    return name


def _fts_match(field: str, q: str) -> str | None:
    """把任意用户输入转成一条不会撞语法错的 FTS5 查询；没有可查的词则返回 None。"""
    terms = (q or "").split()
    if not terms:
        return None
    phrases = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    return f"{field} : ({phrases})"


def _decode_vector(raw: Any, *, table: str, field: str, row_id: str) -> list[float]:
    """把库里存的一行向量解成 float 列表。解不出就抛，不静默跳过。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{table}.{field} 在 id={row_id!r} 这行不是 JSON 数组：{exc}。"
                " 向量列的约定是 JSON 数组文本。"
            ) from exc
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"{table}.{field} 在 id={row_id!r} 这行解出来是 {type(raw).__name__}，不是数组。"
        )
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{table}.{field} 在 id={row_id!r} 这行有非数值分量：{exc}"
        ) from exc


class SqliteStorePort:
    """把核心 `SqliteStore` 包成 StorePort。组合，不是继承。"""

    def __init__(self, store: Any) -> None:
        connection = getattr(store, "_conn", None)
        if connection is None:
            raise TypeError(
                f"{type(store).__name__} 没有暴露 sqlite 连接，包不出 SQLite 后端的 StorePort。"
                " 本适配器只接核心 store.py 的 SqliteStore；换后端请写新的适配器，"
                " 不要去改冻结的 store.py。"
            )
        self._store = store
        self._connection = connection
        lock = getattr(store, "_lock", None)
        #: 借核心 Store 自己那把 RLock。连接是共享的（check_same_thread=False），
        #: 不共用同一把锁，别的线程一次 commit 就能把这边只写了一半的事务提交掉。
        self._mutex: Any = lock if lock is not None else contextlib.nullcontext()
        self._depth = 0

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"SqliteStorePort(store={type(self._store).__name__})"

    # -- StorePort 五方法（F-2 冻结签名）---------------------------------------
    def execute(self, sql: str, params: tuple) -> None:
        with self._mutex:
            self._connection.execute(sql, tuple(params))
            if self._depth == 0:
                self._connection.commit()

    def query(self, sql: str, params: tuple) -> list[dict]:
        with self._mutex:
            cur = self._connection.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]:
        _ident("表", table)
        _ident("字段", field)
        limit = int(limit)
        match = _fts_match(field, q)
        if limit <= 0 or match is None:
            return []
        index = f"{table}{FTS_SUFFIX}"
        if not self._has_table(index):
            raise LookupError(
                f"FTS5 影子表 {index} 不存在，{table}.{field} 的全文通道没法走。"
                " 建表方自己建、自己同步，本适配器不代建（它不知道该同步哪些列）："
                f" CREATE VIRTUAL TABLE {index} USING fts5(id UNINDEXED, {field},"
                " tokenize='trigram');  —— 中文务必 trigram，缺省 unicode61 会把"
                " 整串汉字切成一个 token。这里不回落 LIKE：静默降级的症状是"
                "「全文检索看起来通了，召回却一直是空」。"
            )
        sql = (
            f"SELECT id, -bm25({index}) AS score FROM {index}"
            f" WHERE {index} MATCH ? ORDER BY score DESC, id ASC LIMIT ?"
        )
        try:
            rows = self.query(sql, (match, limit))
        except sqlite3.OperationalError as exc:
            raise LookupError(
                f"在影子表 {index} 上查 {field} 失败：{exc}。"
                f" 影子表必须含 id UNINDEXED 列与同名的 {field} 列，见本模块开头的口径。"
            ) from exc
        return [(str(r["id"]), float(r["score"])) for r in rows]

    def vector_search(
        self, table: str, field: str, vec: list[float], limit: int
    ) -> list[tuple[str, float]]:
        _ident("表", table)
        _ident("字段", field)
        limit = int(limit)
        if limit <= 0:
            return []
        try:
            probe = [float(x) for x in vec]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"查询向量不是一串数值：{exc}") from exc
        probe_norm = math.sqrt(sum(x * x for x in probe))
        if probe_norm == 0.0:
            raise ValueError("查询向量是零向量，余弦相似度无定义 —— 上游的嵌入多半出错了")
        try:
            rows = self.query(
                f"SELECT id, {field} AS vec FROM {table} WHERE {field} IS NOT NULL", ()
            )
        except sqlite3.OperationalError as exc:
            raise LookupError(
                f"读 {table}.{field} 失败：{exc}。向量通道约定主键列名为 id、"
                f" {field} 列存 JSON 数组文本。"
            ) from exc
        scored: list[tuple[str, float]] = []
        for row in rows:
            row_id = str(row["id"])
            other = _decode_vector(row["vec"], table=table, field=field, row_id=row_id)
            if len(other) != len(probe):
                raise ValueError(
                    f"{table}.{field} 在 id={row_id!r} 这行是 {len(other)} 维，"
                    f" 查询向量是 {len(probe)} 维。维度对不上多半是换了嵌入模型而没重算，"
                    " 这里不跳过该行 —— 跳过就变成「半个库检索不到」且毫无症状。"
                )
            other_norm = math.sqrt(sum(x * x for x in other))
            if other_norm == 0.0:
                scored.append((row_id, 0.0))
                continue
            dot = sum(a * b for a, b in zip(probe, other))
            scored.append((row_id, dot / (probe_norm * other_norm)))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]

    def dialect(self) -> str:
        return DIALECT

    # -- 公开的事务入口（替代退款域直接取私有 _conn / _lock 的那两处）-------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator["SqliteStorePort"]:
        """一个事务块：块内的 `execute` 不各自提交，出块才一次性提交。

        块内抛异常则整块回滚。可嵌套（借的是 RLock，只有最外层那次提交），
        「settled 与回执同事务」这类要求靠它成立，不必再去借核心 Store 的私有锁。
        """
        with self._mutex:
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._connection.rollback()
                raise
            self._depth -= 1
            if self._depth == 0:
                self._connection.commit()

    # -- 内部 ------------------------------------------------------------------
    def _has_table(self, name: str) -> bool:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        )
        return bool(rows)
