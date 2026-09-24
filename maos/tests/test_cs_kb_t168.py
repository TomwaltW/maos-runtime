"""p12 T168：话术 kind、话术库与话术检索的机器守卫（契约 review/p12-cs-contracts.md §1.4 / §1.5）。

守五层：

1. **kind 常量**：``kb.KIND_CS_SCRIPT`` 与冻结的 ``types.CS_KB_KIND`` 逐字相等，进
   ``VALID_KINDS``、**不进** ``POSITIVE_KINDS``（话术没有改 DAG 形状的权力）。
2. **话术库**：生成器 ``--check`` 逐字节通过；语料列清单 == ``kb.DOC_COLUMNS``；19 个编号
   与契约目录逐项一致（目录在本文件写死一份 —— 从生成器取就成了拿被测物验被测物）。
3. **标准话术出门前的底线**：p12 没有观察来源，后置校验的 observations 恒为空，
   所以每篇话术在空观察下必须说不出任何状态字眼，也不许承诺时限 / 金额 / 结果、
   不许露内部口径。
4. **检索质量**：自检索（每条例句检出时本篇排第一）与泛化（holdout 改写 top-1、
   无关句全部低于门槛）。
5. **审计与隔离**：一次检索恰好一条 KbRetrieved、客户原文不进 event_log；KB 关着零事件；
   话术与退款语料同库时互相检不到；别的租户检不到 tnt-demo 的话术。
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.domain.cs import corpus, scripts
from maos.domain.cs import types as T
from maos.kb import experiment, retriever

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOLDOUT_PATH = ROOT / "scenarios" / "cs" / "kb" / "cs_scripts_holdout.json"
ROLES_PATH = ROOT / "scenarios" / "refund" / "roles.json"

TENANT = "tnt-demo"
PLAN = T.plan_id_for("csc-t168test000000")
TURN = T.turn_id_for("csc-t168test000000", 1)

#: 契约 §1.5 的冻结目录：编号 → (intent, 适用场景, handoff 标记)。逐字照抄契约表格。
CATALOG_T168 = {
    "LOG-001": ("logistics", "下单后多久发货（发货时效）", ""),
    "LOG-002": ("logistics", "用哪家快递、配送范围", ""),
    "LOG-003": ("logistics", "运费规则、包邮条件", ""),
    "LOG-004": ("logistics", "查某个订单的物流进度、催发货", "needs_order_lookup"),
    "LOG-005": ("logistics", "改收货地址、改约配送", "needs_order_lookup"),
    "LOG-006": ("logistics", "物流显示签收但本人没收到、包裹丢失或破损", "needs_order_lookup"),
    "PAY-001": ("refund_payment", "退款多久能退回（原路退回规则，以支付渠道为准）", ""),
    "PAY-002": ("refund_payment", "支持哪些支付方式", ""),
    "PAY-003": ("refund_payment", "查某笔退款的进度、钱到没到", "needs_order_lookup"),
    "PAY-004": ("refund_payment", "支付失败、重复扣款", "needs_order_lookup"),
    "PAY-005": ("refund_payment", "开发票", ""),
    "RET-001": ("return_exchange", "七天无理由退货的条件", ""),
    "RET-002": ("return_exchange", "退货流程怎么操作", ""),
    "RET-003": ("return_exchange", "质量问题换货（要提供照片）", ""),
    "RET-004": ("return_exchange", "退货运费谁承担", ""),
    "RET-005": ("return_exchange", "为某个订单申请退货 / 退款（要办具体订单）", "needs_order_lookup"),
    "GEN-001": ("general", "问候", ""),
    "GEN-002": ("general", "感谢与道别", ""),
    "GEN-003": ("general", "人工客服的服务时间", ""),
}

BODY_KEYS_T168 = {"scheme_no", "scene", "principle", "script", "intent", "handoff",
                  "synonyms", "examples", "synthetic"}

#: 标准话术里不许出现的字样（本文件自己的一份，不从生成器取）。
FORBIDDEN_WORDS_T168 = ("/", "审批", "规则编号", "MAOS")
#: 时限 / 金额 / 结果承诺。数字一律不许出现：时限与金额都靠它表达。
PROMISE_RE_T168 = re.compile(
    r"[0-9０-９]|(?:天|小时|工作日|分钟)内|当天|马上|立即|立刻|尽快|保证|一定|肯定|承诺"
    r"|元|块钱|全额|包退|包换|补发|赔")
#: 语气词：每篇至少一条例句带它（客户不会用书面语问）。
PARTICLES_T168 = "啊呀吗嘛呢吧哦哈不么啦拉呗亲哇"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
@pytest.fixture
def store_t168():
    s = SqliteStore()
    s.init_schema()
    kb.ensure_schema(s)
    return s


@pytest.fixture
def seeded_t168(store_t168):
    assert corpus.seed_cs_kb(store_t168) == len(CATALOG_T168)
    return store_t168


def _bodies_t168() -> dict[str, dict]:
    return {row["rule_no"]: json.loads(row["body"]) for row in corpus.load_corpus()}


def _norm_t168(text: str) -> str:
    return "".join(kb.tokenize(text))


def _events_t168(store) -> list[dict]:
    rows = kb.query(store, "SELECT * FROM event_log ORDER BY seq")
    for row in rows:
        row["detail"] = json.loads(row["detail"])
    return rows


def _kb_events_t168(store) -> list[dict]:
    return [e for e in _events_t168(store) if e["event_type"] == "KbRetrieved"]


def _match_t168(store, text: str, *, tenant_id: str = TENANT, limit: int = 3):
    return scripts.match_scripts(store, tenant_id=tenant_id, text=text,
                                 plan_id=PLAN, task_id=TURN, limit=limit)


def _internal_role_names_t168() -> set[str]:
    roles = json.loads(ROLES_PATH.read_text(encoding="utf-8"))["roles"]
    names = set(roles)
    for spec in roles.values():
        names.add(spec["title"])
        if spec.get("verdict_role"):
            names.add(spec["verdict_role"])
    return names


# ---------------------------------------------------------------------------
# 1. kind 常量
# ---------------------------------------------------------------------------
def test_kind_constant_equals_the_frozen_type_and_is_not_a_positive_kind():
    assert kb.KIND_CS_SCRIPT == T.CS_KB_KIND == "cs_script"
    assert kb.KIND_CS_SCRIPT in kb.VALID_KINDS
    assert kb.KIND_CS_SCRIPT not in kb.POSITIVE_KINDS, \
        "话术进了规划正例 —— apply_suggestions 会拿它去改 DAG 的形状"
    schema = (ROOT / "maos" / "kb" / "schema.sql").read_text(encoding="utf-8")
    assert "'cs_script'" in schema.split("CHECK (kind IN (", 1)[1].split("))", 1)[0]


# ---------------------------------------------------------------------------
# 2. 话术库
# ---------------------------------------------------------------------------
def test_generator_check_passes():
    """产物与生成器逐字节一致：手改了 JSON、或改了生成器没重跑，这条红。"""
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_cs_kb.py"), "--check"],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_corpus_rows_match_the_frozen_catalog_one_by_one():
    rows = corpus.load_corpus()
    assert corpus.CORPUS_PATH == ROOT / "scenarios" / "cs" / "kb" / "cs_scripts.json"
    by_no = {row["rule_no"]: row for row in rows}
    assert len(rows) == len(by_no), "方案编号重复"
    assert set(by_no) == set(CATALOG_T168), (
        f"编号与契约目录不一致：多 {sorted(set(by_no) - set(CATALOG_T168))}，"
        f"少 {sorted(set(CATALOG_T168) - set(by_no))}")
    for no, (intent, scene, handoff) in CATALOG_T168.items():
        row = by_no[no]
        assert set(row) == set(kb.DOC_COLUMNS)
        assert row["tenant_id"] == TENANT
        assert row["doc_id"] == f"kb-cs-{TENANT}-{no}"
        assert row["kind"] == T.CS_KB_KIND and row["biz_type"] == T.BIZ_TYPE_CS
        assert row["title"] == scene
        for col in ("channel_id", "region", "sku", "policy_version", "workflow_version",
                    "gateway_code", "embedding", "outcome", "source_case_id"):
            assert row[col] is None, f"{no} 的 {col} 应为 NULL"
        body = json.loads(row["body"])
        assert set(body) == BODY_KEYS_T168, f"{no} 的 body 键不对：{sorted(body)}"
        assert (body["scheme_no"], body["intent"], body["scene"], body["handoff"]) == \
            (no, intent, scene, handoff)
        assert body["synthetic"] is True
        assert body["principle"].strip() and body["script"].strip()
        assert len(body["examples"]) >= 5, f"{no} 例句不足 5 条"
        assert body["synonyms"], f"{no} 没有同义词"
        assert any(ch in ex for ex in body["examples"] for ch in PARTICLES_T168), \
            f"{no} 没有一条带语气词的例句"


def test_load_corpus_rejects_missing_and_extra_columns(tmp_path):
    good = corpus.load_corpus()[0]
    missing = {k: v for k, v in good.items() if k != "sku"}
    extra = {**good, "note": "多出来的一列"}
    for idx, row in enumerate((missing, extra)):
        path = tmp_path / f"bad{idx}.json"
        path.write_text(json.dumps({"kb_doc": [row]}, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError):
            corpus.load_corpus(path)
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"kb_doc": [good]}, ensure_ascii=False), encoding="utf-8")
    assert corpus.load_corpus(path) == [good]


def test_seed_is_idempotent_and_honours_the_tenant_filter(store_t168):
    assert corpus.seed_cs_kb(store_t168, tenant_id="tnt-other") == 0
    assert corpus.seed_cs_kb(store_t168, tenant_id=TENANT) == len(CATALOG_T168)
    assert corpus.seed_cs_kb(store_t168) == len(CATALOG_T168)
    rows = kb.list_docs(store_t168, kind=T.CS_KB_KIND)
    assert len(rows) == len(CATALOG_T168)
    assert {r["tenant_id"] for r in rows} == {TENANT}


# ---------------------------------------------------------------------------
# 3. 标准话术的底线
# ---------------------------------------------------------------------------
def test_every_script_is_speakable_under_empty_observations():
    roles = _internal_role_names_t168()
    assert roles, "角色目录读空了，内部岗位名这一条就没在验"
    for no, body in _bodies_t168().items():
        script = body["script"]
        for pattern in T.STATUS_PATTERNS:
            assert not pattern.search(script), f"{no} 的话术命中状态字眼 {pattern.pattern}"
        for word in FORBIDDEN_WORDS_T168:
            assert word not in script, f"{no} 的话术含 {word!r}"
        for name in roles:
            assert name.lower() not in script.lower(), f"{no} 的话术露出内部岗位名 {name!r}"
        found = PROMISE_RE_T168.search(script)
        assert not found, f"{no} 的话术有承诺字样 {found.group(0) if found else ''!r}"


def test_handoff_scripts_are_transitions_and_others_do_not_claim_a_handoff():
    for no, body in _bodies_t168().items():
        script = body["script"]
        if body["handoff"]:
            assert "核实" in script and "订单" in script and "转接人工客服" in script, \
                f"{no} 带转人工标记，话术却没告诉客户要核实订单、已为其转人工"
        else:
            assert "已为您转" not in script, f"{no} 不转人工，话术却说已转"


# ---------------------------------------------------------------------------
# 4. 检索质量
# ---------------------------------------------------------------------------
def test_every_example_retrieves_its_own_script_first(seeded_t168):
    """自检索：每篇的每条例句检出时，该篇排第一且过门槛。"""
    failures = []
    for row in corpus.load_corpus():
        for example in json.loads(row["body"])["examples"]:
            hits = _match_t168(seeded_t168, example)
            if not hits or hits[0].doc_id != row["doc_id"] \
                    or hits[0].score < scripts.MIN_SCRIPT_SCORE:
                failures.append((row["rule_no"], example, hits[:1]))
    assert not failures, failures


def test_holdout_file_is_well_formed_and_disjoint_from_the_corpus():
    holdout = json.loads(HOLDOUT_PATH.read_text(encoding="utf-8"))
    bodies = _bodies_t168()
    assert set(holdout["rewrites"]) == set(CATALOG_T168)
    seen = {_norm_t168(v) for b in bodies.values() for v in b["examples"] + b["synonyms"]}
    for no, rewrites in holdout["rewrites"].items():
        assert len(rewrites) >= 2, f"{no} 的改写不足 2 条"
        for text in rewrites:
            assert _norm_t168(text) not in seen, f"{no} 的改写 {text!r} 抄了库里的说法"
    assert len(holdout["unrelated"]) >= 12


def test_holdout_rewrites_generalise_and_unrelated_text_stays_below_the_threshold(seeded_t168):
    """泛化。实测（task-t168，2026-09-24，57 条改写 = 19 篇 x 3 条）：
    top-1 正确 55/57 = 0.965（错的两条：「偏远地区邮费另算吗」→ LOG-002、
    「发票在哪里开呀」→ RET-002）；top-1 正确且分数 ≥ MIN_SCRIPT_SCORE(0.25) 的 49/57 = 0.860；
    无关句 18 条 + 英文售后 4 条的最高分 0.194（「给我讲个笑话吧」）。
    """
    holdout = json.loads(HOLDOUT_PATH.read_text(encoding="utf-8"))
    total = correct = answerable = 0
    for no, rewrites in holdout["rewrites"].items():
        for text in rewrites:
            hits = _match_t168(seeded_t168, text)
            total += 1
            if hits and hits[0].scheme_no == no:
                correct += 1
                answerable += hits[0].score >= scripts.MIN_SCRIPT_SCORE
    assert correct / total >= 0.9, f"holdout top-1 准确率 {correct}/{total}"
    assert answerable / total >= 0.8, f"holdout 过门槛且判对的只有 {answerable}/{total}"

    over = []
    for text in holdout["unrelated"] + holdout["english"]:
        hits = _match_t168(seeded_t168, text)
        if hits and hits[0].score >= scripts.MIN_SCRIPT_SCORE:
            over.append((text, hits[0].scheme_no, hits[0].score))
    assert not over, f"无关句过了门槛：{over}"


def test_hits_are_sorted_bounded_and_deterministic(seeded_t168):
    text = "退款多久能到账啊"
    first = _match_t168(seeded_t168, text, limit=3)
    second = _match_t168(seeded_t168, text, limit=3)
    assert first == second
    assert 1 <= len(first) <= 3
    assert [h.score for h in first] == sorted((h.score for h in first), reverse=True)
    assert all(0.0 < h.score <= 1.0 for h in first)
    assert len(_match_t168(seeded_t168, text, limit=1)) == 1
    bodies = _bodies_t168()
    for hit in first:
        body = bodies[hit.scheme_no]
        assert (hit.intent, hit.script, hit.principle, hit.handoff) == \
            (body["intent"], body["script"], body["principle"], body["handoff"])
    lookup = _match_t168(seeded_t168, "我的快递到哪了啊")[0]
    assert (lookup.scheme_no, lookup.handoff) == ("LOG-004", T.HANDOFF_NEEDS_ORDER_LOOKUP)


# ---------------------------------------------------------------------------
# 5. 审计与隔离
# ---------------------------------------------------------------------------
def test_exactly_one_kb_retrieved_without_the_customer_text(seeded_t168):
    sentinel = "SENTINELT168QWXZ"
    text = f"我的快递到哪了啊{sentinel}"
    before = len(_events_t168(seeded_t168))
    hits = _match_t168(seeded_t168, text)
    events = _events_t168(seeded_t168)[before:]
    assert hits, "哨兵句一篇都没检出，下面的「docs 与返回一致」就没在验"
    assert len(events) == 1 and events[0]["event_type"] == "KbRetrieved", events
    event = events[0]
    assert (event["plan_id"], event["task_id"], event["trace_id"]) == (PLAN, TURN, "")
    detail = event["detail"]
    assert [(d["doc_id"], d["score"]) for d in detail["docs"]] == \
        [(h.doc_id, h.score) for h in hits]
    assert detail["hit_count"] == len(hits)
    assert detail["query"] == {"tenant_id": TENANT, "biz_type": T.BIZ_TYPE_CS,
                               "keyword": T.text_digest(text)}
    for doc in detail["docs"]:
        assert kb.get_doc(seeded_t168, TENANT, doc["doc_id"]) is not None
    serialized = json.dumps(_events_t168(seeded_t168), ensure_ascii=False)
    assert sentinel not in serialized and sentinel.lower() not in serialized
    assert text not in serialized


def test_nothing_found_still_logs_exactly_one_empty_event(seeded_t168):
    hits = _match_t168(seeded_t168, "asdfghjkl")
    events = _kb_events_t168(seeded_t168)
    assert hits == []
    assert len(events) == 1 and events[0]["detail"]["docs"] == []


def test_kb_switched_off_means_no_hits_and_no_event(seeded_t168, monkeypatch):
    monkeypatch.setenv(kb.KB_ENABLED_ENV, "0")
    assert _match_t168(seeded_t168, "我的快递到哪了啊") == []
    assert _kb_events_t168(seeded_t168) == []


def test_refund_and_cs_corpora_do_not_see_each_other(seeded_t168):
    store = seeded_t168
    experiment._seed_kb_from_corpus(store)
    experiment.seed_process_kb(store)
    # 再往 R5 的租户灌一份话术：隔离得靠 biz_type / kind，不能只靠租户不同。
    for row in corpus.load_corpus():
        kb.upsert_doc(store, {**row, "tenant_id": experiment.TENANT_ID})
    refund_in_demo = [d for d in kb.list_docs(store, tenant_id=TENANT)
                      if d["kind"] != T.CS_KB_KIND]
    assert refund_in_demo, "tnt-demo 里没有退款文档，「话术检不到退款」就没在验"

    r5 = {"tenant_id": experiment.TENANT_ID, "biz_type": experiment.BIZ_TYPE,
          "channel_id": experiment.CHANNEL_ID, "region": experiment.REGION,
          "sku": experiment.SKU, "policy_version": experiment.POLICY_VERSION}
    assert T.CS_KB_KIND not in {c["kind"] for c in retriever.prefilter(store, r5)}
    r5_hits = retriever.retrieve(store, {**r5, "keyword": "退款多久能到账 发货 快递"}, limit=0)
    assert r5_hits and T.CS_KB_KIND not in {h["kind"] for h in r5_hits}
    demo_refund = retriever.retrieve(
        store, {"tenant_id": TENANT, "biz_type": "refund", "keyword": "退款多久能到账"}, limit=0)
    assert demo_refund and T.CS_KB_KIND not in {h["kind"] for h in demo_refund}

    for text in ("退款多久能到账啊", "我的退款到哪一步了", "支付失败重复扣款了"):
        hits = _match_t168(store, text)
        assert hits and all(h.doc_id.startswith(f"kb-cs-{TENANT}-") for h in hits)
    for event in _kb_events_t168(store):
        assert {d["kind"] for d in event["detail"]["docs"]} <= {T.CS_KB_KIND}


def test_a_cs_script_without_biz_type_is_not_served(seeded_t168):
    """阶段一把文档侧 NULL 当通配；话术漏写 biz_type 也不许混进来。"""
    row = next(r for r in corpus.load_corpus() if r["rule_no"] == "LOG-004")
    kb.upsert_doc(seeded_t168, {**row, "doc_id": "kb-cs-wild-LOG-004", "biz_type": None})
    hits = _match_t168(seeded_t168, "我的快递到哪了啊", limit=19)
    assert hits and "kb-cs-wild-LOG-004" not in {h.doc_id for h in hits}


def test_other_tenants_cannot_retrieve_tnt_demo_scripts(seeded_t168):
    hits = _match_t168(seeded_t168, "我的快递到哪了啊", tenant_id="tnt-other")
    assert hits == []
    events = _kb_events_t168(seeded_t168)
    assert len(events) == 1 and events[0]["detail"]["docs"] == []
    assert _match_t168(seeded_t168, "我的快递到哪了啊"), "本租户都检不到，上面那条就没在验"
