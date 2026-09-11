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
    said["assign"] = r.handle(_msg(f"/assign {TICKET} payment_ops", msg_id="c3"))
    said["resolve"] = r.handle(_msg(f"/resolve {TICKET} {EVIDENCE}",
                                    sender=PAYOPS, msg_id="c4"))
    return {"store": store, "said": said, "router": r, "adapter": ad}


def test_gateway_failure_opens_a_manual_ticket(chain):
    """网关明确失败 -> 开一张人工补偿工单，并把下一步说出来。"""
    out = chain["said"]["approve"]
    assert "钱没退出去" in out and FAIL_CODE in out
    assert f"/assign {TICKET} payment_ops" in out, "不给下一步，房间里的人无从接手"

    ticket = CP.ticket_of(chain["store"], TENANT, CASE)
    assert ticket is not None, "工单没落库，`/assign` 就无单可派"


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
def test_confirm_records_the_customer_confirmation(chain):
    """`/confirm` -> `customer_confirmation=confirmed`，并把四判据报回房间。"""
    out = chain["router"].handle(_msg(f"/confirm {CASE}", msg_id="c5"))

    assert "客户确认" in out and "customer_confirmation=confirmed" in out
    row = OUT.read_case_outcome(chain["store"], tenant_id=TENANT, case_id=CASE)
    assert row["customer_confirmation"] == OUT.CONFIRMATION_CONFIRMED


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
    """放行卡上多一行「对客户口径」，取值必须是 `projection.PUBLIC_*` 五个之一。"""
    out = chain["said"]["approve"]
    assert "对客户口径：" in out

    said = [ln.split("：", 1)[1] for ln in out.splitlines() if ln.startswith("对客户口径：")]
    assert said and said[0] in projection.PUBLIC_STATUSES, (
        f"对外口径 {said} 不在契约 §D 那五个字面值里 —— 又拼了第二处")


def test_public_status_line_is_skipped_when_there_is_none(tmp_path):
    """没有对外口径就不打这一行 —— 硬打一行空的等于替这个案子编了一句对外的话。"""
    store = _room_store()

    class Silent(list):
        def __call__(self, payload, **kw):
            self.append(payload)
            return {"case_id": CASE, "decision": "approve", "why": "x",
                    "amount_approved": "1.00", "policy_version_used": 1,
                    "rule_refs": "AS-002@v1", "biz_status": "submitted",
                    "settled_observations": 0, "payment_observations": [],
                    "human_exits": [], "plan_id": "plan-x", "plan_state": "DONE",
                    "public_status": ""}

    r, _ad = _router(store, _ledger_file(tmp_path))
    r._runner = Silent()
    r.handle(_msg(f"/refund {ORDER} 质量问题", msg_id="p1"))
    out = r.handle(_msg(f"/approve {CASE}", msg_id="p2"))

    assert "对客户口径" not in out


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
