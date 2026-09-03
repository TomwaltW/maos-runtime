"""claim.intake —— 把三源报案信号聚合去重，建案并把证据挂上去。

多源是这条链路的前提：一次报案会同时从工单系统、客服记录、定损照片三个口子进来，
同一件事被说三遍。**去重不另写一套** —— 直接复用已经在库的 `issue.aggregate`
（零模型、按归一化标题分组），经 `SkillInvoker` 调用，于是这次复用在 event_log 里
留下一条 SkillInvoked，是可查的事实而不是注释里的声称。

为什么不 import 那个类直接 `.run()`：绕过 invoker 就没有白名单校验、没有审计行，
「复用」变成了一句自述。调用方的 identity 必须同时授予 `issue.aggregate` ——
最小授权本来就该在 identity 上表达，不该由被调方自己放行。

建案只走 `guard.create_case()`：`claim_case` 全系统只有两个写入口，
`objects.execute()` 见到这张表的写语句会直接抛 BypassedGuardError。

## 报案时点是本 skill 落下的第二个锚

`reported_at` 在这里定死，后续任何一步都不许重取「当前时刻」去当报案时点。
它与 `policy_version` 一起决定了**用哪一版条款判这个案子** —— 报案时点漂了，
条款版本就可能跟着漂，而那种错没有症状：金额算得出来，只是算错了年份的规则。
"""

from __future__ import annotations

from maos.domain.claim import guard, objects
from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import register_skill

from . import _common as C

SKILL_AGGREGATE = "issue.aggregate"

#: case_seed 里必须齐的字段。少一个就建不出案 —— 与其让 sqlite 抛 IntegrityError，
#: 不如在这里报出到底缺哪一个。
_SEED_FIELDS = ("tenant_id", "claim_id", "payer_id", "policy_no", "policy_version",
                "loss_type", "incident_at", "amount_claimed")


def _evidence_of(signals: list[dict]) -> list[dict]:
    """带 uri 的信号即证据。

    判据用 `uri` 而不是 `kind == "image"`：证据的本质是「有个外部对象可以调阅」，
    定损照片、录音、PDF 报告都算，而 kind 的取值域不由本 skill 定 —— 按 kind 白名单
    挑会把没见过的证据类型静默丢掉。
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


def _lines_of(payload: dict) -> list[dict]:
    """赔付明细行。没给就整案一行 —— 不许静默落零行。

    零行的后果是 `claim.settle` 算出 0 元赔款而每一步都「成功」：
    一个静默算成零的案子比一次报错难查得多。
    """
    raw = payload.get("claim_lines")
    if not isinstance(raw, list) or not raw:
        return []
    out = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        out.append({
            "line_no": int(item.get("line_no") or i),
            "item_code": str(item.get("item_code") or f"ITEM-{i:02d}"),
            "description": str(item.get("description") or ""),
            "amount_claimed": float(item.get("amount_claimed") or 0.0),
        })
    return out


@register_skill
class ClaimIntakeSkill(Skill):
    contract = SkillContract(
        name="claim.intake",
        version="1.0.0",
        purpose="聚合三源报案信号与证据，去重后建 claim_case 并挂上证据与赔付明细行",
        input_schema={
            "signals": "list[dict]（工单 / 客服记录 / 定损照片，形状同 issue.aggregate 的 findings）",
            "case_seed": "dict{tenant_id,claim_id,payer_id,policy_no,policy_version,"
                         "loss_type,incident_at,amount_claimed}",
            "claim_lines": "list[dict{line_no,item_code,description,amount_claimed}]（可选）",
            "reported_at": "str（可选，报案时点；缺省取当前时刻并就此定死）",
        },
        output_schema={
            "case_draft": "dict（claim_case 那一行，biz_status=submitted）",
            "evidence_refs": "list[dict{evidence_id,kind,uri,digest,source}]",
            "claim_lines": "list[dict]（落进 claim_line 的明细行）",
            "issues": "list[dict]（issue.aggregate 的去重结果）",
            "dedup": "dict{signals:int,issues:int,merged:int}",
            "invocation_id": "str（本次写入的 actor 锚点）",
        },
        preconditions=["signals", "case_seed"],
        depends_tools=[],
        # 纯规则 + 一次库写入。重试会撞 claim_case 主键，没有可重试的失败形态。
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只写 claim_case（经 guard.create_case）/ claim_evidence / claim_line /"
            " claim_business_ref；不调模型、不碰赔付方；"
            "去重经 SkillInvoker 复用 issue.aggregate，调用方 identity 必须同时授予该 skill，"
            "否则 PermissionDenied"
        ),
        reuse_note="任何业务域要把多源诉求收成一个案子都可照此复用 issue.aggregate，不另写去重",
        owner_roles=["claim_intake"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> dict:
        store = C.ensure_schema(ctx)
        invocation_id = C.invocation_id_of(ctx)
        extras = getattr(ctx, "extras", None) or {}
        plan_id = str(extras.get("plan_id") or "")
        task_id = str(extras.get("task_id") or "")
        if not plan_id or not task_id:
            raise ValueError(
                "claim.intake 需要 extras 里的 plan_id / task_id（业务引用要挂到 DAG 上）")

        signals = payload.get("signals")
        if not isinstance(signals, list):
            raise ValueError(
                f"claim.intake 入参 signals 必须是 list，实际 {type(signals).__name__}")
        seed = payload.get("case_seed")
        if not isinstance(seed, dict):
            raise ValueError(
                f"claim.intake 入参 case_seed 必须是 dict，实际 {type(seed).__name__}")
        missing = [k for k in _SEED_FIELDS if seed.get(k) in (None, "")]
        if missing:
            raise ValueError(f"case_seed 缺字段：{missing}")

        # ---- 去重：复用 issue.aggregate，不另写 -------------------------------
        aggregated = self._aggregate(signals, ctx, extras)

        # ---- 建案：唯一入口 guard.create_case --------------------------------
        case = guard.create_case(
            store,
            tenant_id=str(seed["tenant_id"]),
            claim_id=str(seed["claim_id"]),
            payer_id=str(seed["payer_id"]),
            policy_no=str(seed["policy_no"]),
            policy_version=int(seed["policy_version"]),
            loss_type=str(seed["loss_type"]),
            incident_at=str(seed["incident_at"]),
            amount_claimed=float(seed["amount_claimed"]),
            plan_id=plan_id,
            actor_skill=self.contract.name,
            invocation_id=invocation_id,
            reported_at=str(payload.get("reported_at") or ""),
        )

        # ---- 证据落库 --------------------------------------------------------
        evidence = _evidence_of(signals)
        for ev in evidence:
            objects.execute(
                store,
                "INSERT OR REPLACE INTO claim_evidence (tenant_id, claim_id, evidence_id,"
                " kind, uri, digest, source, submitted_at) VALUES (?,?,?,?,?,?,?,?)",
                (case["tenant_id"], case["claim_id"], ev["evidence_id"], ev["kind"],
                 ev["uri"], ev["digest"], ev["source"], C.now_iso()),
            )

        # ---- 赔付明细行 ------------------------------------------------------
        lines = _lines_of(payload)
        if not lines:
            # 没给明细就整案一行。**不落零行** —— 见 `_lines_of` 的说明。
            lines = [{"line_no": 1, "item_code": str(case["loss_type"]),
                      "description": "整案申报（未拆明细）",
                      "amount_claimed": float(case["amount_claimed"])}]
        for line in lines:
            objects.execute(
                store,
                "INSERT OR REPLACE INTO claim_line (tenant_id, claim_id, line_no, item_code,"
                " description, amount_claimed, amount_allowed, carc_code, group_code)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (case["tenant_id"], case["claim_id"], line["line_no"], line["item_code"],
                 line["description"], line["amount_claimed"], 0.0, "", ""),
            )

        # ---- DAG -> 业务对象：只存引用，不存副本 ------------------------------
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="claim_case", object_id=case["claim_id"], purpose="报案建案")
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="policy_contract", object_id=case["policy_no"],
            object_version=int(case["policy_version"]), purpose="理赔依据的保单快照")

        issues = aggregated["issues"]
        return {
            "case_draft": case,
            "evidence_refs": evidence,
            "claim_lines": lines,
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
        「被调方还没合并」不炸链路，但去重一旦静默跳过，三源报案会被当成三个不同的
        案子继续往下走，而 case 照样建得出来 —— 表面全绿，语义全错。
        """
        invoker = SkillInvoker(ctx.identity, ctx.store)
        res = invoker.invoke(SKILL_AGGREGATE, {"findings": signals}, extras=dict(extras))
        if res.status != "ok" or not isinstance(res.output, dict):
            raise RuntimeError(
                f"claim.intake 依赖的 {SKILL_AGGREGATE} 未产出去重结果：{res.error}；"
                "去重不允许降级 —— 跳过它会让三源报案被当成三个不同的案子")
        return res.output
