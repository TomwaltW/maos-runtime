"""核验器的三个盲区，钉成回归测试（E-1 两个 + G-2 一个）。

这个项目最核心的那句话是「退款到没到账，权威在支付网关，不在我们库里」。
但直到 E-1 之前，系统持有的其实是「**有一张回执**」——回执的**内容**从头到尾
没有任何一层校验过，出处注释里的 sha 也没有任何一层比对过。于是两条路走得通：

A. 库里把 ``compensated`` 直接 ``UPDATE`` 成 ``settled``，复用那条现成的
   ``observed_state='failed'`` 回执。回执行在、``actor_invocation_id`` 属于一次**真的**
   ``payment.observe`` 调用 —— 第 3 项两问全过，7/7 PASS。
B. 把某个证据文件首行换成 ``# generated at 2020-01-01… from deadbeef``。格式合法，
   而它自称出自的代码根本不是这次核验的对象 —— 从前 ``load_evidence_json`` 只查
   前缀，sha 是什么完全不看，7/7 PASS。
C. 把 DONE 那个 plan 的 ``external_evidence`` 整条换成
   ``{"kind": "test_report", "ref": "完全编造的"}``，不动库、不动 trace.json。
   第 6 项从前只做两件事 —— 列表非空、``status == "succeeded"``，列表里装什么
   一个字都不验，7/7 PASS。同一个模式的第三个实例。（G-2）

为什么非得在这一层堵：``refund_case`` / ``payment_observation`` 两张表**不参与**
第 4 项 trace 重放（那一项比对 span 树与事件链），所以对这两张表的直接篡改，
全核验器只有第 3 项拦得住；出处注释根本不在任何一项的判据里；而第 4 项**不看
result.json**，于是外部判据那个列表成了整束证据里唯一一处「写什么就是什么」的
地方 —— 偏偏它就是用来证明「这单业务真的成了」的那一处。
三处都没有第二道兜底，没有这三组测试，这些洞会再回来一次。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
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
def _build_db(path: pathlib.Path, *, cases=(), observations=(), observer_ids=(),
              plans=(), artifacts=()) -> None:
    """一个只放退款两张表的最小库。

    `observer_ids` 落成 SkillInvoked 事件 —— 第 3 项就是从这里认「哪些
    invocation_id 是真的 payment.observe 调用」。

    `plans` / `artifacts` 是 G-2 加的：第 6 项要拿 `plan` 表当遍历面、
    拿 `artifact` 表回查外部判据，光有退款两张表不够。
    """
    store = SqliteStore(str(path))
    store.init_schema()
    objects.ensure_schema(store)
    for inv in observer_ids:
        store.append_event_log({
            "plan_id": PLAN, "trace_id": "tr-e1", "event_type": "SkillInvoked",
            "detail": {"skill": "payment.observe", "invocation_id": inv}})
    for plan_id, state in plans:
        store.insert_plan({"plan_id": plan_id, "trace_id": "tr-e1",
                           "goal": "回执得说得出内容", "state": state})
    for artifact in artifacts:
        store.insert_artifact(artifact)

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


# ===========================================================================
# 盲区 C：外部判据整条编造（G-2）
#
# 与上面 A/E 同一个模式的第三个实例：**只验有没有，不验说的是不是真的**。
# 第 6 项从前只做两件事 —— `external_evidence` 列表非空、`status == "succeeded"`，
# 列表里装什么一个字都不看。而第 4 项 trace-tree 的牙齿是「trace.json 与库重放
# 逐字节一致」，它**不看 result.json**。于是这个列表成了整束证据里唯一一处
# 「写什么就是什么」的地方，偏偏它就是用来证明「这单业务真的成了」的那一处：
# 把两条真判据换成 `{"kind": "test_report", "ref": "完全编造的"}`，不动库、
# 不动 trace.json，`verify.py` 照印 RESULT: 7/7 PASS、exit=0。
# ===========================================================================
def _report_artifact(artifact_id: str, *, plan_id: str = PLAN, task_id: str = "t-g2",
                     version: int = 1, kind: str = "test_report",
                     passed: int = 5, failed: int = 0) -> dict:
    """库里那份真产物。"""
    return {"artifact_id": artifact_id, "task_id": task_id, "plan_id": plan_id,
            "kind": kind, "version": version,
            "content": {"passed": passed, "failed": failed, "errors": 0}}


def _report_evidence(artifact_id: str, *, task_id: str = "t-g2", version: int = 1,
                     passed: int = 5, provenance: str = "task_result") -> dict:
    """result.json 里记的那条判据。字段形状抄自
    `make_evidence.py::derive_business_outcome` 判据一。
    """
    return {"kind": "test_report", "artifact_id": artifact_id, "task_id": task_id,
            "version": version, "passed": passed, "provenance": provenance}


def _observation_evidence(case_id: str, *, request_id: str | None = None,
                          observed_state: str = "settled", gateway_code: str = "10000",
                          actor: str = "inv-real") -> dict:
    """判据二的形状 —— 它指的不是产物，**没有 artifact_id**。
    `request_id` 的默认值跟着 `_build_db` 里 `f"req-{case_id}"` 那条走。
    """
    return {"kind": "payment_observation", "case_id": case_id, "tenant_id": TENANT,
            "request_id": request_id or f"req-{case_id}", "gateway_code": gateway_code,
            "observed_state": observed_state, "actor_invocation_id": actor,
            "provenance": "payment_observation"}


def _recorded(*, plan_id: str = PLAN, state: str = "DONE", evidence=(),
              unaudited: int = 0) -> dict:
    """result.json 里的一条 plan 记录（只留第 6 项读的那几个字段）。"""
    return {
        "plan_id": plan_id,
        "state": state,
        "business_outcome": {
            "status": "succeeded" if evidence else "undetermined",
            "basis": "external_evidence" if evidence else "no_external_evidence",
            "plan_state": state,
            "external_evidence": list(evidence),
            "unaudited_evidence_count": unaudited,
            "source": "derived-from-db-at-export-time",
        },
    }


@pytest.fixture
def outcome_case(tmp_path):
    """造一个能跑第 6 项的 `verify.Case`：库里的 plan/artifact + result.json 的 plans。"""
    opened = []

    def _make(*, recorded=(), name: str = "scenario-g2", **kw) -> "verify.Case":
        db = tmp_path / f"{name}.db"
        _build_db(db, **kw)
        conn = verify.connect_ro(str(db))
        opened.append(conn)
        return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                           tables=verify.table_names(conn), trace={},
                           result={"plans": list(recorded)})

    yield _make
    for c in opened:
        c.close()


def test_backed_evidence_passes_and_unaudited_stays_a_warn(outcome_case):
    """正面对照，同时钉死**两个维度不许混**。

    这条判据「来源未审计」（provenance=unknown，入库时绕开 on_task_result）
    却**回查得到** —— scenario 1/2/3/5 现在就是这个样子：真产物，只是没走事件。
    新判据管的是内容对不对得上库；把那条 warn 顺手升级成 FAIL，就是把已经
    兑现的真产物贬回脚手架，正是那条 warn 想防的事情的反面。
    """
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_recorded(evidence=[_report_evidence("art-real", provenance="unknown")],
                            unaudited=1)])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)
    assert any("来源未审计" in n for n in chk.notes), "warn 不许被新判据吃掉"


def test_fabricated_external_evidence_is_caught(outcome_case):
    """攻击 G 的回归钉：整条判据是编的 —— 不动库、不动 trace.json，只改 result.json。

    这就是派单 §2 那次实测：修前 `RESULT: 7/7 PASS`、exit=0。
    """
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_recorded(evidence=[{"kind": "test_report", "ref": "完全编造的",
                                       "note": "没有这份产物"}], unaudited=1)])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL, "外部判据整条编造却过关 —— 业务成功是自封的"
    assert (chk.passed, chk.total) == (0, 1)
    note = " ".join(chk.notes)
    assert "回查不到" in note and "artifact_id" in note, f"报错得说清缺什么，实际：{note}"


def test_evidence_pointing_at_a_missing_artifact_is_caught(outcome_case):
    """字段齐全、形状合法，指的那份产物库里没有 —— 比整条编造更像真的。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_recorded(evidence=[_report_evidence("art_0000deadbeef")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "查无此物" in " ".join(chk.notes)


def test_evidence_borrowed_from_another_plan_is_caught(outcome_case):
    """产物是**真的**，只是属于另一个 plan —— 不许 A 计划的产物给 B 计划背书。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-别家的", plan_id="plan-g2-other")],
        recorded=[_recorded(evidence=[_report_evidence("art-别家的")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "不能给本 plan 背书" in " ".join(chk.notes)


def test_self_check_artifact_cannot_back_the_outcome(outcome_case):
    """产物在、也属于本 plan，但它是 Agent 对自己的评价 —— README §3 写死了不算。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-自评", kind="patch_set")],
        recorded=[_recorded(evidence=[_report_evidence("art-自评")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "不是外部判据类" in " ".join(chk.notes)


def test_unknown_evidence_kind_is_refused(outcome_case):
    """取值域之外的 kind 一律判负：认不出来的东西回查不了，不能默认放行。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_recorded(evidence=[{"kind": "self_check", "artifact_id": "art-real"}])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "不是外部判据类" in " ".join(chk.notes)


def test_failing_report_cannot_back_the_outcome(outcome_case):
    """产物真、归属对、kind 也对，但这份报告自己就是红的 —— 背不了书。

    生成侧只把 `failed==0 and errors==0` 的报告装进判据
    （make_evidence.py::derive_business_outcome 判据一），核验侧照着倒推。
    """
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-红的", failed=2)],
        recorded=[_recorded(evidence=[_report_evidence("art-红的")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "自己就没过" in " ".join(chk.notes)


def test_report_evidence_with_doctored_version_is_caught(outcome_case):
    """指对了产物，却把 task/version 改成别的 —— 生成侧是逐个抄库里的，对不上就是改过。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real", version=1)],
        recorded=[_recorded(evidence=[_report_evidence("art-real", version=7)])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "与库里不符" in " ".join(chk.notes)


def test_payment_observation_evidence_is_backed_by_the_db(outcome_case):
    """判据二的正面对照：它没有 artifact_id，回查的是退款两张表。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        cases=[("case-g2", "settled")],
        observations=[("case-g2", "settled", "10000", "inv-real")],
        recorded=[_recorded(evidence=[_observation_evidence("case-g2")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)


def test_forged_payment_observation_evidence_is_caught(outcome_case):
    """回执是编的：case 真、状态真，`request_id` 库里没有这一行。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        cases=[("case-g2", "settled")],
        observations=[("case-g2", "settled", "10000", "inv-real")],
        recorded=[_recorded(evidence=[_observation_evidence("case-g2",
                                                            request_id="req-编造的")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "查无此回执" in " ".join(chk.notes)


def test_payment_observation_evidence_with_doctored_receipt_is_caught(outcome_case):
    """行在、request_id 也对，回执**说的内容**被改了 —— 与攻击 A 同一个理。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        cases=[("case-g2", "settled")],
        observations=[("case-g2", "processing", "10000", "inv-real")],
        recorded=[_recorded(evidence=[_observation_evidence("case-g2",
                                                            observed_state="settled")])])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "没有一行对得上" in " ".join(chk.notes)


def test_evidence_kinds_match_the_generator_side():
    """取值域在 verify 与 make_evidence 各存一份（核验器不 import 生成脚本），
    那就得有人守着它们别分叉 —— 与 E-1 那条 `test_criterion_matches_the_guard_side_table`
    同一个套路。生成侧新增一类判据而这里没跟上，新那类会被一律判负。
    """
    source = (ROOT / "scripts" / "make_evidence.py").read_text(encoding="utf-8")
    body = source.split("def derive_business_outcome")[1].split("\ndef ")[0]
    appended = set(re.findall(r'"kind":\s*"([a-z_]+)"', body))

    assert appended == set(verify.EXTERNAL_EVIDENCE_KINDS), (
        f"两边的外部判据取值域分叉了：生成侧装 {sorted(appended)}，"
        f"核验侧认 {sorted(verify.EXTERNAL_EVIDENCE_KINDS)}")


# ===========================================================================
# 盲区 D：结论有了牙齿，描述结论的那一层还是自述（H-1）
#
# 与 A/B/C 同一个模式的第四、第五个实例，只是往上挪了一层。G-2 把
# `external_evidence` 里**指得到的东西**做进了回查之后，第 6 项还剩两处没人查：
#
# D. FAILED 那一支只要 `business_outcome` 是个非空 dict 就 `chk.ok()`。于是
#    「库里 FAILED、result.json 也老实记 FAILED（躲开那条 state 比对）、
#    `business_outcome.status` 却写 succeeded」这一手一声不吭 —— H-1 在 1131795
#    上实测：`RESULT: 7/7 PASS`、exit=0、warn 一行不少。判负要判在**自称**上，
#    不是判在 state 上，因为 state 本来就是老实的，那正是它能躲过去的原因。
# E. `plan_state` / `basis` / `source` / `unaudited_evidence_count` 这四个字段
#    描述的是「这份结论是怎么来的」，一层校验都没有。危害不是伪造成功，是
#    **伪造干净**：把 `unaudited_evidence_count` 抹成 0，那条「来源未审计」的 warn
#    就凭空消失，七项读数一个不变（实测 warn 12 行掉到 11 行，仍 7/7 PASS）。
#    一屏没有 warn 的 7/7 比有 warn 的 7/7 更像「这套东西没问题」—— 而那条 warn
#    恰恰是评委判断「这份报告是不是脚手架」的唯一线索。
#
# 所以 warn 改按**列表里数出来的**条数印，不按报告自述的数字印：自述对不上判负，
# warn 照印不误。判据不许把 warn 吃掉（G-2 记进 DECISIONS 的那条口径）。
# ===========================================================================
def _failed_recorded(*, plan_id: str = PLAN) -> dict:
    """FAILED 那一支生成侧唯一写得出的形状（make_evidence.py::derive_business_outcome）。"""
    return {
        "plan_id": plan_id,
        "state": "FAILED",
        "business_outcome": {
            "status": "failed",
            "basis": "plan_failed",
            "plan_state": "FAILED",
            "external_evidence": [],
            "unaudited_evidence_count": 0,
            "source": "derived-from-db-at-export-time",
        },
    }


def _tampered(recorded: dict, **fields) -> dict:
    """把一条记录的 business_outcome 就地改几个字段 —— 攻击者动的就是这里，
    库和 trace.json 一个字节都不碰。
    """
    recorded["business_outcome"].update(fields)
    return recorded


def test_honest_failed_plan_passes(outcome_case):
    """正面对照：库里 FAILED、报告也老实记 FAILED，四个字段都对得上 —— 该过。"""
    case = outcome_case(plans=[(PLAN, "FAILED")], recorded=[_failed_recorded()])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)


def test_failed_plan_claiming_success_is_caught(outcome_case):
    """攻击 D 的回归钉：state 老实记 FAILED，`business_outcome.status` 写 succeeded。

    躲得过 state 比对（那一条对得上），也躲得过外部判据回查（那一段只在 DONE 里跑）。
    修前 `RESULT: 7/7 PASS`、exit=0。
    """
    case = outcome_case(
        plans=[(PLAN, "FAILED")],
        recorded=[_tampered(_failed_recorded(), status="succeeded")])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL, "失败的 Plan 自称业务成功却过关"
    assert (chk.passed, chk.total) == (0, 1)
    note = " ".join(chk.notes)
    assert "库里是 FAILED" in note and "自称" in note, f"报错得说清是自称，实际：{note}"


def test_failed_plan_claiming_success_with_a_matching_basis_is_caught(outcome_case):
    """同一手的加强版：连 `basis` 一起改圆，让这份 outcome 自己内部自洽。

    判据不能只做内部自洽比对 —— 那样「status 和 basis 一起改」就整套躲过去了。
    `status` 得钉在库里的 state 上，那是自洽改不动的地方。
    """
    case = outcome_case(
        plans=[(PLAN, "FAILED")],
        recorded=[_tampered(_failed_recorded(), status="succeeded",
                            basis="external_evidence")])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "库里是 FAILED" in " ".join(chk.notes)


def test_scrubbed_unaudited_count_is_caught_and_the_warn_survives(outcome_case):
    """攻击 E 的主形态：把 `unaudited_evidence_count` 抹成 0，抹掉那条 warn。

    两条断言缺一不可 —— 判负是新长的牙齿；**warn 仍在**是这颗牙齿不许咬掉的东西。
    warn 改按列表里 `provenance == "unknown"` 的实有条数印，抹自述的数字影响不了它。
    """
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_tampered(
            _recorded(evidence=[_report_evidence("art-real", provenance="unknown")],
                      unaudited=1),
            unaudited_evidence_count=0)])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL, "抹掉未审计条数却过关 —— 伪造干净比伪造成功更好使"
    note = " ".join(chk.notes)
    assert "unaudited_evidence_count" in note and "实有 1 条" in note, note
    assert any("来源未审计" in n for n in chk.notes), "判据把 warn 吃掉了"


def test_doctored_plan_state_is_caught(outcome_case):
    """`plan_state` 是 outcome 自己抄的一份 state 副本，从前没人拿它跟库比过。"""
    case = outcome_case(
        plans=[(PLAN, "FAILED")],
        recorded=[_tampered(_failed_recorded(), plan_state="DONE")])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "plan_state 自述" in " ".join(chk.notes)


def test_doctored_basis_is_caught(outcome_case):
    """`basis` 与 `status` 在生成侧是死的一一对应，对不上就是改过。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_tampered(_recorded(evidence=[_report_evidence("art-real")]),
                            basis="no_external_evidence")])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "basis 只能是" in " ".join(chk.notes)


def test_doctored_source_is_caught(outcome_case):
    """`source` 是这份结论的出身声明：改掉它等于声称这些数字不是从库里推出来的。"""
    case = outcome_case(
        plans=[(PLAN, "DONE")],
        artifacts=[_report_artifact("art-real")],
        recorded=[_tampered(_recorded(evidence=[_report_evidence("art-real")]),
                            source="hand-written-by-me")])
    chk = verify.check_business_outcome([case])

    assert chk.status == verify.FAIL
    assert "source 自述" in " ".join(chk.notes)


@pytest.mark.parametrize("state", ["DONE", "FAILED"])
def test_generator_output_satisfies_the_selfclaim_check(tmp_path, state):
    """守着 `TERMINAL_OUTCOME` / `OUTCOME_SOURCE` 别与生成侧分叉。

    与 `test_evidence_kinds_match_the_generator_side` 同一个套路（取值域两边各存
    一份，核验器不 import 生成脚本），只是这条不解析源码文本 —— 直接把生成侧的
    **真产出**喂给核验侧的判据。生成侧哪天改了那三支 if 或那个 source 常量而这里
    没跟上，全量证据束会整片判负，得先在这条测试上红。
    """
    make_evidence = _load_script("make_evidence")
    db = tmp_path / f"gen-{state}.db"
    _build_db(db, plans=[(PLAN, state)], artifacts=[_report_artifact("art-real")])
    conn = verify.connect_ro(str(db))
    try:
        outcome = make_evidence.derive_business_outcome(
            conn, PLAN, state, verify.table_names(conn), {"art-real": "task_result"})
    finally:
        conn.close()

    unaudited = sum(1 for e in outcome["external_evidence"]
                    if e.get("provenance") == "unknown")
    assert verify.outcome_selfclaim(state, outcome, unaudited) == [], (
        f"生成侧的真产出过不了核验侧的自述比对，两边分叉了：{outcome}")
