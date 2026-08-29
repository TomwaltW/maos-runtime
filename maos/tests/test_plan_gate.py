"""第六道闸的 **plan 级判据**（BACKLOG ``## task-W3`` 第 3 条）的口径锁死在这里。

任务级那半在 ``test_gate.py``，本文件只守新加的那半，两半的分界就是 finding 上的
``scope`` 字段：**缺省不写即为任务级**，plan 级显式写 ``"plan"``。

## 这条判据要回答的问题

F-1 的任务级判据按 ``biz_type + amount_claimed`` **逐任务**触发。于是「计划里漏排了
财务复核」这件事它判不出来 —— 漏排意味着没有任何任务带着申报金额，闸连可判的对象都
没有。实测里 ``gate.py`` 那句「漏排财务复核会在这里被拦下」一次都没走过：真正的症状
出在下一步 ``payment.execute`` 查不到 ``finance_entry``，整个 Plan 收在 FAILED。

plan 级判据补的就是这个缺口，判的是 **计划的静态结构**：

    这个 Plan 报了超阈金额（inputs 树里任意深度），
    却没有任何一个任务把它带到顶层 ``inputs["amount_claimed"]`` 上。

## 本文件里最要紧的三条

1. **顺序无关**（``test_plan_level_finding_is_identical_whichever_task_is_reviewed``）。
   闸是逐任务跑的，plan 级判据会在**每一个**进 AWAITING_REVIEW 的任务上各命中一次。
   判定必须可复现、可解释、可审计（``gate.py`` 类 docstring 第 5 行）—— 所以 N 次命中
   必须逐字节相同，不能「在 A 上说漏了、在 B 上说别的」。
2. **不判 finance_entry 在不在**（``test_plan_level_does_not_look_at_artifacts``）。
   BACKLOG 的原话是「有 refund 任务却没有任何 finance_entry」，照字面写会**判错**：
   凭据是跑出来的，正常计划在受理那一步过闸时财务那一步还没轮到，凭据当然不在 ——
   那条判据会对一个完全健康的计划报 blocker。所以判「排没排这一步」，不判「跑出来没有」。
3. **权威边界仍是兜底**（``test_authority_boundary_still_refuses_payment_...``）。
   判据前移之后，R5 对照实验的 without_kb 段不再走到付款那一步，铁律 8 那条
   「没有 finance_entry 就不许发起付款」于是失去了唯一的运行时演示。它在这里补一条
   断言钉住 —— 闸前移是多一层防线，不是把底下那层拆了。
"""

from __future__ import annotations

import pytest

from maos.agents.base import AgentIdentity
from maos.contracts.states import PlanState, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import SqliteStore
from maos.domain.refund import guard, objects
from maos.flows import scenario_6 as s6
from maos.model.client import Tier
from maos.runtime.gate import (
    DEFAULT_FINANCE_THRESHOLD,
    FINANCE_SCAN_MAX_DEPTH,
    FINANCE_THRESHOLD_ENV,
    SCOPE_PLAN,
    ReviewerGate,
)
from maos.skills.builtin.refund import _common as C
from maos.skills.invoker import SkillInvoker
from maos.tools.gateway import MockGateway

_TEST_GATEWAY = "plan-gate-test-gw"

#: 只授权付款那一个 skill —— 兜底那条测试要验的是付款方自己的前置检查。
_PAYMENT_IDENTITY = AgentIdentity(
    agent_id="test-plan-gate", role="test_refund",
    duty="测试夹具：只用来逼出付款方的前置检查",
    allowed_skills=frozenset({"payment.execute"}),
    allowed_tools=frozenset({"gateway.refund", "gateway.query"}),
    write_scope=frozenset({"artifact"}), max_risk="M", model_tier=Tier.LIGHT)

OVER = DEFAULT_FINANCE_THRESHOLD + 1800.0      # 6800，与 R5 靶场同一个量级
UNDER = DEFAULT_FINANCE_THRESHOLD - 1000.0

#: 一份让前五道闸都闭嘴的中性产物：self_check 全 pass 挡验收闸，summary 挡证据闸。
NEUTRAL_ARTIFACT = {"kind": "refund_case_draft",
                    "content": {"summary": "受理完成",
                                "self_check": {"build": "pass", "lint": "pass"}}}


class _RecordingBus(EventBus):
    """只记不发 —— 与 test_gate.py 同一个理由：这里测的是判定，不是状态机。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic, env) -> None:
        self.published.append((topic, env))

    def subscribe(self, topic, group, handler) -> None:
        pass

    def drain(self, max_rounds: int = 1000) -> int:
        return 0


@pytest.fixture
def default_threshold(monkeypatch):
    """把阈值 env 摘干净 —— 本机外挂了 MAOS_FINANCE_THRESHOLD 会让这些断言假绿。"""
    monkeypatch.delenv(FINANCE_THRESHOLD_ENV, raising=False)
    return DEFAULT_FINANCE_THRESHOLD


# ----------------------------------------------------------------- 夹具
def _seed_of(amount) -> dict:
    """R5 / 场景 6-7 三处同形的案件种子：申报金额嵌在这里面，不在顶层。"""
    return {"tenant_id": "tnt-mfg-a", "case_id": "case-1", "channel_id": "ch-online",
            "order_id": "ord-1", "order_version": 1, "sku": "SKU-BRG-6205",
            "reason_code": "quality_defect", "amount_claimed": amount}


def _plan_tasks(*, with_finance: bool, seed_amount=OVER) -> list[dict]:
    """R5 那份 DAG 的最小复刻。``with_finance=False`` 就是「漏排财务核算」的那一版。

    两版只差 finance 这一步，且申报金额**只挂在这一步的顶层** —— 这不是为了造测试，
    是 ``maos/kb/experiment.py:148`` 与场景 6/7 三处照同一份口径写的真实形状。
    """
    tasks = [
        {"task_id": "t-intake", "role": "refund_intake", "title": "受理",
         "inputs": {"biz_type": "refund", "case_id": "case-1",
                    "case_seed": _seed_of(seed_amount)}},
        {"task_id": "t-policy", "role": "refund_policy", "title": "裁定",
         "inputs": {"biz_type": "refund", "case_id": "case-1"}},
        {"task_id": "t-payment", "role": "refund_payment", "title": "付款",
         "inputs": {"biz_type": "refund", "case_id": "case-1", "gateway": "gw"}},
    ]
    if with_finance:
        tasks.insert(1, {
            "task_id": "t-finance", "role": "refund_finance", "title": "核算",
            "inputs": {"biz_type": "refund", "case_id": "case-1",
                       "amount_claimed": seed_amount}})
    return tasks


def _run(tasks: list[dict], *, review: str = "t-intake",
         artifacts: list[dict] | None = None) -> dict:
    """建 plan、把 ``review`` 那个任务推到 AWAITING_REVIEW、跑闸，返回 verdict payload。"""
    store = SqliteStore()
    store.init_schema()
    store.insert_plan({"plan_id": "p1", "trace_id": "tr", "goal": "退款",
                       "state": PlanState.RUNNING})
    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))

    for spec in tasks:
        store.insert_task({
            "plan_id": "p1", "trace_id": "tr", "attempt": 1,
            "state": (TaskState.AWAITING_REVIEW if spec["task_id"] == review
                      else TaskState.PENDING),
            **spec})
    for i, art in enumerate(artifacts or [NEUTRAL_ARTIFACT]):
        store.insert_artifact({"artifact_id": f"a{i}", "task_id": review, "plan_id": "p1",
                               "kind": art["kind"], "version": art.get("version", 1),
                               "content": art["content"]})

    assert gate.review_pending("p1") == 1, "AWAITING_REVIEW 的任务没有被 Gate 取到"
    return bus.published[-1][1].payload


def _plan_findings(payload: dict) -> list[dict]:
    return [f for f in payload["findings"] if f.get("scope") == SCOPE_PLAN]


def _finance_findings(payload: dict) -> list[dict]:
    return [f for f in payload["findings"] if f["gate"] == "finance"]


# ======================================================================
# 题眼：漏排财务复核，判得出来了
# ======================================================================
def test_missing_finance_step_is_a_plan_level_blocker(default_threshold):
    """R5 without_kb 那一版的形状：金额只在 case_seed 里，没有任何任务把它带进闸。

    这一条就是 BACKLOG ``## task-W3`` 第 3 条的正面回答。它在 Phase 7 之前判不出来，
    症状是闸 ``not_triggered``、Plan 一路跑到付款才被权威边界拦下。
    """
    payload = _run(_plan_tasks(with_finance=False))
    fs = _plan_findings(payload)

    assert len(fs) == 1, f"漏排财务复核没有被判出来（findings={payload['findings']}）"
    assert fs[0]["gate"] == "finance"
    assert fs[0]["severity"] == "blocker", (
        "plan 级缺陷必须是 blocker：跨轨冻结契约按 severity != info 才把它路由到人，"
        "判成 info 就等于让它继续返工，而返工补不出一整步")
    assert payload["gate_results"]["finance"] == "fail"
    assert payload["verdict"] == "rework"
    # 人要能只读这条 finding 就知道该干什么：缺的是哪一步、为什么返工没用。
    assert "漏排了财务复核" in fs[0]["message"]
    assert "返工" in fs[0]["message"]


def test_plan_with_the_finance_step_gets_no_plan_level_finding(default_threshold):
    """排了财务复核 -> plan 级判据闭嘴，剩下的交给任务级那半。"""
    payload = _run(_plan_tasks(with_finance=True))
    assert _plan_findings(payload) == [], (
        "计划里明明排了财务复核，plan 级判据却报了 blocker —— "
        "这会让每一个健康的退款计划都转人工")


def test_task_level_findings_carry_no_scope_key(default_threshold):
    """缺省不写即为任务级（跨轨冻结契约 D-1 第 4 条）—— 老 finding 不许被顺手加字段。

    D-1 按 ``scope == "plan"`` 分流。任务级 finding 哪天被顺手补上 ``scope="task"``
    本身没坏处，但那是契约变更，得两轨一起改；这条断言让它不能悄悄发生。
    """
    payload = _run(_plan_tasks(with_finance=True), review="t-finance")
    fs = _finance_findings(payload)
    assert fs, "任务级判据没开口 —— 这一版的 finance 任务本来就交不出凭据"
    assert all("scope" not in f for f in fs), \
        f"任务级 finding 上出现了 scope 字段：{fs}"


def test_plan_and_task_level_are_mutually_exclusive(default_threshold):
    """两条判据不会同时命中：一条管「没进闸」，一条管「进了闸交不出凭据」。

    互斥性是 ``_gate_finance`` 写成相加而不是 if/else 的前提。真同时命中了，
    这里会看见两条 finance finding —— 那说明判据松动了，要停下来看。
    """
    missing = _finance_findings(_run(_plan_tasks(with_finance=False)))
    carried = _finance_findings(_run(_plan_tasks(with_finance=True), review="t-finance"))
    assert len(missing) == 1 and missing[0].get("scope") == SCOPE_PLAN
    assert len(carried) == 1 and "scope" not in carried[0]


# ======================================================================
# 顺序无关性 —— 这条判据能挂在逐任务的闸上的立身之本
# ======================================================================
@pytest.mark.parametrize("review", ["t-intake", "t-policy", "t-payment"])
def test_plan_level_finding_fires_on_every_reviewed_task(review, default_threshold):
    """plan 级判据对 plan 内**任何**一个进评审的任务都命中，不挑任务。

    挑任务就是顺序相关：先评审谁决定判不判得出来，而评审顺序是执行期的事。
    """
    fs = _plan_findings(_run(_plan_tasks(with_finance=False), review=review))
    assert len(fs) == 1, f"评审 {review} 时 plan 级判据没命中"


def test_plan_level_finding_is_identical_whichever_task_is_reviewed(default_threshold):
    """N 次命中必须**逐字节相同** —— 可复现、可解释、可审计那条线就在这儿。

    文案里如果掺进「当前正在评审的任务」，同一个计划缺陷会被描述成 N 种说法，
    审计时无法判断它们说的是不是同一件事。所以报错点取 task_id 最小的那个，
    与谁在被评审无关。
    """
    seen = [_plan_findings(_run(_plan_tasks(with_finance=False), review=r))[0]
            for r in ("t-intake", "t-policy", "t-payment")]
    assert seen[0] == seen[1] == seen[2], (
        "同一个计划缺陷在不同任务上被描述成了不同的 finding：\n"
        + "\n".join(f"{f['message']}" for f in seen))


def test_plan_level_does_not_look_at_artifacts(default_threshold):
    """判据不读产物 —— 读了就等于判「到此刻为止跑出来没有」，那是顺序相关的。

    这里给受理任务挂一份带非空 ``finance_entry`` 的产物：任务级判据会被它说服，
    plan 级判据不该被说服。计划里少排的那一步，不会因为别人顺手产了份凭据就补上。
    """
    with_entry = {"kind": "refund_settlement",
                  "content": {"summary": "核算", "finance_entry": {"amount_approved": 6800},
                              "self_check": {"build": "pass", "lint": "pass"}}}
    fs = _plan_findings(_run(_plan_tasks(with_finance=False), artifacts=[with_entry]))
    assert len(fs) == 1, "plan 级判据被一份产物说服了 —— 它不该读产物"


# ======================================================================
# 触发面：一个字段名，任意深度，同一把尺
# ======================================================================
def test_plan_level_is_silent_for_a_non_refund_plan(default_threshold):
    """场景 1-5 的形状：没有 biz_type=refund 的任务 -> 这条判据恒不触发。

    金额字段照样在（而且是超阈的），触发面是 ``biz_type``，不是金额本身。
    """
    tasks = [{"task_id": "t-code", "role": "coding", "title": "改代码",
              "inputs": {"workdir": "/tmp/x", "case_seed": _seed_of(OVER)}}]
    payload = _run(tasks, review="t-code")
    assert _finance_findings(payload) == [], "非退款计划被财务闸拦下了"
    assert payload["gate_results"]["finance"] == "pass"


def test_plan_level_is_silent_below_threshold(default_threshold):
    """嵌套金额在阈值之下 -> 不触发。阈值这把尺两条判据共用，不许各判各的。"""
    assert _plan_findings(_run(_plan_tasks(with_finance=False, seed_amount=UNDER))) == []


def test_plan_level_is_silent_at_exactly_threshold(default_threshold):
    """恰好等于阈值不触发（``>`` 不是 ``>=``）—— 与 F-1 任务级判据一字不差。"""
    assert _plan_findings(
        _run(_plan_tasks(with_finance=False, seed_amount=DEFAULT_FINANCE_THRESHOLD))) == []


def test_plan_level_reads_the_threshold_from_env(monkeypatch):
    """阈值现读 env，两条判据同源。只改任务级那半的话，症状在这里露出来。"""
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "99999")
    assert _plan_findings(_run(_plan_tasks(with_finance=False))) == [], \
        "阈值抬到 99999 之后 6800 还被判超阈"
    monkeypatch.setenv(FINANCE_THRESHOLD_ENV, "100")
    assert _plan_findings(_run(_plan_tasks(with_finance=False))), \
        "阈值压到 100 之后 6800 反而不超阈了"


def test_unparseable_nested_amount_triggers_instead_of_being_swallowed(default_threshold):
    """嵌套金额解析不出数 = 触发，不是当 0 放过 —— 与任务级那半同一条收严规矩。

    吞掉的话，一笔字段脏掉的高额退款连「有没有排财务复核」都没人问了。
    """
    fs = _plan_findings(_run(_plan_tasks(with_finance=False, seed_amount="六千八")))
    assert len(fs) == 1, "案件种子里的金额脏成了字符串，判据却当它是 0 元"


def test_a_dict_valued_amount_at_top_level_still_counts_as_in_view(default_threshold):
    """顶层 ``amount_claimed`` 是个 dict：算「进了闸的视野」，不算「还藏着个金额」。

    命中的键不再往下潜。潜下去的话，``{"amount_claimed": {"amount_claimed": 9000}}``
    这种形状会同时被两条判据认领，而 F-1 对它的判定是明确的：解析不出 = 触发任务级。
    """
    tasks = _plan_tasks(with_finance=True)
    tasks[1]["inputs"]["amount_claimed"] = {"n": OVER}
    assert _plan_findings(_run(tasks)) == [], \
        "顶层金额虽然形状不对，但它确实进了闸的视野，不该再报「漏排」"


def test_scan_does_not_dive_past_the_depth_limit(default_threshold):
    """扫描有深度上限：埋得比上限还深的金额扫不到，也**不许抛**。

    这不是漏判的借口，是防线：inputs 是外部喂进来的 JSON，Gate 不能假设上游收敛过
    形状。真实形状（``case_seed.amount_claimed``）只有一层，离上限还很远。
    """
    deep = {"biz_type": "refund"}
    node = deep
    for _ in range(FINANCE_SCAN_MAX_DEPTH + 3):
        node["nest"] = {}
        node = node["nest"]
    node["amount_claimed"] = OVER
    payload = _run([{"task_id": "t-deep", "role": "refund_intake", "title": "深",
                     "inputs": deep}], review="t-deep")
    assert _plan_findings(payload) == []
    assert "finance" in payload["gate_results"], "深层结构把闸弄没了"


@pytest.mark.parametrize("inputs,label", [
    (None, "inputs 为 null"),
    ("refund", "inputs 是字符串"),
    ({"biz_type": "refund", "case_seed": ["不是 dict"]}, "种子是列表"),
    ({"biz_type": "refund", "case_seed": {"amount_claimed": None}}, "金额是 null"),
    ({"biz_type": None, "case_seed": _seed_of(OVER)}, "biz_type 为 null"),
])
def test_plan_level_does_not_raise_on_odd_inputs(inputs, label, default_threshold):
    """形状怪的 inputs 一律不许抛 —— ``review_pending()`` 是裸调，异常逃出即整个 plan 崩。

    plan 级判据比任务级更容易踩这条：它读的是**别的任务**的 inputs，而那些任务的
    形状本轮根本没人检查过。
    """
    try:
        payload = _run([{"task_id": "t-odd", "role": "refund_intake", "title": "怪",
                         "inputs": inputs}], review="t-odd")
    except Exception as exc:  # noqa: BLE001 —— 这里要的就是「任何异常都不许有」
        pytest.fail(f"{label} 时 Gate 抛了 {exc!r}")
    assert "finance" in payload["gate_results"], "第六道闸没有出现在 gate_results 里"


def test_plan_level_stays_quiet_when_the_store_cannot_be_read(default_threshold):
    """读不到任务集就闭嘴，不抛也不猜。

    抛会掀掉整个 plan；猜「大概是漏排了」会把一次存储抖动报成计划缺陷，
    而计划缺陷是要转人工的 —— 那等于让存储故障去打扰人。
    """
    store = SqliteStore()
    store.init_schema()
    store.insert_plan({"plan_id": "p1", "trace_id": "tr", "goal": "退款",
                       "state": PlanState.RUNNING})

    original = store.list_tasks
    calls = {"n": 0}

    def flaky(plan_id):
        calls["n"] += 1
        # 第一次是 review_pending 自己取任务，得让它拿到；之后才是判据在读。
        if calls["n"] == 1:
            return original(plan_id)
        raise RuntimeError("database is locked")

    tasks = _plan_tasks(with_finance=False)
    for spec in tasks:
        store.insert_task({"plan_id": "p1", "trace_id": "tr", "attempt": 1,
                           "state": (TaskState.AWAITING_REVIEW
                                     if spec["task_id"] == "t-intake"
                                     else TaskState.PENDING), **spec})
    store.insert_artifact({"artifact_id": "a0", "task_id": "t-intake", "plan_id": "p1",
                           "kind": NEUTRAL_ARTIFACT["kind"], "version": 1,
                           "content": NEUTRAL_ARTIFACT["content"]})
    store.list_tasks = flaky  # type: ignore[method-assign]

    bus = _RecordingBus()
    gate = ReviewerGate(store, bus, ControlPlane(store, bus))
    gate.review_pending("p1")
    payload = bus.published[-1][1].payload
    assert _plan_findings(payload) == [], "存储读不动的时候，判据猜了一个计划缺陷出来"


# ======================================================================
# 兜底还在：闸前移不等于把权威边界拆了
# ======================================================================
def test_authority_boundary_still_refuses_payment_without_a_finance_entry():
    """铁律 8 的那条硬边界：没有 ``finance_entry`` 就不许发起付款。

    plan 级判据落地之前，这条边界的唯一运行时演示是 R5 对照实验的 without_kb 段
    （``python3 -m maos.kb.experiment``：拦点 = ``payment.execute`` 抛 LookupError）。
    判据前移之后那一段不再跑到付款，演示随之消失 —— 于是在这里补一条断言钉住它。

    两层防线各管各的：闸判「计划里排没排这一步」，付款方判「这一笔到底核算过没有」。
    上面那层永远可能被绕过（改阈值、改 biz_type），下面这层不能。
    """
    store = SqliteStore()
    store.init_schema()
    s6.seed_domain(store)
    case_id = "case-noentry"

    guard.create_case(store, tenant_id=s6.TENANT_ID, case_id=case_id,
                      channel_id=s6.CHANNEL_ID, order_id=s6.ORDER_ID,
                      order_version=s6.ORDER_VERSION, sku=s6.SKU,
                      reason_code="quality_defect", amount_claimed=s6.AMOUNT_CLAIMED,
                      plan_id="p1", actor_skill="test", invocation_id="inv-1")
    # 审批照给 —— 要单独逼出「没核算」这一条，不能让「没审批」那条先挡住。
    C.record_approval(store, tenant_id=s6.TENANT_ID, case_id=case_id,
                      approver="测试主管", decision="approved", reason="单测放行")
    assert objects.query(
        store, "SELECT * FROM finance_entry WHERE tenant_id=? AND case_id=?",
        (s6.TENANT_ID, case_id)) == [], "夹具本身就有分录，这条测试证明不了任何东西"

    C.reset_gateways()
    C.register_gateway(_TEST_GATEWAY, MockGateway(settle_after=s6.SETTLE_AFTER))
    try:
        res = SkillInvoker(_PAYMENT_IDENTITY, store).invoke(
            "payment.execute",
            {"tenant_id": s6.TENANT_ID, "case_id": case_id, "gateway": _TEST_GATEWAY},
            extras={"plan_id": "p1", "task_id": "t-payment", "trace_id": "tr",
                    "attempt": 1})
    finally:
        C.reset_gateways()

    assert res.status == "failed", "金额没经核算，付款竟然发出去了"
    assert "finance_entry" in (res.error or ""), (
        f"拦住了，但拦的不是「没核算」这一条：{res.error}")
    assert guard.get_case(store, s6.TENANT_ID, case_id)["biz_status"] == "submitted", (
        "付款被拒之后业务状态被推进了 —— 拒绝路径上不许留下任何外部状态推断")
