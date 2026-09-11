"""结果面的命令（T117）——`/assign`、`/resolve`、`/confirm`、`/complain`，
外加 T129 补的第五条 `/compensate`（补开工单，见 `handle_compensate`）。

## 接线已完成（T122）

T117 写本模块时 router 正被另一会话大改，所以当时只导出处理函数与一张
`COMMAND_HANDLERS` 表，一个字都不碰路由文件。**T122 把那一行接上了**：
`maos/ingress/router.py::handle_outcome` 过一道渠道闸、查出 `tenant_id`、
调本模块的 `dispatch()`，按 `CommandResult.kind` 回帖。

接线之前还要先填一个更深的坑：处置从前跑在 `run_payload` 自建又随手丢掉的
`:memory:` 库里，于是工单根本不在 router 的库中，`/assign` 必撞
`require_ticket` 的 `LookupError`。T122 给 `flows/common.py::build` 与
`flows/custom_case.py::run_payload` 加了 `store=` 入参，房间处置从此跑在
router 自己的库上 —— 命令与处置共用一个库，这四条命令才可能成立。

## 判定顺序不许换（逐字沿用 `hiclaw/matrix_bus.py::RoomApprovalBridge` 的那三步）

1. 不是本模块的命令 -> 一声不吭（`KIND_IGNORED`）。房间里的闲聊不该收到用法提示。
2. 发言人不在名单 -> 回「无审批权限」**并落一条 `ApprovalDenied`**，哪怕参数是错的。
   **先查你是谁，再谈你想干什么。** 反过来的话，名单外的人会先收到一句格式提示，
   等于告诉他「照这个格式发就能做」—— 把权限边界当成了 UI 提示。
3. 名单内但参数不合法 -> 回用法，不落任何动作。

## 名单是权威，角色目录是收窄

谁能在房间里驱动流程，权威是运行时的 `MAOS_APPROVERS`（铁律 6），
`scenarios/refund/roles.json` 只回答「这个账号属于哪个岗」。两者是**与**关系，
判据统一走 `maos/domain/refund/roles.py::can_approve`，本模块不自己写一套。

`/resolve` 比别的多一道：提交人还必须是这张工单的**承接岗**（或被指名的接单人）。
这正是「流程卡住后应由谁补偿」这一问落到可执行处的样子 —— 不然角色目录就只是
一份好看的通讯录，谁都能替支付运维签字说钱退了。

## `/confirm` 与 `/complain` 现在真的落库了（T122）

T117 时它们只到「解析 + 鉴权 + 返回一个待落库的结局」，`data` 里带着**该写什么**，
等 T120 把 `case_outcome` 建出来（那个过渡结局值 T129 已删，见 `KINDS`）。
两件事都已就位，于是这两条命令改为真写表：
走 `domain/refund/outcome.py` 的 `record_confirmation()` / `record_complaint()`
（两者写完各自重算一次四判据），返回 `KIND_DONE` 并把 `case_outcome` 那一行带回
`data`。字面值仍逐字取自跨轨契约 §E，本模块不自造措辞。

**它们碰的表只有 `case_outcome` / `complaint` / `notification.ack_at`**，一个新状态
都不加（铁律 9）：客户确认与投诉是业务对象自己的字段，不是 Task 状态。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from maos.agents.base import AgentIdentity
from maos.domain.refund import outcome as OUT, roles
from maos.model.client import Tier
from maos.skills.builtin.refund import compensate as CP
from maos.skills.invoker import SkillInvoker

log = logging.getLogger("maos.ingress.outcome_commands")

# ---------------------------------------------------------------- 命令词
CMD_ASSIGN = "assign"
CMD_RESOLVE = "resolve"
CMD_CONFIRM = "confirm"
CMD_COMPLAIN = "complain"
#: 补开一张补偿工单（T129）。自动开单失败之后**房间里唯一的救**，理由见
#: `handle_compensate` 的 docstring。
CMD_COMPENSATE = "compensate"
COMMANDS: tuple[str, ...] = (CMD_ASSIGN, CMD_RESOLVE, CMD_CONFIRM, CMD_COMPLAIN,
                             CMD_COMPENSATE)

#: 越权尝试的事件类型。**逐字对齐** `hiclaw/matrix_bus.py::EVENT_APPROVAL_DENIED`。
#: 这里硬编码而不 import：hiclaw 是可选依赖层，`maos.ingress` 不该为读一个字符串把它
#: 绑进来（取向同 `intent_dispatch.ENV_APPROVERS`）。不另起一个事件名是刻意的 ——
#: 「有人越权试了一次」在审计里必须只有一处可查，两个名字就意味着漏查一处 = 假绿。
EVENT_COMMAND_DENIED = "ApprovalDenied"

#: 被调 skill。改这一行等于改「谁把人工凭证落成观察」，该一眼看得见。
SKILL_COMPENSATION_CLOSE = "refund.compensation_close"

#: 房间里 `/resolve` 用的最小授权 identity（T122）。**形状逐字取自**
#: `maos/flows/scenario_7.py::TICKET_DESK_IDENTITY` —— 那一段是本流程的 CLI 范式，
#: 房间是它的第二个入口，两处给的权限必须一模一样，不然「同一件事在群里能做、
#: 在命令行不能做」就成了一条没人说得清的差异。
#:
#: **两个 skill 都要**：关单本身（`refund.compensation_close`），以及它要经 invoker
#: 调的 `payment.observe` —— 后者是全系统唯一写得进 settled 的 actor，关单借道它而
#: 不是自己开一条写终态的路（铁律 8）。少给一个，invoker 当场抛 PermissionDenied。
#:
#: 不 import scenario_7 那一个：`maos.ingress` 不该为拿一个常量把整个演示场景
#: （22 个 Agent、沙箱、靶场补丁）挂到自己的 import 图上。两份的一致由
#: `test_room_outcome_commands.py` 逐字段钉着。
TICKET_DESK_IDENTITY = AgentIdentity(
    agent_id="refund-ticket-desk",
    role="refund_payment_ops",
    duty="补偿工单的派单与关单：把人工线下凭证经 payment.observe 回填成一条观察",
    allowed_skills=frozenset({SKILL_COMPENSATION_CLOSE, "payment.observe"}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)

#: 被调 skill：开工单那一步。与关单分开是刻意的，见下。
SKILL_COMPENSATE = "refund.compensate"

#: 房间里**开**补偿工单用的 identity（T122）。形状逐字取自
#: `maos/flows/scenario_7.py::COMPENSATION_IDENTITY`。
#:
#: **与 `TICKET_DESK_IDENTITY` 分成两个，不合并成一个全能的**：开单是「这一单走不通了，
#: 留档最后观察、推 compensated、开张单」，关单是「我把钱线下退了，这是凭证」。
#: 现实里这是两个岗（主管开、支付运维关），合成一个 identity 等于说「开单的人自己
#: 就能签字说钱退了」—— 那正是 T117 要消灭的东西（见 `handle_resolve` 的第二道闸）。
#: 白名单机制就是用来表达这种最小授权的，省一个常量换来的是一条说不清的授权边界。
COMPENSATION_DESK_IDENTITY = AgentIdentity(
    agent_id="refund-compensation-desk",
    role="refund_compensation",
    duty="退款走不通之后的域内补偿收口：留档最后观察、开人工工单、推进 compensated",
    allowed_skills=frozenset({SKILL_COMPENSATE}),
    allowed_tools=frozenset(),
    write_scope=frozenset(),
    max_risk="M",
    model_tier=Tier.LIGHT,
)

# ---------------------------------------------------------------- 结局
KIND_DONE = "done"          # 动作已生效
KIND_DENIED = "denied"      # 鉴权没过，已落 ApprovalDenied
KIND_USAGE = "usage"        # 名单内但参数不合法（或这一步没生效），什么都没做
KIND_IGNORED = "ignored"    # 不是本模块的命令
#: T129 删掉了 `KIND_PENDING`。它是 T117 的过渡态（「解析与鉴权都过了，但落库那一步
#: 归 T120」），T122 把四条命令全都接上真落库之后**再没有任何一处产出过它**，
#: 全仓也没有任何消费方。留着一个永不出现的结局值，读代码的人会去找「什么时候会
#: pending」，而答案是「不会」—— 死常量的代价从来不是那一行，是那一趟白找。
KINDS = frozenset({KIND_DONE, KIND_DENIED, KIND_USAGE, KIND_IGNORED})

USAGE = (
    "用法：\n"
    "  /assign <工单号> <角色>            把补偿工单派给一个岗\n"
    "  /resolve <工单号> <凭证摘要>       提交线下凭证关单（第一个词当凭证引用）\n"
    "  /confirm <案号>                    客户确认收到退款\n"
    "  /complain <案号> <内容>            记一条客户投诉\n"
    "  /compensate <案号>                 补开一张补偿工单（自动开单没成时用）"
)

#: `SkillInvoker` 把失败拼成 `f"{type(exc).__name__}: {exc}"`（见 `invoker.py`）。
#: 回帖是给房间里的人读的，`ValueError: ` 这半截对他毫无信息量，只会让一句本来
#: 是人话的提示看起来像崩溃日志。**只去掉「标识符 + 冒号 + 空格」这一种形状**：
#: invoker 另外两种失败码 `skill_not_found:<名字>` 与 `precondition_failed:<字段>`
#: 冒号后面没有空格，不会被误伤 —— 那两个是**码**，去掉前缀就查不到出处了。
_EXC_PREFIX = re.compile(r"^([A-Za-z_]\w*): (?=\S)")


def humanize(error: object) -> str:
    """把 invoker 的错误串改成人话。空的一律报「未知原因」，不回一句空白。"""
    text = str(error or "").strip()
    if not text:
        return "未知原因"
    return _EXC_PREFIX.sub("", text, count=1)


@dataclass(frozen=True)
class CommandResult:
    """一条命令的结局。`text` 是要发回房间的话，给人读的。"""

    kind: str
    text: str
    command: str = ""
    case_id: str = ""
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"未知的命令结局：{self.kind!r}")


# ---------------------------------------------------------------- 解析
def parse(text: str) -> tuple[str, list[str]]:
    """把一行命令切成 `(命令词, 参数表)`。不是本模块的命令返回 `("", [])`。

    只按空白切，不做任何语义猜测：`/resolve` 的凭证摘要是自由文本，猜错一个词
    就等于替提交人改了他写的凭证。
    """
    line = str(text or "").strip()
    if not line.startswith("/"):
        return "", []
    parts = line[1:].split()
    if not parts:
        return "", []
    verb = parts[0].lower()
    return (verb, parts[1:]) if verb in COMMANDS else ("", [])


def case_id_of(verb: str, args: list[str]) -> str:
    """一条命令指向哪个案子。参数拿不到案号时返回空串。

    `/assign` `/resolve` 的第一个参数是**工单号**（`MT-<案号>`），`/confirm`
    `/complain` `/compensate` 的是**案号**本身 —— 补开的时候单还不存在，拿单号当
    入参就成了「请你按这个号补一张」，而单号该由开单方按案号推出，不由发命令的人
    指定。这个差别属于本模块，调用方不该各自重写一遍 ——
    router 要先按案号查 `tenant_id` 才调得动 `dispatch()`，而它认不认得工单前缀
    不该成为第二处知识（漏掉前缀的症状是「这个案子没在本房间跑过」，指错方向）。
    """
    if not args:
        return ""
    first = str(args[0])
    return CP.case_id_of_ticket(first) if verb in (CMD_ASSIGN, CMD_RESOLVE) else first


# ---------------------------------------------------------------- 鉴权
def _record_denied(store, *, sender: str, command: str, case_id: str, why: str,
                   extras: dict | None = None) -> None:
    """把越权尝试写进 `event_log`。写不进去也不许把异常抛给调用方。

    理由同 `guard.py` 模块抬头：「系统拒绝了一次越权操作」本身就是要拿给评委看的
    证据，吞掉就没了。而记录失败不该把一条**已经被正确拒绝**的命令升级成一次崩溃。
    """
    ex = dict(extras or {})
    try:
        store.append_event_log({
            "trace_id": str(ex.get("trace_id") or ""),
            "plan_id": str(ex.get("plan_id") or ""),
            "task_id": str(ex.get("task_id") or ""),
            "event_type": EVENT_COMMAND_DENIED,
            "reason": why,
            "detail": {"sender": sender, "command": command, "case_id": case_id,
                       "domain": "refund"},
        })
    except Exception as exc:                            # noqa: BLE001
        log.warning("越权命令记录写入失败（%s）", exc)


def _deny(store, *, sender: str, command: str, case_id: str, why: str,
          extras: dict | None = None) -> CommandResult:
    _record_denied(store, sender=sender, command=command, case_id=case_id, why=why,
                   extras=extras)
    return CommandResult(kind=KIND_DENIED, text=f"无权限：{why}",
                         command=command, case_id=case_id)


def in_approver_list(sender: str, approvers: Iterable[str]) -> bool:
    """只查名单，不按岗收窄 —— `roles.can_approve` 传空角色就是这个语义。"""
    return roles.can_approve(sender, "", approvers=approvers)


# ---------------------------------------------------------------- /assign
def handle_assign(args: list[str], *, store, tenant_id: str, sender: str,
                  approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/assign <工单号> <角色>` —— 把补偿工单派给一个岗。"""
    case_id = CP.case_id_of_ticket(args[0]) if args else ""
    if not in_approver_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_ASSIGN, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) != 2:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_ASSIGN,
                             case_id=case_id)

    role = roles.canonical_role(args[1])
    try:
        ticket = CP.assign_ticket(store, tenant_id=tenant_id, case_id=case_id, role=role)
    except (LookupError, ValueError) as exc:
        # 打错单号 / 派给一个不存在的岗：回一句人话，不落任何动作，也不崩掉房间。
        # 与鉴权失败分开成两种结局 —— 「你没权限」和「你写错了」排查方向完全不同。
        return CommandResult(kind=KIND_USAGE, text=f"派单未生效：{exc}",
                             command=CMD_ASSIGN, case_id=case_id)

    store.append_event_log({
        "trace_id": str((extras or {}).get("trace_id") or ""),
        "plan_id": str((extras or {}).get("plan_id") or ""),
        # `task_id` 与另两个一起挂（T129）：少这一格，「派单是在哪一步之后发生的」
        # 在 trace 里就断了，而同一条链上 `CompensationExecuted` / `CompensationResolved`
        # 都挂着付款那一步 —— 三条事件挂法不一致比三条都不挂更难查。
        "task_id": str((extras or {}).get("task_id") or ""),
        "event_type": CP.EVENT_COMPENSATION_ASSIGNED,
        "reason": f"{sender} 把工单派给 {role}",
        "detail": {"domain": "refund", "tenant_id": tenant_id, "case_id": case_id,
                   "ticket_id": CP.ticket_id_of(case_id), "operator": sender,
                   "assignee_role": ticket["assignee_role"],
                   "assignee": ticket["assignee"]},
    })
    return CommandResult(
        kind=KIND_DONE, command=CMD_ASSIGN, case_id=case_id,
        text=(f"已派单 {CP.ticket_id_of(case_id)} -> {roles.title_of(role)}"
              f"（{role}）· 接单人 {ticket['assignee']}\n"
              f"要做的事：{roles.duty_of(role)}"),
        data=dict(ticket))


# ---------------------------------------------------------------- /resolve
def handle_resolve(args: list[str], *, store, tenant_id: str, sender: str,
                   approvers: Iterable[str], identity=None,
                   extras: dict | None = None) -> CommandResult:
    """`/resolve <工单号> <凭证摘要>` —— 提交线下凭证关单。

    **第一个词当凭证引用**（渠道流水号 / 外部工单号），整句原样留档当摘要。
    这条约定要写在用法里给人看：凭证引用是这份人工凭证作为**外部事实**的出处，
    没有出处的凭证只是一句断言。
    """
    case_id = CP.case_id_of_ticket(args[0]) if args else ""
    if not in_approver_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_RESOLVE, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) < 2:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_RESOLVE,
                             case_id=case_id)

    try:
        ticket = CP.require_ticket(store, tenant_id, case_id)
    except LookupError as exc:
        return CommandResult(kind=KIND_USAGE, text=f"关单未生效：{exc}",
                             command=CMD_RESOLVE, case_id=case_id)

    # —— 第二道：他是不是这张单的承接人 ——
    # 这是角色目录真正干活的地方。放行任何名单内账号替支付运维签字说钱退了，
    # 「应由谁补偿」这一问就又回到了没有答案的状态。
    ticket_role = str(ticket.get("assignee_role") or "")
    named = str(ticket.get("assignee") or "")
    if sender != named and not roles.can_approve(sender, ticket_role, approvers=approvers):
        return _deny(store, sender=sender, command=CMD_RESOLVE, case_id=case_id,
                     why=(f"{sender} 不是工单 {CP.ticket_id_of(case_id)} 的承接人"
                          f"（承接岗 {ticket_role}，接单人 {named}）"),
                     extras=extras)

    if identity is None:
        raise ValueError(
            "handle_resolve 必须带 identity：关单要经 SkillInvoker 调 "
            f"{SKILL_COMPENSATION_CLOSE}，而 invoker 的白名单校验与审计行都挂在 identity 上")

    summary = " ".join(args[1:])
    res = SkillInvoker(identity, store).invoke(SKILL_COMPENSATION_CLOSE, {
        "tenant_id": tenant_id, "case_id": case_id, "operator": sender,
        # 第一个词当凭证引用，整句当摘要 —— 见 docstring。
        "evidence_ref": args[1], "summary": summary,
        "resolution_kind": CP.RESOLUTION_SETTLED,
    }, extras=dict(extras or {}))
    if res.status != "ok" or not isinstance(res.output, dict):
        # 关单没成不许回一句「已关单」。房间里的人会据此以为这件事办完了。
        # 措辞过一道 `humanize`（T129）：重复关单那一句本来是 `resolve_ticket` 写好的
        # 人话（「已于 … 关闭，要改结论请重开一张工单」），但它被 invoker 拼上了
        # `ValueError: ` 才回到房间 —— 房间里看到一行 Python 异常名，像是系统崩了，
        # 而实际上这是一次**被正确拒绝**的重复操作。
        return CommandResult(kind=KIND_USAGE, text=f"关单未生效：{humanize(res.error)}",
                             command=CMD_RESOLVE, case_id=case_id)

    out = res.output
    # 关单刚刚回填了一条观察，而「到账」这一判据的唯一依据就是观察 —— 不重算的话
    # `case_outcome` 停在关单**之前**的样子，房间里读到的四判据是过期的，
    # 而过期的结论比没有结论更坏（它看起来是新的）。`record_case_outcome` 本来就
    # 设计成可以反复调（T120：「结论随观察变」）。
    #
    # 重算失败不许把一次**已经生效**的关单翻成失败：钱确实退了、观察确实落了，
    # 回帖里说「关单未生效」会让人再去线下退一次。
    verdict = ""
    try:
        row = OUT.record_case_outcome(
            store, tenant_id=tenant_id, case_id=case_id,
            plan_id=str((extras or {}).get("plan_id") or "") or None)
        verdict = "\n" + _verdict_line(row)
    except Exception as exc:                            # noqa: BLE001
        log.warning("关单后重算四判据失败（%s）—— 关单本身已生效", exc)
    return CommandResult(
        kind=KIND_DONE, command=CMD_RESOLVE, case_id=case_id,
        text=(f"已关单 {CP.ticket_id_of(case_id)}（{out['resolution_kind']}）· "
              f"提交人 {sender}\n"
              f"回填观察：{out['observation_id']}（observed_state="
              f"{out['observed_state']}，来源 人工线下凭证）" + verdict),
        data=dict(out))


# ---------------------------------------------------------------- /compensate
def handle_compensate(args: list[str], *, store, tenant_id: str, sender: str,
                      approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/compensate <案号>` —— 补开一张补偿工单（T129）。

    ## 为什么必须有这条命令

    自动开单在 `router._compensate_if_stuck` 里，它失败时只回一句「⚠️ 工单没开出来」。
    在那之后房间里**没有任何一条命令能把单补上**：

      · `/approve` 被「这个案子已经在本房间跑过一次，不重跑」那道闸拦死（拦得对 ——
        重跑会给同一个案子产出第二套付款请求与观察）；
      · `/assign` `/resolve` 都走 `CP.require_ticket`，而它明写「不补开」。

    于是 `MAOS_INGRESS_DB` 指到文件的长命库里，这种案子**永久锁死，只能换库**。
    演示当场撞上就没救 —— 这条命令就是那个救。

    ## 为什么是新命令，不是放松 `/approve` 那道闸

    放松闸意味着 `/approve` 有时是「放行并执行」、有时是「什么都不跑只补张单」，
    同一个词两种语义，而人在房间里按的是同一个键。补开是一件**独立的、人做的决定**
    （理由同 `_compensate_if_stuck` 的模块注释：补偿是看过事实之后的决定），
    给它自己的词，回帖、审计、权限三处才都指得准。

    ## 幂等：已经有单就不开第二张

    判据是 `CP.ticket_of()` 查得到 —— 不是「biz_status 是不是 compensated」。
    后者答不了这个问题：自动开单失败的案子**状态可能已经推到 compensated 了**
    （`refund.compensate` 里推状态在开单之后，但更早的失败点会留下别的组合），
    而这条命令要补的恰恰是「有状态、没有单」那一格。工单号由案号推出
    （`CP.ticket_id_of`），所以连打两次不会产生两个号，但会产生**两行**
    `compensation_record`（主键带 `executed_at`）—— 那就是两张单，第二行会把
    `_update_ticket` 的派单/关单结果甩在前一行上。所以在这里拦住。

    ## 业务判据留在 skill 里，本函数不复制一份

    「已经到账的案子不许补偿」由 `refund.compensate` 自己抛（它查 `biz_status`）。
    本函数只管三道：名单闸、幂等、把失败翻成人话。判据写两份的症状是 CLI 与房间
    对同一个案子给出不同答案。

    与自动开单那一条的**判据刻意不同**：`_compensate_if_stuck` 只在最后一条观察是
    `failed` 时才动手（`unknown` / `unobserved` 一律不补，下落不明不等于失败）。
    这条命令没有那道收窄 —— 打它的是人，而「这一单我看过了，卡住了，开单」正是
    人该做的决定；机器不替他把这个决定也做了。
    """
    case_id = str(args[0]) if args else ""
    if not in_approver_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_COMPENSATE, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) != 1:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_COMPENSATE,
                             case_id=case_id)

    existing = CP.ticket_of(store, tenant_id, case_id)
    if existing is not None:
        ticket_id = CP.ticket_id_of(case_id)
        role = str(existing.get("assignee_role") or "")
        seat = (f"（承接岗 {roles.title_of(role)}，接单人 "
                f"{existing.get('assignee') or '未指名'}）" if role else "")
        return CommandResult(
            kind=KIND_USAGE, command=CMD_COMPENSATE, case_id=case_id,
            text=(f"不用补开：{case_id} 已经有工单 {ticket_id}{seat}\n"
                  f"  改派：/assign {ticket_id} payment_ops\n"
                  f"  关单：/resolve {ticket_id} <渠道流水号> <线下凭证摘要>"),
            data=dict(existing))

    res = SkillInvoker(COMPENSATION_DESK_IDENTITY, store).invoke(
        SKILL_COMPENSATE,
        {"tenant_id": tenant_id, "case_id": case_id, "operator": sender,
         # 说清这一张是**补**开的：审计上「自动开单没成、人在房间里补了一张」
         # 与「机器按失败观察开的」是两件事，事件里读得出区别才查得清。
         "reason": f"{sender} 在房间里补开：自动开单没成，钱没退出去，转人工线下退款"},
        extras=dict(extras or {}))
    if res.status != "ok" or not isinstance(res.output, dict):
        # 补开没成不许回一句「已补开」。这条命令存在的理由就是「上一次没开成」，
        # 这一次也没开成时更要说实话，不然人会以为单已经在等人接。
        return CommandResult(kind=KIND_USAGE, text=f"补开未生效：{humanize(res.error)}",
                             command=CMD_COMPENSATE, case_id=case_id)

    out = res.output
    opened = out.get("ticket") if isinstance(out.get("ticket"), dict) else {}
    ticket_id = CP.ticket_id_of(case_id)
    role = str(opened.get("assignee_role") or "")
    seat = (f" · 当前承接岗 {roles.title_of(role)}（{role}）"
            f"· 接单人 {opened.get('assignee') or '未指名'}" if role else "")
    return CommandResult(
        kind=KIND_DONE, command=CMD_COMPENSATE, case_id=case_id,
        text=(f"已补开补偿工单：{ticket_id}{seat}\n"
              # 最后观察到的下落原样报出来 —— 补偿记录里留的就是它，人工对账认的也是它，
              # 房间里读不到这一格，接单的人得自己再去翻一次库（铁律 8）。
              f"最后观察到的下落：{out.get('last_observed_state')}"
              f"（业务状态 {out.get('biz_status')}）\n"
              f"  改派：/assign {ticket_id} payment_ops\n"
              f"  关单：/resolve {ticket_id} <渠道流水号> <线下凭证摘要>"),
        data=dict(out))


# ---------------------------------------------------------------- /confirm /complain
#: 回帖里报出去的四判据。顺序固定，读的人每次看到的是同一张脸。
_VERDICT_FIELDS = ("arrival", "customer_confirmation", "manual_correction", "complaint")


def _verdict_line(row: dict) -> str:
    """四判据的一行摘要。`business_success` 单列 —— 它是那四条算出来的结论，
    与判据本身混在一行会让人以为它也是一条独立观察。"""
    body = " ".join(f"{k}={row.get(k)}" for k in _VERDICT_FIELDS)
    return f"四判据：{body}；业务是否成功={bool(row.get('business_success'))}"


def handle_confirm(args: list[str], *, store, tenant_id: str, sender: str,
                   approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/confirm <案号>` —— 客户确认收到退款。**真写 `case_outcome`**（T122）。

    落库走 `outcome.record_confirmation()`：它顺手把该 case 的通知标成已 ack，
    再重算四判据。命令层**不自己拼 UPDATE** —— 「客户认了没有」在
    `notification.ack_at` 与 `case_outcome.customer_confirmation` 上各有一份，
    两处只能由同一个函数一起写，分开写的症状是「四判据说确认了，晋升规则说没有」。

    一条通知都没发出去时 `record_confirmation` 会抛 `OutcomeError`（客户无从确认）。
    那是**一句人话，不是崩溃**：翻成 `KIND_USAGE` 回帖，不落任何动作。
    """
    case_id = str(args[0]) if args else ""
    if not in_approver_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_CONFIRM, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) != 1:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_CONFIRM,
                             case_id=case_id)
    try:
        # 字面值 `confirmed` 取自跨轨契约 §E 的 `customer_confirmation` 值域。
        row = OUT.record_confirmation(store, tenant_id=tenant_id, case_id=case_id,
                                      decision=OUT.CONFIRMATION_CONFIRMED,
                                      channel="room",
                                      plan_id=str((extras or {}).get("plan_id") or "") or None)
    except OUT.OutcomeError as exc:
        return CommandResult(kind=KIND_USAGE, text=f"确认未生效：{exc}",
                             command=CMD_CONFIRM, case_id=case_id)
    return CommandResult(
        kind=KIND_DONE, command=CMD_CONFIRM, case_id=case_id,
        text=(f"已记下 {case_id} 的客户确认（{sender} 代录，渠道 room）\n"
              f"{_verdict_line(row)}"),
        data=dict(row))


def handle_complain(args: list[str], *, store, tenant_id: str, sender: str,
                    approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/complain <案号> <内容>` —— 记一条客户投诉。**真写 `complaint`**（T122）。

    投诉一开就是 `open`，而 open 一票否决 `business_success`（契约 §E）——
    回帖因此必须把重算后的结论一起说出来：钱到了、客户也签收了，只要投诉还开着，
    这单业务就没算成，房间里看不到这一点就等于没记。
    """
    case_id = str(args[0]) if args else ""
    if not in_approver_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_COMPLAIN, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) < 2:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_COMPLAIN,
                             case_id=case_id)
    content = " ".join(args[1:])
    try:
        row = OUT.record_complaint(store, tenant_id=tenant_id, case_id=case_id,
                                   content=content, channel="room",
                                   plan_id=str((extras or {}).get("plan_id") or "") or None)
    except OUT.OutcomeError as exc:
        return CommandResult(kind=KIND_USAGE, text=f"投诉未记下：{exc}",
                             command=CMD_COMPLAIN, case_id=case_id)
    return CommandResult(
        kind=KIND_DONE, command=CMD_COMPLAIN, case_id=case_id,
        # 存摘要不存原文（`outcome.digest_of`）—— 回帖里报摘要，人才回查得到是哪一条。
        text=(f"已记下 {case_id} 的投诉（{sender} 代录，摘要 "
              f"{OUT.digest_of(content)}）\n{_verdict_line(row)}"),
        data=dict(row))


# ---------------------------------------------------------------- 派发
#: 命令词 -> 处理函数。`maos/ingress/router.py::handle_outcome` 经 `dispatch()` 接这
#: 一张表，router 那边 `CMD_OUTCOME = _outcome_cmds.COMMANDS` —— 词表只有这一份，
#: 加一条命令只需改 `COMMANDS` 与本表，router 的 `KNOWN_VERBS` 与 `_dispatch`
#: 自动跟上（`test_room_outcome_commands.py` 两处都钉着）。
COMMAND_HANDLERS: dict[str, Callable[..., CommandResult]] = {
    CMD_ASSIGN: handle_assign,
    CMD_RESOLVE: handle_resolve,
    CMD_CONFIRM: handle_confirm,
    CMD_COMPLAIN: handle_complain,
    CMD_COMPENSATE: handle_compensate,
}


def dispatch(text: str, *, store, tenant_id: str, sender: str,
             approvers: Iterable[str], identity=None,
             extras: dict | None = None) -> CommandResult:
    """把一行文本派发到对应的处理函数。不是本模块的命令返回 `KIND_IGNORED`。

    `identity` 只有 `/resolve` 用得上（它要经 invoker 调 skill），别的命令不需要，
    所以按 keyword 透传而不是塞进每个处理函数的签名 —— 让不需要它的三条命令
    也背一个必填参数，会诱使调用方随手传一个「反正用不到」的假 identity。
    """
    verb, args = parse(text)
    if not verb:
        return CommandResult(kind=KIND_IGNORED, text="")
    kwargs: dict = {"store": store, "tenant_id": tenant_id, "sender": sender,
                    "approvers": approvers, "extras": extras}
    if verb == CMD_RESOLVE:
        kwargs["identity"] = identity
    return COMMAND_HANDLERS[verb](args, **kwargs)
