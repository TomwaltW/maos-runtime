"""req.normalize —— 把自然语言目标归一成「可执行 + 可验收」的结构化需求。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B，逐字段）：
  入：{"goal": str, "context"?: dict}
  出：{"normalized_goal": str, "constraints": list[str], "acceptance_suggestions": list[str]}

两条分支刻意不对称：
  · ``ctx.model is None``（调用方没接线）→ 规则兜底，确定性输出，不失败。
    skill 不该因为上层忘了传 model 就把整条链路拖挂。
  · 有 model 但输出不符合出参契约 → **抛**，交给 failure_policy="retry" 再试一次。
    这是真故障，静默降级会把「模型坏了」伪装成「需求就长这样」，比失败更糟。
"""

from __future__ import annotations

import json
import time
from typing import Any

from maos.core.store import record_model_failure, record_model_usage
from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

#: 落 ``model_usage`` 时写进 ``call_site`` 列的值。
CALL_SITE = "maos/skills/builtin/req_normalize.py::ReqNormalizeSkill.run"

SYSTEM = """你是需求归一化助手，把用户目标改写成一句可执行、可验收的目标，并抽出约束与验收建议。
只输出 JSON，不要任何解释文字，格式：
{"normalized_goal":"...","constraints":["..."],"acceptance_suggestions":["..."]}
constraints 只写目标或上下文里真实出现的限制，不要编造；acceptance_suggestions 必须可机器判定。"""

# 规则兜底用的验收模板 —— 是「建议」不是事实，所以可以是通用句式，
# 但绝不往 constraints 里塞模板：约束编错了会直接误导后续所有任务。
FALLBACK_ACCEPTANCE = (
    "目标达成有可机器判定的证据（命令输出或测试结果）",
    "变更范围不超出目标描述",
)


def _clean_lines(value: Any) -> list[str]:
    """任何形态收敛成 list[str]：非列表包一层，逐项 str + strip，丢掉空串。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [s for s in (str(v).strip() for v in items) if s]


@register_skill
class ReqNormalizeSkill(Skill):
    contract = SkillContract(
        name="req.normalize",
        version="1.0.0",
        purpose="把自然语言目标归一成可执行、可验收的结构化需求",
        input_schema={"goal": "str", "context": "dict?"},
        output_schema={
            "normalized_goal": "str",
            "constraints": "list[str]",
            "acceptance_suggestions": "list[str]",
        },
        preconditions=["goal"],
        depends_tools=[],
        failure_policy="retry",
        max_retries=1,
        security_boundary="只读入参，不写任何资源、不调用任何工具；context 原样透传给模型，不落盘",
        reuse_note="Manager 规划前的统一入口；任何角色要澄清目标都复用它，不要各写一份归一逻辑",
        owner_roles=["manager"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        goal = " ".join(str(payload.get("goal") or "").split())
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            context = {"raw": context}

        if ctx.model is None:
            return self._fallback(goal, context)

        # 接住整个 ModelResponse 再取 .text：tokens_in/out 原先在这一行被丢掉。
        # 归属键从 extras 取 —— invoker 把 plan_id / task_id / trace_id 一路带到这里
        # （见 skills/invoker.py 的 SkillContext 构造），不需要另造一个 Run id。
        tier = ctx.extras.get("tier") or Tier.STRONG
        started = time.perf_counter()
        try:
            resp = ctx.model.complete(
                system=SYSTEM,
                user=self._build_prompt(goal, context),
                tier=tier,
            )
        except Exception as exc:
            # 失败也要留账（T54）：口径同 core/store.py::record_model_failure ——
            # 不往 model_usage 编 0 token，落进不谈 token 的失败表，异常照旧上抛。
            record_model_failure(
                ctx.store, exc,
                agent_role=getattr(ctx.identity, "role", "") or "unknown",
                call_site=CALL_SITE, tier=tier,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=getattr(ctx.model, "model", "") or "",
                trace_id=ctx.extras.get("trace_id") or "",
                plan_id=ctx.extras.get("plan_id") or "",
                task_id=ctx.extras.get("task_id"),
            )
            raise
        record_model_usage(
            ctx.store, resp, client=ctx.model,
            agent_role=getattr(ctx.identity, "role", "") or "unknown",
            call_site=CALL_SITE, tier=tier,
            latency_ms=int((time.perf_counter() - started) * 1000),
            trace_id=ctx.extras.get("trace_id") or "",
            plan_id=ctx.extras.get("plan_id") or "",
            task_id=ctx.extras.get("task_id"),
        )
        raw = resp.text
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"req.normalize 模型输出非合法 JSON: {exc}") from None
        return self._coerce(data, goal)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(goal: str, context: dict) -> str:
        return "\n\n".join([
            f"原始目标：{goal}",
            f"补充上下文：{json.dumps(context, ensure_ascii=False)}",
        ])

    @staticmethod
    def _coerce(data: Any, goal: str) -> dict:
        """校验模型输出并收敛形状；normalized_goal 缺失或为空即视为失败。"""
        if not isinstance(data, dict):
            raise ValueError(f"req.normalize 输出应为 JSON 对象，实际 {type(data).__name__}")
        normalized = " ".join(str(data.get("normalized_goal") or "").split())
        if not normalized:
            raise ValueError("req.normalize 输出缺少 normalized_goal")
        return {
            "normalized_goal": normalized,
            "constraints": _clean_lines(data.get("constraints")),
            "acceptance_suggestions": _clean_lines(data.get("acceptance_suggestions")),
        }

    @staticmethod
    def _fallback(goal: str, context: dict) -> dict:
        """无模型时的确定性兜底：只搬运上下文里已有的约束，一条都不编。"""
        return {
            "normalized_goal": goal or "（空目标，待澄清）",
            "constraints": _clean_lines(context.get("constraints")),
            "acceptance_suggestions": list(FALLBACK_ACCEPTANCE),
        }
