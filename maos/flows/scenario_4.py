"""场景 4：幂等验证 —— 重复投递同一个 TaskResult，状态不能被改第二次。

这是换 MQ 的前提：publish 只保证「至少一次」，去重责任在下游。
"""

from __future__ import annotations

import json

from maos.contracts import events as E
from maos.contracts.events import Topic, new_id
from maos.flows.common import GOOD_PATCH, build


def run(*, matrix: bool = False) -> int:
    store, bus, cp, model, worker, gate = build({"任务输入": GOOD_PATCH}, matrix=matrix)
    plan_id = cp.create_plan(goal="幂等验证", trace_id=new_id("trace"), tasks=[{
        "role": "coding", "title": "幂等测试任务", "inputs": {}, "acceptance": [],
    }])
    cp.start_plan(plan_id)
    bus.drain()
    task = cp.store.list_tasks(plan_id)[0]
    before = len(cp.store.list_event_log(plan_id))

    dup = E.task_result(plan_id=plan_id, task_id=task["task_id"], attempt=task["attempt"],
                        trace_id=task["trace_id"], status="ok",
                        artifacts=[{"kind": "patch_set", "content": json.loads(GOOD_PATCH)}])
    bus.publish(Topic.TASK_RESULT, dup)
    bus.publish(Topic.TASK_RESULT, dup)   # 故意重投两次
    bus.drain()

    after = len(cp.store.list_event_log(plan_id))
    print(f"\n{'=' * 68}\n场景 4：幂等验证\n{'=' * 68}")
    print(f"重复投递 2 次 TaskResult，新增日志条数 = {after - before}（期望 0）")
    assert after == before, "重复投递导致了额外的状态迁移，幂等失效"
    return 0
