"""单案例端到端证据束（T114）—— 束里那几句话得是真的。

这份测试守的是四件事，每一件失败时都意味着一句**具体的谎**：

1. **缺省零影响**：不给 `roundtable=` / `plan_approval=` 时，`custom_case.run_payload`
   与从前逐字节相同 —— 不开圆桌、不停靠、一条审批事件都不落。红了意味着
   `scripts/run_case.py` 那条命令被本轨改了行为，而它是别轨的面。
2. **停靠点真的停得住**：人驳回之后 plan 停在 PENDING、一个任务都没派发。
   红了意味着「计划先给人看」这句话只是留了条事件，计划照跑不误。
3. **束里每句话都回查得到**：`skills.json` 的 8 个 skill 逐个如实标 present、
   `hitl-trace.json` 每条人做的动作都带操作者、机器判的动作**不许**记在人头上。
   红了意味着 Evidence Bundle 在替系统说好话。
4. **空补丁集根因**（§3.4）：`files` 空且 `summary` 非空是**合法结论**，
   空且无 summary 仍是错误。三处判据同源，红了意味着又分叉了。

跑一条真路径要十几秒，所以端到端那两条走 module 级 fixture，其余全部用手搭的
小库直接喂给收集函数 —— 判据在收集函数上，不必每条都跑一趟 DAG。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maos.agents.coding import CodingAgent  # noqa: E402
from maos.agents.refund import ROLE_PAYMENT  # noqa: E402
from maos.core.store import SqliteStore  # noqa: E402
from maos.domain.refund.case_pack import load_case_pack  # noqa: E402
from maos.flows import custom_case  # noqa: E402
from maos.runtime.gate import ReviewerGate  # noqa: E402
from maos.skills.builtin.refund.notify import NotifyCustomerSkill as NOTIFY  # noqa: E402
from maos.skills.invoker import SkillInvoker  # noqa: E402
#: 红线词表从 skill 那侧的测试里借，**不在这里抄第二份**（T144）：两份清单迟早
#: 漂成两套口径，而这条红线的全部意义就是只有一套（铁律 8 / 跨轨契约 §C）。
from maos.tests.test_reject_aftermath_t137 import FORBIDDEN  # noqa: E402
from maos.tools.sandbox import sandbox_git_apply  # noqa: E402

#: 脚本不是包，按路径装载（idiom 同 `maos/tests/test_generated_docs.py`）。
_SPEC = importlib.util.spec_from_file_location(
    "make_case_bundle", ROOT / "scripts" / "make_case_bundle.py")
MCB = importlib.util.module_from_spec(_SPEC)
sys.modules["make_case_bundle"] = MCB
_SPEC.loader.exec_module(MCB)

_SPEC_ME = importlib.util.spec_from_file_location(
    "make_evidence_for_case", ROOT / "scripts" / "make_evidence.py")
ME = importlib.util.module_from_spec(_SPEC_ME)
sys.modules["make_evidence_for_case"] = ME
_SPEC_ME.loader.exec_module(ME)

_SPEC_V = importlib.util.spec_from_file_location(
    "verify_for_case", ROOT / "scripts" / "verify.py")
VERIFY = importlib.util.module_from_spec(_SPEC_V)
sys.modules["verify_for_case"] = VERIFY
_SPEC_V.loader.exec_module(VERIFY)

#: 测试里的出处 sha。**故意不取真 sha**：临时目录里现产的证据没有出处可言
#: （`verify.check_provenance` 对非仓库交付束一律 SKIP，同一条理由）。
SHA = "testsha0000000000000000000000000000000000"

TRANSCRIPT = "# 房间逐字记录（fixture）\n\n@boss: /approve RC-2026-0904-001\n"


# ---------------------------------------------------------------------------
# 端到端：两条真路径，module 级只跑一次
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def payload() -> dict:
    return load_case_pack()


@pytest.fixture(scope="module")
def happy(tmp_path_factory, payload) -> Path:            # noqa: ANN001
    out = tmp_path_factory.mktemp("case-happy")
    note = out / "transcript.md"
    note.write_text(TRANSCRIPT, encoding="utf-8")
    MCB.build_path("happy", payload, str(out), sha=SHA, secrets={}, live=False,
                   transcript=str(note))
    return out / "happy"


@pytest.fixture(scope="module")
def gateway_fail(tmp_path_factory, payload) -> Path:     # noqa: ANN001
    out = tmp_path_factory.mktemp("case-fail")
    MCB.build_path("gateway_fail", payload, str(out), sha=SHA, secrets={}, live=False,
                   transcript=None)
    return out / "gateway_fail"


@pytest.fixture(scope="module")
def drift(tmp_path_factory, payload) -> Path:            # noqa: ANN001
    """漂移那条 —— 「缺席的 skill 也占一行」现在只剩它与 `reject` 钉得住（T144）。

    补偿收口之后补发的那条通知让 `gateway_fail` 成了 8/8，原先那条判据挂在它
    身上。漂移这条停在核算前面，三个 skill 确实没跑过，判据搬过来仍然是真的。
    """
    out = tmp_path_factory.mktemp("case-drift")
    MCB.build_path("drift", payload, str(out), sha=SHA, secrets={}, live=False,
                   transcript=None)
    return out / "drift"


def _read(path: Path) -> dict:
    """读一份束内 json：跳过首行出处注释（同 `make_evidence.load_evidence_json`）。"""
    with path.open(encoding="utf-8") as fh:
        first = fh.readline()
        assert first.startswith(ME.HEADER_PREFIX), f"{path} 首行不是出处注释（铁律 3）"
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. 缺省零影响
# ---------------------------------------------------------------------------
def test_default_run_opens_no_roundtable_and_no_approval_stop(payload):
    """不给两个开关时，返回的两块观测恒为 None，且一条计划审批事件都不落。

    这是「`scripts/run_case.py` 那条命令行为不变」的机器判据。只断言事件数为 0
    还不够 —— 观测块要是变成了空 dict，读的人就分不清「没接圆桌」与
    「接了但五岗一句话没说」，而后者是 bug。
    """
    import maos.flows.common as common

    seen: list = []
    original = common.SqliteStore

    def tracked(*args, **kwargs):                        # noqa: ANN001
        store = original(*args, **kwargs)
        seen.append(store)
        return store

    common.SqliteStore = tracked
    try:
        row = custom_case.run_payload(payload, verbose=False)
    finally:
        common.SqliteStore = original

    assert row["roundtable"] is None, "缺省不许开圆桌"
    assert row["plan_approval"] is None, "缺省不许停靠"
    kinds = {e["event_type"] for e in seen[-1].list_event_log(row["plan_id"])}
    assert kinds & {"PlanApproved", "PlanRejected", "PlanReplanned",
                    "PlanApprovalExhausted"} == set(), (
        "缺省路径落了计划审批事件 —— 停靠点悄悄生效了")


def test_unknown_approval_mode_is_refused_loudly():
    """`plan_approval` 写错一个词当场抛，不许静默退化成「不停靠」。

    静默退化的症状：束里 `plan_approval` 是 null，而命令行明明给了参数 ——
    读的人会以为这一跑没接停靠点，实际上是参数打错了。
    """
    with pytest.raises(ValueError, match="plan_approval"):
        custom_case.stop_for_plan_approval(None, None, "p", mode="approved",
                                           operator="谁", feedback="")


# ---------------------------------------------------------------------------
# 2. 停靠点停得住
# ---------------------------------------------------------------------------
def test_plan_reject_stops_the_dag_dead(payload):
    """人驳回之后：plan 停在 PENDING、一个任务都没离开 PENDING、落了 PlanRejected。

    这一条是本轨的题眼。红了意味着「计划先给人看、人批准了才许开跑」退化成了
    「先跑起来，顺便记一条人看过」。
    """
    import maos.flows.common as common

    seen: list = []
    original = common.SqliteStore

    def tracked(*args, **kwargs):                        # noqa: ANN001
        store = original(*args, **kwargs)
        seen.append(store)
        return store

    common.SqliteStore = tracked
    try:
        row = custom_case.run_payload(payload, verbose=False, plan_approval="reject",
                                      plan_feedback="风险档位没写清楚")
    finally:
        common.SqliteStore = original

    store = seen[-1]
    stop = row["plan_approval"]
    assert stop["started"] is False and stop["plan_state"] == "PENDING"
    assert [r["decision"] for r in stop["rounds"]] == ["reject"]
    assert row["plan_state"] == "PENDING"
    states = {t["state"] for t in row["tasks"]}
    assert states <= {"PENDING"}, f"驳回之后仍有任务被派发：{states}"
    kinds = [e["event_type"] for e in store.list_event_log(row["plan_id"])]
    assert "PlanRejected" in kinds and "PlanApproved" not in kinds
    assert stop["preview"]["runnable"] > 0, "preview 得告诉人这个计划批了跑得动"


def test_reject_then_approve_keeps_both_rounds_on_the_record(happy, gateway_fail):
    """`reject_then_approve` 两条事件都在，且顺序是先驳后批。

    借 gateway_fail 那一束反过来验：它走的是 `approve`，所以**不该**有 PlanRejected。
    只验有的一侧不够 —— 一个恒返 True 的实现同样能让「有」的那条绿。
    """
    fail_chain = _read(gateway_fail / "event-chain.json")
    assert "PlanRejected" not in fail_chain["by_type"]
    assert fail_chain["by_type"]["PlanApproved"] == 1


# ---------------------------------------------------------------------------
# 3. 束里每句话都回查得到
# ---------------------------------------------------------------------------
def test_every_file_in_the_bundle_carries_its_provenance(happy):
    """束里每个文本文件首行都是出处注释（铁律 3），且 INDEX 自己也登记了它们。"""
    index = _read(happy / "INDEX.json")
    listed = {f["name"]: f for f in index["files"]}
    for path in sorted(happy.iterdir()):
        with path.open(encoding="utf-8", errors="ignore") as fh:
            if not path.name.endswith(".db"):
                assert fh.readline().startswith(ME.HEADER_PREFIX), f"{path.name} 首行没有出处"
        # 索引自己不在清单里 —— 它是**最后**写的，把自己算进去就得先知道自己多大。
        if path.name == "INDEX.json":
            continue
        assert path.name in listed, f"{path.name} 没登记进 INDEX.json"
    assert index["git_sha"] == SHA
    assert index["model_mode"] == "scripted"


def test_room_transcript_is_taken_in_verbatim_with_a_header(happy):
    """`--room-transcript` 给的文件原样收进束，正文一字不改，首行由本脚本补出处。"""
    body = (happy / "room-transcript.md").read_text(encoding="utf-8")
    head, rest = body.split("\n", 1)
    assert head.startswith(ME.HEADER_PREFIX)
    assert rest == TRANSCRIPT, "逐字记录被改写了 —— 它是人工采集的原始证据"


def test_happy_path_shows_all_eight_contract_skills(happy):
    """顺利路径 8/8：契约 §G 的八个 skill 全部在库里留下了调用记录。

    两个圆桌 skill（证据/风险）尤其要在：它们在 `roundtable/stages.py` 里走的是
    用完即弃的 `:memory:` 库，不由本轨另调一次的话，这一束只数得到 6 个。
    """
    skills = _read(happy / "skills.json")
    assert skills["present"] == skills["total"] == 8
    names = {s["skill"] for s in skills["contract_skills"] if s["present"]}
    assert {"refund.evidence_check", "refund.risk_screen"} <= names
    for row in skills["contract_skills"]:
        assert row["invocation_id"] and row["input_digest"] and row["output_hash"]
        assert row["version"], f"{row['skill']} 没记版本 —— 「哪一版判的」答不出来"


def test_absent_skills_are_written_down_not_dropped(drift):
    """缺席的 skill 留在清单里标 `present=false`，不是从清单里删掉。

    删掉的话，一份自称完整的清单里悄悄少了一行 —— 比明写「这个没跑」难查得多。

    **判据从 `gateway_fail` 挪到 `drift`（T144）**：它从前钉的是「付款被拒之后
    通知岗不该跑过」，而补偿收口之后**该**补一条通知 —— 那条路因此成了 8/8。
    那是真相变好，不是判据失效，所以判据搬到仍然缺席的路径上，不是删掉了事。
    漂移这条停在核算前面，付款与通知那几岗确实一次都没跑过。
    """
    skills = _read(drift / "skills.json")
    listed = {s["skill"] for s in skills["contract_skills"]}
    assert len(listed) == 8, "清单条数得恒为 8，缺席的也占一行"
    assert skills["present"] < 8, "漂移停在核算前面，付款与通知那几岗不该跑过"
    absent = [s for s in skills["contract_skills"] if not s["present"]]
    assert absent and all(s["invocation_id"] is None for s in absent)


def test_the_compensated_case_actually_tells_the_customer(gateway_fail):
    """🔴 补偿收口那一束里，客户**收到了**一条通知，而且束里查得到发的是哪一句。

    这一束从前的样子：`notification` 零行、`skills_present` 7/8、
    `business_ref_missing` 里挂着 `notification`。三步收口（补偿、派单、关单）
    全做了，客户一个字都不知道 —— 真跑日房间那条会当场演出通知，评委回头翻束里
    却查不到（`docs/BACKLOG.md` 第 2382、2648 条）。

    「通知恰好一条」是判据的一半：多于一条就是同一件事发了两遍，那比不发更糟。
    """
    conn = sqlite3.connect(f"file:{gateway_fail / 'maos.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    notes = conn.execute("SELECT * FROM notification").fetchall()
    assert len(notes) == 1, f"补偿收口之后客户收到的通知不是恰好一条：{len(notes)}"

    events = conn.execute("SELECT detail FROM event_log WHERE event_type=?",
                          (NOTIFY.EVENT_CUSTOMER_NOTIFIED,)).fetchall()
    assert len(events) == 1, "说过的那句话没留痕，或留了不止一条"
    detail = json.loads(events[0]["detail"])
    assert detail["content_digest"] == notes[0]["content_digest"], (
        "事件里那句话与表上那条通知的摘要对不上 —— 留痕留的是另一条")

    # 补偿那一档的正文要给出**抓手**：工单号与凭证引用，客户拿它们去追问。
    assert "MT-" in detail["content"] and "工单" in detail["content"], (
        f"补偿那一档没给工单号，客户无从追问：{detail['content']}")

    # 束里（给评委看的那一份）直接读得到，不必翻 trace.json 的全量 detail。
    chain = _read(gateway_fail / "event-chain.json")
    said = [e for e in chain["events"]
            if e["event_type"] == NOTIFY.EVENT_CUSTOMER_NOTIFIED]
    assert len(said) == 1 and said[0]["detail"]["content"] == detail["content"]
    assert chain["missing_required"] == [], (
        f"这条路径的必需事件缺了：{chain['missing_required']}")
    assert _read(gateway_fail / "skills.json")["present"] == 8, "通知岗跑过了，该是 8/8"


def test_the_compensation_notice_never_says_the_money_arrived(gateway_fail):
    """🔴 铁律 8：这条**真跑路径**上发出去的正文，四个到账口径一个都不许有。

    今天那几条钉的是纯函数（`_compensation_tail` / `_default_content`）——
    它们证明得了措辞函数是对的，证明不了这一束里真发出去的那句话是对的。
    钱没退成、补偿只开了工单，说出任何一句到账都是替外部系统宣布了一个
    本系统无从观察的资金结果。
    """
    conn = sqlite3.connect(f"file:{gateway_fail / 'maos.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT detail FROM event_log WHERE event_type=?",
                       (NOTIFY.EVENT_CUSTOMER_NOTIFIED,)).fetchone()
    assert row is not None, "这条路径上没有通知，红线判据无从谈起"
    content = json.loads(row["detail"])["content"]
    for word in FORBIDDEN:
        assert word not in content, f"这一束发给客户的正文里说了 {word}：{content}"


def test_the_bundle_event_chain_never_leaks_customer_identity(gateway_fail):
    """🔴 事件链要连同整束交给外人逐条读 —— 客户身份一项都不许在里面。

    正文里本来只有案号、对外三态、工单号与凭证流水（`contract.security_boundary`）。
    姓名、手机、地址、收货凭证号躺在 `order_snapshot.payload_json` 里，搬一项进
    `detail`，这份束就再也不能原样交出去了 —— 而它正是要交出去的那一份。

    比的是**快照里的原值**（这份靶场数据本身已打码，`王**` / `138****6021`）：
    真值泄漏当然要红，打码值出现同样要红 —— 它意味着有人把快照往正文里拼了。

    ## 两份文件一起扫，不能只扫 `event-chain.json`（整合期 p10-g 补）

    `event-chain.json` 的 detail 是被 `make_case_bundle.py:672` 那张键白名单**裁过**的，
    而 `trace.json` 落的是**全量 detail**。只扫前者，这条扛着「never leaks customer
    identity」名字的红线会漏掉真正会泄的那一份：往 `_log_notified` 的 detail 里塞一个
    `customer_name`，`event-chain.json` 把它裁掉、这条照绿，`trace.json` 里却躺着原值
    —— 而整束是一起交出去的。白名单是**减法**，判据不该只盖住减完之后的那一半。
    """
    conn = sqlite3.connect(f"file:{gateway_fail / 'maos.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    snap = conn.execute("SELECT payload_json FROM order_snapshot ORDER BY version").fetchone()
    payload = json.loads((snap["payload_json"] if snap else "") or "{}")
    scanned = {name: json.dumps(_read(gateway_fail / name), ensure_ascii=False)
               for name in ("event-chain.json", "trace.json")}
    checked = 0
    for field in ("customer_name", "phone", "address", "receipt_no"):
        value = str(payload.get(field) or "").strip()
        if not value:
            continue
        checked += 1
        for name, blob in scanned.items():
            assert value not in blob, f"{field}（{value}）进了给评委看的 {name}"
    assert checked == 4, f"四项身份没比全 —— 靶场数据的字段名变了？{sorted(payload)}"


def test_compensation_skills_only_show_up_on_the_failure_path(happy, gateway_fail):
    """补偿那两个 skill 只在失败路径上出现 —— 顺利的案子本来就不该有补偿。"""
    ok = {s["skill"]: s["present"] for s in _read(happy / "skills.json")["compensation_skills"]}
    bad = {s["skill"]: s["present"]
           for s in _read(gateway_fail / "skills.json")["compensation_skills"]}
    assert ok == {"refund.compensate": False, "refund.compensation_close": False}
    assert bad == {"refund.compensate": True, "refund.compensation_close": True}


def test_every_invocation_id_in_skills_json_is_findable_in_the_event_log(happy):
    """`skills.json` 自称跑过的每一次调用，都能在 `event_log` 里回查到。

    这正是 `verify.py` 第 10 项对 case 束的扩覆盖判据，在这里先钉一遍：
    回查不到就意味着那份清单是编的。
    """
    conn = sqlite3.connect(f"file:{happy / 'maos.db'}?mode=ro", uri=True)
    try:
        recorded = {json.loads(r[0]).get("invocation_id") for r in conn.execute(
            "SELECT detail FROM event_log WHERE event_type='SkillInvoked'")}
    finally:
        conn.close()
    for row in _read(happy / "skills.json")["contract_skills"]:
        assert row["invocation_id"] in recorded, f"{row['skill']} 的调用记录不在库里"


def test_gateway_fail_closes_the_whole_compensation_loop(gateway_fail):
    """失败路径把工单闭环走完：开单 -> 派单 -> 关单，三条事件与操作者都在。

    关单结论是 `not_settled`：注入的码是「卖家余额不足」，钱在充值之前不可能到账。
    关成 settled 会造出「已补偿的案子被晋升成成功范本」那种自相矛盾的状态
    （见 `make_case_bundle.RESOLUTION_KIND` 的注释）。
    """
    chain = _read(gateway_fail / "event-chain.json")
    for name in ("CompensationExecuted", "CompensationAssigned", "CompensationResolved"):
        assert chain["by_type"].get(name) == 1, f"缺 {name}"
    outcome = _read(gateway_fail / "outcome.json")
    assert outcome["biz_status"] == "compensated"
    assert outcome["case_outcome"]["arrival"] == "unsettled"
    assert outcome["case_outcome"]["business_success"] is False
    table = _read(gateway_fail / "roundtable.json")["compensation"]
    assert table["resolution_kind"] == "not_settled" and table["assignee"]


def test_the_payment_gate_rejection_is_what_fails_the_plan(gateway_fail):
    """网关明确失败时人在付款闸上**拒签**，Plan 因此如实 FAILED。

    反面正是评委那句话指的病：签了字，Plan 收在 DONE，而钱根本没退出去。
    """
    hitl = _read(gateway_fail / "hitl-trace.json")
    rejects = [r for r in hitl["trace"] if "[human_reject]" in (r["transition"] or "")]
    assert len(rejects) == 1 and rejects[0]["operator"]
    assert _read(gateway_fail / "outcome.json")["plan_state"] == "FAILED"


def test_gateway_fail_carries_a_real_rework_from_the_gate(gateway_fail):
    """返工那一环（T124）：闸判返工 -> requeue，`hitl-trace` 里必须看得见。

    这一束从前一条 `REWORK` 都没有 —— 注入的是终态失败码，机器只发起一次，
    评委第一条要的「返工 / HITL Trace」只能指去别的场景束。现在第一段注的是
    `40005`（retriable + failed），闸判 blocker、`custom_case` 不接 replanner，
    于是落在 `AWAITING_REVIEW -> REWORK [gate_rework]` 上。

    `actor` 必须是 `gate`：这是**机器**自己决定再试一次，不是人按的按钮。
    记成人的动作，这份 Trace 就把两种主体混成了一种。
    """
    hitl = _read(gateway_fail / "hitl-trace.json")
    reworks = [r for r in hitl["trace"] if r.get("kind") == "rework"]
    assert len(reworks) >= 1, (
        "gateway_fail 束里一条 rework 都没有 —— 注入的码换回终态失败码了？"
        "只有 retriable=True + outcome=failed 那一格才长得出 REWORK")
    for row in reworks:
        assert row["actor"] == "gate", f"返工的发起者必须是闸，实际 {row['actor']}"
        assert "REWORK" in (row["transition"] or "")


def test_gateway_fail_leaves_no_dangling_business_ref(gateway_fail):
    """同渠道重试之后不许留悬空引用（T124）。

    这一束正好踩在那条缺陷上：返工重发用的是同一个幂等键，网关原样返回同一笔，
    `request_id` 不变而版本 1->2。摘旧引用的判据只比 `object_id` 的话，v1 会留下来
    指着一个已经是 v2 的对象 —— 而症状是静默的，只有这里和 `verify.py` 数得出来。
    """
    objs = _read(gateway_fail / "business-objects.json")
    assert objs["dangling"] == 0, (
        f"gateway_fail 束里有 {objs['dangling']} 条悬空引用 —— "
        f"payment_execute 摘旧引用的 DELETE 判据退回只比 object_id 了？")
    assert objs["resolved"] == len(objs["objects"]), "每一条引用都得指得到当前那一份"


def test_gateway_fail_really_sent_the_payment_twice(gateway_fail):
    """返工不是只在状态机上转了一圈：`payment.execute` 必须真的又发起了一次。

    只看 `REWORK` 那一跳的话，一个「返工了但没重发」的实现也能骗过守卫 ——
    而那正是这条证据要排除的。
    """
    rows = _read(gateway_fail / "skills.json")["contract_skills"]
    execute = next(r for r in rows if r["skill"] == "payment.execute")
    assert execute["invocations"] >= 2, (
        f"payment.execute 只被调了 {execute['invocations']} 次 —— "
        f"返工之后没有重新发起，这条 Trace 演的就不是返工")


def test_gateway_fail_is_the_path_that_completes_the_ten_object_types(happy, gateway_fail):
    """十类业务对象：顺利路径缺人工补偿，失败路径把它补上 —— 合起来才是 10/10。"""
    ok = _read(happy / "INDEX.json")
    bad = _read(gateway_fail / "INDEX.json")
    assert ok["business_ref_missing"] == ["compensation_record"]
    assert "compensation_record" not in bad["business_ref_missing"]
    assert set(ok["business_ref_missing"]) & set(bad["business_ref_missing"]) == set(), (
        "两条路径缺的是同一类 —— 那就永远凑不齐十类")


# ---------------------------------------------------------------------------
# 收集函数：手搭小库，不跑 DAG
# ---------------------------------------------------------------------------
def _log_store(rows: list[dict]) -> sqlite3.Connection:
    """一个只有 `event_log` 的最小库，按给定顺序灌行。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE event_log (seq INTEGER PRIMARY KEY, created_at TEXT,"
                 " event_type TEXT, plan_id TEXT, task_id TEXT, from_state TEXT,"
                 " to_state TEXT, reason TEXT, detail TEXT)")
    for i, row in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO event_log (seq, created_at, event_type, plan_id, task_id,"
            " from_state, to_state, reason, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, row.get("at", "2026-09-11T00:00:00+00:00"), row["event_type"],
             row.get("plan_id", "p"), row.get("task_id", ""), row.get("from", ""),
             row.get("to", ""), row.get("reason", ""),
             json.dumps(row.get("detail", {}), ensure_ascii=False)))
    return conn


def test_machine_decisions_are_never_recorded_as_human_ones():
    """返工与转人工出口如实记 `operator=null` / `actor=gate`。

    把机器判的动作记在人头上，「这一单人到底介入过几次」就永远算不准 ——
    而那个数正是评委要看的 HITL 密度。
    """
    conn = _log_store([
        {"event_type": "StateTransition", "to": "REWORK", "reason": "gate_rework",
         "task_id": "t1"},
        {"event_type": "StateTransition", "to": "BLOCKED", "reason": "gate_needs_human",
         "task_id": "t1"},
        {"event_type": "StateTransition", "from": "BLOCKED", "to": "DONE",
         "reason": "human_approve", "task_id": "t1", "detail": {"operator": "阿沈"}},
    ])
    out = MCB.collect_hitl(conn)
    kinds = {r["kind"]: r for r in out["trace"]}
    assert kinds["rework"]["operator"] is None and kinds["rework"]["actor"] == "gate"
    assert kinds["blocked"]["actor"] == "gate"
    assert kinds["task_approval"]["operator"] == "阿沈"
    assert out["human_actions"] == 1
    assert out["unattributed_human_actions"] == []


def test_a_human_action_without_an_operator_is_flagged():
    """人做的动作没有操作者时，`unattributed_human_actions` 要点它的名。

    这条判据会红才有用：`verify.py` 第 10 项按它判负。
    """
    conn = _log_store([{"event_type": "PlanApproved", "detail": {}}])
    out = MCB.collect_hitl(conn)
    assert out["unattributed_human_actions"] == [1]


def test_required_events_are_per_path_not_one_list_for_all():
    """漂移路径**不要求** `RefundBizStatusChanged` —— 它按设计一个业务状态都不改。

    四条路径共用一份清单的话，漂移那条恒缺一项，而一个只会红不会绿的守卫等于没写。
    """
    assert "RefundBizStatusChanged" in MCB.PATHS["happy"]["required_events"]
    assert "RefundBizStatusChanged" not in MCB.PATHS["drift"]["required_events"]
    assert "SnapshotDrift" in MCB.PATHS["drift"]["required_events"]

    rows = [{"event_type": name} for name in
            MCB.BASE_REQUIRED_EVENTS + MCB.PATHS["drift"]["required_events"]]
    assert MCB.collect_event_chain(_log_store(rows), path="drift")["missing_required"] == []
    missing = MCB.collect_event_chain(_log_store(rows), path="happy")["missing_required"]
    assert missing == ["RefundBizStatusChanged"]


def test_roundtable_and_planner_events_are_optional(happy):
    """圆桌（T113）与 Planner 建议（T119）的事件有就带上、没有不算错。

    基线上圆桌不落库，写死成「必须有」会让本束在整合之前恒红。
    """
    chain = _read(happy / "event-chain.json")
    assert chain["missing_required"] == []
    assert set(chain["optional_present"]) <= set(MCB.OPTIONAL_EVENTS)


def test_roundtable_probes_for_the_store_parameter_instead_of_assuming_it(payload):
    """圆桌按签名探 `store=`：基线上探不到就不传，五岗照样发言。

    直接传的话在 T113 并入之前是 `TypeError`，而那一刻的症状是「圆桌起不动」——
    拿一个可选参数换掉整段圆桌，比不传更糟。
    """
    import inspect as _inspect

    store = SqliteStore()
    store.init_schema()
    team, takes_store = custom_case.build_roundtable(
        None, custom_case._SilentVoices(), store)

    assert takes_store == ("store" in _inspect.signature(type(team)).parameters)
    assert team.roster(), "探参失败不该让名册变空"


# ---------------------------------------------------------------------------
# 4. 空补丁集根因（§3.4）：三处判据同源
# ---------------------------------------------------------------------------
class _CannedModel:
    """回一句写死的 JSON 的假模型。`complete()` 的出参只用到 `.text`。"""

    model = "canned"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, **_kw):                           # noqa: ANN003, ANN201
        return type("Resp", (), {"text": self.text, "usage": None})()


def _patch(text: str):                                   # noqa: ANN202
    return SkillInvoker(CodingAgent.identity, None).invoke(
        "code.repo-patch", {"title": "定位 token 校验缺失的入口点", "inputs": {},
                            "acceptance": ["清单为空时明确输出 no missing ..."]},
        extras={"model": _CannedModel(text)})


def test_an_empty_patch_set_with_a_summary_is_a_legal_conclusion():
    """「查过了，没有要改的」是一个有效结论，不是一次执行失败。

    出处 docs/BACKLOG.md:2293：模型对定位类任务返回空 `files` + 一句 summary，
    **是照验收标准做的**，却被判成失败，配了 key 的机器上因此恒红。
    口径同 `agents/refund/policy_agent.py`：裁定为 reject 不产出空产物、也不 failed。
    """
    res = _patch(json.dumps({"files": [], "summary": "no missing token validation found"}))
    assert res.status == "ok", res.error
    assert res.output["files"] == [], "空补丁集要如实落成空 list"
    assert res.output["summary"]


def test_an_empty_patch_set_without_a_summary_is_still_an_error():
    """空且没有 summary 仍判错 —— 分不清「没有要改的」与「模型没产出」的话，
    后者会以「成功地什么都没做」收场，而那是静默失败。"""
    res = _patch(json.dumps({"files": []}))
    assert res.status == "failed"
    assert "summary" in (res.error or "")


def test_sandbox_applies_an_empty_patch_set_as_a_no_op(tmp_path):
    """空补丁集带 summary 时落盘成功 —— 零个文件的落盘天然成功。

    签名与返回形状一字不动（`docs/parallel/contracts.md` C-7 冻结的就是这两样）。
    """
    ok = sandbox_git_apply({"files": [], "summary": "查过了，没有要改的"}, str(tmp_path))
    assert ok == {"ok": True, "error": None}

    bad = sandbox_git_apply({"files": []}, str(tmp_path))
    assert bad["ok"] is False and bad["error"]["stage"] == "validate"

    shape = sandbox_git_apply({"files": "not-a-list"}, str(tmp_path))
    assert shape["ok"] is False and shape["error"]["stage"] == "validate"


def test_the_schema_gate_does_not_blocker_an_empty_files_list():
    """Gate 的 schema 闸只挡「缺 files 字段」，不挡「files 是空的」。

    两者是两件事：前者是形状不合契约，后者是一个有效结论。挡了后者，
    skill 放行而 Gate 判 blocker，就出现「同一份产物两个结论」的新分叉。
    """
    empty = [{"kind": "patch_set", "content": {"files": [], "summary": "没有要改的"}}]
    missing = [{"kind": "patch_set", "content": {"summary": "没有要改的"}}]
    assert ReviewerGate._gate_schema({"task_id": "t"}, empty) == []
    assert ReviewerGate._gate_schema({"task_id": "t"}, missing), "缺 files 字段仍要判 blocker"


def test_the_compensation_dry_run_accepts_an_empty_patch_set(tmp_path):
    """补偿干跑对空补丁集不产 blocker —— 反不回去的东西是补丁，不是「没有补丁」。"""
    art = {"content": {"files": [], "summary": "本轮没有要改的"}}
    task = {"task_id": "t", "inputs": {"workdir": str(tmp_path)}}
    assert ReviewerGate._dry_run_reverse(task, art) == []


# ---------------------------------------------------------------------------
# 5. 索引与核验器认得这一束
# ---------------------------------------------------------------------------
def test_aux_index_recurses_one_level_into_case_bundles(tmp_path):
    """`scan_aux_bundles` 递归一层：束里各路径的文件都要登记，不只顶层那一份索引。

    只登记顶层的话，一个装着四条路径、二十多个文件的目录会被记成「文件 1」——
    漏登记了整整四个子目录，而它自称是索引。
    """
    bundle = tmp_path / "case-real-01"
    (bundle / "happy").mkdir(parents=True)
    (bundle / "INDEX.json").write_text(ME.HEADER_PREFIX + "x from y\n{}\n", encoding="utf-8")
    (bundle / "happy" / "trace.json").write_text(
        ME.HEADER_PREFIX + "x from y\n{}\n", encoding="utf-8")
    (bundle / "happy" / "maos.db").write_bytes(b"\x00")

    aux = {entry["name"]: entry for entry in ME.scan_aux_bundles(str(tmp_path))}
    entry = aux["case-real-01"]
    assert entry["file_count"] == 3, "子目录里的文件没算进来"
    subs = {s["name"]: s for s in entry["sub_bundles"]}
    assert {f["name"] for f in subs["happy"]["files"]} == {"trace.json", "maos.db"}
    assert subs["happy"]["files"][1]["sourced"] is True     # trace.json 首行有出处


def test_verify_takes_the_scripted_bundles_and_names_the_live_ones(tmp_path):
    """核验器收 Scripted 束、跳过 `-live` 并把它点名。

    真模型每跑一次说的话都不一样，拿它当重放比对的基准，第 4 项 trace-tree
    会在没有任何 bug 的情况下红。跳过要**点名**，静默跳过等于谎报。
    """
    for name in ("happy", "happy-live"):
        directory = tmp_path / "case-real-01" / name
        directory.mkdir(parents=True)
        for f in ("trace.json", "result.json", "maos.db"):
            (directory / f).write_bytes(b"{}")
    (tmp_path / "case-real-01" / "half").mkdir()          # 缺三样，不够格

    taken, skipped = VERIFY.case_bundle_dirs(str(tmp_path))
    assert [Path(p).name for p in taken] == ["happy"]
    assert skipped == ["case-real-01/happy-live"]


def test_case_bundles_get_their_provenance_hint_pointed_at_the_right_command():
    """单案例束过期时的提示指向 `make_case_bundle.py`，不是 `make_evidence.py`。

    指向一条解决不了它的命令，照做的人会拿到一模一样的报错再撞一次 ——
    比没有提示更坏（口径同 `_DB_HINTS` 对 scenario-R5 的处理）。
    """
    assert "make_case_bundle.py" in VERIFY.provenance_hint("case-real-01")
    assert "make_case_bundle.py" in VERIFY.provenance_hint("case-real-01/happy")
    assert "make_evidence.py" in VERIFY.provenance_hint("scenario-1")


def test_payment_role_is_the_one_rejected_on_the_gateway_fail_path():
    """`reject_roles` 用的是**角色**不是任务号：任务号随 case 变，角色不会。"""
    assert MCB.PATHS["gateway_fail"]["reject_roles"] == (ROLE_PAYMENT,)
    assert all("reject_roles" not in cfg or not cfg["reject_roles"]
               for name, cfg in MCB.PATHS.items() if name != "gateway_fail")
