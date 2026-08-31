"""条款版本锁定与赔款核算 —— 本域的招牌判据。

要买的那句话：

    人家 2023 年投的保，不能拿 2025 年的条款判。

实验设计上有一条讲究：`CL-01` 的 v1 与 v2 **生效区间完全相同，只有版本号不同**。
所以「v2 被排除在外」这件事只可能是保单锁定了 v1，不可能是时间过滤的副产品 ——
把两版设成不同区间会把变量弄混，那样的绿灯证明不了任何事。

第二组断言校的是**裁定产物带得出可核对的规则编号**：`rule_no` 与 `terms_version`
是 `adjudication` 表上的**列**，不是埋在 JSON 里的键 —— 重放校验要能直接 SELECT。
"""

from __future__ import annotations

import json

import pytest

from maos.core.store import SqliteStore
from maos.domain.claim import guard, objects
from maos.skills.builtin.claim import _common as C
from maos.skills.builtin.claim.adjudicate import ClaimAdjudicateSkill
from maos.skills.builtin.claim.settle import ClaimSettleSkill
from maos.skills.contract import SkillContext

TENANT = "tnt-ins"
PAYER = "payer-1"
POLICY = "POL-2023-1"
BOUND_AT = "2023-04-18T00:00:00+00:00"
CLAIM = "clm-1"
PLAN, TASK = "plan-1", "task-1"

#: 两版条款，生效区间**完全相同**。见模块 docstring。
TERMS = [
    ("CL-01", 1, {"coinsurance_rate": 0.9, "deductible": 1000},
     "2020-01-01T00:00:00+00:00", "*", "*"),
    ("CL-01", 2, {"coinsurance_rate": 0.7, "deductible": 3000},
     "2020-01-01T00:00:00+00:00", "*", "*"),
    ("EX-09", 1, {}, "2020-01-01T00:00:00+00:00", "*", "pre_existing"),
]


def _seed(pinned: int = 1, *, loss_type: str = "illness",
          amount: float = 12000.0, sum_insured: float = 100000.0):
    st = SqliteStore()
    st.init_schema()
    objects.ensure_schema(st)
    objects.execute(st, "INSERT OR REPLACE INTO payer (tenant_id, payer_id, kind, name)"
                        " VALUES (?,?,?,?)", (TENANT, PAYER, "insurer", "示例人寿"))
    objects.execute(
        st,
        "INSERT OR REPLACE INTO policy_contract (tenant_id, policy_no, version, product_code,"
        " insured_id, sum_insured, deductible, coinsurance_rate, bound_at,"
        " terms_version_at_bind, payer_id, payload_json, read_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TENANT, POLICY, 1, "MED-INPATIENT", "insured-1", sum_insured, 1000.0, 0.9,
         BOUND_AT, pinned, PAYER, "{}", C.now_iso()))
    for rule_no, version, params, eff_from, product, loss in TERMS:
        objects.execute(
            st,
            "INSERT OR REPLACE INTO policy_terms (tenant_id, rule_no, version, title, body,"
            " effective_from, effective_to, product_scope, loss_scope)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, rule_no, version, f"{rule_no} v{version}",
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             eff_from, None, product, loss))
    guard.create_case(st, tenant_id=TENANT, claim_id=CLAIM, payer_id=PAYER,
                      policy_no=POLICY, policy_version=1, loss_type=loss_type,
                      incident_at="2026-06-20T00:00:00+00:00", amount_claimed=amount,
                      plan_id=PLAN, actor_skill="claim.intake", invocation_id="seed")
    objects.execute(
        st,
        "INSERT OR REPLACE INTO claim_line (tenant_id, claim_id, line_no, item_code,"
        " description, amount_claimed, amount_allowed, carc_code, group_code)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (TENANT, CLAIM, 1, "MED-HOSP", "住院费", amount, 0.0, "", ""))
    return st


def _ctx(st):
    return SkillContext(store=st, extras={"plan_id": PLAN, "task_id": TASK,
                                          "invocation_id": "inv-test"})


def _adjudicate(st, claim_id: str = CLAIM) -> dict:
    return ClaimAdjudicateSkill().run({"tenant_id": TENANT, "claim_id": claim_id}, _ctx(st))


# ------------------------------------------------------------ 条款版本锁定
def test_adjudication_uses_the_version_pinned_at_binding_not_the_latest():
    """论证：保单锁 v1，裁定就按 v1 —— 哪怕表里躺着一个更新的 v2。"""
    st = _seed(pinned=1)
    out = _adjudicate(st)
    assert out["terms_version"] == 1
    assert out["rule_refs"] == ["CL-01@v1"], (
        f"应命中锁定的 CL-01@v1，实际 {out['rule_refs']} —— "
        "命中 v2 就说明用的是 policy_terms 的最新版本，而不是保单锁定的那一版")
    assert out["matched_rules"][0]["params"]["coinsurance_rate"] == 0.9


def test_a_policy_bound_later_gets_the_newer_terms():
    """论证：锁定机制是**真的按保单走**，不是恒返回 v1。

    没有这条，上一条断言用「永远只取最小版本」的实现也能过 —— 那是假绿。
    """
    st = _seed(pinned=2)
    out = _adjudicate(st)
    assert out["terms_version"] == 2
    assert out["rule_refs"] == ["CL-01@v2"]
    assert out["matched_rules"][0]["params"]["coinsurance_rate"] == 0.7


def test_pinned_version_lookup_fails_loudly_without_a_policy_snapshot():
    """论证：没有保单快照就不许猜一个条款版本出来。"""
    st = _seed()
    with pytest.raises(LookupError, match="条款版本无从锁定"):
        objects.pinned_terms_version(st, tenant_id=TENANT, policy_no="POL-NOT-EXIST",
                                     policy_version=1)


# ------------------------------------------------------ 裁定产物的可核对性
def test_adjudication_row_exposes_rule_no_and_terms_version_as_columns():
    """论证：「按哪一条、哪一版判的」是**列**，SELECT 得到，不用解析 JSON。

    埋进 breakdown_json 的话，重放校验就得靠解析字符串才查得到，而那不是一个
    可以被机器逐条对的字段。
    """
    st = _seed()
    _adjudicate(st)
    rows = objects.query(
        st, "SELECT rule_no, terms_version, decision FROM adjudication"
            " WHERE tenant_id=? AND claim_id=?", (TENANT, CLAIM))
    assert len(rows) == 1
    assert rows[0]["rule_no"] == "CL-01"
    assert int(rows[0]["terms_version"]) == 1
    assert rows[0]["decision"] == "approve"


def test_adjudication_advances_biz_status_but_cannot_write_paid():
    """论证：裁定推得动 adjudicated，推不动 paid（守卫拦）。"""
    st = _seed()
    _adjudicate(st)
    assert guard.get_case(st, TENANT, CLAIM)["biz_status"] == "adjudicated"
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(st, TENANT, CLAIM, "paid", "claim.adjudicate", "x")


def test_exclusion_terms_override_covered_terms():
    """论证：判定顺序是**先看除外责任，再看承保条款**。

    反过来的话，一个既命中承保又命中除外的案子会被判成赔付 ——
    除外条款存在的全部意义就是压过承保条款。
    """
    st = _seed(loss_type="pre_existing")
    out = _adjudicate(st)
    assert out["decision"] == "reject"
    assert out["exclusions"] == ["EX-09@v1"]
    assert guard.get_case(st, TENANT, CLAIM)["biz_status"] == "rejected"


def test_no_matching_terms_still_leaves_a_checkable_rule_no():
    """论证：一条条款都没命中时也要留一个查得到的 rule_no。

    空串会让 `adjudication` 的主键退化成 (tenant, claim, '', v)，看起来像有裁定，
    实际上指不到任何一条条款。
    """
    st = _seed()
    out = ClaimAdjudicateSkill().run(
        {"tenant_id": TENANT, "claim_id": CLAIM, "rule_prefix": "ZZ-"}, _ctx(st))
    assert out["decision"] == "reject"
    assert out["primary_rule"] == "ZZ-NONE"
    rows = objects.query(st, "SELECT rule_no FROM adjudication WHERE claim_id=?", (CLAIM,))
    assert rows[0]["rule_no"] == "ZZ-NONE"


# ------------------------------------------------------------------ 赔款核算
def test_settlement_applies_deductible_then_ratio_then_cap():
    """论证：三层扣减顺序正确 —— (12000 - 1000) x 0.9 = 9900.00。

    顺序写反（先乘比例再扣起付线）会得到 12000 x 0.9 - 1000 = 9800.00，
    不报错、只是每一笔都少算一截。这条断言就是那个差的哨兵。
    """
    st = _seed()
    adj = _adjudicate(st)
    out = ClaimSettleSkill().run(
        {"tenant_id": TENANT, "claim_id": CLAIM, "adjudication": adj}, _ctx(st))
    assert out["allowed_amount"] == "9900.00", (
        f"实际 {out['allowed_amount']}；9800.00 说明先乘比例再扣起付线，顺序反了")
    assert out["breakdown"]["primary_rule"] == "CL-01"
    assert out["breakdown"]["terms_version"] == 1


def test_settlement_under_the_latest_terms_would_differ():
    """论证：版本锁定不是摆设 —— 换一版条款算出来是另一个数。

    这条把「锁定」从一句话变成一个可以核的数字：9900.00 vs 6300.00，差 3600。
    """
    st = _seed(pinned=2)
    adj = _adjudicate(st)
    out = ClaimSettleSkill().run(
        {"tenant_id": TENANT, "claim_id": CLAIM, "adjudication": adj}, _ctx(st))
    assert out["allowed_amount"] == "6300.00"


def test_settlement_is_capped_by_sum_insured():
    """论证：保额**最后**封顶，且封顶这件事写在 breakdown 里而不是悄悄发生。"""
    st = _seed(sum_insured=5000.0)
    adj = _adjudicate(st)
    out = ClaimSettleSkill().run(
        {"tenant_id": TENANT, "claim_id": CLAIM, "adjudication": adj}, _ctx(st))
    assert out["allowed_amount"] == "5000.00"
    assert out["breakdown"]["capped_by_sum_insured"] is True


def test_settlement_refuses_when_adjudication_rejected():
    """论证：裁定不通过就不核算，**不给一个 0 元的结果**。

    0 元结果会让下游误以为「核算过了，只是金额为零」，而事实是这笔根本不该进赔付环节。
    """
    st = _seed(loss_type="pre_existing")
    adj = _adjudicate(st)
    with pytest.raises(ValueError, match="不予核算"):
        ClaimSettleSkill().run(
            {"tenant_id": TENANT, "claim_id": CLAIM, "adjudication": adj}, _ctx(st))


def test_line_allocation_sums_to_the_total():
    """论证：逐行分摊之和恰好等于总额，分位余数落在最后一行。

    逐行独立四舍五入会让各行之和与总额差出几分钱 —— 理赔对账最先发现的就是这几分钱。
    """
    st = _seed(amount=10000.0)
    # 三行 3333.33 / 3333.33 / 3333.34 这类除不尽的分法才校得出余数逻辑。
    objects.execute(st, "DELETE FROM claim_line WHERE claim_id=?", (CLAIM,))
    for i, amt in enumerate((3333.34, 3333.33, 3333.33), start=1):
        objects.execute(
            st,
            "INSERT INTO claim_line (tenant_id, claim_id, line_no, item_code, description,"
            " amount_claimed, amount_allowed, carc_code, group_code)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (TENANT, CLAIM, i, f"IT-{i}", "", amt, 0.0, "", ""))
    adj = _adjudicate(st)
    out = ClaimSettleSkill().run(
        {"tenant_id": TENANT, "claim_id": CLAIM, "adjudication": adj}, _ctx(st))
    rows = objects.query(
        st, "SELECT amount_allowed FROM claim_line WHERE claim_id=?", (CLAIM,))
    total = sum(round(float(r["amount_allowed"]), 2) for r in rows)
    assert f"{total:.2f}" == out["allowed_amount"]


# ------------------------------------------------------------ 人工审批阈值
def test_threshold_is_a_real_criterion_not_decoration():
    """论证：「超阈值才停下来等人批」是可单独校验的判据。"""
    assert C.needs_human_approval(C.approval_threshold() + 0.01) is True
    assert C.needs_human_approval(C.approval_threshold()) is False
    assert C.needs_human_approval(0) is False


def test_unparseable_amount_is_fail_closed():
    """论证：金额读不出数 = 要人批，不是放过。

    一个 None 说明上游把金额弄丢了，那种案子更该让人看一眼。
    """
    assert C.needs_human_approval(None) is True
    assert C.needs_human_approval("一万二") is True


def test_threshold_reads_env_at_call_time(monkeypatch):
    """论证：阈值**现读**环境变量，改一次不必重启进程。"""
    monkeypatch.setenv(C.ENV_APPROVAL_THRESHOLD, "20000")
    assert C.approval_threshold() == 20000.0
    assert C.needs_human_approval(12000.0) is False
    monkeypatch.setenv(C.ENV_APPROVAL_THRESHOLD, "不是数")
    assert C.approval_threshold() == C.DEFAULT_APPROVAL_THRESHOLD, (
        "解析不出就回落缺省（更严的一侧），不抛 —— 这个函数挂在派单路径上")
