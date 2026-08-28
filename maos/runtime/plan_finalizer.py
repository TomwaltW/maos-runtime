"""PlanFinalizer —— Plan 到终态后把这一趟的经验沉淀进知识库。

**它独立存在的全部理由是：不把模型调用塞进 Control Plane**（phase-4.md 第 2 步）。
控制面是唯一的状态权威，状态迁移必须可复现、可解释、可审计；复盘则相反 ——
它是可以慢、可以用模型、可以失败也不影响主链路的事。两件事混在一个类里，
控制面就再也说不清「这次状态变化到底是谁决定的」。

所以接线方式是**轮询**而不是回调：finalizer 观察 Plan 状态，控制面不知道它存在。
Plan 没到终态就什么都不做，到了终态且没沉淀过，才复盘一次。

复盘本身当前是**规则驱动、零模型**的：复盘素材（终态、attempt、findings、
是否重规划过、是否执行过补偿）全都在 event_log 与 task 行里，规则能把它们
组织成可检索的条目，而模型只会让同一份数据每次产出不同的文字。
``model`` 参数照常留着并透传进 skill 上下文 —— 这里是模型**该**在的地方，
换成模型复盘时不需要动 Control Plane 一行，这正是本类存在的意义。
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from maos.agents.base import AgentIdentity
from maos.contracts.states import PlanState
from maos.model.client import Tier
from maos.skills.invoker import SkillInvoker

log = logging.getLogger("maos.finalizer")

SKILL_SINK = "kb.sink"
TERMINAL_PLAN_STATES = (PlanState.DONE, PlanState.FAILED)

# 复盘条目上限（phase-4.md：「复盘成 1-3 条」）。多了就不是复盘是流水账，
# 检索时噪声会把真正有用的那条淹掉。
MAX_ENTRIES = 3


class PlanFinalizer:
    """轮询 Plan 终态 -> 复盘 -> 经 kb.sink 落 knowledge。"""

    identity = AgentIdentity(
        agent_id="plan-finalizer",
        role="finalizer",
        duty="Plan 到终态后把 findings 与 verdicts 复盘成 1-3 条知识，供后续 Plan 检索复用",
        # 只有写知识这一项权限：它读得到全量 store，但除了 knowledge 表什么都不该动。
        allowed_skills=frozenset({SKILL_SINK}),
        allowed_tools=frozenset(),
        write_scope=frozenset({"knowledge"}),
        max_risk="L",
        model_tier=Tier.LIGHT,
    )

    def __init__(self, store: Any, *, model: Any = None) -> None:
        self.store = store
        self.model = model
        self.skills = SkillInvoker(self.identity, store)

    # ------------------------------------------------------------------
    def poll(self, plan_id: str) -> list[str]:
        """Plan 到终态则复盘一次，返回写入的 knowledge_id 列表；否则返回空。

        幂等走 store 的幂等闸而不是内存标志：驱动循环可能调很多次，而
        「这个 plan 复盘过了」是必须跨实例成立的事实，内存标志换个 finalizer 实例就失效。
        """
        plan = self.store.get_plan(plan_id)
        if plan is None or plan["state"] not in TERMINAL_PLAN_STATES:
            return []
        if self.store.claim_idempotency(f"finalize:{plan_id}", "finalize", plan_id) is not None:
            return []                       # 已复盘过，短路

        tasks = self.store.list_tasks(plan_id)
        rows = self.store.list_event_log(plan_id)
        entries = self.distill(plan, tasks, rows)

        ids: list[str] = []
        for entry in entries:
            res = self.skills.invoke(SKILL_SINK, {
                "plan_id": plan_id,
                "kind": entry["kind"],
                "title": entry["title"],
                "body": entry["body"],
                "tags": entry["tags"],
            }, extras={"model": self.model, "plan_id": plan_id,
                       "trace_id": plan["trace_id"], "tier": self.identity.model_tier})
            if res.status == "ok" and res.output:
                ids.append(res.output["knowledge_id"])
            else:
                # 沉淀失败不该掀翻已经跑完的 Plan —— 但也不能不出声。
                log.warning("[%s] kb.sink 失败，该条经验未沉淀: %s", plan_id, res.error)
        self.store.finish_idempotency(f"finalize:{plan_id}", {"knowledge": len(ids)})
        log.info("[%s] 复盘完成，沉淀 %d 条", plan_id, len(ids))
        return ids

    # ------------------------------------------------------------------
    @staticmethod
    def distill(plan: dict, tasks: list[dict], rows: list[dict]) -> list[dict]:
        """从终态快照里提炼 1-3 条知识。纯函数：同样的快照必然得到同样的条目。

        第一条恒出（case：这趟到底发生了什么），后两条按证据出 ——
        没有 findings 就不编一条「注意质量」的规则出来，那种条目检索到了也没用。
        """
        goal = plan["goal"]
        state = plan["state"]
        attempts = sum(t["attempt"] for t in tasks)
        findings = [f for t in tasks for f in t["findings"]]

        entries = [{
            "kind": "case",
            "title": f"{goal} —— {state}",
            "body": (
                f"目标：{goal}\n"
                f"结局：Plan {state}，共 {len(tasks)} 个任务、累计 {attempts} 次尝试。\n"
                f"任务终态：" + "；".join(f"{t['title']}={t['state']}" for t in tasks) + "\n"
                f"本轮累计 findings {len(findings)} 条。"
            ),
            "tags": ["case", state.lower(), *sorted({t["role"] for t in tasks})],
        }]

        if findings:
            by_gate = Counter(str(f.get("gate") or "unknown") for f in findings
                              if isinstance(f, dict))
            gate, hits = by_gate.most_common(1)[0]
            samples = [str(f.get("message") or "") for f in findings
                       if isinstance(f, dict) and f.get("gate") == gate]
            entries.append({
                "kind": "rule",
                "title": f"{gate} 闸是本轮最常被卡的一道（{hits} 次）",
                "body": (
                    f"目标「{goal}」执行中，{gate} 闸累计判出 {hits} 条 finding，"
                    f"是本轮占比最高的一道。典型问题：\n"
                    + "\n".join(f"  · {s}" for s in dict.fromkeys(samples) if s)
                    + f"\n下次规划同类任务时，把这条写进 acceptance 可以省掉一轮返工。"
                ),
                "tags": ["rule", "gate", gate],
            })

        replans = sum(1 for e in rows
                      if e.get("event_type") == "PlanTransition"
                      and e.get("from_state") == PlanState.RUNNING
                      and e.get("to_state") == PlanState.PENDING)
        compensations = [e for e in rows if e.get("event_type") == "CompensationExecuted"]
        if replans or compensations:
            entries.append({
                "kind": "rule",
                "title": f"治理动作留痕：重规划 {replans} 次、补偿 {len(compensations)} 次",
                "body": (
                    f"目标「{goal}」触发过 {replans} 次重规划"
                    f"、{len(compensations)} 次补偿回滚。\n"
                    + "\n".join(
                        f"  · 补偿 {e['task_id']}：ok={e['detail'].get('ok')}"
                        f" error={e['detail'].get('error')}"
                        for e in compensations)
                    + ("\n首版方案被判定走不通，说明规划阶段的验收标准不够具体。"
                       if replans else "")
                ),
                "tags": ["rule", "governance",
                         *(["replan"] if replans else []),
                         *(["compensation"] if compensations else [])],
            })

        return entries[:MAX_ENTRIES]
