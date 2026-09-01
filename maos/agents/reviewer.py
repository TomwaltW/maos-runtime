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

from maos.agents._truncate import PackedItems, pack_json_array
from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.agents.testing import record_seeded_artifact
from maos.artifacts import KIND_REVIEW_NOTE
from maos.contracts.events import new_id
from maos.model.client import Tier

SYSTEM = """你是 Reviewer Agent，对交付产物做语义审查。只输出 JSON，不要解释文字，格式：
{"defects":[{"path":"...","severity":"blocker|major|minor","note":"..."}],"conclusion":"..."}
只写你在产物里真实看到的问题；没有问题就给空 defects，不要为了显得认真而编。"""

PROMPT_MARKER = "语义审查产物清单"

#: 产物清单送进模型的字符预算。原先是 ``json.dumps(...)[:8000]`` 那一刀的 8000，
#: 数值照旧，切法改成按份装填（见 ``_truncate.pack_json_array``）。
ARTIFACT_BUDGET = 8000


def _describe_artifact(art: object) -> str:
    """被省略产物在截断说明里的点名方式 —— 要能让人/模型指得回是哪一份。"""
    if isinstance(art, dict):
        bits = [str(art[k]) for k in ("task_id", "kind", "version") if art.get(k) is not None]
        if bits:
            return "/".join(bits)
    return json.dumps(art, ensure_ascii=False, default=str)[:60]


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
        packed = pack_json_array(artifacts, budget=ARTIFACT_BUDGET,
                                 describe=_describe_artifact)
        if artifacts and packed.presented == 0:
            # 一份都塞不进去 -> 与「超时」同类：审查没做成，不许伪装成审过了。
            return self._needs_human(
                f"语义审查无法开始：{packed.total} 份产物中第 1 份单份就超过 "
                f"{ARTIFACT_BUDGET} 字符预算（完整清单 {packed.original_chars} 字符），"
                "一份都呈现不了。请拆小产物或分批送审。")

        try:
            raw = self.ask(SYSTEM, self._build_prompt(ctx, packed))
        except TimeoutError as exc:
            return self._needs_human(f"语义审查超时：{exc}")
        except Exception as exc:                      # noqa: BLE001 —— 见 _needs_human
            return self._needs_human(f"语义审查未完成（{type(exc).__name__}: {exc}）")

        note = self._parse(raw, packed)
        if note is None:
            return self._needs_human("语义审查输出不合契约（非 JSON 或缺 conclusion）")

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": KIND_REVIEW_NOTE, "content": note}],
            # ``reviewed`` 是**实际送到模型眼前的份数**，不是清单长度。截断发生时
            # 两者不等，写 len(artifacts) 就是声称审了全部 —— 那正是这次要修的假话。
            metrics={"defects": len(note["defects"]), "reviewed": packed.presented,
                     "artifacts_total": packed.total, "truncated": packed.truncated},
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
    def _build_prompt(ctx: TaskContext, packed: PackedItems) -> str:
        """没触发截断时，与改造前的提示词**逐字节一致**；触发了才多出一段截断说明。"""
        blocks = [
            f"{PROMPT_MARKER}（计划 {ctx.plan_id}，共 {packed.total} 份）：",
            packed.payload,
        ]
        if packed.truncated:
            blocks.append(packed.note)
        blocks.append(f"验收标准：{json.dumps(list(ctx.acceptance), ensure_ascii=False)}")
        return "\n\n".join(blocks)

    @staticmethod
    def _parse(raw: str, packed: PackedItems) -> dict | None:
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
        # 意见书里的 reviewed 同样是**实际审过的份数**。截断时额外把差额写进
        # summary —— 这份意见书是给人看的，「只审了 M/N 份」必须一眼看得见。
        scope = ("" if not packed.truncated
                 else f"（清单被截断：共 {packed.total} 份，仅审 {packed.presented} 份）")
        return {
            "defects": defects,
            "conclusion": conclusion,
            "reviewed": packed.presented,
            "artifacts_total": packed.total,
            "truncated": packed.truncated,
            "summary": (f"语义审查完成：{len(defects)} 条缺陷，"
                        f"结论「{conclusion[:40]}」{scope}"),
            "self_check": {"build": "pass", "lint": "pass"},
        }


def review_after_gate(reviewer: ReviewerAgent, cp, plan_id: str, *, host_task: dict) -> AgentOutput:
    """Gate 之后、审批之前的那一次语义审查 —— 场景 1/2 共用这一个入口。

    不经 Worker 队列（与 ManagerAgent 同理：它不是被派发的任务角色，
    而是流程里一个明确位置上的调用），所以 review_note 由这里直接落库，
    挂在 ``host_task`` 名下、版本取该任务当前 attempt。

    落库之后补一条 ``ArtifactSeeded``（``record_seeded_artifact``）—— 这是三条
    绕开 ``on_task_result`` 的旁路里最后一条补上来源的。补的是**审计链**不是成色：
    ``provenance`` 照旧标 ``artifact_seeded`` 而不是 ``task_result``，这份 review_note
    确实没走正路，冒充正路就是撒谎；补上的只是「指得到是哪一步产的」。
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
        artifact_id = new_id("art")
        cp.store.insert_artifact({
            "artifact_id": artifact_id, "task_id": host_task["task_id"],
            "plan_id": plan_id, "kind": art["kind"],
            "version": host_task["attempt"], "content": art["content"],
        })
        record_seeded_artifact(
            cp.store, plan_id=plan_id, task_id=host_task["task_id"],
            artifact_id=artifact_id, kind=art["kind"],
            version=host_task["attempt"],
            trace_id=host_task.get("trace_id") or "",
            source="maos.agents.reviewer.review_after_gate",
            reason=("Gate 之后、审批之前的那一次语义审查：Reviewer 不经 Worker 队列"
                    "（与 ManagerAgent 同理，它是流程里一个明确位置上的调用，不是被"
                    "派发的任务角色），review_note 因此由本函数直接落库 —— 没有一次"
                    "on_task_result 能带回它。"),
            # 不带 sandbox_mode / scripted：那两个键是 test_report 的分水岭
            # （预置件 vs 现跑沙箱）。review_note 不经沙箱，编一个值进去就是往
            # 审计链里塞假事实。
        )
    return out
