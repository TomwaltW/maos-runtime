"""W-1 语料（`scenarios/refund/`）的机器守卫 —— 它从此有消费方，分叉就红。

W-1 自己的账（BACKLOG `## task-W1` 第 3 条）写着那几份数据文件**零消费方**：
「造出来了但没有任何东西会因为它变红或变绿，一旦字段与 `schema.sql` / `kb_doc`
列清单分叉，不会有任何报错」。本文件就是那条账的守卫，守三层：

1. **列清单**。历史案例逐条对齐 `kb.DOC_COLUMNS`，政策语料逐条对齐
   `experiment.CORPUS_TABLES` 登记的四张表 —— 多一列、少一列、改个列名都当场红。
   不靠 `INSERT ... VALUES` 的位置对齐：位置错一列不报错，值会悄悄挪一格。
2. **取值域**。kind / outcome 必须落在 `maos.kb` 的常量里。写错的条目查得出来
   但归不了类，而错误发生在写入侧、暴露在几周后的检索侧。
3. **检索漏斗**。全量装库之后钉死「库存 -> 同租户 -> 七维预过滤后」三级数字。
   只钉候选集大小看不出预过滤砍掉了什么；只钉库存看不出跨租户那一半从来没进过
   候选集。三个数一起，融合排序才有话可说 —— 这正是 R5 要的那个「有话可说」。

**为什么历史案例不进 R5 的证据库**：核验器第 7 项要求库里每条 `history_case` 的
`source_case_id` 都能回查到一条 `biz_status='settled'` 的**本库** `refund_case`，
而外部导入的历史知识按定义没有这样一条记录；给它们凭空造 `refund_case` 行就是
伪造证据（铁律 3）。所以 R5 的库里只装政策那一半，24 条历史案例的检索质量在这里
验，账记在 BACKLOG `## task-X3`。
"""

from __future__ import annotations

import json
import os

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import experiment, retriever

TENANT_A = "tnt-mfg-a"
TENANT_B = "tnt-mfg-b"

#: 语料现状的四个规模数。它们不是「跑出来多少就写多少」——
#: 24/8 是 W-1 的 `_note` 自己声明的（历史案例 24 条，其中 8 条 failed，正好 1/3），
#: 16 是「4 条规则 x 2 租户 x 2 版本」，93 是 T118 生成器按类别算出来的
#: （`python3 scripts/gen_refund_kb.py` 末尾会打印这张分类表）。
#: 数字对不上说明语料被动过，该有人知道。
HISTORY_DOCS = 24
HISTORY_FAILED = 8
POLICY_RULES = 16
PROCESS_DOCS = 93

#: T118 起语料覆盖评委点名的九类流程知识，落成 `kb_doc.kind` 的八个取值
#: （「产品与渠道差异」走 `policy` 的渠道 / 品类变体，不另立一类）。
#: 每类的下限钉在这里：低于它就说明某一类知识事实上不存在，而检索照样返回结果，
#: 看不出少了什么。
KIND_FLOOR = 5

#: 装完整份语料之后的检索漏斗。三级都钉死：
#: 133 = 24 条历史案例 + 16 条政策 + 93 条流程知识；
#: 49 = 租户 A 那一份（跨租户的 84 条连候选集都进不了）；
#: 27 = 再按渠道 / 区域 / SKU / 政策版本收窄之后剩下的。
#:
#: T118 之前这三个数是 40 / 21 / 7。变大的是语料，**不是**过滤放松了：
#: 27 这一级里 11 条是错误码处置手册（与渠道 / 商品无关，按既有语义留 NULL 通配），
#: 政策则从 4 条降到 3 条 —— AS-004 补上 `channel_id='ch-dealer'` 之后，
#: 自营渠道的查询不再命中经销专属规则。
FUNNEL_TOTAL = HISTORY_DOCS + POLICY_RULES + PROCESS_DOCS
FUNNEL_SAME_TENANT = 49
FUNNEL_AFTER_PREFILTER = 27

#: 租户 B 那一份。单列而不是拿总数减租户 A：T118 起语料里有三个租户
#: （多了 60 单批量底账的 `tnt-demo`），减法算出来的是「非 A 的全部」。
TENANT_B_DOCS = 47

#: R5 用的那份检索上下文，逐字取自 `experiment` 的常量 —— 两处写死会各自漂。
R5_CONTEXT = {
    "tenant_id": experiment.TENANT_ID,
    "biz_type": experiment.BIZ_TYPE,
    "channel_id": experiment.CHANNEL_ID,
    "region": experiment.REGION,
    "sku": experiment.SKU,
    "policy_version": experiment.POLICY_VERSION,
}


@pytest.fixture
def store():
    s = SqliteStore()
    s.init_schema()
    kb.ensure_schema(s)
    return s


def _history_rows() -> list[dict]:
    """取历史案例，顺带过一遍列清单守卫 —— 读语料这一步就是校验这一步。"""
    payload = experiment.load_corpus(os.path.join("history", "history_cases.json"))
    return experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)


def _load_history(target) -> int:
    """把 24 条历史案例装进 kb_doc。向量语料里恒为 null，落库时现算一份。"""
    rows = _history_rows()
    for row in rows:
        kb.upsert_doc(target, {
            **row,
            "embedding": retriever.embed(f"{row['title']} {row['body']}"),
        })
    return len(rows)


def _process_rows() -> list[dict]:
    """取 T118 的流程知识语料，顺带过一遍列清单守卫。文件清单从消费方取，不另抄一份。"""
    rows: list[dict] = []
    for name in experiment.PROCESS_CORPUS_FILES:
        payload = experiment.load_corpus(os.path.join("kb", name))
        rows.extend(experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS))
    return rows


def _load_all(target) -> None:
    """整份语料：历史案例 + 政策投影 + 九类流程知识。

    后两段都复用 R5 自己那条装载路径（`_seed_kb_from_corpus` / `seed_process_kb`），
    不在测试里另写一套投影 —— 两套迟早在字段口径上分叉，而症状只是「候选集少了些」。
    """
    _load_history(target)
    experiment._seed_kb_from_corpus(target)
    experiment.seed_process_kb(target)


# ---------------------------------------------------------------------------
# 1. 列清单 —— 语料与消费方的列清单分叉，从前不会有任何报错
# ---------------------------------------------------------------------------
def test_history_corpus_columns_match_kb_doc_exactly():
    """历史案例逐条对齐 `kb.DOC_COLUMNS`。多一列少一列都红。"""
    rows = _history_rows()
    assert len(rows) == HISTORY_DOCS
    for idx, row in enumerate(rows):
        assert set(row) == set(kb.DOC_COLUMNS), f"kb_doc[{idx}] 的列清单漂了"


def test_history_corpus_column_guard_actually_fires():
    """守卫本身得会响 —— 只写断言不验断言，等于把守卫写在注释里。"""
    payload = {"kb_doc": [{c: None for c in kb.DOC_COLUMNS if c != "outcome"}]}
    with pytest.raises(ValueError, match="outcome"):
        experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)

    payload = {"kb_doc": [{c: None for c in kb.DOC_COLUMNS} | {"多出来的列": 1}]}
    with pytest.raises(ValueError, match="多出来的列"):
        experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS)


def test_policy_corpus_columns_match_the_consumer_list():
    """政策语料四张表逐条对齐 `experiment.CORPUS_TABLES` 登记的列清单。"""
    payload = experiment.load_corpus(os.path.join("policy", "policy_rules.json"))
    counted = {table: len(experiment._checked_rows(payload, table, columns))
               for table, columns in experiment.CORPUS_TABLES}
    assert counted["policy_rule"] == POLICY_RULES
    assert counted["tenant"] == 2 and counted["channel"] == 4
    assert counted["product_snapshot"] == 4


# ---------------------------------------------------------------------------
# 2. 取值域 —— 写错的条目查得出来但归不了类
# ---------------------------------------------------------------------------
def test_history_corpus_values_stay_in_range():
    rows = _history_rows()
    assert {r["kind"] for r in rows} == {kb.KIND_HISTORY_CASE, kb.KIND_FAILURE_HINT}
    assert {r["outcome"] for r in rows} <= set(kb.VALID_OUTCOMES)
    failed = [r for r in rows if r["outcome"] == kb.OUTCOME_FAILED]
    assert len(failed) == HISTORY_FAILED, "失败案例的比例被动过了"
    assert {r["tenant_id"] for r in rows} == {TENANT_A, TENANT_B}
    for row in rows:
        assert row["source_case_id"], "历史案例缺 source_case_id，核验器第 7 项要它"
        assert isinstance(json.loads(row["body"]), dict), "body 不是 JSON 对象"


def test_failed_history_cases_are_labelled_failure_hint_in_the_corpus_itself():
    """失败案例在**语料侧**就是 `failure_hint`，不靠装载侧补那一刀（T118）。

    从前 24 条的 kind 一律写 `history_case`，靠 `fixtures.seed_history_kb` 按 outcome
    分流。那条路上落库结果一直是对的，但只要有第二条装载路径照着语料的 kind 直装
    （`_load_all` 就是），失败案例就会以 `history_case` 的身份进 `kb.POSITIVE_KINDS`，
    被 `guardrails.apply_suggestions` 当成规划正例 —— 它**只按 kind 过滤，不看 outcome**。

    所以这条断言钉的不是「标签好看」，是「失败案例进不了正例」这件事在语料侧就成立。
    """
    rows = _history_rows()
    for row in rows:
        expected = (kb.KIND_FAILURE_HINT if row["outcome"] == kb.OUTCOME_FAILED
                    else kb.KIND_HISTORY_CASE)
        assert row["kind"] == expected, f"{row['doc_id']} 的 kind 与 outcome 对不上"
    assert not [r for r in rows
                if r["outcome"] == kb.OUTCOME_FAILED and r["kind"] in kb.POSITIVE_KINDS], \
        "失败案例落在了规划正例里 —— apply_suggestions 会照着它补步骤"


def test_history_corpus_workflow_version_is_an_integer():
    """`workflow_version` 是整数，不是 `"1.0.0"` 这样的字符串（T118）。

    `kb/schema.sql` 里这一列是 `INTEGER`。SQLite 弱类型，写字符串进去不报错，
    但阶段一预过滤按整数比就永远不相等 —— 症状是「按 workflow_version 查什么都查不到」，
    而且一行日志都没有。这条守着的正是那种无症状故障。
    """
    for row in _history_rows() + _process_rows():
        version = row["workflow_version"]
        assert version is None or isinstance(version, int), \
            f"{row['doc_id']} 的 workflow_version 是 {type(version).__name__}，不是整数"


def test_history_corpus_bodies_carry_no_planning_steps():
    """历史案例的 body 是叙述（situation/action/…），**不是**可照做的步骤清单。

    这条不是文风检查：`guardrails.apply_suggestions` 只认 body 里的 `steps`，
    语料哪天多出这个键，R5 的 DAG 就会凭空多出几步，而两版 diff 照样是绿的。
    """
    for row in _history_rows():
        assert "steps" not in json.loads(row["body"])


# ---------------------------------------------------------------------------
# 3. 检索漏斗 —— 三级数字一起看才说明问题
# ---------------------------------------------------------------------------
def test_full_corpus_loads_and_the_retrieval_funnel_holds(store):
    _load_all(store)

    total = kb.query(store, "SELECT COUNT(1) AS n FROM kb_doc")[0]["n"]
    same_tenant = kb.query(
        store, "SELECT COUNT(1) AS n FROM kb_doc WHERE tenant_id=?", (TENANT_A,))[0]["n"]
    candidates = retriever.prefilter(store, R5_CONTEXT)

    assert total == FUNNEL_TOTAL
    assert same_tenant == FUNNEL_SAME_TENANT, "同租户条数变了 —— 语料的租户分布被动过"
    assert len(candidates) == FUNNEL_AFTER_PREFILTER, (
        "七维预过滤后的候选集大小变了。变大可能是某一维失效了（跨维召回），"
        "变小可能是语料的维度值改了 —— 两种都要有人看一眼")
    kinds = {c["kind"] for c in candidates}
    assert kinds == set(kb.VALID_KINDS), (
        "候选集没盖住全部八类知识 —— 评委点名的九类里有一类事实上检不到，"
        f"少的是 {sorted(set(kb.VALID_KINDS) - kinds)}")


def test_prefilter_wildcards_let_unscoped_policy_through(store):
    """`channel_scope='*'` 的政策换个渠道 / SKU 照样是候选 —— 「文档侧 NULL = 通配」。

    照抄成具体值会把一条不限渠道的政策锁死在一个渠道上，症状是「换个渠道查就查不到
    政策了」，而且不报错。

    **注意这里验的是通配那几条，不是全部** —— T118 之前这条断言的是「两侧候选集
    完全相同」，那个更强的版本之所以成立，靠的恰恰是投影把经销专属的 AS-004 也写成了
    NULL（见 `test_channel_scoped_policy_is_not_a_candidate_off_its_channel`）。
    修好之后两侧本来就该不同，所以断言收窄到「通配的那几条两侧都在」。
    """
    experiment._seed_kb_from_corpus(store)
    other = dict(R5_CONTEXT, channel_id="ch-dealer", sku="SKU-SRV-A2")
    here = {c["doc_id"] for c in retriever.prefilter(store, R5_CONTEXT)}
    there = {c["doc_id"] for c in retriever.prefilter(store, other)}
    assert here, "本渠道一条政策都取不到，这条测试就没在验通配"
    assert here <= there, \
        "换个渠道 / SKU 就查不到本来不限渠道的政策了 —— 通配没生效"


def test_channel_scoped_policy_is_not_a_candidate_off_its_channel(store):
    """限定渠道的政策（AS-004 `channel_scope='ch-dealer'`）在自营渠道取不到（T118）。

    投影从前把 `channel_id` 一律写 NULL，于是经销专属的 AS-004 对**任何**渠道都是
    候选：自营的单也命中经销规则，而 `policy.match` 那一侧按 `channel_scope` 过滤
    是对的 —— 两边口径不一致，但两边都不报错。这条守的就是那个差。
    """
    experiment._seed_kb_from_corpus(store)
    dealer_only = f"kb-policy-{TENANT_A}-AS-004-v1"
    assert kb.get_doc(store, TENANT_A, dealer_only)["channel_id"] == "ch-dealer", \
        "AS-004 的 channel_scope 没投影进 kb_doc.channel_id"

    on_dealer = {c["doc_id"] for c in retriever.prefilter(
        store, dict(R5_CONTEXT, channel_id="ch-dealer"))}
    on_online = {c["doc_id"] for c in retriever.prefilter(store, R5_CONTEXT)}
    assert dealer_only in on_dealer, "经销渠道反而取不到经销专属规则"
    assert dealer_only not in on_online, \
        "自营渠道命中了经销专属的 AS-004 —— 预过滤的渠道维形同虚设"


def test_doc_ids_are_globally_unique_across_tenants(store):
    """两个租户的 doc_id 不许重名。

    `kb_doc` 的主键是 `(tenant_id, doc_id)`，重名在表里不冲突 —— 正因如此它不报错。
    但 `KbRetrieved.docs[*].doc_id` 落进事件之后就指不到唯一一行了，
    「命中的到底是谁家那条」在证据里再也读不出来。
    """
    _load_all(store)
    rows = kb.query(store, "SELECT tenant_id, doc_id FROM kb_doc")
    per_tenant = {}
    for row in rows:
        per_tenant.setdefault(row["tenant_id"], set()).add(row["doc_id"])
    assert not (per_tenant[TENANT_A] & per_tenant[TENANT_B]), "两个租户有同名 doc_id"
    assert len({r["doc_id"] for r in rows}) == FUNNEL_TOTAL


def test_cross_tenant_never_retrieved_on_the_real_corpus(store):
    """租户 B 的那 47 条知识，对租户 A 的查询必须完全不可见。

    条数写死成常量而不是 `FUNNEL_TOTAL - FUNNEL_SAME_TENANT`：T118 起语料里有
    **三个**租户（多了 60 单批量底账的 `tnt-demo`），两个数一减得到的是「非 A 的全部」，
    不是「B 的」—— 那样即便 B 的语料整份消失，这条断言也照样绿。
    """
    _load_all(store)
    b_docs = {r["doc_id"] for r in kb.query(
        store, "SELECT doc_id FROM kb_doc WHERE tenant_id=?", (TENANT_B,))}
    assert len(b_docs) == TENANT_B_DOCS

    hits = retriever.retrieve(store, {**R5_CONTEXT, "rule_no": experiment.RULE_NO,
                                      "keyword": "轴承 锈蚀 退款 财务核算"}, limit=50)
    assert hits, "本租户自己一条都没召回，这条测试就没在验跨租户"
    assert not (b_docs & {h["doc_id"] for h in hits}), "跨租户召回 —— 这是事故"


# ---------------------------------------------------------------------------
# 4. 四通道在真语料上各自有信号 —— 单测里验过的融合，在真语料上再验一次
# ---------------------------------------------------------------------------
def test_all_four_channels_fire_on_the_real_corpus(store):
    """规则编号 / 网关错误码 / 全文 / 向量，四个通道在真语料上都要点得着。

    自造的最小集里只有 1 条候选，四通道排给谁看都一样；这里的候选集有几条同租户
    知识，精确通道命中的那条必须排在只有文本相关的前面。
    """
    _load_all(store)
    # 两个精确通道都要点着，所以 probe 必须**同时**带 rule_no 与 gateway_code。
    # 只按 `gateway_code IS NOT NULL` 挑（T118 之前的写法）现在会挑中错误码处置手册
    # —— 那一类按定义不挂规则编号，于是 query 里没有 rule_no，
    # 下面那条 `channels["rule_no"] == 1.0` 就永远不可能成立。
    gateway_rows = kb.query(
        store, "SELECT doc_id, rule_no, gateway_code FROM kb_doc"
               " WHERE tenant_id=? AND gateway_code IS NOT NULL AND rule_no IS NOT NULL"
               " ORDER BY doc_id",
        (TENANT_A,))
    assert gateway_rows, "语料里租户 A 没有同时带规则编号与网关错误码的案例，本条无从验起"
    probe = gateway_rows[0]

    hits = retriever.retrieve(store, {
        "tenant_id": TENANT_A, "biz_type": "refund",
        "rule_no": probe["rule_no"], "gateway_code": probe["gateway_code"],
        "keyword": "退款 网关 失败"}, limit=20)

    by_id = {h["doc_id"]: h for h in hits}
    assert probe["doc_id"] in by_id, "精确通道双命中的那条没被召回"
    top = by_id[probe["doc_id"]]
    assert top["channels"]["rule_no"] == 1.0 and top["channels"]["gateway_code"] == 1.0
    assert hits[0]["doc_id"] == probe["doc_id"], \
        "两个精确通道都命中的那条没排第一 —— 融合把精确通道退化成模糊通道了"

    fired = {ch for h in hits for ch, score in h["channels"].items() if score > 0}
    assert fired == set(retriever.CHANNELS), f"这几个通道在真语料上一次都没点着：" \
                                             f"{set(retriever.CHANNELS) - fired}"


def test_retrieval_on_the_real_corpus_is_reproducible(store):
    """同一份语料连查两次，命中与分数逐条一致 —— 演示要能复现。"""
    _load_all(store)
    query = {**R5_CONTEXT, "rule_no": experiment.RULE_NO, "keyword": "轴承 锈蚀 退款"}
    first = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query, limit=20)]
    second = [(h["doc_id"], h["score"]) for h in retriever.retrieve(store, query, limit=20)]
    assert first and first == second


# ---------------------------------------------------------------------------
# 5. R5 的装载路径本身
# ---------------------------------------------------------------------------
def test_r5_seeds_policy_corpus_and_never_seeds_history_cases(store):
    """R5 的库里只该有政策投影 —— 历史案例进去会让核验器第 7 项当场翻脸。"""
    assert experiment._seed_kb_from_corpus(store) == POLICY_RULES
    rows = kb.query(store, "SELECT kind, COUNT(1) AS n FROM kb_doc GROUP BY kind")
    assert {r["kind"]: r["n"] for r in rows} == {kb.KIND_POLICY: POLICY_RULES}

    projected = kb.get_doc(
        store, TENANT_A, f"kb-policy-{TENANT_A}-{experiment.RULE_NO}-v1")
    assert projected is not None, "R5 检索上下文用的那条规则没被投影进来"
    assert projected["rule_no"] == experiment.RULE_NO
    assert projected["policy_version"] == 1
    assert (projected["channel_id"], projected["region"], projected["sku"]) == (None,) * 3
    assert projected["source_case_id"] is None, "政策不是从某一单里沉淀出来的"


def test_missing_corpus_file_raises_instead_of_falling_back(monkeypatch):
    """语料不在就抛。回落到自造的最小集会让 R5 照常跑绿而候选集悄悄退回 1 条。"""
    monkeypatch.setattr(experiment, "CORPUS_ROOT", "/nonexistent/corpus/root")
    with pytest.raises(FileNotFoundError):
        experiment.load_corpus(os.path.join("policy", "policy_rules.json"))
