"""收案面：证据 kind 归一化与供应链审批单引用（T77）。

本文件钉的是一条**静默失效**：政策规则里写 `requires_evidence_kinds: ["image"]`，
而渠道送进来的 kind 是自由文本（photo / img / screenshot / 扫描件），落库原样透传就
一条都对不上 —— 举证闸永远判「证据不足」，且不报任何错。改动前后表面全绿，语义全错，
只有交一张图与不交图得到同一个结论时才看得见。

两类断言：

  · **归一化**：落库的 `kind` 收敛到五个规范值，而**证据集合的成员一条不增不减**
    （归一化不是白名单过滤 —— 按 kind 挑会把没见过的证据类型静默丢掉）；
  · **审批单**：供应链退款的审批单落成 `business_ref` 的一条引用（不扩表），
    且申报金额不许变成裁定金额（铁律 8：MAOS 只持观察与推断）。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity
from maos.core.store import SqliteStore
from maos.domain.refund import objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.skills.builtin.refund import REFUND_SKILLS
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker

#: `b35c618` 的 `refund.intake` 出参键集合。本轮只允许**加键**（applicant_ref），
#: 不给审批单时这一集合必须原样保持 —— 少一个键就是把老调用方打断了。
BASELINE_OUT_KEYS = {"case_draft", "evidence_refs", "issues", "dedup",
                     "aggregate_summary", "invocation_id"}

#: `b35c618` 的每条证据引用的键集合。本轮新增 `kind_raw`（§5.3 已同步 output_schema）。
BASELINE_EV_KEYS = {"evidence_id", "kind", "uri", "digest", "source"}

APPLICANT = {
    "supplier_id": "SUP-0007",
    "po_no": "PO-2026-0331",
    "approver": "王工",
    "approved_amount": "12800.00",
    "doc_no": "SCR-2026-0912",
}

IDENTITY = AgentIdentity(
    agent_id="test-t77", role="test_refund_intake",
    duty="测试夹具：授权退款域全部 skill",
    allowed_skills=frozenset(set(REFUND_SKILLS) | {"issue.aggregate"}),
    model_tier=Tier.LIGHT,
)


@pytest.fixture
def store():
    st = SqliteStore()
    st.init_schema()
    s6.seed_domain(st)
    return st


@pytest.fixture
def invoker(store):
    return SkillInvoker(IDENTITY, store)


def _extras() -> dict:
    return {"plan_id": "plan-t77", "task_id": "task-t77", "trace_id": "trace-t77",
            "attempt": 1}


def _seed() -> dict:
    return {"tenant_id": s6.TENANT_ID, "case_id": s6.CASE_ID,
            "channel_id": s6.CHANNEL_ID, "order_id": s6.ORDER_ID,
            "order_version": s6.ORDER_VERSION, "sku": s6.SKU,
            "reason_code": "quality_defect", "amount_claimed": s6.AMOUNT_CLAIMED}


def _sig(kind: str, uri: str, evidence_id: str, title: str = "收到的轴承有锈蚀") -> dict:
    return {"source": "客户上传", "kind": kind, "severity": "major",
            "title": title, "detail": "客户上传的实物证据",
            "uri": uri, "evidence_id": evidence_id}


def _intake(invoker, signals, **extra_payload):
    payload = {"signals": signals, "case_seed": _seed()}
    payload.update(extra_payload)
    res = invoker.invoke("refund.intake", payload, extras=_extras())
    assert res.status == "ok", res.error
    return res.output


def _evidence_rows(store) -> list[dict]:
    return objects.query(
        store, "SELECT * FROM customer_evidence WHERE case_id=? ORDER BY evidence_id",
        (s6.CASE_ID,))


# ======================================================================
# 归一化：本轨买的东西
# ======================================================================
def test_photo_normalizes_to_image(invoker, store):
    """政策写 image、信号写 photo —— 落库必须是 image，举证闸才数得到。

    这是本轨最要紧的一条：改动前这条证据在库里是 `photo`，
    `requires_evidence_kinds:["image"]` 一条都匹配不上，闸恒判「证据不足」且不报错。
    """
    out = _intake(invoker, [_sig("photo", "oss://a/rust-01.jpg", "ev-01")])

    assert out["evidence_refs"][0]["kind"] == "image"
    assert out["evidence_refs"][0]["kind_raw"] == "photo", "提交方的原始声明要留痕"

    rows = _evidence_rows(store)
    assert [r["kind"] for r in rows] == ["image"], "库里落的是规范值 —— 政策面按它计数"
    assert sum(1 for r in rows if r["kind"] == "image") == 1


def test_unknown_kind_is_kept_not_dropped(invoker, store):
    """认不出的 kind 归 attachment 并留痕，**这条证据仍在证据集合里**。

    归一化不是白名单过滤。按 kind 白名单挑会把没见过的证据类型静默丢掉，
    那比对不上更糟 —— 丢掉的证据连审计都看不见。
    """
    out = _intake(invoker, [_sig("客户自制的验货视频号", "oss://a/odd-01.bin", "ev-01")])

    refs = out["evidence_refs"]
    assert len(refs) == 1, "认不出的证据不许被丢掉"
    assert refs[0]["kind"] == "attachment"
    assert refs[0]["kind_raw"] == "客户自制的验货视频号", "原值必须原样保留"
    assert [r["uri"] for r in _evidence_rows(store)] == ["oss://a/odd-01.bin"]


@pytest.mark.parametrize("raw,expected", [
    ("  PHOTO  ", "image"), ("Img", "image"), ("SCREENSHOT", "image"),
    ("MP4", "video"), (" recording", "video"),
    ("VOICE", "audio"), ("录音", "audio"),
    ("PDF", "document"), ("扫描件 ", "document"), (" docx", "document"),
    ("image", "image"), ("attachment", "attachment"),
])
def test_kind_normalization_ignores_case_and_whitespace(raw, expected):
    """大小写不敏感、去首尾空白；规范值本身原样通过。"""
    assert C.normalize_evidence_kind(raw) == expected


def test_uri_suffix_does_not_override_declared_kind(invoker, store):
    """`.jpg` 结尾但声明 document —— 以声明为准，不按后缀反推。

    后缀是传输细节（谁都能把 PDF 存成 .jpg），kind 是提交方的声明：声明优先，
    且可审计。按后缀猜会让「库里为什么是这个 kind」变成一句解释不清的启发式。
    """
    out = _intake(invoker, [_sig("document", "oss://a/scan-01.jpg", "ev-01")])

    assert out["evidence_refs"][0]["kind"] == "document"
    assert [r["kind"] for r in _evidence_rows(store)] == ["document"]


def test_normalization_does_not_drop_any_evidence(invoker, store):
    """归一化前后证据条数完全相同 —— 成员判据仍然只是「有没有 uri」。"""
    signals = [
        _sig("photo", "oss://a/1.jpg", "ev-01"),
        _sig("某种没见过的东西", "oss://a/2.bin", "ev-02", title="外包装箱破损"),
        _sig("录音", "oss://a/3.mp3", "ev-03", title="客服通话录音"),
        {"source": "工单系统", "kind": "ticket", "title": "无附件的工单",
         "detail": "没有 uri，不是证据"},
    ]
    out = _intake(invoker, signals)

    with_uri = [s for s in signals if s.get("uri")]
    assert len(out["evidence_refs"]) == len(with_uri) == 3, "带 uri 的一条都不许少"
    assert len(_evidence_rows(store)) == 3
    assert {r["kind"] for r in _evidence_rows(store)} == {"image", "attachment", "audio"}


def test_normalized_kinds_stay_within_the_closed_value_domain(invoker, store):
    """落库的 kind 一定落在五个规范值之内 —— 政策面按这套值域写规则。"""
    signals = [_sig(k, f"oss://a/{i}.bin", f"ev-{i:02d}")
               for i, k in enumerate(["jpg", "mov", "mp3", "pdf", "谁也没见过"], start=1)]
    _intake(invoker, signals)

    kinds = {r["kind"] for r in _evidence_rows(store)}
    assert kinds <= set(C.EVIDENCE_KINDS), f"落库出现了值域外的 kind：{kinds}"
    assert kinds == {"image", "video", "audio", "document", "attachment"}


# ======================================================================
# 供应链审批单：不扩表，走 business_ref
# ======================================================================
def test_output_without_applicant_ref_stays_identical_to_baseline(invoker, store):
    """不给审批单 —— 出参与 `b35c618` 逐键一致（除本轮约定新增的 kind_raw）。

    `applicant_ref` 是可选项，老调用方一个字都不用改。
    """
    out = _intake(invoker, s6.SIGNALS)

    assert set(out) == BASELINE_OUT_KEYS, "不给审批单时出参不许多出任何键"
    assert "applicant_ref" not in out
    for ev in out["evidence_refs"]:
        assert set(ev) == BASELINE_EV_KEYS | {"kind_raw"}
        assert ev["kind"] == ev["kind_raw"] == "image", "场景 6 本就写的是规范值"
    refs = objects.list_business_refs(store, plan_id="plan-t77", task_id="task-t77")
    assert {r["object_type"] for r in refs} == {"refund_case", "order_snapshot"}


def test_applicant_ref_is_attached_as_business_ref(invoker, store):
    """给了审批单 —— 落一条 business_ref 引用并原样回填出参，一张表都不新增。"""
    out = _intake(invoker, s6.SIGNALS, applicant_ref=dict(APPLICANT))

    assert out["applicant_ref"] == APPLICANT, "审批单原样回填，不做任何改写"

    refs = objects.list_business_refs(store, plan_id="plan-t77", task_id="task-t77")
    rows = [r for r in refs if r["object_type"] == "applicant_ref"]
    assert len(rows) == 1, "审批单落成一条业务引用"
    assert rows[0]["object_id"] == APPLICANT["doc_no"], "引用指向审批单号"
    assert rows[0]["tenant_id"] == s6.TENANT_ID


def test_approved_amount_does_not_become_the_settled_amount(invoker, store):
    """申报金额不是裁定金额（铁律 8）—— 它不许出现在任何金额字段里。

    审批单上的 `approved_amount` 是**申请方声称的数额**，权威在政策核算那边。
    一旦它顺着 case 往下走，MAOS 就把一个外部声称写成了自己的事实。
    """
    applicant = dict(APPLICANT, approved_amount="99999.00")
    out = _intake(invoker, s6.SIGNALS, applicant_ref=applicant)

    case = out["case_draft"]
    assert float(case["amount_claimed"]) == float(s6.AMOUNT_CLAIMED), \
        "建案金额只来自 case_seed"
    assert "99999" not in str(case), "申报金额不许渗进 refund_case 任何一列"

    refs = objects.list_business_refs(store, plan_id="plan-t77", task_id="task-t77")
    assert all("99999" not in str(r) for r in refs), \
        "申报金额也不许落进业务引用行 —— 落进去下游一眼看去就像本案的金额事实"


def test_applicant_ref_without_doc_no_is_rejected(invoker):
    """审批单缺单号 —— 报错，不猜缺省值（认不出就报错）。"""
    res = invoker.invoke("refund.intake", {
        "signals": s6.SIGNALS, "case_seed": _seed(),
        "applicant_ref": {k: v for k, v in APPLICANT.items() if k != "doc_no"},
    }, extras=_extras())

    assert res.status != "ok"
    assert "doc_no" in str(res.error)


def test_applicant_ref_does_not_bypass_case_seed_required_fields(invoker):
    """有审批单也照样要过 case_seed 的必填校验 —— 两者并列，不是替代。"""
    seed = _seed()
    seed.pop("order_id")
    res = invoker.invoke("refund.intake", {
        "signals": s6.SIGNALS, "case_seed": seed, "applicant_ref": dict(APPLICANT),
    }, extras=_extras())

    assert res.status != "ok"
    assert "order_id" in str(res.error)
