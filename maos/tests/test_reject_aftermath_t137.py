"""驳回之后的三件（T137）—— 客户收不到通知、审批表上没有那一行、代跑的闸署真人名。

三件都出自 `docs/BACKLOG.md ## task-t135`：Wave E 那一轨把「房间不替人签跑起来才
出现的闸」做成了真的，代价是把驳回**之后**的三个空洞暴露出来。

`maos/tests/test_room_outcome_commands.py` 里那条已经从反向改判成正向，钉的是
**房间那条端到端链**（`/refund` -> `/approve` -> `/reject` -> `/assign` -> `/resolve`）。
本文件钉的是它盖不到的那几处：CLI 那条路上的驳回、两种驳回的分界、署名的来源、
以及措辞那条红线在**换一种关单结论**时仍然成立。

## 分界这件事为什么要单独钉

「驳回付款闸」与「驳回整个案子」在库里是两件事，判据分别是两张表上的两个字段：

  · 这一行审批落在哪一跳 -> `business_ref(object_type='approval_record').task_id`
  · 整个案子有没有被驳回 -> `refund_case.biz_status == 'rejected'`

`_reject_case()` 那条 submitted/approved 守卫就是这条分界（房间驳付款闸时案子已在
`gateway_accepted`，守卫不放行）。把两者合进一个字段，或者让房间那一跳顺手把案子
推成 `rejected`，都会让「钱还在处理中」与「这一单不办了」再也分不开 —— 而那两句话
对客户是完全相反的两件事。
"""

from __future__ import annotations

import json
import os

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects, projection
from maos.flows import custom_case
from maos.ingress.contracts import CHANNEL_FEISHU, InboundMessage, OutboundMessage
from maos.ingress.router import IngressRouter
from maos.skills.builtin.refund import _common as C
from maos.skills.builtin.refund import compensate as CP
from maos.skills.builtin.refund.notify import NotifyCustomerSkill as NOTIFY

ORDER = "ORD-2026-0001"
CASE = f"RC-{ORDER}"
TICKET = f"MT-{CASE}"
TENANT = "tnt-demo"
BOSS = "@boss:maos.local"
#: `payment_ops` 的主责人。关单是**第二道闸**：只有工单的承接人签得了
#: 「我把钱线下退了」这句话（T117），名单内的别人一律拒。
PAYOPS = "@payops:maos.local"
FAIL_CODE = "ACQ.SELLER_BALANCE_NOT_ENOUGH"

#: 红线词表（派单 §3.1 / 跨轨契约 §C）。`payment_observation` 上没有到账观察时，
#: 通知里一个都不许出现。**「未到账」不在其列**：那是投影的五个字面值之一
#: （`已补偿（未到账）`），说的恰恰是「没到」。
FORBIDDEN = ("已到账", "已退回", "退款成功", projection.PUBLIC_SETTLED)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ledger(fail_orders: dict | None = None) -> dict:
    led = custom_case.load(os.path.join(ROOT, "scenarios", "custom", "ledger.json"),
                           require_case=False)
    if fail_orders is not None:
        led["gateway"] = {**led.get("gateway", {}), "fail_orders": dict(fail_orders)}
    return led


def _payload(fail_orders: dict | None = None) -> dict:
    from maos.ingress.router import _load_run_requests

    return _load_run_requests().build_case(_ledger(fail_orders), {
        "order_id": ORDER, "reason": "quality_defect", "amount": None,
        "requested_at": "2026-09-05T00:00:00+00:00"})


def _store() -> SqliteStore:
    store = SqliteStore(":memory:")
    store.init_schema()
    objects.ensure_schema(store)
    CP.ensure_ticket_schema(store)
    return store


def _detail(event: dict) -> dict:
    """一条 event_log 的 `detail`。**两种形态都认**：store 直读时是 dict，
    从库里捞回来时是 JSON 串（口径同 `custom_case._blocked_reason`）。"""
    detail = event.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return {}
    return detail if isinstance(detail, dict) else {}


def _notifications(store) -> list[dict]:
    return objects.query(store, "SELECT * FROM notification WHERE tenant_id=? AND case_id=?"
                                " ORDER BY revision", (TENANT, CASE))


def _approvals(store) -> list[dict]:
    return objects.query(store, "SELECT * FROM approval_record WHERE tenant_id=? AND case_id=?"
                                " ORDER BY decided_at", (TENANT, CASE))


# ==========================================================================
# 1. 案子被整体驳回 -> 客户收到一条如实的通知
# ==========================================================================
@pytest.fixture(scope="module")
def rejected(tmp_path_factory):
    """核算闸上驳回整个案子（`approve=False`），跑在一个共享库上。

    module 级：这条链要真跑一个 Plan，下面几条断言各跑一遍毫无必要地慢。
    """
    store = _store()
    result = custom_case.run_payload(_payload(), approve=False, verbose=False, store=store)
    return {"store": store, "result": result}


def test_a_rejected_case_tells_the_customer_it_was_rejected(rejected):
    """驳回整个案子之后，客户**收到了**结论 —— 这条路上从前一个字都发不出去。

    `notify.customer` 在 DAG 上依赖付款、付款依赖刚落 FAILED 的核算，它停在 PENDING
    再也不跑。于是「这一单不予退款」这个结论只存在于库里，客户永远不知道。
    """
    store = rejected["store"]
    assert guard.get_case(store, TENANT, CASE)["biz_status"] == "rejected"

    rows = _notifications(store)
    assert len(rows) == 1, "案子被整体驳回了，客户仍然一条通知都没收到"

    case = guard.get_case(store, TENANT, CASE)
    content = NOTIFY._default_content(
        case, NOTIFY._public_status(store, TENANT, CASE, case),
        NOTIFY._compensation_tail(store, TENANT, CASE, case))
    assert C.digest(content) == rows[0]["content_digest"], (
        f"发出去的与按库里事实重算的对不上 —— 措辞在别处被拼了第二遍：\n{content}")
    assert projection.PUBLIC_REJECTED in content, f"没把结论说出来：{content}"


def test_the_rejection_notice_says_nothing_about_money_arriving(rejected):
    """🔴 铁律 8：一条到账观察都没有，正文里一个到账口径都不许出现。

    这一跑 `payment_observation` 是空的（核算被驳回，付款压根没发起）。
    此时说出任何一句「已到账」，说的都是一件本系统无从观察的外部事实。
    """
    store = rejected["store"]
    obs = objects.query(store, "SELECT * FROM payment_observation WHERE tenant_id=?"
                               " AND case_id=?", (TENANT, CASE))
    assert obs == [], "这一跑不该有任何到账观察 —— 前提变了，下面的断言就不成立了"

    case = guard.get_case(store, TENANT, CASE)
    content = NOTIFY._default_content(
        case, NOTIFY._public_status(store, TENANT, CASE, case),
        NOTIFY._compensation_tail(store, TENANT, CASE, case))
    for word in FORBIDDEN:
        assert word not in content, f"没有观察却说了 {word}：{content}"


def test_the_notification_is_not_attached_to_any_task(rejected):
    """这条通知**不挂 business_ref** —— 它不是任何一个 DAG 任务跑出来的。

    挂到驳回那一跳（核算任务）上，trace 上就成了「核算任务发了条短信」；挂到那个
    PENDING 的 notify 任务上更糟，它压根没跑。`notify.customer` 自己那句
    `if plan_id and task_id` 会跳过 —— 本测试钉的就是调用方**真的**传了空 task_id。
    """
    store, plan_id = rejected["store"], rejected["result"]["plan_id"]
    refs = [r for r in objects.list_business_refs(store, plan_id=plan_id)
            if r["object_type"] == "notification"]
    assert refs == [], f"给一条编排层补发的通知挂了归属不实的引用：{refs}"


def test_the_skill_really_ran_so_confirm_can_look_it_up(rejected):
    """走的是 `SkillInvoker`，不是 `Skill().run()` —— 审计行在，`/confirm` 才查得到。

    直接调就没有白名单校验、没有那条 `SkillInvoked`，「客户到底被告知过没有」
    这句话就只剩自述。
    """
    store, plan_id = rejected["store"], rejected["result"]["plan_id"]
    hits = [e for e in store.list_event_log(plan_id)
            if e.get("event_type") == "SkillInvoked"
            and _detail(e).get("skill") == custom_case.SKILL_NOTIFY_CUSTOMER]
    assert len(hits) == 1, f"notify.customer 的审计行不是恰好一条：{len(hits)}"
    # 挂在 plan 上、task_id 为空：由 plan 那棵树收走，不落进 `stray_events`
    # （口径同 `custom_case.check_snapshot` 那次规划期调用）。
    assert hits[0]["plan_id"] == plan_id and not hits[0]["task_id"]


# ==========================================================================
# 2. 分界 —— 驳回付款闸 ≠ 驳回整个案子
# ==========================================================================
@pytest.fixture(scope="module")
def payment_rejected(tmp_path_factory):
    """网关明确失败，人只驳**付款那一闸**（`reject_roles`），核算照批。

    这是 `make_case_bundle.py` 的 `gateway_fail` 路径那组入参，不是本文件现编的。
    """
    from maos.agents.refund import ROLE_PAYMENT

    store = _store()
    result = custom_case.run_payload(
        _payload({ORDER: FAIL_CODE}), approve=True, verbose=False, store=store,
        reject_roles=(ROLE_PAYMENT,))
    return {"store": store, "result": result}


def test_rejecting_the_payment_gate_does_not_reject_the_whole_case(payment_rejected):
    """🔴 驳回付款闸时案子**不进 rejected** —— 钱的下落还没定，这一单没有被否决。

    `_reject_case()` 那条 submitted/approved 守卫就是这条分界。让它在这里也生效，
    等于对客户宣布「不予退款」，而实际上退款是批了的、只是钱没退出去 ——
    那两句话对客户完全相反。
    """
    store = payment_rejected["store"]
    status = guard.get_case(store, TENANT, CASE)["biz_status"]
    assert status != "rejected", "只驳了一道闸，整个案子却被判成不予退款"
    assert status == "gateway_accepted", f"这一跑的前提变了：biz_status={status}"


def test_no_customer_notice_yet_when_only_the_payment_gate_was_rejected(payment_rejected):
    """付款闸被驳的**那一刻**不通知客户 —— 该说什么要等补偿收口之后才知道。

    此刻工单还没开（补偿是「看过事实之后的决定」，由调用方在 `run_payload`
    之外下），一条通知都发不出有内容的。房间那条路上的告知补在 `/resolve` 之后
    （`router._tell_customer_after_resolve`，端到端判据在
    `test_room_outcome_commands.py`）。
    """
    assert _notifications(payment_rejected["store"]) == [], (
        "在钱的下落还没定的时候就告知了客户 —— 那句话必然是编的")


def test_both_gate_decisions_are_on_the_approval_table_with_their_own_task(payment_rejected):
    """审批表上两行：核算那跳 approved、付款那跳 rejected，各挂各的 `task_id`。

    「这一行落在哪一跳」全靠这条引用 —— 表上那六列里没有任何一列记得下它，
    而这正是不必为此加一列的理由（铁律 9）。
    """
    store, plan_id = payment_rejected["store"], payment_rejected["result"]["plan_id"]
    rows = _approvals(store)
    assert [r["decision"] for r in rows] == ["approved", "rejected"]

    refs = {r["object_version"]: r["task_id"]
            for r in objects.list_business_refs(store, plan_id=plan_id)
            if r["object_type"] == "approval_record"}
    assert len(refs) == 2, f"两次审批没有各自挂上引用：{refs}"
    assert refs[1].endswith("-finance"), f"第 1 次审批没指到核算那一跳：{refs}"
    assert refs[2].endswith("-payment"), f"第 2 次审批没指到付款那一跳：{refs}"


# ==========================================================================
# 3. 房间那条路：闸决定补落审批表
# ==========================================================================
class _Adapter:
    configured = True
    name = CHANNEL_FEISHU

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _router(store, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(_ledger({ORDER: FAIL_CODE}), ensure_ascii=False),
                    encoding="utf-8")
    ad = _Adapter()
    return IngressRouter({ad.name: ad}, store=store, ledger_path=path,
                         approvers=lambda: frozenset({BOSS, PAYOPS}))


def _msg(text: str, *, msg_id: str, sender: str = BOSS) -> InboundMessage:
    return InboundMessage(channel=CHANNEL_FEISHU, chat_id="oc_1", sender=sender,
                          text=text, msg_id=msg_id)


@pytest.fixture()
def room(tmp_path):
    """一单跑到付款闸停下来的房间链，停在人按键之前。"""
    store = _store()
    r = _router(store, tmp_path)
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="r1"))
    r.handle(_msg(f"/approve {CASE}", msg_id="r2"))
    return {"store": store, "router": r}


def test_the_room_rejection_lands_on_the_approval_table(room):
    """房间里那次驳回落进 `approval_record`，署**按键的那个人**、原文当理由。

    从前它只落 `event_log` 的迁移事件，退款域那张审批表上一行都没有 —— 于是
    「谁在什么时候驳回了这笔」在库里查不到，而客户投诉时要对的第一件事就是它。
    """
    store, r = room["store"], room["router"]
    before = len(_approvals(store))

    out = r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="r3"))
    assert "已驳回" in out

    rows = _approvals(store)
    assert len(rows) == before + 1, "房间那次驳回仍然没落进审批表"
    last = rows[-1]
    assert last["decision"] == "rejected"
    assert last["approver"] == BOSS, (
        f"署名不是按键的那个人：{last['approver']} —— 这一跳是 HITL trace 上"
        f"唯一一条『人在现场做的决定』")
    assert last["reason"] == "钱没退出去，不签这一步", "理由被改写了，不是房间里那句原话"


def test_the_room_rejection_does_not_flip_the_case_to_rejected(room):
    """🔴 房间那一跳**不碰 `biz_status`** —— 它驳的是这一步，不是这一单。

    房间这条路上补偿会紧接着开出来（`_compensate_if_stuck`），案子因此走到
    `compensated`。它**不是** `rejected`，两者差的正是「这一单办不办」这件事。
    """
    store, r = room["store"], room["router"]
    r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="r3"))

    status = guard.get_case(store, TENANT, CASE)["biz_status"]
    assert status != "rejected", "房间驳一道闸把整个案子判成了不予退款"
    assert status == "compensated", f"这一跑的前提变了：biz_status={status}"


def test_the_approval_row_points_at_the_payment_task(room):
    """那一行挂的引用指到**付款那一跳** —— 「驳回落在哪」的唯一判据。"""
    store, r = room["store"], room["router"]
    plan_id = r._case_row(CASE)["plan_id"]
    r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="r3"))

    refs = [ref for ref in objects.list_business_refs(store, plan_id=plan_id)
            if ref["object_type"] == "approval_record"]
    last = max(refs, key=lambda ref: ref["object_version"])
    assert last["task_id"].endswith("-payment"), f"驳回那一行没指到付款那一跳：{last}"
    assert "rejected" in last["purpose"]


def test_the_room_notice_is_attached_to_the_payment_step(room, tmp_path):
    """房间那条路上补发的通知**挂在付款那一跳**上 —— 与紧邻的补偿命令同一批。

    与闸循环那条刻意不同（那一条传空 `task_id`，见 `custom_case.notify_customer`）：
    这一条是补偿收口这一串命令的一部分，而 `_command_extras` 早就立了口径 ——
    房间里这几条命令都在收拾「钱没退出去」的尾巴，挂到别的任务上，
    「这一切是因为哪一步走不通」在 trace 里就断了。
    """
    store, r = room["store"], room["router"]
    plan_id = r._case_row(CASE)["plan_id"]
    r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="r3"))
    r.handle(_msg(f"/assign {TICKET} payment_ops", msg_id="r6"))
    out = r.handle(_msg(f"/resolve {TICKET} 20260911104500999 线下核对",
                        msg_id="r7", sender=PAYOPS))
    assert "已关单" in out and "告知客户" in out, f"关单或告知没生效：\n{out}"

    refs = [ref for ref in objects.list_business_refs(store, plan_id=plan_id)
            if ref["object_type"] == "notification"]
    assert len(refs) == 1, f"补发的通知没挂上引用（或挂了不止一条）：{refs}"
    assert refs[0]["task_id"].endswith("-payment"), (
        f"通知没挂在付款那一跳上，trace 里就串不起来：{refs[0]}")


def test_the_room_approval_lands_too(room):
    """放行那一支同样落表 —— 只落驳回的话，「查不到放行记录」就说不清是没发生还是没记。"""
    store, r = room["store"], room["router"]
    before = len(_approvals(store))

    r.handle(_msg(f"/approve {CASE} 已确认钱通过别的渠道到账", msg_id="r4"))

    rows = _approvals(store)
    assert len(rows) == before + 1
    assert rows[-1]["decision"] == "approved" and rows[-1]["approver"] == BOSS


def test_a_second_press_on_a_decided_gate_records_nothing(room):
    """已经决定过的闸再按一次：不落第二行审批。

    `human_decision` 会抛（`assert_transition`），而留痕补在它**之后** ——
    顺序反过来的话，审批表上会多出一行记着一件根本没生效的决定。
    """
    store, r = room["store"], room["router"]
    r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="r3"))
    after_first = len(_approvals(store))

    out = r.handle(_msg(f"/reject {CASE} 再按一次", msg_id="r5"))

    assert len(_approvals(store)) == after_first, f"给一次没生效的决定记了账：\n{out}"


# ==========================================================================
# 4. 闸循环的操作者透传
# ==========================================================================
def test_the_gate_operator_is_carried_into_the_approval_row():
    """给了 `gate_operator` 就署它 —— 核算那一跳不再是 CLI 写死的常量。"""
    store = _store()
    custom_case.run_payload(_payload(), approve=True, verbose=False, store=store,
                            gate_operator=BOSS)

    rows = _approvals(store)
    assert rows, "一行审批都没有 —— 这一跑没走到闸上，下面的判据不成立"
    assert all(r["approver"].startswith(f"{BOSS}（") for r in rows), (
        f"闸上的署名没换成调用方给的那个：{[r['approver'] for r in rows]}")


def test_without_a_gate_operator_the_cli_constant_is_unchanged():
    """🔴 不给就仍是 `APPROVER` —— CLI 缺省行为一个字节都不许变。

    变了的话，八个场景束与单案例四路径的证据字节会集体漂移，而那不是这一轨
    要交的东西（派单 §3.3）。
    """
    store = _store()
    custom_case.run_payload(_payload(), approve=True, verbose=False, store=store)

    rows = _approvals(store)
    assert rows, "一行审批都没有 —— 这一跑没走到闸上，下面的判据不成立"
    assert all(r["approver"].startswith(f"{custom_case.APPROVER}（") for r in rows), (
        f"不给 operator 时署名变了：{[r['approver'] for r in rows]}")


def test_the_plan_level_operator_is_a_different_knob():
    """`approval_operator`（计划级停靠）与 `gate_operator`（闸循环）不是一件事。

    同一次处置里完全可能是两个人：一个在 `create_plan` 与 `start_plan` 之间看方案，
    一个在 DAG 跑起来之后按闸。合成一个入参就再也表达不了这件事。
    """
    store = _store()
    result = custom_case.run_payload(
        _payload(), approve=True, verbose=False, store=store,
        plan_approval="approve", approval_operator="@planner:maos.local",
        gate_operator=BOSS)

    assert result["plan_approval"]["operator"] == "@planner:maos.local"
    assert all(r["approver"].startswith(f"{BOSS}（") for r in _approvals(store))


# ==========================================================================
# 5. 补偿收口那一段措辞 —— 换一种关单结论也不许碰到账口径
# ==========================================================================
def test_the_compensation_tail_is_empty_before_any_ticket_exists():
    """工单没开就返回空串 —— `MT-<case_id>` 算得出来，正因为算得出来才更要先确认它开过。

    给客户一个不存在的工单号，比少说一句话坏得多。
    """
    store = _store()
    custom_case.run_payload(_payload(), approve=False, verbose=False, store=store)
    case = guard.get_case(store, TENANT, CASE)

    assert NOTIFY._compensation_tail(store, TENANT, CASE, case) == ""


@pytest.mark.parametrize("resolution", [CP.RESOLUTION_SETTLED, CP.RESOLUTION_NOT_SETTLED])
def test_the_compensation_tail_never_announces_money(tmp_path, resolution):
    """🔴 两种关单结论下，补上的那一段都不含任何到账口径。

    `not_settled` 那一档（线下核对确认这笔确实没退出去）是派单点名的形态；
    `settled` 那一档更要钉：投影句此刻**说得出**「退款已到账」（有观察撑着，是真的），
    而这一段仍然只许说观察与安排 —— 它在两档里逐字相同，才证明它没在读结果。
    """
    store = _store()
    r = _router(store, tmp_path)
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="t1"))
    r.handle(_msg(f"/approve {CASE}", msg_id="t2"))
    r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步", msg_id="t3"))

    from maos.skills.invoker import SkillInvoker
    from maos.ingress import outcome_commands as OC

    res = SkillInvoker(OC.TICKET_DESK_IDENTITY, store).invoke(
        "refund.compensation_close", {
            "tenant_id": TENANT, "case_id": CASE, "operator": BOSS,
            "evidence_ref": "20260911104500999", "summary": "线下核对",
            "resolution_kind": resolution})
    assert res.status == "ok", f"关单没跑成：{res.error}"

    case = guard.get_case(store, TENANT, CASE)
    tail = NOTIFY._compensation_tail(store, TENANT, CASE, case)
    for word in FORBIDDEN:
        assert word not in tail, f"关单结论 {resolution} 下那一段说了 {word}：{tail}"
    assert f"工单 {TICKET}" in tail and "20260911104500999" in tail, (
        f"工单号或凭证流水没进正文，客户无从追问：{tail}")
