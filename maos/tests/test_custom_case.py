"""自定义 case 入口（`scripts/run_case.py` / `maos/flows/custom_case.py`）的守卫。

这条入口卖的是一句话：**改数据不改代码，结论跟着数据走**。这句话需要东西钉住，
否则哪天金额被写进代码里当常量，样例照样跑绿，而卖点已经没了。

五条，各钉一件：

  1. 样例 case 跑得通，且 `settled` 是**观察**写的（settled 观察至少一条）——
     铁律 8：权威状态归网关，问不出就什么都不写；
  2. 只改政策 `body` 的 `refund_ratio` / `deduct_fee`，核准金额跟着变
     （6800.00 -> 5390.00），代码一个字节没动；
  3. 超窗的诉求裁定 reject，DAG **不排核算**（0 元分录会让下游误以为核算过了）；
  4. 网关问不出终态时，`biz_status` 停在 `gateway_accepted`、settled 观察 0 条 ——
     这是设计，不是失败；
  5. 缺表 / 缺 `case` 块当场报 `CaseFileError`，不静默按缺省值跑绿。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from maos.flows import custom_case as cc
from maos.flows.custom_case import (
    CaseFileError,
    RoomNotConnected,
    load,
    room_degradation,
    run_payload,
)

SAMPLE = Path(__file__).resolve().parents[2] / "scenarios" / "custom" / "refund-case.json"


@pytest.fixture(scope="module")
def sample() -> dict:
    return load(SAMPLE)


def _retuned(payload: dict, *, ratio: str, fee: str) -> dict:
    """把每条政策的退款比例与手续费换掉，其余一字不动。"""
    out = copy.deepcopy(payload)
    for rule in out["policy_rule"]:
        body = json.loads(rule["body"])
        body["refund_ratio"] = ratio
        body["deduct_fee"] = fee
        rule["body"] = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return out


def test_sample_case_settles_from_observation(sample):
    row = run_payload(copy.deepcopy(sample), verbose=False)

    assert row["decision"] == "approve"
    assert row["plan_state"] == "DONE"
    assert row["amount_approved"] == "6800.00"
    assert row["pinned_policy_version"] == 1, "订单锁 v1，库里那条 AS-001@v2 不该被用上"
    assert "AS-001@v2" not in row["matched_rules"]
    # settled 只可能由 payment.observe 写入：没有观察却是 settled，就是把外部状态
    # 写死为终态（铁律 8）。
    assert row["biz_status"] == "settled"
    assert row["settled_observations"] >= 1
    assert row["payment_observations"][-1]["poll_count"] >= 1, (
        "终态必须是问出来的，poll_count 是这条论证仅有的可核字段")


def test_amount_follows_policy_body_not_code(sample):
    row = run_payload(_retuned(sample, ratio="0.8", fee="50"), verbose=False)

    # 6800 * 0.8 - 50 = 5390.00。这个数只能来自政策数据 —— 代码里没有 0.8，也没有 50。
    assert row["amount_approved"] == "5390.00"
    assert row["decision"] == "approve"


def test_out_of_window_claim_is_rejected_and_skips_finance(sample):
    payload = copy.deepcopy(sample)
    payload["case"]["reason_code"] = "no_reason_return"     # AS-001 的窗口才管得着
    payload["requested_at"] = "2026-08-15T09:00:00+00:00"   # 付款 2026-07-01，第 44 天

    row = run_payload(payload, verbose=False)

    assert row["decision"] == "reject"
    assert row["deciding_rule"] == "AS-001@v1"
    assert row["no_reason_days"] == 30
    roles = [t["role"] for t in row["tasks"]]
    assert not any("finance" in r for r in roles), (
        "裁定 reject 就不该排核算：一份 0 元分录会让下游误以为核算过了")
    assert row["amount_approved"] == "0.00"
    assert row["settled_observations"] == 0


def test_gateway_without_terminal_answer_writes_nothing(sample):
    payload = copy.deepcopy(sample)
    # settle_after 高过观察侧的轮询上限 = 这一笔怎么问都问不出终态。
    payload["gateway"] = {"settle_after": 99, "fail_with": "ACQ.SYSTEM_ERROR"}

    row = run_payload(payload, verbose=False)

    assert row["biz_status"] == "gateway_accepted", "问不出终态就停在这里，不许推断成 settled"
    assert row["settled_observations"] == 0


def test_malformed_input_is_refused_loudly(tmp_path, sample):
    missing_table = copy.deepcopy(sample)
    missing_table.pop("policy_rule")
    p1 = tmp_path / "no-policy.json"
    p1.write_text(json.dumps(missing_table, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CaseFileError, match="policy_rule"):
        load(p1)

    no_case = copy.deepcopy(sample)
    no_case.pop("case")
    p2 = tmp_path / "no-case.json"
    p2.write_text(json.dumps(no_case, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CaseFileError, match="case"):
        load(p2)

    with pytest.raises(CaseFileError, match="找不到"):
        load(tmp_path / "nope.json")


def test_room_degradation_reads_the_bus_not_the_wish():
    """降级判据照 `hiclaw/room_demo.py` 的既有口径：deps/connect 拦，env 不拦。"""

    class _Deps:
        degrade_reason, degrade_detail = "deps", "MatrixDepMissing: 解释器没装 matrix-nio"

    class _Connected:
        degrade_reason, degrade_detail = "", ""

    class _EnvMissing:                       # 四个必填没配齐 = 明确的降级意图
        degrade_reason, degrade_detail = "env", ""

    assert room_degradation(_Deps())[0] == "deps"
    assert room_degradation(_Connected()) == ("", "")
    assert room_degradation(_EnvMissing()) == ("", "")
    assert room_degradation(object())[0] == "no-bus", "压根不是 Matrix 总线也算没进房间"


def test_matrix_run_refuses_to_pretend_it_reached_the_room(monkeypatch, sample):
    """要了 `--matrix` 却降级时必须当场停。

    降级之后终端照常刷「房间消息」，那一屏与真房间**一模一样** —— 跑完 exit=0
    等于让人拿一份假证据去演示。这条闸是 2026-09-01 真踩过一次才补的。
    """
    real_build = cc.build

    def degraded_build(*args, **kwargs):
        store, bus, cp, model, worker, gate = real_build(*args, **kwargs)
        bus.degrade_reason, bus.degrade_detail = "deps", "假的：解释器没装 matrix-nio"
        return store, bus, cp, model, worker, gate

    monkeypatch.setattr(cc, "build", degraded_build)

    with pytest.raises(RoomNotConnected, match="deps"):
        run_payload(copy.deepcopy(sample), matrix=True, verbose=False)

    # 显式说了要降级形态就照跑 —— 判据是人的意图，不是环境的现状。
    row = run_payload(copy.deepcopy(sample), matrix=True, verbose=False, allow_degraded=True)
    assert row["plan_state"] == "DONE"
