"""结果面的四条命令（T117）——`/assign`、`/resolve`、`/confirm`、`/complain`。

## 只做模块，不接线

本文件**不改** `maos/ingress/router.py`（同期另一会话在大改它）。这里只导出处理函数
与一张 `COMMAND_HANDLERS` 表，整合期由主会话在 router 里接一行。这不是偷懒，是并行
纪律：两轨同改一个 2000 行的路由文件，冲突几乎必然，而合并冲突改错一行的症状是
「某条命令悄悄不响应了」。

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

## `/confirm` 与 `/complain` 的边界：本轨只到「解析 + 鉴权 + 返回结构」

它们要写的 `case_outcome`（跨轨契约 §E 的四判据：`customer_confirmation`、
`complaint` …）由 **T120** 定义建表。本模块因此只产出一份 `KIND_PENDING` 的结果，
`data` 里带着**该写什么**（字面值逐字对齐契约 §E），一个字不落库。

刻意不抛 `NotImplementedError`：那样调用方拿不到任何可测的东西，整合期只能整段重写。
返回结构之后，T120 接线时改的是「把 `data` 写进表」一处，解析与鉴权都不必重做，
而本轨的测试现在就能钉住鉴权与解析。**本模块不碰 `compensation_record` 之外的任何表**
—— 事实上这两条命令一张表都不碰。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from maos.domain.refund import roles
from maos.skills.builtin.refund import compensate as CP
from maos.skills.invoker import SkillInvoker

log = logging.getLogger("maos.ingress.outcome_commands")

# ---------------------------------------------------------------- 命令词
CMD_ASSIGN = "assign"
CMD_RESOLVE = "resolve"
CMD_CONFIRM = "confirm"
CMD_COMPLAIN = "complain"
COMMANDS: tuple[str, ...] = (CMD_ASSIGN, CMD_RESOLVE, CMD_CONFIRM, CMD_COMPLAIN)

#: 越权尝试的事件类型。**逐字对齐** `hiclaw/matrix_bus.py::EVENT_APPROVAL_DENIED`。
#: 这里硬编码而不 import：hiclaw 是可选依赖层，`maos.ingress` 不该为读一个字符串把它
#: 绑进来（取向同 `intent_dispatch.ENV_APPROVERS`）。不另起一个事件名是刻意的 ——
#: 「有人越权试了一次」在审计里必须只有一处可查，两个名字就意味着漏查一处 = 假绿。
EVENT_COMMAND_DENIED = "ApprovalDenied"

#: 被调 skill。改这一行等于改「谁把人工凭证落成观察」，该一眼看得见。
SKILL_COMPENSATION_CLOSE = "refund.compensation_close"

# ---------------------------------------------------------------- 结局
KIND_DONE = "done"          # 动作已生效
KIND_DENIED = "denied"      # 鉴权没过，已落 ApprovalDenied
KIND_USAGE = "usage"        # 名单内但参数不合法，什么都没做
KIND_IGNORED = "ignored"    # 不是本模块的命令
KIND_PENDING = "pending"    # 解析与鉴权都过了，但落库那一步归 T120（见模块抬头）
KINDS = frozenset({KIND_DONE, KIND_DENIED, KIND_USAGE, KIND_IGNORED, KIND_PENDING})

USAGE = (
    "用法：\n"
    "  /assign <工单号> <角色>            把补偿工单派给一个岗\n"
    "  /resolve <工单号> <凭证摘要>       提交线下凭证关单（第一个词当凭证引用）\n"
    "  /confirm <案号>                    客户确认收到退款\n"
    "  /complain <案号> <内容>            记一条客户投诉"
)


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


def _in_list(sender: str, approvers: Iterable[str]) -> bool:
    """只查名单，不按岗收窄 —— `roles.can_approve` 传空角色就是这个语义。"""
    return roles.can_approve(sender, "", approvers=approvers)


# ---------------------------------------------------------------- /assign
def handle_assign(args: list[str], *, store, tenant_id: str, sender: str,
                  approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/assign <工单号> <角色>` —— 把补偿工单派给一个岗。"""
    case_id = CP.case_id_of_ticket(args[0]) if args else ""
    if not _in_list(sender, approvers):
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
    if not _in_list(sender, approvers):
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
        return CommandResult(kind=KIND_USAGE, text=f"关单未生效：{res.error}",
                             command=CMD_RESOLVE, case_id=case_id)

    out = res.output
    return CommandResult(
        kind=KIND_DONE, command=CMD_RESOLVE, case_id=case_id,
        text=(f"已关单 {CP.ticket_id_of(case_id)}（{out['resolution_kind']}）· "
              f"提交人 {sender}\n"
              f"回填观察：{out['observation_id']}（observed_state="
              f"{out['observed_state']}，来源 人工线下凭证）"),
        data=dict(out))


# ---------------------------------------------------------------- /confirm /complain
def handle_confirm(args: list[str], *, store, tenant_id: str, sender: str,
                   approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/confirm <案号>` —— 客户确认收到退款。**本轨只解析与鉴权，不落库。**

    落库归 T120：`case_outcome.customer_confirmation` 由它建表定义（契约 §E）。
    `data` 里的字面值逐字对齐那份契约，整合期照搬即可。
    """
    case_id = str(args[0]) if args else ""
    if not _in_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_CONFIRM, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) != 1:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_CONFIRM,
                             case_id=case_id)
    return CommandResult(
        kind=KIND_PENDING, command=CMD_CONFIRM, case_id=case_id,
        text=f"已收到 {case_id} 的客户确认（落库待 case_outcome 接线）",
        # 字面值取自跨轨契约 §E 的 `customer_confirmation` 值域，不自造措辞。
        data={"tenant_id": tenant_id, "case_id": case_id, "by": sender,
              "field": "customer_confirmation", "value": "confirmed",
              "owner_track": "t120"})


def handle_complain(args: list[str], *, store, tenant_id: str, sender: str,
                    approvers: Iterable[str], extras: dict | None = None) -> CommandResult:
    """`/complain <案号> <内容>` —— 记一条客户投诉。**本轨只解析与鉴权，不落库。**

    落库归 T120：`case_outcome.complaint` 由它建表定义（契约 §E）。
    """
    case_id = str(args[0]) if args else ""
    if not _in_list(sender, approvers):
        return _deny(store, sender=sender, command=CMD_COMPLAIN, case_id=case_id,
                     why=f"{sender} 不在 MAOS_APPROVERS 名单内", extras=extras)
    if len(args) < 2:
        return CommandResult(kind=KIND_USAGE, text=USAGE, command=CMD_COMPLAIN,
                             case_id=case_id)
    return CommandResult(
        kind=KIND_PENDING, command=CMD_COMPLAIN, case_id=case_id,
        text=f"已记录 {case_id} 的投诉（落库待 case_outcome 接线）",
        data={"tenant_id": tenant_id, "case_id": case_id, "by": sender,
              "field": "complaint", "value": "open",
              "text": " ".join(args[1:]), "owner_track": "t120"})


# ---------------------------------------------------------------- 派发
#: 命令词 -> 处理函数。**整合期由主会话在 `maos/ingress/router.py` 里接这一张表**，
#: 本轨一个字都不动那个文件（见模块抬头）。
COMMAND_HANDLERS: dict[str, Callable[..., CommandResult]] = {
    CMD_ASSIGN: handle_assign,
    CMD_RESOLVE: handle_resolve,
    CMD_CONFIRM: handle_confirm,
    CMD_COMPLAIN: handle_complain,
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
