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

import logging
import os
import re
from typing import Any

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


class PgStorePort:
    """StorePort 的 PG 实现。F-2 五个签名逐字未动，只往里填实现。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn if dsn is not None else os.environ.get(DSN_ENV, "")
        self._conn: Any = None

    def __repr__(self) -> str:
        # 只报有没有，不报是什么 —— DSN 里通常带口令。
        return f"PgStorePort(dsn={'<已配置>' if self.dsn else '<未配置>'})"

    # -- StorePort 五方法（F-2 冻结签名）---------------------------------------
    def execute(self, sql: str, params: tuple) -> None:
        _check_placeholders(sql, params)
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))

    def query(self, sql: str, params: tuple) -> list[dict]:
        _check_placeholders(sql, params)
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]

    def fts_search(self, table: str, field: str, q: str, limit: int) -> list[tuple[str, float]]:
        _ident("表", table)
        _ident("字段", field)
        limit = int(limit)
        if limit <= 0 or not (q or "").strip():
            return []

        config = self.fts_config()
        if _CJK.search(q) and config.lower() in _PG_BUILTIN_FTS_CONFIGS:
            raise LookupError(
                f"查询串含中日韩字符，而当前文本检索配置是 PG 内置的 {config!r} ——"
                " 内置配置一个都没有中文分词器，`to_tsvector` 会把整串汉字当成一个"
                " token，子串查询恒不命中**且不报错**。这里抛错而不是返回空集：F-2"
                " 原话「『后端没准备好』不许伪装成『没命中』」。"
                f" 修法：给库装 zhparser 或 pg_jieba，再把 {FTS_CONFIG_ENV} 指向那个"
                " 配置（比如 zhcfg / jiebacfg），本层立刻把中文查询也交给 PG；"
                " 不装就让检索器退化为本地实现，中文召回照常。"
            )

        # normalization 直接拼进 SQL 而不走 `%s`：它是本模块的 int 常量、不是调用方
        # 传进来的值（`int()` 再拼，形状卡死），而 `ts_rank` 的第三个参数要求解析成
        # `integer` —— 走占位符时驱动会按 Python int 自己挑类型，挑成 numeric 就报
        # 「function ts_rank(tsvector, tsquery, numeric) does not exist」。
        sql = (
            f"SELECT id, ts_rank(to_tsvector(%s, {field}),"
            f" plainto_tsquery(%s, %s), {int(FTS_RANK_NORMALIZATION)}) AS score"
            f" FROM {table}"
            f" WHERE to_tsvector(%s, {field}) @@ plainto_tsquery(%s, %s)"
            f" ORDER BY score DESC, id ASC LIMIT %s"
        )
        rows = self._search_query(
            sql, (config, config, q, config, config, q, limit), table=table, field=field
        )
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
        if not probe:
            raise ValueError("查询向量是空的，没法算相似度")
        if not any(probe):
            # SQLite 侧同样抛。零向量的余弦无定义，pgvector 会安静地返回 NaN，
            # 排序于是变成随机 —— 两边都必须响，否则「换后端不换语义」是空话。
            raise ValueError("查询向量是零向量，余弦相似度无定义 —— 上游的嵌入多半出错了")

        # pgvector 的 `<=>` 是**余弦距离**（0 最近），而 F-2 要求分数「越大越相关」，
        # SQLite 侧返回的是余弦**相似度**。所以取 1 - 距离，两边同一把尺子。
        # 这一步搞反的症状是排序整个倒过来，且看上去仍然「有结果」。
        sql = (
            f"SELECT id, 1 - ({field} <=> %s::vector) AS score"
            f" FROM {table} WHERE {field} IS NOT NULL"
            f" ORDER BY score DESC, id ASC LIMIT %s"
        )
        literal = "[" + ",".join(repr(x) for x in probe) + "]"
        rows = self._search_query(sql, (literal, limit), table=table, field=field)
        return [(str(r["id"]), float(r["score"])) for r in rows]

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
        """关掉缓存的连接。不在 F-2 里，给测试和一次性脚本收尾用。"""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

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
            return self.query(sql, params)
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
