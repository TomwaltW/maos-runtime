"""policy.match —— 按**下单当时锁定的**政策版本检索规则并裁定。

这是退款域最容易写错的一处，也是本域最值得拿给评委看的一处：

    用当前最新政策去判一笔历史订单，等于拿今天的规则追溯昨天的交易。
    客户是按下单当时公示的政策下的单，权威在 `order_snapshot.policy_version_at_order`，
    不在 `policy_rule` 表的 `max(version)` 上。

版本锁定与规则检索**一律调 R-1 的 `objects.policy_rules_at_order()`**，本模块不自己
写一份 SQL —— 两份实现一定会在「≤ 锁定版本取每条规则的最大版本」这个细节上分叉，
而分叉的症状是「金额算错了一点点」，几乎不可能在演示里被当场看出来。

裁定零模型：同一个案子在任何机器任何时刻必须给同一个结论。政策命中是可解释的
规则匹配，不是语义理解 —— 让模型来裁反而丢掉了「按哪一条、哪一版判的」这个可审计点。
"""

from __future__ import annotations

import json

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 售后类规则的编号前缀。租户的规则编号约定，可由入参覆盖。
#: 不写死成「所有规则都适用」：政策表里同时躺着售前、履约、售后各类规则，
#: 全量命中会让一条与退款无关的规则参与金额核算。
AFTER_SALES_PREFIX = "AS-"

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def rule_ref(rule: dict) -> str:
    """规则引用的书写口径：``<rule_no>@v<version>``。

    finance.settle 的 `rule_refs` 与本 skill 的输出共用它 —— 两边各写一套格式，
    合并后「按哪一条判的」这条线就对不上了。
    """
    return f"{rule['rule_no']}@v{rule['version']}"


def rule_params(rule: dict) -> dict:
    """从规则 body 里取机器可读的参数。

    body 是 JSON 就按 JSON 读；不是（人写的自然语言条款）就返回空 dict，由
    finance.settle 落到它的缺省口径上，**不在这里猜**。猜一个比例出来，
    金额会错得很安静。
    """
    body = rule.get("body")
    if not isinstance(body, str) or not body.strip():
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@register_skill
class PolicyMatchSkill(Skill):
    contract = SkillContract(
        name="policy.match",
        version="1.0.0",
        purpose="按订单快照锁定的政策版本检索适用规则并裁定退款资格（零模型，可复现）",
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "rule_prefix": "str（可选，默认 'AS-'）",
        },
        output_schema={
            "policy_version": "int（订单锁定的版本，**不是**当前最新版本）",
            "matched_rules": "list[dict{rule_no,version,title,params}]",
            "rule_refs": "list[str]（形如 AS-01@v1）",
            "decision": "approve|reject",
            "reason": "str",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 refund_case / order_snapshot / policy_rule，只写 business_ref；"
            "不改 biz_status、不调模型、不碰支付网关；"
            "政策版本一律取自订单快照，禁止使用 policy_rule 的最新版本"
        ),
        reuse_note="任何「按快照锁定的版本判定」的场景都可照此复用 objects.policy_rules_at_order",
        owner_roles=["refund_policy"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, case_id = C.required(payload, "tenant_id", "case_id")
        prefix = str(payload.get("rule_prefix") or AFTER_SALES_PREFIX)

        case = guard.get_case(store, tenant_id, case_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} case={case_id}")

        # 版本锁定与规则检索都走 R-1 的冻结口径，本模块不自写 SQL。
        pinned = objects.pinned_policy_version(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])
        applicable = objects.policy_rules_at_order(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])

        matched = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]
        rules_out = [{
            "rule_no": r["rule_no"], "version": int(r["version"]),
            "title": r.get("title", ""), "params": rule_params(r),
        } for r in matched]
        refs = [rule_ref(r) for r in matched]

        if matched:
            decision, reason = DECISION_APPROVE, (
                f"命中 {len(matched)} 条售后规则（政策 v{pinned}）：{'、'.join(refs)}")
        else:
            decision, reason = DECISION_REJECT, (
                f"政策 v{pinned} 下没有适用的 {prefix} 售后规则，"
                f"该订单不在退款范围内（当时可用规则 {len(applicable)} 条）")

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            for r in matched:
                objects.attach_business_ref(
                    store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                    object_type="policy_rule", object_id=r["rule_no"],
                    object_version=int(r["version"]), purpose="裁定依据的政策规则")

        return {
            "policy_version": pinned,
            "matched_rules": rules_out,
            "rule_refs": refs,
            "decision": decision,
            "reason": reason,
            "case_id": case_id,
            "tenant_id": tenant_id,
            "invocation_id": invocation_id,
        }
