"""refund.intake —— 把多源退款诉求聚合去重，建案并把证据挂上去。

多源是这条链路的前提：一次退款诉求会同时从工单系统、客服记录、客户上传的图片
三个口子进来，同一件事被说三遍。**去重不另写一套** —— 直接复用已经在库的
`issue.aggregate`（D 轨落的，零模型、按归一化标题分组），经 `SkillInvoker` 调用，
于是这次复用在 event_log 里留下一条 SkillInvoked，是可查的事实而不是注释里的声称。

为什么不 import 那个类直接 `.run()`：绕过 invoker 就没有白名单校验、没有审计行，
「复用」变成了一句自述。调用方的 identity 必须同时授予 `issue.aggregate` ——
最小授权本来就该在 identity 上表达，不该由被调方自己放行。

建案只走 `guard.create_case()`：`refund_case` 全系统只有两个写入口，
`objects.execute()` 见到这张表的写语句会直接抛 BypassedGuardError。
"""

from __future__ import annotations

from maos.domain.refund import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import register_skill

from . import _common as C

SKILL_AGGREGATE = "issue.aggregate"

#: case_seed 里必须齐的字段。少一个就建不出案 —— 与其让 sqlite 抛 IntegrityError，
#: 不如在这里报出到底缺哪一个。
_SEED_FIELDS = ("tenant_id", "case_id", "channel_id", "order_id", "order_version",
                "sku", "reason_code", "amount_claimed")


def _evidence_of(signals: list[dict]) -> list[dict]:
    """带 uri 的信号即证据。

    判据用 `uri` 而不是 `kind == "image"`：证据的本质是「有个外部对象可以调阅」，
    图片、录音、PDF 都算，而 kind 的取值域不由本 skill 定 —— 按 kind 白名单挑
    会把没见过的证据类型静默丢掉。
    """
    out = []
    for i, sig in enumerate(signals, start=1):
        if not isinstance(sig, dict):
            continue
        uri = str(sig.get("uri") or "").strip()
        if not uri:
            continue
        out.append({
            "evidence_id": str(sig.get("evidence_id") or f"ev-{i:02d}"),
            "kind": str(sig.get("kind") or "attachment"),
            "uri": uri,
            # digest 由信号自带则用自带的（那是上传时算的），否则按 uri 算一个占位，
            # 保证这一列永远非空 —— 空 digest 的证据没法证明「调阅到的还是当初那份」。
            "digest": str(sig.get("digest") or C.digest(uri)),
            "source": str(sig.get("source") or "unknown"),
        })
    return out


@register_skill
class RefundIntakeSkill(Skill):
    contract = SkillContract(
        name="refund.intake",
        version="1.0.0",
        purpose="聚合多源退款诉求与证据，去重后建 refund_case 并挂上证据引用",
        input_schema={
            "signals": "list[dict]（工单 / 客服记录 / 客户上传，形状同 issue.aggregate 的 findings）",
            "case_seed": "dict{tenant_id,case_id,channel_id,order_id,order_version,sku,"
                         "reason_code,amount_claimed}",
        },
        output_schema={
            "case_draft": "dict（refund_case 那一行，biz_status=submitted）",
            "evidence_refs": "list[dict{evidence_id,kind,uri,digest,source}]",
            "issues": "list[dict]（issue.aggregate 的去重结果）",
            "dedup": "dict{signals:int,issues:int,merged:int}",
            "invocation_id": "str（本次写入的 actor 锚点）",
        },
        preconditions=["signals", "case_seed"],
        depends_tools=[],
        # 纯规则 + 一次库写入。重试会撞 refund_case 主键，没有可重试的失败形态。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只写 refund_case（经 guard.create_case）/ customer_evidence / business_ref；"
            "不调模型、不碰支付网关；去重经 SkillInvoker 复用 issue.aggregate，"
            "调用方 identity 必须同时授予该 skill，否则 PermissionDenied"
        ),
        reuse_note="任何业务域要把多源诉求收成一个案子都可照此复用 issue.aggregate，不另写去重",
        owner_roles=["refund_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if not plan_id or not task_id:
            raise ValueError(
                "refund.intake 需要 extras 里的 plan_id / task_id（业务引用要挂到 DAG 上）")

        signals = payload.get("signals")
        if not isinstance(signals, list):
            raise ValueError(
                f"refund.intake 入参 signals 必须是 list，实际 {type(signals).__name__}")
        seed = payload.get("case_seed")
        if not isinstance(seed, dict):
            raise ValueError(
                f"refund.intake 入参 case_seed 必须是 dict，实际 {type(seed).__name__}")
        missing = [k for k in _SEED_FIELDS if seed.get(k) in (None, "")]
        if missing:
            raise ValueError(f"case_seed 缺字段：{missing}")

        # ---- 去重：复用 issue.aggregate，不另写 -------------------------------
        aggregated = self._aggregate(signals, ctx, extras)

        # ---- 建案：唯一入口 guard.create_case --------------------------------
        case = guard.create_case(
            store,
            tenant_id=str(seed["tenant_id"]),
            case_id=str(seed["case_id"]),
            channel_id=str(seed["channel_id"]),
            order_id=str(seed["order_id"]),
            order_version=int(seed["order_version"]),
            sku=str(seed["sku"]),
            reason_code=str(seed["reason_code"]),
            amount_claimed=float(seed["amount_claimed"]),
            plan_id=plan_id,
            actor_skill=self.contract.name,
            invocation_id=invocation_id,
        )

        # ---- 证据落库 --------------------------------------------------------
        evidence = _evidence_of(signals)
        for ev in evidence:
            objects.execute(
                store,
                "INSERT OR REPLACE INTO customer_evidence (tenant_id, case_id, evidence_id,"
                " kind, uri, digest, submitted_at) VALUES (?,?,?,?,?,?,?)",
                (case["tenant_id"], case["case_id"], ev["evidence_id"], ev["kind"],
                 ev["uri"], ev["digest"], C.now_iso()),
            )

        # ---- DAG -> 业务对象：只存引用，不存副本 ------------------------------
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="refund_case", object_id=case["case_id"], purpose="受理建案")
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="order_snapshot", object_id=case["order_id"],
            object_version=case["order_version"], purpose="退款依据的订单快照")

        issues = aggregated["issues"]
        return {
            "case_draft": case,
            "evidence_refs": evidence,
            "issues": issues,
            "dedup": {"signals": len(signals), "issues": len(issues),
                      "merged": len(signals) - len(issues)},
            "aggregate_summary": aggregated["summary"],
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------
    def _aggregate(self, signals: list[dict], ctx: SkillContext, extras: dict) -> dict:
        """经 invoker 调 issue.aggregate。未注册 = 硬失败，不退化成「不去重」。

        软兜底在这里是错的：invoker 的 `skill_not_found` 软兜底是为了让并行开发期
        「被调方还没合并」不炸链路（A-5），但去重一旦静默跳过，多源诉求会被当成
        N 个不同的问题继续往下走，而 case 照样建得出来 —— 表面全绿，语义全错。
        """
        invoker = SkillInvoker(ctx.identity, ctx.store)
        res = invoker.invoke(SKILL_AGGREGATE, {"findings": signals}, extras=dict(extras))
        if res.status != "ok" or not isinstance(res.output, dict):
            raise RuntimeError(
                f"refund.intake 依赖的 {SKILL_AGGREGATE} 未产出去重结果：{res.error}；"
                "去重不允许降级 —— 跳过它会让多源诉求被当成多个不同的问题"
            )
        return res.output
