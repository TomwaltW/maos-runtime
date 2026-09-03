"""退款圆桌的五岗名册与三个钩子。**平台无关** —— 本包一个 Matrix 依赖都没有。

发声面是一个 `VoiceSet` Protocol（`voice(agent_id).say(text)`），谁实现由调用方决定：
真房间里是五个 Matrix 账号，测试里是一个记流水的假件，`--dry-run` 里可以是 stdout。
把「说什么」和「往哪儿说」分开，这个模块才跑得进 `maos/tests` 而不需要 Synapse 在跑。

三个钩子（`on_preflight` / `on_sheet` / `on_execute`）是**旁路观察**：它们不改任何
处置结论，也永远不向外抛 —— 圆桌哑掉可以，把 router 的回帖带崩不行。所以每一岗的
事实汇总、每一次发声、每一个钩子自身，都各有一层 try/except。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from maos.agents.base import AgentIdentity
from maos.model.client import Tier
from maos.roundtable import stages
from maos.roundtable.speaker import SPEECH_LIMIT, SYSTEM_TMPL, Speaker

log = logging.getLogger("maos.roundtable")

#: 发言顺序。名册顺序就是发言顺序 —— 受理 → 裁定 → 证据 → 风险 → 核算，
#: 后一岗接着前一岗的话说，所以这不是一个随便排的集合。
TEAM_ORDER: tuple[str, ...] = (
    "refund-intake", "refund-policy", "refund-evidence", "refund-risk", "refund-finance")

#: 房间里的显示名。`agent_id` 是给机器看的，`refund-risk` 这几个字母不足以让人
#: 一眼知道这句话该由谁负责，而房间里说话的对象是人。
TITLES: dict[str, str] = {
    "refund-intake": "申请受理岗",
    "refund-policy": "规则审核岗",
    "refund-evidence": "证据核验岗",
    "refund-risk": "风险反欺诈岗",
    "refund-finance": "财务执行岗",
}

#: agent_id -> role。`AGENT_POOL` 按 role 索引，而房间与名册按 agent_id 说话。
ROLE_OF: dict[str, str] = {
    "refund-intake": "refund_intake",
    "refund-policy": "refund_policy",
    "refund-evidence": "refund_evidence",
    "refund-risk": "refund_risk",
    "refund-finance": "refund_finance",
}

#: 两个还没有真 Agent 的岗位的最小身份。**过渡件**：证据核验岗与风险反欺诈岗的
#: Agent 各自在别的轨上新建，并进来之后 `AGENT_POOL` 里就有真的了，`identity_of`
#: 会自动改用真身份，这两条随之作废（记在 BACKLOG）。
#:
#: 没有它，整合顺序就被钉死成「那两轨必须先合」；有了它，五岗在任何合并顺序下
#: 都说得出话，只是自我介绍暂时来自这里而不是 Agent 声明。
FALLBACK_IDENTITIES: dict[str, AgentIdentity] = {
    "refund_evidence": AgentIdentity(
        agent_id="refund-evidence",
        role="refund_evidence",
        duty="核验随案证据是否齐备、与订单事实是否自洽；只对证据下结论，"
             "不判金额、不判是否退款",
        allowed_skills=frozenset({"refund.evidence_check"}),
        allowed_tools=frozenset(),          # 核验只读入参，不碰附件字节、不碰网关
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    ),
    "refund_risk": AgentIdentity(
        agent_id="refund-risk",
        role="refund_risk",
        duty="按客户历史与本单金额筛查重复退款与欺诈风险并给出风险档位；"
             "只提示风险，不改裁定、不拦付款",
        allowed_skills=frozenset({"refund.risk_screen"}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
        max_self_repair=0,
    ),
}


def identity_of(role: str) -> AgentIdentity:
    """取岗位身份：**池子里有真 Agent 就用真的**，没有才退到过渡件。

    顺序不能反。反过来（先看过渡件）的症状是：那两个 Agent 合进来之后，房间里的
    自我介绍还是这里写死的那句，而 `docs/agent-identity.md` 里是另一句 —— 两份
    都不报错，只是对不上。
    """
    from maos.agents import base

    import maos.agents.refund  # noqa: F401 —— import 即注册，触发 @register 入池

    cls = base.AGENT_POOL.get(role)
    if cls is not None:
        return cls.identity
    ident = FALLBACK_IDENTITIES.get(role)
    if ident is None:
        raise KeyError(f"没有 role={role!r} 的身份：AGENT_POOL 里没有，也没有过渡件")
    return ident


@dataclass(frozen=True)
class StageReport:
    """一岗说完之后留下的东西。`facts` 与 `speech` 都留着是刻意的 ——
    只留 `speech`，就再也证明不了模型有没有编数字（R1）。"""

    agent_id: str            # 谁说的（TEAM_ORDER 之一）
    title: str               # 岗位名
    facts: str               # 喂给模型的【事实】原文 —— 规则代码算出来的，可直接当事实卡发
    speech: str              # 说出口的话：有模型 = 模型复述；没模型 / 失败 = facts 事实卡
    data: dict               # 结构化结论（各岗键见 stages.py），给测试与 /pending 用
    spoken_by_model: bool


class RefundRoundtable:
    """五岗圆桌。实现 `TeamObserver`：三个钩子 + 一份名册。"""

    def __init__(self, model, voices, *, ledger_loader=None,        # noqa: ANN001
                 pace=None) -> None:
        self.model = model
        self.voices = voices
        self._ledger_loader = ledger_loader
        #: 发言节奏回调 `(i, total) -> None`，每岗**发言进房间之后**调一次。
        #: 缺省 `None` = 一次都不调：测试与冒烟脚本零等待，一秒都不许变慢。
        #: 本包里不许 import `time`、不许自己 sleep —— 停多久由注入方定，
        #: 平台无关层只负责给一个可以插进去的点。
        self._pace = pace
        self._speakers: dict[str, Speaker] = {
            agent_id: Speaker(identity_of(ROLE_OF[agent_id]), model, TITLES[agent_id])
            for agent_id in TEAM_ORDER
        }

    # -- 内部 ---------------------------------------------------------------
    def _ledger(self, ledger: dict | None) -> dict:
        if ledger:
            return ledger
        if self._ledger_loader is None:
            return {}
        try:
            return self._ledger_loader() or {}
        except Exception as exc:                        # noqa: BLE001
            log.warning("读底账失败（%s: %s），按空底账继续", type(exc).__name__, exc)
            return {}

    def _say(self, agent_id: str, facts: str, data: dict,
             history: list[tuple[str, str]]) -> StageReport:
        """一位发言：组织语言 -> 进房间 -> 进上下文。

        顺序不可换 —— 没进房间的话不该出现在下一位的上下文里，否则房间里读到的
        是残缺的对话。进房间失败只记日志：房间是旁路，不是主路。
        """
        speaker = self._speakers[agent_id]
        speech, by_model = speaker.speak(facts, history)
        try:
            self.voices.voice(agent_id).say(speech)
        except Exception as exc:                        # noqa: BLE001
            log.warning("岗位 %s 发言没进房间（%s: %s），后面几岗照常",
                        agent_id, type(exc).__name__, exc)
        history.append((speaker.title, speech))
        return StageReport(agent_id=agent_id, title=speaker.title, facts=facts,
                           speech=speech, data=data, spoken_by_model=by_model)

    def _tick(self, index: int, total: int) -> None:
        """走完一岗，通知注入方可以停一拍了。

        `pace` 是观感不是主路：抛了只记 WARNING，后面几岗照发。缺省 `None`
        直接返回 —— 判空放在这里而不是调用点，是为了让 `_round` 只有一条主线。
        """
        if self._pace is None:
            return
        try:
            self._pace(index, total)
        except Exception as exc:                        # noqa: BLE001
            log.warning("发言节奏回调失败（%s: %s），发言照常",
                        type(exc).__name__, exc)

    def _round(self, builders: dict, agent_ids: tuple[str, ...]) -> list[StageReport]:
        """按名册顺序走一圈。**某一岗算不出事实也照样发言** —— 一个岗位在房间里
        凭空消失，比它说「我这儿出错了」更难排查。"""
        history: list[tuple[str, str]] = []
        reports: list[StageReport] = []
        total = len(agent_ids)
        for index, agent_id in enumerate(agent_ids, 1):
            try:
                facts, data = builders[agent_id]()
            except Exception as exc:                    # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                log.warning("岗位 %s 汇总事实失败（%s）", agent_id, reason)
                facts = f"{TITLES.get(agent_id, agent_id)}的事实汇总失败：{reason}，本岗这一轮没有结论"
                data = {"error": reason}
            reports.append(self._say(agent_id, facts, data, history))
            # 最后一岗说完也调：收口卡在它之后，那一停顿正是「五岗说完了，主席要发言了」。
            self._tick(index, total)
        return reports

    # -- TeamObserver -------------------------------------------------------
    def on_preflight(self, *, payload: dict, checked: dict, ledger: dict,
                     evidence: list, requested_by: str) -> list[StageReport]:
        """一单预检：五岗依次发言。`requested_by` 只进日志，不进事实卡 ——
        发起人是谁不影响任何裁定，写进 facts 只会给 R1 的数字白名单添一串
        与本单无关的字符。"""
        try:
            book = self._ledger(ledger)
            count = len(evidence or [])
            builders = {
                "refund-intake": lambda: stages.facts_intake(payload, checked, count),
                "refund-policy": lambda: stages.facts_policy(checked),
                "refund-evidence": lambda: stages.facts_evidence(payload, checked, book),
                "refund-risk": lambda: stages.facts_risk(payload, checked, book),
                "refund-finance": lambda: stages.facts_finance_preview(payload, checked),
            }
            log.info("圆桌预检 case=%s 发起人=%s", checked.get("case_id"), requested_by)
            return self._round(builders, TEAM_ORDER)
        except Exception as exc:                        # noqa: BLE001
            log.warning("圆桌预检整轮失败（%s: %s），本轮不发言", type(exc).__name__, exc)
            return []

    def on_sheet(self, *, rows: list[dict], ledger: dict,
                 requested_by: str) -> list[StageReport]:
        """一张表：每岗只汇总一次。逐行五连发的代价是 50 行 × 5 岗 = 250 条，
        房间会被刷爆，而人要的只是「这批能不能过」。

        `ledger` 收下但不读：每行的 `checked` 已经是拿底账算过的了，汇总只数行。
        参数留着是因为签名由跨轨契约定，且逐行深挖迟早要用到它。
        """
        try:
            builders = {
                "refund-intake": lambda: stages.facts_sheet_intake(rows),
                "refund-policy": lambda: stages.facts_sheet_policy(rows),
                "refund-evidence": lambda: stages.facts_sheet_evidence(rows),
                "refund-risk": lambda: stages.facts_sheet_risk(rows),
                "refund-finance": lambda: stages.facts_sheet_finance(rows),
            }
            log.info("圆桌读表 %d 行 发起人=%s", len(rows or []), requested_by)
            return self._round(builders, TEAM_ORDER)
        except Exception as exc:                        # noqa: BLE001
            log.warning("圆桌读表整轮失败（%s: %s），本轮不发言", type(exc).__name__, exc)
            return []

    def on_execute(self, *, payload: dict, result: dict,
                   operator: str) -> list[StageReport]:
        """放行之后：**只有财务执行岗**发言。

        受理、裁定、证据、风险这四岗在放行前已经把话说完了，放行后再各说一遍，
        房间里读到的是四条与预检重复的话。真正变化的只有核算与付款观察。
        """
        try:
            log.info("圆桌回执 case=%s 放行人=%s", result.get("case_id"), operator)
            return self._round(
                {"refund-finance": lambda: stages.facts_finance_result(result)},
                ("refund-finance",))
        except Exception as exc:                        # noqa: BLE001
            log.warning("圆桌回执整轮失败（%s: %s），本轮不发言", type(exc).__name__, exc)
            return []

    # -- 合议收口 -----------------------------------------------------------
    def verdict_of(self, reports: list[StageReport], checked: dict | None = None):
        """五岗结论 -> 一张给 boss 的批复建议卡。

        **另取一次，不塞进 reports**：`on_preflight` 的返回形状写在跨轨契约里、
        有三个消费方，往里加第六个元素会让所有按 `TEAM_ORDER` 遍历的地方多出
        一个不存在的岗位。

        惰性 import 是为了避开环：`verdict` 读本模块的 `TEAM_ORDER` / `TITLES`，
        本模块只在这一个方法里用到它。
        """
        from maos.roundtable.verdict import decide

        return decide(reports, case_id=str((checked or {}).get("case_id") or ""))

    # -- 点名问答 -----------------------------------------------------------
    def answer(self, agent_id: str, question: str, *, facts: str = "") -> str:
        """房间里 @某一岗提问，由**那一岗自己**回，而不是主通道用助手的口气回一段。

        `agent_id` 不在名册里直接 `KeyError` —— 怎么跟提问的人说，由调用方决定：
        在这里编一句「查无此人」，router 就没法把它和真的回答区分开。

        没模型 / 调用失败 / 空回答一律退回该岗位的 duty，**不返回空串**：
        房间里的空消息比不回更难查（同 `Speaker.speak` 的取舍）。
        """
        speaker = self._speakers[agent_id]
        duty_line = self._duty_line(speaker)
        if not speaker.live:
            return duty_line

        system = SYSTEM_TMPL.format(
            title=speaker.title, agent_id=speaker.identity.agent_id,
            duty=speaker.identity.duty, room=speaker.room, limit=SPEECH_LIMIT)
        asked = (question or "").strip() or "你是干什么的"
        parts = [f"【有人在群里问你】\n{asked}"]
        if (facts or "").strip():
            parts.append(f"【你手上的事实】\n{facts.strip()}")

        try:
            out = speaker.model.complete(system=system, user="\n\n".join(parts),
                                         tier=speaker.identity.model_tier).text
        except Exception as exc:                        # noqa: BLE001
            log.warning("岗位 %s 答问调模型失败（%s: %s），退回岗位职责",
                        agent_id, type(exc).__name__, exc)
            return duty_line

        text = (out or "").strip()
        if not text:
            log.warning("岗位 %s 答问时模型回了空话，退回岗位职责", agent_id)
            return duty_line
        return text

    @staticmethod
    def _duty_line(speaker: Speaker) -> str:
        """零模型的自我介绍：职责 + 手上装着什么。skill 名来自 identity，不是编的。"""
        skills = sorted(speaker.identity.allowed_skills)
        tail = (f"手上装着 {'、'.join(skills)}，这几件事问我。" if skills
                else "本岗暂时没有装载可用的 skill。")
        return f"我是{speaker.title}，{speaker.identity.duty}。{tail}"

    def roster(self) -> list[dict]:
        """五岗名册。skill 三元组来自注册表里的 `SkillContract`，不是模型编的 ——
        「你有什么 skill」这个问题必须有一个零模型的答案。

        未注册的 skill 也列出来、写「未装载」：漏掉它，房间里看到的是一个
        「什么都不会」的岗位，而事实是那个 skill 还没并进来。
        """
        from maos.skills import registry

        out: list[dict] = []
        for agent_id in TEAM_ORDER:
            speaker = self._speakers[agent_id]
            identity = speaker.identity
            user_id, own_identity = "", False
            try:
                voice = self.voices.voice(agent_id)
                user_id = str(getattr(voice, "user_id", "") or "")
                own_identity = bool(getattr(voice, "own_identity", False))
            except Exception as exc:                    # noqa: BLE001
                log.warning("岗位 %s 取不到发声面（%s: %s），名册里按未接通列",
                            agent_id, type(exc).__name__, exc)

            skills = []
            for name in sorted(identity.allowed_skills):
                contract = getattr(registry.get(name), "contract", None)
                skills.append({
                    "name": name,
                    "version": getattr(contract, "version", "") if contract else "",
                    "purpose": getattr(contract, "purpose", "") if contract else "未装载",
                })
            out.append({
                "agent_id": agent_id, "title": speaker.title, "role": identity.role,
                "duty": identity.duty, "user_id": user_id, "own_identity": own_identity,
                "skills": skills,
            })
        return out
