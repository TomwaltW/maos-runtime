"""场景 1：正常闭环 —— PENDING -> ... -> DONE。

跑完看 event_log：每一次状态迁移都有一条记录，这就是后面 Trace 的数据来源。
"""

from __future__ import annotations

from maos.agents.manager import ManagerAgent
from maos.contracts.events import new_id
from maos.contracts.states import PlanState
from maos.flows.common import GOOD_PATCH, PLAN_JSON, build, dump, run_until_settled
from maos.model.client import select_model_client


def run(*, matrix: bool = False) -> int:
    # 真模型接线点（ORCHESTRATION.md:88 验收「有 key 场景 1 真模型通」唯一指定处）。
    # 走 C-3 明文允许的 model= 注入口，build() 一行不改：无 key 时
    # select_model_client 降级返回 ScriptedModelClient(script)，行为与接线前等价。
    script = {"用户请求": PLAN_JSON, "任务输入": GOOD_PATCH}
    store, bus, cp, model, worker, gate = build(
        script, matrix=matrix, model=select_model_client(script))
    mgr = ManagerAgent(model)
    trace = new_id("trace")
    plan_id = cp.create_plan(goal="修复 demo/app 的 token 校验缺失",
                            trace_id=trace, tasks=mgr.plan("修复 token 校验缺失"))
    cp.start_plan(plan_id)
    run_until_settled(bus, gate, cp, plan_id)
    dump(cp, plan_id, "场景 1：正常闭环")
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE
    return 0
