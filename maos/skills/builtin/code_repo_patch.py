"""code.repo-patch —— Coding 角色唯一的补丁产出入口。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B，逐字段）：
  入：{"title": str, "inputs": dict, "acceptance": list[str], "rework_findings": list[dict]}
  出：{"files": [{"path": str, "diff": str}], "summary": str,
       "self_check": {"build": "pass|fail", "lint": "pass|fail"}}
出参形状与 maos/flows/common.py 的 GOOD_PATCH 一致 —— 直接落成 patch_set artifact。

security_boundary 就在本文件的 ``_reject_protected_paths``：
受保护路径的判定只留这一处。放到 Agent 里再抄一份，两处一定会漂，
而漂的那次没人会发现 —— 直到有人靠改测试让测试通过。

self_check 只做透传，不校验取值：判「build/lint 是不是 pass」是 ReviewerGate 的活，
skill 抢着判会让 Gate 永远见不到失败样本（场景 2 的返工链就断了）。
"""

from __future__ import annotations

import json
from typing import Any

from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

# 受保护路径：命中即安全事件。"tests/" 挡的是「改测试让测试通过」。
PROTECTED_PATHS = ("/infra", "/.github", "tests/", "/secrets")

SYSTEM = """你是 Coding Agent。严格按架构契约产出补丁集。
只输出 JSON，不要任何解释文字，格式：
{"files":[{"path":"...","diff":"..."}],"summary":"...","self_check":{"build":"pass|fail","lint":"pass|fail"}}
禁止修改测试文件。禁止触碰 /infra、/.github、/secrets。"""


class ProtectedPathViolation(Exception):
    """补丁触碰受保护路径。安全事件：不重试、不降级，直接终止本次产出。

    invoker 只把异常转成 ``"<类名>: <消息>"`` 字符串，所以类名本身就是跨模块协议 ——
    改名要同步改 ``maos/agents/coding.py`` 的 SECURITY_ERROR_PREFIX。
    """


def _reject_protected_paths(files: list[dict]) -> None:
    violations = [
        f["path"] for f in files
        if any(f["path"].startswith(p) or p in f["path"] for p in PROTECTED_PATHS)
    ]
    if violations:
        raise ProtectedPathViolation(f"触碰受保护路径，已中止: {violations}")


@register_skill
class CodeRepoPatchSkill(Skill):
    contract = SkillContract(
        name="code.repo-patch",
        version="1.0.0",
        purpose="按任务契约产出补丁集，返回前完成受保护路径校验",
        input_schema={
            "title": "str",
            "inputs": "dict",
            "acceptance": "list[str]",
            "rework_findings": "list[dict]",
        },
        output_schema={
            "files": "list[{path:str,diff:str}]",
            "summary": "str",
            "self_check": "{build:'pass|fail', lint:'pass|fail'}",
        },
        preconditions=["title", "inputs", "acceptance"],
        depends_tools=["git-mcp", "sandbox"],
        # 刻意不 retry：重试归 worker 的 attempt 层（max_attempts），
        # skill 层再叠一层会让 attempt 计数失真；安全违规更不该被重试。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "补丁路径白名单：命中 PROTECTED_PATHS 立即抛 ProtectedPathViolation，"
            "不重试、不降级；skill 自身不落盘、不执行补丁"
        ),
        reuse_note="Coding 角色唯一的补丁产出入口；返工走同一入口，findings 从 payload 进",
        owner_roles=["coding"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        if ctx.model is None:
            # 与 req.normalize 不同：补丁没有规则兜底可言，无模型就是接线错了。
            raise RuntimeError("code.repo-patch 需要 ctx.model，调用方必须传 extras={'model': ...}")

        raw = ctx.model.complete(
            system=SYSTEM,
            user=self._build_prompt(payload, ctx),
            tier=ctx.extras.get("tier") or Tier.MEDIUM,
        ).text

        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型输出非合法 JSON: {exc}") from None
        if not isinstance(patch, dict):
            raise ValueError(f"补丁集应为 JSON 对象，实际 {type(patch).__name__}")

        files = patch.get("files") or []
        if not files:
            raise ValueError("补丁集为空")
        bad = [f for f in files if not isinstance(f, dict) or not isinstance(f.get("path"), str)]
        if bad:
            raise ValueError(f"补丁集里有 {len(bad)} 项缺少合法 path 字段")

        _reject_protected_paths(files)          # security_boundary 执行处

        patch.setdefault("summary", "")
        patch.setdefault("self_check", {})
        return patch

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(payload: dict, ctx: SkillContext) -> str:
        """attempt 从 extras 取，不进 payload —— 入参字段以附录 B 为准，不许扩。"""
        parts = [
            f"任务：{payload.get('title', '')}",
            f"任务输入：{json.dumps(payload.get('inputs') or {}, ensure_ascii=False)}",
            f"验收标准：{json.dumps(payload.get('acceptance') or [], ensure_ascii=False)}",
        ]
        findings = payload.get("rework_findings") or []
        attempt = int(ctx.extras.get("attempt") or 1)
        if attempt > 1 and findings:
            # 返工时把结构化 findings 喂回去，而不是让模型重头猜
            parts.append(
                "这是第 %d 次返工，必须逐条解决以下问题：\n%s"
                % (attempt, json.dumps(findings, ensure_ascii=False, indent=2))
            )
        return "\n\n".join(parts)
