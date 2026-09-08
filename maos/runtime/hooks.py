"""生命周期 hook —— 全仓第一个**能否决**的挂点。

## 这个模块存在的理由

MAOS 今天有四处回调，**没有一处能否决**：

| 回调 | 位置 | 为什么不算 hook |
| :-- | :-- | :-- |
| ``before_review`` | ``maos/flows/common.py`` | 只能补产物，返回值被丢弃 |
| ``decision_hook`` | ``maos/flows/scenario_7.py`` | 人类决策的注入口，不是生命周期挂点 |
| 配置订阅 | ``maos/config/source.py`` | 回调抛异常一律吞掉，无返回值语义 |
| 圆桌钩子 | ``maos/ingress/router.py`` | **回帖发出之后**才 fire，异常吞成 warning —— 它是观察者，按设计就否决不了任何东西 |

全仓唯一「否决 + 带反馈回灌」的形态是 Gate 的 rework findings，但那是**产物评审**，
不是生命周期挂点：它只在 AWAITING_REVIEW 那一刻对着 artifact 说话，管不到
「这个任务该不该被创建」。本模块补的就是后半句。

## 两条失败姿态，方向刻意相反

1. **回调抛异常 = 不否决 + 留痕。** 吞掉异常（协调信号 fail-open，口径同
   ``docs/failure-posture.md`` 判据一：一个 hook 挂掉，最坏后果是「少一道治理」，
   不是「结果算错」——主流程不该被一个第三方回调卡死）。但**必须落一条
   ``HookFailed``**。

   🔴 这一条与 ``maos/ingress/router.py`` 圆桌钩子的「吞成 warning」**刻意不同**：
   那里只打日志，于是钩子挂了没有任何人知道。**一个静默失效的挂点比没有挂点更糟**
   —— 没有挂点时谁都不指望它，静默失效时所有人都以为治理还在。同一个病在
   ``CLAUDE.md`` 里被写成守卫 hook 的开工自检：hook 执行失败被当作非阻塞放行且
   不报警，只能靠主动探。这里不用探，因为失败自己会留一行。

2. **否决 = 落一条 ``HookVetoed``。** 否决是治理动作，必须进审计链 ——
   「谁在什么时候拦下了什么、给的理由是什么」，答不出来的否决等于黑箱。

两条都走 ``store.append_event_log`` 的**自由 ``event_type``**，
**不进 ``maos/contracts/events.py``**（铁律 1）。先例：``maos/agents/testing.py``
的 ``ArtifactSeeded``、``maos/kb/retriever.py`` 的 ``KbRetrieved``、
``maos/config/audit.py`` 的 ``ConfigChanged``。``store=None`` 时跳过落库
（口径同 ``maos/skills/invoker.py::SkillInvoker._settle``）。

## 回调改不了它看见的东西

``fire`` 的 payload 里凡是可变的（``spec`` / ``depends_on`` / ``gate_results``），
控制面侧都**先拷贝再交出来**。理由是保住上面那条设计的自洽：本模块只认 ``Veto``
一种否决形态，别的一律不算 —— 可如果回调还能顺手改写控制面手里的活对象，那条
设计就自相矛盾了。否决走不通的路（要落 ``HookVetoed``、要短路、要被人看见），
改字段反而走得通，而且一行痕迹都没有：一条 ``return None``（明确放行、不否决）
的回调把 ``effect_risk`` 从 H 改成 L，就足以让不可逆产物无人放行地落 DONE。

挂点该有的权力只有一个：说不。

## 三个挂点，只接了两个

``TASK_CREATED`` 与 ``TASK_COMPLETED`` 在 ``maos/core/control_plane.py`` 里接好了
（``create_plan`` 与 ``on_review_verdict`` 的 pass 分支）。

``WORKER_IDLE`` **只定义、不接线**：接线点是
``maos/runtime/worker.py::WorkerRuntime._reply`` 之后，那个文件这一轮归 T107，
跨轨改同一个文件只会给整合期多制造一个冲突 —— 而这个挂点没有它也能证明自己成立
（``maos/tests/test_hooks.py`` 用假 registry 直接调 ``fire`` 钉住了它的形状）。
留给整合期接。

接上的那两个也各有一块覆盖不到的面 —— ``TASK_CREATED`` 管不住重规划建/改的任务，
``TASK_COMPLETED`` 管不住 ``effect_risk=H`` 的任务。**两块都写在对应常量的注释上**，
别只读常量名就当它全覆盖：一个自以为管住了全部的挂点，比一个说清楚自己管到哪里
的挂点更危险，那还是开篇那句话的变体。

## 为什么放在 runtime/ 而不是 core/

``maos/core/control_plane.py`` 因此要 import ``maos.runtime.hooks``，方向上是
core 依赖 runtime。这不构成循环：本模块运行期**不 import 任何 maos.core 的东西**
（``Store`` 只在 ``TYPE_CHECKING`` 下引），``maos/runtime/__init__.py`` 也只有一行
docstring。core 已经在 import ``maos.tools.sandbox``，同一个形状。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:                                    # pragma: no cover - 仅类型
    from maos.core.store import Store

log = logging.getLogger("maos.hooks")

# -- 三个挂点 ------------------------------------------------------------
#: 任务落库前。payload：plan_id / trace_id / goal / role / title /
#: risk_level / effect_risk / depends_on / spec（规格 dict）。
#:
#: ``spec`` 与 ``depends_on`` 交出来的是**副本**，改它们不会影响落库的字段
#: （见模块 docstring「回调改不了它看见的东西」）。
#:
#: 🔴 **覆盖面只有 ``ControlPlane.create_plan`` 这一个入口。**
#: ``ControlPlane._apply_replan`` 的两条路都绕过它，且都不落任何审计行：
#:
#: * **覆写既有任务** —— 重规划能把一个 ``role=coding / effect_risk=L`` 的任务
#:   就地改成 ``role=payment / effect_risk=H``，等于凭空造出一个本挂点本该拦下的
#:   任务，而 hook 一次都没被问过；
#: * **``insert_task`` 直接建新任务** —— 连覆写都不用。
#:
#: 也就是说「哪些任务可以被创建」这个判据今天只管住了首次规划。返工命中
#: ``_should_replan`` 时系统会自己绕过去，不需要人参与 —— 装一条「role == payment
#: 一律 Veto」的 hook，首次规划拦得住，重规划照样把它建出来，``TaskCreationVetoed``
#: 与 ``HookVetoed`` 都是 0 行。写 hook 的人不要以为它管住了全部任务创建。
#: 本轨没补，是因为 ``_replan`` / ``_apply_replan`` 这一轮归 T107，跨轨改同一个
#: 方法只会给整合期多制造一个冲突。缺口记在 ``docs/BACKLOG.md`` 的 ``## task-T110``，
#: 接线留给整合期（覆写也算一次「这个任务该不该以这个形态存在」，不只是新建）。
TASK_CREATED = "task_created"

#: 任务判定完成、落 DONE 前。payload：task_id / plan_id / trace_id / event_id /
#: attempt / role / gate_results / artifact_count。``gate_results`` 是副本，
#: 理由同 ``TASK_CREATED``。
#:
#: 🔴 **不覆盖 ``effect_risk`` 落在 ``NEEDS_HUMAN_APPROVAL``（今天是 H）的任务。**
#: 那一支在 ``on_review_verdict`` 里 Gate 判 pass 就直接转人工（BLOCKED），最终的
#: DONE 由 ``ControlPlane.human_decision`` 的 approved 分支落 —— 那条路上本挂点
#: 一次都不开火，也不落任何「它被跳过了」的痕迹。后果得说白：一条「没有合规产物
#: 就否决完成」的治理规则，对普通任务生效，对**最高 effect_risk** 的那一类完全
#: 不生效，而两者在注册代码里长得一模一样。
#:
#: 这是刻意的取舍——挂点不参与人的决定，人已经看过产物了——**不是**「H 那一支
#: 走不到判定完成」：它走得到，只是走的是另一条路（``human_decision``）。这两句
#: 差别很大，前者是划定边界，后者是事实错误，别把注释读成后者。要让 H 也被问，
#: 得在 ``human_decision`` 的 approved 分支也开火（人仍可覆盖否决，但「挂点被问过、
#: 被人驳回」这件事至少进得了 event_log）。那属于整合期，记在 ``docs/BACKLOG.md``。
#: 边界本身由 ``test_task_completed_does_not_fire_for_high_effect_risk_even_via_human_approval``
#: 钉住 —— 边界不写测试，下一个人会以为它只是漏了。
TASK_COMPLETED = "task_completed"

#: 队友即将空闲。payload：worker_id / roles / just_finished_task_id。
#: **本轨只定义、不接线**，理由见模块 docstring「三个挂点，只接了两个」。
WORKER_IDLE = "worker_idle"

#: 认得的挂点全集。``on()`` 拿它做白名单，理由见该方法的 docstring。
EVENTS = frozenset({TASK_CREATED, TASK_COMPLETED, WORKER_IDLE})

# -- 两个自由 event_type（不进冻结契约，见模块 docstring）------------------
EVT_HOOK_FAILED = "HookFailed"
EVT_HOOK_VETOED = "HookVetoed"

#: 控制面把「完成被 hook 否决」转人工时写进 detail 的 ``reason``。
#: 定在这里而不是控制面：它是 hook 契约的一部分，下游按这个字面量认这一类转人工。
HOOK_VETO_REASON = "hook_veto"


@dataclass(frozen=True)
class Veto:
    """一次否决。``reason`` 必填且非空 —— 空理由的否决等于没说话，构造时就拒。

    为什么在**构造时**拒而不是落库时兜底：这一行会出现在审计链里，是事后唯一能
    回答「为什么这个任务没被创建」的东西。让它空着通过，等于把一条查不出所以然的
    记录留给三个月后的人 —— 而那时写 hook 的人早已不记得当时想拦什么。
    """

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Veto.reason 不许为空：说不出理由的否决不许发出")


class PlanVetoed(RuntimeError):
    """``create_plan`` 的**全部** task spec 都被 ``TASK_CREATED`` 否决。

    为什么抛异常而不是建一个空 plan：空 plan 会立刻走到
    ``ControlPlane._advance`` 的「一条活任务都不剩」分支，被收敛成 ``FAILED`` ——
    那把「治理否决了这个计划」伪装成了「计划执行失败」。两件事在 event_log 上
    必须分得开：前者是机制按预期拦住了一件不该做的事，后者是做了但没做成。
    混成一种，看板上就再也数不清「拦下了多少」。
    """

    def __init__(self, plan_id: str, vetoed: list[dict]) -> None:
        self.plan_id = plan_id
        self.vetoed = list(vetoed)
        detail = "；".join(
            f"{v.get('title', '?')}: {v.get('reason', '')}" for v in self.vetoed
        )
        super().__init__(
            f"计划 {plan_id} 的全部 {len(self.vetoed)} 个任务都被 hook 否决，"
            f"不建空计划（{detail}）"
        )


#: 回调签名：``(**payload) -> Veto | None``。返回 None 即放行。
HookFn = Callable[..., "Veto | None"]


def _hook_name(fn: HookFn) -> str:
    """回调的可读名 —— 留痕里要答得出「是哪个 hook」，``repr`` 兜底 lambda 与偏函数。"""
    return getattr(fn, "__qualname__", None) or repr(fn)


class HookRegistry:
    """挂点注册表。``store=None`` 时一切照跑，只是不留痕。

    ``store`` 可选是为了让 hook 能在没接库的场景（单测、Agent 侧的轻量装配）里
    直接用 —— 口径同 ``SkillInvoker``。代价是那些场景没有审计链。

    接控制面时**不必**记得传：``ControlPlane.__init__`` 看见 ``store is None``
    会把自己的 store 绑进来。这一条不是便利，是补漏 —— 留痕是本模块契约的一半，
    而 ``HookRegistry()`` 不传 store 既不报错也不留痕，最自然的那种写法恰好是
    静默失效的那种写法。已经绑了另一本账时不覆盖：那是调用方的显式选择。
    """

    def __init__(self, store: Store | None = None) -> None:
        self.store = store
        self._hooks: dict[str, list[HookFn]] = {}

    # ------------------------------------------------------------------
    def on(self, event: str, fn: HookFn) -> None:
        """注册一个回调。同一 event 可注册多个，``fire`` 时**按注册顺序**依次跑。

        顺序是语义的一部分，不是实现细节：先注册的先跑，于是「哪一条拦下了它」
        在同一份注册代码下是确定的、可复现的。用 set 或 dict 值去重会让这件事
        依赖插入顺序之外的东西，那一天起同一份输入就可能给出两种审计结论。

        ``event`` 不在 ``EVENTS`` 里直接抛。为什么不宽容地存下来：拼错一个挂点名
        的后果是**这个 hook 永远不会被调用，而且没有任何征兆** —— 正是本模块开篇
        要治的那个病（静默失效的挂点比没有挂点更糟）。这一条是 fail-closed 的：
        它坏掉时结果会变错（治理以为挂上了，其实没有），不是只变差。
        """
        if event not in EVENTS:
            raise ValueError(
                f"未知挂点 {event!r}；认得的是 {sorted(EVENTS)}。"
                "拼错的挂点名会让这个 hook 永远不被调用且不报警，所以这里直接拒"
            )
        if not callable(fn):
            raise TypeError(f"hook 必须可调用，收到 {type(fn).__name__}")
        self._hooks.setdefault(event, []).append(fn)

    def registered(self, event: str) -> tuple[HookFn, ...]:
        """按注册顺序返回某挂点上的回调 —— 只读视图，测试与自检用。"""
        return tuple(self._hooks.get(event, ()))

    # ------------------------------------------------------------------
    def fire(self, event: str, **payload: Any) -> Veto | None:
        """依次跑回调；**第一个返回 ``Veto`` 的即短路**并返回它，后面的不再跑。

        短路是刻意的：否决是终局判定，第二条 hook 再说什么都改变不了结果。跑完
        全部再挑一个，只会白白执行一串可能有副作用的回调，并且让「是谁拦的」
        变成一个要靠优先级规则回答的问题 —— 那个规则迟早与注册顺序分叉。

        三种返回的处置：
        * ``None`` —— 放行，继续下一个。
        * ``Veto`` —— 落 ``HookVetoed``，短路返回。
        * **其它任何值** —— 按「回调坏了」处理：落 ``HookFailed``、不否决、继续。
          理由见 ``_reject_bad_return``。
        """
        for fn in self._hooks.get(event, ()):
            try:
                out = fn(**payload)
            except Exception as exc:                 # noqa: BLE001 —— 见模块 docstring 姿态 1
                self._audit(EVT_HOOK_FAILED, payload, {
                    "hook_event": event,
                    "hook": _hook_name(fn),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                log.warning("[hook] %s 上的 %s 抛了 %s: %s —— 不否决，主链路照走",
                            event, _hook_name(fn), type(exc).__name__, exc)
                continue

            if out is None:
                continue
            if not isinstance(out, Veto):
                self._reject_bad_return(event, fn, out, payload)
                continue

            self._audit(EVT_HOOK_VETOED, payload, {
                "hook_event": event,
                "hook": _hook_name(fn),
                "reason": out.reason,
            })
            log.warning("[hook] %s 被 %s 否决：%s", event, _hook_name(fn), out.reason)
            return out
        return None

    # ------------------------------------------------------------------
    def _reject_bad_return(self, event: str, fn: HookFn, out: Any,
                           payload: dict) -> None:
        """回调返回了非 ``Veto`` 的非 None 值 —— 当成它坏了，不当成否决。

        最危险的具体形态是 ``return False``：写 hook 的人以为自己拦下来了，
        真按布尔去解读的话 ``True``/``False`` 谁是否决还得猜。所以只认 ``Veto``
        这一种否决形态，别的一律不算 —— 但**必须留痕**，否则就成了「他以为拦了、
        实际没拦、还没人知道」，那是本模块开篇那句话的最坏版本。

        为什么不直接抛：抛出去会掀掉主链路（create_plan 当场炸），而这属于
        第三方回调写错，不是主流程的正确性不变量被破坏 —— 同姿态 1，fail-open。
        """
        self._audit(EVT_HOOK_FAILED, payload, {
            "hook_event": event,
            "hook": _hook_name(fn),
            "error_type": "BadReturn",
            "error": f"hook 必须返回 Veto 或 None，收到 {type(out).__name__}: {out!r}",
        })
        log.warning("[hook] %s 上的 %s 返回了 %s，不是 Veto —— 不当作否决",
                    event, _hook_name(fn), type(out).__name__)

    def _audit(self, event_type: str, payload: dict, detail: dict) -> None:
        """落一行自由 ``event_type`` 的 event_log。``store=None`` 时跳过。

        锚点（trace/plan/task/event id）从 payload 里取：挂点自己不持有上下文，
        谁 fire 谁带。取不到就落空串 —— 这与 ``append_event_log`` 里其余调用点
        的口径一致（``plan_id`` 是 NOT NULL，空串而非 NULL）。
        """
        if self.store is None:
            return
        self.store.append_event_log({
            "event_id": payload.get("event_id") or "",
            "trace_id": payload.get("trace_id") or "",
            "plan_id": payload.get("plan_id") or "",
            "task_id": payload.get("task_id"),
            "event_type": event_type,
            "from_state": "",
            "to_state": "",
            "detail": detail,
        })
