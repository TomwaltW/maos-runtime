"""test.verify —— Testing 角色唯一的测试执行入口。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B-3，逐字段）：
  入：{"workdir": str}
  出：test_report（= C-7 的 sandbox_pytest_run 返回）：
      {"passed": int, "failed": int, "errors": int,
       "cases": [{"id": str, "status": str, "msg": str}],
       "duration": float, "tool_error": str | None}
出参形状直接落成 test_report artifact，`maos/artifacts.py::validate_artifact` 按此校验。

**用例挂了不抛异常**：`failed > 0` 是这个 skill 的正常返回，不是它失败。
抛出去会被 invoker 兜成 `SkillResult(status="failed")`，于是「测试跑完、挂了三条」
和「沙箱根本没起来」在上层看起来一模一样 —— 而 Gate 对这两种的判定完全不同
（前者逐条转 findings 喂回 Coding，后者是 blocker）。这个区分靠报告里的
tool_error 字段传递，不靠异常。
"""

from __future__ import annotations

from typing import Any

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill
from maos.tools.port import invoke_tool
from maos.tools.sandbox import PYTEST_RUN_PORT


@register_skill
class TestVerifySkill(Skill):
    contract = SkillContract(
        name="test.verify",
        version="1.0.0",
        purpose="在容器沙箱里跑 workdir 的测试，产出结构化 test_report",
        input_schema={"workdir": "str"},
        output_schema={
            "passed": "int",
            "failed": "int",
            "errors": "int",
            "cases": "list[{id:str,status:str,msg:str}]",
            "duration": "float",
            "tool_error": "str | None",
        },
        preconditions=["workdir"],
        depends_tools=["sandbox"],
        # 与 code.repo-patch 同口径：不在 skill 层重试。重试归 worker 的 attempt
        # 层（max_attempts），这里再叠一层会让 attempt 计数失真；而沙箱起不来
        # 这类环境错重试多半也是白试，该升级就升级。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "不自己执行任何东西，一律经 sandbox.pytest_run 这个 ToolPort："
            "主路径容器（断网、只读、非 root、内存/CPU/进程数限额、不继承宿主 env），"
            "降级路径裸 subprocess 但 env 按白名单重建。skill 自身不落盘、不改 workdir"
        ),
        reuse_note="Testing 角色唯一的测试执行入口；Gate 判代码类任务读的就是它的产物",
        owner_roles=["testing"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        workdir = payload.get("workdir")
        if not isinstance(workdir, str) or not workdir:
            raise ValueError("test.verify 需要 payload['workdir']（非空字符串）")

        # 走 invoke_tool 而不是直接调 sandbox_pytest_run：直接调就没有 ToolInvoked
        # 审计行，出事之后查不到是谁、什么参数、跑了多久。
        return invoke_tool(PYTEST_RUN_PORT, {"workdir": workdir},
                           store=ctx.store, extras=ctx.extras)
