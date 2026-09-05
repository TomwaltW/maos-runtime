"""场景 11 端到端 —— 跨域协同要买的四件东西。

前面十个场景每一个只碰**一个**业务域，证明的是「同一套内核复制着跑了四遍」。
本场景是唯一一条跨两个域的业务链，所以用例钉的不是「它跑通了」，而是：

1. **两个域的业务对象挂在同一个 Plan 上**，各自的主键都能回查到
   （`test_both_domains_hang_on_one_plan` / `test_business_objects_resolve_from_one_plan`）。
2. **权威边界没有因为跨域而松一格**：`settled` 只有 `ap.observe` 写得进，
   `returned` 只有 `investigation.observe` 写得进，越权写入不静默失败
   （`test_returned_is_still_written_only_by_investigation_observe` 等三条）。
3. **翻译层用的是 ap 观察到的那一版**，不是重新去查清算方
   （`test_handoff_reads_the_observed_version`）。
4. **两个域互不认识**，这条设计判断写成了机器判据
   （`test_ap_domain_never_mentions_investigation` / 反向那条）。

外加一条本场景独有的：**没有人工申报就没有翻译**（`test_no_filing_no_handoff`）——
「这笔重复了」的权威在供应商，不在 MAOS（铁律 8）。

用例对**库里的行**下断言，不只看退出码 —— 退出码是场景自己的断言给的，
拿它当判据等于让被测者给自己判分。场景的 `drive()` 只跑不断言正是为此拆出来的。
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.domain.ap import guard as ap_guard
from maos.domain.ap import objects as ap_objects
from maos.domain.investigation import guard as inv_guard
from maos.domain.investigation import objects as inv_objects
from maos.flows import scenario_11 as s11

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def crossed():
    """整条跨域链路跑一次。`drive()` 只跑不断言，断言全在本文件与 `run()` 里。"""
    return s11.drive()


# ------------------------------------------------ 1. 一个 Plan，两个业务域
def test_both_domains_hang_on_one_plan(crossed):
    """两个域的业务对象挂在**同一个** plan_id 上 —— 这是本场景的题眼。

    挂在两个 Plan 上也能把流程跑完，但那证明的是「同一套内核跑了两遍」，
    不是「编排内核协调了一条跨域业务链」。
    """
    store, plan_id = crossed["store"], crossed["plan_id"]
    ap_case = ap_guard.get_case(store, s11.TENANT_ID, s11.CASE_AP)
    inv_case = inv_guard.get_case(store, s11.TENANT_ID, s11.CASE_INV)

    assert ap_case is not None and inv_case is not None
    assert ap_case["plan_id"] == inv_case["plan_id"] == plan_id


def test_business_objects_resolve_from_one_plan(crossed):
    """从 plan_id 出发，两个域的 case 各自按自己的主键回查得到。

    两张表的主键列名不同（`ap_case.case_id` / `investigation_case.case_id` 同名，
    但表名与租户口径各归各域），跨域没有把它们合并成一张公共表 —— 合并会让
    `resolve` 的分派表变成两个域都要改的公共面（`ap/schema.sql` 抬头那句）。
    """
    store, plan_id = crossed["store"], crossed["plan_id"]
    ap_rows = ap_objects.query(
        store, "SELECT case_id, biz_status FROM ap_case WHERE plan_id=?", (plan_id,))
    inv_rows = inv_objects.query(
        store, "SELECT case_id, biz_status FROM investigation_case WHERE plan_id=?",
        (plan_id,))

    assert [r["case_id"] for r in ap_rows] == [s11.CASE_AP]
    assert [r["case_id"] for r in inv_rows] == [s11.CASE_INV]
    assert ap_rows[0]["biz_status"] == "settled"
    assert inv_rows[0]["biz_status"] == "returned"


def test_one_plan_carries_both_domains_tasks(crossed):
    """八个任务、两个域的 role，全在一个 Plan 里由同一个 WorkerRuntime 按 role 派单。"""
    store, plan_id = crossed["store"], crossed["plan_id"]
    tasks = {t["task_id"]: t for t in store.list_tasks(plan_id)}

    assert set(tasks) == set(s11.AP_TASKS) | set(s11.INV_TASKS)
    assert all(t["state"] == TaskState.DONE for t in tasks.values()), (
        f"整条链路应全部收口，实际 "
        f"{ {k: v['state'] for k, v in tasks.items() if v['state'] != TaskState.DONE} }")
    ap_roles = {tasks[t]["role"] for t in s11.AP_TASKS}
    inv_roles = {tasks[t]["role"] for t in s11.INV_TASKS}
    assert not (ap_roles & inv_roles), (
        f"两个域的 role 不该有交集，实际 {sorted(ap_roles & inv_roles)}")


# ---------------------------------------- 2. 权威边界没有因为跨域而松一格
def test_returned_is_still_written_only_by_investigation_observe(crossed):
    """`returned` 只有 `investigation.observe` 写得进，且只有拿到 pacs.004 才写得进。"""
    store, plan_id = crossed["store"], crossed["plan_id"]
    returned = inv_guard.observations_of(store, s11.TENANT_ID, s11.CASE_INV,
                                         observed_state=inv_guard.OBS_RETURNED)

    assert len(returned) == 1
    assert returned[0]["message_type"].startswith(inv_guard.MSG_PAYMENT_RETURN)
    assert returned[0]["returned_amount"] is not None
    assert returned[0]["return_reason_code"]

    # actor 锚点必须指回一次 investigation.observe 调用 —— 跨域之后审计链不许断。
    observers = {
        (e["detail"] or {}).get("invocation_id")
        for e in store.list_event_log(plan_id)
        if e["event_type"] == "SkillInvoked"
        and (e["detail"] or {}).get("skill") == inv_guard.AUTHORITATIVE_WRITER
    }
    assert returned[0]["actor_invocation_id"] in observers, (
        "returned 的观察必须追得到一次 investigation.observe 调用，"
        "否则权威事实边界被绕过了")


def test_settled_is_still_written_only_by_ap_observe(crossed):
    """`settled` 那一侧同样没松：一条带流水号的观察兜底，actor 指回 ap.observe。"""
    store, plan_id = crossed["store"], crossed["plan_id"]
    obs = ap_guard.observations_of(store, s11.TENANT_ID, s11.CASE_AP)

    assert len(obs) == 1 and obs[0]["observed_state"] == "settled"
    assert obs[0]["bank_reference"], "没有流水号的「已付」在财务上对不了账"
    observers = {
        (e["detail"] or {}).get("invocation_id")
        for e in store.list_event_log(plan_id)
        if e["event_type"] == "SkillInvoked"
        and (e["detail"] or {}).get("skill") == ap_guard.AUTHORITATIVE_WRITER
    }
    assert obs[0]["actor_invocation_id"] in observers


def test_cancellation_confirmed_wrote_nothing(crossed):
    """中间那句 CNCL（清算方说撤销成功）**一个状态都没推**。

    跨域链路上最容易被顺手兜底的一处：ap 那边已经 settled 了，看到清算方确认撤销，
    很容易就把差错案收口成功。CNCL 证明的是「撤销指令照办了」，不是「钱回来了」。
    """
    store = crossed["store"]
    confirmed = inv_guard.observations_of(
        store, s11.TENANT_ID, s11.CASE_INV,
        observed_state=inv_guard.OBS_CANCELLATION_CONFIRMED)

    assert confirmed, "顺利路径也必须经过一次 CNCL，否则这条判据一次都没被触发过"
    assert all(o["confirmation_code"] == "CNCL" for o in confirmed)
    assert all(o["returned_amount"] is None for o in confirmed), (
        "camt.029 观察不许带退回金额 —— 带了就让「有金额」不再是资金证据的标志")
    # 它出现在 returned 之前：先确认撤销、后拿到退款报文，时序不能倒。
    seqs = {o["observed_state"]: o["poll_seq"]
            for o in inv_guard.observations_of(store, s11.TENANT_ID, s11.CASE_INV)}
    assert seqs[inv_guard.OBS_CANCELLATION_CONFIRMED] < seqs[inv_guard.OBS_RETURNED]


def test_cross_domain_added_no_task_state_and_no_transition(crossed):
    """铁律 9：跨域不是新的 Task 状态，也没有为它新开一条迁移。"""
    store, plan_id = crossed["store"], crossed["plan_id"]
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    states = {t["state"] for t in store.list_tasks(plan_id)}

    assert states <= known
    for biz in (*ap_guard.BIZ_STATUS_FLOW, *inv_guard.BIZ_STATUS_FLOW):
        assert biz not in states, f"{biz} 是业务对象自己的字段，不许变成 Task 状态"
    moves = {(e["from_state"], e["to_state"]) for e in store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS)


# ------------------------------------ 3. 翻译层读的是 ap 观察到的那一版
def test_handoff_reads_the_observed_version(crossed):
    """快照的每一格都能指回库里**已经落下的那一行**，不是重新问来的。

    三个锚点逐个回查：报文号回 `ap_payment_observation`、端到端号与金额回
    `payment_instruction`。任何一格对不上，都说明翻译层要么另问了一次外部系统、
    要么自己造了一个值。
    """
    store = crossed["store"]
    observed = ap_guard.observations_of(store, s11.TENANT_ID, s11.CASE_AP)[0]
    instruction = ap_objects.query(
        store, "SELECT * FROM payment_instruction WHERE tenant_id=? AND case_id=?",
        (s11.TENANT_ID, s11.CASE_AP))[0]
    snapshot = inv_objects.get_payment_snapshot(
        store, tenant_id=s11.TENANT_ID, original_msg_id=observed["bank_reference"],
        version=1)

    assert snapshot is not None
    assert snapshot["original_msg_id"] == observed["bank_reference"]
    assert snapshot["value_date"] == observed["value_date"]
    assert snapshot["end_to_end_id"] == instruction["instruction_id"]
    assert snapshot["debtor_agent"] == instruction["bank"]
    assert str(ap_objects.money(snapshot["interbank_amount"])) == \
        str(ap_objects.money(instruction["amount"]))
    assert snapshot["currency"] == instruction["currency"]

    # 审计链跨域之后仍然接得上：快照记着是哪一次 ap.observe 观察到这笔付款的。
    payload = inv_objects.query(
        store, "SELECT payload_json FROM original_payment_snapshot"
               " WHERE tenant_id=? AND original_msg_id=?",
        (s11.TENANT_ID, observed["bank_reference"]))[0]["payload_json"]
    assert observed["actor_invocation_id"] in payload


def test_investigation_case_takes_amount_from_the_snapshot(crossed):
    """差错案的金额币种取自快照，不是任务入参自称的。

    `investigation.file` 会拿 `claimed_amount`（三单算出的应付）与快照对账，
    对不上当场抛 —— 这条用例钉的是「对上了之后写进案子的是快照那一份」。
    """
    store = crossed["store"]
    case = inv_guard.get_case(store, s11.TENANT_ID, s11.CASE_INV)
    snapshot = inv_objects.get_payment_snapshot(
        store, tenant_id=s11.TENANT_ID,
        original_msg_id=crossed["advice"]["bank_reference"], version=1)

    assert case["amount"] == snapshot["interbank_amount"]
    assert case["currency"] == snapshot["currency"]
    assert case["end_to_end_id"] == snapshot["end_to_end_id"]
    # 收付款行也来自快照：DAG 的 inputs 里压根没给这两个字段。
    assert case["creator_agent"] == snapshot["debtor_agent"]
    assert case["assignee_agent"] == snapshot["creditor_agent"]


def test_handoff_artifact_names_every_field_source(crossed):
    """交接单把字段对照表原样落进产物 —— 「这个报文号哪来的」答案在库里，不在记忆里。"""
    store, plan_id = crossed["store"], crossed["plan_id"]
    arts = [a for a in store.list_artifacts(s11.TASK_AP_PAY)
            if a["kind"] == s11.KIND_HANDOFF]
    assert len(arts) == 1, f"跨域交接单应恰好一份，实际 {len(arts)} 份"
    content = arts[0]["content"]

    mapped = {m["to"]: m["from"] for m in content["field_mapping"]}
    assert mapped["original_msg_id"] == "ap_payment_observation.bank_reference"
    assert mapped["end_to_end_id"] == "payment_instruction.instruction_id"
    assert mapped["value_date"] == "ap_payment_observation.value_date"
    assert content["filing"]["filed_by"] == s11.APPROVER

    # 旁路入库的产物必须自报来源：审计链指得到是哪一步产的（ArtifactSeeded）。
    seeded = [e for e in store.list_event_log(plan_id)
              if e["event_type"] == "ArtifactSeeded"
              and (e["detail"] or {}).get("artifact_id") == arts[0]["artifact_id"]]
    assert len(seeded) == 1
    detail = seeded[0]["detail"]
    assert detail["source"] == "maos.flows.scenario_11.handoff_to_investigation"
    assert detail["from_domain"] == "ap" and detail["to_domain"] == "investigation"


# ------------------------------------------- 触发点：人不发起就没有差错处理
def test_no_filing_no_handoff(crossed):
    """没有人工申报，翻译层当场抛，快照一行都不写。

    这条是「触发点是人发起」的机器判据。ap 域没有重复发票检测器，本场景也没给它加
    ——「这张发票重复了」的权威在财务与供应商（铁律 8）。兜底成「没申报也翻译一份」
    等于让系统自己发起了一件它无权发起的事。
    """
    store, plan_id = crossed["store"], crossed["plan_id"]
    before = inv_objects.query(
        store, "SELECT COUNT(*) AS n FROM original_payment_snapshot", ())[0]["n"]

    for bad in (None, "", 0, s11.DisputeFiling(filed_by="x", claimed_by="y",
                                               claim="   ", filed_at="z")):
        with pytest.raises(ValueError, match="人工差错申报"):
            s11.handoff_to_investigation(store, plan_id=plan_id, trace_id="t",
                                         filing=bad)

    after = inv_objects.query(
        store, "SELECT COUNT(*) AS n FROM original_payment_snapshot", ())[0]["n"]
    assert after == before, "申报缺失时一行快照都不该写"


def test_filing_is_frozen(crossed):
    """申报一旦签出就不许在链路上被改写 —— 改一个字等于伪造一份人的输入。"""
    with pytest.raises(Exception):
        s11.DEMO_FILING.claim = "改一个字试试"
    # 交接单里留的是申报原文，与常量逐字相同。
    assert crossed["handoff"]["filing"]["claim"] == s11.DEMO_FILING.claim


# ------------------------------- 4. 两个域互不认识（设计判断写成机器判据）
def _grep(pattern: str, *paths: str) -> list[str]:
    """在仓库里跑一次真的 grep，返回命中行。`__pycache__` 由 `--include` 排除。"""
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", pattern, *paths],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def test_ap_domain_never_mentions_investigation():
    """`maos/domain/ap/**` 里不许出现 investigation 的任何符号。

    这条塌了，两个域就不再是「互相不认识」，跨域协同的含金量随之归零 ——
    那时演的是「一个域顺手调了另一个域」，不是「编排层把两个不认识的域接起来」。
    """
    hits = _grep("investigation", "maos/domain/ap/")
    assert hits == [], "maos/domain/ap/ 出现了 investigation：\n" + "\n".join(hits)


def test_investigation_domain_never_imports_ap():
    """反向同理：`maos/domain/investigation/**` 不许 import ap 域。"""
    hits = _grep(r"from maos\.domain\.ap\|import maos\.domain\.ap",
                 "maos/domain/investigation/")
    assert hits == [], ("maos/domain/investigation/ 出现了 ap 域 import：\n"
                        + "\n".join(hits))


def test_translation_lives_in_the_flow_layer():
    """翻译只此一处：跨域的两个域名同时出现在 `maos/` 下的哪些文件里。

    允许名单只有编排层（`flows/scenario_11.py`）与本用例文件本身。多出任何一个，
    都说明有人把翻译挪进了域里、或者又抄了一份 —— 两份翻译一定会漂。
    """
    hits = _grep("original_payment_snapshot", "maos/")
    files = {ln.split(":", 1)[0] for ln in hits}
    allowed = {
        "maos/flows/scenario_9.py",          # investigation 域自己的场景，域内使用
        "maos/flows/scenario_11.py",         # 跨域翻译层
        "maos/domain/investigation/objects.py",
        "maos/domain/investigation/schema.sql",
        "maos/skills/builtin/investigation/intake.py",
        "maos/tests/test_cross_domain_flow.py",
    }
    extra = {f for f in files if f not in allowed and not f.startswith("maos/tests/")}
    assert not extra, f"原始支付快照被这些文件碰到了，翻译层可能被复制了一份：{sorted(extra)}"

    # ap 域一侧同样只此一处：读 ap 的付款指令表的非 ap 文件只有编排层。
    ap_hits = _grep("payment_instruction", "maos/flows/")
    ap_files = {ln.split(":", 1)[0] for ln in ap_hits}
    assert ap_files <= {"maos/flows/scenario_10.py", "maos/flows/scenario_11.py"}, (
        f"flows 里读 payment_instruction 的文件多了：{sorted(ap_files)}")


# --------------------------------------------------------------- 收口与接线
def test_plan_converges_to_done(crossed):
    plan = crossed["cp"].store.get_plan(crossed["plan_id"])
    assert plan["state"] == PlanState.DONE


def test_scenario_11_is_reachable_but_not_in_default_sequence():
    """`--scenario 11` 可达，缺省序列一个字不动。

    `DEFAULT_SCENARIOS`（1-7 + R5）是跨轨冻结口径：改它会打破缺省八束证据、
    `demo_preflight.sh` 与复赛材料里写死的场景清单。
    """
    from maos.main import ALL_SCENARIOS, DEFAULT_SCENARIOS

    assert 11 in ALL_SCENARIOS
    assert DEFAULT_SCENARIOS == (1, 2, 3, 4, 5, 6, 7)
    assert 11 not in DEFAULT_SCENARIOS


def test_dag_does_not_hardcode_the_bank_reference():
    """DAG 建成时**不许**写死原报文号 —— 它是运行时才知道的银行流水号。

    写死等于假装编排层提前知道银行会给出什么流水号，那条链路就成了摆拍。
    """
    inv_file = next(t for t in s11._tasks() if t["task_id"] == s11.TASK_INV_FILE)
    assert "original_msg_id" not in inv_file["inputs"]
    assert "claimed_amount" not in inv_file["inputs"]

    src = (REPO_ROOT / "maos" / "flows" / "scenario_11.py").read_text(encoding="utf-8")
    assert not re.search(r'["\']bkref-[0-9a-f]', src), (
        "场景里出现了写死的银行流水号 —— 它由 uuid 派生，写死的那个必然是假的")


# ------------------------------- 5. 核验器：一个 Plan 挂两个域时的行为
#
# `maos/domain/__init__.py` 的 `DOMAIN_REGISTRY` 是**一个域一份 spec**，而
# `scripts/verify.py` 的三个域级判据都按「该域的 case 表在不在这个库里」选核验对象
# —— 于是一个 plan 同时挂两个域的业务对象时，它被**两个域各核验一次**。
#
# 本节把这个行为钉住。它没有要求改 verify.py（实测四域旧结果逐字节不变，见
# docs/DECISIONS.md 对应那一行），但**它是一条容易被"优化"掉的行为**：给
# `check_business_outcome` 加一句「找到第一个匹配的域就 break」，跨域那一束会从
# 两次核验退化成一次，而屏幕上仍然是 PASS —— 少核验一遍不会变红，只会变松。
#: 两个域各一条权威回执。字段名与取值照各域守卫的判据给全，**表名不写在这里** ——
#: 它由 `DOMAIN_REGISTRY` 提供（见 `_dual_domain_case`）：这个夹具核验的正是注册表
#: 驱动的那几条判据，在夹具里抄一份表名就等于给它们造了第二个事实源。
#: 顺带也就不会撞上 `test_ap_guard.py` 那条「全仓只有守卫写得出 ap_case」的自查。
DUAL_RECEIPTS = {
    "ap": ("ap-1", "settled", "inv-ap", {
        "instruction_id": "ins-1", "observed_state": "settled",
        "bank_reference": "bkref-1", "value_date": "2026-09-05",
        "actor_invocation_id": "inv-ap"}),
    "investigation": ("iv-1", "returned", "inv-iv", {
        "request_id": "req-1", "poll_seq": 3, "message_type": "pacs.004.001.09",
        "confirmation_code": "", "rejection_code": "", "return_reason_code": "CUST",
        "returned_amount": 100.0, "observed_state": "returned",
        "actor_invocation_id": "inv-iv"}),
}


def _sql_type(value) -> str:
    return {float: "REAL", int: "INTEGER"}.get(type(value), "TEXT")


def _dual_domain_case():
    """造一个「一个 Plan、两个域的业务对象」的证据库，形状同 scenario-11 的那一束。"""
    import sqlite3

    from maos.domain import DOMAIN_REGISTRY
    from scripts import verify

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE plan (plan_id TEXT, state TEXT)")
    conn.execute("INSERT INTO plan VALUES ('plan-x11', 'DONE')")
    conn.execute("CREATE TABLE event_log (detail TEXT, event_type TEXT)")

    evidence = []
    for name, (case_id, status, actor, receipt) in DUAL_RECEIPTS.items():
        spec = DOMAIN_REGISTRY[name]
        key = spec.case_id_column
        conn.execute("INSERT INTO event_log VALUES (?, 'SkillInvoked')",
                     (json.dumps({"skill": spec.authoritative_writer,
                                  "invocation_id": actor}),))
        conn.execute(f"CREATE TABLE {spec.case_table} (tenant_id TEXT, {key} TEXT,"
                     f" biz_status TEXT, plan_id TEXT)")
        conn.execute(f"INSERT INTO {spec.case_table} VALUES (?,?,?,?)",
                     ("t-x", case_id, status, "plan-x11"))
        cols = ", ".join(f"{k} {_sql_type(v)}" for k, v in receipt.items())
        conn.execute(f"CREATE TABLE {spec.observation_table} (tenant_id TEXT,"
                     f" {key} TEXT, {cols})")
        marks = ",".join("?" for _ in range(len(receipt) + 2))
        conn.execute(f"INSERT INTO {spec.observation_table} VALUES ({marks})",
                     ("t-x", case_id, *receipt.values()))
        evidence.append({"kind": spec.observation_table,
                         "provenance": spec.observation_table,
                         "tenant_id": "t-x", key: case_id,
                         **{f: receipt[f] for f in spec.observation_fields}})

    case = verify.Case(
        name="scenario-11", directory="", db_path="", conn=conn,
        tables=verify.table_names(conn), trace={},
        result={"plans": [{"plan_id": "plan-x11", "state": "DONE", "business_outcome": {
            "status": "succeeded", "basis": "external_evidence", "plan_state": "DONE",
            "source": verify.OUTCOME_SOURCE, "unaudited_evidence_count": 0,
            "external_evidence": evidence}}]})
    return case, evidence


def test_one_plan_two_domains_is_verified_once_per_domain():
    """两个域各核验一次，谁也没被跳过 —— 少核验一遍不会变红，只会变松。"""
    from maos.domain import DOMAIN_REGISTRY
    from scripts import verify

    case, _ = _dual_domain_case()
    try:
        for name in ("ap", "investigation"):
            spec = DOMAIN_REGISTRY[name]
            auth = verify.check_authoritative_fact([case], domain=spec)
            outcome = verify.check_business_outcome([case], domain=spec)
            assert (auth.status, auth.passed, auth.total) == (verify.PASS, 1, 1), \
                f"{name} 权威判据应恰好核验一次：{auth.notes}"
            assert (outcome.status, outcome.passed, outcome.total) == (verify.PASS, 1, 1), \
                f"{name} 结果判据应恰好核验这一个 plan：{outcome.notes}"

        # 不按域展开时，同一个库里两个域的权威终态各算一次 —— 合起来是 2。
        both = verify.check_authoritative_fact([case])
        assert (both.status, both.passed, both.total) == (verify.PASS, 2, 2), both.notes
    finally:
        case.conn.close()


def test_one_plan_two_domains_fails_loud_when_either_side_is_forged():
    """任一域的判据被改坏，**两个域**的结果核验都判负。

    这是「一个 plan 两个域」在核验器里更严的那一面，不是串味：这个 plan 的
    `business_outcome` 是一份，里面同时装着两个域的判据，任何一条回查不到，
    这份结论就不成立 —— 无论从哪个域看过去。
    """
    from maos.domain import DOMAIN_REGISTRY
    from scripts import verify

    spec = DOMAIN_REGISTRY["investigation"]
    case, evidence = _dual_domain_case()
    try:
        # 反例与手工注入那次一致：把资金证据换成一句肯定答复（CNCL 不是 pacs.004）。
        # 库与 result.json 两侧同改 —— 只改一侧命中的是别的判据（第 4 项重放不一致），
        # 而这条要试的是「守卫判据本身有没有牙齿」。
        case.conn.execute(
            f"UPDATE {spec.observation_table} SET message_type='camt.029.001.08'")
        forged = next(e for e in evidence if e["kind"] == spec.observation_table)
        forged["message_type"] = "camt.029.001.08"

        assert verify.check_authoritative_fact(
            [case], domain=DOMAIN_REGISTRY["investigation"]).status == verify.FAIL
        for name in ("ap", "investigation"):
            result = verify.check_business_outcome([case],
                                                   domain=DOMAIN_REGISTRY[name])
            assert result.status == verify.FAIL, (
                f"{name} 侧也应判负：跨域那份 business_outcome 是一份，"
                f"坏了一条判据整份就不成立")
    finally:
        case.conn.close()


