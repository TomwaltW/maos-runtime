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
import re

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
4. **只有【你手上的事实】里**凡是「· 第 N 行 …」这样逐条列出来的，才必须原样逐条
   搬进发言，一条一行，单号和原因都要写上 —— 主管要的就是「哪一单、为什么」，
   只报一个总数等于没说。这些清单行不计入第 3 条的字数。
   本条的作用域只有【你手上的事实】：【群里已有的发言】里的逐条清单不适用本条，
   一条都不许搬。你手上的事实没有逐条清单时，本条不适用，你就只说总结句。
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


# --------------------------------------------------------------------------
# 回退原因与三道门
# --------------------------------------------------------------------------
#: `speak()` 第三个返回值的五个取值。空串 = 没回退。
FALLBACK_NO_MODEL = "no_model"
FALLBACK_ERROR = "error"
FALLBACK_EMPTY = "empty"
FALLBACK_VALIDATION = "validation"
#: 事实卡只有总结句、按 :data:`SKIP_MODEL_WHEN_FLAT` 跳过模型。**与 `no_model` 分开记**：
#: 那一个说的是「这台机器没配模型」，这一个说的是「配了，但这一岗这一轮没东西可说」。
FALLBACK_SKIPPED = "skipped"

#: 事实卡没有逐条行时跳过模型，直接发事实卡。
#:
#: **这是止血件，不是门。** 实测（HEAD=0ad62b5，`on_sheet` 走 12 行表，8 轮）：
#: 事实卡厚的两岗（受理 7 行、规则 9 行）零编造，事实卡只有一行的三岗
#: （证据 / 风险 / 财务）18/24 轮在编 —— 编出过 7 个假金额、1 个不存在的历史
#: 订单号、24 段假证据结论。一行事实卡过模型的唯一增益是「说人话」，而那一行
#: 本来就是人话；增益近零，风险是上面这些。
#:
#: 逐单事实卡补齐之后这个分支再也命不中（那时三岗自带逐条行），**会自动失效**。
#: 两道门不随它撤 —— 门管的是「说出口的话必须在自己的事实卡里」，那是永久口径。
SKIP_MODEL_WHEN_FLAT = True

#: 「薄」的阈值。**光判「没有逐条行」不够**：单案模式的事实卡是七行「字段：值」，
#: 一条逐条行也没有，但它有实打实的内容 —— 只按逐条行判会把单案圆桌的模型发言
#: 一起关掉（实测打红 `test_roundtable_team.py` 三个用例）。加这条之后判据落在
#: 表模式那三岗的一行事实卡上，正是要止的血。
FLAT_FACTS_MAX_LINES = 2

#: 逐条行的样子。**不只认事实卡自己的 `"  · "` 前缀**：门抓的是「逐条编造」这件事，
#: 模型爱用什么符号是它的自由 —— 实测它用过无前导空格的 `·`、也用过 `1.` 编号。
#: 只认一种前缀的症状是模型换个符号就整条绕过，而两边都不报错。
LIST_LINE_RE = re.compile(r"^\s*(?:[·•\-*]|\d+[.、)])")

#: 思考过程漏进发言的词面。房间里读到「等等，我核一下」等于当众自我怀疑，
#: 而它后面跟的那个数往往正是编的（实测 run_8 财务岗：先报合计 402,318.44，
#: 再自己说「382,616.16 这个数不对，我重算」）。
THINKING_ALOUD = ("等等", "我核一下", "我重算", "让我再算", "算错了")


def has_list_lines(text: str) -> bool:
    """这段文本里有没有逐条行。"""
    return any(LIST_LINE_RE.match(line) for line in (text or "").splitlines())


def is_flat(facts: str) -> bool:
    """事实卡薄不薄：没有逐条行，**且**正文不超过 :data:`FLAT_FACTS_MAX_LINES` 行。"""
    body = [line for line in (facts or "").splitlines() if line.strip()]
    return not has_list_lines(facts) and len(body) <= FLAT_FACTS_MAX_LINES


def violations(speech: str, facts: str) -> list[str]:
    """三道门。返回违规项的人话说明，空列表 = 全过。

    门 1 数字：``ids(speech) ⊆ ids(facts)`` 且 ``nums(speech) ⊆ nums(facts)``
    门 2 结构：``facts`` 没有逐条行时，``speech`` 也不许有
    门 3 格式：思考过程不许进发言

    **只跟自己的事实卡比。** 上游发言一个字都不参与 —— 理由见 `speak()` 的 docstring。
    """
    from maos.roundtable.stages import numbers_in

    said_ids, said_nums = numbers_in(speech)
    have_ids, have_nums = numbers_in(facts)

    bad: list[str] = []
    extra_ids = sorted(said_ids - have_ids)
    if extra_ids:
        bad.append(f"发言里的这些单号/规则号不在你的事实卡里：{'、'.join(extra_ids)}")
    extra_nums = sorted(said_nums - have_nums)
    if extra_nums:
        bad.append(f"发言里的这些数字不在你的事实卡里："
                   f"{'、'.join(str(n) for n in extra_nums)}")
    if has_list_lines(speech) and not has_list_lines(facts):
        bad.append("你的事实卡里没有逐条结果，发言里却逐条列了")
    spoken = [w for w in THINKING_ALOUD if w in (speech or "")]
    if spoken:
        bad.append(f"发言里带了思考过程：{'、'.join(spoken)}")
    return bad


def _retry_note(bad: list[str]) -> str:
    """重试提示。逐条把违规项摆出来，并按门的种类给不同的指令。"""
    lines = ["【上一稿没通过，原因如下】"] + [f"· {b}" for b in bad]
    if any("没有逐条结果" in b for b in bad):
        lines.append("你事实卡里没有逐单结果，只说总结句，一条逐条清单都不要列。")
    if any("不在你的事实卡里" in b for b in bad):
        lines.append("重写：只用【你手上的事实】里出现过的单号与数字，"
                     "【群里已有的发言】里的一个都不许搬。")
    if any("思考过程" in b for b in bad):
        lines.append("重写：直接给结论，不要把核对、重算的过程写进发言。")
    return "\n".join(lines)


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

    def _ask(self, system: str, user: str) -> tuple[str, str]:
        """一次模型调用。返回 `(文本, 失败原因)`，两者恒有一个是空串。"""
        try:
            out = self.model.complete(system=system, user=user,
                                      tier=self.identity.model_tier).text
        except Exception as exc:                        # noqa: BLE001
            # 网关 5xx / 超时 / key 失效都走这里。异常文本已由客户端脱敏。
            log.warning("岗位 %s 调模型失败（%s: %s），退回事实卡",
                        self.identity.agent_id, type(exc).__name__, exc)
            return "", FALLBACK_ERROR
        text = (out or "").strip()
        if not text:
            log.warning("岗位 %s 的模型回了空话，退回事实卡", self.identity.agent_id)
            return "", FALLBACK_EMPTY
        return text, ""

    def speak(self, facts: str,
              history: list[tuple[str, str]]) -> tuple[str, bool, str]:
        """组织一条发言。返回 `(说出口的话, 是不是模型说的, 回退原因)`。

        回退原因是五个字面量之一（空串 = 没回退），**不是靠 `spoken_by_model=False`
        反推**：那个布尔分不清「模型没响应」与「模型响应了但违规」，而 Evidence
        Bundle 要的正是这两者的区别。

        五条退化路径，全部落到同一个结果 —— 原样发事实卡：

          · :data:`FALLBACK_NO_MODEL`  没有真模型（`ScriptedModelClient` 也算）
          · :data:`FALLBACK_SKIPPED`   事实卡没有逐条行，按 :data:`SKIP_MODEL_WHEN_FLAT` 跳过
          · :data:`FALLBACK_ERROR`     `complete()` 抛异常
          · :data:`FALLBACK_EMPTY`     模型回了一句空话（不抛异常，`.strip()` 之后是空串）
          · :data:`FALLBACK_VALIDATION` 两次都没过门

        **门只对自己的事实卡比，不含上游白名单。** 拿上游发言当白名单量过一轮：
        它专门放过「合法单号 + 编造结论」那一类，而那正是最危险的一类
        （实测 8 轮里，只含上游指针的发言段段都在编逐单结论）。
        """
        if not self.live:
            return facts, False, FALLBACK_NO_MODEL

        # 事实卡只有总结句时跳过模型 —— **止血件，不是门**。逐单事实卡补齐之后
        # 三岗的事实卡自带逐条行，这个分支再也命不中，会自动失效；两道门不撤。
        if SKIP_MODEL_WHEN_FLAT and is_flat(facts):
            log.info("岗位 %s 的事实卡只有总结句，跳过模型直接发事实卡",
                     self.identity.agent_id)
            return facts, False, FALLBACK_SKIPPED

        system = SYSTEM_TMPL.format(title=self.title, agent_id=self.identity.agent_id,
                                    duty=self.identity.duty, room=self.room,
                                    limit=SPEECH_LIMIT)
        if history:
            said = "\n".join(f"{who}：{what}" for who, what in history)
        else:
            said = "（你是第一个发言的）"
        user = f"【你手上的事实】\n{facts}\n\n【群里已有的发言】\n{said}"

        text, reason = self._ask(system, user)
        if reason:
            return facts, False, reason

        bad = violations(text, facts)
        if not bad:
            return text, True, ""

        # 重试一次，把违规项**逐条列给它**。只说「你违规了」拿不到修正 ——
        # 模型看不见自己越了哪条界，重试出来的往往是同一段话换个说法。
        log.warning("岗位 %s 的发言没过门（%s），重试一次",
                    self.identity.agent_id, "；".join(bad))
        text2, reason2 = self._ask(system, f"{user}\n\n{_retry_note(bad)}")
        if reason2:
            return facts, False, reason2
        bad2 = violations(text2, facts)
        if not bad2:
            return text2, True, ""

        log.warning("岗位 %s 重试后仍没过门（%s），退回事实卡",
                    self.identity.agent_id, "；".join(bad2))
        return facts, False, FALLBACK_VALIDATION
