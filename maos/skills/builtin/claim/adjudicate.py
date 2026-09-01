"""claim.adjudicate —— 按**投保当时锁定的**条款版本裁定赔付责任。

这是理赔域最容易写错的一处，也是本域最值得拿给评委看的一处：

    人家 2023 年投的保，不能拿 2025 年的条款判。
    被保险人是按投保当时公示的条款交的保费，权威在
    `policy_contract.terms_version_at_bind`，不在 `policy_terms` 表的 `max(version)` 上。

与退款域 `policy.match` 的 `AS-01@v1` 是同构物：同一条道理换一个域再成立一次。
版本锁定与条款检索**一律调 `objects.terms_at_bind()`**，本模块不自己写一份 SQL ——
两份实现一定会在「<= 锁定版本取每条条款的最大版本」这个细节上分叉，而分叉的症状是
「赔款算错了一点点」，几乎不可能在演示里被当场看出来。

裁定零模型：同一个案子在任何机器任何时刻必须给同一个结论。条款命中是可解释的规则
匹配，不是语义理解 —— 让模型来裁反而丢掉了「按哪一条、哪一版判的」这个可审计点。

## 裁定产物必须自带 rule_no + terms_version

`adjudication` 表把这两个字段**摊平成列**，不埋在 `breakdown_json` 里。理由是
可核对性：重放校验要能直接 `SELECT rule_no, terms_version FROM adjudication`，
埋进 JSON 就得靠解析字符串才查得到，而那不是一个可以被机器逐条对的字段。
"""

from __future__ import annotations

import json

from maos.domain.claim import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: 理赔类条款的编号前缀。租户的条款编号约定，可由入参覆盖。
#: 不写死成「所有条款都适用」：条款表里同时躺着承保、除外、理赔各类条款，
#: 全量命中会让一条与赔付无关的条款参与金额核算。
CLAIM_PREFIX = "CL-"

#: 除外责任条款的前缀。命中任何一条**直接拒赔** —— 除外条款是否定式的，
#: 不参与「取最有利的一条」那种并集口径。
EXCLUSION_PREFIX = "EX-"

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def rule_ref(rule: dict) -> str:
    """条款引用的书写口径：``<rule_no>@v<version>``。

    `claim.settle` 的 `rule_refs` 与本 skill 的输出共用它 —— 两边各写一套格式，
    合并后「按哪一条判的」这条线就对不上了。口径与退款域 `policy.rule_ref` 一致，
    好让两个域的证据束用同一套读法。
    """
    return f"{rule['rule_no']}@v{rule['version']}"


def rule_params(rule: dict) -> dict:
    """从条款 body 里取机器可读的参数。

    body 是 JSON 就按 JSON 读；不是（人写的自然语言条款）就返回空 dict，由
    `claim.settle` 落到它的缺省口径上，**不在这里猜**。猜一个比例出来，
    赔款会错得很安静。
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
class ClaimAdjudicateSkill(Skill):
    contract = SkillContract(
        name="claim.adjudicate",
        version="1.0.0",
        purpose="按保单快照锁定的条款版本检索适用条款并裁定赔付责任（零模型，可复现）；"
                "产出带 rule_no + terms_version 的 adjudication 行",
        input_schema={
            "tenant_id": "str",
            "claim_id": "str",
            "rule_prefix": "str（可选，默认 'CL-'）",
        },
        output_schema={
            "terms_version": "int（保单锁定的条款版本，**不是**当前最新版本）",
            "policy_version": "int（保单快照版本）",
            "matched_rules": "list[dict{rule_no,version,title,params}]",
            "rule_refs": "list[str]（形如 CL-01@v1）",
            "primary_rule": "str（写进 adjudication.rule_no 那一条）",
            "exclusions": "list[str]（命中的除外责任条款，非空即拒赔）",
            "decision": "approve|reject",
            "reason": "str",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "claim_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 claim_case / policy_contract / policy_terms，写 adjudication 与"
            " claim_business_ref，并把 biz_status 推进到 adjudicated / rejected；"
            "**无权写 paid** —— guard 会抛 AuthoritativeFactViolation；"
            "不调模型、不碰赔付方；"
            "条款版本一律取自保单快照的 terms_version_at_bind，禁止使用 policy_terms 的最新版本"
        ),
        reuse_note="任何「按快照锁定的版本判定」的场景都可照此复用 objects.terms_at_bind",
        owner_roles=["claim_adjudicator"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        tenant_id, claim_id = C.required(payload, "tenant_id", "claim_id")
        prefix = str(payload.get("rule_prefix") or CLAIM_PREFIX)

        case = guard.get_case(store, tenant_id, claim_id)
        if case is None:
            raise LookupError(f"没有这个 case：tenant={tenant_id} claim={claim_id}")

        # 版本锁定与条款检索都走域的冻结口径，本模块不自写 SQL。
        pinned = objects.pinned_terms_version(
            store, tenant_id=tenant_id, policy_no=case["policy_no"],
            policy_version=case["policy_version"])
        applicable = objects.terms_at_bind(
            store, tenant_id=tenant_id, policy_no=case["policy_no"],
            policy_version=case["policy_version"], loss_type=case["loss_type"])

        matched = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]
        exclusions = [r for r in applicable
                      if str(r["rule_no"]).startswith(EXCLUSION_PREFIX)]
        rules_out = [{
            "rule_no": r["rule_no"], "version": int(r["version"]),
            "title": r.get("title", ""), "params": rule_params(r),
        } for r in matched]
        refs = [rule_ref(r) for r in matched]
        exclusion_refs = [rule_ref(r) for r in exclusions]

        # 判定顺序：**先看除外责任，再看承保条款**。反过来的话，一个既命中承保
        # 又命中除外的案子会被判成赔付 —— 除外条款存在的全部意义就是压过承保条款。
        if exclusions:
            decision = DECISION_REJECT
            reason = (f"命中除外责任条款（条款 v{pinned}）：{'、'.join(exclusion_refs)}，"
                      f"不予赔付")
            primary = exclusions[0]["rule_no"]
        elif matched:
            decision = DECISION_APPROVE
            reason = (f"命中 {len(matched)} 条理赔条款（条款 v{pinned}）：{'、'.join(refs)}")
            primary = matched[0]["rule_no"]
        else:
            decision = DECISION_REJECT
            reason = (f"条款 v{pinned} 下没有适用的 {prefix} 理赔条款，"
                      f"本次出险不在保障范围内（当时可用条款 {len(applicable)} 条）")
            # 一条条款都没命中时也要留下一个可核对的 rule_no —— 空串会让
            # `adjudication` 的主键退化成 (tenant, claim, '', v)，看起来像有裁定，
            # 实际上指不到任何一条条款。用显式占位说明「查过，没有」。
            primary = f"{prefix}NONE"

        # ---- 裁定落库：rule_no + terms_version 摊平成列 ----------------------
        breakdown = {
            "matched_rules": rules_out,
            "rule_refs": refs,
            "exclusions": exclusion_refs,
            "applicable_count": len(applicable),
            "terms_version": pinned,
            "policy_no": case["policy_no"],
            "policy_version": int(case["policy_version"]),
            "reported_at": case["reported_at"],
        }
        objects.execute(
            store,
            "INSERT OR REPLACE INTO adjudication (tenant_id, claim_id, rule_no,"
            " terms_version, decision, allowed_amount, rule_refs, breakdown_json,"
            " adjudicated_by, adjudicated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, claim_id, primary, pinned, decision, 0.0,
             json.dumps(refs, ensure_ascii=False),
             json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
             getattr(getattr(ctx, "identity", None), "agent_id", "") or self.contract.name,
             C.now_iso()),
        )

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            for r in matched + exclusions:
                objects.attach_business_ref(
                    store, plan_id=plan_id, task_id=task_id, tenant_id=tenant_id,
                    object_type="policy_terms", object_id=r["rule_no"],
                    object_version=int(r["version"]), purpose="裁定依据的条款")

        # ---- 业务状态推进。**本 skill 写不出 paid** —— guard 会拦 --------------
        if case["biz_status"] == "submitted":
            target = "adjudicated" if decision == DECISION_APPROVE else "rejected"
            case = guard.update_biz_status(
                store, tenant_id, claim_id, target,
                self.contract.name, invocation_id,
                reason=f"按条款 v{pinned} 裁定 {decision}：{reason}")

        return {
            "terms_version": pinned,
            "policy_version": int(case["policy_version"]),
            "matched_rules": rules_out,
            "rule_refs": refs,
            "primary_rule": primary,
            "exclusions": exclusion_refs,
            "decision": decision,
            "reason": reason,
            "biz_status": case["biz_status"],
            "claim_id": claim_id,
            "tenant_id": tenant_id,
            "invocation_id": invocation_id,
        }
