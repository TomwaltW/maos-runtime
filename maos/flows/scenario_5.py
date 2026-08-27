"""场景 5：占位 —— 补偿闭环（Task-D 落地）。

Task-0 只留骨架：打印「未实现」并以退出码 1 返回，
这样 `run.py --scenario 5` 在场景补全前是**可判定的失败**，而不是假装通过。
"""

from __future__ import annotations


def run(*, matrix: bool = False) -> int:
    print("场景 5：未实现（补偿闭环由 Task-D 落地）")
    return 1
