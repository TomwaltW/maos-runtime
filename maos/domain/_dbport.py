"""业务域的**双后端连接层** —— SQLite 方言进，PostgreSQL 方言出，DDL 只写一份。

## 为什么是翻译器，不是第二份 DDL

退款域的 16 张表原本硬绑在 SQLite 上：`objects._conn()` 直接去取 `SqliteStore`
的私有属性 `_conn`，取不到就抛 `TypeError`。要让这个域跑在 PolarDB 上，最直觉的
做法是照 `schema.sql` 手抄一份 PG 版 —— **那条路本轨明确不走**。

同一个波次里还有三条轨在往退款域加表加列（跨轨契约 §B）。手抄一份 PG DDL 的话，
那三轨的片段一落地就漂：SQLite 侧有新表、PG 侧没有，而**症状要到跑的时候才出现**
（`relation does not exist`），且只在配了 PG 的那台机器上出现。所以这里写的是
`to_pg_ddl()`：片段作者仍然只写一份 SQLite 方言的 DDL，PG 侧由它现翻。

翻译器**遇到不认识的构造就抛 `UnsupportedDdlError`**，不猜也不静默跳过 ——
静默跳过等于 PG 上少一张表，而少一张表同样要到跑的时候才炸。唯一允许的跳过是
FTS5 虚表，理由写在 `_fts5_skip()` 上。

## 两个后端的语义对齐点

1. **借锁**。SQLite 那条连接是共享的（`check_same_thread=False` + 一把 RLock），
   退款域绕过 store.py 直接用它就必须一并借它那把锁，否则别的线程一次 `commit()`
   就能把 guard 只写了回执、还没改状态的事务提交掉（见 `objects.lock_of`）。
   PG 这边同样是一条进程内共享连接（psycopg 的连接不是线程安全的），所以本模块
   自带一把 RLock，语义与 SQLite 侧逐条对齐。

2. **失败的语句不该毒掉整个事务**。SQLite 里一条语句失败，事务照样能往下走；
   PG 里一条语句失败会把整个事务打进 aborted 态，之后每一句都报
   `current transaction is aborted`。而 `objects._has_column()` 这类探针**故意**
   靠「一条会失败的 SELECT」来判断列在不在，还把异常吞掉继续跑 —— 不补这一条，
   探针在 PG 上会把它后面所有语句一起带走。补法见 `_PgConnection._recover()`。

3. **DDL 也要能回滚**。SQLite 需要显式 `SAVEPOINT` 才能把 DDL 拉进事务（传统模式
   下 `DROP TABLE` 是自动提交的）；PG 的 DDL 本来就事务内。两边都用 `SAVEPOINT`，
   `atomic()` 一份代码两个后端。

## 缺省路径一个字节不变

`MAOS_DOMAIN_BACKEND` 不设时走 `sqlite` 分支，行为与本模块出现之前逐字相同：
同一条 `store._conn`、同一把 `store._lock`、同样的 `SAVEPOINT` 名字。
配成 `postgres` 却拿不到库（没装驱动 / 没配 DSN / 连不上），一律抛
`PgBackendUnavailable`，**绝不回落 sqlite** —— 沿用 `maos/store/pg_store.py`
的既有口径：回落的话你会以为 PG 验过了，其实一行 PG 代码都没执行。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from typing import Any, Callable, Iterator, Sequence

log = logging.getLogger("maos.domain.dbport")

#: 选后端的环境变量（跨轨契约 §H）。
BACKEND_ENV = "MAOS_DOMAIN_BACKEND"

SQLITE = "sqlite"
POSTGRES = "postgres"
DEFAULT_BACKEND = SQLITE


class UnsupportedDdlError(ValueError):
    """`to_pg_ddl()` 碰上了它不认识的 SQLite 构造。

    **报错里必须带上原文那一行** —— 翻译器的边界是会变的（下一轨可能就要加一种
    列类型），而「哪一句翻不动」是唯一能让人一眼看出该往哪儿加的信息。
    """


# =============================================================== SQL 词法扫描
# 下面几个函数共用一条口径：**按引号状态扫一遍**。
#
# 为什么不用正则：`?`、`;`、`,` 三个字符在字符串字面量里都是普通字符，
# 而 `WHERE note LIKE '%?%'` 这种 SQL 是合法的。正则分不出「在不在引号里」，
# 改错一条的后果是 SQL 仍然合法、只是语义变了 —— 没有任何症状。
#
# SQLite 与 PG 的字面量语法在这里恰好同构：单引号裹字符串、`''` 是转义的单引号；
# 双引号裹标识符；`--` 到行尾是注释，`/* */` 是块注释。

def _scan(sql: str) -> Iterator[tuple[int, str, bool]]:
    """逐字符扫过 SQL，产出 `(下标, 字符, 是不是在字符串/标识符/注释里)`。"""
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            quote = ch
            yield i, ch, True
            i += 1
            while i < n:
                yield i, sql[i], True
                if sql[i] == quote:
                    # `''` / `""` 是转义，不是收尾。
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 1
                        yield i, sql[i], True
                    else:
                        i += 1
                        break
                i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                yield i, sql[i], True
                i += 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            while i < end:
                yield i, sql[i], True
                i += 1
            continue
        yield i, ch, False
        i += 1


def strip_sql_comments(sql: str) -> str:
    """去掉 `--` 与 `/* */` 注释，字符串字面量里的同形字符原样保留。"""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 1
                        out.append(sql[i])
                    else:
                        i += 1
                        break
                i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(script: str) -> list[str]:
    """把一份脚本按顶层 `;` 切成语句，去注释、去空白段。"""
    cleaned = strip_sql_comments(script)
    chunks: list[str] = []
    start = 0
    depth = 0
    for idx, ch, quoted in _scan(cleaned):
        if quoted:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            chunks.append(cleaned[start:idx])
            start = idx + 1
    chunks.append(cleaned[start:])
    return [c.strip() for c in chunks if c.strip()]


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """按顶层分隔符切（括号内与引号内的分隔符不算）。"""
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, ch, quoted in _scan(text):
        if quoted:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:idx])
            start = idx + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def translate_placeholders(sql: str) -> str:
    """SQLite 的 `?` 翻成 PG 的 `%s`。**字符串字面量里的 `?` 不动。**

    顺带把裸 `%` 转义成 `%%`：psycopg 在**带参数**的那次调用里会按 `%` 找占位符，
    `WHERE note LIKE '%x%'` 原样发过去会报 `unsupported format character`。
    两步的顺序不能反 —— 先转义 `%`、再插 `%s`，否则刚插进去的 `%s` 会被自己转义掉。

    只在「真的要传参数」时调用（见 `_PgConnection.execute`）：不传参数的语句
    psycopg 根本不解析 `%`，此时转义反而会把 `%` 原样写进库里。
    """
    escaped: list[str] = []
    for _idx, ch, _quoted in _scan(sql):
        escaped.append("%%" if ch == "%" else ch)
    text = "".join(escaped)

    out: list[str] = []
    for _idx, ch, quoted in _scan(text):
        out.append("%s" if (ch == "?" and not quoted) else ch)
    return "".join(out)


# =================================================================== DDL 翻译
#: SQLite 类型 -> PG 类型。`TEXT` / `INTEGER` PG 原样认，`REAL` 不认。
_TYPE_MAP = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "REAL": "double precision",
}

#: `DEFAULT` 后面允许的字面量形状。函数默认值（`datetime('now')` 之类）**一律拒**：
#: 跨轨契约 §B.3 也禁了它 —— 时间戳由 Python 侧 `_now()` 传进来，两个后端才可能
#: 拿到同一个时刻，让库自己填等于两个后端各填各的。
_LITERAL_DEFAULT = re.compile(
    r"""^(?:'(?:[^']|'')*'      # 字符串字面量
         |[-+]?\d+(?:\.\d+)?    # 整数 / 小数
         |NULL|TRUE|FALSE)$""",
    re.IGNORECASE | re.VERBOSE,
)

_CREATE_TABLE = re.compile(
    r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
#: `CREATE INDEX` 原样透传。`USING <方法>` 那一段是 PG 专有的（SQLite 语法里没有），
#: 认它是因为 `maos/store/pg_schema.sql` 那几条 tsvector GIN 索引也从这条路过 ——
#: 那份文件装的正是**翻译器翻不出来的东西**，本来就已经是 PG 方言。
#: 透传已经合法的 PG 不违反「不认识就抛」：翻译器的承诺是「出去的是能跑的 PG」。
_CREATE_INDEX = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_]*"
    r"\s+ON\s+[A-Za-z_][A-Za-z0-9_]*\s*(?:USING\s+[A-Za-z_][A-Za-z0-9_]*\s*)?\(.*\)"
    r"(?:\s+WHERE\s+.*)?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_VIRTUAL = re.compile(
    r"^CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)"
    r"\s+USING\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_ADD_COLUMN = re.compile(
    r"^ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_DROP_TABLE = re.compile(
    r"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_]*\s*$",
    re.IGNORECASE,
)
_CREATE_EXTENSION = re.compile(r"^CREATE\s+EXTENSION\s+", re.IGNORECASE)

#: 表级约束的打头词。这几种 PG 逐字都认，原样透传。
_TABLE_CONSTRAINT = re.compile(
    r"^(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK|CONSTRAINT)\b", re.IGNORECASE)

#: `INTEGER PRIMARY KEY AUTOINCREMENT` —— SQLite 的自增列写法，整条换成 bigserial。
#: 拆开翻是错的：PG 没有 `AUTOINCREMENT` 关键字，而 `INTEGER PRIMARY KEY` 在 PG 上
#: 是一个**不自增**的普通主键，翻成那样不报错，只是插入时少了个值。
_AUTOINCREMENT_PK = re.compile(
    r"^INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\s*$", re.IGNORECASE)

_GENERATED = re.compile(
    r"^GENERATED\s+ALWAYS\s+AS\s*(\(.*\))\s*(VIRTUAL|STORED)?\s*$",
    re.IGNORECASE | re.DOTALL)


def _tokens(text: str) -> list[str]:
    """把约束尾巴切成 token：括号组算一个 token，引号串算一个 token。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for _idx, ch, quoted in _scan(text):
        if not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and ch.isspace():
                if buf:
                    out.append("".join(buf))
                    buf = []
                continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _fts5_skip(name: str) -> None:
    """FTS5 虚表**整段跳过**，并记一条日志。这是翻译器唯一允许的跳过。

    理由：FTS5 是 SQLite 专属的全文索引，PG 上没有对应物，也不需要 ——
    PG 侧的全文由 `kb_doc` 上的 tsvector + GIN 承担
    （`PgStorePort.fts_search`，索引 DDL 在 `maos/store/pg_schema.sql`）。
    翻不出来就跳过是安全的：这张表在 PG 上**本来就不该存在**，跳过之后没有
    任何一条 PG 侧的查询会去找它（`retriever._local_fts_rows` 的注释里写着
    「这条路在 PG 后端上是死的」，它自己会退化并只告警一次）。

    与「不认识就抛」的区别在于**知不知道跳过之后会怎样**：这里知道，
    别的构造不知道 —— 那些跳过之后就是 PG 上少一张表，而少表只在跑的时候炸。
    """
    log.info(
        "to_pg_ddl 跳过 FTS5 虚表 %s：FTS5 是 SQLite 专属，"
        "PG 侧的全文走 kb_doc 上的 tsvector（见 maos/store/pg_schema.sql）。", name)


def _translate_column(defn: str, *, origin: str) -> str:
    """翻一条列定义。认不出来的构造一律抛 `UnsupportedDdlError`。"""
    head = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$", defn, re.DOTALL)
    if head is None:
        raise UnsupportedDdlError(
            f"这条列定义拆不出「列名 + 类型」两段：{defn!r}（出自 {origin!r}）")
    name, rest = head.group(1), head.group(2).strip()

    if _AUTOINCREMENT_PK.match(rest):
        return f"{name} bigserial PRIMARY KEY"

    parts = _tokens(rest)
    raw_type = parts[0].upper()
    if raw_type not in _TYPE_MAP:
        raise UnsupportedDdlError(
            f"不认识的列类型 {parts[0]!r}（列 {name}，出自 {origin!r}）。"
            f" 翻译器认的是 {sorted(_TYPE_MAP)}；要加新类型就在 _TYPE_MAP 里加一条，"
            " 不要在别处手写一份 PG DDL —— 那正是本模块要买掉的东西。")
    out = [name, _TYPE_MAP[raw_type]]

    i = 1
    while i < len(parts):
        token = parts[i].upper()
        if token == "NOT" and i + 1 < len(parts) and parts[i + 1].upper() == "NULL":
            out.append("NOT NULL")
            i += 2
        elif token == "NULL":
            out.append("NULL")
            i += 1
        elif token == "PRIMARY" and i + 1 < len(parts) and parts[i + 1].upper() == "KEY":
            out.append("PRIMARY KEY")
            i += 2
        elif token == "UNIQUE":
            out.append("UNIQUE")
            i += 1
        elif token == "DEFAULT":
            if i + 1 >= len(parts) or not _LITERAL_DEFAULT.match(parts[i + 1]):
                got = parts[i + 1] if i + 1 < len(parts) else "<空>"
                raise UnsupportedDdlError(
                    f"DEFAULT 只认字面量，拿到的是 {got!r}（列 {name}，出自 {origin!r}）。"
                    " 函数默认值（datetime('now') 之类）两个后端各填各的时刻，"
                    " 时间戳一律由 Python 侧传进来（跨轨契约 §B.3）。")
            out.append(f"DEFAULT {parts[i + 1]}")
            i += 2
        elif token.startswith("CHECK"):
            # `CHECK(...)` 与 `CHECK (...)` 两种写法，切完可能是一个 token 或两个。
            if parts[i].upper() == "CHECK" and i + 1 < len(parts) and parts[i + 1].startswith("("):
                out.append(f"CHECK {parts[i + 1]}")
                i += 2
            else:
                out.append(parts[i])
                i += 1
        elif token == "REFERENCES":
            out.extend(parts[i:])
            i = len(parts)
        elif token == "GENERATED":
            gen = _GENERATED.match(" ".join(parts[i:]))
            if gen is None:
                raise UnsupportedDdlError(
                    f"生成列只认 `GENERATED ALWAYS AS (...) [VIRTUAL|STORED]`，"
                    f" 拿到的是 {' '.join(parts[i:])!r}（列 {name}，出自 {origin!r}）")
            # PG 16 只有 STORED 生成列（VIRTUAL 要 PG 18）。
            # 把 VIRTUAL 翻成 STORED 是**有意的**：这一列的表达式是纯拼接、恒确定，
            # 两种存法在读取侧不可分辨，差别只在占不占盘。翻成别的（比如干脆丢掉
            # 这一列）会让 F-2 的 `SELECT id` 在 PG 上恒抛 no such column，
            # 检索器那条端口分支一次都走不到 —— 而它不报错，只是召回悄悄变少。
            out.append(f"GENERATED ALWAYS AS {gen.group(1)} STORED")
            i = len(parts)
        else:
            raise UnsupportedDdlError(
                f"不认识的列约束 {parts[i]!r}（列 {name}，出自 {origin!r}）。"
                " 翻译器认的是 NOT NULL / PRIMARY KEY / UNIQUE / DEFAULT <字面量> /"
                " CHECK (...) / REFERENCES ... / GENERATED ALWAYS AS (...)。"
                " 不认识就抛而不是跳过：跳过等于 PG 上这一列悄悄少了个约束。")
    return " ".join(out)


def _translate_create_table(match: re.Match, origin: str) -> str:
    table, body = match.group(1), match.group(2)
    items = _split_top_level(body)
    if not items:
        raise UnsupportedDdlError(f"空的 CREATE TABLE：{origin!r}")
    out = []
    for item in items:
        if _TABLE_CONSTRAINT.match(item):
            out.append(item)                       # 表级约束 PG 逐字都认
        else:
            out.append(_translate_column(item, origin=origin))
    inner = ",\n    ".join(out)
    exists = "IF NOT EXISTS " if re.match(
        r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", origin, re.IGNORECASE) else ""
    return f"CREATE TABLE {exists}{table} (\n    {inner}\n)"


def _translate_statement(statement: str) -> str | None:
    """翻一条 DDL。返回 `None` = 这条整段跳过（只有 FTS5 虚表走得到）。"""
    text = " ".join(statement.split())

    virtual = _CREATE_VIRTUAL.match(text)
    if virtual is not None:
        if virtual.group(2).lower().startswith("fts"):
            _fts5_skip(virtual.group(1))
            return None
        raise UnsupportedDdlError(
            f"不认识的虚表模块 {virtual.group(2)!r}：{statement!r}。"
            " 只有 FTS5 虚表允许跳过（它在 PG 上本来就不该存在），别的虚表跳过之后"
            " 就是 PG 上少一张表，而少表只在跑的时候炸。")

    table = _CREATE_TABLE.match(text)
    if table is not None:
        return _translate_create_table(table, text)

    if _CREATE_INDEX.match(text):
        return text                                 # `CREATE INDEX IF NOT EXISTS` PG 认

    alter = _ALTER_ADD_COLUMN.match(text)
    if alter is not None:
        column = _translate_column(alter.group(2).strip(), origin=text)
        return f"ALTER TABLE {alter.group(1)} ADD COLUMN {column}"

    if _DROP_TABLE.match(text) or _CREATE_EXTENSION.match(text):
        return text

    raise UnsupportedDdlError(
        f"不认识的 DDL：{statement!r}。"
        " 翻译器认的是 CREATE TABLE / CREATE INDEX / ALTER TABLE ADD COLUMN /"
        " DROP TABLE / CREATE EXTENSION，外加整段跳过 FTS5 虚表。"
        " 这里抛而不是原样透传：透传过去多半是一条 PG 语法错，报出来的位置离真正"
        " 的原因很远。")


def to_pg_ddl(sqlite_ddl: str) -> str:
    """SQLite 方言的 DDL 进，PG 方言出。可以是一条语句，也可以是整份 schema.sql。

    覆盖面就是跨轨契约 §B.3 允许片段使用的那个子集，外加退款域与知识层现有表
    实际用到的东西：

        TEXT / INTEGER / REAL / NOT NULL / DEFAULT <字面量> / PRIMARY KEY (...)
        / UNIQUE (...) / CHECK (...) / FOREIGN KEY / REFERENCES
        / INTEGER PRIMARY KEY AUTOINCREMENT / GENERATED ALWAYS AS (...) VIRTUAL
        / CREATE INDEX IF NOT EXISTS / ALTER TABLE ADD COLUMN / DROP TABLE

    别的一律 `UnsupportedDdlError`（唯一例外见 `_fts5_skip`）。
    """
    out = []
    for statement in split_statements(sqlite_ddl):
        translated = _translate_statement(statement)
        if translated is not None:
            out.append(translated + ";")
    return "\n\n".join(out)


# ============================================================ DML 方言翻译
_INSERT_OR = re.compile(
    r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\(([^)]*)\)\s*(VALUES\b.*)$",
    re.IGNORECASE | re.DOTALL,
)

_DDL_HEAD = re.compile(r"^\s*(CREATE|ALTER|DROP)\s", re.IGNORECASE)


def is_ddl(sql: str) -> bool:
    """这条语句要不要走 `to_pg_ddl()`。"""
    return _DDL_HEAD.match(strip_sql_comments(sql)) is not None


def rewrite_upsert(sql: str, pk_of: Callable[[str], Sequence[str]]) -> str:
    """`INSERT OR REPLACE/IGNORE INTO` 翻成 PG 的 `ON CONFLICT`。

    PG 没有 `INSERT OR REPLACE`，而全仓库有二十多处在用它（退款域的每个 skill
    落业务表都是这个写法）。**逐处改成 `ON CONFLICT` 不在本轨的白名单里**，
    所以在这一层翻：冲突目标取那张表的**主键**，从 PG 的系统目录现查，
    不在代码里另抄一份 —— 抄一份的后果是加了列改了主键之后两边悄悄对不上。

    `OR REPLACE` -> `DO UPDATE SET 非主键列=EXCLUDED.列`（整行覆盖，与 SQLite 同义）；
    `OR IGNORE`  -> `DO NOTHING`；插入列全是主键列时 `OR REPLACE` 也退成 `DO NOTHING`
    （没有非主键列可更新，`DO UPDATE SET` 会是语法错）。
    """
    match = _INSERT_OR.match(sql)
    if match is None:
        return sql
    mode, table, cols, tail = match.groups()
    columns = [c.strip() for c in cols.split(",") if c.strip()]
    keys = list(pk_of(table))
    if not keys:
        raise ValueError(
            f"{table} 在 PG 上没有主键，`INSERT OR {mode.upper()}` 没法翻成"
            " ON CONFLICT —— 冲突目标无从确定。给这张表加主键，或把这条 SQL 改成"
            " 显式的 ON CONFLICT。")
    missing = [k for k in keys if k not in columns]
    if missing:
        raise ValueError(
            f"{table} 的主键列 {missing} 不在这条 INSERT 的列清单里，"
            " ON CONFLICT 推不出冲突目标。")
    updatable = [c for c in columns if c not in keys]
    target = ", ".join(keys)
    if mode.upper() == "IGNORE" or not updatable:
        action = "DO NOTHING"
    else:
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in updatable)
        action = f"DO UPDATE SET {sets}"
    return (f"INSERT INTO {table} ({', '.join(columns)}) {tail.strip()}"
            f" ON CONFLICT ({target}) {action}")


#: `sqlite_master` 的 PG 等价。两列与 SQLite 的同名列对齐（`name` / `type`），
#: 所以调用方那句 `WHERE type='table' AND name=?` 一个字都不用改。
#:
#: 为什么翻而不是让调用方分叉：`maos/kb/__init__.py::has_kb_table` 走的是分叉那条
#: 路（它自己按 `dialect_of` 问两个目录），而 `maos/domain/refund/outcome.py` 的
#: `has_outcome_table()` 走不了 —— 那个文件是冻结面，一个字都不许动。它问的
#: `SELECT name FROM sqlite_master WHERE type='table' AND name='case_outcome'`
#: 在 PG 上直接 `UndefinedTable`，而它是**建表路径的探针**：探不动就没有
#: `case_outcome`，四判据整段落空。`sqlite_master` 本来就是 SQLite 方言的一部分，
#: 翻它正是本模块的职责。
#:
#: 索引那一路也带上：`sqlite_master` 里索引与表同表共存，只映射表会让
#: `WHERE type='index'` 的调用方恒答「没有」—— 又一次无症状失效。
_SQLITE_MASTER_PG = (
    "(SELECT table_name AS name, 'table' AS type"
    " FROM information_schema.tables WHERE table_schema = current_schema()"
    " UNION ALL"
    " SELECT indexname AS name, 'index' AS type"
    " FROM pg_indexes WHERE schemaname = current_schema()) AS sqlite_master"
)

_SQLITE_MASTER_REF = re.compile(r"\b(FROM|JOIN)\s+sqlite_master\b", re.IGNORECASE)


def rewrite_sqlite_master(sql: str) -> str:
    """`FROM sqlite_master` 换成 PG 的目录查询。字符串字面量里的同形字原样保留。

    与本模块别处同一条口径：**按引号状态扫一遍**，不用裸正则替换。
    `WHERE note LIKE '%from sqlite_master%'` 是合法 SQL，改了它不报错、只是语义变了。
    """
    if "sqlite_master" not in sql.lower():
        return sql
    quoted = {i for i, _ch, inside in _scan(sql) if inside}
    out: list[str] = []
    last = 0
    for m in _SQLITE_MASTER_REF.finditer(sql):
        if m.start() in quoted:
            continue
        out.append(sql[last:m.start()])
        out.append(f"{m.group(1)} {_SQLITE_MASTER_PG}")
        last = m.end()
    if not out:
        return sql
    out.append(sql[last:])
    return "".join(out)


def to_pg_sql(sql: str, pk_of: Callable[[str], Sequence[str]], *,
              with_params: bool) -> str:
    """一条 SQLite 方言的语句翻成 PG 方言。DDL 走 `to_pg_ddl()`，其余走 DML 那套。

    `with_params=False` 时**不碰 `%`**：psycopg 只在传了参数的那次调用里解析 `%`，
    不传参数时转义反而会把 `%%` 原样写进库里。
    """
    if is_ddl(sql):
        return to_pg_ddl(sql)
    text = rewrite_sqlite_master(rewrite_upsert(sql, pk_of))
    return translate_placeholders(text) if with_params else text


# ============================================================ PG 侧的连接壳
#: 取一张表主键列的目录查询。`indkey` 是 int2vector，要显式转成数组才排得了序；
#: 不排序的话多列主键的顺序是随机的，而 `ON CONFLICT (a, b)` 的列序无所谓、
#: 但读起来会每次都不一样，diff 里全是噪声。
_PK_SQL = """
SELECT a.attname AS name
  FROM pg_index i
  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
 WHERE i.indrelid = %s::regclass AND i.indisprimary
 ORDER BY array_position(i.indkey::int2[], a.attnum)
"""


class _PgConnection:
    """把一条 psycopg 连接包成 `sqlite3.Connection` 的形状。

    **为什么要装成 sqlite 连接的样子**：`maos/domain/refund/guard.py` 直接拿
    `objects._conn(store)` 的返回值调 `.execute()` / `.commit()` / `.rollback()`，
    而 guard.py **不在本轨的白名单里**（它是铁律 8 的落点，本轨一个字都不该动）。
    所以换后端只能换掉这个返回值的实现，不能换掉它的形状。

    连接是 `autocommit=False`：guard 的「settled 必须与回执同事务」靠的正是
    「两条 execute + 一次 commit」这个序列，autocommit 会把它拆成两笔独立提交，
    那道闸当场破，而且是偶发的（只在中间那一刻崩溃时显形）。
    """

    def __init__(self, raw: Any, dsn_label: str = "") -> None:
        self._raw = raw
        self._pk_cache: dict[str, tuple[str, ...]] = {}
        self._label = dsn_label
        #: `atomic()` 的嵌套深度。>0 时一条语句失败**不**自动回滚整个事务 ——
        #: 那一层自己有 SAVEPOINT 要回，抢在它前面 rollback 会把保存点一起丢掉。
        self.atomic_depth = 0

    # -- sqlite3.Connection 的那三个方法 -------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        bound = tuple(params or ())
        text = to_pg_sql(sql, self.primary_key, with_params=bool(bound))
        cur = self._raw.cursor()
        try:
            # 不传参数时给 psycopg 递 None 而不是 ()：后者会让它去解析 `%`，
            # 而建表 DDL 里的 `%` 不是占位符（`DEFAULT '100%'` 这种）。
            cur.execute(text, bound or None)
        except Exception:
            self._recover()
            raise
        return cur

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        if not self._raw.closed:
            self._raw.close()

    def executescript(self, script: str) -> None:
        """整份 SQLite DDL 翻成 PG 之后一次发过去。"""
        text = to_pg_ddl(script)
        if not text.strip():
            return
        cur = self._raw.cursor()
        try:
            cur.execute(text)
        except Exception:
            self._recover()
            raise

    # -- PG 专有 ---------------------------------------------------------------
    def primary_key(self, table: str) -> tuple[str, ...]:
        """一张表的主键列，按主键内的列序。查一次记一次。"""
        if table in self._pk_cache:
            return self._pk_cache[table]
        cur = self._raw.cursor()
        try:
            cur.execute(_PK_SQL, (table,))
            keys = tuple(str(r["name"]) for r in cur.fetchall())
        except Exception:
            self._recover()
            raise
        self._pk_cache[table] = keys
        return keys

    def _recover(self) -> None:
        """一条语句失败之后，把事务从 aborted 态里捞回来。

        PG 与 SQLite 在这里语义不同：SQLite 里一条语句失败，事务照常往下走；
        PG 里整个事务进 aborted 态，之后每一句都报 `current transaction is aborted`。
        而 `objects._has_column()` / `kb._has_column()` 这两个探针**故意**靠一条
        会失败的 SELECT 判断列在不在，还把异常吞掉继续跑 —— 不补这一条，探针在
        PG 上会连坐掉它后面所有语句，症状是一句风马牛不相及的报错。

        `atomic()` 块里不动：那一层自己有 SAVEPOINT，`ROLLBACK TO` 是 PG 在
        aborted 态下少数几条还能执行的语句之一，让它自己回，语义与 sqlite 分支一致。
        """
        if self.atomic_depth:
            return
        try:
            from psycopg.pq import TransactionStatus  # noqa: PLC0415 —— 惰性
            if self._raw.info.transaction_status == TransactionStatus.INERROR:
                self._raw.rollback()
        except Exception:                            # noqa: BLE001 —— 捞不回来就算了
            log.debug("PG 事务状态捞不回来，交给上层处理", exc_info=True)


#: 进程内的 PG 连接池，按 DSN 一条。**刻意不按 store 一条**：退款域的表都在同一个
#: 库里，一个进程开十几条连接只会把连接数打满；而「同一条连接 + 一把锁」正是
#: SQLite 侧的语义（见模块 docstring 第 1 条）。
_PG_CONNS: dict[str, _PgConnection] = {}
_PG_LOCKS: dict[str, threading.RLock] = {}
_POOL_LOCK = threading.RLock()


def _pg_connect(dsn: str) -> _PgConnection:
    from maos.store import pg_store                  # noqa: PLC0415 —— 惰性，见下

    psycopg = pg_store._driver()
    try:
        raw = psycopg.connect(
            dsn,
            autocommit=False,
            connect_timeout=pg_store.DEFAULT_CONNECT_TIMEOUT,
            row_factory=pg_store._dict_row(),
        )
    except Exception as exc:                         # noqa: BLE001 —— 驱动异常不外漏
        raise pg_store.PgBackendUnavailable(
            f"连不上 PG（{pg_store.DSN_ENV} 已配置）：{pg_store._redact(exc)}。"
            " 这里抛错而不是回落 sqlite —— 选了 postgres 就必须是 postgres，"
            " 回落的后果是你以为验过了 PG，其实一行都没跑。"
        ) from exc
    return _PgConnection(raw)


def pg_connection() -> tuple[_PgConnection, threading.RLock]:
    """拿本进程那条 PG 连接与配套的锁。没装驱动 / 没配 DSN / 连不上一律抛。"""
    from maos.store import pg_store                  # noqa: PLC0415

    dsn = os.environ.get(pg_store.DSN_ENV, "")
    if not dsn:
        raise pg_store.PgBackendUnavailable(
            f"{BACKEND_ENV}={POSTGRES} 但没有连接串：DSN 只从环境变量"
            f" {pg_store.DSN_ENV} 读（铁律 6：密钥不落文件），当前未配置。"
            " 这里抛错而不是回落 sqlite —— 回落的话「业务域跑在 PolarDB 上」是假的。")
    with _POOL_LOCK:
        conn = _PG_CONNS.get(dsn)
        if conn is None or conn._raw.closed:
            conn = _pg_connect(dsn)
            _PG_CONNS[dsn] = conn
            _PG_LOCKS.setdefault(dsn, threading.RLock())
        return conn, _PG_LOCKS[dsn]


def close_pg() -> None:
    """关掉本进程缓存的 PG 连接。给测试与一次性脚本收尾用。"""
    with _POOL_LOCK:
        for conn in _PG_CONNS.values():
            with contextlib.suppress(Exception):
                conn.close()
        _PG_CONNS.clear()
        _PG_LOCKS.clear()


# =================================================================== 对外的门面
def backend_name(env: dict | None = None) -> str:
    """当前业务域后端。缺省 `sqlite`；只认两个字面量，别的抛。

    **不回落缺省**：环境变量拼错一个字母就静默跑 sqlite，比直接报错难查一个量级
    —— 你会以为在验 PG，其实一行 PG 代码都没执行（口径同
    `maos/store/__init__.py::create_store`）。
    """
    source = os.environ if env is None else env
    name = (source.get(BACKEND_ENV) or DEFAULT_BACKEND).strip().lower()
    if name not in (SQLITE, POSTGRES):
        raise ValueError(
            f"未知的 {BACKEND_ENV}={name!r}：只认 {SQLITE!r} 或 {POSTGRES!r}。")
    return name


#: **装配级**后端标记挂在 store 上的属性名（T126）。
#:
#: 为什么需要它，而 `MAOS_DOMAIN_BACKEND` 不够：那个变量是**进程级**的，
#: 一设就把这个进程里每一条业务域连接都拨到 PG —— 包括那些**按设计就该是
#: 一次性副本**的库。`maos/roundtable/stages.py::facts_finance_preview` 的原话是
#: 「走的是与 DAG 里逐字相同的三个 skill，只是库换成 `:memory:` 的一次性副本」：
#: 它用 `_memory_store()` 现造一个内存库，跑完即弃，写进去的 `refund_case`
#: 本来到不了真库。
#:
#: 进程级开关让这层隔离**无声消失**：预演写的 case（`plan_id='preview'`）落进真
#: PG 库，紧接着真跑的 `refund.intake` 撞上 `guard.create_case` 的幂等闸
#: （「库里 'preview'、这次 'plan_xxx'」），三次重试全败、整条 DAG 停在第一步。
#: 实测如此（T126 回执），而且症状指向受理幂等，不指向后端开关。
#:
#: 所以本模块把「这条 DAG 的业务域落在哪」变成**装配的属性**而不是进程的属性：
#: 经 `flows/common.build()` 装配出来的 store 带标记 -> PG；`_memory_store()` 这类
#: 不经装配的一次性库没标记 -> 照旧 sqlite，一次性副本的语义原样保住。
#: `MAOS_DOMAIN_BACKEND` 的进程级语义**一个字没动**（90 条 PG 门控测试按它写）。
STORE_BACKEND_ATTR = "_maos_domain_backend"


def mark_store_backend(store: Any, backend: str | None) -> None:
    """给这条 store 打上「本次装配的业务域后端」。`None` / 空串是摘掉标记。

    只认两个字面量，别的当场抛 —— 与 `backend_name()` 同一条「不回落缺省」的口径。
    """
    if not backend:
        with contextlib.suppress(AttributeError):
            delattr(store, STORE_BACKEND_ATTR)
        return
    name = str(backend).strip().lower()
    if name not in (SQLITE, POSTGRES):
        raise ValueError(
            f"未知的业务域后端 {backend!r}：只认 {SQLITE!r} 或 {POSTGRES!r}。")
    setattr(store, STORE_BACKEND_ATTR, name)


def backend_of(store: Any) -> str:
    """这条 store 该用哪个后端：**先看装配级标记，没有才读环境变量**。

    没标记时逐字节退回 `backend_name()`，所以不打标记的调用方一个字节都不受影响。
    """
    marked = getattr(store, STORE_BACKEND_ATTR, None)
    if not marked:
        return backend_name()
    name = str(marked).strip().lower()
    if name not in (SQLITE, POSTGRES):
        raise ValueError(
            f"store 上的业务域后端标记 {marked!r} 不认：只认 {SQLITE!r} 或 {POSTGRES!r}。")
    return name


class DomainConn:
    """业务域的一条连接 —— 「拿连接、执行、事务、建表」四件事的收口。

    `raw` 与 `lock` 两个属性是**故意暴露**的：`guard.py` 直接拿它们用
    （`objects._conn()` / `objects.lock_of()` 的返回值就是这两个），
    而 guard.py 不在本轨白名单里。
    """

    def __init__(self, backend: str, raw: Any, lock: Any) -> None:
        self.backend = backend
        self.raw = raw
        self.lock = lock
        #: sqlite 分支的 `atomic()` 嵌套深度。**记在实例上而不是连接上**：
        #: sqlite 连接是 `sqlite3.Connection`，挂不了自定义属性；而 sqlite 侧
        #: 走 `atomic()` 的只有迁移那一条路径，全程用同一个 DomainConn 实例。
        #: PG 侧相反 —— 深度记在共享的 `_PgConnection` 上（见 `_enter_atomic`）。
        self._sqlite_depth = 0

    @property
    def dialect(self) -> str:
        return self.backend

    @classmethod
    def open(cls, store: Any) -> DomainConn:
        """选后端交一条能用的连接：**先看 store 上的装配级标记，没有才读环境变量**。

        sqlite 分支的行为与本模块出现之前**逐字节一致**：同一条 `store._conn`、
        同一把 `store._lock`。没打标记的 store 走的还是 `MAOS_DOMAIN_BACKEND`
        那条老路（见 `backend_of` 与 `STORE_BACKEND_ATTR`）。
        """
        backend = backend_of(store)
        if backend == POSTGRES:
            conn, lock = pg_connection()
            return cls(POSTGRES, conn, lock)

        raw = getattr(store, "_conn", None)
        if raw is None:
            raise TypeError(
                f"{type(store).__name__} 没有暴露 sqlite 连接，业务域的新增表无处落库。"
                f" 换后端走 {BACKEND_ENV}={POSTGRES}，不要去改冻结的 store.py。")
        lock = getattr(store, "_lock", None)
        return cls(SQLITE, raw, lock if lock is not None else contextlib.nullcontext())

    # -- 四件事 ---------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """一条语句一次提交。`atomic()` 块里不提交（那一层收口）。"""
        with self.lock:
            self.raw.execute(sql, tuple(params))
            if not self._in_atomic:
                self.raw.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.lock:
            rows = self.raw.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def executescript(self, ddl: str) -> None:
        """建表。sqlite 直接执行，postgres 由 `_PgConnection` 先过 `to_pg_ddl()`。"""
        with self.lock:
            self.raw.executescript(ddl)
            self.raw.commit()

    @contextlib.contextmanager
    def atomic(self, name: str) -> Iterator[DomainConn]:
        """一组语句同生共死，**DDL 也算在内**。两个后端同一套 SAVEPOINT 语义。

        用 `SAVEPOINT` 而不是 `BEGIN`：外层已经在事务里时 `BEGIN` 会报
        cannot start a transaction within a transaction，`SAVEPOINT` 开不开事务都能用。
        SQLite 还多一条理由 —— 传统模式下只有 DML 隐式开事务，`DROP TABLE` 一发就
        落盘，显式发一句 `SAVEPOINT` 才拉得进事务。
        """
        with self.lock:
            self._enter_atomic()
            try:
                self.raw.execute(f"SAVEPOINT {name}")
                try:
                    yield self
                except BaseException:
                    self.raw.execute(f"ROLLBACK TO {name}")
                    self.raw.execute(f"RELEASE {name}")
                    self.raw.rollback()
                    raise
                self.raw.execute(f"RELEASE {name}")
                self.raw.commit()
            finally:
                self._exit_atomic()

    # -- 内部 ------------------------------------------------------------------
    @property
    def _in_atomic(self) -> bool:
        return bool(getattr(self.raw, "atomic_depth", 0)) or self._sqlite_depth > 0

    def _enter_atomic(self) -> None:
        if self.backend == POSTGRES:
            self.raw.atomic_depth += 1
        else:
            self._sqlite_depth += 1

    def _exit_atomic(self) -> None:
        if self.backend == POSTGRES:
            self.raw.atomic_depth -= 1
        else:
            self._sqlite_depth -= 1


def add_column_if_missing(conn: DomainConn, table: str, col: str, decl: str) -> None:
    """没有这一列就加上。SQLite 的 `ALTER TABLE ADD COLUMN` 没有 `IF NOT EXISTS`。

    探针两个后端各一套：SQLite 走 `PRAGMA table_info`，PG 走 `information_schema`。
    列声明只写一份 SQLite 方言的（`REAL` / `TEXT` / …），PG 侧现翻 —— 与建表同一个
    翻译器，两处不会漂。

    表名列名都是调用方的字面量、不是外来输入，所以直接拼进 SQL（口径同
    `objects._has_column`）。
    """
    if conn.dialect == POSTGRES:
        # 占位符写 `?` 不写 `%s`：这一层收的是 **SQLite 方言**，`%s` 会被
        # `translate_placeholders` 当成字面量 `%` 转义成 `%%s`，然后 psycopg 报
        # 「0 placeholders but 2 parameters」——而那句话完全不提示是方言写反了。
        rows = conn.query(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ?"
            " AND column_name = ?",
            (table, col))
        if rows:
            return
    else:
        have = {r["name"] for r in conn.query(f"PRAGMA table_info({table})")}
        if col in have:
            return

    # 两个后端发同一句 SQLite 方言的 ALTER。**这里不预先翻译**：翻不翻是
    # `_PgConnection.execute()` 的事，它见到 DDL 会走 `to_pg_ddl()`。
    # 在这里先翻一遍的后果是翻两次 —— 第二次拿到的是已经翻好的
    # `double precision`，而那不是翻译器认识的 SQLite 类型，当场抛
    # `UnsupportedDdlError`，报错还指着一句本来没错的 DDL。
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
