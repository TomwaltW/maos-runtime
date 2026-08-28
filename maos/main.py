"""端到端跑通入口 —— 只做参数解析与分发，场景实现在 maos/flows/scenario_<N>.py。

  场景 1：正常闭环      PENDING -> ... -> DONE
  场景 2：返工闭环      Gate 判 rework -> 带 findings 重跑 -> DONE
  场景 3：高风险审批    Gate 过了也停在 BLOCKED，等人工放行
  场景 4：幂等验证      重复投递不产生第二次状态迁移
  场景 5：占位          补偿闭环，未实现（退出码 1）

无参 = 顺跑 1-4。跑完看 event_log：每一次状态迁移都有一条记录，这是后面 Trace 的数据来源。

本文件在 Task-0 完工后**冻结**（附录 D）：新增或修改场景请动 flows/，不要动这里。
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)-12s %(message)s")
log = logging.getLogger("maos.main")

ALL_SCENARIOS = (1, 2, 3, 4, 5, 6, 7)   # D-05：退款域用整数 6=顺利路径 / 7=失败路径
DEFAULT_SCENARIOS = (1, 2, 3, 4)      # 场景 5 未实现，不进缺省序列


def _run_scenario(n: int, *, matrix: bool) -> int:
    mod = importlib.import_module(f"maos.flows.scenario_{n}")
    return mod.run(matrix=matrix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maos", description="MAOS 端到端场景入口（无参顺跑场景 1-4）")
    parser.add_argument("--scenario", type=int, choices=ALL_SCENARIOS, default=None,
                        help="只跑指定场景；缺省顺跑 1-4")
    parser.add_argument("--matrix", action="store_true",
                        help="事件总线经 HiClaw(Matrix) 转发；未接通时自动降级为进程内总线")
    args = parser.parse_args(argv)

    logging.getLogger("maos.bus").setLevel(logging.WARNING)

    if args.scenario is not None:
        return _run_scenario(args.scenario, matrix=args.matrix)

    for n in DEFAULT_SCENARIOS:
        rc = _run_scenario(n, matrix=args.matrix)
        if rc != 0:
            return rc
    print("\n全部场景通过：事件契约与状态机在真实链路上成立，可以进入并行分轨。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
