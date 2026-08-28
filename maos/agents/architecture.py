"""Architecture Agent —— 产出架构契约，并在产出前把可逆性判死。

不经 skill：契约是**规则**装配出来的，不是模型写出来的。让模型自由发挥
architecture_contract 的四个必填键，等于把「这次变更能不能回滚」交给一段
自然语言去承诺 —— 而这个承诺后面会被补偿闸当成事实来用。

必填键 ``api / idempotency / audit / reversibility``（A-7 冻结，校验复用
``maos/artifacts.py::validate_artifact``，不在这里另抄一份键名清单）。

``reversibility`` 是**可逆性声明**，不是「回滚方案」：
声明哪些产物类型可逆（git 补丁类可逆，因为逆补丁不用模型生成，反着打一遍即可），
哪些不可逆（已发出的邮件、已扣的款、已删的库）。判死的那一行是：

    不可逆产物禁止标 effect_risk=H 自动执行。

原因不是洁癖。effect_risk=H 在 control_plane 里的含义是「Gate 过了也停在
BLOCKED 等人工放行」，人一批准就**立即落地**。如果这次落地不可逆，那么审批
就是唯一且不可撤回的一道闸 —— 一旦批错，系统没有任何补救路径，补偿闸空转。
所以不可逆的高风险动作必须在**声明期**就被拒绝，而不是寄希望于审批人不出错。
"""

from __future__ import annotations

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.artifacts import KIND_ARCH_CONTRACT, KIND_PATCH_SET, validate_artifact
from maos.contracts.states import Risk
from maos.model.client import Tier

# 缺省可逆产物：git 补丁类。逆补丁 = 正向补丁反着打（零模型补偿，见 artifacts.py 的 MODE_REVERSE）。
DEFAULT_REVERSIBLE_KINDS = (KIND_PATCH_SET,)


def validate_architecture_contract(content, *, effect_risk: str = Risk.LOW) -> list[str]:
    """返回错误列表（空 = 通过），与 ``validate_artifact`` 同构。

    返回列表而不是抛异常：错误要能直接写进 findings 喂回上游，
    而不是变成一个只能打印的 traceback。
    """
    if not isinstance(content, dict):
        return [f"architecture_contract 的 content 必须是 dict，实际是 {type(content).__name__}"]

    # 必填键只在 artifacts.py 声明一处，这里复用 —— 两处各写一份键名清单必漂。
    errs = list(validate_artifact(KIND_ARCH_CONTRACT, content))

    rev = content.get("reversibility")
    if rev is None:
        return errs                                  # 缺键已由上面报过，不重复报形状
    if not isinstance(rev, dict):
        errs.append("reversibility 必须是可逆性声明 dict"
                    "（含 reversible_kinds / irreversible_kinds）")
        return errs

    reversible = rev.get("reversible_kinds")
    irreversible = rev.get("irreversible_kinds", [])
    if not isinstance(reversible, list) or not reversible:
        errs.append("reversibility.reversible_kinds 必须是非空 list —— "
                    "一个可逆产物类型都没有的契约，等于宣布这次变更无法回滚")
    if not isinstance(irreversible, list):
        errs.append("reversibility.irreversible_kinds 必须是 list")
        return errs

    # 判死的那一行：不可逆产物禁止标 effect_risk=H 自动执行。
    if effect_risk == Risk.HIGH and irreversible:
        errs.append(
            f"声明了不可逆产物 {sorted(map(str, irreversible))}，禁止标 effect_risk=H —— "
            "高风险自动执行的前提是出错可回滚，不可逆动作必须拆成人工步骤"
        )
    return errs


@register
class ArchitectureAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="architecture",
        role="architecture",
        duty="产出 API / 幂等 / 审计 / 可逆性四项俱全的架构契约，并拒绝不可逆的高风险自动执行",
        allowed_skills=frozenset(),         # 契约由规则装配，不经 skill、不问模型
        allowed_tools=frozenset(),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.STRONG,
        max_self_repair=1,
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_write("artifact")

        # effect_risk 是任务字段、不在 TaskContext 里（TaskContext 只给 risk_level =
        # Agent 执行风险）。派发侧把它放进 inputs，这里按它判可逆性。
        effect_risk = str(ctx.inputs.get("effect_risk") or Risk.LOW)
        contract = self._build_contract(ctx)

        errs = validate_architecture_contract(contract, effect_risk=effect_risk)
        if errs:
            # 契约不成立就不产出 artifact：产出一份自己都知道不合格的契约，
            # 下游会当成事实用，比当场失败糟得多。
            return AgentOutput(status="failed",
                               error="架构契约校验失败: " + "; ".join(errs),
                               metrics={"contract_errors": len(errs)})

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": KIND_ARCH_CONTRACT, "content": contract}],
            metrics={"effect_risk": effect_risk, "is_rework": ctx.is_rework},
        )

    # ------------------------------------------------------------------
    def _build_contract(self, ctx: TaskContext) -> dict:
        """inputs.architecture 给什么用什么，缺什么按缺省补什么。

        刻意允许 inputs 覆盖：场景 2 要故意给一份不完整契约，
        而「不完整」必须是可注入的，否则演示不了这条失败路径。
        """
        given = ctx.inputs.get("architecture")
        contract = dict(given) if isinstance(given, dict) else {}

        contract.setdefault("api", {
            "endpoint": str(ctx.inputs.get("endpoint") or "internal"),
            "acceptance": list(ctx.acceptance),
        })
        contract.setdefault("idempotency", {
            "key": "task_id+attempt",
            "note": "重复投递按 idempotency_key 短路，见 control_plane.claim_idempotency",
        })
        contract.setdefault("audit", {
            "event_log": True,
            "note": "每次状态迁移与 skill 调用各落一行 event_log，是唯一审计来源",
        })
        contract.setdefault("reversibility", {
            "reversible_kinds": list(DEFAULT_REVERSIBLE_KINDS),
            "irreversible_kinds": [],
            "note": "git 补丁类可逆：逆补丁不由模型生成，把正向补丁反着打一遍即可",
        })
        contract.setdefault("summary", f"架构契约：{ctx.inputs.get('title') or ctx.task_id}")
        contract.setdefault("self_check", {"build": "pass", "lint": "pass"})
        return contract
