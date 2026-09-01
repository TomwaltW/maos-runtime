"""三单匹配的判据 —— 每一条拒付理由都挂一个**真实存在**的规则编号。

本文件守的是本域最贵的那个判断题：这张发票该不该付。判错的两个方向都很贵，
所以判据必须可核对。三组用例：

  1. **容差**（数量 / 单价 / 税额）：各自量纲不同，不许合并成一个。
     每一项都测「容差内放过」与「容差外拒付」两侧 —— 只测一侧的话，
     把容差调成无穷大也能全绿。
  2. **勾稽**（BR-CO-10/13/15/16/17、PEPPOL-EN16931-R120）：零容差，
     对不上就是发票自己算错了。
  3. **码表**（BR-CL-01 / BR-CL-17）与**引用**（PEPPOL-EN16931-R003）。

外加一条贯穿全部的：`test_every_finding_is_verifiable` —— 不管哪条判据命中，
产出的 finding 必须带得起「拿编号去查规范能查到」这句话。
"""

from __future__ import annotations

import json

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.ap import fixtures, guard, objects
from maos.model.client import Tier
from maos.skills.builtin.ap import AP_SKILLS
from maos.skills.builtin.ap import match as match_mod
from maos.skills.invoker import SkillInvoker
from maos.tools import ap_codes

TEN = "tnt-ap-match"
CASE, PO, GR, INV = "case-m", "PO-M", "GR-M", "INV-M"
SUP = "SUP-M"

#: 一套完全对得上的三单。两行、13% 标准税率。
#: (line_no, sku, 订单数, 订单单价, 收货到货, 收货不合格, 发票数, 发票单价)
CLEAN_LINES = [
    (1, "SKU-A", 100.0, "12.00", 100.0, 0.0, 100.0, "12.00"),
    (2, "SKU-B", 50.0, "7.50", 50.0, 0.0, 50.0, "7.50"),
]

IDENTITY = AgentIdentity(
    agent_id="match-test", role="match-test", duty="用例专用",
    allowed_skills=frozenset(AP_SKILLS), write_scope=frozenset({"artifact"}),
    max_risk="H", model_tier=Tier.LIGHT)


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    fixtures.seed_supplier(s, tenant_id=TEN, supplier_id=SUP, name="用例供应商",
                           payment_means_code=ap_codes.CODE_CREDIT_TRANSFER)
    return s


def seed(store, *, lines=None, header_overrides=None, invoice_type=None,
         tax_category=None):
    return fixtures.seed_three_way(
        store, tenant_id=TEN, supplier_id=SUP, po_id=PO, gr_id=GR, invoice_id=INV,
        lines=lines if lines is not None else CLEAN_LINES,
        tax_category=tax_category or ap_codes.CODE_TAX_STANDARD, tax_rate=13.0,
        invoice_type=invoice_type or ap_codes.CODE_COMMERCIAL_INVOICE,
        issued_at="2026-08-01T00:00:00+00:00", due_at="2026-09-01",
        header_overrides=header_overrides)


def run_match(store, *, tolerance=None, attempt=1) -> dict:
    """收票 + 匹配，返回匹配 skill 的 output。两步都经 invoker，留审计行。"""
    inv = SkillInvoker(IDENTITY, store)
    res = inv.invoke("ap.intake", {
        "tenant_id": TEN, "case_id": CASE, "invoice_id": INV, "po_id": PO,
        "po_version": 1, "gr_id": GR}, extras={"plan_id": "p", "task_id": "t"})
    assert res.status == "ok", res.error
    res = inv.invoke("ap.match", {
        "tenant_id": TEN, "case_id": CASE, "attempt": attempt,
        "tolerance": tolerance}, extras={"plan_id": "p", "task_id": "t"})
    assert res.status == "ok", (
        f"匹配**不通过**也必须 status=ok —— 那是结论不是失败：{res.error}")
    return res.output


def rule_ids(out: dict) -> list[str]:
    return sorted(f["rule_id"] for f in out["findings"])


# ------------------------------------------------------------------ 全对得上
def test_clean_three_way_matches_and_computes_the_payable(store):
    """三单对得上：匹配通过、业务状态推到 matched、应付按 BR-CO-16 算出。"""
    header = seed(store)
    out = run_match(store)
    assert out["matched"] is True, f"不该有拒付理由，实际 {rule_ids(out)}"
    assert out["findings"] == []
    assert out["payable_amount"] == header["amount_due"]
    assert out["biz_status"] == "matched"
    # 判据跑了多少条要记下来 —— 「没判的」和「判过了没命中」必须分得开。
    assert len(out["checked"]) >= 11
    assert set(out["checked"]) <= set(ap_codes.RULES), "checked 里出现了表外编号"


def test_match_result_is_persisted_even_when_it_fails(store):
    """通过与否都落 `match_result` —— 「第一次为什么没过」要在库里查得到。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 90.0, 0.0, 100.0, "12.00")])
    out = run_match(store, attempt=1)
    assert out["matched"] is False
    rows = objects.query(store, "SELECT * FROM match_result WHERE tenant_id=? AND"
                                " case_id=? ORDER BY attempt", (TEN, CASE))
    assert len(rows) == 1 and rows[0]["matched"] == 0
    stored = json.loads(rows[0]["findings_json"])
    assert [f["rule_id"] for f in stored] == [f["rule_id"] for f in out["findings"]]
    assert json.loads(rows[0]["tolerance_json"])["quantity"] == "0.5"


def test_failed_match_does_not_advance_biz_status(store):
    """匹配不过时业务状态一动不动 —— 也不抛异常。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 90.0, 0.0, 100.0, "12.00")])
    out = run_match(store)
    assert out["biz_status"] == "received"
    assert guard.get_case(store, TEN, CASE)["biz_status"] == "received"
    assert out["payable_amount"] == "", "没通过就不该算出应付额"


# ------------------------------------------------------------------ 容差三项
def test_quantity_within_tolerance_passes(store):
    """收货合格数比发票少 0.4 件，数量容差 0.5 —— 放过。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 99.6, 0.0, 100.0, "12.00")])
    out = run_match(store)
    assert out["matched"] is True, f"0.4 件的差在容差内，不该拒付：{rule_ids(out)}"


def test_quantity_beyond_tolerance_is_rejected_with_a_real_rule(store):
    """收货合格数少 10 件 —— 拒付，理由挂 `RULE_INVOICED_QUANTITY`。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 90.0, 0.0, 100.0, "12.00")])
    out = run_match(store)
    assert out["matched"] is False
    assert ap_codes.RULE_INVOICED_QUANTITY in rule_ids(out)
    f = next(f for f in out["findings"]
             if f["rule_id"] == ap_codes.RULE_INVOICED_QUANTITY)
    assert f["invoiced"] == "100.0" and f["accepted"] == "90.0"
    assert f["tolerance"] == "0.5"


def test_rejected_goods_do_not_count_as_received(store):
    """判的是**合格数**不是到货数：到了 100 件、验收不合格 10 件 = 合格 90 件。

    到了但验收没过的货不该付钱。这条判据只在 `accepted_quantity()` 一处做，
    散在调用点就会出现「有的地方减了、有的地方没减」。
    """
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 100.0, 10.0, 100.0, "12.00")])
    out = run_match(store)
    assert out["matched"] is False
    f = next(f for f in out["findings"]
             if f["rule_id"] == ap_codes.RULE_INVOICED_QUANTITY)
    assert f["accepted"] == "90.0", "合格数应当扣掉验收不合格的那部分"


def test_unit_price_within_tolerance_passes(store):
    """发票单价比订单高 0.01 元，单价容差 0.01 —— 放过（分币进位）。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 100.0, 0.0, 100.0, "12.01")])
    out = run_match(store)
    assert out["matched"] is True, f"0.01 元的差在容差内，不该拒付：{rule_ids(out)}"


def test_unit_price_beyond_tolerance_is_rejected(store):
    """发票单价高 0.50 元 —— 拒付，理由挂 `RULE_ITEM_NET_PRICE`。"""
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 100.0, 0.0, 100.0, "12.50")])
    out = run_match(store)
    assert ap_codes.RULE_ITEM_NET_PRICE in rule_ids(out)
    f = next(f for f in out["findings"] if f["rule_id"] == ap_codes.RULE_ITEM_NET_PRICE)
    assert f["invoiced"] == "12.50" and f["ordered"] == "12.00"


def test_tax_within_tolerance_passes(store):
    """税额差 0.02 元，税额容差 0.02 —— 放过（各行分别进位的累计差）。"""
    header = seed(store)
    bumped = str(objects.money(header["total_vat"]) + objects.money("0.02"))
    # 只动税额，含税总额与应付额跟着走，否则会连带触发 BR-CO-15 / BR-CO-16。
    incl = str(objects.money(header["total_excl_vat"]) + objects.money(bumped))
    seed(store, header_overrides={"total_vat": bumped, "total_incl_vat": incl,
                                  "amount_due": incl})
    out = run_match(store)
    assert out["matched"] is True, f"0.02 元的税差在容差内，不该拒付：{rule_ids(out)}"


def test_tax_beyond_tolerance_is_rejected(store):
    """税额差 1.00 元 —— 拒付，理由挂 `RULE_VAT_AMOUNT`（BR-CO-17）。"""
    header = seed(store)
    bumped = str(objects.money(header["total_vat"]) + objects.money("1.00"))
    incl = str(objects.money(header["total_excl_vat"]) + objects.money(bumped))
    seed(store, header_overrides={"total_vat": bumped, "total_incl_vat": incl,
                                  "amount_due": incl})
    out = run_match(store)
    assert ap_codes.RULE_VAT_AMOUNT in rule_ids(out)
    f = next(f for f in out["findings"] if f["rule_id"] == ap_codes.RULE_VAT_AMOUNT)
    assert f["breakdown"], "税额判据必须给出分税种的计算过程，否则无从复核"
    assert f["breakdown"][0]["tax_category_code"] == ap_codes.CODE_TAX_STANDARD


def test_the_three_tolerances_are_independent(store):
    """三个容差量纲不同，**不许合并成一个**。

    把数量容差调大不该让单价差被放过 —— 合并成一个「1%」的话就会。
    """
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 100.0, 0.0, 100.0, "12.50")])
    out = run_match(store, tolerance={"quantity": "999"})
    assert ap_codes.RULE_ITEM_NET_PRICE in rule_ids(out), (
        "放宽数量容差把单价差也放过了 —— 三个容差被合并了")


def test_unknown_tolerance_key_raises(store):
    """容差键写错**抛**，不静默忽略。

    静默忽略的后果是「我调了容差」和「容差没生效」看起来一模一样。
    """
    seed(store)
    inv = SkillInvoker(IDENTITY, store)
    inv.invoke("ap.intake", {"tenant_id": TEN, "case_id": CASE, "invoice_id": INV,
                             "po_id": PO, "po_version": 1, "gr_id": GR})
    res = inv.invoke("ap.match", {"tenant_id": TEN, "case_id": CASE,
                                  "tolerance": {"qty": "5"}})
    assert res.status == "failed" and "未知的容差键" in res.error


def test_default_tolerances_have_different_units(store):
    """缺省容差三项互不相等且都为正 —— 相等意味着有人把它们当成同一个量纲了。"""
    tol = {"quantity": match_mod.TOLERANCE_QUANTITY,
           "unit_price": match_mod.TOLERANCE_UNIT_PRICE,
           "tax": match_mod.TOLERANCE_TAX}
    assert all(v > 0 for v in tol.values())
    assert len(set(tol.values())) == 3, f"三个容差不该相等：{tol}"
    assert match_mod.TOLERANCE_EXACT == 0, "勾稽等式必须零容差"


# ------------------------------------------------------------------ 勾稽判据
@pytest.mark.parametrize("field, delta, rule", [
    ("line_net_total", "5.00", ap_codes.RULE_SUM_LINE_NET),        # BR-CO-10
    ("total_excl_vat", "5.00", ap_codes.RULE_TOTAL_WITHOUT_VAT),   # BR-CO-13
    ("total_incl_vat", "5.00", ap_codes.RULE_TOTAL_WITH_VAT),      # BR-CO-15
    ("amount_due", "5.00", ap_codes.RULE_AMOUNT_DUE),              # BR-CO-16
])
def test_totals_are_reconciled_with_zero_tolerance(store, field, delta, rule):
    """合计勾稽零容差：改动任一合计 5 元，对应那条规则必须命中。

    这四条是加减法，对不上就是发票自己算错了，不是计量误差。
    """
    header = seed(store)
    broken = str(objects.money(header[field]) + objects.money(delta))
    seed(store, header_overrides={field: broken})
    out = run_match(store)
    assert out["matched"] is False
    assert rule in rule_ids(out), f"改了 {field} 却没命中 {rule}：实际 {rule_ids(out)}"


def test_line_net_must_equal_quantity_times_price(store):
    """行净额勾稽（PEPPOL-EN16931-R120）—— 零容差。

    直接改一行的 `line_net`，让它不等于 数量 × 单价。
    """
    seed(store)
    objects.execute(store, "UPDATE supplier_invoice_line SET line_net='9999.00'"
                           " WHERE tenant_id=? AND invoice_id=? AND line_no=1",
                    (TEN, INV))
    out = run_match(store)
    assert ap_codes.RULE_LINE_NET_AMOUNT in rule_ids(out)
    f = next(f for f in out["findings"]
             if f["rule_id"] == ap_codes.RULE_LINE_NET_AMOUNT)
    assert f["observed"] == "9999.00" and f["expected"] == "1200.00"


def test_prepaid_amount_is_deducted(store):
    """BR-CO-16 的已付金额（BT-113）要扣掉 —— 部分预付过的发票只该付余款。"""
    header = fixtures.seed_three_way(
        store, tenant_id=TEN, supplier_id=SUP, po_id=PO, gr_id=GR, invoice_id=INV,
        lines=CLEAN_LINES, tax_category=ap_codes.CODE_TAX_STANDARD, tax_rate=13.0,
        invoice_type=ap_codes.CODE_COMMERCIAL_INVOICE,
        issued_at="2026-08-01T00:00:00+00:00", prepaid="500.00")
    out = run_match(store)
    assert out["matched"] is True, rule_ids(out)
    expect = objects.money(header["total_incl_vat"]) - objects.money("500.00")
    assert out["payable_amount"] == str(expect), "应付额必须扣掉已付"


# ------------------------------------------------------------ 码表与引用判据
def test_invoice_type_code_must_be_in_the_code_list(store):
    """发票类型码不在 UNCL1001 子集内 -> BR-CL-01。

    经 `ap.match` 判而不是靠 `ap.intake` 抛：收票那步抛的是「码不认识」，
    匹配这步给的是**带编号的拒付理由**，后者才是能拿给供应商看的东西。
    """
    seed(store)
    objects.execute(store, "UPDATE supplier_invoice SET invoice_type_code='999'"
                           " WHERE tenant_id=? AND invoice_id=?", (TEN, INV))
    inv = SkillInvoker(IDENTITY, store)
    # 案子已经在库了（前一次 seed 之后没建案），这里直接建案再匹配。
    guard.create_case(store, tenant_id=TEN, case_id=CASE, supplier_id=SUP, po_id=PO,
                      po_version=1, invoice_id=INV, gr_id=GR, amount_claimed="1",
                      plan_id="p", actor_skill="ap.intake", invocation_id="iv")
    res = inv.invoke("ap.match", {"tenant_id": TEN, "case_id": CASE})
    assert res.status == "ok"
    assert ap_codes.RULE_INVOICE_TYPE_CODED in sorted(
        f["rule_id"] for f in res.output["findings"])


def test_tax_category_code_must_be_in_uncl5305(store):
    """税种码不在 UNCL5305 内 -> BR-CL-17。"""
    seed(store)
    objects.execute(store, "UPDATE supplier_invoice_line SET tax_category_code='QQ'"
                           " WHERE tenant_id=? AND invoice_id=? AND line_no=1",
                    (TEN, INV))
    out = run_match(store)
    assert ap_codes.RULE_TAX_CATEGORY_CODED in rule_ids(out)


def test_missing_order_reference_is_rejected(store):
    """没有采购订单引用 -> PEPPOL-EN16931-R003：三单匹配无从下手。"""
    seed(store)
    guard.create_case(store, tenant_id=TEN, case_id=CASE, supplier_id=SUP, po_id=PO,
                      po_version=1, invoice_id=INV, gr_id=GR, amount_claimed="1",
                      plan_id="p", actor_skill="ap.intake", invocation_id="iv")
    objects.execute(store, "UPDATE supplier_invoice SET po_id='' WHERE tenant_id=?"
                           " AND invoice_id=?", (TEN, INV))
    res = SkillInvoker(IDENTITY, store).invoke("ap.match", {"tenant_id": TEN,
                                                            "case_id": CASE})
    assert ap_codes.RULE_ORDER_REFERENCE in sorted(
        f["rule_id"] for f in res.output["findings"])


def test_line_missing_from_receipt_or_order(store):
    """发票开了一行、收货单与订单都没有 -> 两条理由，各挂各的编号。"""
    seed(store)
    objects.execute(store, "DELETE FROM goods_receipt_line WHERE tenant_id=? AND"
                           " gr_id=? AND line_no=2", (TEN, GR))
    objects.execute(store, "DELETE FROM purchase_order_line WHERE tenant_id=? AND"
                           " po_id=? AND line_no=2", (TEN, PO))
    out = run_match(store)
    ids = rule_ids(out)
    assert ap_codes.RULE_INVOICED_QUANTITY in ids, "收货单没有这一行 = 货没收到"
    assert ap_codes.RULE_ITEM_NET_PRICE in ids, "订单没有这一行 = 没有订过"


def test_lines_only_on_the_order_are_not_reported(store):
    """订单上有、发票上没有的行**不报** —— 那是「还没开票」，分期开票是正常业务。"""
    # 先落两行三单，再只按第一行重落一次发票 —— 订单与收货仍是两行（INSERT OR
    # REPLACE 不删行），发票只剩一行，正是「第二行还没开票」那种局面。
    # 合计必须跟着只剩一行重算，否则会连带触发 BR-CO-10，测的就不是这件事了。
    seed(store)
    objects.execute(store, "DELETE FROM supplier_invoice_line WHERE tenant_id=? AND"
                           " invoice_id=? AND line_no=2", (TEN, INV))
    header = fixtures.seed_three_way(
        store, tenant_id=TEN, supplier_id=SUP, po_id=PO, gr_id=GR, invoice_id=INV,
        lines=[CLEAN_LINES[0]], tax_category=ap_codes.CODE_TAX_STANDARD, tax_rate=13.0,
        invoice_type=ap_codes.CODE_COMMERCIAL_INVOICE,
        issued_at="2026-08-01T00:00:00+00:00")
    out = run_match(store)
    assert out["matched"] is True, f"分期开票不该被判拒付：{rule_ids(out)}"
    assert out["payable_amount"] == header["amount_due"]


# --------------------------------------------------------- 贯穿：理由可核对
def test_every_finding_is_verifiable(store):
    """不管命中哪条判据，finding 都要带得起「拿编号去查规范能查到」这句话。

    这是本文件的兜底：上面每条用例只查自己关心的那个编号，这条查的是
    **所有** finding 的形状。将来加一条判据忘了走 `_finding()`，在这里当场死。
    """
    seed(store, lines=[(1, "SKU-A", 100.0, "12.00", 80.0, 0.0, 100.0, "13.00")],
         header_overrides={"amount_due": "1.00"})
    out = run_match(store)
    assert out["findings"], "这套三单应当产出多条拒付理由"
    for f in out["findings"]:
        assert f["rule_id"] in ap_codes.RULES, (
            f"finding 挂了一个表外编号 {f['rule_id']!r} —— 那就查不到了")
        rule = ap_codes.require_rule(f["rule_id"])
        assert f["text"] == rule.text, "finding 里的原文必须与规则表逐字一致"
        assert f["source"] == rule.source
        assert f["spec"] == ap_codes.SPEC_RELEASE
        assert f["fetched_at"] == ap_codes.FETCHED_AT
        assert f["severity"] in (match_mod.SEVERITY_BLOCK, match_mod.SEVERITY_INFO)
        assert f["message"].strip(), "每条理由都要有一句给人看的话"


def test_finding_refuses_a_fabricated_rule_id():
    """`_finding()` 挂自造编号时当场抛 —— 这是「理由可核对」的最后一道机器闸。"""
    with pytest.raises(KeyError, match="不许挂自造编号"):
        match_mod._finding("BR-CO-99", message="自造的理由")
