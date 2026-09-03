"""合议收口 —— 五岗说完之后，对 boss 说的那一句「所以这一单批还是不批」。

五条结论互相矛盾时谁说了算，由 `decide()` 一处回答：规则审核岗说批准、证据核验岗说
缺件、风险岗说 low，这三句话本身都不错，错的是**没有人把它们合成一个建议**。

`decide()` 是**纯函数**：不 import `time`、不读环境变量、不碰 `model`。同一份 reports
连跑两次逐字一致 —— 它是 boss 唯一能信的那一行，模型只允许在房间里复述它（R1）。
真值表自上而下第一条命中为准，顺序本身就是判断，见 `_recommend` 的逐行注释。

**默认不放行**：算不清楚（某岗汇总失败、reports 不足五岗、policy 缺 decision）一律
`need_more`，让人来看。这是铁律 8 的直接后果 —— MAOS 不持有权威事实，拿不准的时候
沉默地放行比说一句「我拿不准」危险得多。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from maos.roundtable.team import TEAM_ORDER, TITLES

log = logging.getLogger("maos.roundtable")

#: 收口卡里不许出现的五个词（铁律 8 / 契约 §2.2）。MAOS 不持有权威事实 ——
#: 钱有没有到账归外部系统，房间里只能说「建议」与「预演」。
#:
#: 做成模块级常量而不是写死在测试里：将来加词只加这一处，测试引用它就自动跟上。
FORBIDDEN_WORDS: tuple[str, ...] = ("已批准", "已退款", "已到账", "已放款", "已完成")

#: 禁止词的中性改写。**只作用于收口卡**，五岗原文在 `Verdict.seats` 里原样留着。
#:
#: 为什么要运行时改写、而不是只写一条守卫测试：`blockers` 里的证据缺口与风险理由
#: 是 skill 的出参，语料一换就可能带进「已到账」—— 那时候红的是测试，而房间里
#: 已经发出去了一张替外部系统宣布终态的卡。改写保留了「观察到什么」，只去掉
#: 「谁在宣布」：外部系统显示入账，和 MAOS 说钱到了，是两件事。
_NEUTRAL: dict[str, str] = {
    "已批准": "外部系统显示批准",
    "已退款": "外部系统显示退款",
    "已到账": "外部系统显示入账",
    "已放款": "外部系统显示付款",
    "已完成": "外部系统显示完成",
}

#: 风险高档时的审批升档表。已经是最高档就保持 —— 升到一个不存在的角色，
#: 房间里那句「请 X 拍板」就点不到任何人。
ESCALATION: dict[str, str] = {
    "supervisor": "finance_manager",
    "finance_manager": "finance_manager",
}

#: 四种建议。`recommend` 是 `Verdict` 上的一个字段，**不是** Task 状态（铁律 9）——
#: 业务结论归业务对象自己，状态机里一个新状态都不许加。
RECOMMENDS: tuple[str, ...] = ("approve", "reject", "need_more", "escalate")

_NO_FACTS = "缺少可核算的事实"


@dataclass(frozen=True)
class Verdict:
    """给 boss 的一张批复建议卡。**建议，不是处置** —— 真正动钱仍要审批人发 /approve。"""

    case_id: str            # 来自 checked["case_id"]，取不到给 ""
    recommend: str          # RECOMMENDS 四选一
    headline: str           # 给 boss 的一句话，措辞见 _headline
    reasons: list[str]      # 逐条依据，每条前缀「岗位名：」，顺序同 TEAM_ORDER
    blockers: list[str]     # 拦路条；空列表 = 无拦路条
    approver_role: str      # 谁来拍板；风险高档时已升过档
    amount_preview: str     # finance.amount_approved 原样字符串；算不出给 ""
    next_command: str       # boss 下一步打什么
    seats: dict[str, dict]  # agent_id -> 该岗 data 原样，可追溯用


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
def _clean(text: object) -> str:
    """把任意一段要进收口卡的文字过一遍禁止词。非字符串一律 `str()` 之后再过。"""
    out = str(text if text is not None else "")
    for word, neutral in _NEUTRAL.items():
        if word in out:
            log.warning("收口卡里出现禁止词 %s，已改写为 %s", word, neutral)
            out = out.replace(word, neutral)
    return out


def _seats_of(reports: list) -> dict[str, dict]:
    """`agent_id -> data` 原样。**不拷贝**：seats 的用处就是回头核对模型有没有编数字，
    拷一份会让「卡上这个数字来自哪一岗」多一次转手。

    不是 `StageReport` 的东西（None、字符串、少了字段的假件）一律跳过 —— 收口卡
    宁可少读一岗按缺席算，也不许在这里抛：房间是旁路，抛了就是五岗说完一片安静。
    """
    seats: dict[str, dict] = {}
    for report in reports or []:
        agent_id = str(getattr(report, "agent_id", "") or "")
        data = getattr(report, "data", None)
        if agent_id:
            seats[agent_id] = data if isinstance(data, dict) else {}
    return seats


def _recommend(seats: dict[str, dict]) -> str:
    """真值表，自上而下第一条命中为准。**顺序不许调换** —— 顺序本身就是判断。"""
    policy = seats.get("refund-policy") or {}
    evidence = seats.get("refund-evidence") or {}
    risk = seats.get("refund-risk") or {}
    finance = seats.get("refund-finance") or {}
    decision = str(policy.get("decision") or "")

    # 1. 规则说不退就是不退。证据齐、风险低都不能把它翻成批准 —— 那是拿观察去改裁定。
    if decision == "reject":
        return "reject"
    # 2. 算不出金额就不能让人拍板：拍板意味着按某个数字付钱。
    #    finance 岗**缺席**也算算不出（契约 §2.1 第 6 行把「reports 不足五条」归到
    #    need_more；缺席比 preview_ran=False 更算不出，不该反而放行）。
    if ("refund-finance" not in seats or finance.get("preview_ran") is False
            or finance.get("error")):
        return "need_more"
    # 3. 缺件是可补的，不是拒绝。
    if (str(evidence.get("verdict") or "") in ("missing", "unavailable")
            and list(evidence.get("gaps") or [])):
        return "need_more"
    # 4. 风险岗只提示不裁定，所以它把结论推高一档、不推翻。
    if str(risk.get("level") or "") == "high":
        return "escalate"
    # 5. 五岗全绿。**到齐、且没有一岗带 error 才算全绿**（契约 §2.1 第 6 行点名的
    #    「某岗 error」「reports 不足五条」在这里落地）：证据岗汇总失败时 data 只有
    #    一个 `error` 键，`verdict` 与 `gaps` 双双缺席，第 3 行就命中不了 —— 少了
    #    这道判据，一个炸掉的岗位会安安静静地变成「建议批复」。
    if decision == "approve" and _all_seats_sound(seats):
        return "approve"
    # 6. 默认不放行：policy 缺 decision、某岗 error、reports 不足五条都走这里。
    return "need_more"


def _all_seats_sound(seats: dict[str, dict]) -> bool:
    """五岗到齐且都汇总成功。`error` 判真值不判键是否存在 —— 财务岗的 data
    **总是**带一个缺省为 None 的 `error` 键，判键存在就没有一单能放行了。"""
    return all(a in seats and not (seats[a] or {}).get("error") for a in TEAM_ORDER)


def _seat_line(agent_id: str, data: dict) -> str:
    """一岗的依据摘要。全部 `.get()` 带缺省 —— 读到不存在的键要兜住、不许抛。"""
    if agent_id == "refund-intake":
        return (f"订单 {data.get('order_id') or '未知'}，实付 {data.get('amount_paid')}，"
                f"申报 {data.get('amount_claimed')}，"
                f"随案证据 {data.get('evidence_count', 0)} 份"
                + ("，申报高于实付" if data.get("over_paid") else ""))
    if agent_id == "refund-policy":
        matched = list(data.get("matched_rules") or [])
        return (f"裁定 {data.get('decision') or '未给裁定'}，"
                f"依据 {data.get('deciding_rule') or '无单条规则决定'}"
                f"（命中 {len(matched)} 条，政策 v{data.get('pinned_policy_version')}）")
    if agent_id == "refund-evidence":
        gaps = list(data.get("gaps") or [])
        items = list(data.get("items") or [])
        return (f"核验结论 {data.get('verdict') or '未给结论'}，"
                f"缺口 {len(gaps)} 项，随案材料 {len(items)} 份")
    if agent_id == "refund-risk":
        reasons = list(data.get("reasons") or [])
        return (f"风险 {data.get('level') or '未分档'}（评分 {data.get('score')}），"
                f"命中 {len(reasons)} 条信号")
    if agent_id == "refund-finance":
        if data.get("error"):
            return f"核算预演失败（{data['error']}），金额待重算"
        if not data.get("preview_ran"):
            return "未做核算预演（裁定驳回或前置不成立），无金额"
        return (f"核算预演 {data.get('amount_approved')}"
                f"（政策 v{data.get('policy_version')}），未落账")
    return str(data)


def _reasons(seats: dict[str, dict]) -> list[str]:
    """逐岗一行，前缀岗位名，顺序同 TEAM_ORDER。缺席的岗不占行 —— 一行
    「某某岗：无」在卡上和「某某岗没说话」长得一样，而这两件事要分得开。"""
    out: list[str] = []
    for agent_id in TEAM_ORDER:
        data = seats.get(agent_id)
        if data is None:
            continue
        title = TITLES.get(agent_id, agent_id)
        error = data.get("error")
        if error and agent_id != "refund-finance":
            out.append(_clean(f"{title}：事实汇总失败（{error}），本岗这一轮没有结论"))
            continue
        out.append(_clean(f"{title}：{_seat_line(agent_id, data)}"))
    return out


def _blockers(seats: dict[str, dict]) -> list[str]:
    """拦路条。**与 recommend 无关，命中即列** —— 批复建议里也要能看见风险，
    否则「建议批复」四个字会把风险岗那几句话盖掉。"""
    out: list[str] = []
    evidence = seats.get("refund-evidence") or {}
    risk = seats.get("refund-risk") or {}
    finance = seats.get("refund-finance") or {}

    out += [_clean(f"证据：{gap}") for gap in list(evidence.get("gaps") or [])]
    if str(risk.get("level") or "") != "low":
        out += [_clean(f"风险：{reason}") for reason in list(risk.get("reasons") or [])]
    if finance.get("error"):
        out.append(_clean(f"核算：{finance['error']}"))
    # 岗位汇总失败（`_round` 兜底写下的 `{"error": ...}`）。财务岗归上一条的
    # 「核算：」前缀，不在这里列第二遍 —— 同一个错误在卡上出现两次，人会以为是两件事。
    for agent_id in TEAM_ORDER:
        data = seats.get(agent_id) or {}
        if agent_id != "refund-finance" and data.get("error"):
            out.append(_clean(
                f"{TITLES.get(agent_id, agent_id)}事实汇总失败：{data['error']}"))
    return out


def _approver(recommend: str, seats: dict[str, dict], blockers: list[str]) -> str:
    """谁来拍板。只在 `escalate` 时升档；已是最高档就保持并记一条拦路条。"""
    role = str((seats.get("refund-policy") or {}).get("approver_role") or "")
    if recommend != "escalate":
        return role
    upgraded = ESCALATION.get(role)
    if upgraded is None:
        # 认不出的审批角色不猜着升 —— 升到一个不存在的角色，房间里点不到人。
        log.warning("审批角色 %r 不在升档表里，风险高档不升档", role)
        return role
    if upgraded == role:
        blockers.append("风险：已是最高审批档，建议二人复核")
    return upgraded


def _headline(recommend: str, seats: dict[str, dict], blockers: list[str],
              amount_preview: str, approver_role: str) -> str:
    """给 boss 的一句话。**逐字模板**（契约 §2.2），四轨的测试都按它断言。"""
    if recommend == "approve":
        return _clean(f"建议批复 · 核准预演 {amount_preview} · 请 {approver_role} 拍板")
    if recommend == "reject":
        why = (seats.get("refund-policy") or {}).get("why") or "规则审核岗未给判定理由"
        return _clean(f"不建议批复 · {why}")
    if recommend == "escalate":
        level = (seats.get("refund-risk") or {}).get("level") or "未分档"
        return _clean(f"建议升级审批 · 风险 {level} · 请 {approver_role} 复核")
    return _clean(f"需补件后再议 · {blockers[0] if blockers else _NO_FACTS}")


def _next_command(recommend: str, case_id: str, seats: dict[str, dict]) -> str:
    """boss 下一步打什么。命令要能原样复制 —— 让人回头翻手册的收口卡等于没收口。"""
    if recommend in ("approve", "escalate"):
        return f"/approve {case_id}"
    if recommend == "reject":
        return f"/reject {case_id} <理由>"
    order_id = (seats.get("refund-intake") or {}).get("order_id") or ""
    return f"补齐材料后重发 /refund {order_id} <诉求类型>"


# --------------------------------------------------------------------------
# 对外
# --------------------------------------------------------------------------
def decide(reports: list, *, case_id: str = "") -> Verdict:
    """五岗结论 -> 一张批复建议卡。**零模型、纯规则、可复现**。

    `reports` 是 `RefundRoundtable.on_preflight()` 返回的 `list[StageReport]`。
    残缺输入（空列表、某岗 `data={"error": ...}`、`data={}`、不足五岗）一律吃下 ——
    这里抛异常等于房间里五岗说完之后一片安静，而 boss 正在等一句话。
    """
    seats = _seats_of(reports)
    recommend = _recommend(seats)
    blockers = _blockers(seats)
    approver_role = _approver(recommend, seats, blockers)   # 升档时会追加一条 blocker

    amount = (seats.get("refund-finance") or {}).get("amount_approved")
    amount_preview = "" if amount is None else str(amount)
    case = str(case_id or "")

    return Verdict(
        case_id=case,
        recommend=recommend,
        headline=_headline(recommend, seats, blockers, amount_preview, approver_role),
        reasons=_reasons(seats),
        blockers=blockers,
        approver_role=approver_role,
        amount_preview=amount_preview,
        next_command=_next_command(recommend, case, seats),
        seats=seats,
    )
