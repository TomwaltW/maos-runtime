"""房间自然语言值守的**正式入口** —— 把 T66 / T67 / T68 三轨接成一条线。

    python3 -m hiclaw.room_nl [--plan-id <plan>] [--once] [--timeout <秒>]

参数原样透传给 :func:`hiclaw.room_agent.main`，这里只做一件事：把两个默认的
并行期替身换成正式实现。

## 为什么要单独一个文件，而不是直接改 room_agent

跨轨契约 §1.3 规定 T67 不许 import T66 / T68 的面 —— 并行期它们可能还不存在，
import 即红。`maos/tests/test_room_agent.py::test_no_import_of_other_tracks` 用
ast 断言钉着这条，函数内延迟 import 同样会被它抓到（ast 遍历整棵树）。

所以接线只能从外面做：`room_agent.main()` 留了 `intent_parser` / `dispatcher`
两个注入口，本模块把真实实现塞进去。

## 🔴 换掉 stub 不是形式主义，两者有实差

`room_agent._StubDispatcher` 的权限闸写的是 `if approvers and sender not in ...`
—— **`approvers` 为空时整条检查被跳过**，任何人都能拿到确认卡片。
T68 的 `dispatch_intent` 写的是 `if sender not in frozenset(approvers or ())`
—— 空名单一律拒绝。

配置缺失时放行是最经典的权限漏洞形态，且它在测试里长得像「默认行为」。
`MAOS_APPROVERS` 没配的机器上，跑 `hiclaw.room_agent` 与跑 `hiclaw.room_nl`
是两种安全姿态。**真房间只跑本模块。**

回归守卫见 `maos/tests/test_room_nl_wiring.py`。
"""

from __future__ import annotations

import sys
from functools import partial

from hiclaw.room_agent import main as _agent_main
from maos.model.client import select_model_client
from maos.nlu.intent import parse_intent
from maos.runtime.intent_dispatch import dispatch_intent


def build_parser(model=None):
    """把 `parse_intent` 补成 `IntentParser` 的形状（契约 §1.3）。

    `parse_intent(text, *, model, known_task_ids)` 偏应用掉 `model` 之后，
    剩下的签名正好是 `(text, *, known_task_ids)`。

    `model` 缺省走 `select_model_client()`：没配 `MAOS_LLM_*` 的机器上它降级回
    `ScriptedModelClient`，`parse_intent` 那边有关键词兜底，**不会瘫也不会打网络**。
    """
    return partial(parse_intent, model=model if model is not None
                   else select_model_client())


def main(argv: list[str] | None = None) -> int:
    return _agent_main(argv, intent_parser=build_parser(),
                       dispatcher=dispatch_intent)


if __name__ == "__main__":
    sys.exit(main())
