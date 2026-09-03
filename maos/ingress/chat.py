"""群里的闲聊 —— 不是命令的那些话，由真模型接一句，**只依据事实说话**。

router 对非命令文本的既定姿态是「一声不吭」：飞书 / 企微群里人来人往，机器人
对每句闲聊都回用法提示是骚扰。但在**专门的审批房间**里（Matrix 的「MAOS 审批」），
对面只有机器人一个 —— 人打一句「maos」「你能干什么」得到的沉默，与「机器人挂了」
无法分辨。真房间实测正是这个症状。

所以这里是**可选件**：router 缺省不装（`chat=None`，行为与从前逐字一致），
房间入口装上它。装上之后：

  · 有真模型（`MAOS_LLM_*` 配齐）—— 模型看着【事实】回一句人话。事实由 router
    现拼：可用命令、底账里的订单、本会话的待办与证据、上一张申请表的结论。
    模型**不持有事实**（铁律 8）：它不能编一个底账里没有的订单，不能承诺退款结果，
    要起单只能把人引到 ``/refund`` 或申请表上。
  · 没真模型 —— 回一句固定话术说明能做什么。`ScriptedModelClient` 未命中脚本
    返回的是字面量 ``{}``，房间里刷一句 ``{}`` 比不回还糟（口径同 `hiclaw/ap_room.py`
    的不变量 2），所以缺模型不降级到它，改回固定话术。

模型调用失败（网关 5xx、超时、key 失效）也回固定话术并记 WARNING：一次网关抖动
不该让房间里的人以为机器人死了。
"""

from __future__ import annotations

import logging

from maos.model.client import ModelClient, ScriptedModelClient, Tier, select_model_client

log = logging.getLogger("maos.ingress.chat")

#: 单句回话的字数上限。给模型的软约束，不硬截 —— 硬截会把话切在半句上。
REPLY_LIMIT = 200

SYSTEM_TMPL = """你是「MAOS 退款助手」，在公司内部的退款审批群里值班。群里的人会用自然语言跟你说话。

规矩：
1. 只依据【事实】说话。订单、金额、政策、待办、申请表结论一律以【事实】为准，一个数字都不许改、不许编。【事实】里没有的订单，就说底账里没有这个订单。
2. 你自己不能退款、不能承诺退款结果，也不能替人放行。要起单，引导对方发 /refund 命令或把退款申请表（CSV）拖进群；要放行，引导审批人发 /approve <case_id>。
3. 说人话，像同事在群里回一句。不要 JSON、不要编号长文，不超过 {limit} 字。
4. 对方只是打招呼或试探（比如只发一个词），就用一两句说明你能做什么。
5. 对方问的事超出退款处置范围，说明你只管这个群的退款审批，不要编答案。
6. 有人问你是谁、有哪些岗位 / skill / 能干什么，按【事实】里「圆桌岗位与技能」如实说，岗位名、工号、skill 名一个都不许编；【事实】里没有那一段，就说这个进程只有你一个，没接圆桌。"""

FALLBACK = (
    "我是 MAOS 退款助手（当前没接真模型，只能按固定话术回）。我能做的：\n"
    "  · 把退款申请表（CSV）拖进群，我逐行预检并指出填错的地方\n"
    "  · /refund <订单号> <诉求类型> 预检一单（只读，不动钱）\n"
    "  · /approve <case_id> 放行（限审批人）\n"
    "  · /team 看圆桌五岗与各自 skill\n"
    "发 /help 看全部命令")


class ChatResponder:
    """把一句闲聊变成一句回话。``model`` 不传就按环境变量选（`select_model_client`）。"""

    def __init__(self, model: ModelClient | None = None, *, tier: str = Tier.MEDIUM,
                 limit: int = REPLY_LIMIT) -> None:
        self.model = model if model is not None else select_model_client()
        self.tier = tier
        self.limit = limit

    @property
    def live(self) -> bool:
        """有没有真模型。假模型也算没有 —— 理由见模块抬头。"""
        return not isinstance(self.model, ScriptedModelClient)

    def describe(self) -> str:
        """启动时打一行「回话用什么」。只报模型名，不报 key（铁律 6）。"""
        if not self.live:
            return "闲聊回话：固定话术（未配 MAOS_LLM_*，不调模型）"
        return f"闲聊回话：真模型 {getattr(self.model, 'model', '?')}"

    def reply(self, text: str, *, facts: str) -> str:
        if not self.live:
            return FALLBACK
        system = SYSTEM_TMPL.format(limit=self.limit)
        user = f"【事实】\n{facts}\n\n【群里的人说】\n{text.strip()}"
        try:
            out = self.model.complete(system=system, user=user, tier=self.tier).text
        except Exception as exc:                        # noqa: BLE001
            # 网关 5xx / 超时 / key 失效都走这里。异常文本已由客户端脱敏。
            log.warning("闲聊回话调模型失败（%s: %s），退回固定话术",
                        type(exc).__name__, exc)
            return FALLBACK
        out = (out or "").strip()
        return out or FALLBACK
