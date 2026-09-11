"""房间里的结果面四条命令（T122）——`/assign` `/resolve` `/confirm` `/complain`。

被测的是**一件事的两半**，少任何一半这四条命令都只是能解析的字符串：

  · **处置与命令共用一个库。** `handle_execute` 把 `store=self.store` 透给
    `custom_case.run_payload()`（经 `flows/common.build(store=)`）。在这之前，
    处置跑在 `run_payload` 自建又随手丢掉的 `:memory:` 里，于是工单、退款申请、
    到账观察从来不在 router 的库中，`/assign` 必撞 `require_ticket` 的 LookupError。
    这一层的测试不看回帖措辞，看**库里有没有那几行**。
  · **四个动词真的进了 router。** `KNOWN_VERBS` 与 `_dispatch` 同源，渠道闸与名单
    的判定顺序与 `/approve` 一致。

## 三条刻意钉住的边界

1. **先查你是谁，再谈你想干什么。** 名单外的账号打错一个单号，收到的必须是
   「无权限」并落一条 `ApprovalDenied`，不是「这个案子没在本房间跑过」——
   后者是一句免费的探测应答，等于告诉他换个案号再试就行。
2. **关单不许直写 settled**（铁律 8）。`/resolve` 经 `refund.compensation_close`
   → `payment.observe` 回填一条观察，权威仍在外部；测试查的是**那条观察**，
   不是 `case_outcome.arrival` 这个算出来的值。
3. **同一个案子在同一个库里只跑一次。** `task_id` 由 case_id 推出而不是 `new_id`
   （`flows/contrast.py::plan_tasks`），重跑必撞主键；更要紧的是重跑会给同一个
   案子产出第二套观察。router 拦在跑之前。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import objects, outcome as OUT, projection, roles
from maos.flows import custom_case
from maos.flows.common import build
from maos.ingress import outcome_commands as OC
from maos.ingress.contracts import (
    CHANNEL_FEISHU, CHANNEL_MATRIX, CHANNEL_WECHAT_KF, InboundMessage, OutboundMessage,
)
from maos.ingress.router import KNOWN_VERBS, IngressRouter
from maos.skills.builtin.refund import compensate as CP

#: 演示底账里那一单。质保期内质量问题 -> 批准 -> 走到付款。
ORDER = "ORD-2026-0001"
CASE = f"RC-{ORDER}"
TICKET = f"MT-{CASE}"
TENANT = "tnt-demo"

#: 网关明确失败的码。与 `scripts/make_case_bundle.py::GATEWAY_FAIL_CODE` 同一个 ——
#: 证据束里和房间里演的该是同一种失败，用两个码等于演了两件事。
FAIL_CODE = "ACQ.SELLER_BALANCE_NOT_ENOUGH"

BOSS = "@boss:maos.local"                 # after_sales_supervisor
PAYOPS = "@payops:maos.local"             # payment_ops 的主责人
OUTSIDER = "@intern:maos.local"           # 名单外
APPROVERS = frozenset({BOSS, PAYOPS})

#: 线下凭证。**第一个词是渠道流水号**（凭证引用），整句留档当摘要 —— 这条约定
#: 写在 `/resolve` 的用法里，也是这份人工凭证作为外部事实的出处。
EVIDENCE = "20260911104500999 支付宝商家后台已入账 6800.00 元"


class FakeAdapter:
    """记下发出去的消息。`configured` 恒真 —— 这一层测的不是凭证。"""

    configured = True

    def __init__(self, name: str = CHANNEL_FEISHU) -> None:
        self.name = name
        self.sent: list[OutboundMessage] = []

    def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _ledger(fail_orders: dict | None = None) -> dict:
    """演示底账 + 可选的 per-order 失败码注入。

    `fail_orders` 走的是 `custom_case._gateway_of` 认的那个键（T122 新增）——
    底账原有的 `gateway.fail_with` 是**整份底账**的开关，打开之后每一单都失败，
    演不了「一单卡住、其余照常」。
    """
    led = custom_case.load(
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "scenarios", "custom", "ledger.json"),
        require_case=False)
    if fail_orders is not None:
        led["gateway"] = {**led.get("gateway", {}), "fail_orders": dict(fail_orders)}
    return led


def _ledger_file(tmp_path, fail_orders: dict | None = None):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(_ledger(fail_orders), ensure_ascii=False), encoding="utf-8")
    return path


def _payload(order_id: str = ORDER, fail_orders: dict | None = None) -> dict:
    """底账 + 一行申请 -> 一份完整 case。走 `run_requests.build_case`，不手拼。"""
    rr = _run_requests()
    return rr.build_case(_ledger(fail_orders), {
        "order_id": order_id, "reason": "quality_defect", "amount": None,
        "requested_at": "2026-09-05T00:00:00+00:00"})


def _run_requests():
    from maos.ingress.router import _load_run_requests
    return _load_run_requests()


def _room_store() -> SqliteStore:
    """房间那个库：内核四张表 + 退款域的表 + 两处加列。顺序与 `wire()` 里一致。"""
    store = SqliteStore(":memory:")
    store.init_schema()
    objects.ensure_schema(store)
    CP.ensure_ticket_schema(store)
    OUT.ensure_outcome_schema(store)
    return store


def _router(store, ledger_path, *, approvers=APPROVERS, adapter=None):
    ad = adapter or FakeAdapter()
    return IngressRouter({ad.name: ad}, store=store, ledger_path=ledger_path,
                         approvers=lambda: frozenset(approvers)), ad


def _msg(text: str, *, sender: str = BOSS, channel: str = CHANNEL_FEISHU,
         msg_id: str = "") -> InboundMessage:
    return InboundMessage(channel=channel, chat_id="oc_1", sender=sender, text=text,
                          msg_id=msg_id or f"m-{abs(hash((text, sender))) % 1_000_000}")


# ==========================================================================
# 1. store 透传 —— 处置跑在给定的库上
# ==========================================================================
def test_build_uses_the_given_store_and_still_returns_the_frozen_six_tuple():
    """`build(store=…)` 用传进来的那个库；返回仍是六元组、位序类型不动（C-4）。"""
    mine = SqliteStore(":memory:")
    got = build({}, store=mine)

    assert len(got) == 6, "C-4：加一个入参不许把返回换成七元组"
    store, bus, cp, model, worker, gate = got
    assert store is mine, "给了库还自建一个，等于这个参数没接上"
    assert cp.store is mine, "ControlPlane 必须挂在同一个库上，否则 plan 落在别处"


def test_build_without_store_still_builds_its_own():
    """不给就照旧自建 —— 缺省行为逐字节不变。"""
    a = build({})[0]
    b = build({})[0]
    assert a is not b, "两次 build 该是两个独立的库"


def test_run_payload_leaves_the_refund_domain_rows_in_the_given_store():
    """`run_payload(store=…)` 之后，退款域那几张表就在这个库里 —— 四条命令的前提。"""
    store = _room_store()
    custom_case.run_payload(_payload(), approve=True, verbose=False, store=store)

    for table in ("refund_case", "refund_request", "payment_observation"):
        rows = objects.query(store, f"SELECT * FROM {table} WHERE case_id=?", (CASE,))
        assert rows, f"{table} 一行都没有 —— 处置又跑到别的库去了"


def test_run_payload_without_store_does_not_touch_the_room_db():
    """不给 `store=` 时行为与从前逐字节相同：房间的库里一行都不该多出来。"""
    store = _room_store()
    custom_case.run_payload(_payload(), approve=True, verbose=False)

    assert objects.query(store, "SELECT * FROM refund_case", ()) == []


# ==========================================================================
# 2. 共享库上的重复灌装与重跑
# ==========================================================================
def test_seed_case_is_idempotent_on_a_shared_store():
    """同一份底账在同一个库上灌两遍：不炸，行数不翻倍（契约 §B.3 的 DO NOTHING）。"""
    from maos.domain.refund import fixtures

    store = _room_store()
    payload = _payload()
    first = fixtures.seed_case(store, payload)
    second = fixtures.seed_case(store, payload)

    assert first == second, "两遍报的行数该一样 —— 报的是语料里有几行，不是写进去几行"
    rows = objects.query(store, "SELECT * FROM order_snapshot WHERE order_id=?", (ORDER,))
    assert len(rows) == 1, "重灌把同一张快照写成了两行"


def test_the_same_case_is_not_run_twice_in_the_same_room_db(tmp_path):
    """同一个案子第二次 `/approve`：不重跑、不炸、`refund_case` 仍是一行。

    拦在跑之前而不是让它去撞主键：`task_id` 由 case_id 推出（`contrast.plan_tasks`
    刻意如此，为了连跑两次输出逐条一致），第二遍必然 UNIQUE 冲突；而就算不冲突，
    重跑也会给同一个案子产出第二套付款请求与观察 —— 那件事的权威只能有一处。
    """
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))

    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="a1"))
    first = r.handle(_msg(f"/approve {CASE}", msg_id="a2"))
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="a3"))
    again = r.handle(_msg(f"/approve {CASE}", msg_id="a4"))

    assert "已放行" in first
    assert "已经在本房间跑过一次" in again
    assert len(objects.query(store, "SELECT * FROM refund_case WHERE case_id=?",
                             (CASE,))) == 1


# ==========================================================================
# 3. 四个动词进了 router
# ==========================================================================
@pytest.mark.parametrize("verb", OC.COMMANDS)
def test_every_outcome_verb_is_a_known_verb(verb):
    """少列一个词，那条命令带着图进来会多触发一轮证据复检（跨轨契约 §4）。"""
    assert verb in KNOWN_VERBS


def test_known_verbs_and_outcome_commands_stay_one_source():
    """两张词表同源：router 不重新定义一份命令词。"""
    from maos.ingress.router import CMD_OUTCOME

    assert CMD_OUTCOME is OC.COMMANDS


def test_case_id_of_knows_which_arg_is_a_ticket():
    """`/assign` `/resolve` 的第一个参数是工单号，另两条是案号。"""
    assert OC.case_id_of("assign", [TICKET, "payment_ops"]) == CASE
    assert OC.case_id_of("resolve", [TICKET, "x"]) == CASE
    assert OC.case_id_of("confirm", [CASE]) == CASE
    assert OC.case_id_of("complain", [CASE, "少了 200"]) == CASE
    assert OC.case_id_of("assign", []) == ""


# ==========================================================================
# 4. 权限面 —— 渠道闸、名单、承接人
# ==========================================================================
def test_outside_channel_cannot_send_outcome_commands(tmp_path):
    """外部渠道（客服号）发 `/assign` 被渠道闸拒 —— 与 `/approve` 同一道闸。"""
    store = _room_store()
    ad = FakeAdapter(CHANNEL_WECHAT_KF)
    r, _ = _router(store, _ledger_file(tmp_path), adapter=ad)

    out = r.handle(_msg(f"/assign {TICKET} payment_ops", channel=CHANNEL_WECHAT_KF))
    assert "不受理结果面命令" in out


def test_matrix_room_is_an_approval_channel(tmp_path):
    """Matrix 房间在闸内 —— 演示就在那里跑，被自己的闸拦住会很尴尬。"""
    store = _room_store()
    ad = FakeAdapter(CHANNEL_MATRIX)
    r, _ = _router(store, _ledger_file(tmp_path), adapter=ad)

    out = r.handle(_msg(f"/assign {TICKET} payment_ops", channel=CHANNEL_MATRIX))
    assert "不受理结果面命令" not in out


def test_outsider_is_denied_before_the_case_is_even_looked_up(tmp_path):
    """名单外的账号：回「无权限」并落 `ApprovalDenied`，**不许**先告诉他案子不存在。

    顺序反过来的话，那句「没在本房间跑过」就是一句免费的探测应答。
    """
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))

    out = r.handle(_msg(f"/assign {TICKET} payment_ops", sender=OUTSIDER))

    assert "无权限" in out and OUTSIDER in out
    assert "没在本房间跑过" not in out
    denied = [e for e in store.list_event_log("")
              if e.get("event_type") == OC.EVENT_COMMAND_DENIED]
    assert denied, "越权尝试一条痕都没留"


def test_unknown_case_is_reported_as_never_run_here(tmp_path):
    """名单内、但这个案子从没在本房间跑过 -> 一句人话，不猜。"""
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))

    out = r.handle(_msg("/assign MT-RC-ORD-9999 payment_ops"))
    assert "没在本房间跑过" in out and "RC-ORD-9999" in out


# ==========================================================================
# 5. 完整链路：/refund -> /approve -> /assign -> /resolve
# ==========================================================================
@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """跑一遍完整链路，把库、回帖、router 一起交出去。

    module 级：这条链路要真跑一个 Plan，下面十来条断言各跑一遍毫无必要地慢。
    """
    tmp = tmp_path_factory.mktemp("room")
    store = _room_store()
    path = tmp / "ledger.json"
    path.write_text(json.dumps(_ledger({ORDER: FAIL_CODE}), ensure_ascii=False),
                    encoding="utf-8")
    r, ad = _router(store, path)

    said: dict[str, str] = {}
    said["refund"] = r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="c1"))
    said["approve"] = r.handle(_msg(f"/approve {CASE}", msg_id="c2"))
    # **第二次人工决定**（T135）。网关回了终态失败之后，付款那一步停在控制面落的
    # `human_decision` 闸上 —— 群里那次 `/approve` 签的是「这一单要不要办」，
    # 签不到这里。工单也因此挪到这一步之后才开：补偿是「看过事实之后的决定」。
    said["reject"] = r.handle(_msg(f"/reject {CASE} 钱没退出去，不签这一步",
                                   msg_id="c2b"))
    said["assign"] = r.handle(_msg(f"/assign {TICKET} payment_ops", msg_id="c3"))
    said["resolve"] = r.handle(_msg(f"/resolve {TICKET} {EVIDENCE}",
                                    sender=PAYOPS, msg_id="c4"))
    return {"store": store, "said": said, "router": r, "adapter": ad}


def test_gateway_failure_opens_a_manual_ticket(chain):
    """网关明确失败 -> 开一张人工补偿工单，并把下一步说出来。

    开单挂在**第二次人工决定**之后（T135），不在 `/approve` 的回帖里。补偿是
    「看过事实之后的决定」（`_compensate_if_stuck` 的 docstring）——单先开出来、
    人再按 `/reject`，那次按键就只是给一张已经开好的单补签字，第二次决定成了
    走过场。
    """
    out = chain["said"]["reject"]
    assert "钱没退出去" in out and FAIL_CODE in out
    assert f"/assign {TICKET} payment_ops" in out, "不给下一步，房间里的人无从接手"

    ticket = CP.ticket_of(chain["store"], TENANT, CASE)
    assert ticket is not None, "工单没落库，`/assign` 就无单可派"


def test_the_approve_card_does_not_open_the_ticket_yet(chain):
    """🔴 `/approve` 那张卡**不许**宣布开单，也不许说这一单已经补偿过了。

    那一刻人还没做第二个决定。卡上写「已开补偿工单」等于替他宣布了一件他没做过
    的事；写「业务状态：已补偿」更糟 —— 库里那一刻是 `gateway_accepted`，卡片说的
    是一个尚未发生的未来。
    """
    out = chain["said"]["approve"]

    assert "已开人工补偿工单" not in out, f"决定还没做，卡片先把单开了：\n{out}"
    assert "业务状态：已补偿" not in out, f"卡片替这一单宣布了补偿：\n{out}"
    assert "停在人工闸上" in out and "再决定一次" in out, (
        f"停住了却不说要人再决定一次，那一单就没人动了：\n{out}")
    assert f"/reject  {CASE}" in out and f"/approve {CASE}" in out, (
        f"不给下一步的两条命令，人无从决定：\n{out}")


def test_assign_hands_the_ticket_to_payment_ops(chain):
    """`/assign` 把单交到支付运维手上，接单人取自角色目录。"""
    out = chain["said"]["assign"]
    assert "已派单" in out and roles.title_of("payment_ops") in out

    ticket = CP.ticket_of(chain["store"], TENANT, CASE)
    assert ticket["assignee_role"] == "payment_ops"
    assert ticket["assignee"] in roles.accounts_of("payment_ops")


def test_resolve_writes_an_observation_instead_of_writing_settled(chain):
    """铁律 8：关单经 `payment.observe` 回填一条观察，命令层一个字都不直写终态。

    查的是**那条观察**（它的来源必须是人工线下凭证），不是 `case_outcome.arrival`
    —— 后者是算出来的值，拿它当判据就等于用结论证明结论。
    """
    out = chain["said"]["resolve"]
    assert "已关单" in out and "observed_state=settled" in out

    obs = objects.query(
        chain["store"],
        "SELECT * FROM payment_observation WHERE case_id=? ORDER BY observed_at",
        (CASE,))
    last = obs[-1]
    assert last["observed_state"] == "settled"
    # 「来源是人工」写在**回执**里（`ManualReceiptAdapter` 包的那份 `gateway='manual'`），
    # 不在 `refund_request.gateway` 上：关单挂的是**原来那一笔**请求，不另造一笔假的
    # request_id（`compensation_close` 写明了这一点 —— 假 request_id 指不到任何真实请求）。
    receipt = json.loads(last["raw_receipt_json"] or "{}")
    assert receipt["detail"]["gateway"] == OUT.MANUAL_RECEIPT_SOURCE, (
        "回填的观察不自报人工来源 —— 那就分不清钱是网关退的还是人线下退的")
    assert receipt["detail"]["submitted_by"] == PAYOPS, "凭证的提交人没留下"
    assert receipt["detail"]["evidence_ref"] == EVIDENCE.split()[0], (
        "凭证引用不是摘要的第一个词 —— 那是这份人工凭证作为外部事实的出处")


def test_case_outcome_reads_compensated_and_settled(chain):
    """四判据：到账 settled、纠错 compensated，basis 指向刚才那条人工观察。"""
    row = OUT.read_case_outcome(chain["store"], tenant_id=TENANT, case_id=CASE)

    assert row["arrival"] == OUT.ARRIVAL_SETTLED
    assert row["manual_correction"] == OUT.CORRECTION_COMPENSATED
    obs = objects.query(
        chain["store"],
        "SELECT * FROM payment_observation WHERE case_id=? ORDER BY observed_at",
        (CASE,))
    assert row["arrival_basis"] == OUT.observation_basis(obs[-1])


def test_resolve_by_someone_who_is_not_the_assignee_is_denied(chain):
    """名单内、但不是这张单的承接人 -> 拒，且留痕。

    角色目录真正干活就在这一处：放行任何名单内账号替支付运维签字说钱退了，
    「应由谁补偿」这一问就又回到了没有答案的状态。
    """
    r, store = chain["router"], chain["store"]
    # 案子查得到时 router 会把 `plan_id` 一起递下去，于是拒绝那条事件挂在这个 plan
    # 下面（名单外那一条挂空串，因为查案子排在名单之后 —— 见本文件抬头第 1 条）。
    plan_id = r._case_row(CASE)["plan_id"]
    denied = lambda: len([e for e in store.list_event_log(plan_id)      # noqa: E731
                          if e.get("event_type") == OC.EVENT_COMMAND_DENIED])
    before = denied()

    out = r.handle(_msg(f"/resolve {TICKET} {EVIDENCE}", sender=BOSS, msg_id="c9"))

    assert "无权限" in out and "承接人" in out
    assert denied() == before + 1


# ==========================================================================
# 6. /confirm 与 /complain 真写表
# ==========================================================================
@pytest.fixture(scope="module")
def settled_chain(tmp_path_factory):
    """一单**钱真的退出去了**的链路：`/refund` -> `/approve`，没有第二道闸。

    与 `chain` 的差别只有一个：底账不注入失败码。于是网关回 settled、控制面不走
    第三出口、付款那一步一路 DONE，Plan 收在 DONE —— 顺带钉住 T135 **没有误伤**
    `effect_risk=H` 那一类闸（核算那道仍由处置流程代跑，卡上仍是「已放行」）。
    """
    tmp = tmp_path_factory.mktemp("settled")
    store = _room_store()
    r, ad = _router(store, _ledger_file(tmp))
    said = {"refund": r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="s1")),
            "approve": r.handle(_msg(f"/approve {CASE}", msg_id="s2"))}
    return {"store": store, "said": said, "router": r, "adapter": ad}


def test_confirm_records_the_customer_confirmation(settled_chain):
    """`/confirm` -> `customer_confirmation=confirmed`，并把四判据报回房间。

    跑在**钱真的退出去了**那条链上（T135 之后）。借补偿那条链测不行：付款被驳回
    之后 notify 这一步不再跑（DAG 上它依赖付款），客户一条通知都没收到，而
    `/confirm` 的前提正是「有通知发出去过」。借那条链测，测的就不是这条命令本身，
    而是它的前提碰巧还在 —— 那个前提在 T135 之前是**代签**出来的。
    """
    out = settled_chain["router"].handle(_msg(f"/confirm {CASE}", msg_id="s3"))

    assert "客户确认" in out and "customer_confirmation=confirmed" in out
    row = OUT.read_case_outcome(settled_chain["store"], tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == OUT.CONFIRMATION_CONFIRMED


def test_the_happy_path_still_runs_the_gates_the_old_way(settled_chain):
    """🔴 T135 只扣「跑起来之后才出现的闸」，`effect_risk=H` 那一类一个字没动。

    判错方向的代价是整条 happy 路径都停下来等人：演示时每一单都要按两次键，
    而第二次按的是一道本来就该由处置流程代跑的闸。
    """
    out = settled_chain["said"]["approve"]

    assert "任务级审批点 1 个" in out and "由处置流程代跑" in out
    assert "停在这里等你" not in out, f"happy 路径上不该有扣住的闸：\n{out}"
    assert "停在人工闸上" not in out
    assert "收在 DONE" in out, f"钱退出去了，Plan 就该收在 DONE：\n{out}"


def test_a_rejected_payment_leaves_the_customer_un_notified(chain):
    """🔴 付款被驳回之后 notify 这一步**不再跑** —— 客户一条通知都没收到。

    这是 T135 之后浮出来的真实缺口，不是回归：改造前那条通知是在一个**本不该
    完成**的任务（被代签的付款闸）跑完之后才发出去的。补偿路径上该怎么通知客户，
    归 `refund.compensation_close` 那一侧，本轨白名单外 —— 记在
    `docs/BACKLOG.md ## task-t135`，不当场改。

    钉住它是为了让这件事**有人知道**：不钉的话，下一个人只会看到 `/confirm`
    回一句「先跑 notify.customer」，而查不到它是从哪一步起不再发生的。
    """
    rows = objects.query(chain["store"],
                         "SELECT * FROM notification WHERE tenant_id=? AND case_id=?",
                         (TENANT, CASE))
    assert rows == [], "补偿路径上居然发出了通知 —— 那 BACKLOG 那条就该销了"

    out = chain["router"].handle(_msg(f"/confirm {CASE}", msg_id="c8"))
    assert "一条通知都没发出去" in out, f"缺口的症状变了，去核对 BACKLOG：\n{out}"


def test_complain_opens_a_complaint_and_sinks_business_success(chain):
    """`/complain` -> `complaint=open`，而 open 一票否决 `business_success`。

    这正是评委那句「投诉结果」该有的分量：钱到了、客户也签收了，只要投诉还开着，
    这单业务就没算成。回帖里必须看得到这一点，看不到等于没记。
    """
    out = chain["router"].handle(_msg(f"/complain {CASE} 收到的金额少了 200", msg_id="c6"))

    assert "complaint=open" in out and "业务是否成功=False" in out
    row = OUT.read_case_outcome(chain["store"], tenant_id=TENANT, case_id=CASE)
    assert row["complaint"] == OUT.COMPLAINT_OPEN
    assert not row["business_success"]
    assert OUT.list_complaints(chain["store"], tenant_id=TENANT, case_id=CASE)


def test_complain_without_content_only_replies_usage(chain):
    """只给案号不给内容：回用法，一行都不落库（空投诉回查不了）。"""
    store = chain["store"]
    before = len(OUT.list_complaints(store, tenant_id=TENANT, case_id=CASE))

    out = chain["router"].handle(_msg(f"/complain {CASE}", msg_id="c7"))

    assert "/complain" in out and "用法" in out
    assert len(OUT.list_complaints(store, tenant_id=TENANT, case_id=CASE)) == before


# ==========================================================================
# 7. 回帖措辞 —— 对客户口径那一行（跨轨契约 §D）
# ==========================================================================
def test_reply_carries_the_public_status_line(chain):
    """放行卡上多一行「对客户口径」，且**恰好**是补偿路径该说的那一句。

    从前这里只断言「在五个字面值里」，而那正好放过了 T126 那个 bug：卡片在
    补偿开单**之前**渲染，念出来的是 `PUBLIC_FILED`（已提出退款）—— 它也在
    五个里，于是断言全绿而回帖自相矛盾。判据松到「五选一」就等于没判。
    """
    # 念这一句的是**第二次决定**那张卡（T135）：补偿在那一步之后才开，
    # `/approve` 那张卡此刻只能说「已提出退款」，而那是一句真话，不是矛盾。
    out = chain["said"]["reject"]
    assert "对客户口径：" in out

    said = [ln.split("：", 1)[1] for ln in out.splitlines() if ln.startswith("对客户口径：")]
    assert said and said[0] == projection.PUBLIC_COMPENSATED, (
        f"这一单走的是补偿路径，对外只能说 {projection.PUBLIC_COMPENSATED!r}，"
        f"实际说的是 {said}")


def test_the_card_and_the_ticket_notice_tell_the_same_story(chain):
    """同一条回帖的上半截与下半截**不许打架**（T126）。

    补偿路径上，`_compensate_if_stuck` 会把 `biz_status` 推到 `compensated`；
    卡片若在那之前渲染，读到的是补偿**前**的库，于是同一条消息上半截说
    「已提交网关·未确认 / 已提出退款」，下半截说「钱没退出去，已开人工补偿工单」。
    评委肉眼可见，且库里那一刻已经是 compensated —— 卡片说的是过去式。

    所以这里三样一起钉：库里的终态、卡片上的两行、以及后半截的开单提示都在。
    """
    out = chain["said"]["reject"]
    case = CP.guard.get_case(chain["store"], TENANT, CASE) or {}
    assert case.get("biz_status") == "compensated", "前提没成立：这一跑没走到补偿"

    assert "钱没退出去" in out, "后半截的开单提示丢了，这条就不是在测矛盾"
    assert "业务状态：已补偿" in out, (
        f"卡片念的还是补偿前的业务状态 —— 与后半截的开单提示自相矛盾：\n{out}")
    assert "对客户口径：" + projection.PUBLIC_COMPENSATED in out, (
        f"卡片念的还是补偿前的对外口径：\n{out}")
    assert "对客户口径：" + projection.PUBLIC_FILED not in out


class _Silent(list):
    """一个不真跑 Plan 的处置器。`public_status` 由调用方指定，别的字段最小可渲染。"""

    def __init__(self, public_status: str = "") -> None:
        super().__init__()
        self.public_status = public_status

    def __call__(self, payload, **kw):
        self.append(payload)
        return {"case_id": CASE, "decision": "approve", "why": "x",
                "amount_approved": "1.00", "policy_version_used": 1,
                "rule_refs": "AS-002@v1", "biz_status": "submitted",
                "settled_observations": 0, "payment_observations": [],
                "human_exits": [], "plan_id": "plan-x", "plan_state": "DONE",
                "public_status": self.public_status}


def _card_with_public_status(tmp_path, public_status: str, *, tag: str) -> str:
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))
    r._runner = _Silent(public_status)
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id=f"{tag}1"))
    return r.handle(_msg(f"/approve {CASE}", msg_id=f"{tag}2"))


def test_no_public_status_says_so_instead_of_staying_silent(tmp_path):
    """🔴 投不出三态时照实说「还没到」—— 与圆桌事实卡**逐字同一句**（T129）。

    T129 之前这里整行不打，而圆桌那边打「尚未到可对外说的三态」：同一个案子在
    同一个房间里，主管从卡片上读不到任何对外说法，从圆桌那一段读到「还没到」。
    两张嘴现在共用 `router.public_status_line()`，不可能再分叉。
    """
    from maos.ingress.router import PUBLIC_STATUS_PENDING_LINE

    out = _card_with_public_status(tmp_path, projection.NO_PUBLIC_STATUS, tag="p")

    assert PUBLIC_STATUS_PENDING_LINE in out
    for literal in projection.PUBLIC_STATUSES:
        assert f"对客户口径：{literal}" not in out, "没投出来就不许念那五句里的任何一句"


def test_a_made_up_public_status_is_not_spoken_on_the_card(tmp_path, caplog):
    """🔴 来路不明的口径：卡片上**整行不打**，并留 WARNING。

    与空串那一档分开：空串时我们知道这一单还没到那三态；拿到一个不认识的字符串时
    我们不知道它到哪了（可能已经到账，只是标签写坏了），此时连「还没到」都不许说 ——
    那同样是替这个案子宣布一件没人核实过的事（铁律 8）。
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="maos.ingress.router"):
        out = _card_with_public_status(tmp_path, "钱已经打过去了", tag="q")

    assert "钱已经打过去了" not in out
    assert "对客户口径" not in out
    assert any("不在契约" in r.getMessage() for r in caplog.records), caplog.text


def test_the_card_and_the_roundtable_speak_through_the_same_helper(tmp_path):
    """🔴 同一个案子，回帖卡与圆桌事实卡的那一行逐字相同（两张嘴一个口径）。

    `/approve` 的回帖卡走 `IngressRouter._render`，圆桌财务岗走
    `roundtable/stages.py::facts_finance_result` —— 两处都只从
    `router.public_status_line()` 取那一行，本测试并排比对两者的产出。
    """
    from maos.ingress.router import public_status_line
    from maos.roundtable import stages

    for public in (projection.PUBLIC_COMPENSATED, projection.NO_PUBLIC_STATUS):
        card = _card_with_public_status(tmp_path, public, tag=f"s{len(public)}")
        facts, _ = stages.facts_finance_result(
            {"case_id": CASE, "amount_approved": "1.00", "policy_version_used": 1,
             "rule_refs": "AS-002@v1", "biz_status": "submitted",
             "settled_observations": 0, "payment_observations": [], "human_exits": [],
             "plan_state": "DONE", "public_status": public})

        on_card = [ln for ln in card.splitlines() if ln.startswith("对客户口径")]
        at_table = [ln for ln in facts.splitlines() if ln.startswith("对客户口径")]
        assert on_card == at_table == [ln for ln in [public_status_line(public)] if ln], (
            f"public_status={public!r} 时两张嘴说得不一样：卡片 {on_card}／圆桌 {at_table}")


# ==========================================================================
# 8. 不该补偿的时候不补偿
# ==========================================================================
def test_a_settled_case_does_not_open_a_ticket(tmp_path):
    """钱退成了就不开工单 —— 对一笔已经到账的退款开补偿单是凭空多一笔账。"""
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))

    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="s1"))
    out = r.handle(_msg(f"/approve {CASE}", msg_id="s2"))

    assert "钱没退出去" not in out
    assert CP.ticket_of(store, TENANT, CASE) is None
    assert "对客户口径：" + projection.PUBLIC_SETTLED in out


def test_unobserved_outcome_does_not_trigger_compensation(tmp_path):
    """下落不明 ≠ 失败：最后一条观察不是 `failed` 时一律不补偿（铁律 8）。

    对一笔可能已经退出去的钱再退一次，比不补偿贵得多。
    """
    store = _room_store()

    class Unknown(list):
        def __call__(self, payload, **kw):
            self.append(payload)
            return {"case_id": CASE, "decision": "approve", "why": "x",
                    "amount_approved": "1.00", "policy_version_used": 1,
                    "rule_refs": "AS-002@v1", "biz_status": "gateway_accepted",
                    "settled_observations": 0,
                    "payment_observations": [{"observed_state": "unknown"}],
                    "human_exits": [], "plan_id": "plan-u", "plan_state": "DONE",
                    "public_status": projection.PUBLIC_PROCESSING, "tasks": []}

    r, _ad = _router(store, _ledger_file(tmp_path))
    r._runner = Unknown()
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="u1"))
    out = r.handle(_msg(f"/approve {CASE}", msg_id="u2"))

    assert "钱没退出去" not in out
    assert CP.ticket_of(store, TENANT, CASE) is None


# ==========================================================================
# 9. 两个 identity
# ==========================================================================
def test_ticket_desk_identity_matches_the_cli_one():
    """房间与 CLI 给的是**同一份**授权 —— 不然「群里能做、命令行不能做」就成了
    一条没人说得清的差异。"""
    from maos.flows import scenario_7 as s7

    assert OC.TICKET_DESK_IDENTITY == s7.TICKET_DESK_IDENTITY


def test_compensation_desk_identity_is_separate_from_the_ticket_desk():
    """开单与关单是两个岗，两份最小授权不许合并成一个全能 identity。"""
    from maos.flows import scenario_7 as s7

    assert OC.COMPENSATION_DESK_IDENTITY == s7.COMPENSATION_IDENTITY
    assert OC.SKILL_COMPENSATE not in OC.TICKET_DESK_IDENTITY.allowed_skills, (
        "关单的那个 identity 拿到了开单权限 —— 开单的人就能自己签字说钱退了")
    assert OC.SKILL_COMPENSATION_CLOSE not in OC.COMPENSATION_DESK_IDENTITY.allowed_skills


# ==========================================================================
# 10. 底账里的 per-order 失败码
# ==========================================================================
def test_fail_orders_only_fails_the_registered_order():
    """`gateway.fail_orders` 只让登记的那一单失败，同一份底账里别的单照旧到账。"""
    fail = custom_case._gateway_of(_payload(ORDER, {ORDER: FAIL_CODE}), fail_with=None)
    spared = custom_case._gateway_of(_payload("ORD-2026-0002", {ORDER: FAIL_CODE}),
                                     fail_with=None)

    assert fail.script.get(ORDER) == FAIL_CODE
    assert not spared.script, "没登记的单被牵连了 —— 顺利路径当场没了"


def test_ledger_without_fail_orders_behaves_exactly_as_before():
    """底账不写这个键时行为逐字节不变。"""
    plain = custom_case._gateway_of(_payload(), fail_with=None)
    assert not plain.script


def test_explicit_fail_with_still_wins():
    """入参 `fail_with` 仍然优先（CLI 的 `--fail-with` 一个字没变）。"""
    gw = custom_case._gateway_of(_payload(ORDER, {ORDER: FAIL_CODE}),
                                 fail_with="ACQ.SYSTEM_ERROR")
    assert gw.script.get(ORDER) == "ACQ.SYSTEM_ERROR"


# ==========================================================================
# 11. wire() 把库建齐，且 MAOS_INGRESS_DB 指到文件时跨进程查得到
# ==========================================================================
def test_wire_prepares_the_refund_domain_tables(tmp_path, monkeypatch):
    """`wire()` 之后，四条命令要读的表与列都在 —— 早于第一次 `/approve`。"""
    from hiclaw import room_ingress

    class _Channel:
        def listen(self, on_message, on_attachment) -> None:
            self.hooked = (on_message, on_attachment)

        def send(self, plain, html=None) -> None:
            pass

    db = tmp_path / "room.db"
    monkeypatch.setenv("MAOS_INGRESS_DB", str(db))
    r = room_ingress.wire(_Channel(), room_id="!r:maos.local")

    # 加列探针跑过了：`compensation_record` 上有工单那几列。
    cols = {c for _t, c, _d in CP.ticket_columns()}
    have = {row["name"] for row in objects.query(
        r.store, "SELECT name FROM pragma_table_info('compensation_record')", ())}
    assert cols <= have
    assert OUT.has_outcome_table(r.store)


def test_ticket_survives_into_a_second_process(tmp_path):
    """`MAOS_INGRESS_DB` 指到文件：跑完一单，**另一个进程**查得到那张工单。

    走子进程而不是重开一个 `SqliteStore`：验的是「演示完第二天还能把证据链翻出来」
    这件事，而那需要字节真的落在磁盘上。
    """
    db = tmp_path / "room.db"
    ledger = _ledger_file(tmp_path, {ORDER: FAIL_CODE})
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {root!r})
        from maos.tests.test_room_outcome_commands import (
            _room_store, _router, _msg, ORDER, CASE)
        from maos.core.store import SqliteStore
        from maos.domain.refund import objects, outcome as OUT
        from maos.skills.builtin.refund import compensate as CP

        store = SqliteStore({str(db)!r})
        store.init_schema()
        objects.ensure_schema(store)
        CP.ensure_ticket_schema(store)
        OUT.ensure_outcome_schema(store)
        r, _ = _router(store, {str(ledger)!r})
        r.handle(_msg(f"/refund {{ORDER}} 质量问题", msg_id="w1"))
        r.handle(_msg(f"/approve {{CASE}}", msg_id="w2"))
        r.handle(_msg(f"/reject {{CASE}} 钱没退出去", msg_id="w3"))
    """)
    proc = subprocess.run([sys.executable, "-c", script], cwd=root,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    later = SqliteStore(str(db))
    assert CP.ticket_of(later, TENANT, CASE) is not None, (
        "工单没活过进程 —— MAOS_INGRESS_DB 那条路径白铺了")


# ==========================================================================
# 12. 没有误伤
# ==========================================================================
def test_small_talk_is_still_ignored(tmp_path):
    """四个新动词进表之后，闲聊仍然一声不吭。"""
    store = _room_store()
    ad = FakeAdapter()
    r, _ = _router(store, _ledger_file(tmp_path), adapter=ad)

    assert r.handle(_msg("今天午饭吃什么")) == ""
    assert ad.sent == []


def test_an_unknown_slash_command_is_still_not_taken_over(tmp_path):
    """不是我们的命令词照旧不接管 —— 房间里可能还有别的 bot。"""
    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path))

    assert r.handle(_msg("/deploy prod")) == ""


# ==========================================================================
# 13. 补开工单（T129）—— 自动开单失败之后房间里唯一的救
# ==========================================================================
def _ticket_rows(store) -> list[dict]:
    """这个案子落了几行人工工单。**按行数判，不按单号判**：单号由案号推出，
    补开两次也只有一个号，但会有两行 —— 而第二行会把派单/关单的结果甩在前一行上。"""
    return objects.query(
        store,
        "SELECT * FROM compensation_record WHERE tenant_id=? AND case_id=? AND kind=?",
        (TENANT, CASE, CP.KIND_MANUAL_TICKET))


@pytest.fixture
def stuck(tmp_path, monkeypatch):
    """网关失败、**且自动开单也失败**的一单 —— T129 之前的死局。

    让 `refund.compensate` 在注册表里消失来造这个失败：比起去 mock 一个异常，
    它走的是 invoker 真实的 `skill_not_found` 分支，回帖措辞也是真实的那一句。
    """
    from maos.skills import registry

    store = _room_store()
    r, _ad = _router(store, _ledger_file(tmp_path, {ORDER: FAIL_CODE}))

    real = registry.get
    monkeypatch.setattr(
        registry, "get",
        lambda name, version=None: None if name == "refund.compensate"
        else real(name, version))
    said = {"refund": r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="k1")),
            "approve": r.handle(_msg(f"/approve {CASE}", msg_id="k2"))}
    # 开单是在**第二次人工决定**之后才发生的（T135），所以这个 fixture 要跑到那里
    # 才谈得上「自动开单也失败」。
    said["reject"] = r.handle(_msg(f"/reject {CASE} 钱没退出去", msg_id="k2b"))
    monkeypatch.undo()                                  # 之后的 /compensate 要真开单
    return {"store": store, "router": r, "said": said}


def test_a_failed_auto_open_leaves_no_ticket_and_points_at_the_way_out(stuck):
    """🔴 开单失败的回帖：说实话、不回显异常类名、**给出补开那条命令**。

    T129 之前这里只说「请人工介入」，而房间里当时无路可走：`/approve` 被「不重跑」
    拦死，`/assign` `/resolve` 都走 `require_ticket`（明写不补开）。长命库里这种
    案子只能换库 —— 演示当场撞上就没救。
    """
    out = stuck["said"]["reject"]

    assert "补偿工单没开出来" in out
    assert f"/compensate {CASE}" in out, "不给入口的「请人工介入」等于没说"
    assert "Error:" not in out and "Exception:" not in out, f"回帖里有异常类名：\n{out}"
    assert _ticket_rows(stuck["store"]) == [], "开单明明失败了，库里却有单"


def test_compensate_reopens_the_ticket_that_never_got_opened(stuck):
    """🔴 `/compensate` 把那张没开出来的单补上，并说出下一步。"""
    store, r = stuck["store"], stuck["router"]

    out = r.handle(_msg(f"/compensate {CASE}", msg_id="k3"))

    assert "已补开补偿工单" in out and TICKET in out
    assert f"/assign {TICKET} payment_ops" in out, "补开完不给下一步，人还是接不了"
    rows = _ticket_rows(store)
    assert len(rows) == 1, f"补开落了 {len(rows)} 行工单"
    assert CP.ticket_of(store, TENANT, CASE) is not None


def test_compensate_twice_still_opens_exactly_one_ticket(stuck):
    """🔴 幂等：连打两次不许开出两张单，第二次照实说「不用补开」并指回那张单。"""
    store, r = stuck["store"], stuck["router"]

    first = r.handle(_msg(f"/compensate {CASE}", msg_id="k4"))
    again = r.handle(_msg(f"/compensate {CASE}", msg_id="k5"))

    assert "已补开补偿工单" in first
    assert "不用补开" in again and TICKET in again
    assert len(_ticket_rows(store)) == 1, "第二次把同一个案子开成了两行工单"


def test_compensate_from_outside_the_approver_list_is_denied_and_recorded(stuck):
    """🔴 名单外的人补不了单，且这次越权要留痕 —— 与另四条命令同一道闸。"""
    store, r = stuck["store"], stuck["router"]

    out = r.handle(_msg(f"/compensate {CASE}", sender=OUTSIDER, msg_id="k6"))

    assert "无权限" in out and OUTSIDER in out
    assert _ticket_rows(store) == [], "名单外的人把单开出来了"
    denied = objects.query(
        store, "SELECT * FROM event_log WHERE event_type=? AND detail LIKE ?",
        (OC.EVENT_COMMAND_DENIED, f'%"command": "{OC.CMD_COMPENSATE}"%'))
    assert denied, "越权补开没留痕 —— 拒绝本身就是要拿给评委看的证据"


def test_compensate_on_an_outside_channel_is_refused_by_the_channel_gate(tmp_path):
    """渠道闸在最前面：客服号发 `/compensate` 与发 `/assign` 一样被拒。"""
    store = _room_store()
    ad = FakeAdapter(CHANNEL_WECHAT_KF)
    r, _ = _router(store, _ledger_file(tmp_path), adapter=ad)

    out = r.handle(_msg(f"/compensate {CASE}", channel=CHANNEL_WECHAT_KF))
    assert "不受理结果面命令" in out


def test_compensate_is_a_known_verb_without_router_redefining_it():
    """第五个动词进 router 靠的是同一份词表，router 那边一个字都没重新定义。"""
    from maos.ingress.router import CMD_OUTCOME

    assert OC.CMD_COMPENSATE in KNOWN_VERBS
    assert CMD_OUTCOME is OC.COMMANDS
    assert OC.case_id_of(OC.CMD_COMPENSATE, [CASE]) == CASE, "补开吃的是案号，不是单号"


def test_pending_kind_is_gone_and_kinds_refuses_it():
    """`KIND_PENDING` 已删（T122 之后再没人产出过它）；`KINDS` 也不再认这个值。"""
    assert not hasattr(OC, "KIND_PENDING")
    assert OC.KINDS == frozenset({OC.KIND_DONE, OC.KIND_DENIED, OC.KIND_USAGE,
                                  OC.KIND_IGNORED})
    with pytest.raises(ValueError):
        OC.CommandResult(kind="pending", text="x")


# ==========================================================================
# 14. 回帖是人话，不是异常日志（T129）
# ==========================================================================
def test_resolving_a_closed_ticket_replies_in_plain_words(chain):
    """🔴 重复 `/resolve` 的回帖不许带 `ValueError:` —— 那是一次**被正确拒绝**的
    重复操作，不是崩溃，而房间里看到一行 Python 异常名只会以为系统炸了。"""
    out = chain["router"].handle(_msg(f"/resolve {TICKET} {EVIDENCE}",
                                      sender=PAYOPS, msg_id="h1"))

    assert "关单未生效" in out
    assert "ValueError" not in out and "Error:" not in out, f"回帖里有异常类名：\n{out}"
    # 人话那半截得**原样**留着：剃掉的只有 invoker 拼的类名前缀，不是整句重写。
    # （这一句来自 `compensation_close.py` 自己的那道闸，比 `resolve_ticket` 里那句早。）
    assert "已于" in out and "重复关单会盖掉已回填的观察" in out


def test_humanize_keeps_the_invoker_failure_codes_intact():
    """`skill_not_found:<名字>` 与 `precondition_failed:<字段>` 是**码**，不许被剃掉前缀
    —— 去掉它们就查不到这次失败的出处了。判据是「冒号后面有没有空格」。"""
    assert OC.humanize("ValueError: 工单已关闭") == "工单已关闭"
    assert OC.humanize("skill_not_found:refund.compensate") == "skill_not_found:refund.compensate"
    assert OC.humanize("precondition_failed:tenant_id,case_id") == (
        "precondition_failed:tenant_id,case_id")
    assert OC.humanize(None) == "未知原因"


# ==========================================================================
# 15. 事件挂得住（T129）
# ==========================================================================
def _events(store, event_type: str) -> list[dict]:
    return objects.query(store, "SELECT * FROM event_log WHERE event_type=?",
                         (event_type,))


def test_room_commands_hang_their_events_on_the_same_trace(chain):
    """🔴 `/assign` 与 `/resolve` 落的事件带 `trace_id` 与 `task_id`。

    从前 `handle_outcome` 的 extras 只有 `plan_id`，于是房间里这一串命令在事件表里
    像是另一件事的记录：按 trace 串「这一单发生过什么」时，DAG 那一半串得起来，
    命令这一半接不上去。`task_id` 取**付款那一步**的，口径逐字同
    `router._compensate_if_stuck` —— 挂到别的任务上，「补偿是因为哪一步走不通」就断了。
    """
    store = chain["store"]
    plan_id = objects.query(store, "SELECT plan_id FROM refund_case WHERE case_id=?",
                            (CASE,))[0]["plan_id"]

    for event_type in (CP.EVENT_COMPENSATION_ASSIGNED, CP.EVENT_COMPENSATION_RESOLVED):
        rows = _events(store, event_type)
        assert rows, f"{event_type} 一条都没落"
        row = rows[-1]
        assert row["plan_id"] == plan_id
        assert row["trace_id"], f"{event_type} 的 trace_id 是空的"
        assert str(row["task_id"] or "").endswith("-payment"), (
            f"{event_type} 的 task_id 没挂在付款那一步上：{row['task_id']!r}")


def test_case_outcome_computed_still_only_carries_the_plan_id(chain):
    """`CaseOutcomeComputed` **只有 `plan_id`** —— 这一条 T129 补不了，钉住现状。

    它由 `maos/domain/refund/outcome.py::record_case_outcome` 落，而那个文件在
    跨轨契约 §A 的禁动面上（`{projection,objects,guard,outcome}.py` 一个字不许动）；
    它 append 事件时压根没有 `trace_id` / `task_id` 这两个键，命令层把 extras 填满
    也传不进去。钉成测试而不是只写进 BACKLOG：不钉的话「三个事件里有一个挂不上」
    这件事会随着下次有人读代码重新发现一遍。
    """
    rows = _events(chain["store"], OUT.EVENT_OUTCOME_COMPUTED)
    assert rows, "CaseOutcomeComputed 一条都没落"
    assert rows[-1]["plan_id"], "plan_id 是它今天唯一挂得上的一格"
    assert not rows[-1]["trace_id"], (
        "trace_id 居然有值了 —— outcome.py 变了，本测试与 BACKLOG 那条要一起改")


# ==========================================================================
# 16. 两个房间入口建表口径一致（T129）
# ==========================================================================
def _tables(store) -> set[str]:
    return {r["name"] for r in objects.query(
        store, "SELECT name FROM sqlite_master WHERE type='table'", ())}


def test_both_room_entrypoints_build_the_same_schema(tmp_path, monkeypatch):
    """🔴 `scripts/run_ingress.py::_store()` 与 `hiclaw/room_ingress.py::wire()`
    建出同一副表，**且两处都是经 `router.ensure_room_schema()` 建的**。

    从前 `_store()` 只有 `init_schema()`，`wire()` 还多三句退款域的 ensure ——
    同样是「起房间」，两个入口的库形状不一样。今天靠 Skill 层懒建表撑住不崩，
    但读代码的人会在「`/assign` 在这个入口能用吗」这一问上卡住。

    ## 为什么加第二段（T136）

    T129 抽出 `ensure_room_schema()` 时只改了 `run_ingress.py`（`hiclaw/**` 在那一轨的
    白名单外），`wire()` 仍是自己写的四句，于是这条测试的断言比它自己的注释弱一档：
    表集合相等**抓不到**「两处各写各的、碰巧建出同一副表」。那正是当时的真实状态，
    也是下一次分叉的起点 —— 有人往 `ensure_room_schema()` 里加第五句 ensure，
    只有走它的那个入口会有新表，而给两边都手补一句的人会让这条断言重新变绿。

    所以第二段直接断源码里出现那个调用：判据从「结果碰巧相同」升成「同一份口径」。
    表集合那条**不删**：它盯的是运行时的真实结果，源码断言盯的是写法，
    两者各抓一半（只断源码时，`ensure_room_schema` 自己建漏了表不会红）。
    """
    import importlib.util
    import inspect

    from hiclaw import room_ingress

    class _Channel:
        def listen(self, on_message, on_attachment) -> None:
            pass

        def send(self, plain, html=None) -> None:
            pass

    spec = importlib.util.spec_from_file_location(
        "_t129_run_ingress",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "scripts", "run_ingress.py"))
    run_ingress = importlib.util.module_from_spec(spec)
    sys.modules["_t129_run_ingress"] = run_ingress
    spec.loader.exec_module(run_ingress)

    monkeypatch.setenv("MAOS_INGRESS_DB", str(tmp_path / "room.db"))
    wired = room_ingress.wire(_Channel(), room_id="!r:maos.local").store

    assert _tables(run_ingress._store()) == _tables(wired), (
        "两个房间入口的建表口径又分叉了 —— 两处都该走 router.ensure_room_schema()")

    for fn in (room_ingress.wire, run_ingress._store):
        src = inspect.getsource(fn)
        assert "ensure_room_schema(" in src, (
            f"{fn.__module__}.{fn.__qualname__} 不再走 router.ensure_room_schema() ——"
            " 上面那条表集合相等只能抓到「一边建了另一边没建」，抓不到「两处各写"
            " 各的、今天碰巧一样」。建表口径归一个函数，不归两处各自的手抄")
        assert "ensure_schema(store)" not in src.replace("ensure_room_schema(store)", ""), (
            f"{fn.__module__}.{fn.__qualname__} 里又出现了单独一句域 ensure ——"
            " 要加表就加进 router.ensure_room_schema()，那里有顺序约束的说明")


def test_ensure_room_schema_is_idempotent():
    """连跑两次无副作用：`wire()` 对一个已经存在的文件库就是这么用的。"""
    from maos.ingress.router import ensure_room_schema

    store = SqliteStore(":memory:")
    ensure_room_schema(store)
    before = _tables(store)
    ensure_room_schema(store)

    assert _tables(store) == before
    cols = {c for _t, c, _d in CP.ticket_columns()}
    have = {row["name"] for row in objects.query(
        store, "SELECT name FROM pragma_table_info('compensation_record')", ())}
    assert cols <= have, "加列探针没跑到 —— 三句 ensure 的顺序被换了？"
