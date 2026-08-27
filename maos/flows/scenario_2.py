"""场景 2：返工闭环 —— 第一次自检失败 + 无变更说明 -> Gate 判 rework -> 第二次带 findings 修好。"""

from __future__ import annotations

from maos.agents.manager import ManagerAgent
from maos.contracts.events import new_id
from maos.contracts.states import TaskState
from maos.flows.common import BAD_PATCH, GOOD_PATCH, PLAN_JSON, build, dump, run_until_settled
from maos.model.client import ModelResponse, ScriptedModelClient


def run(*, matrix: bool = False) -> int:
    calls = {"n": 0}

    class FlakyModel(ScriptedModelClient):
        def complete(self, *, system, user, tier):
            if "用户请求" in user:
                return super().complete(system=system, user=user, tier=tier)
            calls["n"] += 1
            return ModelResponse(text=BAD_PATCH if calls["n"] == 1 else GOOD_PATCH)

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
