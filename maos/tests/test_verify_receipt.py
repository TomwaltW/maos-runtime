"""核验器的两个盲区，钉成回归测试（E-1）。

这个项目最核心的那句话是「退款到没到账，权威在支付网关，不在我们库里」。
但直到 E-1 之前，系统持有的其实是「**有一张回执**」——回执的**内容**从头到尾
没有任何一层校验过，出处注释里的 sha 也没有任何一层比对过。于是两条路走得通：

A. 库里把 ``compensated`` 直接 ``UPDATE`` 成 ``settled``，复用那条现成的
   ``observed_state='failed'`` 回执。回执行在、``actor_invocation_id`` 属于一次**真的**
   ``payment.observe`` 调用 —— 第 3 项两问全过，7/7 PASS。
B. 把某个证据文件首行换成 ``# generated at 2020-01-01… from deadbeef``。格式合法，
   而它自称出自的代码根本不是这次核验的对象 —— 从前 ``load_evidence_json`` 只查
   前缀，sha 是什么完全不看，7/7 PASS。

为什么非得在这一层堵：``refund_case`` / ``payment_observation`` 两张表**不参与**
第 4 项 trace 重放（那一项比对 span 树与事件链），所以对这两张表的直接篡改，
全核验器只有第 3 项拦得住；而出处注释根本不在任何一项的判据里。
没有这两组测试，这个洞会再回来一次。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import types

import pytest

from maos.core.store import SqliteStore
from maos.domain.refund import objects

ROOT = pathlib.Path(__file__).resolve().parents[2]

TENANT = "tnt-e1"
PLAN = "plan-e1"
SHA = "c1049c2234752e4e0b076e50f07c06853cdaf7a1"


def _load_script(name: str) -> types.ModuleType:
    """``scripts/`` 不是包，只能按路径加载（idiom 同 test_repro_path）。"""
    key = f"_e1_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load_script("verify")


# ---------------------------------------------------------------------------
# 造件
# ---------------------------------------------------------------------------
def _build_db(path: pathlib.Path, *, cases=(), observations=(), observer_ids=()) -> None:
    """一个只放退款两张表的最小库。

    `observer_ids` 落成 SkillInvoked 事件 —— 第 3 项就是从这里认「哪些
    invocation_id 是真的 payment.observe 调用」。
    """
    store = SqliteStore(str(path))
    store.init_schema()
    objects.ensure_schema(store)
    for inv in observer_ids:
        store.append_event_log({
            "plan_id": PLAN, "trace_id": "tr-e1", "event_type": "SkillInvoked",
            "detail": {"skill": "payment.observe", "invocation_id": inv}})

    conn = sqlite3.connect(str(path))
    try:
        for case_id, biz_status in cases:
            conn.execute(
                "INSERT INTO refund_case (tenant_id, case_id, channel_id, order_id,"
                " order_version, sku, reason_code, amount_claimed, biz_status, plan_id,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (TENANT, case_id, "ch-1", "ord-1", 1, "sku-1", "quality", 10.0,
                 biz_status, PLAN, "2026-08-29T00:00:00+00:00"))
        for i, (case_id, observed_state, code, actor) in enumerate(observations):
            # observed_at 逐条错开：(tenant, case, request_id, observed_at) 上有 UNIQUE，
            # 同一笔请求的多次轮询靠时刻区分。
            conn.execute(
                "INSERT INTO payment_observation (tenant_id, case_id, request_id,"
                " gateway_code, raw_receipt_json, observed_state, observed_at,"
                " actor_invocation_id) VALUES (?,?,?,?,?,?,?,?)",
                (TENANT, case_id, f"req-{case_id}", code,
                 json.dumps({"status": observed_state}), observed_state,
                 f"2026-08-29T00:00:{i:02d}+00:00", actor))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def make_case(tmp_path):
    """造一个 ``verify.Case``，第 3 项只读 conn / tables / name。"""
    opened = []

    def _make(name: str = "scenario-e1", **kw) -> "verify.Case":
        db = tmp_path / f"{name}.db"
        _build_db(db, **kw)
        conn = verify.connect_ro(str(db))
        opened.append(conn)
        return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                           tables=verify.table_names(conn), trace={}, result={})

    yield _make
    for c in opened:
        c.close()


def _write_evidence_file(path: pathlib.Path, doc, *, sha: str = SHA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# generated at 2026-08-29T05:38:49.232608+00:00 from {sha}\n"
    path.write_text(header + json.dumps(doc, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    """一束**出处自洽**的最小证据：INDEX.json + 一个场景目录 + 它的库。"""
    root = tmp_path / "evidence"
    _write_evidence_file(root / "INDEX.json", {"git_sha": SHA, "produced": []})
    scenario = root / "scenario-1"
    _write_evidence_file(scenario / "trace.json", {"spans": []})
    _write_evidence_file(scenario / "result.json", {"plans": []})
    _build_db(scenario / "maos.db")
    return root


def _verify_cli(evidence_root: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify.py"), "--evidence", str(evidence_root)],
        capture_output=True, text=True, cwd=str(ROOT))


# ===========================================================================
# 攻击 A：settled 背后那张回执说的是 failed
# ===========================================================================
def test_settled_backed_only_by_failed_receipt_is_caught(make_case):
    """攻击 A 的回归钉：库里直接改判 settled，复用现成的 failed 回执。

    这条回执**每一样都是真的** —— 行在库里，`actor_invocation_id` 属于一次真的
    `payment.observe` 调用。从前第 3 项两问全过；现在第三问（回执说的是什么）拦下。
    """
    case = make_case(cases=[("case-a", "settled")],
                     observations=[("case-a", "failed", "40005", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert chk.status == verify.FAIL, "网关说失败了，settled 却过关 —— 权威事实边界形同虚设"
    assert (chk.passed, chk.total) == (0, 1)
    note = " ".join(chk.notes)
    assert "failed" in note and "settled" in note, f"报错得说清回执说的是什么，实际：{note}"


def test_settled_backed_by_settled_receipt_passes(make_case):
    """正面对照：回执说到账了就该放行 —— 新判据不许把正常证据束判负。"""
    case = make_case(cases=[("case-ok", "settled")],
                     observations=[("case-ok", "settled", "10000", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)


def test_settled_passes_when_any_receipt_says_settled(make_case):
    """多次观察只要**有一条**说到账了就算数 —— 轮询期间的 processing 是正常轨迹。

    没有这条，一笔问了三次才结算的退款（processing/processing/settled）会被判负，
    而那恰恰是「终态是问出来的，不是猜出来的」最想展示的一束证据。
    """
    case = make_case(cases=[("case-poll", "settled")],
                     observations=[("case-poll", "processing", "10000", "inv-real"),
                                   ("case-poll", "settled", "10000", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)


def test_forged_observation_actor_is_still_caught(make_case):
    """攻击 B 的回归钉：改第 3 项时别把这条原有的牙拔了。"""
    case = make_case(cases=[("case-b", "settled")],
                     observations=[("case-b", "settled", "10000", "inv-forged")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert chk.status == verify.FAIL
    assert "不属于任何一次 payment.observe 调用" in " ".join(chk.notes)


def test_criterion_matches_the_guard_side_table():
    """判据在 guard 与 verify 各存一份（铁律 9：核验器不 import 业务域），
    那就得有人守着它们别分叉 —— 就是这条测试。
    """
    from maos.domain.refund import guard

    assert verify.AUTHORITATIVE_RECEIPT_STATE == dict(guard.AUTHORITATIVE_RECEIPT_STATE), \
        "两份判据分叉了：改一边必须改另一边（verify.py / guard.py 的注释都写着同源）"
    assert verify.AUTHORITATIVE_WRITER == guard.AUTHORITATIVE_WRITER


# ===========================================================================
# 攻击 E：出处注释里的 sha 是编的
# ===========================================================================
def test_forged_header_sha_is_caught(bundle):
    """攻击 E 的回归钉：首行格式合法，出处却是编的。"""
    target = bundle / "scenario-1" / "trace.json"
    body = target.read_text(encoding="utf-8").split("\n", 1)[1]
    target.write_text("# generated at 2020-01-01T00:00:00+00:00 from deadbeef\n" + body,
                      encoding="utf-8")

    with pytest.raises(verify.VerifyError) as excinfo:
        verify.load_cases(str(bundle), None)
    message = str(excinfo.value)
    assert "deadbeef" in message and SHA in message, \
        f"报错得同时说出它自称的和该是的，实际：{message}"


def test_forged_header_sha_stops_the_cli(bundle):
    """端到端：伪造出处不是「某一项没过」，是**没法开始核验**（exit=2）。"""
    target = bundle / "scenario-1" / "result.json"
    body = target.read_text(encoding="utf-8").split("\n", 1)[1]
    target.write_text("# generated at 2020-01-01T00:00:00+00:00 from deadbeef\n" + body,
                      encoding="utf-8")

    proc = _verify_cli(bundle)
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "不予采信" in proc.stderr


def test_dirty_suffix_is_tolerated(bundle):
    """`scenario-R5` 的七个文件首行恒带 `-dirty`，那是已认下的口径，不是篡改。

    `build_r5()` 在场景 1-7 已经把 `evidence/` 改脏之后才自算 sha，于是这个后缀
    必然出现。把它判负就是造一个新的假 FAIL —— 那比漏判还坏，因为它会训练人
    忽略红色输出（submission-checklist.md §A-2）。
    """
    r5 = bundle / "scenario-R5"
    _write_evidence_file(r5 / "trace.json", {"spans": []}, sha=f"{SHA}-dirty")
    _write_evidence_file(r5 / "result.json", {"plans": []}, sha=f"{SHA}-dirty")
    _build_db(r5 / "maos.db")

    cases = verify.load_cases(str(bundle), None)
    try:
        assert {c.name for c in cases} == {"scenario-1", "scenario-R5"}
        assert all(c.expect_sha == SHA for c in cases)
    finally:
        for c in cases:
            c.conn.close()


def test_index_that_contradicts_itself_is_caught(bundle):
    """INDEX.json 是全束唯一的出处锚点，它自己首尾不一就没有锚可言。

    否则攻击者改完文件首行，顺手把 INDEX 的首行也改了就万事大吉。
    """
    _write_evidence_file(bundle / "INDEX.json", {"git_sha": "deadbeef", "produced": []})

    with pytest.raises(verify.VerifyError) as excinfo:
        verify.load_cases(str(bundle), None)
    assert "自相矛盾" in str(excinfo.value)


def test_missing_header_is_still_refused(bundle):
    """原有判据不许退化：首行根本不是出处注释，照样不合规。"""
    target = bundle / "scenario-1" / "trace.json"
    target.write_text(json.dumps({"spans": []}), encoding="utf-8")

    with pytest.raises(verify.VerifyError) as excinfo:
        verify.load_cases(str(bundle), None)
    assert "首行不是出处注释" in str(excinfo.value)


def test_bundle_without_index_falls_back_to_shape_only(bundle):
    """没有 INDEX.json 的老束：只校验首行成形，不对着不存在的锚点判负。"""
    (bundle / "INDEX.json").unlink()
    _write_evidence_file(bundle / "scenario-1" / "trace.json", {"spans": []},
                         sha="0123456789abcdef0123456789abcdef01234567")

    assert verify.evidence_sha(str(bundle)) is None
    cases = verify.load_cases(str(bundle), None)
    try:
        assert [c.expect_sha for c in cases] == [None]
    finally:
        for c in cases:
            c.conn.close()
