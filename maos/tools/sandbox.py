"""沙箱工具层 —— 签名冻结桩，实现归 Phase 2（Task-B）。

这个文件现在只有两个函数签名，没有实现。它存在的唯一理由是：
Gate 的补偿干跑闸与补偿执行器都要 import 这两个函数，而它们的实现要等容器沙箱就位。
没有这个桩，上层只能各写各的桩，合并时必然互相覆盖。

签名一旦定下就不许改（上层按签名写代码，实现方按签名填肉）：
  · sandbox_git_apply  —— 对应 phase-2.md 第 3 步的 sandbox.git_apply
      reverse=True                    -> git apply -R        （Phase 4 补偿回滚）
      reverse=True, check_only=True   -> git apply -R --check（Phase 4 补偿干跑闸）
  · sandbox_pytest_run —— 对应 phase-2.md 第 3 步的 sandbox.pytest_run

实现要求见 docs/phases/phase-2.md 第 3 步，不要在这里提前实现：
容器主路径、降级路径的 env 白名单、conftest.py 禁改、结构化错误，一条都不能少。
"""

from __future__ import annotations

from typing import Any


def sandbox_git_apply(
    patch_set: dict[str, Any],
    workdir: str,
    *,
    reverse: bool = False,
    check_only: bool = False,
) -> dict[str, Any]:
    """在沙箱工作目录应用补丁集。

    reverse=True 走 git apply -R（补偿回滚）；check_only=True 加 --check（只干跑不落盘）。
    两者同时为 True 就是 phase-4.md 第 3 步那道补偿干跑闸。

    返回 {"ok": bool, "error": {"stage", "path", "hunk", "message"} | None}。
    ok=False 时 error 必须结构化 —— Gate 要把 path 和 hunk 逐条转成 findings 喂回 Coding。
    """
    raise NotImplementedError("Phase 2（Task-B）实现：见 docs/phases/phase-2.md 第 3 步")


def sandbox_pytest_run(workdir: str) -> dict[str, Any]:
    """在沙箱里跑 pytest，产出结构化测试报告。

    返回 test_report：
      {"passed": int, "failed": int, "errors": int,
       "cases": [{"id": str, "status": str, "msg": str}],
       "duration": float, "tool_error": str | None}

    tool_error 与 failed 必须分开上报：前者是环境或工具炸了（根本没跑成），
    后者是用例真的挂了（跑成了但不过）。Gate 对这两种的判定不一样。
    """
    raise NotImplementedError("Phase 2（Task-B）实现：见 docs/phases/phase-2.md 第 3 步")
