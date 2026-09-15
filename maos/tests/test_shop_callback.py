"""电商 webhook 入站的验收 —— `maos/ingress/shop_callback.py`。

这份用例同时是**七个平台轨的样板**：每轨把 `_demo_verifier` 换成自己平台的
真验签器，其余断言（失败也落库、重投不回源、原文无损、不写状态）照抄。

核心那条是 `test_ingest_never_touches_order_state`：**回调只落原文 + 触发回源，
永不写状态**（模块头 D1）。它用的是结构性判据而不是行为判据 —— 见该用例的注释。
"""

from __future__ import annotations

import base64
import json

import pytest

from maos.core.store import SqliteStore
from maos.ingress.crypto import VerifyError
from maos.ingress import shop_callback as sc


SECRET = "shhh-webhook-secret"
BODY = json.dumps({"id": 1001, "order_number": "R-1001"}, sort_keys=True).encode("utf-8")


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


def _demo_verifier(*, raw: bytes, headers: dict) -> sc.VerifiedCallback:
    """参照验签器：HMAC-SHA256 + base64（Shopify / Woo / TikTok 共用的形状）。"""
    sc.verify_hmac_header(SECRET, raw, sc.header(headers, "X-Demo-Hmac-Sha256"))
    body = json.loads(raw)
    return sc.VerifiedCallback(
        event_id=sc.header(headers, "X-Demo-Webhook-Id"),
        topic=sc.header(headers, "X-Demo-Topic"),
        order_ref=str(body.get("order_number", "")),
    )


def _headers(*, event_id: str = "wh-0001", topic: str = "orders/updated",
             body: bytes = BODY, sign: bool = True) -> dict:
    h = {"X-Demo-Webhook-Id": event_id, "X-Demo-Topic": topic,
         "Content-Type": "application/json"}
    if sign:
        h["X-Demo-Hmac-Sha256"] = sc.hmac_sha256_base64(SECRET, body)
    return h


@pytest.fixture(autouse=True)
def _clean_registry():
    sc.reset_verifiers()
    sc._schema_ready.clear()
    sc.register_verifier("demo", _demo_verifier)
    yield
    sc.reset_verifiers()


def _rows(store) -> list[dict]:
    cur = store._conn.execute(
        "SELECT platform, event_id, topic, verify_result, note, raw_body_b64,"
        " headers_json FROM shop_callback ORDER BY id")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_valid_callback_is_accepted_and_asks_for_resync():
    store = _store()
    verdict = sc.ingest("demo", BODY, _headers(), store=store)
    assert verdict.accepted and verdict.http_status == 200
    assert verdict.verify_result == sc.VERIFY_OK
    assert verdict.event_id == "wh-0001"
    assert verdict.topic == "orders/updated"
    assert verdict.order_ref == "R-1001"
    assert verdict.should_resync is True


def test_raw_body_is_stored_losslessly():
    """落原文不落解析结果 —— 否则日后再也复算不出「当时签名到底对不对」。"""
    store = _store()
    sc.ingest("demo", BODY, _headers(), store=store)
    row = _rows(store)[0]
    assert base64.b64decode(row["raw_body_b64"]) == BODY


def test_non_utf8_body_survives_storage():
    """回调体不保证是 UTF-8。按 TEXT 存要先 decode，decode 那一刻证据就毁了。"""
    store = _store()
    blob = b"\x89PNG\r\n\x1a\n\xff\xfe binary-ish"

    def _blob_verifier(*, raw, headers):
        return sc.VerifiedCallback(event_id="wh-blob", topic="raw/blob", order_ref="")

    sc.register_verifier("demo", _blob_verifier)
    sc.ingest("demo", blob, _headers(sign=False), store=store)
    assert base64.b64decode(_rows(store)[0]["raw_body_b64"]) == blob


# ---------------------------------------------------------------------------
# 三条硬要求
# ---------------------------------------------------------------------------

def test_bad_signature_is_rejected_but_still_recorded():
    """要求 1：验签失败的也落库 —— 那是攻击证据，吞掉就没了。"""
    store = _store()
    bad = dict(_headers())
    bad["X-Demo-Hmac-Sha256"] = base64.b64encode(b"wrong" * 8).decode()
    verdict = sc.ingest("demo", BODY, bad, store=store)

    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    assert verdict.should_resync is False

    rows = _rows(store)
    assert len(rows) == 1                                  # 落了
    assert rows[0]["verify_result"] == sc.VERIFY_BAD_SIGNATURE
    assert base64.b64decode(rows[0]["raw_body_b64"]) == BODY


def test_missing_signature_header_is_rejected():
    store = _store()
    verdict = sc.ingest("demo", BODY, _headers(sign=False), store=store)
    assert verdict.verify_result == sc.VERIFY_BAD_SIGNATURE
    assert len(_rows(store)) == 1


def test_stale_timestamp_is_classified_separately():
    """时间戳闸与签名闸挡的不是同一件事，verify_result 必须分得开。"""
    store = _store()

    def _stale(*, raw, headers):
        raise VerifyError("时间戳偏差 9999s 超过 300s")

    sc.register_verifier("demo", _stale)
    verdict = sc.ingest("demo", BODY, _headers(), store=store)
    assert verdict.verify_result == sc.VERIFY_STALE_TIMESTAMP
    assert _rows(store)[0]["verify_result"] == sc.VERIFY_STALE_TIMESTAMP


def test_duplicate_delivery_records_twice_but_resyncs_once():
    """要求 3：去重走 processed_key。重投不回源 —— 否则平台重试会把我们自己限流。

    但**流水要有两行**：「平台重投了几次」这个问题的答案就在这张表里。
    """
    store = _store()
    first = sc.ingest("demo", BODY, _headers(), store=store)
    second = sc.ingest("demo", BODY, _headers(), store=store)

    assert first.duplicate is False and first.should_resync is True
    assert second.duplicate is True and second.should_resync is False
    assert second.accepted is True and second.http_status == 200   # 让平台停止重试
    assert len(_rows(store)) == 2


def test_dedup_is_scoped_per_platform():
    """两个平台用了同一个 event_id 不该互相顶掉 —— 幂等键带平台前缀。"""
    store = _store()
    sc.register_verifier("other", _demo_verifier)
    a = sc.ingest("demo", BODY, _headers(), store=store)
    b = sc.ingest("other", BODY, _headers(), store=store)
    assert a.duplicate is False and b.duplicate is False


def test_no_event_id_is_rejected():
    """router.py 的「没有 msg_id 一律放行」在这里反过来：没 id 就没法去重。"""
    store = _store()
    verdict = sc.ingest("demo", BODY, _headers(event_id="  "), store=store)
    assert verdict.accepted is False
    assert verdict.http_status == 400
    assert verdict.verify_result == sc.VERIFY_NO_EVENT_ID
    assert _rows(store)[0]["verify_result"] == sc.VERIFY_NO_EVENT_ID


def test_unregistered_platform_is_rejected_not_passed_through():
    store = _store()
    sc.reset_verifiers()
    verdict = sc.ingest("nobody", BODY, _headers(), store=store)
    assert verdict.accepted is False and verdict.verify_result == sc.VERIFY_NO_VERIFIER
    assert len(_rows(store)) == 1


# ---------------------------------------------------------------------------
# D1：回调永不写状态
# ---------------------------------------------------------------------------

def test_ingest_never_touches_order_state():
    """**结构性判据**：这个模块不许 import 任何 domain / 状态机模块。

    行为判据（「跑一遍看状态没变」）挡不住日后有人加一行写状态的代码 —— 那行
    在别的路径上照样能跑起来。import 面是结构性的：一个不认识 domain 的模块，
    在结构上就写不进业务状态，于是这条纪律不需要任何人盯着。
    """
    import ast
    import pathlib

    src = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = [m for m in imported
                 if m.startswith(("maos.domain", "maos.contracts", "maos.runtime",
                                  "maos.agents", "maos.skills"))]
    assert not forbidden, (
        f"shop_callback 不许 import 这些：{forbidden}\n"
        "回调只落原文 + 触发回源，永不写状态（模块头 D1）。要回源请由调用方拿 "
        "verdict.order_ref 去走 order.query 那条既有路径"
    )


def test_verdict_carries_no_order_status_field():
    """判决里不许出现订单状态 —— 有这个字段，迟早有人拿它去写库。"""
    fields = set(sc.CallbackVerdict.__dataclass_fields__)
    assert not (fields & {"status", "order_status", "state", "observed_state"})


def test_verify_results_do_not_collide_with_gateway_states():
    """回调不得引入 gateway 四态之外的第五个值，也不得复用那四个（两回事）。"""
    from maos.tools.gateway import STATUS_SETTLED

    assert STATUS_SETTLED not in sc.ALL_VERIFY_RESULTS


# ---------------------------------------------------------------------------
# 凭据与取证
# ---------------------------------------------------------------------------

def test_secret_headers_are_redacted_but_signature_is_kept():
    """凭据抹掉（铁律 6），签名头留着 —— 那是复算验签与取证的核心材料。"""
    store = _store()
    h = _headers()
    h["Authorization"] = "Bearer super-secret-token"
    sc.ingest("demo", BODY, h, store=store)

    stored = json.loads(_rows(store)[0]["headers_json"])
    assert stored["Authorization"] == "<redacted>"
    assert "super-secret-token" not in _rows(store)[0]["headers_json"]
    assert stored["X-Demo-Hmac-Sha256"] == h["X-Demo-Hmac-Sha256"]


def test_hmac_helper_matches_manual_computation():
    import hashlib
    import hmac as _hmac

    expected = base64.b64encode(
        _hmac.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()
    assert sc.hmac_sha256_base64(SECRET, BODY) == expected


def test_header_lookup_is_case_insensitive():
    assert sc.header({"X-Demo-Topic": "orders/updated"}, "x-demo-topic") == "orders/updated"
    assert sc.header({}, "missing", "fallback") == "fallback"


def test_fragment_parser_keeps_statements_behind_comment_blocks():
    """逐行剥注释，不是按 `;` 切了再判整段 —— 后者会把带注释头的 CREATE 整条吞掉，
    然后在下一条 CREATE INDEX 上报「no such table」，排查方向当场偏掉。"""
    stmts = sc.parse_fragment(
        "-- 一段抬头注释\n-- 还有一行\nCREATE TABLE IF NOT EXISTS t (a INT);\n"
        "-- 中间注释\nCREATE INDEX IF NOT EXISTS i ON t (a);\n")
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE IF NOT EXISTS t")
    assert stmts[1].startswith("CREATE INDEX IF NOT EXISTS i")


def test_fragment_parser_rejects_statements_outside_allowlist():
    """铁律 1：既有表结构禁改，只许新增表。一个能跑任意 SQL 的加载器拦不住 DROP。"""
    with pytest.raises(ValueError, match="不在允许子集"):
        sc.parse_fragment("DROP TABLE task;")
    with pytest.raises(ValueError, match="不在允许子集"):
        sc.parse_fragment("ALTER TABLE task ADD COLUMN x INT;")


def test_real_fragment_parses_to_expected_shape():
    """钉住实际片段文件本身 —— 防止日后往 .sql 里加了语句而加载器悄悄拒掉。"""
    from pathlib import Path

    stmts = sc.parse_fragment(
        Path(sc.__file__).with_name("schema_p11_t160.sql").read_text(encoding="utf-8"))
    assert len(stmts) == 2
    assert "shop_callback" in stmts[0]


def test_empty_order_ref_means_nothing_to_resync():
    """shop/app 级事件不带订单，没什么可回源的 —— 但仍然要落库。"""
    store = _store()

    def _shop_level(*, raw, headers):
        return sc.VerifiedCallback(event_id="wh-shop", topic="app/uninstalled", order_ref="")

    sc.register_verifier("demo", _shop_level)
    verdict = sc.ingest("demo", BODY, _headers(sign=False), store=store)
    assert verdict.accepted is True
    assert verdict.should_resync is False
    assert len(_rows(store)) == 1
