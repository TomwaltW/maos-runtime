"""Testing Agent —— 经 ``test.verify`` 产出**真实**测试报告。

这个角色是本 Phase 的分水岭：在它之前，「任务完成」的证据是 Coding Agent 自己
写的 ``self_check``；在它之后，证据是一份跑出来的报告。Gate 的判据随之从
「Agent 说自检过了」改成「报告里有没有挂掉的用例」（见 runtime/gate.py）。

``test.verify`` 拿不到真报告有两种形态，本文件对两者一视同仁：

  * skill 未注册 —— 并行期被调方尚未合并时的常态，``SkillInvoker`` 按 A-5
    软兜底返回 ``failed / skill_not_found:test.verify``；
  * skill 注册了但工具没跑成 —— 沙箱起不来、workdir 不存在之类。此时
    ``test.verify`` 按自己的契约**不抛**，返回 ok 并把原因写进报告的 tool_error。

两者都是「没有证据」，都不是故障。中间这一步别看走眼：**第二种同样 status=ok**，
把它当成真报告收下，「没跑成」就会被当成「0 条失败」交给 Gate（详见 ``_report_from``）。

    tool_error 与 failed 是两回事 —— 前者是工具根本没跑成，后者是用例真的挂了。
    Gate 对这两种的判定不一样，所以这里绝不能把「没跑成」压成「0 条失败」。

**没有脚本化回落**。这里曾有一条 ``scripted_report`` 分支：沙箱没跑成就从
``ctx.inputs`` 取一份预置报告交出去。那是一条假绿路径 —— 演示当天 Docker 一挂，
屏幕照样全绿而没有人知道。删掉之后，「跑不成」的唯一出口是带 tool_error 的报告，
Gate 判 blocker，屏幕当场变红。这正是本模块存在的意义：证据要么是跑出来的，
要么就没有，不许有第三种。

``seed_scripted_report()`` 是另一回事，仍然保留：场景 3（审批）/ 5（补偿）
**不跑测试**，报告在那里是前置条件而不是产物（见 ``scenario_3.py`` 的自述）。
判据是「宣称真跑的场景不许有脚本化报告」，不是「全仓不许有」。
"""

from __future__ import annotations

from typing import Any

from maos.agents.base import AgentIdentity, AgentOutput, BaseAgent, TaskContext, register
from maos.artifacts import KIND_TEST_REPORT
from maos.model.client import Tier

SKILL_VERIFY = "test.verify"

STATUS_FAILED = "failed"
STATUS_PASSED = "passed"


def make_test_report(
    *,
    passed: int = 0,
    failed: int = 0,
    errors: int = 0,
    cases: Any = (),
    duration: float = 0.0,
    tool_error: str | None = None,
    summary: str = "",
    target_task_id: str | None = None,
    target_attempt: int | None = None,
) -> dict:
    """按 C-7 冻结形状装配一份 test_report content。

    只有这一处装配报告 —— 场景、测试、软兜底分别手搓一份 dict，
    迟早在字段名上分叉，而分叉的那次是静默的（Gate 读不到 cases 就当没失败）。

    ``summary`` 不是可选装饰：Gate 的 evidence 闸要求每个 artifact 都有变更说明，
    缺了会让每一份报告都多带一条 minor finding。
    """
    case_list = [dict(c) for c in (cases or []) if isinstance(c, dict)]
    if not summary:
        summary = (f"测试工具未跑成：{tool_error}" if tool_error
                   else f"测试报告：{passed} 过 / {failed} 挂 / {errors} 错")
    report = {
        "passed": int(passed),
        "failed": int(failed),
        "errors": int(errors),
        "cases": case_list,
        "duration": float(duration),
        "tool_error": tool_error,
        "summary": summary,
    }
    if target_task_id is not None:
        # 报告指向「被验的是哪个任务的哪一次 attempt」。Gate 靠这两个字段把
        # 一份挂在验证方名下的报告，认领到被验任务的验收闸上（见 gate.py）。
        report["target_task_id"] = target_task_id
        report["target_attempt"] = target_attempt
    return report


@register
class TestingAgent(BaseAgent):
    identity = AgentIdentity(
        agent_id="testing",
        role="testing",
        duty="在沙箱里跑真实测试并产出结构化报告，为 Gate 提供可验证的验收证据",
        allowed_skills=frozenset({SKILL_VERIFY}),
        allowed_tools=frozenset({"sandbox"}),
        write_scope=frozenset({"artifact"}),
        max_risk="M",
        model_tier=Tier.MEDIUM,
        max_self_repair=0,          # 测试不自修复：报告怎么样就是怎么样
    )

    def run(self, ctx: TaskContext) -> AgentOutput:
        self.check_risk(ctx.risk_level)
        self.check_tool("sandbox")
        self.check_write("artifact")

        extras = {
            "model": self.model,
            "tier": self.identity.model_tier,
            "plan_id": ctx.plan_id,
            "task_id": ctx.task_id,
            "trace_id": ctx.trace_id,
            "attempt": ctx.attempt,
        }
        target_task_id = str(ctx.inputs.get("verify_target") or ctx.task_id)
        target_attempt = int(ctx.inputs.get("verify_attempt") or ctx.attempt)

        res = self.skills.invoke(SKILL_VERIFY, {
            "workdir": ctx.inputs.get("workdir"),
            "patch_ref": ctx.inputs.get("patch_ref"),
            "acceptance": ctx.acceptance,
        }, extras=extras)

        report = self._report_from(res)
        report["target_task_id"] = target_task_id
        report["target_attempt"] = target_attempt

        return AgentOutput(
            status="ok",
            artifacts=[{"kind": KIND_TEST_REPORT, "content": report}],
            metrics={
                "failed": report.get("failed"),
                "tool_error": report.get("tool_error"),
                "skill_status": res.status,
                "is_rework": ctx.is_rework,
            },
        )

    # ------------------------------------------------------------------
    def _report_from(self, res) -> dict:
        """真报告，否则一份带 tool_error 的空报告。两条都不抛。

        不抛是硬要求：``test.verify`` 未注册在并行期是常态（A-5），
        抛出去会让整条链路挂在一个「被调方还没合并」上。

        「真报告」的判据是**报告自己没带 tool_error**，而不是 ``res.status == "ok"``。
        这两者不等价：``test.verify`` 按自己的契约「跑不成也不抛、原样返回报告」
        （见 skills/builtin/test_verify.py 的模块注释），所以沙箱没起来时它照样
        返回 ok，只把原因写进 tool_error。只看 status 就会把这种「根本没跑成」
        当作真报告收下 —— 那就是把「工具没跑成」读成「0 条失败」，Gate 从此判不出
        blocker。tool_error 与 failed 要分开判这条铁律，在这里的形态就是
        「带 tool_error 的报告不算证据，但它必须原样交出去」。

        交出去而不是换一份能过闸的：没有证据就该被 Gate 拦下，这是本 Phase 的题眼。
        """
        tool_error = res.output.get("tool_error") if isinstance(res.output, dict) else None

        if res.status == "ok" and isinstance(res.output, dict) and not tool_error:
            return self._normalize(res.output)

        # 两种「没跑成」的原因在这里合流：skill 压根没注册（res.error），
        # 和 skill 跑了但工具炸了（report 的 tool_error）。后者更具体，优先。
        return make_test_report(
            tool_error=tool_error or res.error or f"{SKILL_VERIFY} 未产出测试报告")

    @staticmethod
    def _normalize(raw: dict) -> dict:
        """把任意来源的报告收敛成 C-7 形状，缺字段补缺省、不猜取值。"""
        return make_test_report(
            passed=raw.get("passed") or 0,
            failed=raw.get("failed") or 0,
            errors=raw.get("errors") or 0,
            cases=raw.get("cases") or (),
            duration=raw.get("duration") or 0.0,
            tool_error=raw.get("tool_error"),
            summary=str(raw.get("summary") or ""),
        )


def seed_scripted_report(store, *, plan_id: str, task_id: str, attempt: int,
                         report: dict) -> None:
    """把一份**前置条件**性质的测试报告挂到某任务的那一次 attempt 上。

    只给**不跑测试的场景**用：场景 3 演审批闸、场景 5 演补偿与重规划，它们的
    DAG 里没有 testing 节点，报告不可能由谁跑出来，是前置条件而不是产物
    （见 ``scenario_3.py:11-13`` 的自述）。

    **软件域两个场景（1 / 2）不许再走这条路**：它们宣称「外部权威判据 = 真实
    pytest 结果」，报告必须是跑出来的 —— 接线见 ``flows/common.py::patch_verifier``。
    判据是「宣称真跑的场景不许有脚本化报告」，不是「全仓不许有」。

    这里直接 ``insert_artifact``、不经 ``on_task_result``，所以预置件**没有来源
    事件** —— ``maos/obs/trace.py`` 据此把它标成 ``provenance="unknown"`` 并计进
    ``summary.unsourced_artifacts``。那是有意的：审计链有洞就要让洞看得见。
    """
    from maos.contracts.events import new_id      # 局部 import：演示脚手架不进模块级依赖

    store.insert_artifact({
        "artifact_id": new_id("art"), "task_id": task_id, "plan_id": plan_id,
        "kind": KIND_TEST_REPORT, "version": attempt, "content": dict(report),
    })
