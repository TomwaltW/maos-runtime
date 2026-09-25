"""客服只读 MCP 连接器 —— ``maos/tools/mcp/cs_server.py``（p14 · T179）。

契约：review/p14-cs-contracts.md §2「T179」。每条用例都用子进程**真起**
``python3 -m maos.tools.mcp.cs_server --db <临时库>``，经 stdio 走 initialize / tools/list /
tools/call 全流程（一行一帧），不在进程内直接调工具函数。

* 绑定一律 ``records.upsert_binding``（source=test）；查单一律 ``MAOS_CS_ORDER_SYSTEMS=demo`` +
  临时台账（demo 台账复制进 tmp，给一单标 shipped）→ ``MockOrderSystem``，不打网络。
* 子进程 env 从零造（PATH / PYTHONPATH / 本用例要的 MAOS_CS_*），不继承本进程的环境变量。
* 哨兵：query_key、金额、客户标识、卡片正文、``/refund`` 命令行一个都不许出现在 stdout 里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from maos.core.store import SqliteStore
from maos.domain.cs import conversation, objects, records
from maos.domain.cs import ports as P
from maos.domain.cs.ports import Binding
from maos.domain.cs.types import CS_MCP_PLAN_ID, HandoffCard, mask_customer, turn_id_for
from maos.tools.mcp import cs_server
from maos.tools.mcp.protocol import (
    E_INVALID_PARAMS,
    E_INVALID_REQUEST,
    E_METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
)
from maos.tools.mcp.registry import DEFAULT_ROLE_SERVERS, SERVERS, discover, reconcile

ROOT = Path(__file__).resolve().parents[2]
DEMO_LEDGER = ROOT / "scenarios" / "custom" / "ledger.json"

TENANT = "tnt-demo"
CHANNEL = "wechat_kf"
#: 客户标识哨兵：只许住在绑定表 / 会话表里，一个字都不许出连接器。
USER = "wx-SENTINEL-USER-t179-990011"
OTHER_USER = "wx-someone-else-t179"
#: 客户看得到的单号 ≠ 查单键：查单键（台账订单号）是哨兵，只许出现在审计行的参数摘要里。
MY_NO = "MY-ORDER-t179"
QUERY_KEY = "ORD-2026-0001"        # 台账上 paid，金额 6800
SHIPPED_KEY = "ORD-2026-0003"      # 临时台账里标成 shipped
PRECHECK_NO = "ORD-2026-0002"      # 台账上能过预检的一单；预检按绑定的 query_key 查台账
GHOST_KEY = "ORD-GHOST-t179"       # 绑定了但台账里没有 → not_found
CARD_TEXT = "SENTINEL-客户原文-t179"
CARD_SUGGEST = "SENTINEL-处理建议-t179"


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger_t179() -> Path:
    """demo 台账原样（只读）：预检走 custom_case 的严格读法，多一列都读不出来。"""
    return DEMO_LEDGER


@pytest.fixture()
def shipped_ledger_t179(tmp_path) -> Path:
    """demo 台账复制进 tmp，给一单加 status=shipped —— 只给查单用（预检读不动带 status 列的台账）。"""
    data = json.loads(DEMO_LEDGER.read_text(encoding="utf-8"))
    for row in data["order_snapshot"]:
        if row.get("order_id") == SHIPPED_KEY:
            row["status"] = "shipped"
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _bind_t179(store: SqliteStore, display_no: str, query_key: str, *, user: str = USER) -> None:
    records.upsert_binding(store, Binding(
        tenant_id=TENANT, channel=CHANNEL, external_userid=user, display_no=display_no,
        system_name="demo-orders", query_key=query_key, source=P.BINDING_TEST))


@pytest.fixture()
def db_t179(tmp_path) -> Path:
    path = tmp_path / "cs.db"
    store = SqliteStore(str(path))
    store.init_schema()
    objects.ensure_schema(store)
    _bind_t179(store, MY_NO, QUERY_KEY)
    _bind_t179(store, "SHIP-t179", SHIPPED_KEY)
    _bind_t179(store, PRECHECK_NO, PRECHECK_NO)
    _bind_t179(store, "GHOST-t179", GHOST_KEY)
    return path


def _env_t179(*, orders: bool = True, ledger_tenant: bool = True) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "MAOS_FORCE_SCRIPTED": "1"}
    if orders:
        env["MAOS_CS_ORDER_SYSTEMS"] = "demo"
    if ledger_tenant:
        env["MAOS_CS_LEDGER_TENANT"] = TENANT
    return env


def _run_t179(db: Path, ledger: Path, calls: list, *, env: dict | None = None,
              extra_args: tuple[str, ...] = (), raw_lines: tuple[str, ...] = ()
              ) -> tuple[dict, str]:
    """真起一次 server：握手 + tools/list + 依次 tools/call，关 stdin 收尸。

    ``calls`` 的每一项是 ``(工具名, 参数)``，请求 id 从 10 起按序编。返回 ``({id: 回帧}, 原始 stdout)``。
    """
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                    "clientInfo": {"name": "t179", "version": "0"}}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
    lines.extend(raw_lines)
    for i, (name, args) in enumerate(calls):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}},
                                ensure_ascii=False))
    argv = [sys.executable, "-m", "maos.tools.mcp.cs_server", "--db", str(db),
            "--ledger", str(ledger), *extra_args]
    proc = subprocess.run(argv, input="\n".join(lines) + "\n", capture_output=True, text=True,
                          env=env if env is not None else _env_t179(), timeout=120,
                          cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    frames = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    by_id: dict = {f["id"]: f for f in frames if f.get("id") is not None}
    by_id[None] = [f for f in frames if f.get("id") is None]
    # 哨兵只扫工具出参（id ≥ 10 与无 id 的错误帧）：握手与 tools/list 里本来就有 version 等字样。
    calls_out = "\n".join(json.dumps(f, ensure_ascii=False) for f in frames
                          if f.get("id") is None or f["id"] >= 10)
    return by_id, calls_out


def _payload_t179(frames: dict, req_id: int) -> dict:
    frame = frames[req_id]
    assert "result" in frame, frame
    assert frame["result"]["isError"] is False, frame
    body = frame["result"]["structuredContent"]
    assert json.loads(frame["result"]["content"][0]["text"]) == body
    return body


def _order_args_t179(display_no: str, *, user: str = USER, **kw) -> dict:
    args = {"tenant_id": TENANT, "channel": CHANNEL, "external_userid": user,
            "display_no": display_no}
    args.update(kw)
    return args


def _tool_rows_t179(db: Path) -> list[dict]:
    store = SqliteStore(str(db))
    rows = store._conn.execute(
        "SELECT plan_id, task_id, trace_id, detail FROM event_log "
        "WHERE event_type='ToolInvoked' ORDER BY seq").fetchall()
    return [dict(r, detail=json.loads(r["detail"])) for r in rows]


def _assert_no_sentinels_t179(stdout: str, *extra: str) -> None:
    for s in (USER, QUERY_KEY, SHIPPED_KEY, GHOST_KEY, "6800", "/refund", "command_line",
              "query_key", "amount", "updated_at", *extra):
        assert s not in stdout, f"出参里出现了哨兵 {s!r}"


def _assert_detail_clean_t179(rows: list[dict]) -> None:
    """审计行 detail 里只许有参数摘要与异常类名：客户标识 / 查单键 / 客户单号一个都不许落。"""
    for r in rows:
        blob = json.dumps(r["detail"], ensure_ascii=False)
        for s in (USER, QUERY_KEY, SHIPPED_KEY, GHOST_KEY, MY_NO, "SHIP-t179", "GHOST-t179"):
            assert s not in blob, f"审计行 detail 里出现了哨兵 {s!r}"


# ---------------------------------------------------------------------------
# 握手与清单
# ---------------------------------------------------------------------------

def test_handshake_and_tools_list_over_stdio_t179(db_t179, ledger_t179):
    frames, _ = _run_t179(db_t179, ledger_t179, [])
    init = frames[1]["result"]
    assert init["protocolVersion"] == PROTOCOL_VERSION
    assert init["serverInfo"]["name"] == cs_server.SERVER_NAME
    tools = frames[2]["result"]["tools"]
    assert [t["name"] for t in tools] == ["cs_order_status", "cs_refund_precheck",
                                          "cs_handoff_list"]
    req = {t["name"]: t["inputSchema"]["required"] for t in tools}
    assert req == {
        "cs_order_status": ["tenant_id", "channel", "external_userid", "display_no"],
        "cs_refund_precheck": ["tenant_id", "channel", "external_userid", "display_no",
                               "reason_text"],
        "cs_handoff_list": ["tenant_id"],
    }
    assert set(frames) == {1, 2, None} and frames[None] == [], "通知不许回帧"


# ---------------------------------------------------------------------------
# cs_order_status
# ---------------------------------------------------------------------------

def test_order_status_ok_answers_only_with_the_wording_table_t179(db_t179, shipped_ledger_t179):
    frames, stdout = _run_t179(db_t179, shipped_ledger_t179, [
        ("cs_order_status", _order_args_t179(MY_NO)),
        ("cs_order_status", _order_args_t179(MY_NO, lang="en")),
        ("cs_order_status", _order_args_t179("SHIP-t179")),
    ])
    zh, en, shipped = (_payload_t179(frames, i) for i in (10, 11, 12))
    assert zh == {"outcome": "ok", "display_no": MY_NO,
                  "wording": P.ORDER_STATUS_WORDING["zh"]["paid"]}
    assert en == {"outcome": "ok", "display_no": MY_NO,
                  "wording": P.ORDER_STATUS_WORDING["en"]["paid"]}
    assert shipped == {"outcome": "ok", "display_no": "SHIP-t179",
                       "wording": P.ORDER_STATUS_WORDING["zh"]["shipped"]}
    _assert_no_sentinels_t179(stdout, '"paid"', '"shipped"', "version")


def test_order_status_writes_one_cs_mcp_tool_row_per_lookup_t179(db_t179, ledger_t179):
    _run_t179(db_t179, ledger_t179, [
        ("cs_order_status", _order_args_t179(MY_NO)),
        ("cs_order_status", _order_args_t179("SHIP-t179")),
    ])
    rows = _tool_rows_t179(db_t179)
    assert [(r["plan_id"], r["task_id"], r["trace_id"]) for r in rows] == [
        (CS_MCP_PLAN_ID, "mcp-1", ""), (CS_MCP_PLAN_ID, "mcp-2", "")]
    assert all(r["detail"]["tool"] == "order.query" for r in rows)
    _assert_detail_clean_t179(rows)
    # 第二个进程接着同一个库：task_id 继续递增，不从 1 重来。
    _run_t179(db_t179, ledger_t179, [("cs_order_status", _order_args_t179(MY_NO))])
    assert [r["task_id"] for r in _tool_rows_t179(db_t179)] == ["mcp-1", "mcp-2", "mcp-3"]


def test_order_status_identity_unverified_never_looks_up_t179(db_t179, ledger_t179):
    frames, stdout = _run_t179(db_t179, ledger_t179, [
        ("cs_order_status", _order_args_t179(MY_NO, user=OTHER_USER)),   # 不是他的单
        ("cs_order_status", _order_args_t179("NOT-BOUND-t179")),         # 没绑定的单
        ("cs_order_status", _order_args_t179(MY_NO, channel="feishu")),  # 渠道不对
    ])
    for i in (10, 11, 12):
        assert _payload_t179(frames, i) == {"outcome": "identity_unverified"}
    assert _tool_rows_t179(db_t179) == [], "身份不过也查了单"
    _assert_no_sentinels_t179(stdout)


def test_order_status_not_found_has_empty_wording_t179(db_t179, ledger_t179):
    frames, stdout = _run_t179(db_t179, ledger_t179, [
        ("cs_order_status", _order_args_t179("GHOST-t179"))])
    assert _payload_t179(frames, 10) == {"outcome": "not_found", "display_no": "GHOST-t179",
                                         "wording": ""}
    _assert_no_sentinels_t179(stdout, "KeyError")
    rows = _tool_rows_t179(db_t179)
    assert len(rows) == 1
    _assert_detail_clean_t179(rows)


def test_unconfigured_lookup_answers_system_misconfigured_t179(db_t179, ledger_t179):
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("cs_order_status", _order_args_t179(MY_NO)),
        ("cs_refund_precheck", _order_args_t179(PRECHECK_NO, reason_text="七天无理由退货")),
    ], env=_env_t179(orders=False))
    assert _payload_t179(frames, 10) == {"outcome": "system_misconfigured"}
    assert _payload_t179(frames, 11) == {"outcome": "system_misconfigured"}
    assert _tool_rows_t179(db_t179) == []


def test_broken_order_config_is_misconfigured_not_a_crash_t179(db_t179, ledger_t179):
    env = _env_t179()
    env["MAOS_CS_ORDER_SYSTEMS"] = '{"x": {"platform": "no-such-platform"}}'
    frames, _ = _run_t179(db_t179, ledger_t179, [("cs_order_status", _order_args_t179(MY_NO))],
                          env=env)
    assert _payload_t179(frames, 10) == {"outcome": "system_misconfigured"}


# ---------------------------------------------------------------------------
# cs_refund_precheck
# ---------------------------------------------------------------------------

def test_refund_precheck_ok_never_returns_the_command_line_t179(db_t179, ledger_t179):
    frames, stdout = _run_t179(db_t179, ledger_t179, [
        ("cs_refund_precheck", _order_args_t179(PRECHECK_NO, reason_text="七天无理由退货"))])
    body = _payload_t179(frames, 10)
    assert set(body) == {"outcome", "ok", "decision", "rule_ref", "reason_code", "refused_why"}
    assert body["outcome"] == "ok" and body["ok"] is True
    assert body["decision"] in ("approve", "reject") and body["reason_code"]
    assert body["refused_why"] == ""
    _assert_no_sentinels_t179(stdout, "summary", "只读预检")
    assert len(_tool_rows_t179(db_t179)) == 1


def test_refund_precheck_checks_the_ledger_no_not_the_display_no_t179(db_t179, ledger_t179):
    """客户报的单号 ≠ 台账单号：预检要按绑定解析出的 query_key 查台账（与 desk.py 同口径）。"""
    store = SqliteStore(str(db_t179))
    _bind_t179(store, "MY-REFUND-t179", PRECHECK_NO)
    frames, stdout = _run_t179(db_t179, ledger_t179, [
        ("cs_refund_precheck", _order_args_t179("MY-REFUND-t179", reason_text="七天无理由退货"))])
    body = _payload_t179(frames, 10)
    assert (body["outcome"], body["ok"], body["refused_why"]) == ("ok", True, ""), body
    assert body["rule_ref"]
    # 这里台账单号就是查单键：只进预检入参，一个字都不许出连接器。
    _assert_no_sentinels_t179(stdout, PRECHECK_NO)
    _assert_detail_clean_t179(_tool_rows_t179(db_t179))


def test_refund_precheck_refused_passes_refused_why_through_t179(db_t179, ledger_t179):
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("cs_refund_precheck", _order_args_t179(PRECHECK_NO, reason_text="就是想退"))])
    body = _payload_t179(frames, 10)
    assert (body["outcome"], body["ok"], body["refused_why"]) == ("ok", False, "reason_missing")


def test_refund_precheck_gates_identity_and_lookup_first_t179(db_t179, ledger_t179):
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("cs_refund_precheck", _order_args_t179(PRECHECK_NO, user=OTHER_USER,
                                                reason_text="七天无理由退货")),
        ("cs_refund_precheck", _order_args_t179("GHOST-t179", reason_text="七天无理由退货")),
    ])
    assert _payload_t179(frames, 10) == {"outcome": "identity_unverified"}
    assert _payload_t179(frames, 11) == {"outcome": "not_found"}


def test_refund_precheck_without_ledger_tenant_is_misconfigured_t179(db_t179, ledger_t179):
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("cs_refund_precheck", _order_args_t179(PRECHECK_NO, reason_text="七天无理由退货"))],
        env=_env_t179(ledger_tenant=False))
    assert _payload_t179(frames, 10) == {"outcome": "system_misconfigured"}
    assert _tool_rows_t179(db_t179) == []


# ---------------------------------------------------------------------------
# cs_handoff_list
# ---------------------------------------------------------------------------

def _seed_handoffs_t179(db: Path, n: int, *, tenant: str = TENANT) -> list[str]:
    store = SqliteStore(str(db))
    conv = conversation.open_conversation(store, tenant_id=tenant, channel=CHANNEL,
                                          open_kfid="wk_t179", external_userid=USER)
    ids = []
    for seq in range(1, n + 1):
        tid = turn_id_for(conv.conversation_id, seq)
        card = HandoffCard(handoff_id=tid, tenant_id=tenant, conversation_id=conv.conversation_id,
                           turn_id=tid, channel=CHANNEL, reason="complaint", intent="complaint",
                           customer_ref=mask_customer(USER), customer_text=CARD_TEXT,
                           recent_turns=((CARD_TEXT, "好的"),), suggestion=CARD_SUGGEST,
                           created_at=f"2026-09-25T00:00:0{seq}+00:00")
        conversation.record_handoff(store, card, now=f"2026-09-25T00:00:0{seq}+00:00")
        ids.append(tid)
    return ids


def test_handoff_list_returns_only_ids_and_enums_t179(db_t179, ledger_t179):
    ids = _seed_handoffs_t179(db_t179, 3)
    _seed_handoffs_t179(db_t179, 1, tenant="tnt-other")
    frames, stdout = _run_t179(db_t179, ledger_t179, [
        ("cs_handoff_list", {"tenant_id": TENANT}),
        ("cs_handoff_list", {"tenant_id": TENANT, "limit": 2}),
    ])
    full, top2 = _payload_t179(frames, 10), _payload_t179(frames, 11)
    assert [h["handoff_id"] for h in full["handoffs"]] == list(reversed(ids)), "最近的在前"
    assert full["count"] == 3
    assert [h["handoff_id"] for h in top2["handoffs"]] == list(reversed(ids))[:2]
    for h in full["handoffs"]:
        assert set(h) == {"handoff_id", "conversation_id", "reason", "delivery", "created_at"}
        assert (h["reason"], h["delivery"]) == ("complaint", "pending")
    _assert_no_sentinels_t179(stdout, CARD_TEXT, CARD_SUGGEST, mask_customer(USER),
                              "wk_t179", "customer")


# ---------------------------------------------------------------------------
# --tenant-map
# ---------------------------------------------------------------------------

def test_tenant_map_only_admits_mapped_tenants_t179(db_t179, ledger_t179):
    _seed_handoffs_t179(db_t179, 1)
    tmap = ("--tenant-map", json.dumps({"shop-a": TENANT}))
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("cs_order_status", _order_args_t179(MY_NO, tenant_id="shop-a")),
        ("cs_order_status", _order_args_t179(MY_NO)),                # 内部租户名直接来：不认
        ("cs_handoff_list", {"tenant_id": "shop-a"}),
        ("cs_handoff_list", {"tenant_id": TENANT}),
    ], extra_args=tmap)
    assert _payload_t179(frames, 10)["outcome"] == "ok"
    assert _payload_t179(frames, 11) == {"outcome": "identity_unverified"}
    assert _payload_t179(frames, 12)["count"] == 1
    assert _payload_t179(frames, 13) == {"handoffs": [], "count": 0}


# ---------------------------------------------------------------------------
# 协议层：错误码、单帧上限、坏参数
# ---------------------------------------------------------------------------

def test_protocol_errors_and_frame_cap_t179(db_t179, ledger_t179):
    huge = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                       "params": {"pad": "x" * (cs_server.MAX_FRAME_BYTES + 10)}})
    raw = (huge,
           json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}),
           "{not json")
    frames, _ = _run_t179(db_t179, ledger_t179, [
        ("no_such_tool", {}),
        ("cs_order_status", {"tenant_id": TENANT}),                        # 缺参数
        ("cs_order_status", _order_args_t179(MY_NO, lang="fr")),
        ("cs_handoff_list", {"tenant_id": TENANT, "limit": 0}),
        ("cs_order_status", _order_args_t179(MY_NO, amount="1")),           # 不认识的参数
        ("cs_order_status", _order_args_t179(MY_NO)),                       # 之后照常服务
    ], raw_lines=raw)
    assert 3 not in frames, "超长帧不许被解析"
    errors = frames[None]
    assert sorted(f["error"]["code"] for f in errors) == sorted([E_INVALID_REQUEST, -32700])
    assert frames[4]["error"]["code"] == E_METHOD_NOT_FOUND
    assert frames[10]["error"]["code"] == E_INVALID_PARAMS
    for i in (11, 12, 13, 14):
        assert frames[i]["result"]["isError"] is True, frames[i]
    assert _payload_t179(frames, 15)["outcome"] == "ok"


def test_bad_bytes_and_unhashable_tool_name_do_not_kill_the_server_t179(db_t179, ledger_t179):
    """非法 UTF-8 的一帧回 -32700、name 不是字符串回 E_INVALID_PARAMS —— 之后照常服务。"""
    lines = [b"\xff\xfe{\"x\": 1}",
             json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": ["x"]}}).encode(),
             json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": {"a": 1}}}).encode(),
             json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode()]
    argv = [sys.executable, "-m", "maos.tools.mcp.cs_server", "--db", str(db_t179),
            "--ledger", str(ledger_t179)]
    for lang in ("C.UTF-8", ""):
        env = _env_t179()
        env["LANG"] = lang
        proc = subprocess.run(argv, input=b"\n".join(lines) + b"\n", capture_output=True,
                              env=env, timeout=120, cwd=str(ROOT))
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert b"Traceback" not in proc.stderr
        frames = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        by_id = {f.get("id"): f for f in frames}
        assert by_id[None]["error"]["code"] == -32700
        assert by_id[5]["error"]["code"] == E_INVALID_PARAMS
        assert by_id[6]["error"]["code"] == E_INVALID_PARAMS
        assert [t["name"] for t in by_id[7]["result"]["tools"]] == [t["name"] for t in cs_server.TOOLS]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def test_registry_spec_is_readonly_and_not_auto_mounted_t179():
    spec = SERVERS["cs-mcp-server"]
    assert spec.readonly is True
    assert spec.module == "maos.tools.mcp.cs_server"
    assert spec.ports == ()
    assert all("cs-mcp-server" not in names for names in DEFAULT_ROLE_SERVERS.values())
    assert spec.argv()[1:] == ["-m", "maos.tools.mcp.cs_server", "--db", ":memory:"]


def test_registry_discovers_and_reconciles_the_cs_server_t179():
    spec = SERVERS["cs-mcp-server"]
    assert [t["name"] for t in discover(spec)] == list(spec.exposes)
    assert reconcile(spec) == []
