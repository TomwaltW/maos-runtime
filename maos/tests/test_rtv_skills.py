"""RTV 域 Skill 层 —— 六个 skill 的契约、五步主干，以及本轨要买的那三件东西。

    1. **两个权威终态都是问出来的**：`credited` 与 `settled` 只有 `rtv.observe`
       写得进去，且必须同事务附上外部单号的回执。
    2. **`acknowledged` 不是「认账了」**：供应商收到货 ≠ 供应商认了这笔钱，
       回执字段齐全也不放行 —— 这一条不合格，整个域的权威事实边界就是假的。
    3. **对账不推进状态**：算出「应该退我 1200」不等于供应商认了，
       `rtv.reconcile` 一行状态都不许推。

用例对**库里的行**下断言（贷项通知单几条、观察几行、案子停在哪个状态），
不只看返回值 —— 返回值是被测者自己给的，拿它当唯一判据等于让被测者给自己判分。

## 为什么这里自己造 stub 工具而不 import T62

契约 C-R8：`maos/tools/rtv.py`（T62）、`maos/domain/rtv/`（T61）都不在本轨基线里。
跨轨接口一律按契约的名字与形状用注入 + stub，真接线在整合期。stub 的状态取值域
逐字对齐契约 C-R5 与 `_common.py` 里的三张状态表。
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from maos.core.store import SqliteStore
from maos.domain.ap import objects as ap_objects
from maos.skills.builtin.rtv import RTV_SKILLS
from maos.skills.builtin.rtv import _common as C
from maos.skills.contract import SkillContext, SkillContract
from maos.skills.registry import get as skill_get

TENANT = "t-rtv"
CASE = "RTV-1001"
PLAN = "plan-rtv-1"
TASK = "task-rtv-1"
PO_ID = "PO-9001"
GR_ID = "GR-9001"
SUPPLIER = "SUP-77"

#: 契约 C-R4 那张表，逐字抄下来。测试**按它循环断言**，不在各处手抄字面量 ——
#: 手抄的那份和实现一起漂，两边同时错的时候没人会发现。
CONTRACT_C_R4 = {
    "rtv.intake": (["rtv_intake"], []),
    "rtv.dispose": (["rtv_disposition"], []),
    "rtv.ship": (["rtv_logistics"], ["carrier.ship", "carrier.track"]),
    "rtv.reconcile": (["rtv_reconcile"], ["supplier.credit_query"]),
    "rtv.observe": (["rtv_settlement"], ["supplier.credit_query", "ap.adjust_query"]),
    "rtv.compensate": (["rtv_settlement"], ["supplier.rma_submit"]),
}

#: 基线 4c956a8 上 `maos/skills/builtin/__init__.py` 的 sha256。冻结口径 C-1：
#: 投文件即注册，本文件一个字节都不该被改（改成显式清单，多条并行轨就要改同一处）。
BUILTIN_INIT_SHA256 = "472986610cdae62ba0237e671a106998af9ed2f340b5066a135e30b48ef22041"

RTV_PACKAGE = pathlib.Path(C.__file__).resolve().parent


# --------------------------------------------------------------------------- stub
class Stubs:
    """契约 C-R5 那五个工具的 stub，带调用计数与脚本化状态序列。

    状态序列取完之后**粘在最后一个**：轮询本来就是「一直问到终态」，
    序列耗尽抛异常会把「问了几次」这件事变成测试自己的实现细节。
    """

    def __init__(self, *, supplier=("issued",), carrier=("delivered",),
                 ap=("settled",), amount="1200.00"):
        self.supplier = list(supplier)
        self.carrier = list(carrier)
        self.ap = list(ap)
        self.amount = amount
        self.calls: dict[str, int] = {name: 0 for name in C.RTV_TOOLS}

    @staticmethod
    def _next(seq: list[str]) -> str:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    # ---- 供应商门户
    def rma_submit(self, **params) -> dict:
        self.calls[C.TOOL_RMA_SUBMIT] += 1
        return {"rma_id": f"RMA-{self.calls[C.TOOL_RMA_SUBMIT]}", "status": "submitted"}

    def credit_query(self, **params) -> dict:
        self.calls[C.TOOL_CREDIT_QUERY] += 1
        status = self._next(self.supplier)
        out = {"status": status, "poll_count": self.calls[C.TOOL_CREDIT_QUERY]}
        if status == "issued":
            out.update({"credit_note_id": "CN-2026-0007", "document_type": "381",
                        "amount_credited": self.amount, "currency": "CNY",
                        "issued_at": "2026-09-02T02:00:00+00:00"})
        if status == "disputed":
            out["message"] = "供应商不认这笔退货"
        return out

    # ---- 承运商
    def ship(self, **params) -> dict:
        self.calls[C.TOOL_CARRIER_SHIP] += 1
        return {"shipment_id": "SHP-1", "carrier": "demo-carrier",
                "tracking_no": "TRK-8899", "status": "created"}

    def track(self, **params) -> dict:
        self.calls[C.TOOL_CARRIER_TRACK] += 1
        return {"status": self._next(self.carrier),
                "poll_count": self.calls[C.TOOL_CARRIER_TRACK]}

    # ---- AP 系统（只读）
    def adjust_query(self, **params) -> dict:
        self.calls[C.TOOL_AP_ADJUST_QUERY] += 1
        status = self._next(self.ap)
        out = {"status": status, "poll_count": self.calls[C.TOOL_AP_ADJUST_QUERY]}
        if status in ("built", "settled", "voided"):
            out.update({"adjustment_id": "ADJ-551", "ap_reference": "AP-REF-551"})
        return out

    def as_extras(self) -> dict:
        return {
            C.TOOL_RMA_SUBMIT: self.rma_submit,
            C.TOOL_CREDIT_QUERY: self.credit_query,
            C.TOOL_CARRIER_SHIP: self.ship,
            C.TOOL_CARRIER_TRACK: self.track,
            C.TOOL_AP_ADJUST_QUERY: self.adjust_query,
        }


def make_ctx(store, stubs: Stubs) -> SkillContext:
    return SkillContext(store=store, extras={
        "plan_id": PLAN, "task_id": TASK, "trace_id": "tr-1",
        "tools": stubs.as_extras(),
    })


def seed_source_documents(store) -> None:
    """灌源单：供应商 / 采购订单 / 收货单。

    五张表复用 `maos/domain/ap/schema.sql` 的既有定义（契约 C-R1 抬头、C-R9），
    本域一张都不重建 —— 这里也只往里写测试数据，不改一个字段。
    """
    C.execute(store, "INSERT OR REPLACE INTO supplier (tenant_id, supplier_id, name)"
                     " VALUES (?,?,?)", (TENANT, SUPPLIER, "示例供应商"))
    C.execute(store, "INSERT OR REPLACE INTO purchase_order (tenant_id, po_id, version,"
                     " supplier_id, currency, ordered_at, read_at) VALUES (?,?,?,?,?,?,?)",
              (TENANT, PO_ID, 1, SUPPLIER, "CNY", "2026-08-01T00:00:00+00:00",
               "2026-09-01T00:00:00+00:00"))
    C.execute(store, "INSERT OR REPLACE INTO purchase_order_line (tenant_id, po_id, version,"
                     " line_no, sku, quantity, unit_price) VALUES (?,?,?,?,?,?,?)",
              (TENANT, PO_ID, 1, 1, "SKU-1", 10.0, "400.00"))
    C.execute(store, "INSERT OR REPLACE INTO goods_receipt (tenant_id, gr_id, po_id,"
                     " po_version, received_at, read_at) VALUES (?,?,?,?,?,?)",
              (TENANT, GR_ID, PO_ID, 1, "2026-08-20T00:00:00+00:00",
               "2026-09-01T00:00:00+00:00"))
    C.execute(store, "INSERT OR REPLACE INTO goods_receipt_line (tenant_id, gr_id, line_no,"
                     " sku, quantity_received, quantity_rejected) VALUES (?,?,?,?,?,?)",
              (TENANT, GR_ID, 1, "SKU-1", 10.0, 0.0))


INTAKE_PAYLOAD = {
    "tenant_id": TENANT, "case_id": CASE, "po_id": PO_ID, "po_version": 1,
    "gr_id": GR_ID,
    "lines": [{"gr_line_no": 1, "sku": "SKU-1", "quantity_returned": 3.0,
               "unit_price": "400.00", "reason_code": "defective"}],
}


def run_skill(name: str, payload: dict, ctx: SkillContext):
    """直接跑 skill 的 run()。

    不经 `SkillInvoker`：本轨要验的是 skill 自己的行为，而 invoker 会把异常包成
    `SkillResult(status='failed')` —— 那样「守卫拒了一次越权写入」和「skill 里有个
    拼写错误」在断言里长得一模一样。
    """
    cls = skill_get(name)
    assert cls is not None, f"注册表里没有 {name}"
    return cls().run(payload, ctx)


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()                       # 编排层的表（event_log 在里面）
    # 五张源单表（supplier / purchase_order / ... ）的持有方是 ap 域，rtv 不重建：
    # 见 maos/domain/rtv/objects.py::require_upstream_tables 的 docstring。
    ap_objects.ensure_schema(s)
    C.ensure_schema(SkillContext(store=s))
    seed_source_documents(s)
    return s


def drive_to_shipped(store, stubs: Stubs) -> SkillContext:
    """跑到 `shipped` —— 后面几组用例都从这里起跳。"""
    ctx = make_ctx(store, stubs)
    run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    run_skill("rtv.ship", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    return ctx


# ------------------------------------------------------------------ 1. 注册与契约
def test_six_skills_registered_matching_frozen_contract():
    """六个 skill 都取得到，且 name/version/owner_roles/depends_tools 与 C-R4 逐字相同。"""
    assert list(RTV_SKILLS) == ["rtv.intake", "rtv.dispose", "rtv.ship",
                                "rtv.reconcile", "rtv.observe", "rtv.compensate"]
    assert sorted(RTV_SKILLS) == sorted(CONTRACT_C_R4)
    for name, (owner_roles, depends_tools) in CONTRACT_C_R4.items():
        cls = skill_get(name)
        assert cls is not None, f"注册表里没有 {name}"
        contract = cls.contract
        assert contract.name == name
        assert contract.version == "1.0.0"
        assert contract.owner_roles == owner_roles, f"{name} 的 owner_roles 与 C-R4 不符"
        assert contract.depends_tools == depends_tools, f"{name} 的 depends_tools 与 C-R4 不符"


def test_reconcile_does_not_depend_on_ap_and_observe_depends_on_both():
    """依赖清单本身就是一条论证：谁问谁、谁不问谁。

    · `rtv.reconcile` 只问供应商 —— 问 AP 是 observe 的事，两处都问会让
      「钱到账了没有」有两个答案。
    · `rtv.observe` 两个都问 —— 两个权威终态各归各的外部系统。
    · `rtv.intake` / `rtv.dispose` 一个工具都不依赖 —— 它们做的是本地推断。
    · `rtv.observe` 的依赖里**没有** `supplier.rma_submit`：unknown 时重发那条路
      在依赖清单这一层就不存在，不是靠 if 分支拦着。
    """
    assert skill_get("rtv.reconcile").contract.depends_tools == ["supplier.credit_query"]
    assert skill_get("rtv.observe").contract.depends_tools == [
        "supplier.credit_query", "ap.adjust_query"]
    assert skill_get("rtv.intake").contract.depends_tools == []
    assert skill_get("rtv.dispose").contract.depends_tools == []
    assert "supplier.rma_submit" not in skill_get("rtv.observe").contract.depends_tools


def test_every_contract_field_is_filled_in():
    """`SkillContract` 十二个字段一项不许空。

    两个例外，各有理由，不是放水：
      · `depends_tools` —— C-R4 给 intake / dispose 定的就是 `[]`（本地推断，不碰外部）。
      · `max_retries` —— 0 是合法取值（`failure_policy=escalate` 时本来就不该重试）。
    其余十项全部要求非空，尤其 `failure_policy` / `security_boundary` / `preconditions`：
    Agent 不读实现，只读契约来决定要不要调、怎么兜底。
    """
    fields = [f for f in SkillContract.__dataclass_fields__]
    assert len(fields) == 12, f"契约字段数变了：{fields}"
    optional = {"depends_tools", "max_retries"}
    for name in RTV_SKILLS:
        contract = skill_get(name).contract
        for field in fields:
            value = getattr(contract, field)
            if field in optional:
                assert value is not None, f"{name}.{field} 不该是 None"
                continue
            assert value, f"{name}.{field} 是空的 —— 契约字段缺一不可"
        assert contract.failure_policy in ("retry", "fallback", "escalate")


# ------------------------------------------------------------------ 2. 五步顺利路径
def test_happy_path_walks_every_hop_to_settled(store):
    """intake -> dispose -> ship -> reconcile -> observe(credited) -> observe(settled)。

    每一跳都对库里那行下断言，并把状态轨迹整条打出来 —— 这条轨迹就是 SOP 五步
    在代码里的样子。
    """
    stubs = Stubs(supplier=["submitted", "acknowledged", "issued"],
                  carrier=["created", "in_transit", "delivered"],
                  ap=["staged", "built", "settled"])
    ctx = make_ctx(store, stubs)
    trail = []

    intake = run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    trail.append(intake["case"]["biz_status"])
    assert intake["amount_claimed"] == "1200.00"
    assert intake["case"]["return_action"] == "", "受理的人不该替裁定的人拍板"
    assert intake["source"]["supplier_id"] == SUPPLIER
    assert len(intake["refs"]) == 3

    dispose = run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    trail.append(dispose["biz_status"])
    assert dispose["return_action"] == "credit"
    assert [item["rule_id"] for item in dispose["rationale"]] == ["RTV-R-01"]
    assert C.get_case(store, TENANT, CASE)["return_action"] == "credit"

    ship = run_skill("rtv.ship", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    trail.append(ship["biz_status"])
    assert ship["carrier_status"] == "delivered"
    assert ship["poll_count"] == 3 > 1, "送达是问出来的，一次 track 不够"

    # 对账跑在开票与到账之前，所以第一次必然对不上 —— 缺的正是那两条外部的腿。
    recon = run_skill("rtv.reconcile", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    assert recon["reconciled"] is False
    assert recon["amount_claimed"] == "1200.00"
    assert recon["creditable_amount"] == ""
    assert {item["rule_id"] for item in recon["findings"]} == {"RTV-R-11", "RTV-R-12"}
    assert recon["biz_status"] == "shipped", "对账是推断，一行状态都不许推"

    credited = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    trail.append(credited["biz_status"])
    assert credited["advanced"] is True and credited["observed_state"] == "issued"
    assert credited["reference"] == "CN-2026-0007"

    settled = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    trail.append(settled["biz_status"])
    assert settled["settled"] is True and settled["observed_state"] == "settled"
    assert settled["reference"] == "AP-REF-551"

    assert trail == ["received", "disposed", "shipped", "credited", "settled"]

    # 库里那两份凭据 —— 状态是跟着它们一起落的，不是自己长出来的。
    notes = C.credit_notes_of(store, TENANT, CASE)
    assert len(notes) == 1 and notes[0]["credit_note_id"] == "CN-2026-0007"
    assert notes[0]["document_type"] == "381"
    assert notes[0]["amount_credited"] == "1200.00"
    assert notes[0]["observed_by"] == "rtv.observe"
    assert notes[0]["invocation_id"], "回执必须带 actor 锚点，否则审计链断了"
    assert notes[0]["issued_at"] != notes[0]["observed_at"], (
        "开票时间与观察时间刻意分开：一个是供应商的动作，一个是我方的动作")

    obs = C.settlement_observations_of(store, TENANT, CASE)
    assert len(obs) == 1 and obs[0]["observed_state"] == "settled"
    assert obs[0]["ap_reference"] == "AP-REF-551"
    assert obs[0]["invocation_id"]

    # 三条腿齐了，再对一次就对得上。
    again = run_skill("rtv.reconcile", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    assert again["reconciled"] is True
    assert again["creditable_amount"] == "1200.00"
    assert again["findings"] == []
    assert again["attempt"] == 2, "对账保留历史，返工重跑不覆盖旧结论"


def test_poll_count_proves_the_terminal_state_was_asked_for(store):
    """一次 query 不够：`poll_count > 1` 才拿到终态，而次数落在事件里。

    把外部系统换成一步返回 issued 的桩，`rtv.observe` 就没有存在理由了，
    整条论证跟着塌 —— 所以这条断言盯的是「问了不止一次」。
    """
    stubs = Stubs(supplier=["submitted", "acknowledged", "issued"])
    ctx = drive_to_shipped(store, stubs)
    out = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    assert out["poll_count"] == 3 > 1
    assert stubs.calls[C.TOOL_CREDIT_QUERY] == 3

    changed = [e for e in store.list_event_log(PLAN)
               if e.get("event_type") == C.STATUS_EVENT and e.get("to_state") == "credited"]
    assert len(changed) == 1
    detail = changed[0]["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["poll_count"] == 3, "轮询次数是「终态是问出来的」的证据，不能丢"
    assert detail["observation_attached"] is True


# ------------------------------------------------------- 3. acknowledged 不推进
def test_acknowledged_does_not_advance_to_credited(store):
    """🔴 供应商收到货 ≠ 供应商认了这笔钱。

    stub 一直回 `acknowledged`：回执字段齐全、形状与终态回执一模一样，
    但 `rtv.observe` 不推进，案子仍停在 `shipped`，库里一张贷项通知单都没有。
    """
    stubs = Stubs(supplier=["acknowledged"])
    ctx = drive_to_shipped(store, stubs)

    out = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    assert out["advanced"] is False
    assert out["observed_state"] == "acknowledged"
    assert out["credited"] is False and out["settled"] is False
    assert out["biz_status"] == "shipped"
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    assert C.credit_notes_of(store, TENANT, CASE) == []
    assert out["poll_count"] == 5, "非终态要问满 max_polls，不是问一次就放弃"


def test_guard_rejects_an_acknowledged_receipt_even_from_the_writer(store):
    """第二层防线：连权威写入方递一张 `acknowledged` 的回执也写不进 credited。

    分支会被改，守卫不会被顺手改 —— 两层都在，这条边界才不是「某个 if 恰好写对了」。
    """
    stubs = Stubs(supplier=["acknowledged"])
    drive_to_shipped(store, stubs)
    with pytest.raises(C.AuthoritativeFactViolation) as err:
        C.update_biz_status(store, TENANT, CASE, "credited", C.AUTHORITATIVE_WRITER,
                            "inv-fake", observation={
                                "credit_note_id": "CN-FAKE", "observed_state": "acknowledged",
                                "amount_credited": "1200.00", "issued_at": "2026-09-02"})
    assert "acknowledged" in str(err.value)
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    assert C.AUTHORITATIVE_RECEIPT_STATE["credited"] == frozenset({"issued"})


# ------------------------------------------------------------- 4. 越权写入被拒
def test_non_observe_skills_cannot_write_authoritative_states(store):
    """越权写 `credited` / `settled` 一律拒，且**不静默失败**：抛异常 + 落事件。"""
    stubs = Stubs()
    drive_to_shipped(store, stubs)
    for actor in ("rtv.reconcile", "rtv.ship", "rtv.compensate", "rtv.dispose"):
        with pytest.raises(C.AuthoritativeFactViolation):
            C.update_biz_status(store, TENANT, CASE, "credited", actor, "inv-x")
    violations = [e for e in store.list_event_log(PLAN)
                  if e.get("event_type") == C.VIOLATION_EVENT]
    assert len(violations) == 4, "拒绝一次越权写入本身就是证据，吞掉就没了"
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"


def test_no_bypass_path_around_the_guard(store):
    """旁路也堵死：三张受保护的表从 `execute()` 写不进去。

    grep 挡的是提交进仓库的旁路，这一条挡的是运行时的旁路。
    """
    for sql in (
        "UPDATE rtv_case SET biz_status='credited' WHERE tenant_id=? AND case_id=?",
        "INSERT INTO credit_note (tenant_id, case_id, credit_note_id) VALUES (?,?,'X')",
        "INSERT INTO rtv_settlement_observation (tenant_id, case_id, seq) VALUES (?,?,1)",
    ):
        with pytest.raises(C.BypassedGuardError):
            C.execute(store, sql, (TENANT, CASE))


def test_only_observe_touches_the_two_receipt_tables():
    """源码层面：两张回执表的名字只出现在 `_common.py` 与 `observe.py` 里。

    另外四个 skill 连提都不该提它们 —— 「不留第二条路径」这句话在源码里也要成立。
    """
    for path in sorted(RTV_PACKAGE.glob("*.py")):
        if path.name in ("_common.py", "observe.py", "__init__.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for table in ("credit_note", "rtv_settlement_observation"):
            assert f"INSERT INTO {table}" not in text
            assert f"INSERT OR REPLACE INTO {table}" not in text
            assert f"UPDATE {table}" not in text


def test_reconcile_never_advances_the_business_status(store):
    """🔴 对账是推断：即使供应商已经开票、AP 已经核销，对账也不推进一格。

    算出「应该退我 1200」不等于供应商认了这 1200，那一跳归 `rtv.observe`。
    """
    stubs = Stubs(supplier=["issued"], ap=["settled"])
    ctx = drive_to_shipped(store, stubs)

    out = run_skill("rtv.reconcile", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    assert out["supplier_status"] == "issued", "供应商侧确实说开票了"
    assert out["biz_status"] == "shipped"
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    assert C.credit_notes_of(store, TENANT, CASE) == [], (
        "对账不许替 rtv.observe 把贷项通知单落库 —— 那等于自己给自己开发票")
    assert {item["rule_id"] for item in out["findings"]} == {"RTV-R-11", "RTV-R-12"}


# --------------------------------------------------------------- 5. unknown 不重发
def test_unknown_does_not_resubmit_the_rma(store):
    """🔴 供应商侧查不到时：不推进、不留终态、**尤其不重发申请**。

    那笔申请可能已经受理了，只是查不到。重发会造出第二笔退货申请，
    而两笔申请对同一批货，对账那步会看到双倍金额。
    """
    stubs = Stubs(supplier=["unknown"])
    ctx = drive_to_shipped(store, stubs)
    before = stubs.calls[C.TOOL_RMA_SUBMIT]

    out = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    assert out["advanced"] is False and out["observed_state"] == "unknown"
    assert out["needs_compensation"] is False, "问不出来不是坏消息，是还没问出来"
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    assert stubs.calls[C.TOOL_RMA_SUBMIT] == before == 0


def test_compensate_refuses_to_resubmit_on_unknown(store):
    """补偿也不许在 unknown 上重发 —— 同一条理由，换个入口不该有第二种答案。"""
    stubs = Stubs(supplier=["unknown"])
    ctx = drive_to_shipped(store, stubs)
    with pytest.raises(ValueError, match="unknown"):
        run_skill("rtv.compensate", {"tenant_id": TENANT, "case_id": CASE,
                                     "reason": "查不到", "observed_state": "unknown",
                                     "resubmit": True}, ctx)
    assert stubs.calls[C.TOOL_RMA_SUBMIT] == 0
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"


# ------------------------------------------------------------------ 6. 失败路径
def test_disputed_leaves_a_trace_and_routes_to_compensation(store):
    """供应商明确不认：不推进、留痕、转补偿；补偿做完才收口到 compensated。"""
    stubs = Stubs(supplier=["disputed"])
    ctx = drive_to_shipped(store, stubs)

    out = run_skill("rtv.observe", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    assert out["advanced"] is False and out["needs_compensation"] is True
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped", (
        "补偿还没做，不许替它宣布收口")
    adverse = [e for e in store.list_event_log(PLAN)
               if e.get("event_type") == C.ADVERSE_EVENT]
    assert len(adverse) == 1, "「供应商说不认」这件事必须留痕，不能只活在日志文本里"

    comp = run_skill("rtv.compensate", {
        "tenant_id": TENANT, "case_id": CASE, "reason": "供应商 disputed",
        "observed_state": "disputed", "resubmit": True}, ctx)
    assert comp["biz_status"] == "compensated"
    assert comp["resubmitted"] is True and stubs.calls[C.TOOL_RMA_SUBMIT] == 1
    detail = json.loads(comp["record"]["detail_json"])
    assert detail["from_status"] == "shipped" and detail["rma"]["rma_id"] == "RMA-1"


def test_carrier_exception_does_not_ship(store):
    """承运商异常：`shipped` 不许落，因为 `shipped` 的判据是承运商说送到了。"""
    stubs = Stubs(carrier=["exception"])
    ctx = make_ctx(store, stubs)
    run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    out = run_skill("rtv.ship", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    assert out["delivered"] is False and out["needs_compensation"] is True
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "disposed"


def test_ship_does_not_resend_the_waybill(store):
    """重跑只查不发：`carrier.ship` 是写操作，重发会造出第二张运单。"""
    stubs = Stubs(carrier=["in_transit", "delivered"])
    ctx = make_ctx(store, stubs)
    run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    first = run_skill("rtv.ship", {"tenant_id": TENANT, "case_id": CASE}, ctx)
    second = run_skill("rtv.ship", {"tenant_id": TENANT, "case_id": CASE}, ctx)

    assert first["reshipped"] is True and second["reshipped"] is False
    assert stubs.calls[C.TOOL_CARRIER_SHIP] == 1
    rows = C.query(store, "SELECT * FROM rtv_shipment WHERE tenant_id=? AND case_id=?",
                   (TENANT, CASE))
    assert len(rows) == 1


def test_out_of_window_is_rejected_without_a_disposition_row(store):
    """不予受理走 `rejected`，且不往 `rtv_disposition` 里塞一个 CHECK 拒收的值。"""
    stubs = Stubs()
    ctx = make_ctx(store, stubs)
    run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)

    out = run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE,
                                    "within_return_window": False}, ctx)

    assert out["rejected"] is True and out["biz_status"] == "rejected"
    assert out["rationale"][0]["rule_id"] == "RTV-R-05"
    assert C.query(store, "SELECT * FROM rtv_disposition WHERE tenant_id=? AND case_id=?",
                   (TENANT, CASE)) == []


# ------------------------------------------------------------------ 7. 受理与幂等
def test_intake_refuses_a_return_line_that_points_nowhere(store):
    """指不回去就抛：这一档是可重试的失败（源单还没同步），与「该不该退」分开。"""
    ctx = make_ctx(store, Stubs())
    payload = dict(INTAKE_PAYLOAD)
    payload["lines"] = [dict(INTAKE_PAYLOAD["lines"][0], gr_line_no=9)]
    with pytest.raises(LookupError, match="收货行"):
        run_skill("rtv.intake", payload, ctx)
    assert C.get_case(store, TENANT, CASE) is None


def test_intake_refuses_to_return_more_than_was_accepted(store):
    """退得比收得多是数据错，不是可裁定的分歧。"""
    ctx = make_ctx(store, Stubs())
    payload = dict(INTAKE_PAYLOAD)
    payload["lines"] = [dict(INTAKE_PAYLOAD["lines"][0], quantity_returned=99.0)]
    with pytest.raises(ValueError, match="合格数"):
        run_skill("rtv.intake", payload, ctx)


def test_intake_is_idempotent_and_does_not_roll_a_case_back(store):
    """重跑受理不许把已经推进的案子悄悄倒回 `received`。"""
    stubs = Stubs()
    ctx = drive_to_shipped(store, stubs)
    again = run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    assert again["case"]["biz_status"] == "shipped"
    assert C.get_case(store, TENANT, CASE)["biz_status"] == "shipped"
    assert len(C.list_business_refs(store, plan_id=PLAN)) == 3, (
        "rtv_business_ref 没有主键，幂等靠先删后插；重跑不该攒出重复行")


def test_intake_rejects_a_reused_case_id_with_different_facts(store):
    """同一个案号上来一份业务字段不同的受理：拒 + 留事件，不静默覆盖。"""
    ctx = make_ctx(store, Stubs())
    run_skill("rtv.intake", dict(INTAKE_PAYLOAD), ctx)
    payload = dict(INTAKE_PAYLOAD)
    payload["lines"] = [dict(INTAKE_PAYLOAD["lines"][0], quantity_returned=2.0)]
    with pytest.raises(C.CaseIdentityConflict):
        run_skill("rtv.intake", payload, ctx)
    conflicts = [e for e in store.list_event_log(PLAN)
                 if e.get("event_type") == C.CASE_CONFLICT_EVENT]
    assert len(conflicts) == 1


def test_mixed_reasons_are_not_merged_into_one_action(store):
    """一案里既要补发又要退款：拆案，不许在这里挑一个当代表。"""
    ctx = make_ctx(store, Stubs())
    C.execute(store, "INSERT OR REPLACE INTO goods_receipt_line (tenant_id, gr_id, line_no,"
                     " sku, quantity_received, quantity_rejected) VALUES (?,?,?,?,?,?)",
              (TENANT, GR_ID, 2, "SKU-2", 5.0, 0.0))
    payload = dict(INTAKE_PAYLOAD)
    payload["lines"] = [
        dict(INTAKE_PAYLOAD["lines"][0]),
        {"gr_line_no": 2, "sku": "SKU-2", "quantity_returned": 1.0,
         "unit_price": "100.00", "reason_code": "wrong_item"},
    ]
    run_skill("rtv.intake", payload, ctx)
    with pytest.raises(ValueError, match="拆案"):
        run_skill("rtv.dispose", {"tenant_id": TENANT, "case_id": CASE}, ctx)


# ------------------------------------------------------------ 8. 机制与冻结口径
def test_builtin_init_is_untouched():
    """🔴 冻结口径 C-1：`builtin/__init__.py` 一个字节都没动。

    它用 `pkgutil.iter_modules` 自动发现，投文件即注册。改成显式 import 清单，
    多条并行轨都要改同一处，合并必冲突。
    """
    init_py = pathlib.Path(C.__file__).resolve().parents[1] / "__init__.py"
    assert init_py.name == "__init__.py" and init_py.parent.name == "builtin"
    raw = init_py.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BUILTIN_INIT_SHA256, (
        "builtin/__init__.py 被改了 —— 冻结口径 C-1 不许动它")
    assert b"rtv" not in raw, "本包靠动态发现进注册表，不该在这里出现显式清单"


def test_only_one_tentative_cross_track_import():
    """🔴 试探性 import 只有一处，且在 `try` 里。

    T61 / T62 的模块不在本轨基线里（契约 C-R8）。裸 import 会让本轨从第一条测试起
    就是红的；散在六个 skill 里则会让整合期删 fallback 要改六处。
    """
    hits = []
    for path in sorted(RTV_PACKAGE.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "maos.domain.rtv" in line or "maos.tools.rtv" in line:
                if line.strip().startswith("from ") or line.strip().startswith("import "):
                    hits.append((path.name, lineno, line.strip()))
    assert len(hits) == 1, f"跨轨 import 应当只有一处，实际：{hits}"
    assert hits[0][0] == "_common.py"
    assert hits[0][2].startswith("from maos.domain.rtv import")


def test_frozen_constants_match_the_contract():
    """C-R2 / C-R3 的常量逐字对齐契约 —— 整合期换成 T61 的 guard 时，两边必须仍相同。"""
    assert C.AUTHORITATIVE_WRITER == "rtv.observe"
    assert C.AUTHORITATIVE_STATES == frozenset({"credited", "settled"})
    assert C.AUTHORITATIVE_RECEIPT_STATE == {
        "credited": frozenset({"issued"}),
        "settled": frozenset({"settled"}),
    }
    assert set(C.AUTHORITATIVE_RECEIPT_STATE) == set(C.AUTHORITATIVE_STATES), (
        "两张表必须同增同减：有权威终态而没有判据，等于「有回执就算数」")
    assert C.BIZ_STATUS_FLOW == {
        "received": ("disposed", "rejected"),
        "disposed": ("shipped", "rejected", "compensated"),
        "shipped": ("credited", "compensated"),
        "credited": ("settled", "compensated"),
        "settled": (),
        "rejected": (),
        "compensated": (),
    }
    assert C.INITIAL_STATUS == "received"
    assert C.VIOLATION_EVENT == "AuthoritativeFactViolation"
    assert C.DOMAIN == "rtv"


def test_business_status_never_leaks_into_the_task_state_machine():
    """铁律 9：业务状态是 `rtv_case.biz_status` 的字段，不是 Task 状态。"""
    from maos.contracts import states

    # `TaskState` / `PlanState` 是常量类（不是 Enum），取值靠扫类属性。
    task_states = {v for k, v in vars(states.TaskState).items()
                   if not k.startswith("_") and isinstance(v, str)}
    plan_states = {v for k, v in vars(states.PlanState).items()
                   if not k.startswith("_") and isinstance(v, str)}
    for status in C.BIZ_STATUS_FLOW:
        assert status not in task_states, f"{status} 混进了 Task 状态机"
        assert status not in plan_states, f"{status} 混进了 Plan 状态机"


def test_terminal_is_judged_by_us_not_by_the_receipt(store):
    """终态判据在我方：外部回执自称 `is_terminal=True` 也不算数。

    信它等于把「这算不算终态」的判据交给被观察方 —— 一个把 acknowledged 标成
    终态的实现会让 `rtv.observe` 提前收口，而那正是本域最不该出的错。
    """
    assert C.terminal("supplier", "issued") is True
    assert C.terminal("supplier", "acknowledged") is False
    assert C.terminal("carrier", "delivered") is True
    assert C.terminal("ap", "settled") is True
    assert C.terminal("ap", "built") is False
    with pytest.raises(ValueError, match="取值域外"):
        C.require_state("supplier", "totally-new-word")


def test_unknown_tool_is_not_silently_stubbed(store):
    """工具取不到就抛，不自动造一个空实现 —— 自动兜底会让忘了注册变成静默假绿。"""
    ctx = SkillContext(store=store, extras={"plan_id": PLAN, "tools": {}})
    with pytest.raises(LookupError, match="没有工具"):
        C.tool_port(ctx, C.TOOL_CREDIT_QUERY)
    with pytest.raises(LookupError, match="C-R5"):
        C.tool_port(make_ctx(store, Stubs()), "ap.adjust_build")
