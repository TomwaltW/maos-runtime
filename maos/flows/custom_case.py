"""自定义退款 case —— 把一份**你自己写的 JSON** 跑成一次真实处置。

    python3 scripts/run_case.py <你的 case.json>

## 与三组对照（`maos/flows/contrast.py`）的分工

`contrast.py` 跑的是**带判据**的对照实验：case json 必须有 `_expected` 块，
跑完逐条比对，不符即抛 —— 它回答「结论对不对」。本模块回答另一个问题：
「**换成我的数据，它会怎么判**」。于是：

  · 不要求 `_expected`，也不做任何判据比对 —— 你的数据没有标准答案；
  · 政策视图、指令展开、窗口判定、DAG 骨架**逐个复用** `contrast.py` 的函数，
    一行都不另抄（两套口径迟早分叉，症状是「同一份数据两个结论」，且不报错）；
  · 在对照骨架之后**补上支付段**（发起 -> 轮询观察）：对照实验用不着它，
    而「钱到没到账」正是自定义 case 要看的那个结果。

## 输出里哪些是观察、哪些是推断

`biz_status == "settled"` **只可能由 payment.observe 写入**（铁律 8）。
网关问不出终态时本模块什么都不写，案子停在 `gateway_accepted`，
输出里 `settled_observations` 就是 0 —— 这不是 bug，是设计。
注入了失败码的那一跑同理：本模块**不做域内补偿**（那是场景 7 的面），
它只如实报出案子停在哪、Plan 收在什么状态。
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from maos.agents.base import AgentIdentity
from maos.agents.manager import ManagerAgent
from maos.agents.refund import ROLE_FINANCE, ROLE_PAYMENT
from maos.contracts.events import new_id
from maos.contracts.states import TaskState
from maos.core.control_plane import AWAIT_HUMAN_DECISION
from maos.domain.refund import case_pack, fixtures, guard, objects, projection
from maos.flows import contrast
from maos.flows.common import build, dump, run_until_settled
from maos.model.client import Tier, select_model_client
from maos.runtime.gate import HumanApprovalQueue
from maos.runtime.plan_approval import PlanApprovalQueue
from maos.skills.builtin.refund import _common as C
from maos.skills.builtin.refund import snapshot_check
from maos.skills.invoker import SkillInvoker

log = logging.getLogger("maos.custom_case")
from maos.tools import order as order_tools
from maos.tools.gateway import MockGateway

#: 网关按名取：`task.inputs` 会被 json.dumps，实例塞不进去（`_common.py` 第 3 条）。
GATEWAY_NAME = "custom-case"

#: 订单系统按名取，同上。
ORDER_SYSTEM_NAME = "custom-case-orders"

SKILL_SNAPSHOT_CHECK = "refund.snapshot_check"

#: 跑「执行前读外部当前版本」这一步的调用身份。
#:
#: 与 `scenario_7.COMPENSATION_IDENTITY` 同一条口径：这一步发生在**规划期**，
#: 不属于任何一个 Agent 的 ctx —— 那时 DAG 还没排出来，付款岗自然也还没上场。
#: 造一个最小授权的 identity 走 `SkillInvoker`，而不是直接 `Skill().run()`：
#: 直接调就没有白名单校验、没有 SkillInvoked 审计行，「执行前真的读了一次」
#: 这句话就只剩自述 —— 而那正是评委要看的那半句。
SNAPSHOT_IDENTITY = AgentIdentity(
    agent_id="refund-snapshot-check",
    role="refund_payment",
    duty="付款前读订单系统的当前版本，与本案锁定的快照比对",
    allowed_skills=frozenset({SKILL_SNAPSHOT_CHECK}),
    allowed_tools=frozenset({"order.query"}),
    write_scope=frozenset(),                 # 只读：这一步一个业务表都不写
    max_risk="L",
    model_tier=Tier.LIGHT,
    max_self_repair=0,
)

SKILL_NOTIFY_CUSTOMER = "notify.customer"

#: 案子被**整体**驳回之后，补一次告知客户的调用身份（T137）。
#:
#: 与 `SNAPSHOT_IDENTITY` 同一条口径：这一次告知发生在**闸循环里**，不属于任何
#: 一个 Agent 的 ctx —— DAG 上那个 `notify.customer` 任务依赖付款、付款依赖核算，
#: 核算刚落 FAILED，它停在 PENDING 再也不跑。造一个最小授权的 identity 走
#: `SkillInvoker`，而不是直接 `Skill().run()`：直接调就没有白名单校验、
#: 没有 SkillInvoked 审计行，而「客户到底被告知过没有」正是 `/confirm` 要回查的
#: 那条事实。
#:
#: `role` 取 `refund_intake` —— 与 `notify.customer` 的 `owner_roles` 一致：
#: 告知客户是收案面的职责，不是付款岗的。
NOTIFY_IDENTITY = AgentIdentity(
    agent_id="refund-reject-notice",
    role="refund_intake",
    duty="案子被整体驳回之后把结论如实告知客户",
    allowed_skills=frozenset({SKILL_NOTIFY_CUSTOMER}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="L",
    model_tier=Tier.LIGHT,
    max_self_repair=0,
)

#: >1 才能证明「一次 query 不一定够」—— 终态是问出来的，不是一步返回的。
DEFAULT_SETTLE_AFTER = 2

#: 审批是人的动作。CLI 代跑时名字写死，两次跑输出一致。
#:
#: **只是缺省值**（T137）：`run_payload(gate_operator=…)` 给了就用给的那个 ——
#: 房间那条路传进来的是按 `/approve` 的那个 Matrix 账号。不给仍是这个常量，
#: CLI 的两条命令因此逐字节不变。
APPROVER = "沈思锴"

#: 少了任何一张，`policy_view` 就读不出政策，裁定无从谈起。
REQUIRED_TABLES = ("tenant", "channel", "product_snapshot", "order_snapshot", "policy_rule")

#: 审批捞几轮。放行一个可能让下游又停一个，但轮数有限 —— 无限循环会把
#: 「计划真的收敛不了」变成一个跑不完的进程，那种失败最难查。
MAX_APPROVAL_ROUNDS = 5


class CaseFileError(ValueError):
    """输入 JSON 不合形状。消息直接给人看，不用翻栈。"""


class RoomNotConnected(RuntimeError):
    """要了 `--matrix` 却没接通房间。消息直接给人看。"""


def room_degradation(bus) -> tuple[str, str]:
    """`--matrix` 这一跑到底进没进房间。返回 `(降级原因, 一行详情)`，接通时原因为空。

    **这一条非有不可**：降级之后终端照常刷「房间消息」，输出形态与真房间**一模一样**
    —— 截那个窗口当证据与真的分辨不出来。库内既有口径见
    `hiclaw/room_demo.py` 的 `_DEGRADE_SAYS`：`deps`（解释器没装 matrix-nio）与
    `connect`（连不上/token 失效/撞加密房）要拦，`env`（四个必填没配齐）不拦 ——
    那是明确的降级意图，不是意外。
    """
    reason = getattr(bus, "degrade_reason", None)
    if reason is None:
        return ("no-bus", "事件总线不是 MatrixEventBus —— hiclaw 没接上")
    if not reason or reason == "env":
        return ("", "")
    return (str(reason), str(getattr(bus, "degrade_detail", "") or ""))


# --------------------------------------------------------------------- 读输入
def load(path: str | Path, *, require_case: bool = True) -> dict:
    """读一份自定义 case，并把「缺什么」当场说清楚。

    不做「缺了就补一个缺省值」：靶场少一张表意味着裁定的前提变了，
    而补默认值会让它照常跑绿 —— 跑绿的错结论比报错难查得多。

    `require_case=False` 读的是**底账**（`scenarios/custom/ledger.json`）：五张外部
    快照表照样逐张查，只是不要求 `case` 块 —— 那一块由申请表逐行合成
    （`scripts/run_requests.py`），不写在底账里。
    """
    p = Path(path)
    if not p.exists():
        raise CaseFileError(f"找不到 case 文件：{p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CaseFileError(f"{p} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise CaseFileError(f"{p} 的顶层必须是对象（顶层键即表名）")

    missing = [t for t in REQUIRED_TABLES if not payload.get(t)]
    if missing:
        raise CaseFileError(
            f"{p} 缺这几张外部快照表：{', '.join(missing)}。形状照 "
            "scenarios/custom/refund-case.json（顶层键即表名，列名逐字对齐 schema.sql）")
    if require_case and (not isinstance(payload.get("case"), dict) or not payload["case"]):
        raise CaseFileError(f"{p} 缺 `case` 块 —— 那是 refund.intake 的建案入参，建不出案子")
    return payload


def _requested_at(payload: dict, seed: dict) -> str:
    """本次诉求的时刻。窗口判定靠它与订单 `paid_at` 现算天数，不读任何预置的天数。

    顺序：顶层 `requested_at` -> `case.requested_at` -> 现在。取到「现在」时
    窗口结论会随跑的日子变，这是真实行为，不是不确定性 —— 想要可复现就写死它。
    """
    for src in (payload.get("requested_at"), seed.get("requested_at")):
        if isinstance(src, str) and src.strip():
            return src.strip()
    return C.now_iso()


def _gateway_of(payload: dict, *, fail_with: str | None) -> MockGateway:
    """按输入造网关。`fail_with` 是错误码，注入到本单的 out_trade_no（= order_id）上。

    码必须在 `gateway_codes.ALL_CODES` 里，`MockGateway.__init__` 当场校验 ——
    未收录的码不许兜底成「默认可重试」，那正是最贵的一类 bug。

    三个来源，从强到弱，**先命中先算**：

    1. `fail_with` 入参（CLI 的 `--fail-with`、`make_case_bundle` 的那条路径）；
    2. 底账 `gateway.fail_with` —— 整份底账**每一单**都按这个码失败；
    3. 底账 `gateway.fail_orders`（T122 新增）—— `{订单号: 码}`，**只有登记的那一单失败**。

    第 3 条是为房间演示加的：要在群里演一遍「网关失败 -> 开工单 -> `/assign` ->
    `/resolve` 关单」，底账里就得有一单退不出去，而第 2 条做不到这件事 ——
    它是整份底账的开关，打开之后同一份底账里**每一单**都失败，顺利路径当场没了。
    缺省（底账不写这个键）时行为逐字节不变。
    """
    cfg = payload.get("gateway") if isinstance(payload.get("gateway"), dict) else {}
    settle_after = int(cfg.get("settle_after") or DEFAULT_SETTLE_AFTER)
    code = fail_with or cfg.get("fail_with")
    per_order = cfg.get("fail_orders")
    per_order = per_order if isinstance(per_order, dict) else {}
    script = None
    if code or per_order:
        # `case_seed_of` 留在分支里：三个来源都没给码时它一次都不该被调到
        # （`_gateway_of` 不该因为一份没有 `case` 块的 payload 而抛）。
        order_id = str(fixtures.case_seed_of(payload)["order_id"])
        code = code or per_order.get(order_id)
        if code:
            script = {order_id: str(code)}
    return MockGateway(settle_after=settle_after, script=script)


# --------------------------------------------------- 执行前读外部订单当前版本
def _order_system_of(payload: dict, seed: dict, *, drift: bool) -> Any:
    """按输入造订单系统。`drift=True` 时**注入一次外部改单**，版本推高一格。

    外部账本的初值取自案子手上那份快照（同一个 order_id、同一个版本、同一个金额）
    —— 「一致」这一档必须真的是一致，不是靠 mock 恰好返回了同一个数。
    `--drift` 那一跑随后调 `amend()` 模拟「客服在退款跑到一半时改了订单」，
    于是手上的 v1 与外部的 v2 对不上，而这个"对不上"是数据造成的、不是开关造成的。
    """
    rows = [r for r in (payload.get("order_snapshot") or [])
            if str(r.get("order_id")) == str(seed["order_id"])]
    row = rows[0] if rows else {}
    system = order_tools.MockOrderSystem()
    system.ext_order(
        order_id=str(seed["order_id"]),
        version=int(row.get("version") or seed["order_version"]),
        status=order_tools.ORDER_PAID,
        amount=f"{float(row.get('amount_paid') or 0):.2f}",
        updated_at=str(row.get("paid_at") or C.now_iso()),
    )
    if drift:
        system.amend(str(seed["order_id"]), status=order_tools.ORDER_AMENDED,
                     updated_at=C.now_iso())
    return system


def check_snapshot(store, seed: dict, *, plan_id: str, trace_id: str) -> dict:
    """付款之前先问一次订单系统：我手上这版快照，外面还是不是这一版？

    返回 `refund.snapshot_check` 的出参。**这一步不改任何业务状态**（铁律 8/9）——
    漂移的处置在调用方：把付款任务的 `effect_risk` 提到 `H`，走既有的
    `AWAITING_REVIEW -> BLOCKED`（`gate_needs_human`）出口停下来等人。
    不加新状态、不加新迁移。

    跑在**规划之前**：DAG 的形状取决于漂不漂移，所以这一步必须早于组装 tasks。
    那时 `refund.intake` 还没建案，所以订单号与版本从 `case_seed` 上显式给
    （skill 支持这条路径，见它的 run() 注释）。
    """
    invoker = SkillInvoker(SNAPSHOT_IDENTITY, store)
    res = invoker.invoke(SKILL_SNAPSHOT_CHECK, {
        "tenant_id": str(seed["tenant_id"]), "case_id": str(seed["case_id"]),
        "order_system": ORDER_SYSTEM_NAME,
        "order_id": str(seed["order_id"]),
        "order_version": int(seed["order_version"]),
    }, extras={"plan_id": plan_id, "trace_id": trace_id, "task_id": ""})
    if res.status != "ok" or not isinstance(res.output, dict):
        # 读不出来**不当作一致**：这一步的全部意义就是不放过「依据可能变了」。
        raise RuntimeError(
            f"{SKILL_SNAPSHOT_CHECK} 没产出结果：{res.error}；"
            "执行前读不到订单当前版本时不许默认放行")
    return res.output


#: 人做的状态推进在审计链上的 actor 名。**不借用任何 skill 的名字** ——
#: 借了就等于让一次人工决定挂在一个它没参与的 skill 名下，
#: 而 `verify.py` 的 authoritative-fact 那一项正是按 actor 对账的。
HUMAN_ACTOR = "human.approval"


def _reject_case(store, tenant_id: str, case_id: str, who: str) -> dict | None:
    """主管驳回之后把业务对象推到 `rejected`。已经是终态就什么都不做。

    `rejected` 不在 `guard.AUTHORITATIVE_STATES` 里（那里只有 `settled`），
    所以人的决定写得进去 —— 权威在外部的是「钱到没到账」，不是「我们批不批」。
    迁移 `submitted -> rejected` / `approved -> rejected` 本来就在
    `guard.BIZ_STATUS_FLOW` 里，本函数一条新迁移都不加（铁律 9）。
    """
    case = guard.get_case(store, tenant_id, case_id)
    if case is None or case["biz_status"] not in ("submitted", "approved"):
        return None
    return guard.update_biz_status(
        store, tenant_id, case_id, "rejected", HUMAN_ACTOR, uuid.uuid4().hex,
        reason=f"{who} 驳回本次退款申请")


def notify_customer(store, tenant_id: str, case_id: str, *, plan_id: str,
                    trace_id: str = "", task_id: str = "") -> str:
    """告知客户本案此刻的结论。返回错误串，成功返回空串。

    **公开的**：房间那条路（`ingress/router.py` 的 `/resolve` 之后）要做同一件事。
    在那边另写一遍，两条路迟早对「该不该发、发什么」给出不同答案 —— 而症状是
    同一个案子在群里和在命令行里被告知了两句不一样的话。

    ## 措辞一个字都不在这里拼

    全由 `notify.customer` 按库里的事实产出（`notify.py::_default_content`）。
    这一条是契约 §D 的直接后果：对外口径只许有一个产出处，第二处迟早会在没有
    到账观察的时候说出「已到账」（铁律 8）。

    ## `task_id` 由调用方给，两条路给得不一样

    挂一条归属不实的引用比不挂坏，所以这个参数不设默认的「随便找一个任务」：

    · **闸循环那条**（案子被整体驳回）传**空串**。这条通知不是任何一个 DAG 任务
      跑出来的 —— 它是编排层在计划走不下去之后补的一次告知。挂到驳回那一跳
      （核算任务）上，trace 上就成了「核算任务发了条短信」；挂到 PENDING 的那个
      notify 任务上更糟，它压根没跑。口径同 `check_snapshot`：规划期那次调用也传
      空 `task_id`，由 plan 那棵树收走，不落进 `stray_events`。代价是
      `notification` 不挂 business_ref（`notify.py` 那句 `if plan_id and task_id`
      自己会跳过）。
    · **房间 `/resolve` 那条**传**付款那一步**的 id。那条通知是补偿收口这一串命令
      的一部分，口径逐字同 `ingress/router.py::_command_extras`：房间里这几条命令
      都是在收拾「钱没退出去」的尾巴，挂到别的任务上，「这一切是因为哪一步走不通」
      在 trace 里就断了。

    ## 兜异常

    告知失败不许把一次**已经生效**的驳回变成一次崩溃：人的决定已经落库了。
    记一行并把错误交回调用方，让「这一单没通知出去」在日志里看得见。
    """
    try:
        res = SkillInvoker(NOTIFY_IDENTITY, store).invoke(
            SKILL_NOTIFY_CUSTOMER, {"tenant_id": tenant_id, "case_id": case_id},
            extras={"plan_id": plan_id, "trace_id": trace_id, "task_id": task_id})
    except Exception as exc:                            # noqa: BLE001 —— 见 docstring
        log.warning("案子 %s 的客户告知没发出去（%s: %s）", case_id, type(exc).__name__, exc)
        return f"{type(exc).__name__}: {exc}"
    if res.status != "ok":
        log.warning("案子 %s 的客户告知没发出去（%s）", case_id, res.error)
        return str(res.error or "notify_failed")
    return ""


# ------------------------------------------------------------------- 圆桌五岗
#: 圆桌上真正会在**本案的库里**留痕的两个 skill（跨轨契约 §G 的第 2、4 个）。
#: `maos/roundtable/stages.py` 调它们走的是一个用完即弃的 `:memory:` 库
#: （`_memory_store()`），于是房间里说得出「证据齐 / 风险低」，而这一跑的
#: `event_log` 里一条 `SkillInvoked` 都没有 —— 8 个 skill 只数得到 6 个。
SEAT_SKILLS = (("refund_evidence", "refund.evidence_check"),
               ("refund_risk", "refund.risk_screen"))


class _SilentVoice:
    """一张只记账、不进房间的嘴，顶 `hiclaw.room_voices.Voice` 的位（跨轨契约 §1.3）。

    不复用 `scripts/room_team_smoke.py` 的 `_LocalVoices`：那是脚本层的件，
    `maos/**` 不许把 `scripts/` 当上游（依赖方向同 `obs/trace.py` 的规矩）。
    """

    def __init__(self, agent_id: str, title: str, said: list) -> None:
        self.agent_id, self.title, self._said = agent_id, title, said
        self.user_id, self.own_identity = "", False

    def say(self, text: str) -> None:
        self._said.append({"agent_id": self.agent_id, "title": self.title, "text": text})

    def say_with_actions(self, text: str, actions) -> None:      # noqa: ANN001
        """按钮不进逐字记录 —— 它不是这一岗说的话（口径同 `team._say`）。"""
        self.say(text)


class _SilentVoices:
    """`VoiceSet` 的假件：任何 agent_id 都给得出一张嘴，**永不抛**（同真件语义）。"""

    def __init__(self, titles: dict | None = None) -> None:
        self._titles = dict(titles or {})
        self.said: list[dict] = []

    def voice(self, agent_id: str) -> _SilentVoice:
        return _SilentVoice(agent_id, self._titles.get(agent_id, agent_id), self.said)

    def bot_users(self) -> frozenset:
        return frozenset()

    def close(self) -> None:
        return None


def build_roundtable(model, voices, store):                       # noqa: ANN001
    """探参构造 `RefundRoundtable`：认 `store=` 就传，不认就不传。

    写法照 `hiclaw/room_ingress.py::_build_team`。**非探不可**：圆桌落库（T113）
    与本模块是两条并行的轨，直接传 `store=` 在它并入之前是 `TypeError`，
    而那一刻的症状是「圆桌起不动」—— 拿一个可选参数换掉整段圆桌，比不传更糟。
    """
    from maos.roundtable.team import RefundRoundtable

    params = inspect.signature(RefundRoundtable).parameters
    kwargs: dict[str, Any] = {"ledger_loader": lambda: {}}
    takes_store = "store" in params
    if takes_store:
        kwargs["store"] = store
    else:
        log.info("圆桌引擎不认 store= 参数（T113 尚未并入），本轮圆桌事件不落库")
    return RefundRoundtable(model, voices, **kwargs), takes_store


def _seat_skill_traces(store, payload: dict, seed: dict, rules: list[dict],
                       *, plan_id: str, trace_id: str, requested_at: str) -> list[dict]:
    """让证据岗与风险岗那两个只读 skill 在**真库**上各留一次痕。

    入参形状逐字照 `maos/roundtable/stages.py::_evidence_check` / `_risk_screen`
    —— 两处拼法不同源的症状是「房间说缺照片、束里说证据齐」，而两边各自都不报错。
    这一步**不改任何业务状态**：两个 skill 的 `write_scope` 都只有 artifact。
    """
    from maos.roundtable.team import identity_of

    rows = [r for r in (payload.get("order_snapshot") or [])
            if isinstance(r, dict) and str(r.get("order_id")) == str(seed["order_id"])]
    order = rows[0] if rows else {}
    facts = json.loads(order.get("payload_json") or "{}") if order else {}
    customer_id = str(facts.get("customer_id") or "")
    all_orders = [r for r in (payload.get("order_snapshot") or []) if isinstance(r, dict)]
    kin = [r for r in all_orders
           if str(json.loads(r.get("payload_json") or "{}").get("customer_id") or "")
           == customer_id] if customer_id else ([order] if order else [])

    inputs = {
        "refund.evidence_check": {
            "case_seed": seed,
            "customer_evidence": payload.get("customer_evidence") or [],
            "rules": list(rules),
            "order_facts": {k: facts[k] for k in ("logistics", "qc_report") if k in facts},
            "requested_at": requested_at,
        },
        "refund.risk_screen": {
            "case_seed": seed, "order": order, "customer_orders": kin,
            "refund_history": payload.get("refund_history") or [],
            "requested_at": requested_at,
        },
    }
    out = []
    for role, skill in SEAT_SKILLS:
        res = SkillInvoker(identity_of(role), store).invoke(
            skill, inputs[skill],
            extras={"plan_id": plan_id, "trace_id": trace_id, "task_id": ""})
        out.append({"skill": skill, "role": role, "status": res.status,
                    "error": res.error, "invocation_id": res.invocation_id})
    return out


def run_roundtable(store, payload: dict, seed: dict, rules: list[dict], *,
                   plan_id: str, trace_id: str, requested_at: str,
                   model=None) -> dict:                           # noqa: ANN001
    """规划之前先开一次圆桌：五岗依次发言，主席合议出一张批复建议卡。

    返回**观测到的事实**（谁说了话、是不是模型说的、合议出了什么），不含任何期望值。
    圆桌是旁路观察者：它炸了不该让这一单跑不下去（同 `ingress/router._fire` 的口径），
    所以整段兜异常，失败只记一行 `error` 并照常往下走。

    `model=None` 时五岗走事实卡兜底（`Speaker.live` 为假）；给真客户端就是真发言。
    """
    result: dict[str, Any] = {"ran": False, "store_param": False, "seats": [],
                              "verdict": None, "skills": [], "error": None}
    try:
        from maos.ingress.router import preflight
        from maos.roundtable.team import TITLES
        from maos.roundtable.verdict import decide

        voices = _SilentVoices(TITLES)
        team, result["store_param"] = build_roundtable(model, voices, store)
        checked = preflight(payload)
        reports = team.on_preflight(payload=payload, checked=checked, ledger={},
                                    evidence=list(payload.get("customer_evidence") or []),
                                    requested_by=APPROVER)
        result["seats"] = [{
            "agent_id": r.agent_id, "title": r.title,
            "spoken_by_model": bool(r.spoken_by_model),
            "fallback_reason": r.fallback_reason, "speech": r.speech,
        } for r in reports]
        card = decide(reports, case_id=str(checked.get("case_id") or ""))
        result["verdict"] = {
            "recommend": card.recommend, "approver_role": card.approver_role,
            "blockers": list(card.blockers), "headline": card.headline,
            "amount_preview": card.amount_preview,
        }
        result["transcript"] = list(voices.said)
        result["ran"] = True
    except Exception as exc:                            # noqa: BLE001 —— 见 docstring
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("圆桌这一段没跑成（%s），本单照常往下走", result["error"])

    # 两个只读 skill 的留痕**独立兜异常**：五岗说没说话与 skill 有没有留下审计行
    # 是两件事，混成一句会指错方向（口径同 `router._attach_verdict`）。
    try:
        result["skills"] = _seat_skill_traces(
            store, payload, seed, rules,
            plan_id=plan_id, trace_id=trace_id, requested_at=requested_at)
    except Exception as exc:                            # noqa: BLE001
        result["skills_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("圆桌两个 skill 没能在真库留痕（%s）", result["skills_error"])
    return result


# --------------------------------------------------------------- 计划审批停靠
#: `plan_approval` 的三个取值。缺省 `None` = 不停靠，`create_plan` 之后直接
#: `start_plan`，与本模块此前的行为**逐字节相同**。
APPROVAL_MODES = ("approve", "reject", "reject_then_approve")


def stop_for_plan_approval(store, cp, plan_id: str, *, mode: str, operator: str,
                           feedback: str) -> dict:
    """在 `create_plan` 与 `start_plan` 之间停一下，把计划交给人。

    这正是 `maos/runtime/plan_approval.py` 的模块 docstring 点名的接线位置：
    全仓场景 `create_plan` 之后**立刻** `start_plan`，PENDING 这个状态在生产路径上
    从来没有停留过一个瞬间。本函数补的就是那一瞬间，**一行内核都不改**。

    `reject_then_approve` 演的是两级驳回里的第一级：人驳回一次并留下反馈
    （基线上没接 replanner，所以方案原样留着，见 `PlanApprovalQueue.reject`），
    再看一眼之后放行 —— 计划本身跑得动，这一单该不该退是后面那道人工闸的事。

    返回这一停靠点上发生过什么；`started` 为假时调用方**不许**再自己 `start_plan`。
    """
    if mode not in APPROVAL_MODES:
        raise ValueError(f"plan_approval 只能是 {list(APPROVAL_MODES)}，实际 {mode!r}")
    queue = PlanApprovalQueue(store, cp)
    out: dict[str, Any] = {"mode": mode, "operator": operator,
                           "preview": queue.preview(plan_id), "rounds": []}
    if mode in ("reject", "reject_then_approve"):
        out["rounds"].append({"decision": "reject", "operator": operator,
                              "feedback": feedback,
                              "accepted": queue.reject(plan_id, operator, feedback)})
    if mode in ("approve", "reject_then_approve"):
        out["rounds"].append({"decision": "approve", "operator": operator,
                              "accepted": queue.approve(plan_id, operator)})
    # 驳回之后 plan 仍停在 PENDING（本轨的题眼）—— 没批准就是没开跑。
    out["started"] = any(r["decision"] == "approve" and r["accepted"] for r in out["rounds"])
    out["plan_state"] = store.get_plan(plan_id)["state"]
    return out


# ----------------------------------------------------------------- DAG 补支付段
def _with_payment(tasks: list[dict], *, seed: dict, gateway: str,
                  drift: bool = False) -> list[dict]:
    """在核算之后、通知之前插入支付任务。裁定为 reject 时原样返回。

    形状照 `flows/scenario_6.py` 的 TASK_PAYMENT：同样**只带网关名不带金额** ——
    申报金额只挂在核算那一步，别的退款任务带上它，就会被第六道闸要求交一份
    自己根本产不出的 finance_entry，闸恒 blocker。

    `drift=True` 时给它挂 `effect_risk="H"`：产物落地（真的把钱退出去）在依据
    可能已经变了的情况下就是不可逆的高风险动作，Gate 过了也不自动放行。走的是
    **既有**的那条出口（`control_plane.on_review_verdict` 里 `effect_risk in
    NEEDS_HUMAN_APPROVAL` 那一支 -> `AWAITING_REVIEW -> BLOCKED`），
    一个新状态、一条新迁移都没加（铁律 9）。
    """
    finance = next((t for t in tasks if t["role"] == ROLE_FINANCE), None)
    if finance is None:
        return tasks                      # reject 不排核算，也就没有钱要付
    notify = next(t for t in tasks if (t["inputs"] or {}).get("step") == "notify")
    stem = finance["task_id"].rsplit("-", 1)[0]
    payment = {
        "task_id": f"{stem}-payment", "role": ROLE_PAYMENT,
        "title": "发起退款并观察网关终态",
        "inputs": {"biz_type": C.BIZ_TYPE, "tenant_id": seed["tenant_id"],
                   "case_id": seed["case_id"], "gateway": gateway},
        "acceptance": ["发起后不得写 settled", "终态必须由 query 观察得到"],
        "depends_on": [finance["task_id"]], "risk_level": "M",
    }
    if drift:
        payment["effect_risk"] = "H"
    notify["depends_on"] = [payment["task_id"]]
    out = list(tasks)
    out.insert(out.index(notify), payment)
    return out


def _obs_row(o: dict) -> dict:
    """一条支付观察。`poll_count` 与 `resolved_from` 都不是表上的列，在回执里。

    这两个字段是「终态是**问出来的**、不是本地推断的」那条论证仅有的可核证据：
    问了几次，以及一笔先报 unknown 的退款最后是被问成了什么。读不到它们，
    这条论证就只剩自述。
    """
    receipt = json.loads(o.get("raw_receipt_json") or "{}")
    detail = receipt.get("detail") if isinstance(receipt.get("detail"), dict) else {}
    return {
        "observed_state": o["observed_state"], "gateway_code": o.get("gateway_code"),
        "poll_count": receipt.get("poll_count"),
        "resolved_from": detail.get("resolved_from"),
    }


def _blocked_reason(cp, plan_id: str, task_id: str) -> str:
    """这个任务因为什么停下来等人 —— 取 event_log 里**最后一次**进 BLOCKED 的那一跳。

    判据取自 event_log 而不是任务行上的某个字段：`detail` 只落在迁移那一条事件上，
    在任务行上另开一个字段就有了第二份事实（`HumanApprovalQueue.pending` 同一口径）。
    """
    hits = [e for e in cp.store.list_event_log(plan_id)
            if e.get("event_type") == "StateTransition"
            and e.get("task_id") == task_id and e.get("to_state") == TaskState.BLOCKED]
    if not hits:
        return "未知：event_log 里没有进 BLOCKED 的那一跳"
    last = hits[-1]
    detail = last.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    detail = detail if isinstance(detail, dict) else {}
    kind = detail.get("human_exit") or detail.get("await") or ""
    note = str(detail.get("message") or detail.get("note") or "").strip()
    head = f"{last.get('reason') or '?'}" + (f"（{kind}）" if kind else "")
    return f"{head} —— {note}" if note else head


def await_kind(store, plan_id: str, task_id: str) -> str:      # noqa: ANN001
    """这一次停在 BLOCKED 等的是哪一类人工决定 —— 取 `detail["await"]`，没有返回空串。

    **公开的**：房间那条路（`ingress/router.py::_held_gate_of`）要问同一个问题，
    在那边另写一遍就是同一条判据的第二份实现 —— 改一处漏一处时不报错，只会让
    两条路对「这个闸该不该由我代签」给出不同答案。理由与
    `core/control_plane.py::AWAIT_HUMAN_DECISION` 那段注释逐字同源。

    与 `HumanApprovalQueue.pending()` 里那个集合判定**不是**一件事，别互相替代：
    那边答「这个任务历史上等过人吗」，**宽**是对的（多捞不会错，漏捞才会）；
    这边答「它**这一次**等的是哪一类」，**准**是对的（判错就会代签一个签不到的闸）。

    两类的分界：

      · 没有 `await` 标记的是 ``effect_risk=H`` —— 产物落地要人放行，**规划期就知道
        它会来**，一句「这一单可以去退」包得住它；
      · `AWAIT_HUMAN_DECISION` 是控制面第三出口 / replan 上限，判的是「机器已经没有
        别的招了」，**执行中才出现**，规划期那次签字签不到它。

    判据取 event_log 的**最后一次**进 BLOCKED 那一跳，口径逐字同 `_blocked_reason`：
    `detail` 只落在迁移那一条事件上，在任务行上另开一个字段就有了第二份事实。
    取最后一次而不是第一次，是因为 BLOCKED 的三条出边全是人的动作 —— 历史上等过人、
    `human_resume` 回去、这次因**别的**原因再停一次的任务，要按**这一次**的原因判。
    """
    hits = [e for e in store.list_event_log(plan_id)
            if e.get("event_type") == "StateTransition"
            and e.get("task_id") == task_id and e.get("to_state") == TaskState.BLOCKED]
    if not hits:
        return ""
    detail = hits[-1].get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    return str((detail or {}).get("await") or "") if isinstance(detail, dict) else ""


# --------------------------------------------------------------------- 跑一次
def run_payload(payload: dict, *, approve: bool = True, fail_with: str | None = None,
                matrix: bool = False, verbose: bool = True,
                allow_degraded: bool = False, drift: bool = False,
                roundtable: bool = False, roundtable_model=None,   # noqa: ANN001
                plan_approval: str | None = None,
                approval_operator: str = APPROVER,
                plan_feedback: str = "",
                reject_roles: tuple[str, ...] = (),
                hold_awaits: tuple[str, ...] = (),
                gate_operator: str = "",
                store=None) -> dict:                              # noqa: ANN001
    """跑一份自定义 case，返回**观测到的事实**（不含任何期望值）。

    `drift=True` 注入一次外部改单（`--drift`）：订单系统的版本被推高一格，
    于是付款之前那一步 `refund.snapshot_check` 会报漂移，付款任务停在 BLOCKED
    等人。**业务状态一个字节都不会因此改变**（铁律 8）——漂移是「停下来问人」，
    不是一个新的业务状态。

    `roundtable=True` 在规划**之前**开一次五岗圆桌（T114）：五岗发言 + 主席合议，
    另让证据岗、风险岗那两个只读 skill 在真库上各留一次痕。缺省 `False` ——
    圆桌是旁路，`scripts/run_case.py` 那条命令一个字节不变。

    `plan_approval` 在 `create_plan` 与 `start_plan` 之间插一个人工停靠点
    （取值见 `APPROVAL_MODES`）。缺省 `None` = 不停靠，两句紧挨着，与从前逐字节相同。
    驳回而没有随后的批准时，plan 停在 PENDING，DAG 一个任务都不派发 —— 那正是
    这个停靠点存在的理由，不是一次失败。

    `reject_roles` 是**逐岗**的人工闸决定：命中的角色在 BLOCKED 上被驳回，其余照
    `approve` 走。缺省空元组 = 全按 `approve`，行为不变。它存在的理由只有一个：
    网关明确失败时，人在付款闸上该做的是**不放行**（钱没退出去，不能签「这一步完成了」），
    而核算那一步照常批 —— 一个全局布尔表达不了「这一步批、那一步不批」。

    `hold_awaits` 是**不代签**的闸的清单，按 `detail["await"]` 匹配（取值见
    `core/control_plane.py::AWAIT_HUMAN_DECISION`）。缺省空元组 = 一个都不扣，
    行为逐字节不变。它与 `reject_roles` 答的是**不同的问题**：那个答「这一步批还是
    不批」，要求调用方**跑之前**就知道答案；这个答「这一步该不该由我代签」——
    `AWAIT_HUMAN_DECISION` 的闸是控制面判定「机器已经没有别的招了」才落的，
    **执行中才出现**，跑之前根本不存在，于是调用方那一次签字签不到它。
    命中的任务停在 BLOCKED、`human_exits` 记一条 `held_for_human`，Plan 不收 DONE ——
    与 `held_for_drift` 同一个形状，理由也同一条：代跑「停下来问人」等于问了个寂寞。
    第二次决定由调用方在别处下（房间那条路见 `ingress/router.py::handle_gate_decision`）。

    `gate_operator` 是**闸循环里**那些决定的操作者（T137）。缺省空串 = 仍是
    `APPROVER` 那个 CLI 常量，两条 CLI 命令的输出逐字节不变。房间那条路传进来的是
    按 `/approve` 的那个 Matrix 账号 —— T135 之后付款闸那一跳已经署真人名，
    核算这一跳却还署一个 CLI 常量，同一条 HITL trace 上两跳署了两个不同来源的名字。
    这个入参把后一跳也接上了。`approval_operator` 管的是**计划级**那个停靠点，
    两者不是一件事：一个在 `create_plan` 与 `start_plan` 之间，一个在 DAG 跑起来
    之后的闸上，同一次处置里完全可能是两个人。

    代跑那一跳的语义没变：不给 `gate_operator` 时它仍然是「由处置流程代跑」，
    只是署名从写死的常量变成了「调用方给的那个，不给才回落到常量」。

    `store` 给了就跑在那个库上（透给 `build`），没给照旧自建一个 `:memory:`（T122）。
    房间入口由此让 `/refund` 跑出来的十一张退款域表落在 router 自己的库里，
    紧接着的 `/assign` `/resolve` 才查得到工单。**缺省行为逐字节不变。**
    共享库上同一单可能被跑第二遍（复检、人手滑），靶场装载因此要幂等 ——
    见 `fixtures.seed_case`。
    """
    seed = fixtures.case_seed_of(payload)
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])

    # 脚本是可变 dict：规划应答要等政策读出来才拼得出来，而 ScriptedModelClient
    # 每次 complete() 才查表。占位不能是空 dict（`script or {}` 会另造一个，
    # 后填的内容到不了客户端，症状是 Plan 里一个任务都没有且不报错）。
    script: dict[str, str] = {"用户请求": "{}"}
    model = select_model_client(script, force_scripted=True)
    store, bus, cp, model, worker, gate = build(script, matrix=matrix, model=model,
                                                store=store)

    if matrix and not allow_degraded:
        # 早失败：靶场都灌完了才发现没进房间，那一屏「房间消息」已经骗过人一次了。
        why, detail = room_degradation(bus)
        if why:
            raise RoomNotConnected(f"要了 --matrix 但房间没接通（{why}）：{detail}")

    fixtures.seed_case(store, payload)        # 五张外部快照表，只 INSERT 不建表
    fixtures.seed_policy_kb(store)            # 知识库照灌全量：检索本来就该在
    fixtures.seed_history_kb(store)           # 有别人家知识的库里做，跨租户召不回才有说服力

    C.reset_gateways()
    C.register_gateway(GATEWAY_NAME, _gateway_of(payload, fail_with=fail_with))
    order_tools.reset_order_systems()
    order_tools.register_order_system(
        ORDER_SYSTEM_NAME, _order_system_of(payload, seed, drift=drift))

    # id 先生成：下面的 snapshot_check 跑在 `create_plan` **之前**，不先拿到 id，
    # 它落的 SkillInvoked / ToolInvoked / SnapshotDrift 就只能挂空串，
    # 成为 trace 里认领不了的游离事件（口径同 `scenario_6` 那段注释）。
    trace_id, plan_id = new_id("trace"), new_id("plan")

    # ---- 执行前读外部订单当前版本（T116）：付款之前先确认依据没变 ----
    snapshot = check_snapshot(store, seed, plan_id=plan_id, trace_id=trace_id)

    # ---- 规划期读政策：版本锁定 + 渠道过滤，全走冻结口径 ----
    view = contrast.policy_view(store, tenant_id=tenant_id, order_id=str(seed["order_id"]),
                                order_version=int(seed["order_version"]))
    directives = contrast.policy_directives(view["rules"])
    paid_at = objects.query(
        store, "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
        (tenant_id, seed["order_id"], int(seed["order_version"])))[0]["paid_at"]
    requested_at = _requested_at(payload, seed)
    days = contrast.elapsed_days(paid_at, requested_at)
    verdict = contrast.evaluate_eligibility(view["rules"], reason_code=str(seed["reason_code"]),
                                            elapsed_days=days)

    # ---- 圆桌五岗（T114）：排 DAG 之前先让五个岗位各说一句，主席合议出建议卡 ----
    # 位置在规划**之前**不是随手排的：圆桌回答的是「这一单该不该退、谁来拍板」，
    # 那正是计划长什么样的前提。排完 DAG 再开会，会开成一场对既成事实的复述。
    table = run_roundtable(store, payload, seed, view["rules"], plan_id=plan_id,
                           trace_id=trace_id, requested_at=requested_at,
                           model=roundtable_model) if roundtable else None

    # `_signals_of` 刻意直接复用：工单那条信号的口径（标题/正文由 case 数据推出，
    # 不现编情节）就在它里面，另抄一份就是第二套口径。
    tasks = _with_payment(
        contrast.plan_tasks(seed=seed, signals=contrast._signals_of(payload),
                            directives=directives, decision=verdict["decision"]),
        seed=seed, gateway=GATEWAY_NAME, drift=bool(snapshot["drift"]))
    script["用户请求"] = json.dumps({"tasks": tasks}, ensure_ascii=False)

    # ---- Manager 零改动复用：为代码域写的规划器，在退款域照样规划 DAG ----
    goal = contrast.GOAL_TEMPLATE.format(**{k: seed[k] for k in
                                            ("tenant_id", "channel_id", "reason_code")})
    mgr = ManagerAgent(model, store=store)
    planned = mgr.plan(goal, context={
        "tenant_id": tenant_id, "biz_type": C.BIZ_TYPE, "channel_id": seed["channel_id"],
        "sku": seed["sku"], "plan_id": plan_id, "trace_id": trace_id})
    cp.create_plan(goal=goal, trace_id=trace_id, plan_id=plan_id, tasks=planned)

    # ---- 计划审批停靠（T114）：人批准了才许开跑 ----
    # 缺省 `plan_approval=None` 时这一段退化成原来那一句 `start_plan` —— 不停靠、
    # 不落任何审批事件，`scripts/run_case.py` 的七条老测试因此一条都不受影响。
    # 变量名**不叫 approval**：下面那段人工审批循环里 `approval` 已经是
    # 「一条 approval_record」了，重名会让计划级的这一份被逐轮覆盖掉，
    # 而两者都是 dict、都不报错，症状只是束里 `plan_approval` 恒为 null。
    plan_stop = None
    if plan_approval is None:
        cp.start_plan(plan_id)
    else:
        plan_stop = stop_for_plan_approval(
            store, cp, plan_id, mode=plan_approval, operator=approval_operator,
            feedback=plan_feedback or "计划先给人看一眼：请说明这一单为什么该走人工闸")
        if not plan_stop["started"]:
            # 人驳回了且没有随后的批准：**这里就是终点**。往下跑一步都等于
            # 把「人否决了」演成「人否决了但照样开跑」。
            if verbose:
                dump(cp, plan_id, f"自定义 case {case_id}（计划被驳回，未开跑）")
            return _observe(store, cp, plan_id, seed=seed, view=view, directives=directives,
                            verdict=verdict, days=days, paid_at=paid_at,
                            requested_at=requested_at, approvals=[], approve=approve,
                            human_exits=[], snapshot=snapshot, roundtable=table,
                            plan_stop=plan_stop)
    run_until_settled(bus, gate, cp, plan_id)

    # ---- 人工审批：停在 BLOCKED 的任务都在等人，CLI 代跑人的那一半 ----
    # **必须循环**：放行一个之后下游可能又停下来（比如裁定 reject 的计划走到
    # 第六道闸的 plan 级判据上），单轮只捞第一批，剩下的静默挂着，Plan 收在
    # RUNNING 而摘要看不出为什么 —— 那正是 `HumanApprovalQueue.pending` 的
    # docstring 点名的「漏捞比多捞坏」。
    hq = HumanApprovalQueue(store, cp)
    approvals: list[dict] = []
    human_exits: list[dict] = []
    # 闸上那个人的署名（T137）。`gate_operator` 空串时回落到 CLI 常量 —— 缺省行为
    # 逐字节不变，`run.py` 与 `make_case_bundle` 那几束里的 `approver` 一个字不动。
    who = f"{gate_operator or APPROVER}（{directives['approver_role']}）"
    # 漂移之后**一个都不代跑放行**。
    #
    # CLI 代的是「主管照常审批」这一半，而"订单被人改过了，这笔还退不退"不是照常
    # 审批 —— 那要人去订单系统核对之后才谈得上。代跑它等于把「停下来问人」演成
    # 「问了个寂寞」。
    #
    # **扣住的是核算那一步，而不是付款那一步**，这一点很要紧：`effect_risk=H` 的
    # 判定发生在 Gate **之后**（`control_plane.on_review_verdict` 里 verdict=="pass"
    # 那一支），也就是任务已经跑完了才停下来等人放行产物。而退款域的付款任务在
    # **执行时**就把钱退出去了 —— 只给付款任务挂 `effect_risk=H`，结果是钱照退、
    # 然后停在 BLOCKED，漂移检查等于什么都没拦住。核算是付款的前置，扣住它，
    # 付款任务就停在 PENDING 压根不派发，`biz_status` 停在 submitted。
    # （派单验收 3 的字面是「付款任务落 BLOCKED」，本轨做不到那个字面 + 钱不退
    #  两全，取了后者 —— 详见 docs/DECISIONS.md 的 ## task-t116。）
    held_for_drift: set[str] = set()
    # 跑起来之后才出现的闸，代签不了 —— 理由见 `hold_awaits` 那段 docstring。
    held_for_human: set[str] = set()
    for _ in range(MAX_APPROVAL_ROUNDS):
        pending = hq.pending(plan_id)
        if not pending:
            break
        acted = False
        for blocked in pending:
            if snapshot["drift"]:
                if blocked["task_id"] not in held_for_drift:
                    held_for_drift.add(blocked["task_id"])
                    human_exits.append({
                        "task_id": blocked["task_id"], "title": blocked["title"],
                        "why": _blocked_reason(cp, plan_id, blocked["task_id"]),
                        "decision": "held_for_drift",
                    })
                continue
            kind = await_kind(cp.store, plan_id, blocked["task_id"])
            if kind and kind in hold_awaits:
                # 扣住，**不代签**。与漂移那一支同形：记一条留痕，任务停在 BLOCKED，
                # 等调用方把这一条递给真正该决定的人。判的是这一次进 BLOCKED 的
                # `await` 标记，不是角色也不是 `effect_risk` —— 那两个规划期就定了，
                # 而这一类闸规划期还不存在。
                if blocked["task_id"] not in held_for_human:
                    held_for_human.add(blocked["task_id"])
                    human_exits.append({
                        "task_id": blocked["task_id"], "title": blocked["title"],
                        "role": blocked["role"],
                        "why": _blocked_reason(cp, plan_id, blocked["task_id"]),
                        "await": kind,
                        "decision": "held_for_human",
                    })
                continue
            acted = True
            # 逐岗决定：全局 `approve` 之上再让 `reject_roles` 收窄一次。
            # 判在这里而不是在调用方，是因为「这一批停下来等人的任务」只有这个
            # 循环见得到 —— 调用方拿不到 blocked 列表，也就无从逐条表态。
            ok = approve and blocked["role"] not in reject_roles
            human_exits.append({
                "task_id": blocked["task_id"], "title": blocked["title"],
                "role": blocked["role"],
                "why": _blocked_reason(cp, plan_id, blocked["task_id"]),
                "decision": "approved" if ok else "rejected",
            })
            if ok:
                # 顺序不可换：先落 approval_record（人的决定），再放行任务 ——
                # payment.execute 会核对审批记录，没有它就拒绝发起付款。
                approval = C.record_approval(
                    store, tenant_id=tenant_id, case_id=case_id, approver=who,
                    decision="approved", reason=f"金额与订单锁定的政策 v{view['pinned']} 一致")
                # T116：补修订号 + 把审批挂成一条 business_ref。审批是人的动作，
                # 落在哪个 Task 上只有这里知道（`_common.record_approval` 拿不到 DAG）。
                case_pack.stamp_approval_revision(
                    store, approval, plan_id=plan_id, task_id=blocked["task_id"])
                approvals.append(approval)
                hq.decide(blocked["task_id"], approved=True, operator=who,
                          note=f"按 {directives['approver_role']} 权限放行")
            else:
                # 驳回同样是人的决定，同样要留痕并挂引用 —— 只是不放行任务。
                # 不落这一条的话，「谁在什么时候驳回了这笔」在库里查不到，
                # 而客户投诉时要对的第一件事就是它。
                approval = C.record_approval(
                    store, tenant_id=tenant_id, case_id=case_id, approver=who,
                    decision="rejected", reason="主管驳回")
                case_pack.stamp_approval_revision(
                    store, approval, plan_id=plan_id, task_id=blocked["task_id"])
                # 业务对象跟着人的决定走（T116）：状态机里 `submitted -> rejected`
                # 一直在（`guard.BIZ_STATUS_FLOW`），但**整个退款域没有任何一处写它**
                # —— 于是驳回之后案子仍停在 submitted，对外投影只能说「还在核定」，
                # 而实际上已经不予退款了。`rejected` 不是权威终态（那只有 settled），
                # 所以人的动作写得进去；actor 写成 human.approval 而不是某个 skill，
                # 因为做这个决定的是人，审计链上不该挂在一个它没参与的 skill 名下。
                rejected_case = _reject_case(store, tenant_id, case_id, who)
                hq.decide(blocked["task_id"], approved=False, operator=who, note="主管驳回")
                if rejected_case is not None:
                    # 案子被**整体**驳回了 —— 这一单在 MAOS 这边到此为止，而 DAG 上
                    # 那个 `notify.customer` 任务依赖付款、付款依赖刚落 FAILED 的这一步，
                    # 它停在 PENDING 再也不跑。不在这里补一次，客户从头到尾收不到
                    # 任何告知：钱没退、结论也不知道（T137）。
                    #
                    # **判据用 `_reject_case` 的返回值，不是「这一跳被驳回了」**：
                    # 它那条 submitted/approved 守卫正是「驳回整个案子」与「只驳回
                    # 这一跳」的分界。付款闸上那次驳回时案子已在 `gateway_accepted`，
                    # 守卫不放行、返回 None —— 那一档钱的下落还没定，该说什么要等
                    # 补偿收口之后才知道，于是告知归那一侧（房间的 `/resolve` 之后）。
                    # 在这里对两档说同一句话，等于替一笔还在处理的钱宣布了结局。
                    notify_customer(store, tenant_id, case_id,
                                    plan_id=plan_id, trace_id=trace_id)
        if not acted:
            break                          # 剩下的全被漂移扣住，再跑一轮也是同一批
        run_until_settled(bus, gate, cp, plan_id)

    if verbose:
        dump(cp, plan_id, f"自定义 case {case_id}")
    return _observe(store, cp, plan_id, seed=seed, view=view, directives=directives,
                    verdict=verdict, days=days, paid_at=paid_at, requested_at=requested_at,
                    approvals=approvals, approve=approve, human_exits=human_exits,
                    snapshot=snapshot, roundtable=table, plan_stop=plan_stop)


def _observe(store, cp, plan_id: str, *, seed: dict, view: dict, directives: dict,
             verdict: dict, days: int, paid_at: str, requested_at: str,
             approvals: list[dict], approve: bool, human_exits: list[dict],
             snapshot: dict, roundtable: dict | None = None,
             plan_stop: dict | None = None) -> dict:
    """把这一跑的事实收成一份字典。只读库，不做任何判定。"""
    tenant_id, case_id = str(seed["tenant_id"]), str(seed["case_id"])

    def q(sql: str) -> list[dict]:
        return objects.query(store, sql, (tenant_id, case_id))

    entries = q("SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?")
    breakdown = json.loads(entries[0]["breakdown_json"]) if entries else {}
    obs = q("SELECT * FROM payment_observation WHERE tenant_id=? AND case_id=?"
            " ORDER BY observed_at")
    notes = q("SELECT * FROM notification WHERE tenant_id=? AND case_id=?")
    approval_rows = q("SELECT * FROM approval_record WHERE tenant_id=? AND case_id=?"
                      " ORDER BY decided_at")
    case = guard.get_case(store, tenant_id, case_id) or {}
    # 对外三态由 `projection.public_status` 唯一产出（跨轨契约 §D）——
    # 这里**不另拼一句话**：拼第二处的症状是两个出口对同一个案子说得不一样，
    # 而其中一句迟早会在没有观察行的时候说出「已到账」（铁律 8）。
    drift_events = [
        e for e in cp.store.list_event_log(plan_id)
        if e.get("event_type") == snapshot_check.EVENT_SNAPSHOT_DRIFT]
    return {
        # 两个可选停靠点的观测。**没开就是 None，不是空 dict** —— 读的人要分得清
        # 「这一跑没接圆桌」与「接了但五岗一句话都没说」，后者是 bug。
        "roundtable": roundtable,
        "plan_approval": plan_stop,
        "snapshot_check": {
            "drift": bool(snapshot["drift"]),
            "snapshot_version": snapshot["snapshot_version"],
            "current_version": snapshot["current_version"],
            "reason": snapshot["reason"],
            "events": len(drift_events),
        },
        "public_status": projection.public_status(
            str(case.get("biz_status") or ""),
            bool(q("SELECT request_id FROM refund_request"
                   " WHERE tenant_id=? AND case_id=? LIMIT 1")),
            projection.observed_state_of(obs)),
        "approval_revisions": [int(r["revision"]) for r in approval_rows],
        "approval_decisions": [str(r["decision"]) for r in approval_rows],
        "case_id": case_id, "tenant_id": tenant_id,
        "channel_id": str(seed["channel_id"]), "reason_code": str(seed["reason_code"]),
        "amount_claimed": seed.get("amount_claimed"),
        "paid_at": paid_at, "requested_at": requested_at, "elapsed_days": days,
        "pinned_policy_version": view["pinned"],
        "matched_rules": [r["ref"] for r in view["rules"]],
        "decision": verdict["decision"], "deciding_rule": verdict["rule_ref"],
        "no_reason_days": verdict["no_reason_days"], "why": verdict["why"],
        "extra_tasks": [s["task_key"] for s in directives["extra_tasks"]],
        "approver_role": directives["approver_role"],
        "approved": approve and bool(approvals),
        "approvals": [a["approver"] for a in approvals],
        "human_exits": human_exits,
        "amount_approved": breakdown.get("amount_approved", "0.00"),
        "policy_version_used": breakdown.get("policy_version"),
        "rule_refs": entries[0]["rule_refs"] if entries else None,
        "biz_status": case.get("biz_status"),
        "payment_observations": [_obs_row(o) for o in obs],
        "settled_observations": sum(1 for o in obs if o["observed_state"] == "settled"),
        "notifications": [{"channel": n.get("channel"), "acked": bool(n.get("ack_at"))}
                          for n in notes],
        "business_refs": len(objects.list_business_refs(store, plan_id=plan_id)),
        # 十类业务对象的覆盖体检（T116）。算在 `case_pack.ref_coverage` 一处，
        # 验收命令 / CLI 输出 / 测试读的都是它 —— 三处各算一遍的话，
        # 「resolved 10/10」这句话迟早在某一处说错。
        "business_ref_coverage": case_pack.ref_coverage(store, plan_id=plan_id),
        "plan_id": plan_id, "plan_state": cp.store.get_plan(plan_id)["state"],
        "tasks": [{"task_id": t["task_id"], "role": t["role"], "title": t["title"],
                   "state": t["state"], "attempt": t["attempt"]}
                  for t in cp.store.list_tasks(plan_id)],
    }


def run_file(path: str | Path, **kw: Any) -> dict:
    """读文件并跑。`load()` 的报错原样上抛，由 CLI 翻成人话。"""
    return run_payload(load(path), **kw)
