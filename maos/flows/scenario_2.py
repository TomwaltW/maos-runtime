"""场景 2：返工闭环 —— 第一次自检失败 + 无变更说明 -> Gate 判 rework -> 第二次带 findings 修好。"""

from __future__ import annotations

from maos.agents.manager import ManagerAgent
from maos.contracts.events import new_id
from maos.contracts.states import TaskState
from maos.flows.common import BAD_PATCH, GOOD_PATCH, PLAN_JSON, build, dump, run_until_settled
from maos.model.client import ModelResponse, ScriptedModelClient


def run(*, matrix: bool = False) -> int:
    class FlakyModel(ScriptedModelClient):
        """第一次产坏补丁、返工后产好补丁 —— 按 **prompt 内容** 分派，不按调用序数。

        判据是 code_repo_patch._build_prompt 只在 ``attempt > 1 且有 findings``
        时才写入的「返工」二字。按序数计数（原写法）对新增的 model 调用不设防：
        coding.py 在补丁 skill 之前还会调 kb.retrieve，该 skill 由 Task-D 落地、
        且能从 extras 拿到 model；它一旦也走模型，序数就整体错位一格，
        症状是场景 2 的 attempt 断言失败，而原因离断言很远。
        """

        def complete(self, *, system, user, tier):
            if "用户请求" in user:
                return super().complete(system=system, user=user, tier=tier)
            return ModelResponse(text=GOOD_PATCH if "返工" in user else BAD_PATCH)

    # 注入式构造：走 build() 这一条路，不再手工拼装六件套（C-3）
    script = {"用户请求": PLAN_JSON}
    store, bus, cp, model, worker, gate = build(script, matrix=matrix, model=FlakyModel(script))

    mgr = ManagerAgent(model)
    plan_id = cp.create_plan(goal="返工路径验证", trace_id=new_id("trace"),
                            tasks=mgr.plan("修复 token 校验缺失"))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    dump(cp, plan_id, "场景 2：返工闭环（第 1 次自检失败，第 2 次修好）")
    task = cp.store.list_tasks(plan_id)[0]
    assert task["state"] == TaskState.DONE and task["attempt"] == 2
    return 0
