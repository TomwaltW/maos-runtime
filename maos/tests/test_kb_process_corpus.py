"""T118 流程知识语料（`scenarios/refund/kb/`）的机器守卫。

评委第二条建议点名了**九类**面向 workflow 规划的企业流程知识：售后政策及生效范围、
产品与渠道差异、历史退款原因、任务拆分、人工驳回、支付错误码、超时与补偿路径、
客户沟通结果、真实到账结果。检索机制（两阶段检索、四通道融合、三条护栏、R5 对照）
在 T118 之前就齐了，缺的是**知识本身** —— `kb_doc` 那时只有 `policy` 与 `history_case`
两类，九类里覆盖了三类。

本文件守四层，每一层都对着一种「不报错的失效」：

1. **覆盖与规模**。九类都要有、每类不少于 `KIND_FLOOR` 条。少了某一类不会报错，
   只会表现为「问这类问题时检索返回的全是别的东西」。
2. **正例边界**。`policy` 与 `error_code_playbook` 在 `kb.POSITIVE_KINDS` 里，
   它们的 body 一旦多出 `steps` 键，`guardrails.apply_suggestions` 就会照着它往
   当前 DAG 里补步骤 —— 而两版 DAG 的 diff 照样是绿的。这是本文件最硬的一条。
3. **非合成字段的忠实度**。错误码那几个字段是程序化搬运的，不是人工转写；
   任务形状取自真实 DAG，不是手写的。两者都当场比对，漂了就红。
4. **生成器的确定性**。产物必须是「同样输入两次生成逐字节相同」，否则语料每跑一次
   就产生一份无意义的 diff，`git diff` 从此不能当判据用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from maos import kb
from maos.core.store import SqliteStore
from maos.kb import experiment, retriever

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GENERATOR = os.path.join(REPO_ROOT, "scripts", "gen_refund_kb.py")

TENANT_A = "tnt-mfg-a"
TENANT_B = "tnt-mfg-b"
TENANT_DEMO = "tnt-demo"

#: 每类知识的条数下限，与 `test_kb_corpus.KIND_FLOOR` 同一个数（派单的验收判据）。
KIND_FLOOR = 5
#: 流程知识语料的总量下限。
PROCESS_FLOOR = 80

#: 评委点名的九类 -> `kb_doc.kind`。九类落成八个 kind：「产品与渠道差异」走 `policy`
#: 的渠道 / 品类变体，不另立一类 —— 它讲的是同一条规则在不同渠道上落地的差异。
NINE_CATEGORIES = {
    "售后政策及生效范围": kb.KIND_POLICY,
    "产品与渠道差异": kb.KIND_POLICY,
    "历史退款原因": kb.KIND_HISTORY_CASE,
    "任务拆分": kb.KIND_TASK_PATTERN,
    "人工驳回": kb.KIND_REJECTION,
    "支付错误码": kb.KIND_ERROR_CODE_PLAYBOOK,
    "超时与补偿路径": kb.KIND_ERROR_CODE_PLAYBOOK,
    "客户沟通结果": kb.KIND_COMMS_RESULT,
    "真实到账结果": kb.KIND_ARRIVAL_RESULT,
}


def _process_rows() -> list[dict]:
    """全部流程知识语料，逐行过列清单守卫 —— 读语料这一步就是校验这一步。"""
    rows: list[dict] = []
    for name in experiment.PROCESS_CORPUS_FILES:
        payload = experiment.load_corpus(os.path.join("kb", name))
        rows.extend(experiment._checked_rows(payload, "kb_doc", kb.DOC_COLUMNS))
    return rows


@pytest.fixture
def store():
    s = SqliteStore()
    s.init_schema()
    kb.ensure_schema(s)
    return s


# ---------------------------------------------------------------------------
# 1. 覆盖与规模 —— 少一类不会报错，只会「问这类问题时答非所问」
# ---------------------------------------------------------------------------
def test_nine_categories_all_have_knowledge(store):
    """九类都要在库里有文档，且每类不少于 `KIND_FLOOR` 条。

    装的是**整份语料**（历史案例 + 政策 + 流程知识），不是 R5 那个库：
    R5 的库按设计不装 24 条历史案例（核验器第 7 项要每条 `history_case` 的
    `source_case_id` 都回查得到一条本库 `refund_case`，外部导入的历史知识没有），
    所以拿 R5 的库验「九类齐不齐」会把 `history_case` 判成空 —— 那是装载策略，
    不是语料缺这一类。
    """
    from maos.domain.refund import fixtures

    fixtures.seed_history_kb(store)
    experiment._seed_kb_from_corpus(store)
    experiment.seed_process_kb(store)
    counted = {r["kind"]: r["n"] for r in kb.query(
        store, "SELECT kind, COUNT(1) AS n FROM kb_doc GROUP BY kind")}

    for category, kind in sorted(NINE_CATEGORIES.items()):
        assert counted.get(kind, 0) >= KIND_FLOOR, (
            f"「{category}」落到 kind={kind}，库里只有 {counted.get(kind, 0)} 条，"
            f"不足 {KIND_FLOOR} 条 —— 这一类知识事实上不存在，而检索照样返回结果")


def test_process_corpus_is_large_enough_and_covers_every_new_kind():
    rows = _process_rows()
    assert len(rows) >= PROCESS_FLOOR, f"流程知识只有 {len(rows)} 条，不足 {PROCESS_FLOOR} 条"

    counted: dict[str, int] = {}
    for row in rows:
        counted[row["kind"]] = counted.get(row["kind"], 0) + 1
    for kind in (kb.KIND_TASK_PATTERN, kb.KIND_REJECTION,
                 kb.KIND_COMMS_RESULT, kb.KIND_ARRIVAL_RESULT,
                 kb.KIND_ERROR_CODE_PLAYBOOK, kb.KIND_POLICY):
        assert counted.get(kind, 0) >= KIND_FLOOR, f"{kind} 只有 {counted.get(kind, 0)} 条"


def test_process_corpus_values_stay_in_range():
    """kind / outcome 落在取值域里，doc_id 全局唯一，body 是 JSON 对象。

    `doc_id` 的唯一性是跨租户的：`kb_doc` 主键是 `(tenant_id, doc_id)`，重名在表里
    不冲突 —— 正因如此它不报错。但 `KbRetrieved.docs[*].doc_id` 落进事件之后就指不到
    唯一一行了，「命中的到底是谁家那条」在证据里再也读不出来。
    """
    rows = _process_rows()
    seen: set[str] = set()
    for row in rows:
        assert row["kind"] in kb.VALID_KINDS, f"{row['doc_id']} 的 kind 越界"
        assert row["outcome"] is None or row["outcome"] in kb.VALID_OUTCOMES
        assert row["doc_id"] not in seen, f"doc_id 重名：{row['doc_id']}"
        seen.add(row["doc_id"])
        assert isinstance(json.loads(row["body"]), dict), f"{row['doc_id']} 的 body 不是 JSON 对象"
        assert row["embedding"] is None, "语料里不许预置向量 —— 那等于替使用方选了嵌入实现"
        assert row["tenant_id"] in (TENANT_A, TENANT_B, TENANT_DEMO)


def test_schema_check_and_valid_kinds_are_the_same_list():
    """`schema.sql` 的 CHECK 与 `kb.VALID_KINDS` 是同一份取值域，改一处必须改两处。

    分叉的后果不对称：CHECK 少一个值 -> 写入当场抛（还算响）；`VALID_KINDS` 少一个值
    -> `upsert_doc` 先抛，落不进库 —— 两种都是「某一类知识静默地进不来」。
    """
    schema = open(os.path.join(REPO_ROOT, "maos", "kb", "schema.sql"), encoding="utf-8").read()
    check = schema.split("CHECK (kind IN (", 1)[1].split("))", 1)[0]
    in_check = {part.strip().strip("'") for part in check.replace("\n", " ").split(",")}
    assert in_check == set(kb.VALID_KINDS), (
        f"schema.sql 的 CHECK 与 kb.VALID_KINDS 分叉："
        f"CHECK 多 {sorted(in_check - set(kb.VALID_KINDS))}，"
        f"少 {sorted(set(kb.VALID_KINDS) - in_check)}")


# ---------------------------------------------------------------------------
# 2. 正例边界 —— 本文件最硬的一条
# ---------------------------------------------------------------------------
def test_positive_kinds_carry_no_planning_steps():
    """进「规划正例」的那几类，body 里**不许**有 `steps` 键。

    `guardrails.apply_suggestions` 只按 kind 过滤（`kb.POSITIVE_KINDS`），命中的正例
    文档 body 里有 `steps` 就会被并进当前 DAG。`policy` 与 `error_code_playbook`
    都在正例里，它们哪天多出这个键，R5 的计划就凭空多出几步 ——
    而两版 DAG 的 diff 照样是绿的，没有任何症状。
    """
    for row in _process_rows():
        if row["kind"] not in kb.POSITIVE_KINDS:
            continue
        body = json.loads(row["body"])
        assert "steps" not in body, (
            f"{row['doc_id']}（kind={row['kind']}）是规划正例却带了 steps —— "
            "apply_suggestions 会照着它改 DAG 的形状")


def test_task_pattern_is_not_a_positive_kind_but_keeps_the_steps_shape():
    """`task_pattern` 的 body 用 `steps`（与 `_steps_of` 对齐），但它今天不进正例。

    两件事都要钉住：键名对齐，是为了将来把它加进 `POSITIVE_KINDS` 一处就能启用；
    今天不进正例，是因为「让一类知识自动改写 DAG 形状」是规划面的判断（T119），
    不是语料面能替它定的。哪天有人把它加进正例，这条会红 —— 那正是该有人来看一眼的时候。
    """
    from maos.kb import guardrails

    assert kb.KIND_TASK_PATTERN not in kb.POSITIVE_KINDS

    patterns = [r for r in _process_rows() if r["kind"] == kb.KIND_TASK_PATTERN]
    assert patterns, "一条任务拆分知识都没有"
    for row in patterns:
        steps = guardrails._steps_of(row)
        assert steps, f"{row['doc_id']} 的 body 里读不出步骤 —— 键名与 _steps_of 对不上"
        for step in steps:
            assert step.get("role"), "步骤缺 role，collect_steps 会整条跳过"
            # 依赖存成 [role, step] 键而不是 task_id：id 只在那一份计划里有意义。
            for dep in step.get("depends_on_keys") or []:
                assert isinstance(dep, list) and len(dep) == 2, f"{row['doc_id']} 的依赖键形状不对"


def test_task_pattern_steps_match_the_real_dag():
    """任务形状逐字段取自真实 DAG，不是手写的第二份。

    手写的后果是流程改了形状而语料不知道，于是「任务拆分知识」教的是一条早就不存在的
    流程，且没有任何东西会红。
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import gen_refund_kb
    finally:
        sys.path.pop(0)
    from maos.flows import scenario_6

    want = gen_refund_kb._shape_from_tasks(scenario_6._TASKS)
    got = None
    for row in _process_rows():
        if row["doc_id"] == f"kb-tp-mfga-happy-path":
            got = json.loads(row["body"])["steps"]
    assert got is not None, "顺利路径那条任务拆分知识不见了"
    assert got == want, "语料里的步骤形状与 scenario_6 的真实 DAG 漂开了"

    facts = {"case_id", "tenant_id", "order_id", "amount_claimed", "case_seed", "signals"}
    for step in got:
        assert not (facts & set(step.get("inputs") or {})), \
            "步骤里残留了那一单的事实字段 —— 知识层只存步骤，事实从当前 case 来（护栏 2）"


# ---------------------------------------------------------------------------
# 3. 非合成字段的忠实度 —— 错误码是搬运的，不是凭记忆写的
# ---------------------------------------------------------------------------
def test_playbook_gateway_fields_are_copied_verbatim_from_the_code_table():
    """11 条码逐字段比对 `maos/tools/gateway_codes.py`，一个字段漂了就红。

    手打就等于「凭记忆写」，而凭记忆写正是那张码表存在的理由所要防的事。
    """
    from maos.tools import gateway_codes as gcodes

    books = [r for r in _process_rows() if r["kind"] == kb.KIND_ERROR_CODE_PLAYBOOK]
    assert books, "一条错误码处置手册都没有"

    covered: dict[str, set] = {}
    for row in books:
        code = row["gateway_code"]
        assert code in gcodes.ALL_CODES, f"{row['doc_id']} 用了未收录的码 {code!r}"
        spec = gcodes.lookup(code)
        got = json.loads(row["body"])["gateway"]
        assert got == {
            "code": spec.code, "message": spec.message, "retriable": spec.retriable,
            "outcome": spec.outcome, "remedy": spec.remedy, "layer": spec.layer,
            "source": spec.source,
        }, f"{row['doc_id']} 的 gateway 字段与码表漂开了"
        covered.setdefault(row["tenant_id"], set()).add(code)

    for tenant, codes in sorted(covered.items()):
        assert codes == set(gcodes.ALL_CODES), \
            f"{tenant} 缺了这几条码的处置手册：{sorted(set(gcodes.ALL_CODES) - codes)}"


def test_playbook_never_flattens_unknown_into_failed():
    """官方 `outcome=unknown` 的码，`kb_doc.outcome` 一律留 NULL（铁律 8）。

    本列的取值域只有 `success` / `failed`，把 `unknown` 压成 `failed` 就是「把查不到
    当成没发生」—— 那正是铁律 8 说的、把外部状态写死成终态的那类 bug。
    官方那三个字段原样躺在 body 的 `gateway` 里，谁要用都取得到。
    """
    for row in _process_rows():
        if row["kind"] != kb.KIND_ERROR_CODE_PLAYBOOK:
            continue
        assert row["outcome"] is None, f"{row['doc_id']} 给处置手册记了 outcome，它记的不是某一单"
        body = json.loads(row["body"])
        assert body["gateway"]["outcome"] in ("success", "failed", "unknown")
        # 四象限的判据必须同时看两个字段：只看 retriable 会在 20000 上重发出第二笔退款。
        assert str(body["gateway"]["retriable"]) in body["quadrant"]
        assert body["gateway"]["outcome"] in body["quadrant"]


def test_arrival_results_never_claim_settlement_without_an_observation():
    """说「到账」的每一条都记明 `basis=payment_observation`（铁律 8）。

    没有观察行就不许说「已到账」—— 未观察到的那几条记 `unsettled` 而不是空着，
    空着会被下游当成「还没查」，而事实是「查了，没到」。
    """
    rows = [r for r in _process_rows() if r["kind"] == kb.KIND_ARRIVAL_RESULT]
    assert rows, "一条到账结果都没有"
    for row in rows:
        body = json.loads(row["body"])
        assert body["observed_state"] in ("settled", "unsettled")
        if body["observed_state"] == "settled":
            assert body["basis"] == "payment_observation", \
                f"{row['doc_id']} 说到账了，却不是从观察行来的"
            assert row["outcome"] == kb.OUTCOME_SUCCESS
        else:
            assert body["basis"] != "payment_observation"


# ---------------------------------------------------------------------------
# 4. 检索侧：薄封装与租户硬约束
# ---------------------------------------------------------------------------
def test_retrieve_playbook_puts_the_matching_code_first(store):
    """按错误码取处置手册，那条码自己排第一 —— `gateway_code` 是精确通道。"""
    experiment.seed_process_kb(store)
    code = "ACQ.SYSTEM_ERROR"
    hits = retriever.retrieve_playbook(
        store, code, {"tenant_id": TENANT_A, "biz_type": "refund"})
    assert hits, "按码检索一条都没召回"
    assert {h["kind"] for h in hits} == {kb.KIND_ERROR_CODE_PLAYBOOK}, "kinds 收窄没生效"
    assert hits[0]["doc"]["gateway_code"] == code, "命中的第一条不是这个码的手册"
    assert hits[0]["channels"]["gateway_code"] == 1.0, "精确通道没点着"


def test_retrieve_task_patterns_returns_only_patterns(store):
    experiment.seed_process_kb(store)
    hits = retriever.retrieve_task_patterns(
        store, {"tenant_id": TENANT_A, "biz_type": "refund"}, limit=20)
    assert hits, "一条任务拆分模式都没召回"
    assert {h["kind"] for h in hits} == {kb.KIND_TASK_PATTERN}


def test_thin_wrappers_keep_the_tenant_hard_constraint(store):
    """两个薄封装都不许绕开租户硬约束 —— 没有 `tenant_id` 就返回空。

    另写一套「按码查表」的直接 SQL 会绕开这条约束，而绕过去不报错，
    只是某天别家的处置口径出现在了本租户的结果里。
    """
    experiment.seed_process_kb(store)
    assert retriever.retrieve_playbook(store, "ACQ.SYSTEM_ERROR", {"biz_type": "refund"}) == []
    assert retriever.retrieve_task_patterns(store, {"biz_type": "refund"}) == []

    b_docs = {r["doc_id"] for r in kb.query(
        store, "SELECT doc_id FROM kb_doc WHERE tenant_id=?", (TENANT_B,))}
    assert b_docs, "租户 B 一条流程知识都没有，这条测试就没在验跨租户"
    hits = retriever.retrieve_playbook(
        store, "ACQ.SYSTEM_ERROR", {"tenant_id": TENANT_A, "biz_type": "refund"}, limit=50)
    assert not (b_docs & {h["doc_id"] for h in hits}), "跨租户召回 —— 这是事故"


# ---------------------------------------------------------------------------
# 5. 生成器的确定性 —— 产物必须是可复现的
# ---------------------------------------------------------------------------
def test_generator_output_matches_what_is_on_disk():
    """`--check` 通过 = 磁盘上那份就是当前生成器的产物，两次生成逐字节相同。

    走子进程而不是 import 后调 `build()`：验的是「人手里那条命令」的退出码，
    不是某个内部函数的返回值。
    """
    proc = subprocess.run([sys.executable, GENERATOR, "--check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, (
        "语料与生成器不一致，请重跑 python3 scripts/gen_refund_kb.py\n"
        + proc.stdout + proc.stderr)


def test_generator_is_deterministic_in_process():
    """连着生成两次，产物逐字节相同 —— 没有随机数、没有 `now()`、没有集合迭代序。"""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import gen_refund_kb
    finally:
        sys.path.pop(0)
    assert gen_refund_kb.build() == gen_refund_kb.build()
