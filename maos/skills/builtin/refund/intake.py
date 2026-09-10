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

from maos.domain.refund import case_pack, guard, objects
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
    图片、录音、PDF 都算，而**成员判据**不由 kind 定 —— 按 kind 白名单挑
    会把没见过的证据类型静默丢掉。

    但「不按 kind 挑」不等于「kind 的取值域不管」：政策规则里写的是
    `requires_evidence_kinds: ["image"]`，渠道送来的却可能是 photo / img /
    screenshot，落库原样透传就一条都对不上，举证闸永远判「证据不足」且不报错。
    所以落库前过一次 `C.normalize_evidence_kind`（值域见 `_common.EVIDENCE_KINDS`），
    认不出的归 attachment 但把提交方的原始声明留在出参的 `kind_raw` 里 ——
    归一化只改写 `kind` 这一列的取值，**证据集合的成员一条不增不减**。
    """
    out = []
    for i, sig in enumerate(signals, start=1):
        if not isinstance(sig, dict):
            continue
        uri = str(sig.get("uri") or "").strip()
        if not uri:
            continue
        kind_raw = str(sig.get("kind") or "")
        out.append({
            "evidence_id": str(sig.get("evidence_id") or f"ev-{i:02d}"),
            "kind": C.normalize_evidence_kind(kind_raw),
            # 原始声明只到出参与 event_log，不落库：customer_evidence 本轮不加列。
            "kind_raw": kind_raw,
            "uri": uri,
            # digest 由信号自带则用自带的（那是上传时算的），否则按 uri 算一个占位，
            # 保证这一列永远非空 —— 空 digest 的证据没法证明「调阅到的还是当初那份」。
            "digest": str(sig.get("digest") or C.digest(uri)),
            "source": str(sig.get("source") or "unknown"),
        })
    return out


def _applicant_of(payload: dict) -> dict | None:
    """可选的审批单引用。没给就返回 None，对既有行为零影响。

    消费者售后进来的是 `case_seed` 那八个字段，供应链进来的是一张审批单：
    `{supplier_id, po_no, approver, approved_amount, doc_no}`。这里**不按字段名过滤**
    ——只校验 `doc_no`，其余原样回填：审批单的字段随渠道而异，挑白名单会把
    没见过的字段静默丢掉，和证据 kind 那处是同一个错。

    **为什么不扩表**：退款域的 14 张表本轮一张不加、一列不改。审批单本质上是
    「外部系统里有一份单子，本案引用它」—— 这正是 `business_ref` 的语义，
    落成一条 `object_type="applicant_ref"` 的引用即可，不必为它开一张表。
    真要把供应链退款做深，它该有自己的域（口径同 ap / claim），不是往这里塞列。

    **doc_no 缺了就抛**：单号是这条引用的 object_id，没有单号就没有可指向的外部
    对象，落进去是一条指不到任何地方的引用。认不出就报错，不猜缺省值。
    """
    raw = payload.get("applicant_ref")
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"refund.intake 入参 applicant_ref 必须是 dict，实际 {type(raw).__name__}")
    if not str(raw.get("doc_no") or "").strip():
        raise ValueError(
            "applicant_ref 缺 doc_no：审批单没有单号就没有可引用的外部对象")
    return dict(raw)


def _product_version(store, tenant_id: str, sku: str) -> int | None:
    """本案 SKU 的商品快照版本 —— 取库里最大的那一版。查不到返回 None。

    与政策规则的取法（`objects.policy_rules_at_order` 按 `version <= pinned`）
    刻意不同：政策有「下单当时锁定哪一版」这个外部事实可依（`order_snapshot`
    上就有 `policy_version_at_order`），而商品快照没有对应的锁定字段。
    在没有锁定依据的地方硬造一个（比如"取 ≤ 订单版本的那一版"）是把两个不相干的
    版本维度焊在一起，那种错没有症状 —— 只是质保月数悄悄取错了一版。

    真要做到「按下单当时的商品快照判质保」，得先有一个 `product_version_at_order`
    之类的外部事实。已记 `docs/BACKLOG.md` 的 `## task-t116`。
    """
    rows = objects.query(
        store,
        "SELECT MAX(version) AS v FROM product_snapshot WHERE tenant_id=? AND sku=?",
        (tenant_id, sku))
    v = rows[0]["v"] if rows else None
    return int(v) if v is not None else None


def _applicant_purpose(applicant: dict) -> str:
    """business_ref.purpose 上的人类可读说明。

    刻意**不带 approved_amount**：那是申请方声称的数额，不是裁定金额（铁律 8，
    MAOS 只持观察与推断）。把它写进业务引用行，下游一眼看去就像本案的金额事实，
    而权威在政策核算那边。它只回填进出参，谁要用都得自己知道那是申报值。
    """
    parts = [f"{k}={applicant[k]}" for k in ("supplier_id", "po_no", "approver")
             if str(applicant.get(k) or "").strip()]
    tail = ("；" + " ".join(parts)) if parts else ""
    return f"供应链退款审批单引用（approved_amount 为申报值，非裁定金额）{tail}"


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
            "applicant_ref": "dict{supplier_id,po_no,approver,approved_amount,doc_no}"
                             "（可选；供应链退款的审批单，给了就落一条 business_ref，"
                             "doc_no 必填，approved_amount 只是申报值不是裁定金额；"
                             "不给则行为与不带此键时完全一致，也不替代 case_seed 的必填校验）",
        },
        output_schema={
            "case_draft": "dict（refund_case 那一行，biz_status=submitted）",
            "evidence_refs": "list[dict{evidence_id,kind,kind_raw,uri,digest,source}]"
                             "（kind 已归一化到 image/video/audio/document/attachment，"
                             "kind_raw 是提交方的原始声明，认不出的 kind 原样留在这里）",
            "applicant_ref": "dict（原样回填的审批单；入参没给则本键不出现）",
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
            "调用方 identity 必须同时授予该 skill，否则 PermissionDenied；"
            "kind 归一化只改写落库的取值，不改变证据集合的成员（成员判据仍是有没有 uri）"
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
        # 审批单是**并列**的可选项，不是 case_seed 的替代 —— 供应链退款照样要有
        # 订单号与版本，否则政策面锁不到版本。所以这一句在必填校验之后，且不放行任何缺字段。
        applicant = _applicant_of(payload)

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
        # `version` 是 T116 加的列：同一份证据（同 evidence_id）被重新提交时
        # ——换了更清晰的照片、补了原始文件——主键不变、内容变了，那是同一个对象的
        # 第二版。版本号**不进出参的 evidence dict**：那份 dict 的键集合有
        # `test_refund_intake_evidence_kind.py` 逐键钉着，加一个键就是把老调用方
        # 的形状改了。它落在库里与 `business_ref.object_version` 上，读得到。
        evidence = _evidence_of(signals)
        evidence_versions: dict[str, int] = {}
        for ev in evidence:
            version = case_pack.next_version(
                store, "customer_evidence", tenant_id=case["tenant_id"],
                case_id=case["case_id"], column="version",
                evidence_id=ev["evidence_id"])
            evidence_versions[ev["evidence_id"]] = version
            objects.execute(
                store,
                "INSERT OR REPLACE INTO customer_evidence (tenant_id, case_id, evidence_id,"
                " kind, uri, digest, submitted_at, version) VALUES (?,?,?,?,?,?,?,?)",
                (case["tenant_id"], case["case_id"], ev["evidence_id"], ev["kind"],
                 ev["uri"], ev["digest"], C.now_iso(), version),
            )

        # ---- DAG -> 业务对象：只存引用，不存副本 ------------------------------
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="refund_case", object_id=case["case_id"], purpose="受理建案")
        objects.attach_business_ref(
            store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
            object_type="order_snapshot", object_id=case["order_id"],
            object_version=case["order_version"], purpose="退款依据的订单快照")
        # 商品快照（T116）：质保期判定的依据在它的 `warranty_months` 上，
        # 所以它和订单快照一样是**裁定依据**，一样要带版本挂上去。
        # 查不到就不挂 —— 挂一条指不到任何行的引用，比不挂更坏（见
        # `attach_business_ref` 的 docstring：只存引用，读的时候一定读到当前那一份）。
        product_version = _product_version(store, case["tenant_id"], case["sku"])
        if product_version is not None:
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
                object_type="product_snapshot", object_id=case["sku"],
                object_version=product_version, purpose="质保期判定依据的商品快照")
        # 客户证据（T116）：一份一条引用，带各自的版本。
        for ev in evidence:
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
                object_type="customer_evidence", object_id=ev["evidence_id"],
                object_version=evidence_versions[ev["evidence_id"]],
                purpose=f"客户提交的{ev['kind']}证据")
        if applicant is not None:
            objects.attach_business_ref(
                store, plan_id=plan_id, task_id=task_id, tenant_id=case["tenant_id"],
                object_type="applicant_ref",
                object_id=str(applicant["doc_no"]).strip(),
                purpose=_applicant_purpose(applicant))

        issues = aggregated["issues"]
        out = {
            "case_draft": case,
            "evidence_refs": evidence,
            "issues": issues,
            "dedup": {"signals": len(signals), "issues": len(issues),
                      "merged": len(signals) - len(issues)},
            "aggregate_summary": aggregated["summary"],
            "invocation_id": invocation_id,
        }
        # 没给审批单就**不加这个键** —— 加一个 None 会让不带审批单的调用方出参形状
        # 也跟着变，向后兼容就不是逐键相同了。
        if applicant is not None:
            out["applicant_ref"] = applicant
        return out

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
