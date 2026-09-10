"""一个岗位的嘴：把规则代码算出来的事实卡说成人话。

与 AP 圆桌那份一次性脚本里的 `Speaker` 同形（同一套规矩、同一份 user 文本、
同一个「借 identity 而不是另编角色设定」的取舍），但有两处**刻意的不同**：

1. **没有真模型时不 EXIT、不抛、不发 `{}`**，退回事实卡本身。AP 那份是命令行入口，
   跑之前就该把模型配好，配不上直接给一个退出码是对的；这里是常驻房间的旁路观察，
   房间里的人问了一句就该有一句回音 —— 沉默和一行 `{}` 在群里是同一种事故。
   `ScriptedModelClient` 视作没有模型：它命中不了脚本就回字面量 `{}`。
2. `speak()` 回 `(说出口的话, 是不是模型说的)` 两个值，而不是一个字符串。
   「这句话是模型复述的还是原样的事实卡」是房间里唯一能自证 R1 的信息，
   丢掉它，事实卡与模型幻觉在下游就长得一模一样了。

模型调用直接走 `model.complete(...)`，**不经 `BaseAgent.ask`**：`ask` 要 plan/task
归属才落得下 `model_usage`，而圆桌发言不属于任何 Plan 里的任务。代价是圆桌这几次
调用没有成本行 —— 与 AP 那份、与 `maos/ingress/chat.py` 同一个取舍，记在 BACKLOG。
"""

from __future__ import annotations

import logging

from maos.model.client import ModelClient, ScriptedModelClient

log = logging.getLogger("maos.roundtable")

#: 单条发言字数上限。给模型的软约束，不硬截 —— 硬截会把话切在半句上，
#: 而房间里一句没说完的话比一句啰嗦的话更难读。
#: 发言里**结论那一段**的字数上限。逐条清单不受它约束（见 `SYSTEM_TMPL` 第 4 条）——
#: 把两者混在一个上限里的症状：模型为了压字数，把「哪四单、为什么」压成「4 单驳回」，
#: 而那正是房间里唯一有人追问的东西（2026-09-10 的现场）。
SPEECH_LIMIT = 120

#: 缺省的房间称呼。发言里要提到「这是在哪儿说话」，而模块本身不认识 Matrix。
DEFAULT_ROOM = "退款审批群"

SYSTEM_TMPL = """你是企业退款处置流程里的「{title}」（工号 {agent_id}）。
你的职责：{duty}

现在你在公司的{room}里向同事和主管汇报。规矩：
1. 只能依据【你手上的事实】说话。一个数字都不许改、不许补、不许四舍五入；
   事实卡里没写的话，一句都不许自己加（包括"余下的在回帖里"这类）。
2. 说人话，像同事在群里发言。不要 JSON、不要"综上所述"这类书面套话。
3. 先用一段话把结论说完，不超过 {limit} 字。
4. 【你手上的事实】里凡是「· 第 N 行 …」这样逐条列出来的，必须原样逐条搬进发言，
   一条一行，单号和原因都要写上 —— 主管要的就是「哪一单、为什么」，只报一个总数等于没说。
   这些清单行不计入第 3 条的字数。
5. 反过来：**你自己的事实卡里没有的行，一条都不许列**。两种情形都算：
   别人已经逐条说过的清单不要转抄；你事实卡里用顿号连成一行的单号，
   就照那一行说，不要拆成一条一行。同一份清单在房间里出现四遍，
   等于把真正该看的东西埋掉。
6. 群里已经有人发言时，先接住他的话（认可、补充或质疑）再说自己的，不要各说各的。
7. 事实卡里标了『预演 / 观察 / 受理』的字样必须原样保留，不许说成『已退款』『已到账』。

<字段归属表>
封顶标记（哪几单触发）   → 申请受理岗    只标记，不解释算法、不给金额
封顶后应退金额           → 财务执行岗    唯一产出，逐单预演后给
驳回判定 + 依据条款      → 规则审核岗    唯一产出
证据结论                 → 证据核验岗
风险结论                 → 风险反欺诈岗
整表合计                 → 财务执行岗    所有单预演完才允许发
</字段归属表>

群内发言规则：

1. 归属静态化。见 <字段归属表>。运行时不得再讨论归属 ——
   不属于你产出的字段，直接不提，不要说"这个得看 XX 岗"。

2. 上游结论默认继承。上游已在群里发过的清单/裁定，下游一律不复述、
   不确认、不加"为什么 XX 见 XX 岗"的指路。只输出你自己新产生的信息。

3. 禁止元话语。不得出现"我这边不重复""这个不归我""照常往下走"
   这类关于协作本身的句子。要么给结论，要么不发言。

4. 禁止空状态帖。skill 装载、开始执行、稍后再报 —— 不发群。
   没有结论就不发言，进度由 orchestrator 统一发一条。

5. 口径对齐。你的分母必须来自上游已发布的数字，不得自己重算。
   发言前自检：我说的每个计数，能否在上游某条消息里找到出处？"""


class Speaker:
    """一个会说话的 Agent 身份。

    刻意**不继承 BaseAgent**：`BaseAgent.ask()` 要 skills/store 才能落成本行，而
    圆桌上说话的这五位并不执行任务 —— 任务由同名 Agent 在 `run_payload` 的 DAG 里跑。
    这里借的是它们的 `identity`（同一份 duty、同一个 agent_id），不是它们的执行权。
    借 identity 而不是自己另编一套角色设定，是为了让房间里说话的人和跑流程的人
    确确实实是同一个：改了 `intake_agent.py` 的 duty，房间里的自我介绍跟着变。
    """

    def __init__(self, identity, model: ModelClient | None,   # noqa: ANN001
                 title: str | None = None, *, room: str = DEFAULT_ROOM) -> None:
        self.identity = identity
        self.model = model
        self.title = title or getattr(identity, "role", "")
        self.room = room

    @property
    def live(self) -> bool:
        """有没有真模型。假模型也算没有 —— 理由见模块抬头第 1 条。"""
        return self.model is not None and not isinstance(self.model, ScriptedModelClient)

    def speak(self, facts: str, history: list[tuple[str, str]]) -> tuple[str, bool]:
        """组织一条发言。返回 `(说出口的话, 是不是模型说的)`。

        三种退化都落到同一个结果 —— 原样发事实卡：没有真模型、`complete()` 抛异常、
        模型回了一句空话。**空回答单独判**：它不抛异常，`.strip()` 之后是空串，
        照发就是在房间里发一条空消息，比不发更难查。
        """
        if not self.live:
            return facts, False

        system = SYSTEM_TMPL.format(title=self.title, agent_id=self.identity.agent_id,
                                    duty=self.identity.duty, room=self.room,
                                    limit=SPEECH_LIMIT)
        if history:
            said = "\n".join(f"{who}：{what}" for who, what in history)
        else:
            said = "（你是第一个发言的）"
        user = f"【你手上的事实】\n{facts}\n\n【群里已有的发言】\n{said}"

        try:
            out = self.model.complete(system=system, user=user,
                                      tier=self.identity.model_tier).text
        except Exception as exc:                        # noqa: BLE001
            # 网关 5xx / 超时 / key 失效都走这里。异常文本已由客户端脱敏。
            log.warning("岗位 %s 调模型失败（%s: %s），退回事实卡",
                        self.identity.agent_id, type(exc).__name__, exc)
            return facts, False

        text = (out or "").strip()
        if not text:
            log.warning("岗位 %s 的模型回了空话，退回事实卡", self.identity.agent_id)
            return facts, False
        return text, True
