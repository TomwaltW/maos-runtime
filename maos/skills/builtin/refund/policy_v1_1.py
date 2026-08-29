"""policy.match **v1.1.0** —— 在 v1.0.0 的规则检索之上补一道「申请时效窗口」闸。

## 与 v1.0.0 的真实差异（不是改个版本号）

v1.0.0（`policy.py`）判「命中」只看一件事：规则编号的前缀是不是 `AS-`。
于是一条**早已过了申请时效**的售后规则照样被算作命中，金额照退 ——
政策文本里写着「自支付之日起 30 天内提出」，而代码里没有任何地方读这句话。

v1.1.0 把这句话变成机器可读的判据：规则 body 里可以声明 ``window_days``，
命中前缀之后再过一道时效闸 ——

  · 规则**没声明** ``window_days``  → 照旧命中，与 v1.0.0 逐字节同结论。
  · 声明了且申请时点仍在窗内        → 照旧命中。
  · 声明了且申请时点已超窗          → **不算命中**，从 `matched_rules` / `rule_refs`
                                      里剔除，也不写进 `business_ref`（超窗的规则
                                      不是裁定依据，记进去等于伪造依据链）。

命中集合被剔空时 `decision` 从 `approve` 翻成 `reject` —— 这就是演示里一眼可见的
那处差异（`maos/skills/version_demo.py`）。

## 为什么是一个新文件，而不是把 policy.py 改成 1.1.0

**旧版本从不被覆盖**，这是注册表按 ``dict[name][version]`` 保留历史版本的唯一理由
（`maos/skills/registry.py:16`）：升级期间还在跑的旧 Plan 必须能拿到当年那一个类，
行为可复现。把 `policy.py` 原地改成 1.1.0，回滚路径当场消失 ——
`get("policy.match", "1.0.0")` 会返回 None，而不是「当年那一个」。

两版共享 `policy.py` 的模块级口径（`rule_ref` / `rule_params` / `AFTER_SALES_PREFIX` /
`DECISION_*`），**不共享 run()**：规则引用的书写格式两边分叉，合并后「按哪一条判的」
这条线就对不上；而裁定流程本来就是两版各自的实现，那正是版本这个概念存在的意义。
刻意不继承 `PolicyMatchSkill`：`super().run()` 会连 `business_ref` 的写入一起继承，
超窗规则会先被记成依据再被剔除，审计链上留下一条永远解释不清的记录。

## 为什么本模块不进 `refund/__init__.py` 的清单（跨轨契约 1）

本包的 `__init__.py` 是**显式清单**，不 import 就不注册。这里刻意不加那一行：

`registry.get(name)` 缺省返回最高版本（`maos/skills/registry.py:66`），而退款域两个
调用点（`maos/agents/refund/policy_agent.py:40`、`finance_agent.py:48`）都不钉版本。
本模块一旦进正常 import 路径，那两处会**静默升版**，落库那行 `SkillInvoked` 的
`detail.version` 随之从 `"1.0.0"` 变成 `"1.1.0"` —— `evidence/scenario-*/trace.json`
里那些 `"version": "1.0.0"` 全部跟着变。既有场景的证据束必须逐字节不变，
所以 v1.1.0 由演示入口与测试**按需 import**（那也正是「投放即注册」演得出来的原因：
演示开场时 `versions("policy.match")` 还是 `['1.0.0']`）。

要让它接管默认，得先让那两个调用点显式钉 `version="1.0.0"` —— 那两个文件不在本轨
手里。已记 `docs/BACKLOG.md ## task-T11`。
"""

from __future__ import annotations

from datetime import datetime, timezone

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C
from .policy import (
    AFTER_SALES_PREFIX,
    DECISION_APPROVE,
    DECISION_REJECT,
    rule_params,
    rule_ref,
)

#: 规则 body 里声明申请时效窗口的键。天数，自订单 `paid_at` 起算。
#: 不声明 = 无时效限制（v1.0.0 的全部规则都属于这一档，所以两版同结论）。
WINDOW_KEY = "window_days"


def window_days_of(params: dict) -> float | None:
    """从规则参数里取时效窗口天数；没声明、非数字、非正数一律返回 None（= 不限时效）。

    `bool` 要单独排掉 —— 它是 `int` 的子类，``{"window_days": True}`` 会被算成 1 天。
    """
    raw = params.get(WINDOW_KEY)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw > 0 else None


def _as_datetime(text: object) -> datetime | None:
    """ISO8601 -> aware datetime；解析不了返回 None。无时区的按 UTC 读。

    naive 与 aware 相减会抛 TypeError，而这里抛出去等于「时间戳格式不标准」
    变成一次 skill 失败。时效闸的缺省方向是**放行**（见 `_elapsed_days`）。
    """
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _elapsed_days(paid_at: object, as_of: object) -> float | None:
    """申请时点距支付时点的天数；任一端解析不出就返回 None。

    None 的语义是**不判超窗**，不是「超窗」：时点读不出来时拒付一笔本该退的钱，
    比放行一笔本该拒的钱更难解释，而且这一档的行为与 v1.0.0 一致 —— 时效闸只在
    数据足够时才收紧口径，数据不足时不制造两版之间的无谓分叉。
    """
    paid = _as_datetime(paid_at)
    now = _as_datetime(as_of)
    if paid is None or now is None:
        return None
    return (now - paid).total_seconds() / 86400.0


@register_skill
class PolicyMatchV11Skill(Skill):
    contract = SkillContract(
        name="policy.match",
        version="1.1.0",
        purpose=(
            "按订单快照锁定的政策版本检索适用规则并裁定退款资格，"
            "并按规则声明的申请时效窗口（window_days）剔除超窗规则（零模型，可复现）"
        ),
        input_schema={
            "tenant_id": "str",
            "case_id": "str",
            "rule_prefix": "str（可选，默认 'AS-'）",
            "as_of": "str（可选，ISO8601 申请时点；默认取 refund_case.created_at）",
        },
        output_schema={
            "policy_version": "int（订单锁定的版本，**不是**当前最新版本）",
            "matched_rules": "list[dict{rule_no,version,title,params}]（**已剔除超窗规则**）",
            "rule_refs": "list[str]（形如 AS-01@v1）",
            "decision": "approve|reject（命中集合被时效闸剔空则 reject）",
            "reason": "str（有规则超窗时点明剔除了哪几条、窗口多少天）",
            "invocation_id": "str",
        },
        preconditions=["tenant_id", "case_id"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读 refund_case / order_snapshot / policy_rule，只写 business_ref；"
            "不改 biz_status、不调模型、不碰支付网关；"
            "政策版本一律取自订单快照，禁止使用 policy_rule 的最新版本；"
            "时效判定只读订单快照的 paid_at 与 case 的 created_at，不写任何表，"
            "且超窗规则不写进 business_ref —— 依据链里只许留真正采信的那几条"
        ),
        reuse_note=(
            "在 v1.0.0「按快照锁定的版本判定」之上补时效闸；"
            "任何「规则自身带生效时长、需按业务时点逐条核对」的场景可照此复用 window_days_of"
        ),
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

        # 版本锁定与规则检索都走 R-1 的冻结口径，本模块不自写 SQL（同 v1.0.0）。
        pinned = objects.pinned_policy_version(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])
        applicable = objects.policy_rules_at_order(
            store, tenant_id=tenant_id, order_id=case["order_id"],
            order_version=case["order_version"])

        # 申请时点：调用方给了就用调用方的（演示与回归要可复现），否则取建案时刻。
        as_of = payload.get("as_of") or case.get("created_at")
        elapsed = _elapsed_days(self._paid_at(store, tenant_id, case), as_of)

        # ---- v1.1.0 唯一的新增判据：前缀命中之后再过一道时效闸 ----
        prefix_hits = [r for r in applicable if str(r["rule_no"]).startswith(prefix)]
        matched, expired = [], []
        for rule in prefix_hits:
            window = window_days_of(rule_params(rule))
            if window is None or elapsed is None or elapsed <= window:
                matched.append(rule)
            else:
                expired.append((rule, window))

        rules_out = [{
            "rule_no": r["rule_no"], "version": int(r["version"]),
            "title": r.get("title", ""), "params": rule_params(r),
        } for r in matched]
        refs = [rule_ref(r) for r in matched]

        decision, reason = self._conclude(pinned, prefix, matched, refs, expired,
                                          len(applicable), elapsed)

        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if plan_id and task_id:
            # 只挂真正采信的那几条：超窗规则不是裁定依据。
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

    # ------------------------------------------------------------------
    @staticmethod
    def _paid_at(store, tenant_id: str, case: dict) -> str | None:
        """订单支付时刻 —— 时效窗口的起算点，权威在订单快照上（铁律 8）。"""
        rows = objects.query(
            store,
            "SELECT paid_at FROM order_snapshot WHERE tenant_id=? AND order_id=? AND version=?",
            (tenant_id, case["order_id"], int(case["order_version"])),
        )
        return rows[0]["paid_at"] if rows else None

    @staticmethod
    def _conclude(pinned, prefix, matched, refs, expired, n_applicable, elapsed):
        """结论与文案。

        **没有任何规则超窗时，这里逐字返回 v1.0.0 的两句话** —— 既有场景的规则都没
        声明 window_days，于是两版输出逐字节相同（跨轨契约 1）。测试
        `test_skill_versioning.py::test_existing_scenario_input_yields_identical_output`
        钉住这一条：文案在这里分叉，证据束就会变。
        """
        if not expired:
            if matched:
                return DECISION_APPROVE, (
                    f"命中 {len(matched)} 条售后规则（政策 v{pinned}）：{'、'.join(refs)}")
            return DECISION_REJECT, (
                f"政策 v{pinned} 下没有适用的 {prefix} 售后规则，"
                f"该订单不在退款范围内（当时可用规则 {n_applicable} 条）")

        dropped = "、".join(
            f"{rule_ref(r)}（窗口 {w:g} 天）" for r, w in expired)
        days = f"{elapsed:.1f}" if elapsed is not None else "?"
        if matched:
            return DECISION_APPROVE, (
                f"命中 {len(matched)} 条售后规则（政策 v{pinned}）：{'、'.join(refs)}；"
                f"另有 {len(expired)} 条超出申请时效已剔除：{dropped}（距支付 {days} 天）")
        return DECISION_REJECT, (
            f"政策 v{pinned} 下命中的 {len(expired)} 条 {prefix} 售后规则全部超出申请时效"
            f"（距支付 {days} 天）：{dropped}，该笔申请不予受理")
