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
把它当成真报告收下，脚本化兜底就永远够不着（Task-B 合并当天场景 1/2 即因此转红，
详见 ``_report_from``）。

    tool_error 与 failed 是两回事 —— 前者是工具根本没跑成，后者是用例真的挂了。
    Gate 对这两种的判定不一样，所以这里绝不能把「没跑成」压成「0 条失败」。

``scripted_report`` 是 Scripted 演示模式的报告源，与 ``ScriptedModelClient``
同构：没有备好沙箱 workdir 的机器上，补丁是脚本化的（common.py 的 GOOD_PATCH），
报告也只能是脚本化的，否则四场景一条都跑不起来。等演示链路真接上沙箱
（BACKLOG ## task-C「演示链路仍未真连沙箱」一条），报告不再带 tool_error，
这条分支自然让位给真报告 —— 不必删，它是降级路径。
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

        report = self._report_from(res, ctx)
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
    def _report_from(self, res, ctx: TaskContext) -> dict:
        """真报告 > 脚本化报告 > tool_error 报告。三级都不抛。

        不抛是硬要求：``test.verify`` 未注册在并行期是常态（A-5），
        抛出去会让整条链路挂在一个「被调方还没合并」上。

        「真报告」的判据是**报告自己没带 tool_error**，而不是 ``res.status == "ok"``。
        这两者不等价：``test.verify`` 按自己的契约「跑不成也不抛、原样返回报告」
        （见 skills/builtin/test_verify.py 的模块注释），所以沙箱没起来时它照样
        返回 ok，只把原因写进 tool_error。只看 status 就会把这种「根本没跑成」
        当作真报告收下，脚本化兜底从此**够不着** —— Task-B 合并当天场景 1/2 正是
        这样红的：workdir 无人准备 -> tool_error -> Gate 判 blocker，而 inputs 里
        预置的 PASS_REPORT 一直没人取。tool_error 与 failed 要分开判这条铁律，
        在这里的形态就是「带 tool_error 的报告不算证据」。
        """
        tool_error = res.output.get("tool_error") if isinstance(res.output, dict) else None

        if res.status == "ok" and isinstance(res.output, dict) and not tool_error:
            return self._normalize(res.output)

        scripted = ctx.inputs.get("scripted_report")
        if isinstance(scripted, dict):
            return self._normalize(scripted)

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
    """Scripted 演示模式：把一份脚本化测试报告挂到被验任务的那一次 attempt 上。

    这是 ``scripted_report`` 的同胞。那条走 Testing Agent 的软兜底；这条给
    **产出补丁的那个任务**用 —— DAG 里 testing 依赖 coding，coding 过闸时
    testing 还没跑，报告不可能已经存在，所以演示期由场景预置。

    与 common.py 的 GOOD_PATCH / BAD_PATCH 同性质：无沙箱的机器上补丁是脚本化的，
    报告也只能是脚本化的。Task-B 的沙箱合并后这里换成 Testing Agent 真跑的产物，
    Gate 一行不改 —— 判据读的始终是 test_report，不关心谁产的。
    """
    from maos.contracts.events import new_id      # 局部 import：演示脚手架不进模块级依赖

    store.insert_artifact({
        "artifact_id": new_id("art"), "task_id": task_id, "plan_id": plan_id,
        "kind": KIND_TEST_REPORT, "version": attempt, "content": dict(report),
    })
