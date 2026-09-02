#!/usr/bin/env python3
"""自然语言接入面的契约级冒烟 —— 一句人话 → Intent → DispatchResult。

三件事，按这个顺序：

1. 三个模块（意图解析层 / 房间常驻监听器 / 意图派发与权限闸）能不能 import。
   缺任意一个就打印**缺的是谁**，退出码 **3**。
2. 都在的话，跑一遍进程内端到端：起一个高风险任务拿到真实 task_id，
   把「同意，把 <task_id> 批了吧」喂进解析层，再把解出的 Intent 喂进派发层，
   每一步都打出来。
3. 顺手自证红线 R1：自然语言解出的 approve **只许产出确认卡片**，
   不许直接生效。破了就退 1。

**退出码 3 而不是 1** 是有意的：「还没合并」与「合并了但坏了」必须在 CI 与人眼里
分得开。一个把两者混成 1 的脚本，会让整合期每天都在看假红。

**不打网络、不连房间**：模型走 ``force_scripted=True``，总线走进程内
（``build(..., matrix=False)``）。这份冒烟在没有任何 key、没有 Synapse 的
裸机器上必须能跑。
"""

from __future__ import annotations

import importlib
import os
import sys

#: 从 scripts/ 直接跑时 sys.path[0] 是 scripts/，仓库根不在路径上 —— 不补这一句，
#: 三个模块**合并之后**照样报「没合并」，这份冒烟就成了永远为 3 的摆设。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 三个模块任一缺席时的退出码。见模块头「退出码 3 而不是 1」。
EXIT_NOT_MERGED = 3

#: (模块名, 职责)。职责是打给人看的 —— 缺席时人读到的是「谁没到」，不是一行 traceback。
REQUIRED: tuple[tuple[str, str], ...] = (
    ("maos.nlu.intent", "意图解析层（一句人话 → Intent）"),
    ("hiclaw.room_agent", "房间常驻监听器（只收发与路由，不含理解逻辑）"),
    ("maos.runtime.intent_dispatch", "意图派发与权限闸（Intent → 动作）"),
)

SENDER = "@boss:maos.local"


def probe() -> tuple[dict, list[tuple[str, str, Exception]]]:
    """import 三个模块，返回 (已到的, 缺席的)。缺席不抛，交给调用方决定退出码。"""
    present: dict[str, object] = {}
    missing: list[tuple[str, str, Exception]] = []
    for name, duty in REQUIRED:
        try:
            present[name] = importlib.import_module(name)
        except ImportError as exc:
            missing.append((name, duty, exc))
    return present, missing


def seed_known_task():
    """进程内起一个 effect_risk=H 的任务，返回 (store, cp, task_id)。

    要的是一个**真实存在于库里**的 task_id —— 红线 R2 要求解析出的 task_id 必须
    命中 known_task_ids，拿一个编的 id 来冒烟等于没冒。

    ``matrix=False``：总线走进程内，一行网络都不打，也不碰房间。
    """
    from maos.contracts.events import new_id
    from maos.flows.common import build

    store, _bus, cp, _model, _worker, _gate = build({}, matrix=False)
    plan_id = cp.create_plan(goal="自然语言接入面冒烟", trace_id=new_id("trace"), tasks=[{
        "role": "coding",
        "title": "变更生产环境配置",
        "inputs": {"repo": "demo/app"},
        "acceptance": ["build 通过"],
        "risk_level": "M",     # 产出补丁在 Agent 授权内
        "effect_risk": "H",    # 但合进生产必须人工放行
    }])
    task_id = cp.store.list_tasks(plan_id)[0]["task_id"]
    return store, cp, task_id


def end_to_end(present: dict) -> int:
    """三个模块都在时跑的那条路径。返回退出码。"""
    from maos.model.client import select_model_client

    nlu = present["maos.nlu.intent"]
    dispatch = present["maos.runtime.intent_dispatch"]

    store, cp, task_id = seed_known_task()
    text = f"同意，把 {task_id} 批了吧"
    model = select_model_client({}, force_scripted=True)   # 一行网络都不走

    print(f"[1/3] 人说的话        {text}")
    intent = nlu.parse_intent(text, model=model, known_task_ids=[task_id])
    print(f"[2/3] 解析出的 Intent  action={intent.action} task_id={intent.task_id or '(空)'} "
          f"confidence={intent.confidence}")

    result = dispatch.dispatch_intent(intent, store=store, cp=cp,
                                      sender=SENDER, approvers=[SENDER])
    print(f"[3/3] 派发结果        kind={result.kind} task_id={result.task_id or '(空)'}")
    print(f"      回房间的话      {result.text}")

    if intent.action == nlu.ACTION_APPROVE and result.kind != dispatch.KIND_CONFIRM:
        print(f"[红线 R1 被破] approve 意图派发出 kind={result.kind}，"
              f"应当只产出 {dispatch.KIND_CONFIRM} —— 自然语言不许直接授权。",
              file=sys.stderr)
        return 1

    print("[R1 自证] 自然语言不授权：approve 意图只产出确认卡片，"
          "真正生效仍要人再发一次显式指令。")
    return 0


def main() -> int:
    present, missing = probe()
    if missing:
        print("自然语言接入面尚未合并齐，本次只做存在性冒烟：")
        for name, duty, exc in missing:
            print(f"  - {duty} 尚未合并（{name}：{exc}）")
        print(f"\n{len(missing)}/{len(REQUIRED)} 个模块缺席，退出码 {EXIT_NOT_MERGED}（不是 1）：")
        print("  「还没合并」与「合并了但坏了」必须分得开，否则整合期天天看假红。")
        return EXIT_NOT_MERGED
    return end_to_end(present)


if __name__ == "__main__":
    raise SystemExit(main())
