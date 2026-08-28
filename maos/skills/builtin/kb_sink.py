"""kb.sink —— 把一条复盘结论写进 knowledge 表。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B B-5，逐字段）：
  入：{"plan_id": str, "kind": "rule"|"case", "title": str, "body": str, "tags": list[str]}
  出：{"knowledge_id": str}

存取一律走 ``ctx.store.insert_knowledge(row)``（A-8 已实现），本文件不写一行 SQL ——
知识表的读写口径只留 store 一处，skill 里再写一份，换 PolarDB 时就得改两处。

``kind`` 取值域恒为 rule|case，越界**抛**不降级：写错 kind 的条目查得出来但归不了类，
而错误发生在写入侧、暴露在几周后的检索侧，是最难回溯的一类脏数据。
"""

from __future__ import annotations

from typing import Any

from maos.contracts.events import new_id
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

KIND_RULE = "rule"
KIND_CASE = "case"
VALID_KINDS = (KIND_RULE, KIND_CASE)


@register_skill
class KbSinkSkill(Skill):
    contract = SkillContract(
        name="kb.sink",
        version="1.0.0",
        purpose="把复盘结论作为一条 rule 或 case 沉淀进知识库",
        input_schema={
            "plan_id": "str",
            "kind": "rule|case",
            "title": "str",
            "body": "str",
            "tags": "list[str]",
        },
        output_schema={"knowledge_id": "str"},
        preconditions=["plan_id", "kind", "title", "body"],
        depends_tools=[],
        # 写库失败多半是 store 没接线或 schema 不匹配，重试无益，直接上报。
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只写 knowledge 表一张，经 store.insert_knowledge；不碰其余五表、不落盘、不调模型",
        reuse_note="Plan 复盘的唯一写入口；任何角色要沉淀经验都走它，不要各自拼 INSERT",
        owner_roles=["manager"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        if ctx.store is None:
            # 与 kb.retrieve 的不对称是刻意的：检索取不到可以返回空继续跑，
            # 写入取不到 store 就是**这条经验丢了**，静默丢比失败更糟。
            raise RuntimeError("kb.sink 需要 ctx.store，调用方必须用 SkillInvoker(identity, store)")

        kind = str(payload.get("kind") or "")
        if kind not in VALID_KINDS:
            raise ValueError(f"kb.sink 的 kind 必须是 {VALID_KINDS} 之一，实际 {kind!r}")

        tags = payload.get("tags") or []
        if not isinstance(tags, (list, tuple)):
            tags = [tags]
        tags = [s for s in (str(t).strip() for t in tags) if s]

        knowledge_id = new_id("kn")
        ctx.store.insert_knowledge({
            "id": knowledge_id,
            "plan_id": str(payload["plan_id"]),
            "kind": kind,
            "title": " ".join(str(payload["title"]).split()),
            "body": str(payload["body"]),
            "tags": tags,
        })
        return {"knowledge_id": knowledge_id}
