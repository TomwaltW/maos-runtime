"""退款圆桌的五岗名册与三个钩子。**平台无关** —— 本包一个 Matrix 依赖都没有。

发声面是一个 `VoiceSet` Protocol（`voice(agent_id).say(text)`），谁实现由调用方决定：
真房间里是五个 Matrix 账号，测试里是一个记流水的假件，`--dry-run` 里可以是 stdout。
把「说什么」和「往哪儿说」分开，这个模块才跑得进 `maos/tests` 而不需要 Synapse 在跑。

三个钩子（`on_preflight` / `on_sheet` / `on_execute`）是**旁路观察**：它们不改任何
处置结论，也永远不向外抛 —— 圆桌哑掉可以，把 router 的回帖带崩不行。所以每一岗的
事实汇总、每一次发声、每一个钩子自身，都各有一层 try/except。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from maos.agents.base import AgentIdentity
from maos.model.client import Tier
from maos.roundtable import stages
from maos.roundtable.speaker import (FALLBACK_NO_MODEL, FALLBACK_SKIPPED, SPEECH_LIMIT,
                                     SYSTEM_TMPL, Speaker)

log = logging.getLogger("maos.roundtable")

# --------------------------------------------------------------------------
# 落库归属：一个带前缀的伪 plan_id
# --------------------------------------------------------------------------
#: 圆桌落库那些行的 `plan_id` 前缀（跨轨契约 §B）。
#:
#: `event_log.plan_id` 与 `model_usage.plan_id` 都是必填列，而圆桌**不属于任何
#: Plan** —— 它是旁路观察者，不建 plan、不建 task。三条路只能选一条：
#:
#:   1. 给圆桌造一条 plan 行 —— `scripts/verify.py` 的树按 plan 表认，DAG 的
#:      证据束里会凭空多出几棵不是任务的树，且 `check_trace_tree` 的重放会把它
#:      们算成正经 Run。**这条被派单 §8 明令禁掉。**
#:   2. 落空串 plan_id —— 与「建 Plan 之前的调用」（`stray_events` 里那一类）
#:      混成一堆，再也分不出哪条是圆桌的。
#:   3. 带前缀的伪 plan_id —— `plan` 表里查不到它，所以这些行照旧落进
#:      `stray_events` / `unattributed_usage`（如实：它们确实不在任何一棵树里），
#:      但 `plan_id LIKE 'roundtable:%'` 一句就能把圆桌那一摊单独捞出来。
#:
#: 选 3。前缀是契约，后半段见 :func:`plan_id_of` 与 :func:`sheet_plan_id`。
PLAN_PREFIX = "roundtable:"

#: 房间里 @某一岗提问（:meth:`RefundRoundtable.answer`）的 plan_id。
#: 那条路没有 case —— 有人问「你是干什么的」时并没有在过任何一单，
#: 硬塞一个 case_id 进去就是编。用量行照旧挂得进 `roundtable:%` 这一摊。
ANSWER_PLAN_ID = f"{PLAN_PREFIX}answer"

#: 事实卡 / 发言摘要取 sha256 的前多少位。**原文一个字都不进 event_log** ——
#: 事实卡里有订单号、金额、客户历史，事件表不是它们该待的地方；摘要够回答
#: 「这一轮说的还是不是同一段话」，那正是回放要证的事。
DIGEST_LEN = 16

#: 整表模式 plan_id 里那截 rows 摘要的长度。比 :data:`DIGEST_LEN` 短是因为它进的是
#: 主键一样的 plan_id 串，12 位足够区分同一天的几张表，再长只是让人读不动。
SHEET_DIGEST_LEN = 12


def _sha(text: str, length: int = DIGEST_LEN) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:length]


def plan_id_of(case_id: str) -> str:
    """单案模式的伪 plan_id。`case_id` 为空也照给一个 —— 落不下库比落一条
    没有 case_id 的行更糟：前者整摊事件都不见了，后者至少还能按前缀捞出来。"""
    return f"{PLAN_PREFIX}{case_id or ''}"


def sheet_plan_id(rows) -> str:                          # noqa: ANN001
    """整表模式的伪 plan_id：`roundtable:sheet:<rows 摘要>`。

    读表那一轮**没有单一 case_id**（一张表十几单），拿其中任何一单的 id 当归属
    都是假的。摘要取整份 rows 而不是行数 —— 行数一样但内容不同的两张表会共用
    一个 plan_id，回放时两轮事件混成一摊，而两边都不报错。

    rows 里有算不动 JSON 的东西（真出现过：payload 里塞过 Decimal）就退回 `repr`，
    口径与 `maos/skills/invoker.py::_digest` 同 —— 这里要的是「同一份 rows 得到
    同一个串」，不是一个跨进程稳定的哈希。
    """
    try:
        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):                     # noqa: BLE001
        raw = repr(rows)
    return f"{PLAN_PREFIX}sheet:{_sha(raw, SHEET_DIGEST_LEN)}"


def _tenant_of(payload, checked) -> str:                 # noqa: ANN001
    """这一轮的租户。**只读、不抛、不猜** —— 取不到就空串。

    `router.preflight()` 的返回里没有 tenant_id（去看它的 return），所以权威在
    `payload["case"]["tenant_id"]`；`checked` 那一路留着是给以后可能补上的调用方。
    这里不走 `fixtures.case_seed_of`：那个函数缺 `case` 块会抛 KeyError，而落库
    归属算不出来不该让整轮圆桌哑掉。
    """
    case = (payload or {}).get("case") if isinstance(payload, dict) else None
    if isinstance(case, dict) and case.get("tenant_id"):
        return str(case["tenant_id"])
    if isinstance(checked, dict) and checked.get("tenant_id"):
        return str(checked["tenant_id"])
    return ""


def _sheet_tenant(rows) -> str:                          # noqa: ANN001
    """一张表的租户：第一行报得出来的那个。

    整表**恒为同一个租户**（申请表是一个租户交上来的），所以取第一行不是抽样。
    一行都读不出来就空串 —— 在这里编一个默认租户，落库那行会指向一个不存在的
    租户，而查的人以为它是真的。
    """
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tenant = _tenant_of(row.get("payload"), row.get("checked"))
        if tenant:
            return tenant
    return ""


@dataclass(frozen=True)
class _RoundCtx:
    """一轮圆桌的落库归属。**只影响写不写库、写到哪**，一个字的发言都不改。"""

    plan_id: str
    entry: str                  # preflight | sheet | execute（契约 §B）
    round_no: int
    tenant_id: str
    case_id: str
    #: 整表模式那截 rows 摘要（`sheet_plan_id` 里的同一个值）。单案恒为空串。
    sheet_digest: str = ""

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
class Action:
    """发言后面挂的一个**动作按钮**（平台无关）：一个标签、一个能点开的地址。

    圆桌只负责说「这一单缺照片、去这里传」；按钮长什么样归发声面 —— Matrix 那侧
    把它渲染成带底色的链接 chip（发声面那一层的事），冒烟脚本
    与测试里的假嘴把它打成一行文字（:func:`actions_text`）。
    """

    label: str
    url: str

    def as_text(self) -> str:
        return f"📎 {self.label}：{self.url}"


def actions_text(actions) -> str:                        # noqa: ANN001
    """一批按钮的纯文本形态，一行一个。给只有 `say(text)` 的老嘴退化用。"""
    return "\n".join(a.as_text() for a in (actions or ()))


#: 一条发言后面最多挂几个按钮。与 `stages.ROW_CAP` 同数：清单只列这么多行，
#: 按钮比清单多，房间里就会出现「没提到的单却有按钮」。
MAX_ACTIONS = stages.ROW_CAP


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
    #: 回退原因（`speaker.FALLBACK_*` 之一），空串 = 没回退。**带默认值是刻意的**：
    #: 加它的时候 `StageReport(...)` 在三个测试文件里已有构造点，给默认值就一处不用改。
    #: 与 `spoken_by_model` 并存而不是替掉它：那个布尔答的是「这话谁说的」，
    #: 本字段答的是「为什么不是模型说的」—— Evidence Bundle 要分清「没响应」与「违规」。
    fallback_reason: str = ""
    #: 这一岗发言后面挂的按钮（`Action`）。带默认值的理由同上一个字段。
    #: 空元组 = 没挂：没配上传地址、或这一岗这一轮没说「缺材料」。
    actions: tuple = ()


class RefundRoundtable:
    """五岗圆桌。实现 `TeamObserver`：三个钩子 + 一份名册。"""

    def __init__(self, model, voices, *, ledger_loader=None,        # noqa: ANN001
                 pace=None, upload_link: Callable[[dict], str] | None = None,
                 store=None) -> None:
        self.model = model
        self.voices = voices
        self._ledger_loader = ledger_loader
        #: 事件与用量的去处。**缺省 `None` = 一个字节都不落**，圆桌的行为与
        #: 屏幕输出逐字节不变（`test_room_team_recheck.PLAIN_STDOUT_MD5` 钉着
        #: 冒烟的 stdout）。接上之后每一轮多三类事件行 + 两个 skill 的
        #: `SkillInvoked` + 真模型的 `model_usage`，见模块内 :data:`PLAN_PREFIX`。
        #:
        #: 🔴 **必须带默认值**：`router.py::_fire`、房间入口那一侧的 `_build_team`、
        #: `scripts/room_team_smoke.py` 三处都按签名探参，少一个默认值就是 `TypeError`
        #: 落进 `_fire` 那条只记 WARNING 的 except —— 整个圆桌静默哑掉、回帖照发，
        #: 没有任何测试会红（同 `on_preflight` 那两个参的红字警告）。
        self._store = store
        #: `plan_id -> tenant_id`。合议收口（:meth:`verdict_of`）另取一次、入参只有
        #: `checked`，而 `checked` 里**没有 tenant_id**（`router.preflight()` 的返回
        #: 形状），于是这一轮的租户只能由发言那一轮记下来。按 plan_id 存而不是存
        #: 一个「上一次的租户」：两个线程同时过两单时（`IngressServer` 的工作线程
        #: 与 Matrix 回调线程都会调 `handle`），后者会把前者的租户覆盖掉。
        self._tenant_of_plan: dict[str, str] = {}
        #: 「上传材料」按钮的地址生成器 `(gap: dict) -> str`，缺省 `None` = 不挂按钮。
        #: 圆桌不认识 URL 长什么样 —— 补件页由房间入口起，地址由它给；这里只在
        #: 事实卡说「缺材料」的时候按每一单问一次。回空串或抛异常都当没有按钮。
        self._upload_link = upload_link
        #: 发言节奏回调 `(i, total) -> None`，每岗**发言进房间之后**调一次。
        #: 缺省 `None` = 一次都不调：测试与冒烟脚本零等待，一秒都不许变慢。
        #: 本包里不许 import `time`、不许自己 sleep —— 停多久由注入方定，
        #: 平台无关层只负责给一个可以插进去的点。
        self._pace = pace
        self._speakers: dict[str, Speaker] = {
            agent_id: Speaker(identity_of(ROLE_OF[agent_id]), model, TITLES[agent_id],
                              store=store, agent_role=ROLE_OF[agent_id])
            for agent_id in TEAM_ORDER
        }

    # -- 落库 ---------------------------------------------------------------
    def attach_store(self, store) -> None:              # noqa: ANN001
        """事后接上 store。给「先建圆桌、后建 store」的调用方用。

        真房间那侧的 `_build_team` 在 `wire()` **之前**跑（圆桌要先建出来才传得给
        router），而 store 是 `wire()` 里建的 —— 没有这个方法，房间那条路就只能靠
        改建圆桌的时机来接线，而那一段是 7af9022 刚落的面。

        五个 `Speaker` 一起换：漏掉它们的症状是事件行落得下、`model_usage`
        一行没有，而两边都不报错。
        """
        self._store = store
        for speaker in self._speakers.values():
            speaker.store = store

    def _emit(self, event_type: str, ctx: _RoundCtx, detail: dict) -> None:
        """写一条圆桌事件。**`store=None` 直接返回**，落库失败只记 WARNING。

        圆桌是旁路观察者：它的记账挂了不该让房间里的发言跟着不见（红线 R4，
        口径同 `core/store.py::record_model_usage` 的落库失败分支）。但必须留声 ——
        静默吞掉等于事件链凭空缺几条，而屏幕上看不出来。

        三类事件都是 `event_log` 的**字符串** `event_type`，不加 Topic、不碰
        `maos/contracts/events.py`（铁律 1），写法沿 `maos/domain/refund/guard.py`
        的 `RefundBizStatusChanged`。
        """
        if self._store is None:
            return
        try:
            self._store.append_event_log({
                "plan_id": ctx.plan_id,
                "event_type": event_type,
                "detail": {"tenant_id": ctx.tenant_id, "case_id": ctx.case_id, **detail},
            })
        except Exception as exc:                        # noqa: BLE001
            log.warning("圆桌事件 %s 落库失败（%s: %s），发言不受影响",
                        event_type, type(exc).__name__, exc)

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

    def _actions_of(self, data: dict) -> tuple[Action, ...]:
        """这一岗这一轮要挂的按钮：事实卡说了「缺材料」的每一单一个。

        只认 `data["material_gaps"]`（`stages._material_gaps` 的形状）：按钮是
        结构化数据长出来的，不是从文案里刮的。没配 `upload_link` 恒为空。
        """
        if self._upload_link is None or not isinstance(data, dict):
            return ()
        out: list[Action] = []
        for gap in list(data.get("material_gaps") or [])[:MAX_ACTIONS]:
            if not isinstance(gap, dict) or not gap.get("order_id"):
                continue
            try:
                url = str(self._upload_link(gap) or "")
            except Exception as exc:                    # noqa: BLE001
                log.warning("生成上传链接失败（%s: %s），本单不挂按钮",
                            type(exc).__name__, exc)
                continue
            if not url:
                continue
            what = stages.kinds_cn(gap.get("kinds")) or "材料"
            out.append(Action(label=f"上传{what} · {gap['order_id']}", url=url))
        return tuple(out)

    def _say(self, agent_id: str, facts: str, data: dict,
             history: list[tuple[str, str]], ctx: _RoundCtx | None = None) -> StageReport:
        """一位发言：组织语言 -> 进房间 -> 进上下文 -> 落一条 `RoundtableSeatSpoke`。

        顺序不可换 —— 没进房间的话不该出现在下一位的上下文里，否则房间里读到的
        是残缺的对话。进房间失败只记日志：房间是旁路，不是主路。

        按钮**不进 `speech`、不进 history**：它不是这一岗说的话，是发声面挂在话
        后面的东西。进了 history，下一岗的模型就会看到一串 URL 并试图复述它。

        事件落在最后：`spoken_by_model` / `fallback_reason` 要等 `speak()` 回来才知道，
        而那两个字段正是这条事件存在的理由 —— 评委问「这五岗到底是模型说的还是
        事实卡」，答案只有这里有。
        """
        speaker = self._speakers[agent_id]
        plan_id = ctx.plan_id if ctx is not None else ""
        speech, by_model, fallback = speaker.speak(facts, history, plan_id=plan_id)
        actions = self._actions_of(data)
        try:
            voice = self.voices.voice(agent_id)
            if not actions:
                voice.say(speech)
            elif hasattr(voice, "say_with_actions"):
                voice.say_with_actions(speech, actions)
            else:
                # 只有 `say(text)` 的老嘴：按钮退化成文字行。加第二个方法而不是改
                # `say` 的签名（跨轨契约 §1.3 只有那一个方法），是为了让现有的每一张嘴
                # —— 冒烟脚本、测试假件、代言兜底 —— 不改一行也照样能跑。
                voice.say(f"{speech}\n{actions_text(actions)}")
        except Exception as exc:                        # noqa: BLE001
            log.warning("岗位 %s 发言没进房间（%s: %s），后面几岗照常",
                        agent_id, type(exc).__name__, exc)
        history.append((speaker.title, speech))
        if ctx is not None and self._store is not None:
            # 模型真被调过的两条路之外（没模型 / 事实卡太薄被跳过）一次调用都没有，
            # 也就没有 `model_usage` 行可以回查 —— 那时 `model_call_id` 留空串，
            # 而不是给一个指不到任何用量行的假 id。
            called = fallback not in (FALLBACK_NO_MODEL, FALLBACK_SKIPPED)
            self._emit("RoundtableSeatSpoke", ctx, {
                "seat": agent_id,
                "spoken_by_model": bool(by_model),
                "fallback_reason": fallback,
                "facts_digest": _sha(facts),
                "speech_digest": _sha(speech),
                "speech_len": len(speech or ""),
                "model_call_id": (f"{ctx.plan_id}|{speaker.agent_role}|{uuid.uuid4().hex}"
                                  if called else ""),
            })
        return StageReport(agent_id=agent_id, title=speaker.title, facts=facts,
                           speech=speech, data=data, spoken_by_model=by_model,
                           fallback_reason=fallback, actions=actions)

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

    def _round(self, builders: dict, agent_ids: tuple[str, ...],
               ctx: _RoundCtx | None = None) -> list[StageReport]:
        """按名册顺序走一圈。**某一岗算不出事实也照样发言** —— 一个岗位在房间里
        凭空消失，比它说「我这儿出错了」更难排查。

        `ctx` 只管落库归属（缺省 `None` = 不落库），发言一个字都不受它影响。
        """
        history: list[tuple[str, str]] = []
        reports: list[StageReport] = []
        total = len(agent_ids)
        if ctx is not None:
            self._tenant_of_plan[ctx.plan_id] = ctx.tenant_id
            detail = {"entry": ctx.entry, "round_no": ctx.round_no,
                      "seats": list(agent_ids)}
            if ctx.sheet_digest:
                detail["sheet_digest"] = ctx.sheet_digest
            self._emit("RoundtableRound", ctx, detail)
        for index, agent_id in enumerate(agent_ids, 1):
            try:
                facts, data = builders[agent_id]()
            except Exception as exc:                    # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                log.warning("岗位 %s 汇总事实失败（%s）", agent_id, reason)
                facts = f"{TITLES.get(agent_id, agent_id)}的事实汇总失败：{reason}，本岗这一轮没有结论"
                data = {"error": reason}
            reports.append(self._say(agent_id, facts, data, history, ctx))
            # 最后一岗说完也调：收口卡在它之后，那一停顿正是「五岗说完了，主席要发言了」。
            self._tick(index, total)
        return reports

    # -- TeamObserver -------------------------------------------------------
    def on_preflight(self, *, payload: dict, checked: dict, ledger: dict,
                     evidence: list, requested_by: str,
                     round_no: int = 1, added_evidence: int = 0) -> list[StageReport]:
        """一单预检：五岗依次发言。`requested_by` 只进日志，不进事实卡 ——
        发起人是谁不影响任何裁定，写进 facts 只会给 R1 的数字白名单添一串
        与本单无关的字符。

        `round_no`（本会话里这是第几轮）与 `added_evidence`（本轮新增几份随案证据）
        是复检那一轮的口径，只影响受理岗与证据岗多说的那一行，不影响任何裁定。

        🔴 **两个都必须留默认值。** 理由不是风格：`maos/ingress/router.py::_fire`
        与 `scripts/room_team_smoke.py` 仍按五参调用，少一个默认值就是 `TypeError`
        落进 `_fire` 的 except —— 那条 except 只记 WARNING，症状是整个圆桌静默哑掉、
        回帖照发，没有任何测试会红。

        两个数都不在这里算：`evidence` 是**合并后**的全量，减不出「本轮新增几份」
        （老 ticket 可能本来就带着几份）。谁触发复检谁数，这里只负责排进事实卡。
        """
        try:
            book = self._ledger(ledger)
            count = len(evidence or [])
            ctx = _RoundCtx(plan_id=plan_id_of(str(checked.get("case_id") or "")),
                            entry="preflight", round_no=int(round_no or 1),
                            tenant_id=_tenant_of(payload, checked),
                            case_id=str(checked.get("case_id") or ""))
            store = self._store
            builders = {
                # 两个新参靠闭包捕获进零参 builder，**不改 `_round` 的签名** ——
                # `on_sheet` / `on_execute` 也在用它，改签名要连着改三处调用。
                "refund-intake":
                    lambda: stages.facts_intake(payload, checked, count, round_no),
                "refund-policy": lambda: stages.facts_policy(checked),
                # 下游两岗多带 store / plan_id：这两个 skill 的 `SkillInvoked`
                # 就是从这里第一次落进真库的（跨轨契约 §G）。
                "refund-evidence":
                    lambda: stages.facts_evidence(payload, checked, book, added_evidence,
                                                  store=store, plan_id=ctx.plan_id),
                "refund-risk": lambda: stages.facts_risk(payload, checked, book,
                                                         store=store, plan_id=ctx.plan_id),
                "refund-finance": lambda: stages.facts_finance_preview(payload, checked),
            }
            log.info("圆桌预检 case=%s 发起人=%s 第 %s 轮（本轮新增 %s 份证据）",
                     checked.get("case_id"), requested_by, round_no, added_evidence)
            return self._round(builders, TEAM_ORDER, ctx)
        except Exception as exc:                        # noqa: BLE001
            log.warning("圆桌预检整轮失败（%s: %s），本轮不发言", type(exc).__name__, exc)
            return []

    def on_sheet(self, *, rows: list[dict], ledger: dict,
                 requested_by: str) -> list[StageReport]:
        """一张表：每岗只汇总一次。逐行五连发的代价是 50 行 × 5 岗 = 250 条，
        房间会被刷爆，而人要的只是「这批能不能过」。

        `ledger` 传给下游两岗：证据核验要订单快照里的物流 / 质检事实，风险筛查要
        同账号订单与退款历史，两者都只在底账里。上游两岗只数行，不读它。
        """
        try:
            book = self._ledger(ledger)
            plan_id = sheet_plan_id(rows)
            # 整表模式**没有单一 case_id**：`detail.case_id` 如实留空串，那一摊行靠
            # `sheet_digest` 与 plan_id 认（契约 §B 只要求这个键在，没要求它非空）。
            # 拿其中任一单的 id 填进去，回放时会读成「这十几岗都在说那一单」。
            ctx = _RoundCtx(plan_id=plan_id, entry="sheet", round_no=1,
                            tenant_id=_sheet_tenant(rows), case_id="",
                            sheet_digest=plan_id.rsplit(":", 1)[-1])
            store = self._store
            builders = {
                "refund-intake": lambda: stages.facts_sheet_intake(rows),
                "refund-policy": lambda: stages.facts_sheet_policy(rows),
                "refund-evidence": lambda: stages.facts_sheet_evidence(
                    rows, book, store=store, plan_id=plan_id),
                "refund-risk": lambda: stages.facts_sheet_risk(
                    rows, book, store=store, plan_id=plan_id),
                "refund-finance": lambda: stages.facts_sheet_finance(rows),
            }
            log.info("圆桌读表 %d 行 发起人=%s", len(rows or []), requested_by)
            return self._round(builders, TEAM_ORDER, ctx)
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
            ctx = _RoundCtx(plan_id=plan_id_of(str(result.get("case_id") or "")),
                            entry="execute", round_no=1,
                            tenant_id=_tenant_of(payload, result),
                            case_id=str(result.get("case_id") or ""))
            return self._round(
                {"refund-finance": lambda: stages.facts_finance_result(result)},
                ("refund-finance",), ctx)
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
        return self.decide(reports, case_id=str((checked or {}).get("case_id") or ""))

    def decide(self, reports: list, *, case_id: str = ""):   # noqa: ANN201
        """合议收口 + 落一条 `RoundtableVerdict`。

        **这个方法名不是随手起的**：`maos/ingress/router.py::_attach_verdict` 先探
        `getattr(self.team, "decide", None)`，探不到才去 import 模块级的那一个。
        于是圆桌只要**提供**它，真房间那条路就自动把合议事件落上了，router 一行
        都不用改（本轨在那个文件里只许加 store 透传那一句）。签名照那个调用点的
        形状来：`decide(reports, case_id=...)`。

        `verdict.decide` 只 import、不改（禁词表与合议真值表是别人的面）。
        """
        from maos.roundtable.verdict import decide as _decide

        case = str(case_id or "")
        verdict = _decide(reports, case_id=case)
        if self._store is not None:
            plan_id = plan_id_of(case)
            ctx = _RoundCtx(plan_id=plan_id, entry="preflight", round_no=1,
                            tenant_id=self._tenant_of_plan.get(plan_id, ""),
                            case_id=case)
            # `seats` 只落**座位名**，不落五岗 data 的原样副本：那里面有金额、
            # 缺口原话、客户历史，而事件表不是它们该待的地方（同 facts/speech 只落摘要）。
            # `recommend` 是**建议**不是业务终态（铁律 8）—— 这一条落库不改变任何
            # 外部系统的状态，措辞由 `verdict.py` 的禁词表管着，本轨一个字不动。
            self._emit("RoundtableVerdict", ctx, {
                "recommend": str(getattr(verdict, "recommend", "") or ""),
                "approver_role": str(getattr(verdict, "approver_role", "") or ""),
                "blockers": list(getattr(verdict, "blockers", []) or []),
                "seats": sorted(getattr(verdict, "seats", {}) or {}),
            })
        return verdict

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
            # 经 `Speaker.complete` 而不是直接 `model.complete`：那一层掐时、记账，
            # 是本包唯一的 `record_model_usage` 调用点。这里绕过它的症状是
            # 「房间里 @了五轮，成本表上一行都没有」，而两边都不报错。
            out = speaker.complete(system, "\n\n".join(parts),
                                   plan_id=ANSWER_PLAN_ID).text
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
