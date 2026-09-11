"""圆桌落库：AgentTeams 的事件链可核验（T113）。

在这之前，圆桌那一层**一个字节都不落**。房间里五岗说得再好，跑完就没了 ——
评委问「这五岗到底调了什么、说了什么、花了多少 token」，我们只有截图。
DAG 那一层的事件链早就落库并被 `scripts/verify.py` 核着，圆桌这一层是个洞。

本文件钉住把洞补上之后的九件事：

1. **`store=None` 逐字节不变**：不接 store 时一行都不写，屏幕输出与从前相同。
2. **一轮的形状**：`RoundtableRound` 1 条 + `RoundtableSeatSpoke` 5 条 +
   `RoundtableVerdict` 1 条，detail 里都带 `tenant_id` / `case_id`。
3. **两个 skill 首次留痕**：`refund.evidence_check` 与 `refund.risk_screen` 的
   `SkillInvoked`，`input_digest` / `output_hash` 由 `SkillInvoker` **自己**写。
4. **整表逐单**：一张表上逐单调用，`SkillInvoked` 条数 = 真跑过的行数。
5. **真模型有账**：每岗一条 `model_usage`，`latency_ms > 0`，`call_site` 已登记。
6. **Scripted 不写账**：假模型的 token 数是 `len(user)//4` 算的，印出来是假精确。
7. **回放对得上**：`replay_roundtable.py` 从 `event_log` 重建出的座次与实跑一致。
8. **真房间落文件库**：`MAOS_INGRESS_DB` 指到哪，`wire()` 就把 schema 建到哪。
9. **verify 收得下**：对含圆桌行的库直接跑 `check_hash_integrity` /
   `check_trace_tree` / `check_cost_attribution`，三项 PASS。第 9 条是 T114 把圆桌
   事件并进证据束的前提 —— 它红了，说明伪 plan_id 那条路走不通，要停手改契约。

零 Matrix、零网络。凡是要模型的地方一律**显式注入**假件：`maos/tests/conftest.py`
只清 `MATRIX_*`，不清 `MAOS_LLM_*`，这台机器 source 过密钥的 shell 里任何无参
`select_model_client()` 都会真打网关。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from maos.core.store import SqliteStore
from maos.model.client import ModelClient, ModelResponse, ScriptedModelClient
from maos.obs import trace as trace_mod
from maos.obs.call_sites import REGISTERED_CALL_SITES, unregistered_in_store
from maos.roundtable import stages
from maos.roundtable.speaker import CALL_SITE
from maos.roundtable.team import (PLAN_PREFIX, TEAM_ORDER, TITLES, RefundRoundtable,
                                  plan_id_of, sheet_plan_id)
from maos.skills.invoker import _digest
from maos.tests.test_roundtable_stages import ORDER, _case, ledger  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FACTS_HEAD = "【你手上的事实】\n"
SAID_HEAD = "\n\n【群里已有的发言】"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: 一轮圆桌的形状（契约 §B）。座位数就是名册长度 —— 写死 5 会让名册改了这里不红。
SEATS = len(TEAM_ORDER)


def _load_script(name: str):
    """加载 `scripts/` 下的脚本。范式与 `test_room_team_recheck.py` 逐字一致。"""
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load_script("replay_roundtable")
smoke = _load_script("room_team_smoke")
verify = _load_script("verify")


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class _Voice:
    def __init__(self, agent_id: str, said: list) -> None:
        self.agent_id = agent_id
        self.title = TITLES.get(agent_id, agent_id)
        self.user_id = f"@maos-{agent_id.removeprefix('refund-')}:maos.local"
        self.own_identity = True
        self._said = said

    def say(self, text: str) -> None:
        self._said.append((self.agent_id, text))


class _Voices:
    def __init__(self) -> None:
        self.said: list = []

    def voice(self, agent_id: str) -> _Voice:
        return _Voice(agent_id, self.said)


class _TokenModel(ModelClient):
    """原样回显事实卡、并报出 token 数的假模型。

    **不是 `ScriptedModelClient`**，所以 `Speaker` 认它是真模型 —— 那是「有模型」
    那条分支唯一的入口，也是 `model_usage` 唯一会被写的路。回显而不是回固定话，
    是为了让三道门全过（发言与事实卡逐字相同，数字自然是子集）。

    `sleep` 两毫秒是**为了让 `latency_ms > 0` 这条判据不是抛硬币**：`perf_counter`
    的差经 `int()` 截断，一次纯内存回显算下来常常正好是 0 毫秒，而那时这条断言
    在同一份代码上时红时绿。测试里可以 sleep，圆桌包里不行（那条守卫在
    `test_roundtable_team.py`）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.model = "fake-llm-1"

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.calls.append({"tier": tier})
        time.sleep(0.002)
        body = user.split(FACTS_HEAD, 1)[-1].split(SAID_HEAD, 1)[0]
        return ModelResponse(text=body, tokens_in=len(user) // 4,
                             tokens_out=len(body) // 4, model=self.model)


class _BrokenStore:
    """`append_event_log` 恒抛的库。用来钉「记账挂了不许把发言带崩」。"""

    def __init__(self) -> None:
        self.tries = 0

    def append_event_log(self, row: dict) -> None:
        self.tries += 1
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture()
def db_path(tmp_path) -> Path:
    """库落在 tmp 里的哪个文件。**用文件库而不是 `:memory:`**：本轨要证的正是
    「跑完还在」，而 `:memory:` 的库随连接消失，回放与 verify 都没东西可读。"""
    return tmp_path / "rt.db"


@pytest.fixture()
def store(db_path) -> SqliteStore:
    st = SqliteStore(str(db_path))
    st.init_schema()
    return st


@pytest.fixture()
def case(ledger: dict) -> tuple[dict, dict]:            # noqa: F811
    """质量问题那一单：批准、要举证、表里没带证据 → 证据岗判 missing。"""
    return _case(ledger, "quality_defect")


def _preflight(rt: RefundRoundtable, case, ledger, **kw) -> list:  # noqa: ANN001, F811
    payload, checked = case
    return rt.on_preflight(payload=payload, checked=checked, ledger=ledger,
                           evidence=[], requested_by="@boss:maos.local", **kw)


def _rows(path, plan_like: str = f"{PLAN_PREFIX}%") -> list[dict]:  # noqa: ANN001
    """库里圆桌那一摊 event_log，按 seq 升序，`detail` 就地解析。

    先断言文件在：`sqlite3.connect` 对一个不存在的路径会**凭空建一个空库**，
    于是「路径传错了」和「这一轮真的没落库」在断言里长得一模一样（实测：把
    fixture 名当值传进来，屏幕上只报 `no such table`，工作区还多出一个
    名叫 `<function db_path ...>` 的文件）。
    """
    assert Path(path).exists(), f"库不在：{path}（路径传错了，不是没落库）"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        out = []
        for r in conn.execute("SELECT * FROM event_log WHERE plan_id LIKE ?"
                              " ORDER BY seq", (plan_like,)):
            row = dict(r)
            row["detail"] = json.loads(row["detail"])
            out.append(row)
        return out
    finally:
        conn.close()


def _by_type(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["event_type"], []).append(r)
    return out


# --------------------------------------------------------------------------
# 1 不接 store 时逐字节不变
# --------------------------------------------------------------------------
def test_without_store_the_roundtable_writes_nothing(case, ledger, store, db_path):  # noqa: F811
    """🔴 `store=None` 是缺省，那条路上一行都不许落库。

    这是整轨的安全网：接线出错时该退化成「和从前一样」，而不是「写了半份账」。
    旁边**摆一个真库当探针**（建好、但不传给圆桌）—— 断言「没传进去的那个库是
    空的」，比断言「没调 append」更硬：后者拦不住某天有人在别处偷偷拿到 store。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices())
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])
    rt.on_sheet(rows=[], ledger=ledger, requested_by="@boss:maos.local")

    assert len(reports) == SEATS
    assert rt._store is None
    assert _rows(db_path) == [], "没接 store，库里却有行"
    assert store.list_model_usage() == []


def test_smoke_stdout_is_identical_with_and_without_db(tmp_path, monkeypatch):
    """🔴 `--db` 一个字都不许改屏幕输出。

    `test_room_team_recheck.PLAIN_STDOUT_MD5` 钉的是不带参数那条路；这条钉的是
    「带上 `--db` 之后还是同一份」。两条缺一不可：只有指纹那条时，`--db` 往
    stdout 多打一行也照样绿（那条测试根本不跑这个分支）。
    """
    import io

    monkeypatch.setattr(smoke, "ATTACHMENT_ROOT", tmp_path / "attachments")
    plain = io.StringIO()
    assert smoke.run(smoke.DEFAULT_SHEET, smoke.DEFAULT_LEDGER, out=plain) == smoke.EXIT_OK

    st = SqliteStore(str(tmp_path / "rt.db"))
    st.init_schema()
    with_db = io.StringIO()
    assert smoke.run(smoke.DEFAULT_SHEET, smoke.DEFAULT_LEDGER, out=with_db,
                     store=st) == smoke.EXIT_OK

    assert with_db.getvalue() == plain.getvalue()
    assert hashlib.md5(with_db.getvalue().encode()).hexdigest() == \
        hashlib.md5(plain.getvalue().encode()).hexdigest()


# --------------------------------------------------------------------------
# 2 一轮的形状
# --------------------------------------------------------------------------
def test_one_preflight_round_lands_one_round_five_spokes_one_verdict(
        case, ledger, store, db_path):  # noqa: F811
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    rows = _rows(db_path)
    kinds = _by_type(rows)
    assert len(kinds["RoundtableRound"]) == 1
    assert len(kinds["RoundtableSeatSpoke"]) == SEATS
    assert len(kinds["RoundtableVerdict"]) == 1

    case_id = str(case[1]["case_id"])
    for r in rows:
        assert r["plan_id"] == plan_id_of(case_id), "圆桌所有行共用一个伪 plan_id"
    for r in kinds["RoundtableRound"] + kinds["RoundtableSeatSpoke"] + \
            kinds["RoundtableVerdict"]:
        assert r["detail"]["tenant_id"] == "tnt-demo"
        assert r["detail"]["case_id"] == case_id

    rnd = kinds["RoundtableRound"][0]["detail"]
    assert rnd["entry"] == "preflight" and rnd["round_no"] == 1
    assert rnd["seats"] == list(TEAM_ORDER)
    assert [s["detail"]["seat"] for s in kinds["RoundtableSeatSpoke"]] == list(TEAM_ORDER)


def test_seat_spoke_carries_digests_not_the_words_themselves(
        case, ledger, store, db_path):  # noqa: F811
    """🔴 事实卡与发言的**原文一个字都不进 event_log**。

    事实卡里有订单号、金额、客户历史 —— 事件表不是它们该待的地方（铁律 6 同一
    条思路）。摘要够回答「这一轮说的还是不是同一段话」，那正是回放要证的事。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)

    spokes = _by_type(_rows(db_path))["RoundtableSeatSpoke"]
    assert len(spokes) == len(reports)
    for spoke, report in zip(spokes, reports):
        d = spoke["detail"]
        assert d["facts_digest"] == hashlib.sha256(
            report.facts.encode()).hexdigest()[:16]
        assert d["speech_digest"] == hashlib.sha256(
            report.speech.encode()).hexdigest()[:16]
        assert d["speech_len"] == len(report.speech)
        assert d["spoken_by_model"] is False
        assert d["fallback_reason"] == "no_model"
        # 没模型 = 一次调用都没有 = 没有用量行可回查，id 就该是空串而不是假 id。
        assert d["model_call_id"] == ""

    # `case_id` 本身是 `RC-<订单号>`，契约要求它在 detail 里 —— 所以判据落在
    # **除它之外**的字段上：事实卡与发言的原文、金额、逐条清单一个字都不许进来。
    blob = json.dumps([{k: v for k, v in s["detail"].items() if k != "case_id"}
                       for s in spokes], ensure_ascii=False)
    assert ORDER not in blob, "订单号漏进了 case_id 之外的字段"
    for report in reports:
        head = report.facts.splitlines()[0]
        assert head not in blob, f"事实卡原文漏进了事件表：{head!r}"
        assert report.speech not in blob, "发言原文漏进了事件表"


def test_verdict_event_records_the_recommendation_not_a_terminal_state(
        case, ledger, store, db_path):  # noqa: F811
    """合议事件记的是**建议**（铁律 8）：`recommend` 取自 `decide()`，
    `seats` 只落座位名 —— 五岗 data 的原样副本里有金额与缺口原话。"""
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    verdict = rt.verdict_of(reports, case[1])

    d = _by_type(_rows(db_path))["RoundtableVerdict"][0]["detail"]
    assert d["recommend"] == verdict.recommend
    assert d["approver_role"] == verdict.approver_role
    assert d["blockers"] == list(verdict.blockers)
    assert d["seats"] == sorted(verdict.seats), "只落座位名，不落 data 副本"
    assert all(isinstance(s, str) for s in d["seats"])


def test_router_style_decide_hook_is_what_lands_the_verdict(
        case, ledger, store, db_path):  # noqa: F811
    """🔴 `router._attach_verdict` 先探 `getattr(team, "decide", None)`。

    圆桌**提供**这个名字，真房间那条路才会落上合议事件 —— 本轨在 router 里只许加
    store 透传那一句。这条测试就是那个契约：签名与 router 的调用点一致。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)

    decide = getattr(rt, "decide", None)
    assert callable(decide), "router 探不到 team.decide，真房间就落不上合议事件"
    verdict = decide(reports, case_id=str(case[1]["case_id"]))

    kinds = _by_type(_rows(db_path))
    assert len(kinds["RoundtableVerdict"]) == 1
    assert kinds["RoundtableVerdict"][0]["detail"]["recommend"] == verdict.recommend


def test_a_store_that_throws_does_not_take_the_round_down(case, ledger):  # noqa: F811
    """圆桌是旁路观察者：记账挂了，五岗照样把话说完（红线 R4）。"""
    broken = _BrokenStore()
    voices = _Voices()
    rt = RefundRoundtable(ScriptedModelClient({}), voices, store=broken)
    reports = _preflight(rt, case, ledger)

    assert len(reports) == SEATS
    assert [a for a, _ in voices.said] == list(TEAM_ORDER)
    assert broken.tries >= SEATS + 1, "每一岗与开轮都该试过落库"


# --------------------------------------------------------------------------
# 3 两个 skill 首次留痕
# --------------------------------------------------------------------------
def test_evidence_and_risk_skills_finally_leave_a_skill_invoked_row(
        case, ledger, store, db_path):  # noqa: F811
    """🔴 T113 之前这两个 skill 在**全部证据里 0 条** `SkillInvoked`。

    根因是 `stages._invoke` 造 `SkillInvoker` 时不带 store，而 `_settle` 第一行
    `if self.store is None: return result` 直接早退。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    _preflight(rt, case, ledger)

    invoked = _by_type(_rows(db_path))["SkillInvoked"]
    names = sorted(r["detail"]["skill"] for r in invoked)
    assert names == [stages.EVIDENCE_SKILL, stages.RISK_SKILL]
    assert all(r["detail"]["status"] == "ok" for r in invoked)
    assert {r["plan_id"] for r in invoked} == {plan_id_of(str(case[1]["case_id"]))}


def test_skill_digests_are_written_by_the_invoker_not_recomputed_in_the_roundtable(
        case, ledger, store, db_path):  # noqa: F811
    """🔴 `input_digest` / `output_hash` 只许有**一份**实现。

    在圆桌里另算一份的症状是两份实现哪天分叉了，`scripts/verify.py` 第 1 项还是
    绿的 —— 它比的是自己算的那一份。

    两头夹：**正面**是直接调一次 `stages._evidence_check`，拿它返回的
    `SkillResult.output` 用 `invoker._digest` 复算，必须与库里那一行相等；
    **反面**是圆桌加工过的 `report.data`（多了 `added_evidence` / `material_gaps`）
    必须**不**等于它 —— 那正是「有人在圆桌这一层重算了一遍」会留下的指纹。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    evidence = next(r for r in reports if r.agent_id == "refund-evidence")

    row = next(r for r in _by_type(_rows(db_path))["SkillInvoked"]
               if r["detail"]["skill"] == stages.EVIDENCE_SKILL)
    d = row["detail"]
    assert HEX64.match(d["input_digest"]) and HEX64.match(d["output_hash"])
    assert d["invocation_id"] and len(d["invocation_id"]) == 32

    payload, checked = case
    res = stages._evidence_check(payload, checked, ledger)
    assert res.status == "ok", res.error
    # skill 的出参里带着**这一次** invoke 的 `invocation_id`（invoker 塞进
    # `ctx.extras` 的那个，一次调用一个新值，见 invoker 里的回归守卫），所以两次
    # 调用的出参天生不逐字相同。把那一个字段对齐之后其余必须一字不差 ——
    # 这比「两次调用哈希相等」更硬：它证明落库那份摘要正是 skill 出参本身。
    rebuilt = dict(res.output)
    rebuilt["invocation_id"] = d["invocation_id"]
    assert d["output_hash"] == _digest(rebuilt), "落库那份摘要与 skill 出参对不上"
    assert d["output_hash"] != _digest(evidence.data), (
        "落库的摘要等于圆桌加工后的 data —— 说明有人在这一层重算了一遍")


def test_finance_preview_stays_on_its_throwaway_store(
        case, ledger, store, db_path):  # noqa: F811
    """财务岗那三步是**预演不是执行**，仍走用完即弃的 `:memory:` 库。

    混进真库的症状：`refund.intake` / `policy.match` / `finance.settle` 的
    `SkillInvoked` 与 DAG 里真跑那三条长得一模一样，「这一单核算过没有」再也答不了。
    """
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    finance = next(r for r in reports if r.agent_id == "refund-finance")
    assert finance.data["preview_ran"] is True, "预演没跑通，这条测试的前提不成立"

    landed = {r["detail"]["skill"]
              for r in _by_type(_rows(db_path))["SkillInvoked"]}
    assert landed == {stages.EVIDENCE_SKILL, stages.RISK_SKILL}
    assert not landed & {"refund.intake", "policy.match", "finance.settle"}


# --------------------------------------------------------------------------
# 4 整表逐单
# --------------------------------------------------------------------------
def _row(line: int, case, reason_raw: str) -> dict:      # noqa: ANN001
    payload, checked = case
    return {"line": line, "order_id": ORDER, "reason_raw": reason_raw,
            "payload": payload, "checked": checked, "error": None,
            "problems": [], "warnings": []}


def test_sheet_mode_invokes_the_skills_once_per_live_row(
        ledger, store, db_path):  # noqa: F811
    """整表逐单：证据核验只看批准的行，风险筛查看所有预检走通的行。

    条数对不上时最可能的原因是有人把逐单调用改回了「整表只调一次」——
    那正是 2026-09-10 房间里三岗念空话的根因。
    """
    approve = _case(ledger, "quality_defect")
    reject = _case(ledger, "no_reason_return")
    rows = [_row(2, approve, "质量问题"), _row(3, approve, "质量问题"),
            _row(4, reject, "无理由")]
    assert reject[1]["decision"] == "reject", "语料变了，这条测试的分母要重取"

    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    rt.on_sheet(rows=rows, ledger=ledger, requested_by="@boss:maos.local")

    landed = _by_type(_rows(db_path))["SkillInvoked"]
    evidence = [r for r in landed if r["detail"]["skill"] == stages.EVIDENCE_SKILL]
    risk = [r for r in landed if r["detail"]["skill"] == stages.RISK_SKILL]
    assert len(evidence) == 2, "证据核验 = 裁定批准的行数"
    assert len(risk) == 3, "风险筛查 = 预检走通的行数"
    # 逐单的 task_id 带行号与订单号：整表十几单共用一个字面量的话，
    # 「第 3 行那单核验了没有」这个问题答不了。
    assert sorted(r["task_id"] for r in evidence) == \
        [f"sheet-evidence-2-{ORDER}", f"sheet-evidence-3-{ORDER}"]


def test_sheet_mode_plan_id_is_a_digest_not_someone_elses_case_id(
        ledger, store, db_path):  # noqa: F811
    """整表没有单一 case_id：plan_id 用 rows 的摘要，`detail.case_id` 如实留空。"""
    rows = [_row(2, _case(ledger, "quality_defect"), "质量问题")]
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    rt.on_sheet(rows=rows, ledger=ledger, requested_by="@boss:maos.local")

    landed = _rows(db_path)
    plan_id = sheet_plan_id(rows)
    assert plan_id.startswith(f"{PLAN_PREFIX}sheet:")
    assert re.match(r"^roundtable:sheet:[0-9a-f]{12}$", plan_id)
    assert {r["plan_id"] for r in landed} == {plan_id}

    rnd = _by_type(landed)["RoundtableRound"][0]["detail"]
    assert rnd["entry"] == "sheet" and rnd["case_id"] == ""
    assert rnd["sheet_digest"] == plan_id.rsplit(":", 1)[-1]
    assert rnd["tenant_id"] == "tnt-demo"
    # 同一份 rows 的摘要必须可复现，否则同一轮的事件会散进两个 plan。
    assert sheet_plan_id(rows) == plan_id


# --------------------------------------------------------------------------
# 5 / 6 模型用量
# --------------------------------------------------------------------------
def test_a_real_model_leaves_one_usage_row_per_seat(case, ledger, store):  # noqa: F811
    """🔴 每岗一条 `model_usage`：`tokens` 留住了、`latency_ms > 0`、`call_site` 已登记。

    从前 `speaker.py` 那一行 `.complete(...).text` 当场把 `ModelResponse` 丢掉，
    于是「圆桌烧了多少 token」这个问题在库里根本没有答案。
    """
    model = _TokenModel()
    rt = RefundRoundtable(model, _Voices(), store=store)
    reports = _preflight(rt, case, ledger)

    assert all(r.spoken_by_model for r in reports), "五岗都该真过一遍模型"
    usage = store.list_model_usage()
    assert len(usage) == SEATS
    assert [u["agent_role"] for u in usage] == \
        ["refund_intake", "refund_policy", "refund_evidence", "refund_risk",
         "refund_finance"]
    for u in usage:
        assert u["call_site"] == CALL_SITE
        assert u["call_site"] in REGISTERED_CALL_SITES
        assert u["plan_id"] == plan_id_of(str(case[1]["case_id"]))
        assert u["latency_ms"] > 0
        assert u["tokens_in"] > 0 and u["tokens_out"] > 0
        assert u["model"] == "fake-llm-1" and u["estimated"] == 0
        # 圆桌不属于任何 Run、不建 task：两个都留空是**如实**，不是忘了填。
        assert u["trace_id"] == "" and u["task_id"] is None
    assert unregistered_in_store(store) == []


def test_scripted_model_writes_no_usage_row(case, ledger, store):  # noqa: F811
    """Scripted 的 token 数是 `len(user)//4` 算的 —— 印成用量就是虚假的精确信号。"""
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)

    assert not any(r.spoken_by_model for r in reports)
    assert store.list_model_usage() == []


def test_model_call_id_pairs_with_the_usage_rows_in_order(
        case, ledger, store, db_path):  # noqa: F811
    """`model_call_id` 与用量行怎么互查。

    `model_usage` 的表结构是冻结的，**没有一列放得下这个 id**（`trace_id` 与
    `task_id` 都被 `verify.py` 第 8 项查着，`plan_id` 要留给「圆桌所有行共用一个」）。
    所以互查是按 `(plan_id, agent_role)` 的**写入顺序**对齐：event_log 里第 n 个
    带 `model_call_id` 的 `RoundtableSeatSpoke`，对应 `model_usage` 里同
    `(plan_id, agent_role)` 的第 n 段。id 本身带着 plan 与座位，读的人不必回表就
    知道该去哪一摊找。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    _preflight(rt, case, ledger)

    spokes = _by_type(_rows(db_path))["RoundtableSeatSpoke"]
    usage = store.list_model_usage()
    called = [s for s in spokes if s["detail"]["model_call_id"]]
    assert len(called) == len(usage) == SEATS

    for spoke, row in zip(called, usage):
        plan, role, uid = spoke["detail"]["model_call_id"].split("|")
        assert plan == spoke["plan_id"] == row["plan_id"]
        assert role == row["agent_role"]
        assert len(uid) == 32, "uuid4().hex，一次发言一个"
    assert len({s["detail"]["model_call_id"] for s in called}) == SEATS


def test_answer_hook_also_bills_through_the_single_call_site(store):
    """房间里 @某一岗的问答走的是同一个 `Speaker.complete` —— 也就是同一本账。

    绕开它的症状是「房间里 @了五轮，成本表上一行都没有」，而两边都不报错。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    text = rt.answer("refund-risk", "你手上装着什么")

    assert text
    usage = store.list_model_usage()
    assert len(usage) == 1
    assert usage[0]["call_site"] == CALL_SITE
    assert usage[0]["agent_role"] == "refund_risk"
    assert usage[0]["plan_id"] == f"{PLAN_PREFIX}answer"


# --------------------------------------------------------------------------
# 7 回放
# --------------------------------------------------------------------------
def test_replay_rebuilds_the_same_seat_order_that_actually_spoke(
        case, ledger, store, db_path):  # noqa: F811
    """回放只读 `event_log`、**一次模型都不调**，重建出的座次与实跑一致。"""
    voices = _Voices()
    rt = RefundRoundtable(ScriptedModelClient({}), voices, store=store)
    reports = _preflight(rt, case, ledger)
    verdict = rt.verdict_of(reports, case[1])

    db = str(db_path)
    conn = replay.connect(str(db_path))
    try:
        rounds = replay.rebuild(replay.read_events(
            conn, replay.plan_id_of(str(case[1]["case_id"]))))
    finally:
        conn.close()

    assert len(rounds) == 1
    rnd = rounds[0]
    assert [s["seat"] for s in rnd["seats"]] == [r.agent_id for r in reports]
    assert [s["seat"] for s in rnd["seats"]] == [a for a, _ in voices.said]
    assert rnd["seats_expected"] == list(TEAM_ORDER)
    assert rnd["entry"] == "preflight" and rnd["round_no"] == 1
    assert rnd["verdict"]["recommend"] == verdict.recommend
    assert sorted(s["skill"] for s in rnd["skills"]) == \
        [stages.EVIDENCE_SKILL, stages.RISK_SKILL]


def test_replay_reports_two_rounds_for_a_rechecked_case(
        case, ledger, store, db_path):  # noqa: F811
    """复检那条路一单两轮：回放要分成两段，而不是把十岗糊成一轮。"""
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    _preflight(rt, case, ledger, round_no=1, added_evidence=0)
    _preflight(rt, case, ledger, round_no=2, added_evidence=1)

    conn = replay.connect(str(db_path))
    try:
        rounds = replay.rebuild(replay.read_events(
            conn, replay.plan_id_of(str(case[1]["case_id"]))))
    finally:
        conn.close()

    assert [r["round_no"] for r in rounds] == [1, 2]
    assert all(len(r["seats"]) == SEATS for r in rounds)
    assert all(not r["headless"] for r in rounds)


def test_replay_on_an_unknown_case_says_what_the_db_does_have(store, db_path, capsys):
    """case 号敲错一位时，只说「没找到」等于把人推去写 SQL。"""
    rc = replay.main(["--db", str(db_path), "--case", "RC-NOPE"])
    out = capsys.readouterr().out
    assert rc == replay.EXIT_DATA
    assert "roundtable:RC-NOPE" in out
    assert "--db" in out, "空库时要指出「跑冒烟带上 --db 才会落库」"


# --------------------------------------------------------------------------
# 8 真房间落文件库
# --------------------------------------------------------------------------
def test_ingress_db_env_makes_wire_build_a_file_backed_store(tmp_path, monkeypatch):
    """`MAOS_INGRESS_DB` 指到哪，`wire()` 就把 schema 建到哪（跨轨契约 §F）。

    不设它时仍是 `:memory:` —— 缺省行为一个字不动。
    """
    from hiclaw import room_ingress

    class _Channel:
        def __init__(self) -> None:
            self.listened = False

        def listen(self, on_message, on_attachment) -> None:
            self.listened = True

    db = tmp_path / "ingress.db"
    monkeypatch.setenv("MAOS_INGRESS_DB", str(db))
    router = room_ingress.wire(_Channel(), room_id="!demo:maos.local")

    assert db.exists(), "wire() 没把库建到 MAOS_INGRESS_DB 指的地方"
    conn = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"event_log", "model_usage", "plan", "task"} <= names
    assert router.store is not None

    # 幂等：同一个文件库再 wire 一次不许炸（房间重启是常态）。
    room_ingress.wire(_Channel(), room_id="!demo:maos.local")


def test_router_hands_its_store_to_the_roundtable(store):
    """router 与圆桌共用同一个 store —— 圆桌是先建出来才传进来的，接线只能在那里补。"""
    from maos.ingress.router import IngressRouter

    rt = RefundRoundtable(ScriptedModelClient({}), _Voices())
    assert rt._store is None
    IngressRouter({}, store=store, team=rt)

    assert rt._store is store
    assert all(sp.store is store for sp in rt._speakers.values()), \
        "五个 Speaker 也要一起换，否则事件落得下、用量一行没有"


def test_attach_store_is_idempotent_and_reaches_every_speaker(store):
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices())
    rt.attach_store(store)
    rt.attach_store(store)

    assert rt._store is store
    assert {id(sp.store) for sp in rt._speakers.values()} == {id(store)}


# --------------------------------------------------------------------------
# 9 verify 收得下（T114 的前提）
# --------------------------------------------------------------------------
def _verify_case(db_path: str, directory: Path):
    """照 `verify.load_cases` 的形状手搭一个 Case，只是证据来自现搭的库。"""
    bundle = trace_mod.export_trace_bundle(db_path)
    trace = json.loads(json.dumps(bundle, ensure_ascii=False))
    conn = verify.connect_ro(db_path)
    return verify.Case(name="roundtable", directory=str(directory), db_path=db_path,
                       conn=conn, tables=verify.table_names(conn), trace=trace,
                       result={}, expect_sha=None, evidence_root=str(directory))


def test_verify_accepts_a_db_that_contains_roundtable_rows(
        case, ledger, store, db_path, tmp_path):  # noqa: F811
    """🔴 verify 的三项对含圆桌行的库必须 PASS。

    这条是 T114 把圆桌事件并进证据束的前提。它红了意味着伪 plan_id 那条路走不通
    （`roundtable:` 的行被树或成本核验拒收），那时该做的是**停手改契约**，
    而不是给圆桌造 plan 行 —— 后者会让 DAG 的证据束里多出几棵不是任务的树。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    db = str(db_path)
    vcase = _verify_case(db, tmp_path)
    try:
        hashes = verify.check_hash_integrity([vcase])
        tree = verify.check_trace_tree([vcase])
        cost = verify.check_cost_attribution([vcase])
    finally:
        vcase.conn.close()

    for chk in (hashes, tree, cost):
        assert chk.status == verify.PASS, f"{chk.key} 没过：{chk.notes}"
    assert cost.total >= SEATS, "成本核验一次判据都没执行 —— 这条守卫在空转"
    assert tree.total > 0, "树核验一次判据都没执行"
    # hash-integrity 在这个库上 `total == 0` 是**对的**：它的反向判据是「库里有、
    # 证据里没有的调用」，而圆桌那些行的 plan_id 在 plan 表里根本不存在，于是
    # 被跳过。这条断言钉的正是那个跳过 —— 库里确实有它本可以点名的行。
    invoked = [r for r in _rows(db_path) if r["event_type"] == "SkillInvoked"]
    assert invoked, "库里一条 SkillInvoked 都没有，这条测试的前提不成立"
    assert hashes.total == 0, (
        "hash-integrity 开始对圆桌行较真了 —— 去看它是把这些行当成了某棵树的一部分"
        "（那说明有人给圆桌造了 plan 行），还是判据口径变了")


def test_verify_reports_the_roundtable_timeline_instead_of_warning_about_strays(
        case, ledger, store, db_path, tmp_path):  # noqa: F811
    """第 4 项把圆桌那一段**印出来**，而不是报一行「不在任何一棵树内」。

    T134 之前评委在这一栏读到的是 8 条读不懂的游离事件。现在是一行说得清的
    info：几轮、几岗、几次 skill、花了多少。`info` 而不是 `warn` —— 这不是缺口
    （warn 有基线，见 `test_verify_warn.py`），是「这一段现在有树了」这件事本身。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    vcase = _verify_case(str(db_path), tmp_path)
    try:
        chk = verify.check_trace_tree([vcase])
    finally:
        vcase.conn.close()

    assert chk.status == verify.PASS, chk.notes
    assert not [n for n in chk.notes if n.startswith("warn:")], \
        f"圆桌有树了就不该再出 warn：{chk.notes}"
    line = next(n for n in chk.notes if "圆桌" in n)
    assert line.startswith("info:")
    for fragment in ("1 轮", f"{SEATS} 岗发言", "2 次 SkillInvoked", "1 次合议",
                     "replay_roundtable.py"):
        assert fragment in line, f"info 里少了 {fragment!r}：{line}"


def test_verify_catches_a_roundtable_event_that_is_in_no_family_at_all(
        case, ledger, store, db_path, tmp_path):  # noqa: F811
    """🔴 把判据放宽成「有 `roundtable:` 前缀就豁免」时，第 4 项必须当场红。

    这是 T134 那条收窄判据的看门人。只看 warn 的话，「真收进树里」和「按前缀
    豁免掉」长得一模一样 —— 两种写法都让那四行 warn 消失。所以核验器**回库数**：
    库里每条带前缀的事件必须恰好出现在一个地方（某棵圆桌树，或 `stray_events`）。

    这里就地伪造那种放宽：把圆桌树摘掉、`stray_events` 仍留空 —— 于是那几条事件
    哪儿都不在。核验器要点名这件事，而不是让它静静过去。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    vcase = _verify_case(str(db_path), tmp_path)
    assert vcase.trace["roundtable_traces"], "前提：本该有一棵圆桌树"
    vcase.trace["roundtable_traces"] = []          # ← 伪造「按前缀豁免」
    vcase.trace["stray_events"] = []
    try:
        chk = verify.check_trace_tree([vcase])
    finally:
        vcase.conn.close()

    assert chk.status == verify.FAIL, "判据被放宽了却全绿 —— 看门人没上岗"
    hidden = [n for n in chk.notes if "哪儿都找不到" in n]
    assert len(hidden) == SEATS + 4, (
        f"库里 {SEATS + 4} 条带前缀的事件都该被点名，实际 {len(hidden)} 条")
    assert "判据被放宽成" in hidden[0]


def test_roundtable_rows_get_their_own_tree_not_a_fake_run(
        case, ledger, store, db_path):  # noqa: F811
    """圆桌那一摊进**自己那一族树**，而不是冒充一条 Run（T134 改了这条的期望）。

    T113 立这条时的期望是「如实落进 `stray_events` / `unattributed_usage`」——
    那时圆桌在 trace 里确实无处安放，承认这一点比藏起来好。T134 给了它一族树
    （`roundtable_traces`），所以期望跟着变：**那 8 条不再是游离的**。

    没变的是这条测试真正要挡的事，而且一个字节都不许松：

    * `plan_count` 仍是 **0** —— 多出一棵 plan 树才是该查的那种红（给圆桌造
      plan 行会让 DAG 的证据束凭空多几棵不是任务的树）；
    * 圆桌树的 `trace_id` 仍是**空串** —— 不编一个 Run id 让它看起来有归属。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    bundle = trace_mod.export_trace_bundle(str(db_path))
    assert bundle["plan_count"] == 0, "圆桌不许在证据里冒充一棵 Run 的树"
    assert bundle["traces"] == [], "圆桌不许进 plan 那一族"

    trees = bundle["roundtable_traces"]
    assert len(trees) == 1, f"该有一棵圆桌树，实际 {len(trees)} 棵"
    tree = trees[0]
    assert tree["plan_id"] == plan_id_of(str(case[1]["case_id"]))
    assert tree["case_id"] == str(case[1]["case_id"]), "去掉前缀就是 case_id"
    assert tree["trace_id"] == "", "圆桌不属于任何 Run，不许编一个 trace_id"
    assert tree["summary"]["by_event_type"] == {
        "RoundtableRound": 1, "RoundtableSeatSpoke": SEATS,
        "RoundtableVerdict": 1, "SkillInvoked": 2}
    assert tree["summary"]["round_count"] == 1
    assert tree["summary"]["seat_spoke_count"] == SEATS
    assert tree["summary"]["verdict_count"] == 1
    assert tree["summary"]["tree_errors"] == []

    # 收走了就不许再算游离 —— 同一条记录在证据里只能出现在一个地方。
    assert bundle["stray_events"] == [], (
        f"圆桌事件已有树，不该再算游离：{bundle['stray_events']}")
    assert bundle["unattributed_usage"] == []
    assert bundle["summary"]["unattributed_model_calls"] == 0
    assert bundle["summary"]["roundtable_model_calls"] == SEATS
    assert bundle["summary"]["roundtable_event_count"] == SEATS + 4
    assert bundle["summary"]["stray_event_count"] == 0
    # 成本归到了圆桌那一段：五岗各一条，token 数进 attributed 那一栏。
    assert tree["cost"]["calls"] == SEATS
    assert tree["cost"]["tokens_total"] > 0
    assert bundle["summary"]["attributed_tokens_total"] == tree["cost"]["tokens_total"]
    assert len(tree["model_usage"]) == SEATS, "五岗各一行用量，逐行列在树上"


def test_claiming_the_roundtable_does_not_excuse_real_strays(
        case, ledger, store, db_path):  # noqa: F811
    """🔴 判据是「**被树收走的**不算游离」，不是「plan_id 非空就不算游离」。

    这条是 T134 的看门人。把 `stray_events` 的判据写成「带 `roundtable:` 前缀就
    豁免」也能让那四行 warn 消失，而代价是**两类真该查的事件一起被藏掉**：

    * `plan_id` 是空串的（建 Plan 之前的调用，`stray_events` docstring 点名的那类）；
    * `plan_id` 指向一个**不存在的** Plan（`docs/BACKLOG.md ## task-t110` 那类，
      「被否决的计划」上的事件）。

    所以这里在同一个库里把三类掺在一起：只有圆桌那一摊该被收走，另外两条必须
    照旧被点名。少点名一条，这条测试就红 —— 那正是「判据被放宽了」的信号。
    """
    rt = RefundRoundtable(_TokenModel(), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])
    # 第二类与第三类：手写两条，plan 表里都查不到它们。
    store.append_event_log({"plan_id": "", "event_type": "SkillInvoked",
                            "detail": {"skill": "issue.aggregate"}})
    store.append_event_log({"plan_id": "plan_does_not_exist",
                            "event_type": "TaskCreationVetoed", "detail": {}})

    bundle = trace_mod.export_trace_bundle(str(db_path))
    strays = bundle["stray_events"]
    assert len(strays) == 2, f"两条真游离都要留下，实际 {strays}"
    assert {s["event_type"] for s in strays} == {"SkillInvoked", "TaskCreationVetoed"}
    assert {s["plan_id"] for s in strays} == {"", "plan_does_not_exist"}
    assert bundle["summary"]["stray_event_count"] == 2
    # 圆桌那一摊照旧被收走，两件事互不影响。
    assert bundle["summary"]["roundtable_event_count"] == SEATS + 4
    assert not any(s["plan_id"].startswith(PLAN_PREFIX) for s in strays)


# --------------------------------------------------------------------------
# 冒烟脚本的两个开关
# --------------------------------------------------------------------------
def test_events_out_without_db_is_a_hard_error(capsys):
    """不许静默：没有库就没有事件可导，而空文件与「真的没事件」长得一模一样。"""
    with pytest.raises(SystemExit) as exc:
        smoke.main(["--events-out", "/tmp/should-not-be-written.json"])
    assert exc.value.code != 0
    assert "--db" in capsys.readouterr().err


def test_events_out_carries_the_provenance_header_and_the_db_contents(
        case, ledger, store, db_path, tmp_path):                 # noqa: F811
    """`--events-out` 落的那份：首行出处（铁律 3），正文与库里逐条对得上。"""
    rt = RefundRoundtable(ScriptedModelClient({}), _Voices(), store=store)
    reports = _preflight(rt, case, ledger)
    rt.verdict_of(reports, case[1])

    target = tmp_path / "events.json"
    doc = smoke.export_events(str(db_path), str(target))

    lines = target.read_text(encoding="utf-8").splitlines()
    assert re.match(r"^# generated at \S+ from \S+$", lines[0]), lines[0]
    on_disk = json.loads("\n".join(lines[1:]))
    assert on_disk == doc

    rows = _rows(db_path)
    assert doc["summary"]["event_count"] == len(rows)
    assert doc["summary"]["by_type"]["RoundtableSeatSpoke"] == SEATS
    assert doc["summary"]["plans"] == [plan_id_of(str(case[1]["case_id"]))]
    assert doc["events"][0]["detail"]["entry"] == "preflight"
