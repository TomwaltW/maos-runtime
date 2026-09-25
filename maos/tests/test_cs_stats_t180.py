"""T180 · 运营统计 scripts/cs_stats.py（review/p14-cs-contracts.md §2「T180」）。

造数据用**真前台**：FrontDesk + ``evaluate.fixture_ports`` 把 p13 开发集（p13_cases.json）整批跑进
一个临时库（与 ``scripts/cs_eval.py --set dev13 --db`` 同口径），再用真前台跑几轮带哨兵的会话、
用 SQL 把哨兵原文塞满客户原文 / 回复原文 / 客户标识 / 客服账号 / query_key / 订单号 / display_no /
槽位值 / 卡片正文，然后：

* 统计的数字与库里**直接 SQL** 数出来的逐项对得上（会话按 stage、轮数、route 分布、意图、转人工原因、
  查单结果、追问按槽位、兜底率 / 转人工率）；
* stdout 与 ``--json`` 里一个哨兵都不出现（库被写坏、枚举列塞了原文也一样）；
* 只读：库文件逐字节不变、不存在的库不会被建出来；没有 cs_ 表 → 全零并说明、退 0；
* ``--tenant`` / ``--since`` / ``--top`` 的过滤口径；拦截与会诊卡读 event_log。

本文件不读任何留出集。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation as cs_conversation
from maos.domain.cs import evaluate
from maos.domain.cs.corpus import seed_cs_kb
from maos.domain.cs.desk import CsConfig, FrontDesk
from maos.domain.cs.ports import Binding
from maos.domain.cs.records import upsert_binding
from maos.domain.cs.types import (
    CHANNEL_WECHAT_KF, CheckResult, Violation, conversation_id_for, plan_id_for,
)
from maos.ingress.contracts import InboundMessage

ROOT_T180 = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_T180 = ROOT_T180 / "scripts" / "cs_stats.py"

#: 哨兵：塞进库里各个原文 / 标识列，任何输出里都不许出现。
SENT_TEXT_T180 = "哨兵原文T180QZX"
SENT_REPLY_T180 = "SENTINEL-REPLY-T180-QZX"
SENT_USER_T180 = "wmSENTUSERT180QZX"
SENT_KFID_T180 = "wkSENTKFIDT180QZX"
SENT_QKEY_T180 = "SENTQKEYT180QZX"
SENT_ORDER_T180 = "SENTORDERT180QZX"
SENT_DISPLAY_T180 = "SENTDISPLAYT180QZX"
SENT_SLOT_T180 = "哨兵槽位T180QZX"
SENT_CARD_T180 = "哨兵卡片T180QZX"
SENT_ENUM_T180 = "哨兵枚举T180QZX"
SENTINELS_T180 = (SENT_TEXT_T180, SENT_REPLY_T180, SENT_USER_T180, SENT_KFID_T180,
                  SENT_QKEY_T180, SENT_ORDER_T180, SENT_DISPLAY_T180, SENT_SLOT_T180,
                  SENT_CARD_T180, SENT_ENUM_T180)

TENANT_B_T180 = "tnt-b-t180"
KFID_B_T180 = "wk_b_t180"


def _load_cli_t180():
    spec = importlib.util.spec_from_file_location("cs_stats_cli_t180", SCRIPT_T180)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli_t180():
    return _load_cli_t180()


def _run_t180(mod, capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = mod.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _json_t180(mod, capsys, db: pathlib.Path, *extra: str) -> dict:
    code, out, _ = _run_t180(mod, capsys, "--db", str(db), "--json", *extra)
    assert code == 0
    return json.loads(out)


def _sha_t180(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sql_t180(db: pathlib.Path, sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _exec_t180(db: pathlib.Path, *stmts: tuple[str, tuple]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        for sql, params in stmts:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _msg_t180(user: str, kfid: str, text: str, n: int) -> InboundMessage:
    return InboundMessage(channel=CHANNEL_WECHAT_KF, chat_id=user, sender=user, text=text,
                          msg_id=f"t180-{user}-{n}", raw={"open_kfid": kfid})


def _build_db_t180(path: pathlib.Path) -> pathlib.Path:
    """真前台造库：p13 开发集整批 + 一段带哨兵的会话 + 另一个租户的会话；再用 SQL 塞满哨兵。"""
    store = SqliteStore(str(path))
    store.init_schema()
    seed_cs_kb(store)
    tenants = dict(evaluate.DEFAULT_TENANT_MAP)

    def factory(ports):
        return FrontDesk(store, CsConfig(tenants=dict(tenants), handoff_target=None), **dict(ports))

    report = evaluate.run_eval_p13(factory, evaluate.load_cases(evaluate.P13_EVAL_PATH))
    assert report.turns > 0

    # 带哨兵的真会话：客户标识、客服账号、原文都是哨兵（p12 路径：不注入端口）。
    sent_desk = FrontDesk(store, CsConfig(tenants={SENT_KFID_T180: "tnt-demo"}, handoff_target=None))
    for i, text in enumerate((f"{SENT_TEXT_T180} 你们用哪家快递", f"{SENT_TEXT_T180} 我要投诉",
                              f"{SENT_TEXT_T180} 还在吗"), start=1):
        sent_desk.handle(_msg_t180(SENT_USER_T180, SENT_KFID_T180, text, i))
    # 第二个租户：两段会话（一段问政策、一段点名人工）。
    b_desk = FrontDesk(store, CsConfig(tenants={KFID_B_T180: TENANT_B_T180}, handoff_target=None))
    b_desk.handle(_msg_t180("cust-b-1", KFID_B_T180, "你们一般下单后几天能发出来", 1))
    b_desk.handle(_msg_t180("cust-b-2", KFID_B_T180, "我要转人工", 1))
    upsert_binding(store, Binding(tenant_id="tnt-demo", channel=CHANNEL_WECHAT_KF,
                                  external_userid=SENT_USER_T180, display_no=SENT_DISPLAY_T180,
                                  system_name="demo-orders", query_key=SENT_QKEY_T180,
                                  source="test"))

    # 把哨兵塞满所有原文 / 标识列（统计的数字不受影响：这些列本来就不该被读）。
    _exec_t180(
        path,
        ("UPDATE cs_turn SET inbound_text = inbound_text || ?, reply_text = reply_text || ?,"
         " msg_dedup_key = msg_dedup_key || ?", (SENT_TEXT_T180, SENT_REPLY_T180, SENT_ORDER_T180)),
        ("UPDATE cs_conversation SET open_kfid = ?, external_userid = ? || external_userid",
         (SENT_KFID_T180, SENT_USER_T180)),
        ("UPDATE cs_observation SET query_key = ?, system_name = system_name || ?",
         (SENT_QKEY_T180, SENT_QKEY_T180)),
        ("UPDATE cs_refund_bridge SET order_no = ?, command_line = '/refund ' || ?,"
         " refused_why = refused_why || ?", (SENT_ORDER_T180, SENT_ORDER_T180, SENT_TEXT_T180)),
        ("UPDATE cs_slot SET value = ?", (SENT_SLOT_T180,)),
        ("UPDATE cs_handoff SET card_json = ?, delivered_to = ?",
         (json.dumps({"customer_text": SENT_CARD_T180, "display_no": SENT_DISPLAY_T180},
                     ensure_ascii=False), SENT_USER_T180)),
    )
    return path


@pytest.fixture(scope="module")
def base_db_t180(tmp_path_factory) -> pathlib.Path:
    return _build_db_t180(tmp_path_factory.mktemp("cs_stats_t180") / "cs.db")


@pytest.fixture()
def db_t180(base_db_t180, tmp_path) -> pathlib.Path:
    """每条测试一份库的拷贝（要改库的测试改拷贝，不改共享底库）。"""
    dst = tmp_path / "cs.db"
    shutil.copyfile(base_db_t180, dst)
    return dst


def _assert_no_sentinel_t180(*outs: str) -> None:
    for out in outs:
        for s in SENTINELS_T180:
            assert s not in out, "输出里出现了哨兵原文"


# ---------------------------------------------------------------------------
# 数字与直接 SQL 逐项对得上
# ---------------------------------------------------------------------------
def _sql_dist_t180(db, sql, params=()) -> dict[str, int]:
    return {str(k): int(n) for k, n in _sql_t180(db, sql, params)}


def _nonzero_t180(d: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in d.items() if v}


def test_numbers_match_direct_sql_t180(cli_t180, capsys, base_db_t180):
    db = base_db_t180
    st = _json_t180(cli_t180, capsys, db)
    assert st["has_cs_tables"] is True and st["notes"] == []

    routes = _sql_dist_t180(db, "SELECT route, COUNT(*) FROM cs_turn GROUP BY route")
    assert _nonzero_t180(st["routes"]) == routes
    # 造出来的库要真的覆盖到这几种 route，不然「对得上」可能只是全零对全零。
    assert {"answer", "fallback", "handoff", "silent", "clarify"} <= set(routes)

    reasons = _sql_dist_t180(db, "SELECT handoff_reason, COUNT(*) FROM cs_turn"
                                 " WHERE handoff_reason != '' GROUP BY handoff_reason")
    assert _nonzero_t180(st["handoff_reasons"]) == reasons
    assert len(reasons) >= 8

    lookups = _sql_dist_t180(db, "SELECT lookup_outcome, COUNT(*) FROM cs_turn_ext"
                                 " WHERE lookup_outcome != '' GROUP BY lookup_outcome")
    assert _nonzero_t180(st["lookup_outcomes"]) == lookups
    assert len(lookups) == 6            # p13 开发集覆盖全部六种查单结果

    stages = _sql_dist_t180(db, "SELECT stage, COUNT(*) FROM cs_conversation GROUP BY stage")
    assert _nonzero_t180(st["conversations"]["by_stage"]) == stages
    assert st["conversations"]["total"] == _sql_t180(db, "SELECT COUNT(*) FROM cs_conversation")[0][0]

    asks = _sql_dist_t180(db, "SELECT ask_slot, COUNT(*) FROM cs_turn_ext WHERE ask_slot != ''"
                              " GROUP BY ask_slot")
    assert asks and _nonzero_t180(st["clarify_by_slot"]) == asks

    total = _sql_t180(db, "SELECT COUNT(*) FROM cs_turn")[0][0]
    answered = _sql_t180(db, "SELECT COUNT(*) FROM cs_turn WHERE route != 'silent'")[0][0]
    assert st["turns"] == total and st["answered_turns"] == answered
    assert st["rates"]["fallback_rate"] == round(routes.get("fallback", 0) / answered, 4)
    assert st["rates"]["handoff_rate"] == round(routes.get("handoff", 0) / answered, 4)

    intents = _sql_dist_t180(db, "SELECT intent, COUNT(*) FROM cs_turn WHERE route != 'silent'"
                                 " GROUP BY intent")
    want = sorted(intents.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    assert [tuple(x) for x in st["intents_top"]] == want

    cites: dict[str, int] = {}
    for (raw,) in _sql_t180(db, "SELECT draft_json FROM cs_turn"):
        for c in set(json.loads(raw).get("citations") or ()):
            cites[c] = cites.get(c, 0) + 1
    assert cites
    assert [tuple(x) for x in st["scripts_top"]] == sorted(
        cites.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    # dev13 本身不产生会诊卡与拦截（T178 并行在写）：读 event_log 读到 0。
    assert st["conferences"]["total"] == 0 and st["rejections"]["total"] == 0


def test_text_output_carries_the_same_numbers_t180(cli_t180, capsys, base_db_t180):
    st = _json_t180(cli_t180, capsys, base_db_t180)
    code, out, _ = _run_t180(cli_t180, capsys, "--db", str(base_db_t180))
    assert code == 0
    assert f"轮数 {st['turns']}（应答轮 {st['answered_turns']}" in out
    assert f"会话数 {st['conversations']['total']}" in out
    for reason, n in st["handoff_reasons"].items():
        assert any(line.split() == [reason, str(n)] for line in out.splitlines())
    for outcome, n in st["lookup_outcomes"].items():
        assert any(line.split() == [outcome, str(n)] for line in out.splitlines())


# ---------------------------------------------------------------------------
# 只出计数与 id：哨兵一个都不出现
# ---------------------------------------------------------------------------
def test_sentinels_never_in_any_output_t180(cli_t180, capsys, base_db_t180):
    db = base_db_t180
    # 哨兵确实在库里（防「库里本来就没有、于是输出里也没有」的空转）。
    blob = "\n".join(str(r) for t in ("cs_turn", "cs_conversation", "cs_observation",
                                      "cs_refund_bridge", "cs_slot", "cs_handoff",
                                      "cs_order_binding")
                     for r in _sql_t180(db, f"SELECT * FROM {t}"))
    for s in SENTINELS_T180:
        if s != SENT_ENUM_T180:
            assert s in blob
    outs = []
    for extra in ((), ("--json",), ("--tenant", "tnt-demo"), ("--tenant", "tnt-demo", "--json"),
                  ("--since", "2000-01-01T00:00:00+00:00", "--top", "50", "--json")):
        code, out, err = _run_t180(cli_t180, capsys, "--db", str(db), *extra)
        assert code == 0
        outs += [out, err]
    _assert_no_sentinel_t180(*outs)


def test_corrupted_enum_columns_do_not_leak_t180(cli_t180, capsys, db_t180):
    """枚举列 / 引用 / 事件 detail 被写坏塞了原文：归 other，原文不出。"""
    conv, turn = _sql_t180(db_t180, "SELECT conversation_id, turn_id FROM cs_turn"
                                    " WHERE route='answer' ORDER BY turn_id LIMIT 1")[0]
    _exec_t180(
        db_t180,
        ("UPDATE cs_turn SET intent = ?, draft_json = ? WHERE turn_id = ?",
         (SENT_ENUM_T180, json.dumps({"text": SENT_TEXT_T180, "citations": [SENT_ENUM_T180]},
                                     ensure_ascii=False), turn)),
        ("INSERT INTO event_log (event_id, trace_id, plan_id, task_id, event_type, detail,"
         " created_at) VALUES ('', '', ?, ?, 'CsConferenceHeld', ?, '2026-09-25T00:00:00+00:00')",
         (plan_id_for(conv), turn, json.dumps({"recommendation": SENT_ENUM_T180},
                                              ensure_ascii=False))),
        ("INSERT INTO event_log (event_id, trace_id, plan_id, task_id, event_type, detail,"
         " created_at) VALUES ('', '', ?, ?, 'CsReplyRejected', ?, '2026-09-25T00:00:00+00:00')",
         (plan_id_for(conv), turn, json.dumps({"violation_kinds": [SENT_ENUM_T180]},
                                              ensure_ascii=False))),
    )
    st = _json_t180(cli_t180, capsys, db_t180, "--top", "50")
    assert ["other", 1] in st["intents_top"]
    assert ["other", 1] in st["scripts_top"]
    assert st["conferences"]["by_recommendation"]["other"] == 1
    assert st["rejections"]["by_kind"]["other"] == 1
    _, out, _ = _run_t180(cli_t180, capsys, "--db", str(db_t180))
    _assert_no_sentinel_t180(out, json.dumps(st, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 只读；没有 cs_ 表
# ---------------------------------------------------------------------------
def test_database_is_opened_read_only_t180(cli_t180, capsys, db_t180, tmp_path):
    before = _sha_t180(db_t180)
    _run_t180(cli_t180, capsys, "--db", str(db_t180))
    _run_t180(cli_t180, capsys, "--db", str(db_t180), "--json", "--tenant", "tnt-demo")
    assert _sha_t180(db_t180) == before
    conn = cli_t180.connect_ro(str(db_t180))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM cs_turn")
    finally:
        conn.close()
    absent = tmp_path / "absent.db"
    code, out, err = _run_t180(cli_t180, capsys, "--db", str(absent))
    assert code == 2 and out == "" and "库读不到" in err
    assert not absent.exists()


@pytest.mark.parametrize("kind", ["core_only", "unrelated_table"])
def test_no_cs_tables_is_all_zero_and_exit_zero_t180(cli_t180, capsys, tmp_path, kind):
    db = tmp_path / "plain.db"
    if kind == "core_only":
        SqliteStore(str(db)).init_schema()          # 有 event_log，没有 cs_ 表
    else:
        _exec_t180(db, ("CREATE TABLE other_t (x TEXT)", ()))
    st = _json_t180(cli_t180, capsys, db)
    assert st["has_cs_tables"] is False
    assert st["notes"] == [cli_t180.NOTE_NO_CS]
    zero = cli_t180.empty_stats(tenant=None, since=None, top=10)
    assert {k: v for k, v in st.items() if k not in ("notes",)} == \
        {k: v for k, v in zero.items() if k not in ("notes",)}
    code, out, _ = _run_t180(cli_t180, capsys, "--db", str(db))
    assert code == 0 and cli_t180.NOTE_NO_CS in out


def test_cli_subprocess_end_to_end_t180(base_db_t180):
    proc = subprocess.run([sys.executable, str(SCRIPT_T180), "--db", str(base_db_t180), "--json",
                           "--top", "3"], capture_output=True, text=True, cwd=str(ROOT_T180),
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    st = json.loads(proc.stdout)
    assert len(st["intents_top"]) == 3 and st["filters"]["top"] == 3
    _assert_no_sentinel_t180(proc.stdout, proc.stderr)
    bad = subprocess.run([sys.executable, str(SCRIPT_T180)], capture_output=True, text=True,
                         cwd=str(ROOT_T180), timeout=120)
    assert bad.returncode == 2


# ---------------------------------------------------------------------------
# 过滤：--tenant / --since / --top
# ---------------------------------------------------------------------------
def test_tenant_filter_t180(cli_t180, capsys, base_db_t180):
    db = base_db_t180
    b = _json_t180(cli_t180, capsys, db, "--tenant", TENANT_B_T180)
    assert b["conversations"]["total"] == 2 and b["turns"] == 2
    assert _nonzero_t180(b["routes"]) == _sql_dist_t180(
        db, "SELECT route, COUNT(*) FROM cs_turn WHERE tenant_id = ? GROUP BY route",
        (TENANT_B_T180,))
    assert _nonzero_t180(b["handoff_reasons"]) == {"requested": 1}
    demo = _json_t180(cli_t180, capsys, db, "--tenant", "tnt-demo")
    everything = _json_t180(cli_t180, capsys, db)
    for key in ("turns", "answered_turns"):
        assert demo[key] + b[key] + _json_t180(cli_t180, capsys, db, "--tenant", "")[key] \
            == everything[key]
    assert _json_t180(cli_t180, capsys, db, "--tenant", "nobody-t180")["turns"] == 0


def test_tenant_filter_applies_to_event_log_rows_t180(cli_t180, capsys, db_t180):
    store = SqliteStore(str(db_t180))
    conv_id = conversation_id_for(TENANT_B_T180, CHANNEL_WECHAT_KF, KFID_B_T180, "cust-b-2")
    conv = cs_conversation.get_conversation(store, TENANT_B_T180, conv_id)
    assert conv is not None
    turn = _sql_t180(db_t180, "SELECT turn_id FROM cs_turn WHERE conversation_id = ?",
                     (conv_id,))[0][0]
    cs_conversation.record_reply_rejected(store, conv, turn_id=turn, check=CheckResult(
        ok=False, violations=(Violation("unbacked_status", SENT_TEXT_T180),
                              Violation("unbacked_status", SENT_TEXT_T180),
                              Violation("uncited_rule", SENT_TEXT_T180))))
    store.append_event_log({"trace_id": "", "plan_id": plan_id_for(conv_id), "task_id": turn,
                            "event_type": "CsConferenceHeld",
                            "detail": {"handoff_id": turn, "reason": "complaint",
                                       "seats": [], "recommendation": "supervisor_review",
                                       "open_question_count": 0}})
    b = _json_t180(cli_t180, capsys, db_t180, "--tenant", TENANT_B_T180)
    assert b["rejections"]["total"] == 1
    assert _nonzero_t180(b["rejections"]["by_kind"]) == {"unbacked_status": 1, "uncited_rule": 1}
    assert b["conferences"]["total"] == 1
    assert _nonzero_t180(b["conferences"]["by_recommendation"]) == {"supervisor_review": 1}
    demo = _json_t180(cli_t180, capsys, db_t180, "--tenant", "tnt-demo")
    assert demo["rejections"]["total"] == 0 and demo["conferences"]["total"] == 0
    everything = _json_t180(cli_t180, capsys, db_t180)
    assert everything["conferences"]["total"] == 1
    # 会诊卡计数与 event_log 直接数的一致
    assert everything["conferences"]["total"] == _sql_t180(
        db_t180, "SELECT COUNT(*) FROM event_log WHERE event_type='CsConferenceHeld'")[0][0]


def test_since_filter_t180(cli_t180, capsys, db_t180):
    old = "2020-01-01T00:00:00+00:00"
    _exec_t180(db_t180,
               ("UPDATE cs_turn SET created_at = ? WHERE seq = 1", (old,)),
               ("UPDATE cs_conversation SET updated_at = ? WHERE turn_count = 1", (old,)))
    since = "2021-01-01T00:00:00"                   # 不带时区 → 按 UTC
    st = _json_t180(cli_t180, capsys, db_t180, "--since", since)
    assert st["filters"]["since"] == since
    assert _nonzero_t180(st["routes"]) == _sql_dist_t180(
        db_t180, "SELECT route, COUNT(*) FROM cs_turn WHERE seq > 1 GROUP BY route")
    assert st["turns"] == _sql_t180(db_t180, "SELECT COUNT(*) FROM cs_turn WHERE seq > 1")[0][0]
    assert st["conversations"]["total"] == _sql_t180(
        db_t180, "SELECT COUNT(*) FROM cs_conversation WHERE turn_count > 1")[0][0]
    # 查单结果只算窗口里的轮
    assert _nonzero_t180(st["lookup_outcomes"]) == _sql_dist_t180(
        db_t180, "SELECT e.lookup_outcome, COUNT(*) FROM cs_turn_ext e JOIN cs_turn t"
                 " ON t.tenant_id = e.tenant_id AND t.turn_id = e.turn_id"
                 " WHERE t.seq > 1 AND e.lookup_outcome != '' GROUP BY e.lookup_outcome")
    future = _json_t180(cli_t180, capsys, db_t180, "--since", "2999-01-01T00:00:00Z")
    assert future["turns"] == 0 and future["conversations"]["total"] == 0
    code, _, err = _run_t180(cli_t180, capsys, "--db", str(db_t180), "--since", "not-a-time")
    assert code == 2 and "--since" in err


def test_since_filter_applies_to_event_log_rows_t180(cli_t180, capsys, db_t180):
    """--since 也截 event_log 行：窗口外的拦截 / 会诊卡不计（复核 L2-1）。"""
    conv, turn = _sql_t180(db_t180, "SELECT conversation_id, turn_id FROM cs_turn"
                                    " ORDER BY turn_id LIMIT 1")[0]
    old, new = "2020-01-01T00:00:00+00:00", "2030-06-01T00:00:00+00:00"
    ins = ("INSERT INTO event_log (event_id, trace_id, plan_id, task_id, event_type, detail,"
           " created_at) VALUES ('', '', ?, ?, ?, ?, ?)")
    _exec_t180(
        db_t180,
        (ins, (plan_id_for(conv), turn, "CsReplyRejected",
               json.dumps({"violation_kinds": ["unbacked_status"]}), old)),
        (ins, (plan_id_for(conv), turn, "CsReplyRejected",
               json.dumps({"violation_kinds": ["uncited_rule"]}), new)),
        (ins, (plan_id_for(conv), turn, "CsConferenceHeld",
               json.dumps({"recommendation": "callback_soothe"}), old)),
        (ins, (plan_id_for(conv), turn, "CsConferenceHeld",
               json.dumps({"recommendation": "manual_lookup"}), new)),
    )
    since = "2021-01-01T00:00:00Z"
    everything = _json_t180(cli_t180, capsys, db_t180)
    st = _json_t180(cli_t180, capsys, db_t180, "--since", since)
    for ev, key in (("CsReplyRejected", "rejections"), ("CsConferenceHeld", "conferences")):
        in_window = _sql_t180(db_t180, "SELECT COUNT(*) FROM event_log WHERE event_type = ?"
                                       " AND created_at >= '2021-01-01'", (ev,))[0][0]
        total = _sql_t180(db_t180, "SELECT COUNT(*) FROM event_log WHERE event_type = ?",
                          (ev,))[0][0]
        assert st[key]["total"] == in_window
        assert everything[key]["total"] == total
        assert everything[key]["total"] - st[key]["total"] == 1
    assert st["rejections"]["by_kind"].get("unbacked_status", 0) == \
        everything["rejections"]["by_kind"]["unbacked_status"] - 1
    assert st["rejections"]["by_kind"]["uncited_rule"] >= 1
    assert st["conferences"]["by_recommendation"].get("callback_soothe", 0) == \
        everything["conferences"]["by_recommendation"]["callback_soothe"] - 1
    assert st["conferences"]["by_recommendation"]["manual_lookup"] >= 1
    # 未来窗口：一条事件都不计
    future = _json_t180(cli_t180, capsys, db_t180, "--since", "2999-01-01T00:00:00Z")
    assert future["rejections"]["total"] == 0 and future["conferences"]["total"] == 0


def test_unhashable_citations_do_not_crash_t180(cli_t180, capsys, db_t180):
    """draft_json.citations 里有 dict 等不可哈希元素：照常出统计、退 0、归 other（复核 L2-2）。"""
    turn = _sql_t180(db_t180, "SELECT turn_id FROM cs_turn WHERE route='answer'"
                              " ORDER BY turn_id LIMIT 1")[0][0]
    _exec_t180(db_t180, ("UPDATE cs_turn SET draft_json = ? WHERE turn_id = ?",
                         (json.dumps({"citations": [{"doc_id": SENT_ENUM_T180}, [1],
                                                    {"x": SENT_TEXT_T180}]},
                                     ensure_ascii=False), turn)))
    code, out, err = _run_t180(cli_t180, capsys, "--db", str(db_t180), "--json", "--top", "50")
    assert code == 0, err
    st = json.loads(out)
    assert ["other", 1] in st["scripts_top"]
    code, text, _ = _run_t180(cli_t180, capsys, "--db", str(db_t180))
    assert code == 0
    _assert_no_sentinel_t180(out, text)


def test_top_n_t180(cli_t180, capsys, base_db_t180):
    st = _json_t180(cli_t180, capsys, base_db_t180, "--top", "2")
    assert len(st["intents_top"]) == 2 and len(st["scripts_top"]) == 2
    full = _json_t180(cli_t180, capsys, base_db_t180, "--top", "50")
    assert st["intents_top"] == full["intents_top"][:2]
    counts = [n for _, n in full["intents_top"]]
    assert counts == sorted(counts, reverse=True)
    for bad in ("0", "-1", "x"):
        code, _, _ = _run_t180(cli_t180, capsys, "--db", str(base_db_t180), "--top", bad)
        assert code == 2
