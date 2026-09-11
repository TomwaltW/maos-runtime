"""五岗事实卡（`maos/roundtable/stages.py`）—— 全是规则代码，本文件一个模型都不注入。

本文件钉住的是 R1 的下半截：**事实卡里的每个数字都必须能在入参里找到**。
上半截（模型不许在事实卡之外编数字）在 `test_roundtable_team.py`。

数字白名单的口径：把入参 `json.dumps` 之后正则抽出的全部数字，加上几个显式的计数
（随案证据份数、命中规则条数、审批点个数）。用 `Decimal` 比较，`6800` / `6800.0` /
`6800.00` 视作同一个数 —— 事实卡把金额统一成两位小数，而底账里是 `6800.0`。
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

import pytest

from maos.domain.refund import projection
from maos.flows import custom_case
from maos.ingress.router import _load_run_requests, preflight
from maos.roundtable import stages
from maos.skills import registry

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"
ORDER = "ORD-2026-0001"
CASE_ID = "RC-ORD-2026-0001"
REQUESTED_AT = "2026-09-03T00:00:00+08:00"


# --------------------------------------------------------------------------
# 语料：真底账 + 真 build_case + 真 preflight，一个假件都不用
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ledger() -> dict:
    return custom_case.load(str(LEDGER), require_case=False)


def _case(ledger: dict, reason: str) -> tuple[dict, dict]:
    payload = _load_run_requests().build_case(ledger, {
        "order_id": ORDER, "reason": reason, "amount": None, "requested_at": REQUESTED_AT})
    return payload, preflight(payload)


@pytest.fixture(scope="module")
def approved(ledger: dict) -> tuple[dict, dict]:
    """质量问题 → `checked["decision"] == "approve"`。"""
    return _case(ledger, "quality_defect")


@pytest.fixture(scope="module")
def rejected(ledger: dict) -> tuple[dict, dict]:
    """七天无理由、第 63 天才申请 → 超窗，`decision == "reject"`。"""
    return _case(ledger, "no_reason_return")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(t) for t in re.findall(r"\d+(?:\.\d+)?", text or "")}


def _allowed(*objs: object, extra: tuple = ()) -> set[Decimal]:
    pool: set[Decimal] = set()
    for obj in objs:
        pool |= _numbers(json.dumps(obj, ensure_ascii=False, default=str))
    pool |= {Decimal(str(x)) for x in extra}
    return pool


def _hide(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """让注册表对指定 skill 名装作没有。

    不依赖「基线上恰好没装载」：那两个 skill 由别的轨新建，合进来之后基线就变了，
    而这几条测试要钉的是**没装载时的姿态**，不是当下的注册表内容。
    """
    real = registry.get

    def fake(name: str, version: str | None = None):
        return None if name in names else real(name, version)

    monkeypatch.setattr(registry, "get", fake)


# --------------------------------------------------------------------------
# 申请受理岗 / 规则审核岗
# --------------------------------------------------------------------------
def test_intake_facts_numbers_are_subset_of_inputs(approved: tuple[dict, dict]) -> None:
    payload, checked = approved
    facts, data = stages.facts_intake(payload, checked, 2)

    allowed = _allowed(payload, checked, extra=(2, len(checked["matched_rules"])))
    assert _numbers(facts) <= allowed, f"事实卡里出现了入参里没有的数字：{_numbers(facts) - allowed}"
    assert data["order_id"] == ORDER
    assert data["evidence_count"] == 2
    assert data["over_paid"] is False
    assert "质量问题" in facts and "SKU-BRG-6204" in facts


def test_intake_facts_warn_when_claimed_amount_is_over_paid(approved: tuple[dict, dict]) -> None:
    """申报高于实付要当场点出来 —— 核算会封顶，不说的话群里以为能退这么多。"""
    payload, checked = approved
    over = json.loads(json.dumps(payload))
    over["case"]["amount_claimed"] = 9999.0

    facts, data = stages.facts_intake(over, checked, 0)
    assert data["over_paid"] is True
    assert "封顶" in facts
    assert _numbers(facts) <= _allowed(over, checked, extra=(0, len(checked["matched_rules"])))


def test_policy_facts_numbers_are_subset_of_checked(approved: tuple[dict, dict]) -> None:
    _payload, checked = approved
    facts, data = stages.facts_policy(checked)

    allowed = _allowed(checked, extra=(len(checked["matched_rules"]),))
    assert _numbers(facts) <= allowed, f"多出来的数字：{_numbers(facts) - allowed}"
    assert set(data) == {"decision", "deciding_rule", "matched_rules", "elapsed_days",
                         "pinned_policy_version", "approver_role", "why"}
    assert "批准" in facts and "supervisor" in facts


# --------------------------------------------------------------------------
# 证据核验岗 / 风险反欺诈岗：未装载是主路径
# --------------------------------------------------------------------------
def test_evidence_facts_say_unavailable_when_skill_is_not_registered(
        approved: tuple[dict, dict], ledger: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _hide(monkeypatch, "refund.evidence_check")
    payload, checked = approved

    facts, data = stages.facts_evidence(payload, checked, ledger)
    assert data == {"verdict": "unavailable"}
    assert "未装载" in facts
    assert "refund.evidence_check" in facts


def test_risk_facts_say_unavailable_when_skill_is_not_registered(
        approved: tuple[dict, dict], ledger: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _hide(monkeypatch, "refund.risk_screen")
    payload, checked = approved

    facts, data = stages.facts_risk(payload, checked, ledger)
    assert data == {"level": "unavailable"}
    assert "未装载" in facts
    assert "refund.risk_screen" in facts


# --------------------------------------------------------------------------
# 财务执行岗（放行前）：核算预演
# --------------------------------------------------------------------------
def test_finance_preview_amount_equals_run_payload_amount(approved: tuple[dict, dict]) -> None:
    """**本轨最重要的接缝守卫**：预演金额必须等于真跑金额。

    两边都真跑，不 mock。预演走的是与 DAG 同一批 skill，只是库换成一次性副本 ——
    这条测试是「同一批」这个说法唯一的证据。它一红，房间里报的金额就不是将来会退的
    那个数，而两条路各自都自洽、都不报错。
    """
    payload, checked = approved
    _facts, data = stages.facts_finance_preview(payload, checked)
    real = custom_case.run_payload(payload, approve=True, verbose=False)

    assert data["preview_ran"] is True
    assert data["amount_approved"] == real["amount_approved"] == "6800.00"
    assert data["policy_version"] == real["policy_version_used"]


def test_finance_preview_does_not_run_when_decision_is_reject(
        rejected: tuple[dict, dict]) -> None:
    """裁定驳回就不预演。预演不看 `checked` 的话会报 6800，而真跑退 0.00。"""
    payload, checked = rejected
    assert checked["decision"] == "reject"

    facts, data = stages.facts_finance_preview(payload, checked)
    assert data["preview_ran"] is False
    assert data["amount_approved"] is None
    assert "裁定驳回，无需核算" in facts
    assert "6800" not in facts


def test_finance_preview_keeps_preview_wording_and_approve_hint(
        approved: tuple[dict, dict]) -> None:
    """R8：放行前只许说「预演」。措辞之外没有别的机制拦得住「已退款」这三个字。"""
    payload, checked = approved
    facts, _data = stages.facts_finance_preview(payload, checked)

    assert "核算预演" in facts
    assert "未落账" in facts
    assert f"/approve {CASE_ID}" in facts
    assert checked["approver_role"] in facts
    assert "已退款" not in facts and "已到账" not in facts


def test_finance_preview_failure_is_spoken_not_raised(
        approved: tuple[dict, dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """预演挂了照样发言。一个岗位在房间里凭空消失，比它说「我这儿出错了」更难排查。"""
    _hide(monkeypatch, "finance.settle")
    payload, checked = approved

    facts, data = stages.facts_finance_preview(payload, checked)
    assert data["preview_ran"] is False
    assert data["error"] and data["error"].startswith("finance.settle:")
    assert "核算预演失败：finance.settle" in facts
    assert f"/approve {CASE_ID}" in facts


# --------------------------------------------------------------------------
# 财务执行岗（放行后）：铁律 8 措辞
# --------------------------------------------------------------------------
def test_finance_result_does_not_claim_settled_without_settled_observation() -> None:
    """观察到了但不是 settled → 只许说「未确认到账」，不许出现别的「到账」。

    判据刻意写成「去掉『未确认到账』之后不含『到账』」，而不是「不含『已到账』」——
    后者放过了「已经到账了」「到账 1 笔」这类同义写法。
    """
    from maos.tests.test_ingress_router import RESULT_UNCONFIRMED

    facts, data = stages.facts_finance_result(RESULT_UNCONFIRMED)
    assert data["settled_observations"] == 0
    assert "未确认到账" in facts
    assert "到账" not in facts.replace("未确认到账", "")
    assert "已受理" in facts


def test_finance_result_says_settled_only_with_settled_observation() -> None:
    from maos.tests.test_ingress_router import RESULT_SETTLED

    facts, data = stages.facts_finance_result(RESULT_SETTLED)
    assert data["settled_observations"] == 1
    assert "到账" in facts
    assert set(data) == {"amount_approved", "policy_version_used", "rule_refs", "biz_status",
                         "settled_observations", "payment_observations", "human_exits",
                         "plan_state", "public_status"}


def test_finance_result_says_no_payment_when_there_is_no_observation() -> None:
    """一条观察都没有 ≠ 没到账，也 ≠ 到账了 —— 是根本没走到付款那一步。"""
    from maos.tests.test_ingress_router import RESULT_SETTLED

    result = {**RESULT_SETTLED, "settled_observations": 0, "payment_observations": [],
              "biz_status": "submitted"}
    facts, _data = stages.facts_finance_result(result)
    assert "未走到付款" in facts
    assert "到账" not in facts


# --------------------------------------------------------------------------
# 财务执行岗（放行后）：三态对外投影（T123）
# --------------------------------------------------------------------------
def test_finance_result_speaks_the_public_status_next_to_the_internal_one() -> None:
    """🔴 事实卡上**两句状态**：内部七态给主管看，三态投影给客户看。

    从前只有前一句，于是财务岗在群里念的和 `notify.py` 发给客户的是两套措辞，
    说的却是同一单 —— 那正是评委第二条要区分的那三档。
    """
    from maos.tests.test_ingress_router import RESULT_SETTLED

    result = {**RESULT_SETTLED, "public_status": projection.PUBLIC_SETTLED}
    facts, data = stages.facts_finance_result(result)

    assert data["public_status"] == projection.PUBLIC_SETTLED
    assert "业务状态：" in facts, "内部七态那句得留着 —— 主管要的是它"
    assert f"对客户口径：{projection.PUBLIC_SETTLED}" in facts

    # 念出去的那个字面值只能是契约 §D 的五个之一，不许自造第六句。
    spoken = [ln.split("：", 1)[1] for ln in facts.splitlines()
              if ln.startswith("对客户口径：")]
    assert spoken == [projection.PUBLIC_SETTLED]
    assert spoken[0] in projection.PUBLIC_STATUSES


def test_public_status_covers_every_literal_in_the_contract() -> None:
    """五个字面值逐一走一遍，都要原样念出来。

    逐个走而不是只测一个：这一句是「对外口径」的唯一出口，漏掉哪一档的症状
    是房间里那一档没有对外说法，而其余四档看起来都对。
    """
    from maos.tests.test_ingress_router import RESULT_SETTLED

    for literal in projection.PUBLIC_STATUSES:
        facts, _ = stages.facts_finance_result({**RESULT_SETTLED, "public_status": literal})
        assert f"对客户口径：{literal}" in facts


def test_finance_result_says_so_when_there_is_nothing_to_tell_the_customer() -> None:
    """🔴 三态投不出来（`submitted`、或 `approved` 还没落请求行）时照实说「还没到」。

    不回落到七态那句去凑一个对外说法 —— 那等于把内部进度当成对客户的交代。
    也不打空行：房间里一个空行读起来是「这一岗没话说」，而这一岗是有话说的。
    """
    from maos.tests.test_ingress_router import RESULT_SETTLED

    result = {**RESULT_SETTLED, "biz_status": "submitted", "settled_observations": 0,
              "payment_observations": [], "public_status": projection.NO_PUBLIC_STATUS}
    facts, data = stages.facts_finance_result(result)

    assert data["public_status"] == ""
    assert "对客户口径：尚未到可对外说的三态" in facts
    assert "" not in facts.splitlines(), "事实卡里不该多出空行"


def test_public_status_is_absent_when_upstream_did_not_project_one() -> None:
    """上游连这个键都没有（老的 result 形状）时，与空串同解 —— 不炸、不猜。"""
    from maos.tests.test_ingress_router import RESULT_UNCONFIRMED

    facts, data = stages.facts_finance_result(RESULT_UNCONFIRMED)

    assert data["public_status"] is None
    assert "对客户口径：尚未到可对外说的三态" in facts


def test_a_made_up_public_status_is_refused_not_repeated(
        caplog: pytest.LogCaptureFixture) -> None:
    """🔴 不是那五句的一律不念，并留 WARNING。

    这一行是「对客户口径」的最后一道闸。上游传一个自造措辞过来的症状是房间里
    多出第六句对外说法，而它长得和那五句一样像真的。
    """
    from maos.tests.test_ingress_router import RESULT_SETTLED

    with caplog.at_level(logging.WARNING, logger="maos.roundtable"):
        facts, data = stages.facts_finance_result(
            {**RESULT_SETTLED, "public_status": "钱已经打过去了"})

    assert data["public_status"] == "钱已经打过去了", "data 留原样，可追溯"
    assert "钱已经打过去了" not in facts
    assert "对客户口径：尚未到可对外说的三态" in facts
    assert any("不在契约" in r.getMessage() for r in caplog.records), caplog.text


def test_settled_wording_and_public_status_point_the_same_way() -> None:
    """🔴 「已观察到账」与「对客户口径：退款已到账」同向，且都以观察行为准（铁律 8）。

    圆桌自己**不从 `biz_status` 推三态**：`public_status` 是 `custom_case._observe()`
    看着 `payment_observation` 投出来的，本函数只是把它留住。所以没有 settled 观察
    的那一单，两句话都不许出现「到账」——下面后半段钉的就是这个。
    """
    from maos.tests.test_ingress_router import RESULT_SETTLED, RESULT_UNCONFIRMED

    settled, _ = stages.facts_finance_result(
        {**RESULT_SETTLED, "public_status": projection.PUBLIC_SETTLED})
    assert "已观察到账" in settled
    assert f"对客户口径：{projection.PUBLIC_SETTLED}" in settled

    # 没有 settled 观察：付款那句说「未确认到账」，对外那句一个「到账」都没有。
    # 判据同 `:208` 那条 —— 去掉「未确认到账」之后整张卡不含「到账」。
    unconfirmed, _ = stages.facts_finance_result(
        {**RESULT_UNCONFIRMED, "public_status": projection.PUBLIC_FILED})
    assert "未确认到账" in unconfirmed
    assert "到账" not in unconfirmed.replace("未确认到账", "")
    assert f"对客户口径：{projection.PUBLIC_FILED}" in unconfirmed


def test_preview_stages_do_not_claim_a_public_status(approved: tuple[dict, dict]) -> None:
    """放行**前**的两张卡一个字都不提对外口径 —— 那时候还没有观察，投不出三态。

    预演阶段硬说一个对外口径就是编：`facts_finance_preview` 手上只有核算预演，
    `payment_observation` 表还是空的。
    """
    payload, checked = approved
    preview, _ = stages.facts_finance_preview(payload, checked)

    assert "对客户口径" not in preview
    for literal in projection.PUBLIC_STATUSES:
        assert literal not in preview


# --------------------------------------------------------------------------
# 一张表：每岗只汇总一次
# --------------------------------------------------------------------------
def test_sheet_facts_summarize_rows_once_per_stage(approved: tuple[dict, dict],
                                                   rejected: tuple[dict, dict]) -> None:
    ok_payload, ok_checked = approved
    no_payload, no_checked = rejected
    rows = [
        {"line": 2, "order_id": ORDER, "reason_raw": "质量问题", "payload": ok_payload,
         "checked": ok_checked, "error": None, "problems": [], "warnings": []},
        {"line": 3, "order_id": ORDER, "reason_raw": "无理由", "payload": no_payload,
         "checked": no_checked, "error": None, "problems": [], "warnings": ["日期未填"]},
        # 表格填错的行：`router._sheet_rows` 的 docstring 说死了 payload / checked /
        # error **三者都是 None** —— 它压根没走到预检。原来这一行把 error 也填上了，
        # 于是 invalid 与 problem_rows 恰好都等于 1，两张事实卡取错计数器也测不出来。
        {"line": 4, "order_id": "ORD-9999", "reason_raw": "坏了", "payload": None,
         "checked": None, "error": None,
         "problems": ["底账里没有订单 ORD-9999"], "warnings": []},
        # 进了预检才抛错的行：表本身填得对，problems 是空的。这一态与上一态必须分开摆，
        # 合成一行的话「填错」与「预检失败」两个计数就再也分不出对错。
        {"line": 5, "order_id": ORDER, "reason_raw": "质量问题", "payload": ok_payload,
         "checked": None, "error": "预检失败：底账政策视图读不到",
         "problems": [], "warnings": []},
    ]
    stats = stages.sheet_stats(rows)
    assert (stats["total"], stats["valid"], stats["invalid"]) == (4, 2, 1)
    assert stats["problem_rows"] == 1
    # 三态互斥且穷尽 —— 加不平就说明有一整类行没人数，正是「填错 0 行」那个 bug。
    assert stats["valid"] + stats["invalid"] + stats["problem_rows"] == stats["total"]
    assert (stats["approve"], stats["reject"]) == (1, 1)
    assert stats["pending_case_ids"] == [CASE_ID]

    allowed = _allowed(rows, extra=tuple(v for v in stats.values() if isinstance(v, int)))
    for build in (stages.facts_sheet_intake, stages.facts_sheet_policy,
                  stages.facts_sheet_evidence, stages.facts_sheet_risk,
                  stages.facts_sheet_finance):
        facts, data = build(rows)
        assert facts.strip(), f"{build.__name__} 一句话都没说"
        assert data["total"] == 4
        extra = _numbers(facts) - allowed
        assert not extra, f"{build.__name__} 的汇总里出现了 rows 里没有的数字：{extra}"


def test_all_rows_misfiled_is_not_reported_as_zero_problems() -> None:
    """整表填错时，两张卡不许把「填错」念成 0。

    真房间 2026-09-04 的现场：boss 拖进一张 5 行的表，「诉求类型」那列整列是空的
    （表头写成了别名表里没有的名字），5 行全部停在解析阶段、一行都没进预检。
    于是 `valid=0`、`invalid=0`，而受理岗那句「填错的 N 行」取的正是 `invalid` ——
    房间里念出来就是「5 行里能建案 0 行，填错 0 行，但有填写问题 5 行」，
    规则审核岗接着念「另有 0 行因填写问题没有裁定」。两句话同用「填写问题」四个字、
    背后却是两个计数器，模型受「一个数字都不许改」约束，只能如实报「数字对不上」，
    后面三岗跟着一路推诿 —— 五岗全程没说错一个字，错的是事实卡取数。
    """
    rows = [{"line": i, "order_id": f"ORD-2026-000{i}", "reason_raw": "",
             "payload": None, "checked": None, "error": None,
             "problems": ["诉求类型不能空 —— 不知道为什么退，就套不上任何一条政策"],
             "warnings": []} for i in range(2, 7)]

    stats = stages.sheet_stats(rows)
    assert (stats["total"], stats["valid"], stats["invalid"]) == (5, 0, 0)
    assert stats["problem_rows"] == 5

    intake, _ = stages.facts_sheet_intake(rows)
    policy, _ = stages.facts_sheet_policy(rows)
    # 不钉措辞、钉「填错」这个语义旁边的数：它必须是 5，不许是 0。
    # 两个方向都认 —— 受理岗写「填错…5 行」，规则岗写「5 行因…填错」。
    near_5 = re.compile(r"填错[^0-9]{0,12}5 行|5 行[^0-9]{0,12}填错")
    assert near_5.search(intake), intake
    assert near_5.search(policy), policy
    # 两张卡说的是同一件事，就不许出现一张说 5、另一张说 0。
    assert "0 行因" not in policy and "填错的 0 行" not in intake
# --------------------------------------------------------------------------
# 事实卡要说清「哪一行、为什么」，不能只报数
# --------------------------------------------------------------------------
def test_sheet_facts_spell_out_which_row_and_why(approved: tuple[dict, dict],
                                                 rejected: tuple[dict, dict]) -> None:
    """填错的行与驳回的单，都要逐条写出行号 / 单号 / 原因。

    真房间 2026-09-10 的现场：50 行表跑完，五岗只念得出「驳回 4 行」，
    老板追问「哪四单不能过」，一岗都答不上来 —— 判据本来就在 `checked["why"]` 里，
    只是没进事实卡。而 R1 的约束是「模型只许复述事实卡」：卡里没有的它编不出来，
    也**不该**编。所以这是事实卡的缺口，不是文案问题。
    """
    ok_payload, ok_checked = approved
    no_payload, no_checked = rejected
    rows = [
        {"line": 2, "order_id": ORDER, "reason_raw": "质量问题", "payload": ok_payload,
         "checked": ok_checked, "error": None, "problems": [], "warnings": []},
        {"line": 3, "order_id": ORDER, "reason_raw": "无理由", "payload": no_payload,
         "checked": no_checked, "error": None, "problems": [],
         "warnings": ["申报 9999 超过订单实付 6800，核算时会按实付封顶"]},
        {"line": 4, "order_id": "ORD-9999", "reason_raw": "坏了", "payload": None,
         "checked": None, "error": None,
         "problems": ["底账里没有订单 ORD-9999"], "warnings": []},
        {"line": 5, "order_id": ORDER, "reason_raw": "质量问题", "payload": ok_payload,
         "checked": None, "error": "预检失败：底账政策视图读不到",
         "problems": [], "warnings": []},
    ]

    intake, _ = stages.facts_sheet_intake(rows)
    # 填错的行：行号、单号、原因，三样都要在同一行上。
    assert "· 第 4 行 ORD-9999（坏了）：底账里没有订单 ORD-9999" in intake
    # 「进了预检才失败」与「表就填错了」分开摆：后者要人改表，前者不是人的错。
    assert "· 第 5 行" in intake and "预检失败：底账政策视图读不到" in intake
    # 不阻断的提示也要点名 —— 只说「1 行带提示」等于没说，而封顶会改变到账金额。
    assert "· 第 3 行" in intake and "按实付封顶" in intake

    policy, _ = stages.facts_sheet_policy(rows)
    why = str(no_checked.get("why") or "").strip()
    assert why, "语料本身要带判据，否则这条测试什么都没钉住"
    # 判据**照搬**，不在事实卡这一层改写：改写一次，房间里的说法就和回帖对不上了。
    assert f"· 第 3 行 {ORDER}（无理由）：{why}" in policy

    # 下游三岗对**自己范围内的单**逐条说自己的结论（证据缺什么、风险几档、预演多少），
    # 但规则岗的判据一个字都不转抄、驳回的单不再点名、指路句不许有
    # （`SYSTEM_TMPL` 群内发言规则 1、2）。原来这里钉的是「下游连单号都不许提」——
    # 那是三岗事实卡只有一行计数时的权宜（2026-09-10 上午的房间），逐单真跑之后
    # 单号是本岗自己结论的一部分，不再是转抄。
    for build in (stages.facts_sheet_evidence, stages.facts_sheet_risk,
                  stages.facts_sheet_finance):
        facts, _ = build(rows)
        assert why not in facts, f"{build.__name__} 把规则岗的判据又抄了一遍"
        assert "见规则审核岗" not in facts, f"{build.__name__} 还在往规则岗指路"
    evidence, _ = stages.facts_sheet_evidence(rows)
    finance, _ = stages.facts_sheet_finance(rows)
    for facts, name in ((evidence, "证据岗"), (finance, "财务岗")):
        assert "第 3 行" not in facts, f"{name} 把驳回的单又点了一遍名"
        assert f"· 第 2 行 {ORDER}（质量问题）：" in facts, f"{name} 没对自己范围内的单逐条说结论"


def test_long_lists_are_capped_and_point_back_to_the_reply() -> None:
    """清单有盖子：Matrix 一条消息装不下 50 行，读的人也读不完。

    截断**说出来**并指回申请表回帖（那份是逐行全的），不报截掉了几行 ——
    那个差值不在入参里，报出去就破了「数字都是 rows 的子集」这条。
    """
    rows = [{"line": i, "order_id": f"ORD-2026-{i:04d}", "reason_raw": "质量问题",
             "payload": None, "checked": None, "error": None,
             "problems": ["底账里没有这个订单"], "warnings": []}
            for i in range(2, 2 + stages.ROW_CAP + 5)]

    intake, _ = stages.facts_sheet_intake(rows)
    listed = [ln for ln in intake.splitlines() if ln.startswith("  · 第 ")]
    assert len(listed) == stages.ROW_CAP
    assert "余下的同样在申请表回帖里逐行写着" in intake
    # 计数照报：截断的是清单，不是数字。
    assert f"表格填错没进预检 {stages.ROW_CAP + 5} 行" in intake


def test_pending_line_stops_listing_case_ids_when_there_are_many() -> None:
    """待放行 46 单时不逐个念 case_id：连成一行 900 多字符，房间里读不完。

    这一句现在**只有规则岗念**（财务岗转抄一遍就是复述上游裁定，见群内发言规则 2），
    句尾也不许再带「这里不重复」那类交代 —— 那是关于协作本身的元话语（规则 3）。
    """
    few = stages._pending_line({"pending_case_ids": ["RC-A", "RC-B"]})
    assert "RC-A、RC-B" in few

    many = [f"RC-ORD-{i}" for i in range(stages.ROW_CAP + 1)]
    long = stages._pending_line({"pending_case_ids": many})
    assert f"待放行 {len(many)} 单" in long
    assert "RC-ORD-0" not in long, "超过盖子就不许逐个念"
    assert "申请表回帖" in long, "不念清单，就得说清去哪儿看"
