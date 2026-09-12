"""StorePort 的 PostgreSQL 后端 —— P5 填实，全文走 tsvector、向量走 pgvector。

P1 留的是空壳（五个方法全 `raise NotImplementedError`），本模块把它填成真实现，
并在本机 `pgvector/pgvector:pg16` 上实测跑通。实测输出与 PolarDB 的迁移口径见
`deploy/polardb.md`；那份文档**分「已实测 / 未实测」两栏**，PolarDB 实例本身没连过。

连接串只从环境变量 `MAOS_PG_DSN` 读（铁律 6：密钥不落文件）。DSN 里通常带口令，
所以 `__repr__` 只报「配了 / 没配」，且**任何一条错误信息里都不插 `self.dsn`** ——
免得它顺着某份 traceback 或某个 evidence 文件漏出去。

## 为什么「后端不可用」抛的是 `NotImplementedError` 的子类

`maos/tests/test_store_port.py::test_postgres_shell_raises_on_every_operation` 是
冻结的 28 条之一，它拿一个**连不上的** DSN 构造本类，断言四个方法全抛
`NotImplementedError`。那条测试守的是契约甲 —— **不许静默回落 sqlite**：PG 后端
拿不到库时必须当场响，绝不能悄悄给一个能用的 sqlite，否则「PG 验过了」是假的，
而没有任何东西提示你该重验。

填实之后「拿不到库」仍然是常态（驱动没装、DSN 没配、库没起），所以这里定义
`PgBackendUnavailable(NotImplementedError)`：既让那条冻结测试在**有驱动和无驱动
两种环境下都绿**，又把契约甲的语义原样保住 —— 抛，不回落。它是 `NotImplementedError`
不是因为「代码没写」，而是因为**这个后端此刻确实提供不了这项能力**，两者对调用方
是同一件事：别把结果当真。

## 与 SQLite 后端的已知差异（`deploy/polardb.md` 有完整清单）

1. **占位符方言不同**：SQLite 用 `?`，PG 用 `%s`。这条没法在本层安全地自动翻译
   （`?` 也是 PG 的 jsonb 算子，字符串字面量里的 `?` 更不能动），所以**不翻译**，
   只在检测到「传了参数、SQL 里有 `?` 却没有 `%s`」时抛一条说人话的 ValueError。
2. **标识符大小写**：PG 把不加引号的标识符折成小写，SQLite 不折。本层沿用 sqlite
   适配器的做法**不加引号**（加了引号 `"KB_Doc"` 就要求精确匹配，反而更容易踩），
   校验形状后直接拼。
3. **向量维度不匹配**：SQLite 侧逐行比对、能点名是哪一行；PG 侧由 pgvector 在查询
   层一次性报错，**报不出行号**。两边都抛 `ValueError`、都不跳过，但信息量不同。
4. **全文分数不是同一把尺子**：`ts_rank` 缺省不做文档长度归一，`bm25` 做。本层因此
   给 `ts_rank` 传了 normalization（见 `FTS_RANK_NORMALIZATION`），让两边的**名次**
   一致 —— 检索器按名次归一，名次不一致等于「换后端悄悄改排序」。**分数本身仍然
   不可跨后端比较**，别去比绝对值。
5. **中文全文**：见下。
6. **PG 侧比 SQLite 多一列**（T139 拍的板，见 `pg_schema.sql` 的「向量通道」一节）：
   `kb_doc.embedding_vec vector(N)`，`GENERATED ALWAYS AS` 生成列，由权威列
   `embedding`（TEXT，两个后端同形）派生，上面建 HNSW。权威仍在 TEXT 那一列，
   SQLite 侧一个字没动。加速列建不出来（云上普通账号建不了 `vector` 扩展）时读路径
   自动回落，回落之后的行为与 T139 之前逐字相同。
7. **PG 的全文查影子表，SQLite 查 FTS5 虚表**，两边其实是同一张 `kb_doc_fts`。
   T139 之前 PG 查的是 `kb_doc` 的原文列，于是 PG 自己再切一遍词，与
   `kb.fts_text()` 的口径对不上 —— 按错误码检索恒不命中且不报错。改查影子表之后
   切词在全仓库只剩 `kb.tokenize()` 一处。详见 `_FTS_SHADOW` 与 `_fts_terms()`。

## 中文全文检索的口径（本轨的选择，理由记在 docs/DECISIONS.md）

PG 不自带中文分词。缺省配置 `simple` 对 `to_tsvector` 而言把一整串汉字当**一个
token**，「退款政策超时未到账」整条是一个词 —— 查「退款政策」一条都命不中，
**而且不报错**。这跟 SQLite 侧缺省 unicode61 的毛病是同一个，`maos/kb/schema.sql`
第 41 行起记着同一条坑。

本层的处理是**照 F-2 原话办：「后端没准备好」不许伪装成「没命中」**。所以：

- 查询串含 CJK 字符、而当前文本检索配置是 PG 的内置配置（内置的一个都没有中文
  分词器）→ **抛 `LookupError`**，报错里写清怎么修。检索器 `maos/kb/retriever.py`
  的 `_port_search` 捕获异常即把该通道判定为不可用、退化为本模块的本地实现 ——
  中文召回因此仍然是好的，只是不经过 PG。
- 非 CJK 查询照常走 `to_tsvector` / `ts_rank`，是真跑通的 PG 全文通道。
- 装了 `zhparser` / `pg_jieba` 的部署，把 `MAOS_PG_FTS_CONFIG` 指向那个配置即可，
  本层立刻把 CJK 查询也交给 PG。**升级路径是一个环境变量，不用改代码。**

⚠️ 一条必须知道的连带后果：`_port_search` 是「探一次记一次」，一次 CJK 查询抛错就
把该 store 的全文通道**永久**标记为不可用，之后连非 CJK 查询也走本地实现。在本仓库
这种中文语料上，等于 PG 全文通道基本不会被用上 —— 这是如实的结果，不是缺陷伪装。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("maos.store.pg")

#: `dialect()` 的返回值，F-2 只认 "sqlite" | "postgres" 两个字面量。
DIALECT = "postgres"

#: 连接串的唯一来源。禁止写进任何文件，禁止出现在 evidence/ 里。
DSN_ENV = "MAOS_PG_DSN"

#: 连接超时（秒）。给缺省值是为了让「连不上」快速响而不是挂住整个测试。
CONNECT_TIMEOUT_ENV = "MAOS_PG_CONNECT_TIMEOUT"
DEFAULT_CONNECT_TIMEOUT = 5

#: 文本检索配置。缺省 `simple`；装了中文分词扩展的部署把它指过去即可。
FTS_CONFIG_ENV = "MAOS_PG_FTS_CONFIG"
DEFAULT_FTS_CONFIG = "simple"

#: **方言开关**：打开之后本层接收 SQLite 方言的 SQL（`?` 占位符、
#: `INSERT OR REPLACE`、SQLite DDL），发出去之前现翻成 PG 方言。缺省**关**。
#:
#: 为什么是开关而不是无条件翻译（这是对 `docs/DECISIONS.md` 2026-08-29 那条
#: 「不翻译」的**有条件**修订，理由记在 DECISIONS 的 `## task-t115` 小节）：
#:
#: · 那条决策的理由今天仍然成立 —— `?` 同时是 PG 的 jsonb 算子，字符串字面量里的
#:   `?` 更不能动。所以翻译器是**按引号状态扫一遍**的（`_dbport.translate_placeholders`），
#:   不是 `str.replace`；`WHERE note LIKE '%?%'` 逐字保住，有测试钉着。
#: · 缺省关着，`maos/tests/test_pg_store_live.py` 那条「`?` 要报得说人话」的断言
#:   逐字不变，别的调用方也一个字节都不受影响。
#: · 打开它是为了**知识层**：`kb_doc` 的建表、写入（`kb.upsert_doc`）与阶段一
#:   预过滤都是同一份 SQLite 方言的 SQL，它们跨轨契约里不归本轨改。不给这个开关，
#:   「知识层跑在 PolarDB 上」就只能靠在 `maos/kb/**` 里散着写方言分支 —— 那是
#:   把一处收口换成十处分叉。
SQLITE_DIALECT_ENV = "MAOS_PG_SQLITE_DIALECT"

#: HNSW 的检索深度。**必须显式设**，不能吃服务端缺省 —— 这是本文件最容易
#: 无声退化的一处。
#:
#: 症状（实测，见 deploy/polardb-live.md §3.6 与 BACKLOG `## polardb-live-r2`）：
#: `ef_search=40` 时召回 **99.3%**、延迟 0.6ms；调到 10 就掉到 **85%–90%**，
#: 而且**不稳定** —— 同一份数据、同一组查询，换一次索引构建就能从 100% 掉到
#: 89.7%。也就是说低 `ef_search` 下的召回率是「这一次索引怎么建出来的」的
#: 函数，不是数据的函数，复现性本身就没了。
#:
#: 不设的后果比数值本身更糟：换一台实例、换一个 pgvector 版本，或有人在实例
#: 参数里改了这个值，**向量召回会静默变化 —— 不报错、不变慢，结果悄悄变差**。
#: 这正是铁律 8 要防的那类无症状假象，也是最难被发现的一种退化。显式 SET 把它
#: 从「环境的缺省」变成「代码的选择」。
#:
#: 取 40 是因为它就是 pgvector 的缺省值：在两个规模上都稳定 99.3%、延迟只有
#: 0.6ms，**没有理由往下调**。留环境变量是给「召回要求更高、愿意换延迟」的
#: 部署往上调用的，不是给往下调的。
HNSW_EF_SEARCH_ENV = "MAOS_PG_HNSW_EF_SEARCH"
DEFAULT_HNSW_EF_SEARCH = 40

#: `ts_rank` 的 normalization 位掩码。**必须传** —— 缺省的 0 不做文档长度归一。
#:
#: 症状（实测，T10 记在 BACKLOG、T18 在自己的库上复跑确认）：同一条查询 `timeout`、
#: 同一份语料，PG 侧 `d1`(6 词) 与 `d2`(1 词) **同分 0.06079271**，并列后按 id 升序
#: 排成 `['d1', 'd2']`；而 SQLite 侧 `-bm25` 给 `d2` 严格高于 `d1`，排成 `['d2', 'd1']`。
#: 两边都满足 F-2 的「越大越相关、降序、同分按 id 升序」，所以**谁都不报错**，
#: 但**名次不同** —— 而 `maos/kb/retriever.py` 的 `_rank_normalize` 正是按名次归一的，
#: 于是「换个后端，混合召回的最终排序悄悄变了」。铁律 8 要防的正是这类无症状假象。
#:
#: 取 2 =「除以文档长度」，与 bm25 的长度惩罚同向。**口径是以本地 `-bm25` 为准**：
#: 本地是缺省路径、是全部现有测试与演示的基准，让 PG 向它对齐影响面最小
#: （docs/DECISIONS.md 有这一行）。
#:
#: 为什么不取 8 / 16（「除以唯一词数」）：实测在长度差异大的语料上它们与本地**对不上**
#: —— 一篇 61 词、但只有 3 个唯一词的文档会被判得比 9 词 9 个唯一词的更相关，PG 排
#: `['e1', 'e3', 'e2']`，本地排 `['e1', 'e2', 'e3']`。1 与 2 在实测语料上都对得上，
#: 取 2 是因为它归的是真·文档长度，8/16 归的是词表大小。见 maos/tests/test_pg_rank_parity.py。
#:
#: ⚠️ 这只对齐**名次**，不对齐**分数**。两边的分数仍然不可跨后端比较。
FTS_RANK_NORMALIZATION = 2

#: PG 16 自带的全部文本检索配置。**一个都没有中文分词器** —— 所以「配置在这张表里」
#: 等价于「这个部署没装中文分词」。不在表里的配置是运维自己装的，本层信任它。
_PG_BUILTIN_FTS_CONFIGS = frozenset({
    "arabic", "armenian", "basque", "catalan", "danish", "dutch", "english",
    "finnish", "french", "german", "greek", "hindi", "hungarian", "indonesian",
    "irish", "italian", "lithuanian", "nepali", "norwegian", "portuguese",
    "romanian", "russian", "serbian", "simple", "spanish", "swedish", "tamil",
    "turkish", "yiddish",
})

#: 表名 / 列名要拼进 SQL（标识符没法用占位符绑定），所以拼之前先卡死形状。
#: 参数一律走 `%s`，一个都不拼 —— 这两条合起来才算「不拼 SQL」。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: CJK 表意文字 + 假名 + 谚文。用来判断「这条查询需不需要中文分词器」。
_CJK = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]"
)

#: 错误信息里可能夹带凭证的两种形状：`password=xxx` 与 `scheme://user:pass@host`。
_SECRETISH = (
    re.compile(r"(password\s*=\s*)\S+", re.IGNORECASE),
    re.compile(r"(://)[^/@\s]*@"),
)

_INSTALL_HINT = (
    "PG 后端的驱动是**可选依赖**（核心零运行时依赖，见 pyproject.toml 的"
    " dependencies = []）：`pip install -e '.[pg]'`（或直接 `pip install"
    " 'psycopg[binary]'`）再试。"
)


class PgBackendUnavailable(NotImplementedError):
    """PG 后端此刻服务不了这次调用：驱动没装 / DSN 没配 / 连不上库。

    继承 `NotImplementedError` 是**有意的**，理由见模块开头那一节：契约甲要求
    选了 postgres 就当场响、绝不回落 sqlite，而冻结的 28 条正是拿
    `NotImplementedError` 钉住这条。别改成别的基类 —— 改了 28 条里那条当场红，
    而且是在「有驱动」和「无驱动」两种环境下红得不一样，最难查。
    """


#: 布尔环境变量认的关值。与 `kb.kb_enabled()` 同一份口径，只是方向相反：
#: 这个开关缺省**关**，所以认的是开值。
_TRUE_VALUES = ("1", "true", "yes", "on")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _redact(text: str) -> str:
    """把驱动报错里可能夹带的凭证抹掉再往上带（铁律 6）。"""
    out = str(text)
    for pattern in _SECRETISH:
        out = pattern.sub(r"\1<已脱敏>", out)
    return out


def _ident(kind: str, name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(
            f"非法的{kind}名 {name!r}：只允许字母、数字、下划线，且不以数字开头。"
            " 标识符是拼进 SQL 的，这里不卡形状就等于开了一条注入路径。"
        )
    return name


def _driver() -> Any:
    """惰性 import psycopg。**模块级不许 import** —— 核心是零运行时依赖。"""
    try:
        import psycopg  # noqa: PLC0415 —— 惰性 import 是本模块的硬要求
    except ImportError as exc:
        raise PgBackendUnavailable(
            f"没装 PostgreSQL 驱动，PG 后端起不来。{_INSTALL_HINT}"
            " 这里显式抛错而不是回落 sqlite：回落的话你会以为 PG 验过了，"
            " 而实际上一行 PG 代码都没执行。"
        ) from exc
    return psycopg


def _dict_row() -> Any:
    from psycopg.rows import dict_row  # noqa: PLC0415

    return dict_row


def _check_placeholders(sql: str, params: tuple) -> None:
    """SQLite 用 `?`、PG 用 `%s`。撞上了就说人话，别让 psycopg 报语法错。"""
    if params and "?" in sql and "%s" not in sql:
        raise ValueError(
            "这条 SQL 用的是 SQLite 的 `?` 占位符，PG 的占位符是 `%s`。"
            " 本层**不做自动翻译**：`?` 同时是 PG 的 jsonb 算子，字符串字面量里的"
            " `?` 更不能动，机器改写迟早改错一条而且没有症状。换后端时这条 SQL"
            " 要自己改，`deploy/polardb.md` 的「已知差异」栏列了全部这类差异。"
        )


#: PG 侧那份「翻译器翻不出来」的 DDL。
_PG_SCHEMA_PATH = Path(__file__).with_name("pg_schema.sql")

#: `kb_extra_statements()` 要滤掉的语句。建扩展要高权限账号（云上普通账号建不了，
#: 见 deploy/polardb-live.md §1.3），挂在建表路径上会让整个知识层在那种实例上起不来。
_EXTENSION_HEAD = re.compile(r"^\s*CREATE\s+EXTENSION\b", re.IGNORECASE)

#: `pg_schema.sql` 里的嵌入维度占位符。**唯一事实源是 `retriever.EMBED_DIM`** ——
#: SQL 文件里再写一个字面量 64 的后果是：改了 EMBED_DIM 之后 Python 侧算 128 维、
#: PG 侧的列还是 vector(64)，每一行的 cast 都失败回 NULL，整条向量通道**静默**退回
#: 纯 Python 余弦。不报错、不变慢，只是召回悄悄不走索引了 —— 铁律 8 要防的那类假象。
EMBED_DIM_PLACEHOLDER = "@EMBED_DIM@"

#: 渲染之后不许再有这个形状的东西剩下。漏一个就当场抛，好过发给 PG 当语法错 ——
#: 语法错的报文里看不出「是占位符没渲染」这件事。
_PLACEHOLDER_RE = re.compile(r"@[A-Z][A-Z0-9_]*@")

#: 向量加速列的命名规则：`<权威列>` + 本后缀。`embedding` -> `embedding_vec`。
#: 建列 DDL 在 `pg_schema.sql`，读路径的解析在 `PgStorePort._vector_accel()`，
#: 两处靠这一个常量对齐。
VECTOR_ACCEL_SUFFIX = "_vec"

#: 全文通道在 PG 上的**实际查询目标**：`kb_doc` 的全文查影子表 `kb_doc_fts`。
#:
#: 理由见 `pg_schema.sql` 里 `kb_doc_fts` 那一节的红字，一句话是：影子表存的正是
#: `kb.fts_text()` 的产物，而 `kb_doc` 的 title/body 是原文。查原文时 PG 自己按
#: 默认 parser 的 file/host 规则切，`ACQ.TRADE_NOT_EXIST` 整串是一个 token，于是
#: **按错误码检索恒不命中且不报错**（BACKLOG `## task-t132` 第 1 条）。
#:
#: 表不在（影子表还没建的旧库）就回落查原表 —— 那是 T139 之前的行为，仍然可用，
#: 只是错误码那条通道照旧是瞎的。回落由 `_fts_target()` 探一次记一次。
_FTS_SHADOW = {"kb_doc": "kb_doc_fts"}


#: 本层在 PG 侧额外加出来的列。**它们不许出现在 `query()` 的结果里。**
#:
#: 加速列是本后端的实现细节，权威列始终是 `embedding`。但 `kb_doc` 的读路径大多是
#: `SELECT *`（`retriever.prefilter` / `kb.get_doc` / `kb.list_docs`），加一列的直接
#: 后果是**同一条 SQL 在两个后端上返回的行不再相等** —— `test_kb_pg_prefilter.py::
#: test_prefilter_limit_is_honoured_on_both` 那条「换后端不换语义」当场破，案例证据束
#: 的「业务状态与 SQLite 束逐字一致」也跟着破。差异是本层自己制造的，就由本层收掉。
#:
#: 逃生口是 `_raw_query()`：本层内部要看加速列（探测维度、验双写）都走它，不过滤。
_ACCEL_COLUMNS = frozenset({f"embedding{VECTOR_ACCEL_SUFFIX}"})


def _without_accel_columns(row: Any) -> dict:
    """一行结果去掉本层的加速列。见 `_ACCEL_COLUMNS`。"""
    out = dict(row)
    if not _ACCEL_COLUMNS.isdisjoint(out):
        for name in _ACCEL_COLUMNS:
            out.pop(name, None)
    return out


def embed_dim() -> int:
    """向量加速列的维度。**从 `retriever.EMBED_DIM` 取，不在本模块另写一个数。**

    惰性 import 的理由与 `_dialect` 那处相同：`maos.store` 是内核侧的可插拔面，
    模块级依赖知识层会把 `maos.kb` 挂到内核的 import 图上。调用时机都在建表 /
    检索路径上，那时 `maos.kb` 早已加载完。
    """
    from maos.kb.retriever import EMBED_DIM        # noqa: PLC0415 —— 惰性，见 docstring

    return int(EMBED_DIM)


def rendered_schema_sql() -> str:
    """`pg_schema.sql` 的可执行形态：占位符已替换成真实维度。

    手跑本文件时**不能直接 `psql -f`**，要先过这里：

        python3 -c "from maos.store.pg_store import rendered_schema_sql as r; print(r())" \\
            | psql -U <user> -d <db> -f -

    忘了渲染的话 `@EMBED_DIM@` 是语法错，当场炸 —— 有意的，好过静默建错维度。
    """
    text = _PG_SCHEMA_PATH.read_text(encoding="utf-8")
    out = text.replace(EMBED_DIM_PLACEHOLDER, str(embed_dim()))
    left = sorted(set(_PLACEHOLDER_RE.findall(out)))
    if left:
        raise ValueError(
            f"pg_schema.sql 里还剩没渲染的占位符 {left}。每个占位符都要在本模块里有"
            " 一条替换规则，加了新的就把规则一起加上 —— 漏渲染的语句发到 PG 上只会"
            " 报语法错，那条报文里看不出真正的原因。"
        )
    return out


def kb_extra_statements() -> list[str]:
    """`pg_schema.sql` 里知识层建表要跟着跑的那几条，已滤掉 `CREATE EXTENSION`。

    单一事实源：这几条 DDL 只写在 `pg_schema.sql` 里，Python 侧不另抄一份 ——
    抄一份的后果是「手跑那份文件」与「ensure_schema 自动跑的那份」形状不同，
    而两边都不报错。

    走 `rendered_schema_sql()` 而不是直接读文件：向量加速列那条 DDL 带维度占位符。
    """
    from maos.domain import _dbport                # noqa: PLC0415 —— 惰性，理由同 _dialect

    return [s for s in _dbport.split_statements(rendered_schema_sql())
            if not _EXTENSION_HEAD.match(s)]


class PgStorePort:
    """StorePort 的 PG 实现。F-2 五个签名逐字未动，只往里填实现。"""

    def __init__(self, dsn: str | None = None, *,
                 sqlite_dialect: bool | None = None) -> None:
        self.dsn = dsn if dsn is not None else os.environ.get(DSN_ENV, "")
        self._conn: Any = None
        #: 见 `SQLITE_DIALECT_ENV`。显式传就用传的，否则读环境变量，缺省关。
        self.sqlite_dialect = (
            _env_flag(SQLITE_DIALECT_ENV) if sqlite_dialect is None else bool(sqlite_dialect))
        self._pk_cache: dict[str, tuple[str, ...]] = {}
        #: `(table, field) -> 加速列名 | None`。见 `_vector_accel()`。
        self._vec_accel_cache: dict[tuple[str, str], str | None] = {}
        #: `影子表名 -> 在不在`。见 `_fts_target()`。
        self._fts_target_cache: dict[str, bool] = {}

    def __repr__(self) -> str:
        # 只报有没有，不报是什么 —— DSN 里通常带口令。
        return f"PgStorePort(dsn={'<已配置>' if self.dsn else '<未配置>'})"

    # -- StorePort 五方法（F-2 冻结签名）---------------------------------------
    def execute(self, sql: str, params: tuple) -> None:
        sql, bound = self._dialect(sql, params)
        if not sql.strip():
            # 翻译之后是空的 —— 只有一种来路：FTS5 虚表那条整段跳过了
            # （`_dbport._fts5_skip`）。psycopg 对空语句会抛「can't execute an
            # empty query」，那句话离真正的原因很远，所以在这里收掉。
            return
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, bound)

    def query(self, sql: str, params: tuple) -> list[dict]:
        sql, bound = self._dialect(sql, params)
        if not sql.strip():
            return []
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, bound)
            if cur.description is None:
                return []
            return [_without_accel_columns(row) for row in cur.fetchall()]

    # -- 方言 ------------------------------------------------------------------
    def _dialect(self, sql: str, params: tuple) -> tuple[str, tuple | None]:
        """方言开关关着就原样过（只做那条说人话的占位符检查）；开着就现翻。

        翻译之后**参数为空时递 `None` 而不是 `()`**：psycopg 只在传了参数的那次
        调用里解析 `%`，递空元组会让它去解析建表 DDL 里的 `%`（`DEFAULT '100%'`
        这种），报一句与真正原因毫不相干的 `unsupported format character`。
        """
        bound = tuple(params or ())
        if not self.sqlite_dialect:
            _check_placeholders(sql, bound)
            return sql, bound
        from maos.domain import _dbport            # noqa: PLC0415 —— 惰性，见下

        # 惰性 import：`maos.store` 是内核侧的可插拔面，模块级依赖 `maos.domain`
        # 会把业务域挂到内核的 import 图上（铁律 9 那句「内核对 domain 零依赖」）。
        # 翻译器住在 `_dbport` 是因为退款域那条路也要用同一份，两处各一份必漂。
        return _dbport.to_pg_sql(
            sql, self.primary_key, with_params=bool(bound)), (bound or None)

    def primary_key(self, table: str) -> tuple[str, ...]:
        """一张表的主键列，按主键内的列序。查一次记一次。

        `INSERT OR REPLACE` 翻成 `ON CONFLICT (...)` 要它。**从系统目录现查**，
        不在代码里另抄一份：抄一份的后果是别的轨改了主键之后两边悄悄对不上。
        """
        if table in self._pk_cache:
            return self._pk_cache[table]
        from maos.domain import _dbport            # noqa: PLC0415

        keys = tuple(str(r["name"]) for r in self._raw_query(_dbport._PK_SQL, (table,)))
        self._pk_cache[table] = keys
        return keys

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """一组语句同生共死。**不在 F-2 五方法里**，是知识层的能力探测项。

        `maos/kb/__init__.py` 的 `_atomic()` 硬要求端口有它（迁移中途失败会让库
        停在半成品上，那种失效没有症状），`upsert_doc()` 则是有就用、没有就退化
        成逐条提交。本层连接是 `autocommit=True`，psycopg 的 `transaction()` 在
        autocommit 下照样显式开一个事务块，语义与 SQLite 适配器那份一致。
        """
        with self.connect().transaction():
            yield

    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]:
        _ident("表", table)
        _ident("字段", field)
        limit = int(limit)
        if limit <= 0 or not (q or "").strip():
            return []

        config = self.fts_config()
        if _CJK.search(q) and config.lower() in _PG_BUILTIN_FTS_CONFIGS:
            # ⚠️ T139 之后这条抛错**不再是「查了也命不中」**：全文改查影子表，
            #    里面的中文已被 `kb.fts_text()` 按字切开，`simple` 其实匹配得上。
            #    仍然抛，是因为「按字 AND」不是中文检索：「退款政策」与「政策退款」
            #    在它眼里一样，召回偏宽而排序无意义。把这种劣质召回当成「中文通了」
            #    会直接诱出那句不许说的话（`docs/submission-checklist.md:224`
            #    「缺省支持中文分词检索」）。显式退化比悄悄用劣质召回诚实。
            #    装了真分词器的部署走不到这里（配置不在内置表里），判据见
            #    `test_chinese_query_recalls_on_real_tokenizer`。
            raise LookupError(
                f"查询串含中日韩字符，而当前文本检索配置是 PG 内置的 {config!r} ——"
                " 内置配置一个都没有中文分词器，`to_tsvector` 会把整串汉字当成一个"
                " token，子串查询恒不命中**且不报错**。这里抛错而不是返回空集：F-2"
                " 原话「『后端没准备好』不许伪装成『没命中』」。"
                f" 修法：给库装 zhparser 或 pg_jieba，再把 {FTS_CONFIG_ENV} 指向那个"
                " 配置（比如 zhcfg / jiebacfg），本层立刻把中文查询也交给 PG；"
                " 不装就让检索器退化为本地实现，中文召回照常。"
            )

        terms = self._fts_terms(q)
        if not terms:
            # 整条查询里一个可用 token 都没有（全是标点 / 算子字符）。空集是真的
            # 「没命中」，不是「后端没准备好」—— 别往下发一条空的 tsquery。
            return []
        tsquery = " & ".join(terms)
        target = self._fts_target(table)

        # normalization 直接拼进 SQL 而不走 `%s`：它是本模块的 int 常量、不是调用方
        # 传进来的值（`int()` 再拼，形状卡死），而 `ts_rank` 的第三个参数要求解析成
        # `integer` —— 走占位符时驱动会按 Python int 自己挑类型，挑成 numeric 就报
        # 「function ts_rank(tsvector, tsquery, numeric) does not exist」。
        sql = (
            f"SELECT id, ts_rank(to_tsvector(%s, {field}),"
            f" to_tsquery(%s, %s), {int(FTS_RANK_NORMALIZATION)}) AS score"
            f" FROM {target}"
            f" WHERE to_tsvector(%s, {field}) @@ to_tsquery(%s, %s)"
            f" ORDER BY score DESC, id ASC LIMIT %s"
        )
        rows = self._search_query(
            sql, (config, config, tsquery, config, config, tsquery, limit),
            table=target, field=field,
        )
        return [(str(r["id"]), float(r["score"])) for r in rows]

    @staticmethod
    def _fts_terms(q: str) -> list[str]:
        """查询串切成喂给 `to_tsquery` 的 token。**切词借 `kb.tokenize()`，不另写。**

        为什么从 `plainto_tsquery` 换成 `to_tsquery`（BACKLOG `## task-t132` 第 1 条
        给的两条路里更收口的那条）：`plainto_tsquery` 会让 PG **自己再切一遍**，而
        PG 的默认 parser 与 `kb.fts_text()` 的 `_TOKEN_RE` 对英数复合词口径不同 ——
        `acq.trade_not_exist` 在 PG 眼里是一个 token，在 `_TOKEN_RE` 眼里是四个。
        两边各切各的，按错误码检索就恒不命中且不报错。改成「Python 切好、PG 只匹配」
        之后，切词在全仓库只剩 `kb.tokenize()` 一处。

        **注入面**：`&` `|` `!` `:` `*` `(` `)` 都是 `to_tsquery` 的元字符，把用户串
        直接拼进去会让查询变成语法错（抛 `SyntaxError` 给调用方，那是把「查了个怪
        东西」升格成「后端坏了」，检索器会据此把整条通道判死）。所以这里**不信任
        输入**：调用方递进来的通常已经是 `kb.fts_text()` 的产物，但那是约定不是保证。
        再过一遍 `kb.tokenize()` 对已切好的串是幂等的，对没切过的串则把元字符全丢掉。
        判据见 `test_fts_search_survives_hostile_query_text` 与
        `test_tsquery_metacharacters_are_stripped_not_executed`。
        """
        from maos.kb import tokenize                # noqa: PLC0415 —— 惰性，见 embed_dim

        return tokenize(q)

    def _fts_target(self, table: str) -> str:
        """全文实际查哪张表。见 `_FTS_SHADOW`。探一次记一次。

        影子表不在就回落查原表：那是 T139 之前的行为，仍然可用（只是错误码那条
        通道照旧是瞎的）。**回落而不是抛** —— 影子表缺席只影响召回质量，让整条
        知识层因此起不来是比问题本身大得多的破坏。
        """
        shadow = _FTS_SHADOW.get(table)
        if shadow is None:
            return table
        if shadow not in self._fts_target_cache:
            self._fts_target_cache[shadow] = self._relation_exists(shadow)
            if not self._fts_target_cache[shadow]:
                log.warning(
                    "%s 不在，全文回落查 %s 的原文列。按错误码检索（形如"
                    " ACQ.TRADE_NOT_EXIST）在原文列上恒不命中且不报错 —— 建表 DDL 见"
                    " maos/store/pg_schema.sql。", shadow, table)
        return shadow if self._fts_target_cache[shadow] else table

    def _relation_exists(self, name: str) -> bool:
        """当前 search_path 上有没有这张表。目录查询，不靠 try/except 试探。"""
        return bool(self._raw_query(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = ANY(current_schemas(true)) AND table_name = %s",
            (name,)))

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
        if not probe:
            raise ValueError("查询向量是空的，没法算相似度")
        if not any(probe):
            # SQLite 侧同样抛。零向量的余弦无定义，pgvector 会安静地返回 NaN，
            # 排序于是变成随机 —— 两边都必须响，否则「换后端不换语义」是空话。
            raise ValueError("查询向量是零向量，余弦相似度无定义 —— 上游的嵌入多半出错了")

        # pgvector 的 `<=>` 是**余弦距离**（0 最近），而 F-2 要求分数「越大越相关」，
        # SQLite 侧返回的是余弦**相似度**。所以取 1 - 距离，两边同一把尺子。
        # 这一步搞反的症状是排序整个倒过来，且看上去仍然「有结果」。
        literal = "[" + ",".join(repr(x) for x in probe) + "]"
        accel = self._vector_accel(table, field)
        if accel is None:
            # 回落 = T139 之前的行为：直接对权威列发 `<=>`。那一列是 TEXT，PG 报
            # 「operator does not exist: text <=> vector」→ `_search_query` 翻成
            # LookupError → 检索器退化成纯 Python 余弦。**这条路必须留着**：
            # PolarDB 上普通账号建不了 `vector` 扩展（deploy/polardb-live.md §1.3），
            # 那种实例上加速列根本建不出来，整条检索不许因此挂掉。
            sql = (
                f"SELECT id, 1 - ({field} <=> %s::vector) AS score"
                f" FROM {table} WHERE {field} IS NOT NULL"
                f" ORDER BY score DESC, id ASC LIMIT %s"
            )
            rows = self._search_query(sql, (literal, limit), table=table, field=field)
            return [(str(r["id"]), float(r["score"])) for r in rows]

        # 🔴 两层查询，**不是为了好看**。HNSW 索引只在 `ORDER BY <列> <=> <常量>`
        #    这个形状上用得上：planner 认的是算子本身，认不出 `ORDER BY 1 - dist DESC`
        #    与它等价（实测 `EXPLAIN` 里是 `Seq Scan`，换成本形状才是
        #    `Index Scan using idx_kb_doc_embedding_hnsw`）。所以内层按距离升序取
        #    top-limit 走索引，外层再翻成 F-2 要的「相似度降序、同分 id 升序」——
        #    外层只排 ≤limit 行，代价可忽略。判据见
        #    `test_pg_vector_channel_t139.py::test_vector_search_plan_uses_hnsw_index`。
        sql = (
            f"SELECT id, score FROM ("
            f" SELECT id, 1 - ({accel} <=> %s::vector) AS score"
            f" FROM {table} WHERE {accel} IS NOT NULL"
            f" ORDER BY {accel} <=> %s::vector LIMIT %s"
            f") AS hits ORDER BY score DESC, id ASC"
        )
        rows = self._search_query(
            sql, (literal, literal, limit), table=table, field=accel)
        if not rows and self._vector_accel_is_empty(table, field, accel):
            # 加速列在、却一行都没派生出来，而权威列有值 —— 双写没生效（最可能是
            # 建列时 `vector` 扩展还在、后来被 DROP，或有人手工灌数据绕开了生成列）。
            # 这时**空集不是「没命中」**，照原样返回会让检索器把它当真，语义召回
            # 静默归零。抛出去让它退化成纯 Python 余弦，召回照常。
            raise LookupError(
                f"{table}.{accel} 一行都没有派生出向量，而 {table}.{field} 有值 ——"
                " 加速列的双写没生效，返回空集会被当成『真的没命中』。建列 DDL 见"
                " maos/store/pg_schema.sql；它是 GENERATED ALWAYS 生成列，正常情况下"
                " 由 PG 自己跟着每次写入派生，不需要回填。"
            )
        return [(str(r["id"]), float(r["score"])) for r in rows]

    def _vector_accel(self, table: str, field: str) -> str | None:
        """`table.field` 的向量加速列，没有就 `None`（调用方回落到权威列）。

        权威列是 `embedding` 那一列 TEXT（与 SQLite 同构，一份 DDL 两个后端）；
        加速列是 PG 侧多出来的 `embedding_vec vector(N)` 生成列。**这是 T139 拍板的
        形状分叉**，边界写在 `pg_schema.sql` 的「向量通道」一节。

        探一次记一次（schema 事实，一条连接内不会变；`close()` 清缓存）。这一次探测
        顺带守两件**静默失效**：

        1. **维度对不上**就当没有加速列。`ALTER TABLE ADD COLUMN IF NOT EXISTS` 对
           已存在的列是 no-op，所以改了 `EMBED_DIM` 之后老库上的列还是旧维度 ——
           不报错，只是每一行的 cast 都回 NULL，向量通道悄悄退成纯 Python 余弦。
        2. **部分行没派生**记一条 warning。生成列的 cast 失败会回 NULL（有意的：
           加速列不许拦写入），于是坏数据那几行没有加速向量、召回里就少了它们。
           少几条召回不该打死通道，但也不该一声不吭。
        """
        key = (table, field)
        if key in self._vec_accel_cache:
            return self._vec_accel_cache[key]
        accel = _ident("字段", f"{field}{VECTOR_ACCEL_SUFFIX}")
        self._vec_accel_cache[key] = None           # 先记「没有」，任何一步不成立就停在这
        rows = self._raw_query(
            "SELECT a.atttypmod AS dim FROM pg_attribute a"
            " JOIN pg_class c ON c.oid = a.attrelid"
            " JOIN pg_type t ON t.oid = a.atttypid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relname = %s AND a.attname = %s AND t.typname = 'vector'"
            "   AND a.attnum > 0 AND NOT a.attisdropped"
            "   AND n.nspname = ANY(current_schemas(true))",
            (table, accel))
        if not rows:
            return None
        want = embed_dim()
        got = int(rows[0]["dim"])
        if got != want:
            log.warning(
                "%s.%s 是 vector(%s)，而 retriever.EMBED_DIM 是 %s —— 维度对不上，"
                "本次起向量通道不用这条加速列（回落纯 Python 余弦）。ADD COLUMN IF NOT"
                " EXISTS 改不动已存在的列，要么重建这一列，要么把 EMBED_DIM 改回去。",
                table, accel, got, want)
            return None
        counts = self._raw_query(
            f"SELECT count(*) FILTER (WHERE {field} IS NOT NULL) AS txt,"
            f" count(*) FILTER (WHERE {accel} IS NOT NULL) AS vec FROM {table}", ())
        txt, vec = int(counts[0]["txt"]), int(counts[0]["vec"])
        if txt > vec:
            log.warning(
                "%s 里有 %s 行的 %s 有值、却只有 %s 行派生出了 %s —— 差的那 %s 行"
                "多半是解析不了的向量文本或维度对不上的旧向量，它们不会出现在 pgvector"
                "的召回里（本地纯 Python 余弦仍能给分）。加速列的 cast 失败回 NULL 是"
                "有意的：它不许拦住写入。", table, txt, field, vec, accel, txt - vec)
        self._vec_accel_cache[key] = accel
        return accel

    def _vector_accel_is_empty(self, table: str, field: str, accel: str) -> bool:
        """权威列有值、加速列一行都没有？只在查询返回空集时才问，正常路径零成本。"""
        rows = self._raw_query(
            f"SELECT count(*) FILTER (WHERE {field} IS NOT NULL) AS txt,"
            f" count(*) FILTER (WHERE {accel} IS NOT NULL) AS vec FROM {table}", ())
        return int(rows[0]["txt"]) > 0 and int(rows[0]["vec"]) == 0

    def dialect(self) -> str:
        # 方言是静态事实，不是「还没实现的操作」：即使连不上库也答得出。检索器要按
        # 方言分支时（PG 走 tsvector、SQLite 走 FTS5），至少得先问得出自己在哪边。
        return DIALECT

    # -- 连接与配置 ------------------------------------------------------------
    def connect(self) -> Any:
        """拿一条可用连接。驱动缺失 / DSN 未配 / 连不上 → 抛，**绝不回落 sqlite**。"""
        if self._conn is not None and not self._conn.closed:
            return self._conn

        psycopg = _driver()
        if not self.dsn:
            raise PgBackendUnavailable(
                f"没有连接串：PG 后端只从环境变量 {DSN_ENV} 读 DSN（铁律 6：密钥不落"
                " 文件），当前未配置。形如"
                " postgresql://<user>:<pass>@<host>:<port>/<db>。"
            )
        try:
            self._conn = psycopg.connect(
                self.dsn,
                autocommit=True,
                connect_timeout=self.connect_timeout(),
                row_factory=_dict_row(),
            )
        except Exception as exc:  # noqa: BLE001 —— 驱动的异常树不该漏给调用方
            # 不插 self.dsn，只带驱动的原话并过一遍脱敏（铁律 6）。
            raise PgBackendUnavailable(
                f"连不上 PG（{DSN_ENV} 已配置）：{_redact(exc)}。"
                " 这里抛错而不是回落 sqlite —— 契约甲：选了 postgres 就必须是"
                " postgres，回落的后果是你以为验过了 PG，其实一行都没跑。"
            ) from exc
        self._apply_session_params(self._conn)
        return self._conn

    def _apply_session_params(self, conn: Any) -> None:
        """把检索行为钉成代码的选择，而不是环境的缺省。见 HNSW_EF_SEARCH_ENV。

        **失败只记 warning，不抛** —— 这不违反上面那句「绝不回落 sqlite」：

        那条契约管的是**后端身份**（选了 postgres 就必须是 postgres，不许偷偷
        换成别的后端把人骗过去）。`SET hnsw.ef_search` 失败时后端身份没有任何
        变化 —— 这仍是一条真 postgres 连接，全文通道、KV 通道、向量通道全都
        照常工作，向量召回只是回到服务端缺省，也就是**这次改动之前的现状**。

        为它抛错会把整个 store 打死，连根本不碰向量的调用方一起打死；
        「装了 PG 但没装 pgvector」的部署会因此完全不能用 —— 那是比问题
        本身大得多的破坏。而静默吞掉又回到了正要治的病（召回静默变化）。
        所以取中间档：warning 可见、可被测试钉住、不阻断。这与 `verify.py`
        对沙箱降级的处置是同一个取舍 —— 判成失败会让证据根本产不出来，
        静默通过会让这一维凭空消失，warn 正好卡在两者之间。
        """
        ef = self.hnsw_ef_search()
        try:
            with conn.cursor() as cur:
                # 值已过 int()，不是拼进来的外部字符串。
                cur.execute(f"SET hnsw.ef_search = {ef}")
        except Exception as exc:  # noqa: BLE001 —— 调优参数不该打死连接
            log.warning(
                "SET hnsw.ef_search = %s 失败：%s。向量召回将回落到服务端缺省值，"
                "换实例 / 换版本时召回可能静默变化。连接本身正常，其余通道不受影响。",
                ef,
                _redact(exc),
            )

    def hnsw_ef_search(self) -> int:
        """检索深度。取值同 connect_timeout()：非法或非正数一律回缺省。"""
        raw = os.environ.get(HNSW_EF_SEARCH_ENV, "")
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_HNSW_EF_SEARCH
        return value if value > 0 else DEFAULT_HNSW_EF_SEARCH

    def close(self) -> None:
        """关掉缓存的连接。不在 F-2 里，给测试和一次性脚本收尾用。

        顺带清掉两张 schema 探测缓存：连接换了可能连的是另一个库 / 另一条
        search_path，那边的表与列不一定同形。留着旧判定会让「换库之后加速列明明
        建好了却一直不用」这种事没有任何症状。
        """
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None
        self._vec_accel_cache.clear()
        self._fts_target_cache.clear()

    def connect_timeout(self) -> int:
        raw = os.environ.get(CONNECT_TIMEOUT_ENV, "")
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_CONNECT_TIMEOUT
        return value if value > 0 else DEFAULT_CONNECT_TIMEOUT

    def fts_config(self) -> str:
        """当前文本检索配置。标识符要拼进 `to_tsvector` 的参数位，照样卡形状。"""
        raw = (os.environ.get(FTS_CONFIG_ENV, "") or DEFAULT_FTS_CONFIG).strip()
        return _ident("文本检索配置", raw)

    # -- 内部 ------------------------------------------------------------------
    def _raw_query(self, sql: str, params: tuple) -> list[dict]:
        """本层**自己拼的** SQL 直接发，不过方言翻译。

        本模块内部生成的 SQL（两条检索通道、主键目录查询）本来就是 PG 方言：
        `%s` 占位符、`::vector` 转换、`ts_rank(...)`。再过一遍
        SQLite→PG 的翻译只会把 `%s` 的 `%` 转义成 `%%`，报一句
        「0 placeholders but N parameters」—— 那句话完全不提示是自己把自己翻坏了。
        方言开关管的是**调用方递进来**的 SQL，不管本层自己写的。
        """
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]

    def _search_query(
        self, sql: str, params: tuple, *, table: str, field: str
    ) -> list[dict]:
        """两条检索通道共用的收口：把 PG 的异常翻成 F-2 约定的那两类。

        - 表/列不存在、`vector` 扩展没建 → `LookupError`：这是「后端没准备好」，
          检索器据此退化为本地实现（`maos/kb/retriever.py::_port_search`）。
        - 数据形状不对（维度不匹配等）→ `ValueError`，与 SQLite 侧同类。
        """
        psycopg = _driver()
        try:
            return self._raw_query(sql, params)
        except psycopg.errors.UndefinedFunction as exc:
            raise LookupError(
                f"{table}.{field} 的检索通道走不通：{exc}。向量通道需要先在这个库上"
                " 建扩展：CREATE EXTENSION IF NOT EXISTS vector; 建表 DDL 见"
                " maos/store/pg_schema.sql。这里不回落别的实现：静默降级的症状是"
                "「检索看起来通了，召回却一直是空」。"
            ) from exc
        except (
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedColumn,
            psycopg.errors.UndefinedObject,
        ) as exc:
            raise LookupError(
                f"{table}.{field} 的检索通道走不通：{exc}。F-2 约定源表主键列名固定"
                f" 为 id、{field} 列存对应类型（全文是 text，向量是 vector(N)）。"
                " 建表与索引 DDL 见 maos/store/pg_schema.sql。"
            ) from exc
        except psycopg.errors.DataError as exc:
            raise ValueError(
                f"{table}.{field} 上的检索被数据形状挡下：{exc}。最常见的是向量维度"
                " 对不上（换了嵌入模型没重算）。注意 PG 侧由 pgvector 在查询层一次性"
                " 报错，**报不出是哪一行**，SQLite 侧才逐行点名 —— 这是两个后端的"
                " 已知差异，见 deploy/polardb.md。"
            ) from exc
