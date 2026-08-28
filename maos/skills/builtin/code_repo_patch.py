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

self_check 只收敛类型、不校验取值：判「build/lint 是不是 pass」是 ReviewerGate 的活，
skill 抢着判会让 Gate 永远见不到失败样本（场景 2 的返工链就断了）。
但类型必须收敛 —— 非 dict 的 self_check 传下去会让 Gate 崩在 .get 上，
那不叫「留给 Gate 判」，那叫让 Gate 没机会判。
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

from maos.model.client import Tier
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

# 受保护目录名：路径按 / 分段后任一段命中即安全事件。"tests" 挡的是「改测试让测试通过」。
#
# 存的是**目录名**，不是路径前缀 —— 这是本清单唯一容易写错的地方。上一版存前缀
# ("/infra", "/.github", "tests/", "/secrets") 配 startswith / 子串判定，结果是
# 声明拦的四项里只有 tests/ 真生效：仓库相对路径 "infra/main.tf" 不带前导斜杠，
# startswith("/infra") 恒 False，"/infra" 也不是它的子串。同时子串判定又把
# infrastructure、contests 这类正常目录误伤成安全事件。
# 分段相等把漏拦和误伤一起消掉，代价就是这里必须写裸目录名，不带任何斜杠。
PROTECTED_SEGMENTS = frozenset({"infra", ".github", "secrets", "tests"})

SYSTEM = """你是 Coding Agent。严格按架构契约产出补丁集。
只输出 JSON，不要任何解释文字，格式：
{"files":[{"path":"...","diff":"..."}],"summary":"...","self_check":{"build":"pass|fail","lint":"pass|fail"}}
禁止触碰任意层级下名为 infra、.github、secrets、tests 的目录 —— 尤其不许改测试让测试通过。"""


class ProtectedPathViolation(Exception):
    """补丁触碰受保护路径。安全事件：不重试、不降级，直接终止本次产出。

    invoker 只把异常转成 ``"<类名>: <消息>"`` 字符串，所以类名本身就是跨模块协议 ——
    改名要同步改 ``maos/agents/coding.py`` 的 SECURITY_ERROR_PREFIX。
    """


def _path_segments(path: str) -> list[str]:
    """把补丁路径规范化成小写分段，供分段相等匹配。

    归一四件事，每一件不做就是一个绕过口：
      * 反斜杠 → 斜杠：``.github\\workflows\\ci.yml`` 否则整条是一个段，判不出来；
      * 折叠 ``.`` / ``..`` / 重复斜杠：``./infra/x`` 与 ``maos/../infra/x``
        必须和 ``infra/x`` 判成同一个；
      * 剥前导斜杠：声明里写的就是 ``/infra``，模型照抄一遍不该反而放行；
      * casefold：本机 APFS 默认大小写不敏感，``Secrets/prod.env`` 与
        ``secrets/prod.env`` 在磁盘上是同一个文件，判定却会放行前者。

    normpath 消不掉开头的 ``..``（``../infra/x`` 原样返回），所以残留的
    ``..`` 段在这里一并滤掉 —— 留着它只会让越界路径躲开分段匹配。
    """
    collapsed = posixpath.normpath(path.replace("\\", "/"))
    return [seg.casefold() for seg in collapsed.split("/") if seg not in ("", ".", "..")]


def _reject_protected_paths(files: list[dict]) -> None:
    violations = [
        f["path"] for f in files
        if PROTECTED_SEGMENTS.intersection(_path_segments(f["path"]))
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
            "受保护路径判定：补丁路径规范化后按 / 分段，任一段命中 PROTECTED_SEGMENTS"
            "（infra / .github / secrets / tests，任意层级、大小写不敏感）"
            "立即抛 ProtectedPathViolation，不重试、不降级；skill 自身不落盘、不执行补丁"
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
        # diff 与 path 同等必校：output_schema 声明的是 {path:str,diff:str}，
        # 而代价落在零模型补偿链 —— artifacts.py 反向打补丁时拿不到 diff，
        # 补偿会「成功」地什么都没还原，是静默失败，不是报错。
        bad = [
            f for f in files
            if not isinstance(f, dict)
            or not isinstance(f.get("path"), str)
            or not isinstance(f.get("diff"), str)
        ]
        if bad:
            raise ValueError(f"补丁集里有 {len(bad)} 项缺少合法 path/diff 字段")

        _reject_protected_paths(files)          # security_boundary 执行处

        # 显式类型收敛，不是 setdefault。setdefault 只在键缺失时填缺省，键在则
        # 原样保留 —— 于是 self_check: null 和 self_check: "pass" 会照原样穿透到
        # Gate，在 gate.py 的 check.get() 上抛 AttributeError。那里是裸调用，
        # 异常逃出后整个 plan 驱动循环当场崩，连一次返工都退化不出来。
        # 收敛的是**类型**不是取值：build/lint 判 pass 还是 fail 是 ReviewerGate
        # 的活，skill 抢着判会让 Gate 永远见不到失败样本（场景 2 的返工链就断了）。
        if not isinstance(patch.get("self_check"), dict):
            patch["self_check"] = {}
        if not isinstance(patch.get("summary"), str):
            patch["summary"] = ""
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
