"""refund.evidence_check —— 随案证据够不够，以及它和物流/质检对不对得上。

## 为什么要有这个 skill

政策规则里的 `requires_evidence_kinds` / `min_evidence_count` 两个字段自写进契约起
就**没有任何消费方**（`docs/BACKLOG.md` 2026-09-02 那条）。没有消费方的判据字段
比没有更糟：规则作者以为自己声明的举证要求生效了，实际上一行代码都不读它 ——
这是一条静默失效，且只有在真发生纠纷时才暴露。本 skill 是它们的第一个消费方。

## 判定分两级，以及为什么必须分两级

演示底账 `scenarios/custom/ledger.json` 的三条规则（AS-001/002/003）body 里都没写
这两个字段，而底账只许新增不许改。只认规则声明的话，证据核验岗在演示里永远判
`not_required`，「证据缺」这条剧情根本跑不出来 —— 一个恒真的判据等于没有判据。

所以：

    规则声明优先；适用规则一条都没声明时，退到 `REASON_EVIDENCE_DEFAULTS`。

默认表是**公司缺省举证口径**（「质量问题和发错货至少要一张图」），政策可以覆盖它 ——
一旦有规则声明了，默认表整张退场，不做两级取并集。取并集会让「政策特意放宽」变得
无法表达：规则写了 `min_evidence_count: 0` 却仍被默认表拉回 1，规则作者无从察觉。
出参里的 `requirement_source` 把这件事显式说出来，读者不用推断这次是按哪一级判的。

## 方向：举证不足 → 免责条款不予适用，**不是**拒赔

`unmet[].direction` 恒为 `not_applied`，与 T74 的 `policy.py::_unmet` 同一口径。
「没交图就拒赔」是把举证责任倒置到客户身上，是本域最严重的做错方式。本 skill
只报「哪条判据没满足」，裁不裁、赔不赔归规则审核岗与财务岗。

## 边界

纯函数：不写库、不调模型、不碰附件字节（只看 `digest` 是否登记过，不去取 uri）。
入参就是全部事实 —— 同一份入参在任何机器任何时刻必须给同一份出参，
这也是本模块不用 `now()`、不把 `set` 直接落进出参的原因。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

from . import _common as C

#: `unmet[].requirement` 的取值域。与 T74 `policy.py` 的同名常量同值 ——
#: 那边是政策面按判据剔参，这边是证据面按判据报缺口，说的是同两个字段。
REQUIREMENT_REQUIRES_EVIDENCE_KINDS = "requires_evidence_kinds"
REQUIREMENT_MIN_EVIDENCE_COUNT = "min_evidence_count"

#: 方向只有这一个取值，写死是为了让方向在出参里**显式可读**（见模块 docstring）。
DIRECTION_NOT_APPLIED = "not_applied"

#: `requirement_source` 的三态：按规则判 / 按公司缺省口径判 / 两级都没有要求。
SOURCE_POLICY = "policy"
SOURCE_DEFAULT = "default"
SOURCE_NONE = "none"

#: `verdict` 四态。`missing` 与 `partial` 的区别是「一类都没有」和「有一些但不齐」。
VERDICT_COMPLETE = "complete"
VERDICT_PARTIAL = "partial"
VERDICT_MISSING = "missing"
VERDICT_NOT_REQUIRED = "not_required"

#: `consistency[].check` 的固定名字。调用方按名取，不靠下标 —— 下标会在加检查项时错位。
CHECK_SIGNED_BEFORE_REQUEST = "logistics_signed_before_request"
CHECK_QC_MATCHES_REASON = "qc_result_matches_reason"
CHECK_DIGEST_NONEMPTY = "evidence_digest_nonempty"


def _unmet(ref: str, requirement: str, required: Any, actual: Any) -> dict:
    """一条未满足的判据。形状照 T74 `policy.py::_unmet`，下游剔参时不必再改形状。"""
    return {
        "rule_ref": ref,
        "requirement": requirement,
        "required": required,
        "actual": actual,
        "direction": DIRECTION_NOT_APPLIED,
    }


def _int_or_none(raw: Any) -> int | None:
    """`min_evidence_count` 取整数值。**非数字一律忽略，不猜、不解析字符串**。

    `bool` 单独排掉（它是 `int` 的子类，`True` 会变成 1）—— 同 T74
    `policy.py::_positive_number` 的口径。规则里写了 `"3"` 这种字符串时忽略而不是
    转成 3：政策参数是机器可读字段，字符串数字说明规则录入有问题，静默转换会把
    录入错误变成一条看起来正常的判据。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _parse_ts(raw: str) -> datetime | None:
    """ISO8601 -> datetime，解析不了返回 None（**不抛**）。

    时间格式对不上是数据问题，不是本 skill 的失败：抛出去会让整个证据核验没有出参，
    而调用方真正需要的是「这一项没能核对」这条信息本身。
    """
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


@register_skill
class RefundEvidenceCheckSkill(Skill):
    """随案证据的核验器。零模型、零 IO、确定性。"""

    # ------------------------------------------------------------------ 词表
    # INTEGRATION-POINT: 并入 integrate/p9-t74-t79 后改 import _common.EVIDENCE_KINDS
    #: `customer_evidence.kind` 的规范值域，与 T77 的 `_common.EVIDENCE_KINDS` 同值。
    #: 政策规则里的 `requires_evidence_kinds` 写的就是这套词 —— 两边不同值的症状是
    #: 举证闸恒判「证据不足」且不报错。
    EVIDENCE_KINDS = ("image", "video", "audio", "document", "attachment")

    # INTEGRATION-POINT: 并入 integrate/p9-t74-t79 后改 import _common._KIND_ALIASES
    #: 常见写法 -> 规范值。前半段与 T77 的 `_KIND_ALIASES` **逐项同值**；后半段是本轨
    #: 增补的 MIME 型，因为进房间这条链路上的证据来自 Matrix 附件，声明的是 mimetype
    #: 而不是自由文本。**只认字面同义词，不做语义猜测**，覆盖不全是预期内的。
    KIND_ALIASES = {
        # ---- 与 T77 同值的自由文本词表 ----
        "img": "image", "photo": "image", "picture": "image", "screenshot": "image",
        "jpg": "image", "jpeg": "image", "png": "image",
        "mp4": "video", "mov": "video", "recording": "video",
        "voice": "audio", "mp3": "audio", "录音": "audio",
        "pdf": "document", "doc": "document", "docx": "document",
        "scan": "document", "扫描件": "document",
        # ---- 本轨增补：Matrix 附件送来的 mimetype ----
        "image/jpeg": "image", "image/png": "image",
        "video/mp4": "video",
        "audio/mpeg": "audio",
        "application/pdf": "document", "application/msword": "document",
    }

    #: MIME 大类兜底：`image/webp` 这种没列进表的子类型仍应归 image。
    #: 只对 MIME 生效（判据是带 `/`），**不按 uri 后缀猜** —— 后缀是传输细节，
    #: kind 是提交方的声明，声明优先且可审计（同 T77 `normalize_evidence_kind` 第 2 条）。
    KIND_MIME_PREFIXES = (("image/", "image"), ("video/", "video"), ("audio/", "audio"))

    #: **公司缺省举证口径，政策可覆盖**（见模块 docstring 第二节）。
    #: `no_reason_return` 刻意没有默认：无理由退货本来就不该要求客户举证。
    REASON_EVIDENCE_DEFAULTS: dict[str, tuple[tuple[str, ...], int]] = {
        "quality_defect": (("image",), 1),
        "wrong_item": (("image",), 1),
    }

    #: 质检结论与诉求类型的期望对应（小写比较）。对不上只报 `consistency` 里的一条
    #: 不一致，**不改 verdict** —— 交叉核对是给人看的线索，不是举证是否充分的判据。
    QC_EXPECT: dict[str, tuple[str, ...]] = {
        "quality_defect": ("defect", "fail"),
        "wrong_item": ("mismatch", "wrong_item"),
    }

    contract = SkillContract(
        name="refund.evidence_check",
        version="1.0.0",
        purpose="核验随案证据是否满足政策/缺省举证要求，并与物流签收、质检结论交叉核对（零模型，可复现）",
        input_schema={
            "case_seed": "dict（同 refund.intake 的 case_seed，取 reason_code）",
            "customer_evidence": "list[dict{evidence_id,kind,uri,digest,source}]（可空）",
            "rules": "list[dict{rule_no,version,title,ref,params}]（contrast.policy_view 的 rules 形状）",
            "order_facts": "dict{logistics:{carrier,tracking_no,signed_at}, qc_report:{report_no,result,issued_at}}（可空）",
            "requested_at": "str（ISO8601）",
        },
        output_schema={
            "items": "list[dict{evidence_id,kind,source,ok,note}]（逐份证据的核验）",
            "required_kinds": "list[str]（归一化后的排序并集）",
            "min_count": "int（min_evidence_count 的最大值，无则 0）",
            "gaps": "list[str]（人话：缺什么）",
            "verdict": "complete|partial|missing|not_required",
            "consistency": "list[dict{check,ok,note}]（物流时序 / 质检结论 / digest 非空）",
            "unmet": "list[dict{rule_ref,requirement,required,actual,direction}]",
            "requirement_source": "policy|default|none（这次按哪一级判的）",
            "invocation_id": "str",
        },
        preconditions=["case_seed"],
        depends_tools=[],
        failure_policy="escalate",
        max_retries=0,
        security_boundary=(
            "只读入参，不碰附件字节（只看 digest 是否登记，不去取 uri）；"
            "不写库不调模型；不裁定赔付，举证不足只报 direction=not_applied"
        ),
        reuse_note="任何「按规则声明的举证要求核验随案材料」的场景都可照此复用，换域只换默认表与交叉核对项",
        owner_roles=["refund_evidence"],
    )

    # ------------------------------------------------------------------ 归一化
    @classmethod
    def normalize_kind(cls, raw: Any) -> str:
        """把渠道送来的 kind 归一化到 `EVIDENCE_KINDS` 之一。

        三条口径与 T77 `_common.normalize_evidence_kind` 一致：认不出**不抛异常**、
        大小写不敏感、**不按 uri 后缀反推**。认不出归 `attachment` 而不是丢弃 ——
        按白名单挑会把没见过的证据类型静默丢掉，那比对不上更糟。
        """
        key = str(raw or "").strip().lower()
        if not key:
            return "attachment"
        if key in cls.EVIDENCE_KINDS:
            return key
        alias = cls.KIND_ALIASES.get(key)
        if alias is not None:
            return alias
        for prefix, kind in cls.KIND_MIME_PREFIXES:
            if key.startswith(prefix):
                return kind
        return "attachment"

    @classmethod
    def _normalized_kinds(cls, raw: Any) -> list[str]:
        """规则里声明的 kind 清单 -> 归一化、去重、排序。排序是为了出参逐字节可复现。"""
        if isinstance(raw, str):
            values: list[Any] = [raw]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            values = []
        return sorted({cls.normalize_kind(v) for v in values})

    # ------------------------------------------------------------------ 适用性
    @staticmethod
    def _rule_applies(params: dict, reason_code: str) -> bool:
        """这条规则适不适用于本次诉求类型。`applies_when.reason_code` 没写就是不限。

        语义与 `maos/flows/contrast.py::_applies_to` 相同，但**刻意自己写一份**：
        `flows` 已经单向 import `skills`，反向 import 会成环。这是十几行纯判断，
        重复的代价远小于把依赖方向搞成双向（记 DECISIONS）。
        """
        cond = params.get("applies_when")
        if not isinstance(cond, dict):
            return True
        codes = cond.get("reason_code")
        if not codes:
            return True
        return reason_code in [str(c) for c in codes]

    # ------------------------------------------------------------------ 主流程
    def run(self, payload: dict, ctx: SkillContext) -> dict:
        invocation_id = C.invocation_id_of(ctx)

        seed = payload.get("case_seed") or {}
        reason_code = str(seed.get("reason_code") or "").strip()
        evidence = payload.get("customer_evidence") or []
        rules = payload.get("rules") or []
        order_facts = payload.get("order_facts") or {}
        requested_at = str(payload.get("requested_at") or "").strip()

        items = self._items(evidence)
        # `ok` 为假的证据不计入「有什么」：digest 空意味着这份材料没真的落下来。
        have = sorted({it["kind"] for it in items if it["ok"]})
        count = sum(1 for it in items if it["ok"])

        declared = self._declared_requirements(rules, reason_code)
        source, required_kinds, min_count = self._requirement(declared, reason_code)
        unmet = self._unmet_rows(source, declared, reason_code,
                                 required_kinds, min_count, have, count)
        verdict = self._verdict(source, required_kinds, min_count, have, count)

        return {
            "items": items,
            "required_kinds": required_kinds,
            "min_count": min_count,
            "gaps": self._gaps(required_kinds, min_count, have, count, items),
            "verdict": verdict,
            "consistency": self._consistency(order_facts, requested_at, reason_code, items),
            "unmet": unmet,
            "requirement_source": source,
            "invocation_id": invocation_id,
        }

    # ------------------------------------------------------------------ 分步
    def _items(self, evidence: Any) -> list[dict]:
        """逐份证据的核验。**成员判据只有 digest 非空** —— 不按 kind 白名单挑。

        `note` 留住原始声明：归一化把 `photo` 变成 `image` 之后，出参里若不留痕，
        「当初是谁声明成什么」就查不回来了（T77 用 `kind_raw` 留痕，本 skill 不加
        契约外的键，改留在 note 里）。
        """
        out = []
        for raw in evidence if isinstance(evidence, (list, tuple)) else []:
            if not isinstance(raw, dict):
                continue
            declared_kind = raw.get("kind")
            kind = self.normalize_kind(declared_kind)
            ok = bool(str(raw.get("digest") or "").strip())
            evidence_id = str(raw.get("evidence_id") or "")
            if not ok:
                note = "digest 为空，未计入证据集合"
            elif str(declared_kind or "").strip().lower() != kind:
                note = f"原始声明 {declared_kind} 归一化为 {kind}"
            else:
                note = ""
            out.append({
                "evidence_id": evidence_id,
                "kind": kind,
                "source": str(raw.get("source") or ""),
                "ok": ok,
                "note": note,
            })
        return out

    def _declared_requirements(self, rules: Any, reason_code: str) -> list[tuple]:
        """适用规则里声明过举证判据的那些。返回 `[(rule_ref, kinds|None, min|None)]`。

        「声明过」按**键在不在**判，不按值真不真：`requires_evidence_kinds: []`
        是规则作者明确表达的「这条不要求特定类型」，与压根没写是两回事 ——
        前者应当让默认表退场，后者不应当。
        """
        out = []
        for rule in rules if isinstance(rules, (list, tuple)) else []:
            if not isinstance(rule, dict):
                continue
            params = rule.get("params")
            if not isinstance(params, dict):
                params = {}
            if not self._rule_applies(params, reason_code):
                continue
            has_kinds = REQUIREMENT_REQUIRES_EVIDENCE_KINDS in params
            has_min = REQUIREMENT_MIN_EVIDENCE_COUNT in params
            if not (has_kinds or has_min):
                continue
            ref = str(rule.get("ref") or rule.get("rule_no") or "")
            kinds = (self._normalized_kinds(params.get(REQUIREMENT_REQUIRES_EVIDENCE_KINDS))
                     if has_kinds else None)
            minv = (_int_or_none(params.get(REQUIREMENT_MIN_EVIDENCE_COUNT))
                    if has_min else None)
            out.append((ref, kinds, minv))
        return out

    def _requirement(self, declared: list[tuple],
                     reason_code: str) -> tuple[str, list[str], int]:
        """两级判定：规则声明优先，一条都没声明才退到默认表。见模块 docstring 第二节。"""
        if declared:
            kinds: set[str] = set()
            mins: list[int] = []
            for _ref, rule_kinds, minv in declared:
                if rule_kinds is not None:
                    kinds.update(rule_kinds)
                if minv is not None:
                    mins.append(minv)
            return SOURCE_POLICY, sorted(kinds), (max(mins) if mins else 0)

        fallback = self.REASON_EVIDENCE_DEFAULTS.get(reason_code)
        if fallback is None:
            return SOURCE_NONE, [], 0
        kinds_raw, minv = fallback
        return SOURCE_DEFAULT, sorted({self.normalize_kind(k) for k in kinds_raw}), int(minv)

    def _unmet_rows(self, source: str, declared: list[tuple], reason_code: str,
                    required_kinds: list[str], min_count: int,
                    have: list[str], count: int) -> list[dict]:
        """结构化的未满足判据。policy 级逐条规则报，default 级挂 `default:<reason_code>`。

        policy 级**逐条规则**报而不是按并集报：规则审核岗要知道是哪一条没满足，
        并集只能回答「有没有缺」。`source == none` 时恒空 —— 没有要求就没有未满足。
        """
        rows: list[dict] = []
        if source == SOURCE_POLICY:
            for ref, kinds, minv in declared:
                if kinds is not None and not set(kinds) <= set(have):
                    rows.append(_unmet(ref, REQUIREMENT_REQUIRES_EVIDENCE_KINDS,
                                       list(kinds), list(have)))
                if minv is not None and count < minv:
                    rows.append(_unmet(ref, REQUIREMENT_MIN_EVIDENCE_COUNT, minv, count))
        elif source == SOURCE_DEFAULT:
            ref = f"{SOURCE_DEFAULT}:{reason_code}"
            if required_kinds and not set(required_kinds) <= set(have):
                rows.append(_unmet(ref, REQUIREMENT_REQUIRES_EVIDENCE_KINDS,
                                   list(required_kinds), list(have)))
            if count < min_count:
                rows.append(_unmet(ref, REQUIREMENT_MIN_EVIDENCE_COUNT, min_count, count))
        return sorted(rows, key=lambda r: (r["rule_ref"], r["requirement"]))

    def _verdict(self, source: str, required_kinds: list[str], min_count: int,
                 have: list[str], count: int) -> str:
        """四态，**按此顺序判**（顺序本身是判据的一部分，换序会改语义）。"""
        if source == SOURCE_NONE:
            return VERDICT_NOT_REQUIRED
        if set(required_kinds) <= set(have) and count >= min_count:
            return VERDICT_COMPLETE
        if count == 0 or (required_kinds and not set(have) & set(required_kinds)):
            return VERDICT_MISSING
        return VERDICT_PARTIAL

    def _gaps(self, required_kinds: list[str], min_count: int, have: list[str],
              count: int, items: list[dict]) -> list[str]:
        """人话版的缺口清单。给房间里的岗位直接念，不用再翻 `unmet`。"""
        out = [f"缺少 {kind} 类证据" for kind in required_kinds if kind not in have]
        if count < min_count:
            out.append(f"证据份数不足：要求 {min_count} 份，实收 {count} 份")
        void = [it["evidence_id"] for it in items if not it["ok"]]
        if void:
            out.append(f"{len(void)} 份证据的 digest 为空，未计入：{'、'.join(void)}")
        return out

    def _consistency(self, order_facts: Any, requested_at: str, reason_code: str,
                     items: list[dict]) -> list[dict]:
        """三项交叉核对。**缺数据一律 ok=True 并在 note 里说清跳过的原因** ——
        判 False 会让「没这份数据」和「数据对不上」混成一个信号，房间里读不出区别。
        """
        facts = order_facts if isinstance(order_facts, dict) else {}
        return [
            self._check_signed_before_request(facts, requested_at),
            self._check_qc_matches_reason(facts, reason_code),
            self._check_digest_nonempty(items),
        ]

    def _check_signed_before_request(self, facts: dict, requested_at: str) -> dict:
        logistics = facts.get("logistics")
        signed_at = str((logistics or {}).get("signed_at") or "").strip() \
            if isinstance(logistics, dict) else ""
        if not signed_at or not requested_at:
            return {"check": CHECK_SIGNED_BEFORE_REQUEST, "ok": True,
                    "note": "物流签收时间或申请时间未提供，跳过"}
        signed, requested = _parse_ts(signed_at), _parse_ts(requested_at)
        if signed is None or requested is None:
            return {"check": CHECK_SIGNED_BEFORE_REQUEST, "ok": False,
                    "note": f"时间格式无法解析（签收 {signed_at}／申请 {requested_at}），未能核对"}
        if signed <= requested:
            return {"check": CHECK_SIGNED_BEFORE_REQUEST, "ok": True,
                    "note": f"签收 {signed_at} 早于申请 {requested_at}"}
        return {"check": CHECK_SIGNED_BEFORE_REQUEST, "ok": False,
                "note": f"签收 {signed_at} 晚于申请 {requested_at}，时序不成立"}

    def _check_qc_matches_reason(self, facts: dict, reason_code: str) -> dict:
        qc = facts.get("qc_report")
        result = str((qc or {}).get("result") or "").strip().lower() \
            if isinstance(qc, dict) else ""
        expect = self.QC_EXPECT.get(reason_code)
        if not result or expect is None:
            return {"check": CHECK_QC_MATCHES_REASON, "ok": True,
                    "note": "质检结论未提供，或该诉求类型没有期望的质检结论，跳过"}
        if result in expect:
            return {"check": CHECK_QC_MATCHES_REASON, "ok": True,
                    "note": f"质检结论 {result} 与诉求 {reason_code} 相符"}
        return {"check": CHECK_QC_MATCHES_REASON, "ok": False,
                "note": f"质检结论 {result} 与诉求 {reason_code} 不符"
                        f"（期望 {'／'.join(expect)}）"}

    @staticmethod
    def _check_digest_nonempty(items: list[dict]) -> dict:
        if not items:
            return {"check": CHECK_DIGEST_NONEMPTY, "ok": True,
                    "note": "未收到证据，无可核验的 digest"}
        void = [it["evidence_id"] for it in items if not it["ok"]]
        if not void:
            return {"check": CHECK_DIGEST_NONEMPTY, "ok": True,
                    "note": f"{len(items)} 份证据 digest 均非空"}
        return {"check": CHECK_DIGEST_NONEMPTY, "ok": False,
                "note": f"{len(void)} 份证据 digest 为空：{'、'.join(void)}"}
