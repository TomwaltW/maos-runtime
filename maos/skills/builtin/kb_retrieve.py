"""kb.retrieve —— 按 tags / 关键词检索沉淀过的经验。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B B-6，逐字段）：
  入：{"tags"?: list[str], "keyword"?: str, "limit"?: int}
  出：{"items": list[knowledge 行], "count": int}

两条**必须**成立的性质，否则它一落地就会把别人的链路搞坏：

  · **空结果不阻塞**（phase-4.md:14）。检索不到经验是系统冷启动时的常态，
    不是故障。返回空清单，不抛。
  · **零模型**。``maos/agents/coding.py:58`` 在产补丁**之前**就调它，而
    ``flows/scenario_2.py`` 的 FlakyModel 按 prompt 内容分派 —— 该文件的
    docstring 已经点名：这个 skill 一旦也走模型，场景 2 的调用序就整体错位，
    症状是 attempt 断言失败，而原因离断言很远。这里一行模型调用都不能有。

``ctx.store is None`` 同样返回空而不抛：Agent 可能用 ``cls(model)`` 老写法构造，
此时 invoker 拿不到 store。检索是锦上添花，不该因为调用方没接线就中断主链路。
"""

from __future__ import annotations

from typing import Any

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

DEFAULT_LIMIT = 5


def _limit_of(payload: dict) -> int | None:
    """limit 缺省 5；显式 0 或负数表示不限量。非数字一律回退缺省，不抛。"""
    if "limit" not in payload or payload["limit"] is None:
        return DEFAULT_LIMIT
    try:
        value = int(payload["limit"])
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return None if value <= 0 else value


@register_skill
class KbRetrieveSkill(Skill):
    contract = SkillContract(
        name="kb.retrieve",
        version="1.0.0",
        purpose="按标签或关键词检索沉淀过的经验，供 Agent 执行前带入上下文",
        input_schema={"tags": "list[str]?", "keyword": "str?", "limit": "int?"},
        output_schema={"items": "list[knowledge 行]", "count": "int"},
        # 两个筛选条件都可缺省（= 取全部），所以没有 precondition。
        preconditions=[],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只读 knowledge 表，经 store.list_knowledge；不写任何资源、不调模型、不落盘",
        reuse_note="Coding 与 Manager 的执行前检索入口（两者白名单已含本 skill）；空结果不阻塞",
        owner_roles=["manager", "coding"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        if ctx.store is None:
            return {"items": [], "count": 0}

        tags = payload.get("tags") or None
        if tags is not None and not isinstance(tags, (list, tuple)):
            tags = [tags]
        if tags is not None:
            tags = [s for s in (str(t).strip() for t in tags) if s] or None

        keyword = payload.get("keyword")
        keyword = " ".join(str(keyword).split()) if keyword else None

        items = ctx.store.list_knowledge(tags=tags, keyword=keyword)
        limit = _limit_of(payload)
        if limit is not None:
            items = items[:limit]
        return {"items": items, "count": len(items)}
