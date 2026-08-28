"""kb.retrieve —— 检索沉淀过的经验。两条通路，一个出口。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B B-6 + Phase 5 扩展）：
  入：{"tags"?, "keyword"?, "limit"?,                       # 通路一：knowledge 表
       "tenant_id"?, "biz_type"?, "channel_id"?, "region"?, # 通路二：kb_doc 两阶段检索
       "sku"?, "policy_version"?, "workflow_version"?,
       "rule_no"?, "gateway_code"?}
  出：{"items": list[knowledge 行], "count": int,          # 恒有，语义与 1.0.0 一致
       "docs": list[命中文档], "doc_count": int}          # 仅当入参带 tenant_id 时出现

**两条通路刻意都留着**，不是过渡态：

  · `items` 走 `knowledge` 表（复盘沉淀，PlanFinalizer 写入），按 tags / keyword 取；
  · `docs` 走 `kb_doc` 表（结构化知识层），走两阶段检索 —— 阶段一按租户等七个维度
    做**硬过滤**，阶段二四通道加权融合。**没有 tenant_id 就没有 docs**：
    跨租户的知识永远不能被召回，这条是硬约束不是打分项。

老调用方（Coding Agent 只读 `kb.output`）零改动继续可用，`items` 的语义一个字没变。

三条**必须**成立的性质，否则它一落地就会把别人的链路搞坏：

  · **零模型**。``maos/agents/coding.py`` 在产补丁**之前**就调它，而
    ``flows/scenario_2.py`` 的 FlakyModel 按 prompt 内容分派 —— 这里多一次模型调用，
    场景 2 的调用序整体错位，症状是 attempt 断言失败，而原因离断言很远。
    语义通道用的是确定性 hash embedding（见 retriever.embed），不是模型向量。
  · **空结果不阻塞**。检索不到经验是系统冷启动时的常态，不是故障。返回空清单，不抛。
  · **``ctx.store is None`` 返回空而不抛**：Agent 可能用 ``cls(model)`` 老写法构造，
    此时 invoker 拿不到 store。检索是锦上添花，不该因为调用方没接线就中断主链路。

`MAOS_KB_ENABLED=0` 时两条通路都返回空、且**一条 KbRetrieved 事件都不落** ——
这是 RAG 有无对照实验（R5）的唯一变量，关掉就该干净地什么都没有。
"""

from __future__ import annotations

import logging
from typing import Any

from maos import kb
from maos.kb import retriever
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

log = logging.getLogger("maos.kb")

DEFAULT_LIMIT = 5

#: 阶段一的过滤维度 + 阶段二的两个精确通道，从 payload 里原样取。
_QUERY_FIELDS = (*retriever.PREFILTER_FIELDS, "rule_no", "gateway_code")


def _limit_of(payload: dict) -> int | None:
    """limit 缺省 5；显式 0 或负数表示不限量。非数字一律回退缺省，不抛。"""
    if "limit" not in payload or payload["limit"] is None:
        return DEFAULT_LIMIT
    try:
        value = int(payload["limit"])
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return None if value <= 0 else value


def _build_query(payload: dict) -> dict:
    query = {f: payload.get(f) for f in _QUERY_FIELDS if payload.get(f) not in (None, "")}
    keyword = payload.get("keyword")
    if keyword:
        query["keyword"] = " ".join(str(keyword).split())
    return query


@register_skill
class KbRetrieveSkill(Skill):
    contract = SkillContract(
        name="kb.retrieve",
        version="1.1.0",
        purpose="两阶段检索沉淀过的经验与结构化知识，供 Agent 规划/执行前带入上下文",
        input_schema={
            "tags": "list[str]?", "keyword": "str?", "limit": "int?",
            "tenant_id": "str?", "biz_type": "str?", "channel_id": "str?",
            "region": "str?", "sku": "str?", "policy_version": "int?",
            "workflow_version": "int?", "rule_no": "str?", "gateway_code": "str?",
        },
        output_schema={"items": "list[knowledge 行]", "count": "int",
                       "docs": "list[{doc_id, score, title, kind, channels}]",
                       "doc_count": "int"},
        # 两个筛选条件都可缺省（= 取全部），所以没有 precondition。
        # 这条不许加：Coding Agent 只传 keyword，加了 tenant_id 会让它恒 precondition_failed。
        preconditions=[],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 knowledge 与 kb_doc 两张表；写入仅限一条 KbRetrieved 事件日志"
            "（走现有 append_event_log，不加新 Topic）；不写任何业务资源、不调模型、不落盘"),
        reuse_note="Manager 规划前与 Coding 执行前的检索入口（两者白名单已含本 skill）；空结果不阻塞",
        owner_roles=["manager", "coding"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        if ctx.store is None or not kb.kb_enabled():
            return {"items": [], "count": 0}

        limit = _limit_of(payload)
        out = {"items": [], "count": 0}
        out["items"] = self._knowledge_items(payload, ctx, limit)
        out["count"] = len(out["items"])
        if not payload.get("tenant_id"):
            # 没有租户就没有通路二 —— 输出**逐字段等于 1.0.0**，不多带 docs 键。
            # 老调用方（只传 keyword 的 Coding Agent、按形状断言的既有测试）
            # 看到的东西一个字节没变；带不带 docs 由「有没有真的做结构化检索」决定，
            # 而不是由版本号决定。
            return out
        docs = self._kb_docs(payload, ctx, limit)
        return {**out, "docs": docs, "doc_count": len(docs)}

    # ------------------------------------------------------------------
    @staticmethod
    def _knowledge_items(payload: dict, ctx: SkillContext, limit: int | None) -> list[dict]:
        """通路一：复盘沉淀。行为与 1.0.0 逐字段一致，老调用方零改动。"""
        tags = payload.get("tags") or None
        if tags is not None and not isinstance(tags, (list, tuple)):
            tags = [tags]
        if tags is not None:
            tags = [s for s in (str(t).strip() for t in tags) if s] or None

        keyword = payload.get("keyword")
        keyword = " ".join(str(keyword).split()) if keyword else None

        items = ctx.store.list_knowledge(tags=tags, keyword=keyword)
        return items[:limit] if limit is not None else items

    @staticmethod
    def _kb_docs(payload: dict, ctx: SkillContext, limit: int | None) -> list[dict]:
        """通路二：两阶段检索 + 落 KbRetrieved 事件。

        任何异常都压成空结果并告警：检索不该把调用方的主链路带下水（空结果不阻塞）。
        `store` 不是 SQLite 后端时 `ensure_schema` 会抛 TypeError，也走这条兜底。
        """
        query = _build_query(payload)
        if not query.get("tenant_id"):
            return []                      # 没有租户就没有候选集，硬约束
        extras = ctx.extras or {}
        try:
            kb.ensure_schema(ctx.store)
            hits = retriever.retrieve_and_log(
                ctx.store, query,
                limit=limit if limit is not None else 0,
                plan_id=str(extras.get("plan_id") or ""),
                task_id=extras.get("task_id"),
                trace_id=str(extras.get("trace_id") or ""))
        except Exception as exc:                       # noqa: BLE001 —— 检索不阻塞
            log.warning("kb_doc 检索失败（%s），本次返回空 docs", exc)
            return []
        # 整行 doc 不回给调用方：Agent 要的是「命中了哪几条、有多相关」，
        # 把 body 全文塞进 prompt 是另一件事，由调用方按需自己取。
        return [{k: h[k] for k in ("doc_id", "score", "title", "kind", "outcome",
                                   "source_case_id", "channels")} for h in hits]
