"""薄入口 —— `python run.py` 顺跑场景 1-7 端到端，等价于 `python -m maos.main`。

多一个开关：`python run.py --contrast` 跑三组对照 case（租户 / 渠道 / 政策版本），
实现在 `maos/flows/contrast.py`。

**它为什么挂在这里而不是 `maos/main.py`**：`main.py` 在 Task-0 完工后冻结（附录 D），
且它的 `--scenario` 选项域是 `ALL_SCENARIOS`，而三组对照**刻意不进那个集合** ——
缺省证据束恒为 8 束是跨轨冻结口径（`scripts/demo_preflight.sh` 与复赛材料都写死了 8）。
对照是**另开一条路**，不是第 8、9、10 个场景。所以本文件先把 `--contrast` 摘掉，
其余参数原样透传给 `maos.main`，`python run.py` 不带参数时行为一个字节不变。
"""

from __future__ import annotations

import sys

from maos.main import main as scenarios_main

CONTRAST_FLAG = "--contrast"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if CONTRAST_FLAG not in args:
        return scenarios_main(args)

    args.remove(CONTRAST_FLAG)
    matrix = "--matrix" in args
    if matrix:
        args.remove("--matrix")
    if args:
        print(f"{CONTRAST_FLAG} 不接受其它参数（多余的：{args}）", file=sys.stderr)
        return 2

    from maos.flows.contrast import run as run_contrast
    return run_contrast(matrix=matrix)


if __name__ == "__main__":
    sys.exit(main())
