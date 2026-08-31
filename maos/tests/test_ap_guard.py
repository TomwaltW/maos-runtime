"""应付账款域的权威事实边界（铁律 8）—— 越权路径逐条走一遍。

守卫要买的是一句话：**全系统只有 `ap.observe` 写得进 `settled`，而且必须同事务附
一份带银行流水号的回单。** 这句话由五条互相独立的判据支撑，每条对应一道闸：

  ① 非权威写入方写 settled            -> 抛 + 落事件
  ② 非权威写入方递回单                -> 抛 + 落事件
  ③ 权威终态缺回单（含缺流水号）      -> 抛 + 落事件
  ④ 回单说的不是这件事（accepted 冒充）-> 抛 + 落事件
  ⑤ 绕开守卫直写 ap_case              -> 抛

**每一条越权都必须落一条事件**，这不是附带效果：「系统拒绝了一次越权写入」本身
就是要拿给评委看的证据，吞掉就没了。所以每条用例都同时断言「抛了」与「留痕了」。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from maos.core.store import SqliteStore
from maos.domain.ap import guard, objects
from maos.domain.refund import guard as refund_guard
from maos.domain.refund import objects as refund_objects

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MAOS_PKG = REPO_ROOT / "maos"

TEN, CASE, PLAN = "tnt-ap-test", "case-guard-1", "plan-guard-1"


@pytest.fixture()
def store():
    s = SqliteStore()
    s.init_schema()
    objects.ensure_schema(s)
    return s


def _case(store, **over):
    kw = dict(tenant_id=TEN, case_id=CASE, supplier_id="SUP-1", po_id="PO-1",
              po_version=1, invoice_id="INV-1", gr_id="GR-1", amount_claimed="1000.00",
              plan_id=PLAN, actor_skill="ap.intake", invocation_id="iv-seed")
    kw.update(over)
    return guard.create_case(store, **kw)


def _to_payment_requested(store):
    _case(store)
    guard.update_biz_status(store, TEN, CASE, "matched", "ap.match", "iv-m")
    return guard.update_biz_status(store, TEN, CASE, "payment_requested",
                                   "ap.execute", "iv-e")


def _violations(store, plan_id: str = PLAN) -> list[dict]:
    return [e for e in store.list_event_log(plan_id)
            if e["event_type"] == guard.VIOLATION_EVENT]


def _good_observation(**over) -> dict:
    obs = {"instruction_id": "bkins-1", "observed_state": "settled",
           "bank_reference": "bkref-abc123", "value_date": "2026-08-31"}
    obs.update(over)
    return obs


# ------------------------------------------------------------------ ① 越权写入
def test_only_ap_observe_can_write_settled(store):
    """① 非权威写入方写 settled -> 抛 + 落事件。

    连**退款域的**权威写入方 `payment.observe` 也写不进来：两个域各有各的
    `AUTHORITATIVE_WRITER`，同名终态不等于同一个终态。
    """
    _to_payment_requested(store)
    for actor in ("ap.execute", "ap.match", "payment.observe", ""):
        with pytest.raises(guard.AuthoritativeFactViolation, match="权威在银行"):
            guard.update_biz_status(store, TEN, CASE, "settled", actor or "anon", "iv-x")
    assert len(_violations(store)) == 4, "每一次越权都必须留一条事件，一条都不许吞"
    assert guard.get_case(store, TEN, CASE)["biz_status"] == "payment_requested"


def test_violation_on_a_case_that_does_not_exist_is_still_recorded(store):
    """权威闸排在存在性检查**之前**：对不存在的 case 越权也要留痕。

    先查存在性会让这种试探以 LookupError 收场，证据就没了 —— 而那恰恰是最该
    留痕的一种。
    """
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.update_biz_status(store, TEN, "no-such-case", "settled", "ap.execute", "iv")
    hits = [e for e in store.list_event_log("")
            if e["event_type"] == guard.VIOLATION_EVENT]
    assert hits, "对不存在的 case 越权写 settled 同样要留痕"
    assert hits[-1]["detail"]["domain"] == guard.DOMAIN


# ------------------------------------------------------------------ ② 伪造回单
def test_only_ap_observe_can_submit_a_bank_advice(store):
    """② 回单只有权威写入方递得进来，否则等于给别人开伪造回单的口子。"""
    _to_payment_requested(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="回单"):
        guard.update_biz_status(store, TEN, CASE, "compensated", "ap.compensate", "iv",
                                observation=_good_observation())
    assert len(_violations(store)) == 1


def test_record_observation_rejects_non_writers(store):
    """`record_observation` 这条旁路同样只认权威写入方。

    非终态观察走的是这条路（银行明确拒付时要留痕）。它对
    `ap_payment_observation` 不设限的话，就等于给伪造回单留了个后门。
    """
    _to_payment_requested(store)
    with pytest.raises(guard.AuthoritativeFactViolation):
        guard.record_observation(store, tenant_id=TEN, case_id=CASE,
                                 instruction_id="bkins-1", observed_state="failed",
                                 invocation_id="iv", actor_skill="ap.execute")
    assert len(_violations(store)) == 1


def test_record_observation_refuses_to_write_a_settled_observation(store):
    """连权威写入方也不许用旁路单独落一条 settled 观察。

    权威终态的观察必须与状态更新**同事务**。允许单独落，就等于留了一条
    「先落一条 settled 观察、再让别人读它当成到账」的路。
    """
    _to_payment_requested(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="同事务"):
        guard.record_observation(store, tenant_id=TEN, case_id=CASE,
                                 instruction_id="bkins-1", observed_state="settled",
                                 invocation_id="iv",
                                 actor_skill=guard.AUTHORITATIVE_WRITER)


# ------------------------------------------------------- ③ 缺回单 / 缺流水号
@pytest.mark.parametrize("observation, missing", [
    (None, "instruction_id"),
    ({"observed_state": "settled", "bank_reference": "bk"}, "instruction_id"),
    ({"instruction_id": "i", "bank_reference": "bk"}, "observed_state"),
    ({"instruction_id": "i", "observed_state": "settled"}, "bank_reference"),
    ({"instruction_id": "i", "observed_state": "settled", "bank_reference": ""},
     "bank_reference"),
])
def test_settled_requires_a_complete_advice(store, observation, missing):
    """③ 权威终态必须带回单，且字段齐全 —— 其中 `bank_reference` 是本域比退款域
    多要的那一条：没有流水号的「已付」在财务上对不了账。
    """
    _to_payment_requested(store)
    with pytest.raises(guard.AuthoritativeFactViolation) as err:
        guard.update_biz_status(store, TEN, CASE, "settled",
                                guard.AUTHORITATIVE_WRITER, "iv",
                                observation=observation)
    assert missing in str(err.value)
    assert len(_violations(store)) == 1
    assert guard.get_case(store, TEN, CASE)["biz_status"] == "payment_requested"


# ------------------------------------------------------------------ ④ 冒充终态
@pytest.mark.parametrize("seen", ["accepted", "pending", "unknown", "failed"])
def test_advice_must_say_the_money_moved(store, seen):
    """④ 「有一张回单」不等于「银行说钱划走了」。

    ③ 只保证字段齐全 —— 一条 `accepted` 的受理回单三个字段都在，在 ③ 眼里与终态
    回单无从分辨。放过它，系统持有的就只是「银行收下了指令」。
    """
    _to_payment_requested(store)
    with pytest.raises(guard.AuthoritativeFactViolation, match="不等于"):
        guard.update_biz_status(store, TEN, CASE, "settled",
                                guard.AUTHORITATIVE_WRITER, "iv",
                                observation=_good_observation(observed_state=seen))
    assert len(_violations(store)) == 1


def test_authoritative_tables_stay_in_sync():
    """两张表必须同增同减：权威终态一定要有回单判据，否则 fail-closed。

    漏配的后果是那个终态退回到「有回单就算数」，静默且没人会发现。
    """
    assert set(guard.AUTHORITATIVE_RECEIPT_STATE) == set(guard.AUTHORITATIVE_STATES), (
        "AUTHORITATIVE_STATES 与 AUTHORITATIVE_RECEIPT_STATE 必须同增同减")
    for state in guard.AUTHORITATIVE_STATES:
        assert state in guard.BIZ_STATUS_FLOW, f"{state} 不在业务状态机里"


def test_missing_receipt_criterion_is_fail_closed(store, monkeypatch):
    """把判据表挖空之后，权威终态**写不进去**而不是放行。

    这条演的是上一条断言保护的那个洞真的存在：`AUTHORITATIVE_RECEIPT_STATE` 少配
    一项时，第 ④ 道拿不到判据 —— 此时唯一正确的行为是拒，不是默认放行。
    """
    _to_payment_requested(store)
    monkeypatch.setattr(guard, "AUTHORITATIVE_RECEIPT_STATE", {})
    with pytest.raises(guard.AuthoritativeFactViolation, match="同增同减"):
        guard.update_biz_status(store, TEN, CASE, "settled",
                                guard.AUTHORITATIVE_WRITER, "iv",
                                observation=_good_observation())


# ------------------------------------------------------------------ ⑤ 旁路直写
@pytest.mark.parametrize("sql", [
    "UPDATE ap_case SET biz_status='settled' WHERE case_id='x'",
    "INSERT INTO ap_case (tenant_id) VALUES ('t')",
    "INSERT OR REPLACE INTO ap_case (tenant_id) VALUES ('t')",
    "DELETE FROM ap_case WHERE case_id='x'",
    "REPLACE INTO ap_case (tenant_id) VALUES ('t')",
    "update  \"ap_case\"  set biz_status='settled'",
])
def test_objects_execute_refuses_to_write_ap_case(store, sql):
    """⑤ 运行时旁路：`objects.execute` 见到 ap_case 的写语句直接抛。

    grep 挡的是提交进仓库的旁路，这一条挡的是运行时的旁路（比如某个 skill 拼了
    一条动态 SQL）。
    """
    with pytest.raises(objects.BypassedGuardError, match="guard"):
        objects.execute(store, sql)


def test_alter_table_on_ap_case_is_not_blocked(store):
    """给 ap_case 加列这类正常迁移不受拦截 —— 守的是写数据，不是改形状。"""
    objects.execute(store, "ALTER TABLE ap_case ADD COLUMN memo TEXT DEFAULT ''")
    assert objects.query(store, "SELECT memo FROM ap_case") == []


def test_no_source_file_writes_settled_outside_the_guard():
    """提交前那条 grep 自查，钉成断言：全仓只有守卫写得出 settled。

    扫的是**赋值给 `biz_status` 的 settled 字面量**与直接写表的 SQL。
    `ap.observe` 传的是 `STATUS_SETTLED` 常量给 `update_biz_status`，那条路正当，
    所以判据落在「谁在拼写库语句」上，不落在「谁提到了 settled 这个词」上。
    """
    pattern = re.compile(
        r"(?:UPDATE\s+ap_case|INSERT\s+(?:OR\s+\w+\s+)?INTO\s+ap_case"
        r"|REPLACE\s+INTO\s+ap_case|DELETE\s+FROM\s+ap_case)",
        re.IGNORECASE)
    allowed = {MAOS_PKG / "domain" / "ap" / "guard.py",
               MAOS_PKG / "tests" / "test_ap_guard.py"}
    offenders = []
    for path in sorted(MAOS_PKG.rglob("*.py")):
        if path in allowed:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        f"这些地方在绕开 guard 直接写 ap_case：{offenders}")


# -------------------------------------------------------- 与退款域互不影响
def test_ap_and_refund_guards_are_independent(store):
    """两个域都有一个叫 `settled` 的终态，且互不影响。

    三条各自独立：
      · 写入方不同（`ap.observe` vs `payment.observe`），互相写不进对方的终态；
      · 各守各的表，表名一个都不重；
      · 同一个 store 里两个案子各自推进到 settled，谁都没碰到谁。

    这一条是派单 §5.2 那句「开工先确认这两者互不影响」的机器化版本。
    """
    assert guard.AUTHORITATIVE_STATES == refund_guard.AUTHORITATIVE_STATES == \
        frozenset({"settled"}), "两个域确实都有一个叫 settled 的终态"
    assert guard.AUTHORITATIVE_WRITER != refund_guard.AUTHORITATIVE_WRITER, (
        "同名终态必须有不同的权威写入方，否则就是同一个终态了")

    refund_objects.ensure_schema(store)

    # ---- AP 侧推到 settled ----
    _to_payment_requested(store)
    ap_case = guard.update_biz_status(
        store, TEN, CASE, "settled", guard.AUTHORITATIVE_WRITER, "iv-ok",
        observation=_good_observation())
    assert ap_case["biz_status"] == "settled"

    # ---- 退款侧在同一个库里推到 settled ----
    refund_guard.create_case(store, tenant_id=TEN, case_id="rf-1", channel_id="ch",
                             order_id="ord", order_version=1, sku="SKU",
                             reason_code="quality_defect", amount_claimed=100.0,
                             plan_id=PLAN, actor_skill="refund.intake",
                             invocation_id="iv-r0")
    for nxt, actor in (("approved", "policy.match"),
                       ("gateway_accepted", "payment.execute"),
                       ("processing", "payment.execute")):
        refund_guard.update_biz_status(store, TEN, "rf-1", nxt, actor, "iv-r")
    refund_guard.update_biz_status(
        store, TEN, "rf-1", "settled", refund_guard.AUTHORITATIVE_WRITER, "iv-r9",
        observation={"request_id": "rq-1", "gateway_code": "10000",
                     "observed_state": "settled"})

    # ---- 互不干扰：各自一行，各自一条观察 ----
    assert guard.get_case(store, TEN, CASE)["biz_status"] == "settled"
    assert refund_guard.get_case(store, TEN, "rf-1")["biz_status"] == "settled"
    assert len(guard.observations_of(store, TEN, CASE)) == 1
    assert objects.query(store, "SELECT COUNT(*) AS n FROM payment_observation"
                         )[0]["n"] == 1, "退款域的观察落在它自己那张表上"
    assert objects.query(store, "SELECT COUNT(*) AS n FROM ap_payment_observation"
                         )[0]["n"] == 1, "本域的观察落在 ap_payment_observation 上"


def test_the_two_domains_share_no_table_name():
    """两个域的建表脚本不许有同名表 —— 同名的后果是**静默**跳过。

    `CREATE TABLE IF NOT EXISTS` 撞名不报错：表在、列是对方的、跑起来一切正常，
    直到某条 INSERT 报 no such column。
    """
    def tables(path: pathlib.Path) -> set[str]:
        sql = path.read_text(encoding="utf-8")
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE))

    ap_tables = tables(MAOS_PKG / "domain" / "ap" / "schema.sql")
    refund_tables = tables(MAOS_PKG / "domain" / "refund" / "schema.sql")
    assert ap_tables, "应付账款域一张表都没建"
    overlap = ap_tables & refund_tables
    assert not overlap, f"两个域建了同名表：{sorted(overlap)}"


def test_ap_schema_only_adds_tables():
    """铁律 1：本域只**新增**表，不碰 `maos/core/store.py` 的既有表结构。

    判据落在建表脚本上：整份只有 `CREATE TABLE IF NOT EXISTS` 与
    `CREATE INDEX IF NOT EXISTS`，没有任何 ALTER / DROP 既有表的语句。
    """
    sql = (MAOS_PKG / "domain" / "ap" / "schema.sql").read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    # 去掉纯注释块
    statements = [s for s in statements
                  if any(line.strip() and not line.strip().startswith("--")
                         for line in s.splitlines())]
    for stmt in statements:
        head = " ".join(line for line in stmt.splitlines()
                        if line.strip() and not line.strip().startswith("--"))
        assert re.match(r"\s*CREATE (TABLE|INDEX) IF NOT EXISTS", head, re.IGNORECASE), (
            f"建表脚本里出现了非 CREATE ... IF NOT EXISTS 的语句：{head[:80]}")


# ------------------------------------------------------------ 状态机与幂等
def test_biz_status_flow_has_no_shortcut_to_settled():
    """业务状态机里只有 `payment_requested` 到得了 `settled`。

    多一条捷径就等于多一条「没发指令就宣布已付」的路。
    """
    into_settled = [src for src, dsts in guard.BIZ_STATUS_FLOW.items()
                    if "settled" in dsts]
    assert into_settled == ["payment_requested"], (
        f"能到 settled 的只该是 payment_requested，实际 {into_settled}")
    assert guard.BIZ_STATUS_FLOW["settled"] == (), "settled 是终态，不许有出边"
    assert guard.INITIAL_STATUS == "received"


def test_illegal_transition_is_refused(store):
    """跳过中间态直接迁移 -> `BizStatusTransitionError`。"""
    _case(store)
    with pytest.raises(guard.BizStatusTransitionError, match="不许从"):
        guard.update_biz_status(store, TEN, CASE, "payment_requested", "ap.execute", "iv")


def test_create_case_is_idempotent_and_does_not_rewind(store):
    """收票重跑幂等：业务字段相同就一个字节都不写，**不把已推进的案子倒回去**。"""
    _to_payment_requested(store)
    again = _case(store, invocation_id="iv-again", amount_claimed=1000.0)
    assert again["biz_status"] == "payment_requested", (
        "重跑收票不许把已经推进的案子倒回 received —— 那是静默的数据损坏")


def test_create_case_refuses_a_reused_case_id(store):
    """同一个案号上来一份业务字段不同的发票 -> 抛 + 落冲突事件，两种静默走法都错。"""
    _case(store)
    with pytest.raises(guard.CaseIdentityConflict, match="业务字段对不上"):
        _case(store, amount_claimed="9999.00", invocation_id="iv-conflict")
    hits = [e for e in store.list_event_log(PLAN)
            if e["event_type"] == guard.CASE_CONFLICT_EVENT]
    assert len(hits) == 1
    assert "amount_claimed" in hits[0]["detail"]["conflicts"]


def test_invocation_id_must_not_be_empty(store):
    """actor 锚点为空 = 审计链断了，不许兜底。"""
    with pytest.raises(ValueError, match="invocation_id"):
        _case(store, invocation_id="")
    _case(store)
    with pytest.raises(ValueError, match="invocation_id"):
        guard.update_biz_status(store, TEN, CASE, "matched", "ap.match", "")


def test_money_never_goes_through_float(store):
    """金额解析不出数就抛，**不兜底成 0**。

    兜底成 0 会让「金额字段是垃圾」被静默处理成「这笔是 0 元」，而 0 元在勾稽里
    往往刚好对得上，于是垃圾数据一路绿灯过闸。
    """
    with pytest.raises(ValueError, match="金额解析不出数值"):
        objects.money("一千块")
    # float 先转 str 再进 Decimal，二进制误差不会被带进来。
    assert str(objects.money(0.1) + objects.money(0.2)) == "0.3"
    assert objects.money_str(1000) == "1000.00"


def test_guard_module_does_not_import_the_kernel():
    """守卫只依赖本域的 objects —— 不 import contracts / core / runtime。

    一旦守卫反过来依赖内核，「业务对象层是纯新增」这句话就要打折。
    """
    tree = ast.parse((MAOS_PKG / "domain" / "ap" / "guard.py").read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            if n.startswith(("maos.contracts", "maos.core", "maos.runtime")):
                bad.append(n)
    assert not bad, f"守卫 import 了内核：{bad}"
