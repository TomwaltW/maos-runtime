"""T13 的机器验收 —— 知识层的检索**真的经由 StorePort 取数**，不是悄悄走本地实现。

## 这一束在守什么

「MAOS 的 RAG 跑在 PolarDB 上」这句话，代码层面成立与否只有一条判据：检索时
`StorePort.fts_search` / `vector_search` **被调用到了**。在 T13 之前它一次都没被调用
过，而且两条独立原因各自都足够（原状记在 BACKLOG `## task-X3` 第 1、2 条）：

* **没有任何真实对象同时满足两边的口径。** `kb._conn()` 只认暴露了 `_conn` 的 store，
  而 `SqliteStorePort` 故意不叫 `_conn`；核心 `SqliteStore` 有 `_conn` 却没有那两个
  方法。于是检索器里那条端口分支在真链路上恒不成立。
* **主键列名对不上。** F-2 约定源表主键列名固定为 `id`，`kb_doc` 的主键却是
  `(tenant_id, doc_id)`，两条通道双双抛 `no such column: id`。

两条都修好之后仍然会**看起来像修好了而其实没有**：检索器有一层「通道抛异常就退化
成本地实现」的探测（那层是对的，PG 装不上时靠它优雅降级），退化之后召回照常、
分数照常、日志只有一行。所以本文件第一条断言不看结果、只看**后端有没有被问过**。

## 什么必须一致，什么本来就可以不一致

必须一致的只有 F-2 附则那两条：**分数越大越相关**、**次序确定**。跨后端的查询语义
本来就归后端所有 —— 端口把词间做 AND 且只查 `field` 那一列，本地实现是跨列 OR，
两者召回集不同不是 bug。第 5 节把这条分歧**钉成断言**：不写下来，下一个人只会
看到一条莫名其妙的召回差异，然后去改错地方。
"""

from __future__ import annotations

import collections
import logging

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import retriever
from maos.store.sqlite_store import SqliteStorePort

TENANT_A = "tnt-a"
TENANT_B = "tnt-b"

#: 对齐语料：查询词 `timeout` **只出现在 body**，且是单个英数 token。
#:
#: 这两条限制不是凑巧，是为了让「端口 vs 本地」的比较只暴露分数方向与排序问题：
#:   · 单 token —— 端口的词间 AND 与本地的 OR 在只有一个词时同义；
#:   · 只在 body —— 端口只查 `field` 那一列，本地跨列查，词进了标题两边就必然分叉。
#: 换语料前先读一遍这两条，否则第 3 节会红在一个与它要守的东西无关的地方。
ALIGNED_CORPUS = [
    ("doc-timeout-a", "支付回执缺失", "gateway timeout timeout retry twice", "AS-101"),
    ("doc-timeout-b", "回执延迟", "gateway timeout then settled", "AS-102"),
    ("doc-quiet", "包装破损", "carton crushed on arrival", "AS-103"),
]

QUERY = {"tenant_id": TENANT_A, "biz_type": "refund", "keyword": "timeout"}

#: 只留全文通道，让第 3 节的比较不被另外三个通道的分数摊平。
ONLY_FTS = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 1.0, "vector": 0.0}
ONLY_VECTOR = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 0.0, "vector": 1.0}


def _seed(target, corpus=ALIGNED_CORPUS, tenant=TENANT_A):
    """把语料灌进 target。target 可以是核心 Store，也可以是 StorePort —— 这正是本轨的点。"""
    kb.ensure_schema(target)
    for doc_id, title, body, rule_no in corpus:
        kb.upsert_doc(target, {
            "tenant_id": tenant, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "biz_type": "refund", "rule_no": rule_no, "title": title, "body": body,
            "embedding": retriever.embed(f"{title} {body}")})
    return target


def _core():
    store = SqliteStore()
    store.init_schema()
    return store


@pytest.fixture
def local():
    """核心 `SqliteStore` —— 没有那两个方法，检索必然走本地实现。"""
    return _seed(_core())


@pytest.fixture
def ported():
    """真 `SqliteStorePort`。建表与灌库都走端口，顺带验证 `ensure_schema` 的端口路径。"""
    return _seed(SqliteStorePort(_core()))


class _CountingPort(SqliteStorePort):
    """真端口 + 一个计数器。行为一字未改，只记「后端被问过几次」。"""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.calls: collections.Counter = collections.Counter()

    def fts_search(self, table, field, q, limit):
        self.calls["fts_search"] += 1
        return super().fts_search(table, field, q, limit)

    def vector_search(self, table, field, vec, limit):
        self.calls["vector_search"] += 1
        return super().vector_search(table, field, vec, limit)


# ---------------------------------------------------------------------------
# 1. 通道真被调用 —— 本轨的核心断言，没有它本轨等于没做
# ---------------------------------------------------------------------------
def test_retrieve_really_calls_both_store_port_channels():
    """一次 `retrieve` 必须把两条通道各问一次后端。

    **只断言结果是不够的**：退化路径下召回一模一样。所以这里数的是调用次数。
    **只数调用次数也不够**：抛异常的通道同样被调用过一次，然后退化。所以调用数与
    探测结论两条一起断言，缺一条都能被「问了后端、后端报错、悄悄走本地」蒙混过去。
    也顺带钉住「一次检索问一次」——按候选逐条去问后端是另一种没有症状的退化，
    小库上只是慢一点，PolarDB 上是每条候选一个来回。
    """
    port = _seed(_CountingPort(_core()))

    hits = retriever.retrieve(port, QUERY, limit=10)

    assert hits, "端口通道下一条都没召回 —— 后端被问了，但问出来的东西对不上"
    assert port.calls["fts_search"] >= 1, "fts_search 一次都没被调用 —— RAG 没跑在端口上"
    assert port.calls["vector_search"] >= 1, "vector_search 一次都没被调用"
    assert retriever.port_channel_state(port) == {"fts_search": True,
                                                  "vector_search": True}, \
        "调用到了但没走通 —— 结果是本地实现给的，端口只是被问了一句然后退化了"
    assert port.calls["fts_search"] == 1 and port.calls["vector_search"] == 1, \
        f"一次检索每条通道只该问一次后端，实际 {dict(port.calls)}"


def test_port_channel_state_is_true_on_the_real_sqlite_store_port(ported):
    """`port_channel_state` 在真 `SqliteStorePort` 上两条通道均为 True。

    这条等价于「BACKLOG `## task-X3` 第 1 条的主键分叉已消除」：只要 `kb_doc.id`
    或影子表那列被拿掉，探测会抛 `no such column: id`，本条立刻变成 False。
    """
    assert retriever.retrieve(ported, QUERY, limit=10)
    assert retriever.port_channel_state(ported) == {"fts_search": True,
                                                    "vector_search": True}


def test_kb_layer_accepts_a_store_port_end_to_end(ported):
    """知识层的读写口在 StorePort 上整条通 —— 建表、写入、回查、影子表同步。

    以前这些全部撞 `kb._conn()` 的 TypeError（`SqliteStorePort` 故意不叫 `_conn`），
    「换个后端就能跑」于是只是一句话。判据集中在 `kb.port_of()` 一个函数里：
    散在各调用点的后果是某几处走了新路、某几处还走老路，而两边都不报错。
    """
    assert kb.port_of(ported) is ported
    assert kb.has_kb_table(ported)
    assert {d["doc_id"] for d in kb.list_docs(ported, tenant_id=TENANT_A)} == \
        {doc_id for doc_id, *_ in ALIGNED_CORPUS}

    got = kb.get_doc(ported, TENANT_A, "doc-timeout-a")
    assert got is not None and got["title"] == "支付回执缺失"
    # 生成列：写入侧碰都碰不到它，永远等于主键本身。
    assert got["id"] == kb.doc_row_id(TENANT_A, "doc-timeout-a")

    shadow = kb.query(ported, "SELECT id, doc_id FROM kb_doc_fts ORDER BY doc_id", ())
    assert [r["id"] for r in shadow] == [
        kb.doc_row_id(TENANT_A, doc_id) for doc_id, *_ in sorted(ALIGNED_CORPUS)], \
        "影子表的 id 列没跟着写 —— 全文通道会恒空，而日志一片正常"


def test_core_store_still_takes_the_old_path(local):
    """缺省链路一个字节不许变：核心 `SqliteStore` 仍走 `_conn`，探测仍恒不成立。"""
    assert kb.port_of(local) is None
    assert not hasattr(local, "fts_search")
    assert retriever.retrieve(local, QUERY, limit=10)
    assert retriever.port_channel_state(local) == {}


# ---------------------------------------------------------------------------
# 2. 口径一致 —— F-2 附则：分数越大越相关、次序确定
# ---------------------------------------------------------------------------
def test_fts_channel_matches_the_local_implementation_doc_for_doc(local, ported):
    """全文通道：端口与本地的**命中集合与次序逐条一致**。

    对不上就是 F-2 附则被破坏了 —— 最常见的是分数方向记反（bm25 原值越负越相关，
    符号只在适配器里翻一次），那会让最相关的一条稳定排在最后，而检索照常返回结果。
    """
    local_hits = retriever.retrieve(local, QUERY, limit=10, weights=ONLY_FTS)
    port_hits = retriever.retrieve(ported, QUERY, limit=10, weights=ONLY_FTS)

    assert retriever.port_channel_state(ported)["fts_search"] is True, \
        "端口那边退化成本地实现了 —— 这条测试就白比了"
    assert [h["doc_id"] for h in port_hits] == [h["doc_id"] for h in local_hits] \
        == ["doc-timeout-a", "doc-timeout-b"], \
        f"端口 {[h['doc_id'] for h in port_hits]} vs 本地 {[h['doc_id'] for h in local_hits]}"
    assert [h["channels"]["fts"] for h in port_hits] == \
        [h["channels"]["fts"] for h in local_hits], "同一批命中归一出了不同的分"


def test_vector_channel_matches_the_local_implementation_doc_for_doc(local, ported):
    """语义通道：同上。端口算的余弦与本模块纯 Python 那份必须给出同一个次序。"""
    local_hits = retriever.retrieve(local, QUERY, limit=10, weights=ONLY_VECTOR)
    port_hits = retriever.retrieve(ported, QUERY, limit=10, weights=ONLY_VECTOR)

    assert retriever.port_channel_state(ported)["vector_search"] is True
    assert [h["doc_id"] for h in port_hits] == [h["doc_id"] for h in local_hits]
    assert all(h["channels"]["vector"] > 0 for h in port_hits), \
        "端口回来的分数被截成 0 了 —— 多半是方向反了（余弦越大越相关）"


def test_full_pipeline_is_identical_through_the_port(local, ported):
    """四通道融合之后整条链路的输出仍逐条一致，含分数。"""
    query = dict(QUERY, rule_no="AS-101")
    local_hits = retriever.retrieve(local, query, limit=10)
    port_hits = retriever.retrieve(ported, query, limit=10)

    assert [(h["doc_id"], h["score"]) for h in port_hits] == \
        [(h["doc_id"], h["score"]) for h in local_hits]
    assert port_hits[0]["doc_id"] == "doc-timeout-a", "精确通道被融合摊平了"


def test_port_channels_never_leak_across_tenants(ported):
    """F-2 的两条通道都没有租户参数，端口是**全表**查的。

    跨租户不召回只能由阶段一的候选集兜住（`_to_doc_scores` 里那一句）。这条守的
    正是那一层 —— 而且 `kb_doc.id` 带着租户前缀，别家租户的行连回查表都进不去。
    """
    _seed(ported, corpus=[("doc-timeout-a", "别家的同名文档", "gateway timeout leak", "AS-101")],
          tenant=TENANT_B)

    raw = ported.fts_search("kb_doc", "body", kb.fts_text("timeout"), 10)
    assert kb.doc_row_id(TENANT_B, "doc-timeout-a") in {row_id for row_id, _ in raw}, \
        "端口本来就该查得到它（全表）—— 查不到的话这条测试守不住任何东西"

    hits = retriever.retrieve(ported, QUERY, limit=10)
    assert [h["doc_id"] for h in hits] == ["doc-timeout-a", "doc-timeout-b"]
    assert all(h["doc"]["tenant_id"] == TENANT_A for h in hits), \
        "别家租户的同名 doc_id 被端口带进来了 —— 这是事故，不是效果问题"


# ---------------------------------------------------------------------------
# 3. 退化路径仍在 —— 这层是设计意图，本轨改的是「能不能通过」，不是「有没有退路」
# ---------------------------------------------------------------------------
class _BrokenFtsPort(SqliteStorePort):
    """全文通道走不通、向量通道正常的端口 —— PG 装了驱动但没建索引就是这个形态。"""

    def fts_search(self, table, field, q, limit):
        raise LookupError("后端没准备好：全文索引还没建")


def test_broken_channel_degrades_to_local_and_warns_exactly_once(caplog, local):
    """通道抛异常 -> 退化到本地实现、检索不抛、且**只告警一次**。

    每次检索都抛一遍再吞掉的写法，症状是日志被刷满而没人看得出这条通道一直没通。
    另一条通道不受牵连：退化是按通道记的，不是按 store 记的。
    """
    broken = _seed(_BrokenFtsPort(_core()))

    with caplog.at_level(logging.WARNING, logger="maos.kb"):
        first = retriever.retrieve(broken, QUERY, limit=10)
        second = retriever.retrieve(broken, QUERY, limit=10)

    assert first == second, "退化之后两次结果不一致"
    assert [h["doc_id"] for h in first] == \
        [h["doc_id"] for h in retriever.retrieve(local, QUERY, limit=10)], \
        "退化之后的结果与本地实现不一致 —— 降级把召回改小了"
    assert retriever.port_channel_state(broken) == {"fts_search": False,
                                                    "vector_search": True}, \
        "一条通道走不通就把另一条也判死了"

    warned = [r for r in caplog.records if "StorePort.fts_search" in r.getMessage()]
    assert len(warned) == 1, f"该只告警一次，实际 {len(warned)} 条"


# ---------------------------------------------------------------------------
# 4. 分词对齐 —— 原因丙：端口不知道影子表存的是切过的文本
# ---------------------------------------------------------------------------
def test_port_query_text_goes_through_the_same_tokenizer(ported):
    """发给端口的查询串必须先过 `kb.fts_text()`，否则中文一条都命不中且不报错。

    影子表用缺省的 unicode61，整串汉字是一个 token；写入侧靠 `kb.fts_text()` 切成
    单字，查询侧不走同一个函数，索引里存的是「退 款」而查询发的是「退款」——
    对不上，而日志一片正常。这条把两种发法的差别直接摆出来。
    """
    _seed(ported, corpus=[("zh-refund", "退款政策", "退款政策超时未到账", "AS-201")])

    assert ported.fts_search("kb_doc", "body", "退款政策", 5) == [], \
        "原样发过去居然命中了 —— 影子表的分词口径变了，本条的前提要重写"
    assert ported.fts_search("kb_doc", "body", kb.fts_text("退款政策"), 5), \
        "切过词也命不中 —— 写入侧与查询侧的分词函数漂了"

    hits = retriever.retrieve(ported, {"tenant_id": TENANT_A, "keyword": "退款政策"},
                              limit=10)
    assert [h["doc_id"] for h in hits] == ["zh-refund"]
    assert hits[0]["channels"]["fts"] > 0, "检索器没走切过词的那条发法"


# ---------------------------------------------------------------------------
# 5. 端口与本地的查询语义**本来就不同** —— 钉成断言，别让下一个人去改错地方
# ---------------------------------------------------------------------------
def test_port_and_local_fts_semantics_differ_by_design(local, ported):
    """端口：词间 AND、只查 `field` 一列。本地：跨列 OR。两者召回集不同**不是 bug**。

    F-2 把查询语义下放给后端（`sqlite_store.py` 原话：「要算子就另开方法，别动
    F-2 那五个签名」），PG 的 tsquery 也会有它自己的一套。所以跨后端能要求的只有
    附则那两条：分数越大越相关、次序确定 —— 那两条由第 2 节守。

    这条钉的是现状，不是理想态。真要让两边召回一致，得给 F-2 加算子或加列表参数，
    那是三轨一起改的事（账记在 BACKLOG `## task-T13`），不许在这一层偷偷抹平。
    """
    zh = [("zh-both", "标题甲", "锈蚀 与 退款 都 在 正文", "AS-301"),
          ("zh-one", "标题乙", "只 有 退款 两 个 字", "AS-302")]
    _seed(local, corpus=zh)
    _seed(ported, corpus=zh)
    query = {"tenant_id": TENANT_A, "keyword": "锈蚀退款"}

    local_ids = {h["doc_id"] for h in retriever.retrieve(local, query, limit=10,
                                                         weights=ONLY_FTS)}
    port_ids = {h["doc_id"] for h in retriever.retrieve(ported, query, limit=10,
                                                        weights=ONLY_FTS)}

    assert retriever.port_channel_state(ported)["fts_search"] is True
    assert local_ids == {"zh-both", "zh-one"}, "本地是 OR：命中任一字就算召回"
    assert port_ids == {"zh-both"}, "端口是 AND：四个字都得在 body 里"
    assert port_ids < local_ids, "端口的召回集应当是本地的真子集（AND 比 OR 严）"
