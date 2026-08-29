"""T16 的机器验收 —— 知识层**整条链路**走端口：检索走 StorePort、落事件走核心 Store。

## 这一束在守什么

T13 把「检索」那半条接上了端口，但主链路仍然换不成端口对象：`retrieve_and_log`
落 `KbRetrieved` 时直接调 `store.append_event_log`，而 F-2 的五个方法里没有它
（事件日志是核心 Store 的冻结表，不是端口的职责）。后果有两层，第二层才是要命的：

* 把 `ctx.store` 换成 `SqliteStorePort` 会撞 `AttributeError`；
* 而且撞在检索**之后** —— 检索那半条看起来是通的，所以「跑一下试试」发现不了，
  只有落事件那一步炸，被上层兜底压成 `docs: []`。

T16 的解法是把两件事拆开：`emit_kb_retrieved` / `retrieve_and_log` 各收一个独立的
`event_sink`，缺省回落 `store`（老调用方一个字节不用改）。**没有**给 StorePort 加
第六个方法 —— 那是动 F-2 冻结面。

第二件事是跨列召回。端口的 `fts_search(table, field, q, limit)` 一次只认一列，
检索器却只发了 `body` 那一次，于是**标题命中的知识召不回来**，症状是「换了后端
之后 RAG 好像笨了一点」，两边都不报错。改成 `PORT_FTS_FIELDS` 每列各发一次再合并。

## 与 test_kb_pg_channel.py 的分工

那一束守「端口通道**被调用到了**」（RAG 真跑在 PG 上的唯一代码判据）与两条通道的
口径一致；本束守「链路整条通」与「合并口径」。两束都要，删任何一束另一束都盖不住。
"""

from __future__ import annotations

import json
import logging

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import retriever
from maos.skills.builtin.kb_retrieve import KbRetrieveSkill, _event_sink
from maos.skills.contract import SkillContext
from maos.store.sqlite_store import SqliteStorePort

TENANT_A = "tnt-a"

#: 只留全文通道，让跨列那几条断言不被另外三个通道的分数摊平。
ONLY_FTS = {"rule_no": 0.0, "gateway_code": 0.0, "fts": 1.0, "vector": 0.0}

#: 查询词 `timeout` **只出现在 body**，与 test_kb_pg_channel 的对齐语料同一套用意：
#: 端口与本地在这份语料上必须逐字段一致，跨列改造不该动它。
BODY_ONLY_CORPUS = [
    ("doc-timeout-a", "支付回执缺失", "gateway timeout timeout retry twice", "AS-101"),
    ("doc-timeout-b", "回执延迟", "gateway timeout then settled", "AS-102"),
    ("doc-quiet", "包装破损", "carton crushed on arrival", "AS-103"),
]

#: 跨列语料：`only-title` 只在标题命中，`only-body` 只在正文命中，各自独占一列。
#: 两条各自是自己那一列的第一名 —— 合并口径对不对，全看它们最后是不是同一个分。
ONE_COLUMN_EACH_CORPUS = [
    ("only-title", "timeout notice", "nothing relevant here", "AS-201"),
    ("only-body", "quiet title", "a timeout happened inside", "AS-202"),
]

QUERY = {"tenant_id": TENANT_A, "biz_type": "refund", "keyword": "timeout"}


def _core() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


def _seed(target, corpus=BODY_ONLY_CORPUS, tenant=TENANT_A):
    kb.ensure_schema(target)
    for doc_id, title, body, rule_no in corpus:
        kb.upsert_doc(target, {
            "tenant_id": tenant, "doc_id": doc_id, "kind": kb.KIND_POLICY,
            "biz_type": "refund", "rule_no": rule_no, "title": title, "body": body,
            "embedding": retriever.embed(f"{title} {body}")})
    return target


def _kb_events(store) -> list[dict]:
    return kb.query(store, "SELECT * FROM event_log WHERE event_type=?", ("KbRetrieved",))


@pytest.fixture
def linked():
    """真链路的形态：**同一个库**，检索走端口、落事件走它包着的那个核心 Store。

    刻意不给两个库：端口与 sink 指向同一份数据才是换后端时的真实样子，
    分成两个库会让「事件落在哪儿」这条断言变得不值钱。
    """
    core = _core()
    port = SqliteStorePort(core)
    _seed(port)
    return core, port


# ---------------------------------------------------------------------------
# 1. 整条链路走端口 —— 本轨的核心断言（§5.3 第 1 条）
# ---------------------------------------------------------------------------
def test_retrieve_and_log_runs_end_to_end_through_the_port(linked):
    """检索吃 `SqliteStorePort`、落事件吃核心 Store，**不再撞 AttributeError**。

    两条都要断言：不抛只说明没炸，事件真落进 event_log 才说明后半条也接上了。
    只断言「不抛」的话，把 `append_event_log` 那一段整个删掉本条照样绿。
    """
    core, port = linked

    hits = retriever.retrieve_and_log(
        port, QUERY, limit=10, plan_id="plan-x", task_id="task-x",
        trace_id="trace-x", event_sink=core)

    assert [h["doc_id"] for h in hits] == ["doc-timeout-a", "doc-timeout-b"], \
        "检索这半条断了 —— 端口没把 kb_doc 查出来"
    assert retriever.port_channel_state(port) == {"fts_search": True,
                                                  "vector_search": True}, \
        "退化成本地实现了 —— 那这条测的就不是端口链路"

    rows = _kb_events(core)
    assert len(rows) == 1, f"KbRetrieved 没落下去或落了多条：{len(rows)}"
    assert rows[0]["plan_id"] == "plan-x" and rows[0]["task_id"] == "task-x"
    detail = json.loads(rows[0]["detail"])
    assert {d["doc_id"] for d in detail["docs"]} == {h["doc_id"] for h in hits}
    for doc in detail["docs"]:
        assert kb.get_doc(port, TENANT_A, doc["doc_id"]) is not None, \
            "doc_id 在 kb_doc 里查不到 —— 判据是「RAG 命中是编的」"


def test_missing_event_sink_fails_loudly_and_names_the_fix(linked):
    """端口当 store、又不给 sink 时抛 `TypeError` 并把解法写进报错，不静默跳过。

    静默跳过是这里最坏的选项：「命中为空也落」的前提是事件必落，悄悄不落会让
    「这次到底检没检」无从追溯，而 RAG 有无对照实验（R5）正是拿 event_log 判的 ——
    那种失效没有症状，只有结论变形。报错里点名 `event_sink`，是因为撞它的人
    一定正在把 `store` 换成端口，而错误发生在检索**之后**，现场看着像检索通了。
    """
    _core_store, port = linked

    with pytest.raises(TypeError) as excinfo:
        retriever.retrieve_and_log(port, QUERY, limit=10)

    message = str(excinfo.value)
    assert "append_event_log" in message and "event_sink" in message, \
        f"报错没告诉人怎么修：{message}"


def test_event_sink_defaults_to_the_store_so_old_callers_are_untouched():
    """不传 `event_sink` 时落到 `store` 上 —— 今天所有调用方的行为一个字节没变。"""
    core = _seed(_core())

    retriever.retrieve_and_log(core, QUERY, limit=10, plan_id="plan-y")

    rows = _kb_events(core)
    assert len(rows) == 1 and rows[0]["plan_id"] == "plan-y"


# ---------------------------------------------------------------------------
# 2. 事件内容不变 —— 「换后端不改变可观测行为」的判据（§5.3 第 2 条）
# ---------------------------------------------------------------------------
def test_event_detail_is_identical_whichever_store_retrieved(linked):
    """端口检索落的 detail，与全走核心 Store 时落的**逐字段一致**。

    只有 `duration_ms` 例外（它记的是这一跑花了多久，两跑本来就不同），
    所以比较前把它摘掉、单独断言它还在且是个正数 —— 少了它 trace 上的
    KbRetrieved span 会退化成零长。

    这条比「命中集合一致」严：命中一样但 `weights` / `candidate_count` / `query`
    任一项漂了，事后复盘「这次排序是按哪套权重算的」就答不上来，而检索照常返回结果。
    """
    core, port = linked
    plain = _seed(_core())

    ported_hits = retriever.retrieve_and_log(port, QUERY, limit=10, plan_id="p",
                                             trace_id="t", event_sink=core)
    plain_hits = retriever.retrieve_and_log(plain, QUERY, limit=10, plan_id="p",
                                            trace_id="t")

    assert [h["doc_id"] for h in ported_hits] == [h["doc_id"] for h in plain_hits]

    ported_detail = json.loads(_kb_events(core)[0]["detail"])
    plain_detail = json.loads(_kb_events(plain)[0]["detail"])

    for detail in (ported_detail, plain_detail):
        assert detail.pop("duration_ms") >= 0.0, "duration_ms 没了 —— span 会退化成零长"
    assert ported_detail == plain_detail, (
        "换了后端事件内容就变了 —— 可观测行为不该跟着存储后端走\n"
        f"端口：{ported_detail}\n核心：{plain_detail}")


# ---------------------------------------------------------------------------
# 3. 跨列召回 —— 端口那条路不再只问 body（§5.3 第 3 条）
# ---------------------------------------------------------------------------
def test_title_only_hit_is_recalled_through_the_port():
    """只在**标题**命中的知识，走端口也召得回来。

    改造前端口只问 `body` 那一列，这条召回集是 1 条（另一条只在标题里）；
    改造后是 2 条。两边都不报错，所以这条差异只能靠断言守，不能靠观察。
    """
    local = _seed(_core(), corpus=ONE_COLUMN_EACH_CORPUS)
    ported = _seed(SqliteStorePort(_core()), corpus=ONE_COLUMN_EACH_CORPUS)

    local_ids = {h["doc_id"] for h in retriever.retrieve(local, QUERY, limit=10,
                                                         weights=ONLY_FTS)}
    port_ids = {h["doc_id"] for h in retriever.retrieve(ported, QUERY, limit=10,
                                                        weights=ONLY_FTS)}

    assert retriever.port_channel_state(ported)["fts_search"] is True, \
        "退化成本地实现了 —— 这条就不是在测端口"
    assert local_ids == {"only-title", "only-body"}, "本地跨列查，两条都该召回"
    assert port_ids == local_ids, \
        f"端口召回集与本地不一致：{port_ids} —— 标题那一列多半又没问"


def test_both_columns_are_asked_and_the_count_is_a_constant():
    """全文通道对每列各问一次后端，**次数是常数**，不随候选集规模涨。

    往返数是这条改造要付的代价（PolarDB 上是真的网络往返），所以把它钉死：
    列数写在 `PORT_FTS_FIELDS` 里，多一列就多一个来回，改列表要连这条一起想清楚。
    真正要拦的是「按候选逐条去问后端」——那在小库上只是慢一点，PolarDB 上是每条
    候选一个来回，而且没有任何症状。所以断言的是「等于列数」而不是「小于某个数」。
    """
    class _CountingPort(SqliteStorePort):
        def __init__(self, store):
            super().__init__(store)
            self.fields: list[str] = []

        def fts_search(self, table, field, q, limit):
            self.fields.append(field)
            return super().fts_search(table, field, q, limit)

    port = _seed(_CountingPort(_core()), corpus=ONE_COLUMN_EACH_CORPUS)

    retriever.retrieve(port, QUERY, limit=10)

    assert port.fields == list(retriever.PORT_FTS_FIELDS), \
        f"问的列与 PORT_FTS_FIELDS 对不上：{port.fields}"
    assert len(port.fields) == len(retriever.PORT_FTS_FIELDS), \
        f"往返数不是常数，实际问了 {len(port.fields)} 次"


def test_each_column_is_normalized_before_the_merge():
    """两列的分数**先各自按名次归一再取 max**，不拿两列的 bm25 直接比大小。

    `only-title` 与 `only-body` 各是自己那一列的唯一命中、也就是第一名，所以合并后
    必须是**同一个分**。拿原始 bm25 混着排的话，title 那列短、IDF 与列长都不同量纲，
    两条会分出高下 —— 症状是「标题命中的知识总排在正文命中的前面（或后面）」，
    而排序本身看起来完全正常。
    """
    ported = _seed(SqliteStorePort(_core()), corpus=ONE_COLUMN_EACH_CORPUS)

    hits = retriever.retrieve(ported, QUERY, limit=10, weights=ONLY_FTS)

    scores = {h["doc_id"]: h["channels"]["fts"] for h in hits}
    assert scores == {"only-title": 1.0, "only-body": 1.0}, \
        f"两列各自的第一名没拿到同一个分：{scores}"
    assert len({h["score"] for h in hits}) == 1, "融合之后又分出高下了"


def test_one_broken_column_degrades_the_whole_channel_not_half_of_it(caplog):
    """一列走不通就整条通道退化本地，**不拿半份结果凑**，且仍然只告警一次。

    只有 body 那列通的话，召回集会在两次检索之间飘：这一次半份（探测结论还是 True），
    下一次退化成本地全份。「连跑两次输出一致」是本仓库的硬判据，飘了就等于作废，
    而两次都返回了结果、都不报错 —— 没有人会去查。
    """
    class _TitleBrokenPort(SqliteStorePort):
        """title 列走不通、body 正常 —— PG 上只给 body 建了索引就是这个形态。"""

        def fts_search(self, table, field, q, limit):
            if field == "title":
                raise LookupError("后端没准备好：title 列还没建索引")
            return super().fts_search(table, field, q, limit)

    broken = _seed(_TitleBrokenPort(_core()), corpus=ONE_COLUMN_EACH_CORPUS)
    local = _seed(_core(), corpus=ONE_COLUMN_EACH_CORPUS)

    with caplog.at_level(logging.WARNING, logger="maos.kb"):
        first = retriever.retrieve(broken, QUERY, limit=10, weights=ONLY_FTS)
        second = retriever.retrieve(broken, QUERY, limit=10, weights=ONLY_FTS)

    assert [h["doc_id"] for h in first] == [h["doc_id"] for h in second], \
        "两次检索的召回集飘了 —— 多半是拿了半份端口结果"
    assert {h["doc_id"] for h in first} == {
        h["doc_id"] for h in retriever.retrieve(local, QUERY, limit=10, weights=ONLY_FTS)}, \
        "退化之后与本地实现不一致 —— 降级把召回改小了"
    assert retriever.port_channel_state(broken)["fts_search"] is False
    warned = [r for r in caplog.records if "StorePort.fts_search" in r.getMessage()]
    assert len(warned) == 1, f"该只告警一次，实际 {len(warned)} 条"


# ---------------------------------------------------------------------------
# 4. skill 层把 sink 接上了 —— 漏接的症状是「检索看起来在跑，docs 恒空」
# ---------------------------------------------------------------------------
def test_skill_takes_the_event_sink_from_extras_and_falls_back_to_the_store():
    """`_event_sink` 是「事件落哪儿」的唯一判据：extras 优先，缺省 `ctx.store`。

    判据集中在一个函数里而不是散在调用点，理由与 `kb.port_of()` 那条一样 ——
    散开的后果是某几处走了新路、某几处还走老路，而两边都不报错。
    """
    core = _core()
    port = SqliteStorePort(core)

    assert _event_sink(SkillContext(store=port, extras={"event_sink": core})) is core
    assert _event_sink(SkillContext(store=core)) is core, "缺省行为变了"
    assert _event_sink(SkillContext(store=core, extras={"plan_id": "p"})) is core, \
        "extras 里没有 event_sink 时该回落 ctx.store"


def test_skill_still_logs_exactly_one_event_on_the_core_store():
    """skill 走缺省链路（`ctx.store` 是核心 Store）的行为一个字节没变。"""
    core = _seed(_core())

    out = KbRetrieveSkill().run(
        {"tenant_id": TENANT_A, "keyword": "timeout", "limit": 10},
        SkillContext(store=core, extras={"plan_id": "plan-z"}))

    assert [d["doc_id"] for d in out["docs"]] == ["doc-timeout-a", "doc-timeout-b"]
    rows = _kb_events(core)
    assert len(rows) == 1 and rows[0]["plan_id"] == "plan-z"
