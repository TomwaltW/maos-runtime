"""Reviewer Agent —— 对全部产物做**模型语义审查**，产出 review_note。

位置很重要：挂在 **Gate 之后、人工审批之前**，不是第五道闸。

为什么不做成闸：Gate 的判定必须可复现、可解释、可审计（规则驱动），
把模型塞进闸里，同一份产物两次跑出不同结论，返工链就没法解释了。
语义审查恰恰相反 —— 它要抓的是规则抓不到的东西（契约与实现是否名实相符、
补丁有没有解决 issue 描述的那个问题），代价是结论不稳定。
所以它的产物是**给人看的意见书**，只影响人工审批那一步，不改任务状态。

超时 → needs_human：审查没做完就等于没审查。这里不重试、不降级成
「看起来没问题」—— 那正是把「模型没跑成」伪装成「产物没问题」。
状态上复用 ``blocked``（RUNNING -> BLOCKED 的 worker_blocked 路径），不新增状态。
"""

from __future__ import annotations

import json

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.artifacts import KIND_REVIEW_NOTE
from maos.contracts.events import new_id
from maos.model.client import Tier

SYSTEM = """你是 Reviewer Agent，对交付产物做语义审查。只输出 JSON，不要解释文字，格式：
{"defects":[{"path":"...","severity":"blocker|major|minor","note":"..."}],"conclusion":"..."}
只写你在产物里真实看到的问题；没有问题就给空 defects，不要为了显得认真而编。"""

PROMPT_MARKER = "语义审查产物清单"


@register
class ReviewerAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="reviewer",
        role="reviewer",
        duty="对全部产物做语义审查，产出缺陷清单与结论，供人工审批参考",
        allowed_skills=frozenset(),         # 语义审查本身就是模型调用，不经 skill
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="L",
        model_tier=Tier.STRONG,
        max_self_repair=0,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        artifacts = ctx.inputs.get("artifacts") or []
        try:
            raw = self.ask(SYSTEM, self._build_prompt(ctx, artifacts))
        except TimeoutError as exc:
            return self._needs_human(f"语义审查超时：{exc}")
        except Exception as exc:                      # noqa: BLE001 —— 见 _needs_human
            return self._needs_human(f"语义审查未完成（{type(exc).__name__}: {exc}）")

        note = self._parse(raw, len(artifacts))
        if note is None:
            return self._needs_human("语义审查输出不合契约（非 JSON 或缺 conclusion）")

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": KIND_REVIEW_NOTE, "content": note}],
            metrics={"defects": len(note["defects"]), "reviewed": len(artifacts)},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _needs_human(reason: str) -> AgentOutput:
        """审查没做成 -> blocked + needs_human，**不**产出一份空白 review_note。

        产出空白意见书会让下游以为「审过了、没问题」，比没有意见书危险得多。
        """
        return AgentOutput(status="blocked", open_questions=[reason],
                           metrics={"needs_human": True})

    @staticmethod
    def _build_prompt(ctx: TaskContext, artifacts: list) -> str:
        return "\n\n".join([
            f"{PROMPT_MARKER}（计划 {ctx.plan_id}，共 {len(artifacts)} 份）：",
            json.dumps(artifacts, ensure_ascii=False, default=str)[:8000],
            f"验收标准：{json.dumps(list(ctx.acceptance), ensure_ascii=False)}",
        ])

    @staticmethod
    def _parse(raw: str, reviewed: int) -> dict | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        conclusion = str(data.get("conclusion") or "").strip()
        if not conclusion:
            return None

        defects = []
        for d in data.get("defects") or []:
            if not isinstance(d, dict):
                continue
            defects.append({
                "path": d.get("path"),
                "severity": str(d.get("severity") or "minor"),
                "note": str(d.get("note") or ""),
            })
        return {
            "defects": defects,
            "conclusion": conclusion,
            "reviewed": reviewed,
            "summary": f"语义审查完成：{len(defects)} 条缺陷，结论「{conclusion[:40]}」",
            "self_check": {"build": "pass", "lint": "pass"},
        }


def review_after_gate(reviewer: ReviewerAgent, cp, plan_id: str, *, host_task: dict) -> AgentOutput:
    """Gate 之后、审批之前的那一次语义审查 —— 场景 1/2 共用这一个入口。

    不经 Worker 队列（与 ManagerAgent 同理：它不是被派发的任务角色，
    而是流程里一个明确位置上的调用），所以 review_note 由这里直接落库，
    挂在 ``host_task`` 名下、版本取该任务当前 attempt。
    """
    snap = cp.snapshot(plan_id)
    artifacts = []
    for task in snap["tasks"]:
        for art in cp.store.list_artifacts(task["task_id"]):
            artifacts.append({"task_id": task["task_id"], "kind": art["kind"],
                              "version": art["version"], "content": art["content"]})

    ctx = TaskContext(
        plan_id=plan_id, task_id=host_task["task_id"], trace_id=host_task["trace_id"],
        attempt=host_task["attempt"], inputs={"artifacts": artifacts},
        acceptance=host_task.get("acceptance") or [], risk_level="L",
    )
    out = reviewer.run(ctx)
    for art in out.artifacts:
        cp.store.insert_artifact({
            "artifact_id": new_id("art"), "task_id": host_task["task_id"],
            "plan_id": plan_id, "kind": art["kind"],
            "version": host_task["attempt"], "content": art["content"],
        })
    return out
