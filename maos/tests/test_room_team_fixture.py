"""退款圆桌演示数据的守卫 —— 底账扩展、五岗演示表、冒烟脚本。

这一轨只供数据，不供代码。所以要钉住的不是「算得对」，而是**「加了东西没弄坏原来的东西」**
和**「加的东西真的够五个岗位演出剧情」**：

  1. 🔴 老申请表的输出**逐字节不变**。底账新增两个顶层键、三张订单快照，
     `seed_case` 静默忽略未知顶层键、`build_case` 只按订单号查 —— 这是实测口径，
     不是推测。这条一红，整个扩展就得推翻重做；
  2. 新订单快照的**列集合与老行严格相等**（`fixtures.EXTRA_CASE_TABLES` 是严格列集合，
     多一个兄弟键 `seed_case` 当场抛），扩展只进 `payload_json` 字符串；
  3. 没为新订单造规则：新单经 `contrast.policy_view` 命中的仍是现有那三条 AS-；
  4. 演示表的五种剧情在数据里**各有落点**（不是只数行数）：证据齐 / 证据缺 /
     重复退款 / 大额 / 驳回，逐条能在 `order_snapshot` 或 `refund_history` 里指出来；
  5. 冒烟脚本在圆桌引擎缺席时**退化**（打人话 + exit 3），不抛栈。

零模型、零房间、零网络。不 import `maos.roundtable` / `hiclaw` / `maos.ingress` ——
那三个面属别的轨，本轨并轨前它们不存在，import 了这份测试就先红。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CUSTOM = ROOT / "scenarios" / "custom"
LEDGER = CUSTOM / "ledger.json"
LEGACY_SHEET = CUSTOM / "refund-requests.csv"
TEAM_SHEET = CUSTOM / "refund-requests-team.csv"

#: 扩展前 `python3 scripts/run_requests.py scenarios/custom/refund-requests.csv`
#: 的 stdout 指纹（编排侧 2026-09-03 在基线 cfd43a9 上实跑）。
#: 它红了先看下面那条「同一份数据两个底账」的断言：那条给的是可读的 diff，
#: 这条只说「变了」。两条一起红 = 老链路真的被扩展弄动了。
LEGACY_STDOUT_MD5 = "d877f7b1ac603b7da68e60ed52ef0aff"

LEGACY_ORDERS = ("ORD-2026-0001", "ORD-2026-0002", "ORD-2026-0003")
NEW_ORDERS = ("ORD-2026-0004", "ORD-2026-0005", "ORD-2026-0006")
CUSTOMER_ID = "CUS-2026-0042"

#: 与 `refund.evidence_check` 的交叉核对同一口径：质量诉求要的是「判不合格」。
DEFECT_RESULTS = frozenset({"defect", "fail"})


def _load_script(name: str):
    """加载 `scripts/` 下的脚本。范式与 `test_request_sheet.py::_load_script` 逐字一致。"""
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load_script("run_requests")
smoke = _load_script("room_team_smoke")


@pytest.fixture(scope="module")
def ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def orders(ledger) -> dict:
    return {row["order_id"]: row for row in ledger["order_snapshot"]}


def _run_sheet(sheet: Path, ledger_path: Path) -> bytes:
    """跑一遍业务方入口，返回 **stdout 原始字节**。判据是字节，不是解析后的对象。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_requests.py"), str(sheet),
         "--ledger", str(ledger_path)],
        cwd=str(ROOT), capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout


def _facts_of(order: dict) -> dict:
    return json.loads(order["payload_json"])


# ---------------------------------------------------------------- 1 老链路不动
def test_new_ledger_does_not_change_legacy_sheet_output(tmp_path, ledger):
    """🔴 本轨最重要的一条：扩展后的底账喂老申请表，输出与扩展前逐字节相同。

    两条判据。一条是**同一份数据、两个底账**：把新增的两个顶层键与三张新订单剔掉，
    得到一份「扩展前」的底账，两跑比字节 —— 它红了给的是能读的 diff。
    另一条是指纹，钉的是编排侧在基线上实测的那一份，防「两边一起漂」。
    """
    legacy = {k: v for k, v in ledger.items() if k not in ("customer", "refund_history")}
    legacy["order_snapshot"] = [row for row in ledger["order_snapshot"]
                                if row["order_id"] in LEGACY_ORDERS]
    legacy_path = tmp_path / "ledger-before.json"
    legacy_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    extended_out = _run_sheet(LEGACY_SHEET, LEDGER)
    legacy_out = _run_sheet(LEGACY_SHEET, legacy_path)

    assert extended_out.decode("utf-8") == legacy_out.decode("utf-8"), (
        "扩展后的底账改变了老申请表的输出 —— 新增的顶层键或新订单被老链路读到了")
    assert hashlib.md5(extended_out).hexdigest() == LEGACY_STDOUT_MD5, (
        "老申请表输出的指纹与基线不一致；先看上一条断言的 diff 再判断是谁动了")


def test_seed_case_does_not_reject_extended_ledger(ledger):
    """新底账过 `fixtures.seed_case` 不抛，且两个新顶层键**不在登记表里**（静默忽略）。"""
    from maos.core.store import SqliteStore
    from maos.domain.refund import fixtures

    registered = {table for table, _cols in fixtures.case_tables()}
    assert "customer" not in registered and "refund_history" not in registered, (
        "新顶层键一旦进了登记表就会被列集合校验，扩展底账当场抛")

    store = SqliteStore(":memory:")
    store.init_schema()
    counted = fixtures.seed_case(store, ledger)

    assert counted["order_snapshot"] == 6, "三老三新，六张订单快照都该落库"
    assert counted["policy_rule"] == 3, "扩展不许动政策：仍是三条 AS- 规则"


def test_ledger_does_not_gain_a_customer_evidence_block(ledger):
    """底账**不许**有顶层 `customer_evidence` —— 它会挂到每一单头上，包括老三单。

    机理：`contrast._signals_of` 读 `payload["customer_evidence"]`，而
    `fixtures.evidence_signals_of` **不按 case_id 过滤**。所以一份顶层证据会变成
    每一单的多源信号，哪怕那行证据的 `case_id` 指着别的案子。

    🔴 注意它**不会**让 `run_requests.py` 的汇总表变色（实测：加了之后 stdout 指纹
    仍是 `LEGACY_STDOUT_MD5`）—— 裁定、金额、状态都不受多一条信号影响。也就是说
    这条脏数据**指纹测不出来**，只能靠本条守。演示要的随案证据走房间里拖附件那条路
    （`Ticket.evidence` → 圆桌的 `evidence` 入参），不走底账。
    """
    from maos.flows import contrast

    assert "customer_evidence" not in ledger

    polluted = dict(ledger)
    polluted["customer_evidence"] = [{
        "tenant_id": "tnt-demo", "case_id": "RC-ORD-2026-0004", "evidence_id": "EV-X",
        "kind": "image", "uri": "https://example.invalid/x.jpg", "digest": "sha256:x",
        "submitted_at": "2026-08-15T00:00:00+00:00"}]
    old_req = next(r for r in rr.read_sheet(LEGACY_SHEET)
                   if r["order_id"] == "ORD-2026-0001")

    clean = contrast._signals_of(rr.build_case(ledger, old_req))
    dirty = contrast._signals_of(rr.build_case(polluted, old_req))

    assert [s["kind"] for s in clean] == ["ticket"], "老单只该有工单那一条信号"
    assert len(dirty) == len(clean) + 1, (
        "顶层证据没挂到老单上？那本条守的理由就不成立了，去核 evidence_signals_of")


# ---------------------------------------------------------------- 2 形状不变
def test_order_snapshot_rows_keep_the_frozen_column_set(ledger, orders):
    """新三行的键集合与老行**严格相等** —— 多一个兄弟键 `seed_case` 当场抛。"""
    from maos.domain.refund import fixtures

    frozen = dict(fixtures.EXTRA_CASE_TABLES)["order_snapshot"]
    legacy_keys = {frozenset(orders[oid]) for oid in LEGACY_ORDERS}
    assert legacy_keys == {frozenset(frozen)}, "老行本身就该与登记的列清单一致"

    for oid in NEW_ORDERS:
        assert frozenset(orders[oid]) == frozenset(frozen), (
            f"{oid} 的列集合与登记的十列不一致；扩展只能进 payload_json 字符串")

    assert [row["order_id"] for row in ledger["order_snapshot"]] == [
        *LEGACY_ORDERS, *NEW_ORDERS], "新订单要追加在老订单之后，不许插队"


def test_new_orders_payload_json_carries_customer_and_order_facts(orders):
    """新三单的 `payload_json` 是合法 JSON，且带 `order_facts` 三件套；老三单仍是 `{}`。"""
    for oid in LEGACY_ORDERS:
        assert orders[oid]["payload_json"] == "{}", f"{oid} 是回归判据，一个字不许动"

    for oid in NEW_ORDERS:
        facts = _facts_of(orders[oid])
        assert facts["customer_id"] == CUSTOMER_ID, "三单同一个客户，风险面才有横向可比"
        assert set(facts["logistics"]) == {"carrier", "tracking_no", "signed_at"}
        assert set(facts["qc_report"]) == {"report_no", "result", "issued_at"}
        assert facts["logistics"]["signed_at"] < "2026-09"
        assert facts["qc_report"]["result"] in DEFECT_RESULTS | {"pass"}


def test_new_orders_still_match_the_existing_as_rules(ledger):
    """新单经 `contrast.policy_view` 命中的仍是现有三条 AS- —— 没为新单造规则的机器化证明。"""
    from maos.core.store import SqliteStore
    from maos.domain.refund import fixtures
    from maos.flows import contrast

    store = SqliteStore(":memory:")
    store.init_schema()
    fixtures.seed_case(store, ledger)

    assert [r["rule_no"] for r in ledger["policy_rule"]] == ["AS-001", "AS-002", "AS-003"]
    for oid in NEW_ORDERS:
        view = contrast.policy_view(store, tenant_id="tnt-demo", order_id=oid,
                                    order_version=1)
        assert view["pinned"] == 1, f"{oid} 锁定的政策版本要与老单一致"
        assert [r["ref"] for r in view["rules"]] == ["AS-001@v1", "AS-002@v1", "AS-003@v1"]


# ---------------------------------------------------------------- 3 剧情立得住
def test_team_sheet_covers_all_five_scenarios(ledger, orders):
    """演示表列头与老表逐字同、≥ 5 行，且五种剧情**各能在数据里指出落点**。"""
    from maos.flows import contrast

    legacy_header = LEGACY_SHEET.read_text(encoding="utf-8-sig").splitlines()[0]
    team_header = TEAM_SHEET.read_text(encoding="utf-8-sig").splitlines()[0]
    assert team_header == legacy_header, "列头一变，业务方那套「只填四列」的说法就不成立"

    rows = rr.read_sheet(TEAM_SHEET)
    assert len(rows) >= 5
    assert {r["order_id"] for r in rows} <= set(orders), "演示表的订单必须都在底账里"

    history_orders = {h["order_id"] for h in ledger["refund_history"]
                      if h["status"] in ("settled", "pending")}
    max_paid = max(float(o["amount_paid"]) for o in orders.values())

    hits = {name: [] for name in
            ("证据齐", "证据缺", "重复退款", "大额", "驳回")}
    for row in rows:
        order = orders[row["order_id"]]
        facts = _facts_of(order) if order["payload_json"] != "{}" else {}
        result = (facts.get("qc_report") or {}).get("result")
        paid = float(order["amount_paid"])
        claimed = row["amount"] if row["amount"] is not None else paid
        days = contrast.elapsed_days(order["paid_at"], row["requested_at"])

        if row["reason"] == "quality_defect" and result in DEFECT_RESULTS:
            hits["证据齐"].append(row["order_id"])
        if row["reason"] == "quality_defect" and result == "pass":
            hits["证据缺"].append(row["order_id"])
        if row["order_id"] in history_orders:
            hits["重复退款"].append(row["order_id"])
        if paid >= max_paid and claimed > paid:
            hits["大额"].append(row["order_id"])
        if row["reason"] == "no_reason_return" and days > 30:
            hits["驳回"].append(row["order_id"])

    missing = [name for name, found in hits.items() if not found]
    assert not missing, f"演示表没覆盖这几种剧情：{missing}"


def test_refund_history_provides_each_risk_signal(ledger):
    """`already_refunded` / `duplicate_refund` / `frequency_30d >= 2` 各至少一例。

    判据照跨轨契约 §1.5 `refund.risk_screen` 的定义在本地算一遍 —— T86 还没并进来，
    这里钉的是**数据立不立得住**，不是那个 skill 算得对不对。
    """
    from datetime import datetime, timedelta

    history = ledger["refund_history"]
    assert {h["status"] for h in history} <= {"settled", "rejected", "pending"}
    assert {h["customer_id"] for h in history} == {CUSTOMER_ID}
    for row in history:
        assert set(row) == {"tenant_id", "case_id", "order_id", "customer_id",
                            "amount", "status", "decided_at"}

    settled = {h["order_id"] for h in history if h["status"] == "settled"}
    pending = {h["order_id"] for h in history if h["status"] == "pending"}
    assert settled, "没有 settled 记录，already_refunded 这条信号在演示里立不起来"
    assert pending, "没有 pending 记录，duplicate_refund 这条信号在演示里立不起来"

    # frequency_30d：同一客户在某个申请时刻前 30 天内的记录条数。
    # 取演示表里最晚的那次申请当基准 —— 它就是「重复退款」那行。
    latest = max(r["requested_at"] for r in rr.read_sheet(TEAM_SHEET))
    since = datetime.fromisoformat(latest) - timedelta(days=30)
    recent = [h for h in history
              if since <= datetime.fromisoformat(h["decided_at"])
              <= datetime.fromisoformat(latest)]
    assert len(recent) >= 2, "30 天内不足两条，frequency_30d 这条信号演不出来"


# ---------------------------------------------------------------- 4 冒烟脚本
def test_smoke_degrades_instead_of_raising_when_roundtable_missing(monkeypatch):
    """圆桌引擎缺席时打一行人话并 exit 3 —— 这是设计的退化路径，不是失败。"""
    monkeypatch.setattr(smoke, "load_team", lambda: None)
    out = io.StringIO()

    code = smoke.run(TEAM_SHEET, LEDGER, out=out)

    assert code == smoke.EXIT_NO_ROUNDTABLE == 3
    assert "圆桌引擎未装载" in out.getvalue()
    assert "Traceback" not in out.getvalue()
    assert "token" not in out.getvalue().lower(), "退化提示里不许出现任何口令字样"


def test_smoke_orders_reports_by_team_order_and_flags_unsaid_ones():
    """发言按 `TEAM_ORDER` 排序；没经 Voice 发出的岗位被点名（房间是旁路，会掉话）。"""

    class _Report:
        def __init__(self, agent_id, title, speech):
            self.agent_id, self.title, self.speech = agent_id, title, speech

    order = ("refund-intake", "refund-policy", "refund-finance")
    reports = [_Report("refund-finance", "财务执行岗", "核算预演：9600.00"),
               _Report("refund-intake", "申请受理岗", "订单 ORD-2026-0004"),
               _Report("zzz-unknown", "编外", "我是谁")]

    ranked = smoke.order_reports(reports, order)
    assert [r.agent_id for r in ranked] == [
        "refund-intake", "refund-finance", "zzz-unknown"], "名单外的排最后，不丢发言"

    said = [("refund-intake", "申请受理岗", "订单 ORD-2026-0004")]
    assert smoke.check_said(reports, said) == ["财务执行岗", "编外"]
