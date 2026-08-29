"""核验器 warn 的**基线**与两条新判据，钉成机器判据（T12）。

`docs/submission-checklist.md` §A-2 一直靠人肉数「跑出来是 N 行 / M 类」。人肉判据
会漂，而且漂的方向最坏：多出来的那一行淹在十几行里，没人发现；有人把一条判据删了
让 warn 变少，看上去反而像是修好了。这个文件把那张表变成会红的东西。

三组：

1. **基线**（`test_warn_baseline_*`）——「哪一项出几行 warn」逐项对，多一行、少一行、
   换一项都红。**少一行也红**是有意的：warn 归零从来不是目标，删掉一条判据同样能
   让它变少，而那是这个仓库能犯的最贵的错（`verify.py` 是给评委的答案）。
   顺带钉住 B 类（`test_report` 缺 `sandbox_mode`）与 C 类（事件不在任何一棵树内）
   **不许再出现** —— 那两类已在整合轮 5 归零，回来就是回归。

2. **E 类判据没有被削弱**（`test_orphan_receipt_*`）—— T12 把「有回执但 `biz_status`
   不是 settled」拆成了两支：收口在别的终态（compensated / rejected）是预期，停在
   中间态才点名。细化与删除的区别就在这几条：**真出问题时它仍然会报**。
   连同 settled 那三道判负一起钉，防的是「为了让 warn 归零把整条判据删掉」。

3. **`provenance="artifact_seeded"` 得兑现**（`test_seeded_provenance_*`）——
   这个标签是 T12 新加的，它让一份旁路入库的产物不再计进 `unsourced_artifacts`。
   于是必须有人查「自称的那条来源事件真的在吗」，否则给任意来路不明的产物贴上标签
   就能让 warn 消失，而 trace-tree 照旧满分 —— 那是拿一条 warn 换一个新洞。

跑 `make_evidence.py` 的那两条用例把产物落进 `tmp_path`（`--out`），**不碰工作区的
`evidence/`**：否则每次 pytest 之后 `git status` 都是几十行脏，别的轨会当成自己
搞坏的。实测整束 ~5 秒，不值得打 slow 标记。
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

TENANT = "tnt-t12"
PLAN = "plan-t12"


def _load_script(name: str) -> types.ModuleType:
    """``scripts/`` 不是包，只能按路径加载（idiom 同 test_verify_receipt）。"""
    key = f"_t12_{name}"
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


verify = _load_script("verify")


# ===========================================================================
# 1. warn 基线
# ===========================================================================
#: 「哪一项出几行 warn」。**这就是 submission-checklist.md §A-2 那张表的机器版**，
#: 改这里就要同步改那张表，反之亦然。
#:
#: * `trace-tree` 0 行 —— A 类**已归零**：最后一条旁路
#:   `agents/reviewer.py::review_after_gate`（scenario-1 / 2 / 6 / 7 各一份 review_note）
#:   已补上 `ArtifactSeeded`，三条旁路现在全部自报来源。该项因此不再出现在本表里，
#:   并进了下面的 `RETIRED_WARN_MARKERS` —— 再出现就是回归。
#: * `authoritative-fact` 1 行 = E 类残余：`scenario-7 case-s7-0002` 停在中间态
#:   `gateway_accepted` —— 有回执、案子却既没到 settled 也没收口，那是真该看一眼的。
#:   同场景的 `case-s7-0001` 收口在 `compensated`，是**预期**，出 info 不出 warn。
#:
#: T12 之前是 12 行 / 3 类（A 6 / D 4 / E 2）；T12 首轮收口到 5 行 / 2 类（A 4 / E 1）。
#: D 类（外部判据来源未审计）随 A 类的前两条旁路补上来源事件一起归零 —— 它数的就是
#: 那批产物，从 `business-outcome` 看过去。A 类剩下的 4 行由 T12 收尾轮补掉（授权改
#: `maos/agents/reviewer.py`，见 `docs/DECISIONS.md`），至此 **1 行 / 1 类**。
WARN_BASELINE = {
    "authoritative-fact": 1,
}

#: 已归零、**回来就是回归**的三类（B / C 由整合轮 5 的 Y-1 / Y-2 补掉，A 由 T12 收尾轮补掉）。
#: 按 warn 正文里的稳定字样认，不按行数认 —— 这两类的期望值恒为 0。
RETIRED_WARN_MARKERS = {
    "B 类（test_report 缺 sandbox_mode）": "执行路径不可审计",
    "C 类（事件不在任何一棵树内）": "不在任何一棵树内",
    "A 类（产物没有来源事件）": "没有来源事件",
}


@pytest.fixture(scope="module")
def verify_report(tmp_path_factory) -> dict:
    """整束证据现产一次，跑 `verify.py --json`，返回它的报告。

    `--out` 指向 tmp：工作区的 `evidence/` 一个字节都不许被这条测试碰
    （铁律 4：证据只能由真实命令产出，而那一份的产出时机归整合轮）。
    """
    out = tmp_path_factory.mktemp("t12-evidence")
    made = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_evidence.py"), "--out", str(out)],
        capture_output=True, text=True, cwd=str(ROOT))
    assert made.returncode == 0, f"make_evidence 没跑成：{made.stderr[-2000:]}"

    ran = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify.py"),
         "--evidence", str(out), "--json"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert ran.returncode == 0, f"verify 没跑成（exit={ran.returncode}）：{ran.stdout[-2000:]}"
    # `--json` 之后照旧会跟一段人类可读的 RESULT/证据来源，所以只解析 JSON 那一段。
    # 不去改 render() 把尾巴掐掉：那是核验器对外的输出形状，动它是另一件事。
    return json.JSONDecoder().raw_decode(ran.stdout)[0]


def _warns(report: dict) -> dict[str, list[str]]:
    """按项收 warn 行。`info:` 不算 —— 「查过了，是预期」不是「值得看一眼」。"""
    return {c["key"]: [n for n in c["notes"] if n.startswith("warn:")]
            for c in report["checks"]}


def test_warn_baseline_per_check(verify_report):
    """逐项对行数。多一行是新缺口，少一行**同样红** —— 可能是判据被删了。"""
    actual = {k: len(v) for k, v in _warns(verify_report).items() if v}
    assert actual == WARN_BASELINE, (
        f"warn 分布与基线不符。\n基线：{WARN_BASELINE}\n实测：{actual}\n"
        f"多出来的先查是不是新缺口；少掉的先查是不是有人为了让 warn 归零删了判据 ——"
        f"留一个有理由的 warn，好过删一条判据。\n"
        f"确认是预期变化，就同步改本文件的 WARN_BASELINE 和"
        f" docs/submission-checklist.md §A-2 那张表。")


def test_warn_total_line_count(verify_report):
    """总行数单独钉一条：checklist §A-2 那句「跑出来的 warn 是 N 行」对的就是它。"""
    total = sum(len(v) for v in _warns(verify_report).values())
    assert total == sum(WARN_BASELINE.values()), (
        f"warn 总行数 {total}，基线 {sum(WARN_BASELINE.values())}")


def test_retired_warn_classes_stay_retired(verify_report):
    """B 类与 C 类不许再出现 —— 出现了就是回归，不是「已知缺口」。"""
    all_notes = "\n".join(n for v in _warns(verify_report).values() for n in v)
    for label, marker in RETIRED_WARN_MARKERS.items():
        assert marker not in all_notes, (
            f"{label} 又回来了：warn 里出现了 {marker!r}。这一类已在整合轮 5 归零，"
            f"再出现是回归，不是已知缺口。")


def test_verify_still_seven_of_seven(verify_report):
    """收口 warn 的过程中一次都不许把判据判坏 —— 7 项全 PASS，一项都不许 SKIP。"""
    bad = [c["key"] for c in verify_report["checks"] if c["status"] != "PASS"]
    assert not bad, f"这些项不是 PASS：{bad}"
    assert len(verify_report["checks"]) == 7, "核验器应恰好 7 项"


# ===========================================================================
# 2. E 类：细化不等于削弱
# ===========================================================================
def _build_refund_db(path: pathlib.Path, *, cases=(), observations=(),
                     observer_ids=()) -> None:
    """一个只放退款两张表的最小库（造法同 test_verify_receipt::_build_db）。"""
    store = SqliteStore(str(path))
    store.init_schema()
    objects.ensure_schema(store)
    for inv in observer_ids:
        store.append_event_log({
            "plan_id": PLAN, "trace_id": "tr-t12", "event_type": "SkillInvoked",
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
        for i, (case_id, observed_state, actor) in enumerate(observations):
            # observed_at 逐条错开：(tenant, case, request_id, observed_at) 上有 UNIQUE。
            conn.execute(
                "INSERT INTO payment_observation (tenant_id, case_id, request_id,"
                " gateway_code, raw_receipt_json, observed_state, observed_at,"
                " actor_invocation_id) VALUES (?,?,?,?,?,?,?,?)",
                (TENANT, case_id, f"req-{case_id}", "0000",
                 json.dumps({"status": observed_state}), observed_state,
                 f"2026-08-29T00:00:{i:02d}+00:00", actor))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def make_case(tmp_path):
    """造一个 `verify.Case`。第 3 项只读 conn / tables / name。"""
    opened = []

    def _make(name: str = "scenario-t12", **kw) -> "verify.Case":
        db = tmp_path / f"{name}.db"
        _build_refund_db(db, **kw)
        conn = verify.connect_ro(str(db))
        opened.append(conn)
        return verify.Case(name=name, directory=str(tmp_path), db_path=str(db), conn=conn,
                           tables=verify.table_names(conn), trace={}, result={})

    yield _make
    for c in opened:
        c.close()


def _notes_of(chk, prefix: str) -> list[str]:
    return [n for n in chk.notes if n.startswith(prefix)]


@pytest.mark.parametrize("mid_state", ["submitted", "approved",
                                       "gateway_accepted", "processing"])
def test_orphan_receipt_in_transit_still_warns(make_case, mid_state):
    """**判据没被削弱的证据**：有回执、没 settled、也没进任何收口分支 —— 必须报。

    这正是 §5.3 要的那条人为构造：案子停在中间态，回执却已经来了。T12 之前这会
    报一行 warn，之后仍然报 —— 细化改的是措辞与分流，不是牙齿。
    """
    case = make_case(cases=[("case-mid", mid_state)],
                     observations=[("case-mid", "processing", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    warns = _notes_of(chk, "warn:")
    assert len(warns) == 1, f"中间态 {mid_state} 的孤儿回执必须报出来，实际 notes={chk.notes}"
    assert "case-mid" in warns[0] and mid_state in warns[0], \
        f"报出来得说清是哪个 case、停在哪一态，实际：{warns[0]}"
    assert chk.status == verify.PASS, "这是 warn 不是判负 —— 观察到了但没收口，不是造假"


@pytest.mark.parametrize("terminal_state", ["compensated", "rejected"])
def test_orphan_receipt_settled_elsewhere_is_info_not_warn(make_case, terminal_state):
    """收口在别的终态上是**正确行为**，不该报成可疑。

    场景 7 的题眼就是「业务状态 compensated，全程没有经过 settled，settled 观察 0 条」。
    对着它报 warn，等于把设计意图报成缺口 —— 那正是 T12 要收的口。
    """
    case = make_case(cases=[("case-done", terminal_state)],
                     observations=[("case-done", "failed", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert not _notes_of(chk, "warn:"), \
        f"收口在 {terminal_state} 的案子不该报 warn，实际：{chk.notes}"
    infos = _notes_of(chk, "info:")
    assert len(infos) == 1 and terminal_state in infos[0], \
        f"但也不许一声不吭 —— 得留一条 info 说明查过了，实际：{chk.notes}"


def test_settled_without_receipt_still_fails(make_case):
    """细化的是**反面那一支**，settled 那三道判负一条没动 —— 这条钉住它。

    没有这一条，「让 warn 归零」最省事的写法就是把整个 orphan 分支连同 settled
    的判负一起删掉，而 7/7 照旧。
    """
    case = make_case(cases=[("case-fake", "settled")], observations=[],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert chk.status == verify.FAIL, "settled 没有回执 = 外部状态被写死为终态（铁律 8）"
    assert any("payment_observation" in n for n in chk.notes), \
        f"报错得说清缺的是什么，实际：{chk.notes}"


def test_multiple_receipts_on_one_case_warn_once(make_case):
    """一个 case 多条回执只出一行 warn —— 否则 §A-2 的行数随轮询次数漂。"""
    case = make_case(cases=[("case-poll", "gateway_accepted")],
                     observations=[("case-poll", "processing", "inv-real"),
                                   ("case-poll", "processing", "inv-real")],
                     observer_ids=["inv-real"])
    chk = verify.check_authoritative_fact([case])

    assert len(_notes_of(chk, "warn:")) == 1, \
        f"同一个 case 该只报一次，实际：{chk.notes}"


# ===========================================================================
# 3. provenance="artifact_seeded" 得兑现它自称的来源
# ===========================================================================
def _trace_with(artifact_attrs: dict, *, seeded_event: bool = True) -> dict:
    """一棵最小的树：一条 artifact span，外加（可选）它自称的那条来源 span。"""
    spans = [{
        "span_id": "sp-art", "parent_span_id": "sp-src", "name": "artifact:test_report",
        "kind": "artifact", "start": None, "end": None,
        "attributes": {"maos.artifact.id": "art-1", **artifact_attrs},
    }]
    if seeded_event:
        spans.append({
            "span_id": "sp-src", "parent_span_id": None,
            "name": "artifact-seeded:test_report", "kind": "event",
            "start": None, "end": None, "attributes": {},
        })
    return {"traces": [{"plan_id": PLAN, "spans": spans}],
            "summary": {"seeded_artifacts": 1}}


def _seeded_check(trace: dict):
    chk = verify.Check("trace-tree", "t12")
    case = verify.Case(name="scenario-t12", directory="", db_path="", conn=None,
                       tables=set(), trace=trace, result={})
    verify._check_seeded_provenance(chk, case)
    return chk


def test_seeded_provenance_honoured_passes():
    """正面对照：来源事件在、是 ArtifactSeeded、source 也写了 —— 放行。"""
    chk = _seeded_check(_trace_with({
        "maos.artifact.provenance": "artifact_seeded",
        "maos.artifact.provenance.event_span": "sp-src",
        "maos.artifact.provenance.source": "maos.flows.common.patch_verifier",
    }))
    assert chk.status == verify.PASS and (chk.passed, chk.total) == (1, 1)
    assert any("旁路入库" in n for n in _notes_of(chk, "info:")), \
        f"旁路本身要一眼看得见，实际：{chk.notes}"


def test_seeded_provenance_pointing_nowhere_fails():
    """贴了标签、来源事件却根本不存在 —— 判负。

    没有这一条，给任意来路不明的产物贴上 `artifact_seeded` 就能让它从
    `unsourced_artifacts` 里消失，A 类 warn 跟着消失，而 trace-tree 照旧满分。
    """
    chk = _seeded_check(_trace_with({
        "maos.artifact.provenance": "artifact_seeded",
        "maos.artifact.provenance.event_span": "sp-src",
        "maos.artifact.provenance.source": "谁编的都行",
    }, seeded_event=False))
    assert chk.status == verify.FAIL, "指不到来源 span 却自称有来源 —— 这是伪造"
    assert any("指不到来源" in n for n in chk.notes), f"实际：{chk.notes}"


def test_seeded_provenance_pointing_at_wrong_span_fails():
    """来源指到了一条**别的** span —— 同样判负，不能只查「指得到」。"""
    trace = _trace_with({
        "maos.artifact.provenance": "artifact_seeded",
        "maos.artifact.provenance.event_span": "sp-src",
        "maos.artifact.provenance.source": "maos.flows.common.patch_verifier",
    })
    trace["traces"][0]["spans"][1]["name"] = "state:DISPATCHED->AWAITING_REVIEW"
    chk = _seeded_check(trace)
    assert chk.status == verify.FAIL, "随手指一条状态迁移当来源，不算有来源"
    assert any("不是 ArtifactSeeded" in n for n in chk.notes), f"实际：{chk.notes}"


def test_seeded_provenance_without_source_fails():
    """有来源事件、却没说是谁产的 —— 审计链只补了一半，判负。"""
    chk = _seeded_check(_trace_with({
        "maos.artifact.provenance": "artifact_seeded",
        "maos.artifact.provenance.event_span": "sp-src",
        "maos.artifact.provenance.source": None,
    }))
    assert chk.status == verify.FAIL
    assert any("是谁产的" in n for n in chk.notes), f"实际：{chk.notes}"


def test_unknown_provenance_untouched_by_seeded_check():
    """`provenance=unknown` 不归这条判据管 —— 它照旧只被 A 类那行 warn 点名。

    两者不许混：一个是「审计链指不到」，一个是「指得到，只是没走正路」。
    """
    chk = _seeded_check(_trace_with({
        "maos.artifact.provenance": "unknown",
        "maos.artifact.provenance.event_span": None,
        "maos.artifact.provenance.source": None,
    }, seeded_event=False))
    assert chk.status == verify.PASS and chk.total == 0
