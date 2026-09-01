"""场景 11 端到端 + RTV 域五个薄壳 Agent 的机器验收（T64）。

本轨要买的四件东西，每一件都有一条用例钉着：

    1. **五个角色与冻结契约 C-R7 逐字相同** —— role / duty / 白名单 / tier 一个字
       都不许漂。契约的表在这里有一份机器副本（`CONTRACT_C_R7`），循环比对，
       不手抄进断言。
    2. **薄壳自证**：五个 Agent 模块的源码里**没有业务判定** —— 没有金额比较，
       没有拿权威终态当值来比较/赋值。这是「同一个编排内核，换个领域只换
       Skill / ToolPort / 业务对象」那句话的机器守卫：判定一旦漏进 Agent，
       下一个业务域就得把这五个 Agent 也重写一遍，而那时已经晚了。
    3. **两个权威终态都只能观察得到**（契约 C-R3，铁律 8）：`credited` 要供应商
       开出的贷项通知单，`settled` 要 AP 核销的调整凭单；换个写入方、
       或者拿 `acknowledged` 冒充 `issued`，都在守卫里被拒并落一条事件。
    4. **「Agent 都说完成了」不等于业务成功**：失败路径上五个 Agent 全回 ok，
       而供应商不认这笔退货，案子确实没成。

用例对**库里的行**下断言（贷项通知单几张、终态观察几条、补偿记录几行、
Task 迁移落在哪张表里），不只看退出码 —— 退出码是场景自己的断言给的，
拿它当判据等于让被测者给自己判分。

场景的 `drive_happy()` / `drive_failure()` 只跑不断言，正是为此拆出来的：
测试不再拼第二份流程，两边不会漂。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from maos.agents.base import PermissionDenied
from maos.agents.rtv import (
    ALL_RTV_KINDS,
    RTV_ROLES,
    RtvDispositionAgent,
    RtvIntakeAgent,
    RtvLogisticsAgent,
    RtvReconcileAgent,
    RtvSettlementAgent,
)
from maos.artifacts import KIND_PATCH_SET, KIND_TEST_REPORT
from maos.contracts.states import TASK_TRANSITIONS, PlanState, TaskState
from maos.flows import scenario_11 as s11
from maos.model.client import Tier
from maos.skills.registry import get as skill_get

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_AGENT_DIR = Path(s11.__file__).resolve().parent.parent / "agents" / "rtv"


@pytest.fixture(scope="module")
def happy():
    return s11.drive_happy()


@pytest.fixture(scope="module")
def failure():
    return s11.drive_failure()


# =========================================================================
# 冻结契约的机器副本 —— 改动这里等于改契约，而契约只在人类终端解锁
# =========================================================================
#: 契约 C-R7：role / duty / allowed_skills / allowed_tools / model_tier。
CONTRACT_C_R7 = {
    "rtv_intake": {
        "cls": RtvIntakeAgent,
        "duty": "受理退货诉求，定位源 PO 与收货单，建案并挂上业务对象引用",
        "allowed_skills": frozenset({"rtv.intake"}),
        "allowed_tools": frozenset(),
        "model_tier": Tier.LIGHT,
    },
    "rtv_disposition": {
        "cls": RtvDispositionAgent,
        "duty": "按退货理由与合同条款裁定 credit/exchange/replacement，并保留规则出处",
        "allowed_skills": frozenset({"rtv.dispose"}),
        "allowed_tools": frozenset(),
        "model_tier": Tier.LIGHT,
    },
    "rtv_logistics": {
        "cls": RtvLogisticsAgent,
        "duty": "登记退货发运并取得承运商回执（shipped 只能由回执得到）",
        "allowed_skills": frozenset({"rtv.ship"}),
        "allowed_tools": frozenset({"carrier.ship", "carrier.track"}),
        "model_tier": Tier.LIGHT,
    },
    "rtv_reconcile": {
        "cls": RtvReconcileAgent,
        "duty": "退货行 × 贷项通知单 × 到账三方对账，产出可核对的结论",
        "allowed_skills": frozenset({"rtv.reconcile"}),
        "allowed_tools": frozenset({"supplier.credit_query"}),
        "model_tier": Tier.LIGHT,
    },
    "rtv_settlement": {
        "cls": RtvSettlementAgent,
        "duty": "轮询取得终态回执（credited 与 settled 都只能由观察得到）",
        "allowed_skills": frozenset({"rtv.observe", "rtv.compensate"}),
        "allowed_tools": frozenset({"supplier.credit_query", "ap.adjust_query",
                                    "supplier.rma_submit"}),
        "model_tier": Tier.LIGHT,
    },
}

#: 契约 C-R4：name -> (version, owner_roles, depends_tools)。
CONTRACT_C_R4 = {
    "rtv.intake": ("1.0.0", ["rtv_intake"], []),
    "rtv.dispose": ("1.0.0", ["rtv_disposition"], []),
    "rtv.ship": ("1.0.0", ["rtv_logistics"], ["carrier.ship", "carrier.track"]),
    "rtv.reconcile": ("1.0.0", ["rtv_reconcile"], ["supplier.credit_query"]),
    "rtv.observe": ("1.0.0", ["rtv_settlement"],
                    ["supplier.credit_query", "ap.adjust_query"]),
    "rtv.compensate": ("1.0.0", ["rtv_settlement"], ["supplier.rma_submit"]),
}

#: 契约 C-R5：五个 ToolPort 名。
CONTRACT_C_R5 = ("supplier.rma_submit", "supplier.credit_query",
                 "carrier.ship", "carrier.track", "ap.adjust_query")

#: 契约 C-R6：五个 artifact kind。
CONTRACT_C_R6 = ("rtv_intake", "rtv_disposition", "rtv_shipment",
                 "rtv_reconciliation", "rtv_settlement_advice")


# =========================================================================
# 一、五个角色（C-R7）与投放即注册（C-2）
# =========================================================================
def test_five_rtv_agents_are_registered_without_touching_agents_init():
    """五个 Agent 靠 `@register` 自动进 `AGENT_POOL`（冻结口径 C-2），池从 22 涨到 27。"""
    from maos.agents import AGENT_POOL

    for role, cls in ((r, CONTRACT_C_R7[r]["cls"]) for r in CONTRACT_C_R7):
        assert AGENT_POOL.get(role) is cls, f"{role} 没按 role 注册进 AGENT_POOL"
    assert sorted(r for r in AGENT_POOL if r.startswith("rtv_")) == sorted(CONTRACT_C_R7)
    assert len(AGENT_POOL) == 27, (
        f"本轨投放 5 个 Agent，池应从 22 涨到 27，实际 {len(AGENT_POOL)} —— "
        "对不上说明别的轨也动了池，整合期要重新对数")
    assert RTV_ROLES == ("rtv_intake", "rtv_disposition", "rtv_logistics",
                         "rtv_reconcile", "rtv_settlement"), (
        "RTV_ROLES 的顺序即 SOP 五步，场景的依赖链照它连，不许乱序")


@pytest.mark.parametrize("role", sorted(CONTRACT_C_R7))
def test_identity_matches_frozen_contract_c_r7(role):
    """role / duty / 白名单 / tier 与契约 C-R7 **逐字**相同。

    循环断言而不是手抄五遍：手抄的那一份迟早只有其中几行被人跟着改。
    """
    spec = CONTRACT_C_R7[role]
    identity = spec["cls"].identity
    assert identity.role == role
    assert identity.duty == spec["duty"], f"{role} 的 duty 与契约 C-R7 不符"
    assert identity.allowed_skills == spec["allowed_skills"]
    assert identity.allowed_tools == spec["allowed_tools"]
    assert identity.model_tier == spec["model_tier"]
    # 五个都只写 artifact —— 业务对象由 skill 写，Agent 碰不到库。
    assert identity.write_scope == frozenset({"artifact"})


def test_least_privilege_across_the_five_roles():
    """最小授权：🔴 裁定的人不碰承运商，发运的人不碰供应商开票（C-R7 那条红字）。"""
    assert RtvIntakeAgent.identity.allowed_tools == frozenset()
    assert RtvDispositionAgent.identity.allowed_tools == frozenset()
    # 发运岗碰得到承运商，碰不到供应商门户与 AP。
    assert RtvLogisticsAgent.identity.allowed_tools == frozenset(
        {"carrier.ship", "carrier.track"})
    # 对账岗只读供应商门户，碰不到承运商，也写不了 RMA。
    assert RtvReconcileAgent.identity.allowed_tools == frozenset({"supplier.credit_query"})
    assert "supplier.rma_submit" not in RtvReconcileAgent.identity.allowed_tools
    # 权威写入方只有观察岗持有 rtv.observe。
    for cls in (RtvIntakeAgent, RtvDispositionAgent, RtvLogisticsAgent, RtvReconcileAgent):
        assert "rtv.observe" not in cls.identity.allowed_skills, (
            f"{cls.__name__} 不该能自己写权威终态")
        assert "rtv.compensate" not in cls.identity.allowed_skills, (
            f"{cls.__name__} 不该能自己做补偿收口")


def test_unauthorized_tool_and_skill_are_refused():
    """越权是安全事件，必须抛出来，不能变成一条 failed 记录被吞掉。"""
    from maos.model.client import ScriptedModelClient

    agent = RtvReconcileAgent(ScriptedModelClient({}))
    with pytest.raises(PermissionDenied):
        agent.check_tool("carrier.ship")
    with pytest.raises(PermissionDenied):
        agent.skills.invoke("rtv.observe", {})


# =========================================================================
# 二、薄壳自证 —— 「换域只换 Skill」那句话的机器守卫
# =========================================================================
#: 权威终态的名字（C-R3）。它们**允许**出现在 identity 的 `duty=` 那一行里
#: —— 那是契约 C-R7 的冻结原文，一个字都不许改。除此之外出现即红。
_AUTHORITATIVE_WORDS = ("credited", "settled")

#: 金额/数量比较的形状。`->` 是返回值注解，先剔掉再扫，否则每个函数签名都误报。
_COMPARISON = re.compile(r"[<>]=?")

_AGENT_MODULES = ("intake_agent.py", "disposition_agent.py", "logistics_agent.py",
                  "reconcile_agent.py", "settlement_agent.py")


def _scrubbed_source(name: str) -> str:
    """读 Agent 模块源码，剔掉 `duty=` 那一行与返回值注解箭头。

    `duty` 是契约 C-R7 的冻结原文（`rtv_settlement` 那句里就带着两个权威终态的
    名字），不参与扫描；剔的是**一整行**，所以谁把判定塞进 duty 那行也藏不住 ——
    duty 的字面量另有一条用例逐字比对（`test_identity_matches_frozen_contract_c_r7`）。
    """
    src = (_AGENT_DIR / name).read_text(encoding="utf-8")
    kept = [ln for ln in src.splitlines() if not ln.lstrip().startswith("duty=")]
    return "\n".join(kept).replace("->", "")


@pytest.mark.parametrize("name", _AGENT_MODULES)
def test_agents_are_thin_shells_no_authoritative_literals(name):
    """五个 Agent 模块里不许把权威终态当值来用。

    判定一旦漏进 Agent，「同一个编排内核，换个领域只换 Skill / ToolPort / 业务对象」
    就不成立了：下一个业务域得把这五个 Agent 也重写一遍。而这种漏是**无声**的 ——
    代码照跑、测试照绿，直到换域那天才发现。所以钉在源码文本上。
    """
    scrubbed = _scrubbed_source(name)
    for word in _AUTHORITATIVE_WORDS:
        assert word not in scrubbed, (
            f"{name} 里出现了权威终态字面量 {word!r} —— 权威状态归 "
            f"{s11.AUTHORITATIVE_WRITER}，Agent 只搬运，不许自己判（铁律 8）")


@pytest.mark.parametrize("name", _AGENT_MODULES)
def test_agents_are_thin_shells_no_business_arithmetic(name):
    """五个 Agent 模块里不许有金额比较，也不许有金额算术。

    薄壳只做三件事：过闸、调 skill、把 output 包成 artifact。出现 `>` / `<` /
    `Decimal` / `float(` 就说明有人在这里算账了 —— 而算账是 skill 的事。
    """
    scrubbed = _scrubbed_source(name)
    hit = _COMPARISON.search(scrubbed)
    assert hit is None, (
        f"{name} 第 {scrubbed[:hit.start()].count(chr(10)) + 1} 行出现比较运算符 "
        f"{hit.group()!r} —— 薄壳里不该有任何判定")
    for token in ("Decimal", "float(", "sum(", "amount >", "amount <"):
        assert token not in scrubbed, (
            f"{name} 里出现 {token!r} —— 算账是 skill 的事，不是 Agent 的事")


def test_agents_init_is_untouched():
    """🔴 `maos/agents/__init__.py` **零改动**（冻结口径 C-2：投文件即注册）。

    显式 import 清单意味着多条并行轨都要改同一处，合并必冲突。所以本轨新增五个
    Agent 只做一件事：往 `maos/agents/rtv/` 投文件。

    断言的是**字节指纹**，取自本轨基线 4c956a8。这一条红了只有两种可能：
    有人给它加了显式 import（那就是破了 C-2），或者别的轨合法改了它（那就要在
    整合期一并裁定新的指纹）。两种都该停下来看一眼，而不是自动放过。
    """
    root = _AGENT_DIR.parent
    for name, digest in (
            ("__init__.py", "f76b3ea11dfadf7047635686ae21e0e0a7d2749172996c3d6fc66f2a7f5cacea"),
            ("base.py", "36bfd8c216195c842df511086108d6fe0c74289cf1b28b59cef61cf633164b5a"),
    ):
        raw = (root / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest, (
            f"maos/agents/{name} 与基线 4c956a8 不一致 —— 本轨不许动它（C-2）")
    # 光比指纹挡不住「改完再把指纹更新一遍」，所以再钉一条语义：入口里不认识 rtv。
    src = (root / "__init__.py").read_text(encoding="utf-8")
    assert "rtv" not in src, "注册入口里不该出现 rtv —— 投文件即注册，不靠显式 import"
    assert "pkgutil" in src and "iter_modules" in src


# =========================================================================
# 三、artifact kind（C-R6）
# =========================================================================
def test_artifact_kinds_match_contract_and_avoid_code_kinds():
    """五个 kind 与 C-R6 逐字相同，且**没有一个**是 patch_set / test_report。

    Gate 用产物类型判「这是不是代码类任务」（`runtime/gate.py::CODE_ARTIFACT_KINDS`）。
    沾上那两个 kind，退货任务就会被要求交一份跑出来的测试报告，而本域根本没有
    那种东西 —— 闸会恒 blocker，且报错信息指向测试而不是退货。
    """
    assert ALL_RTV_KINDS == CONTRACT_C_R6
    for kind in ALL_RTV_KINDS:
        assert kind not in (KIND_PATCH_SET, KIND_TEST_REPORT)

    from maos.artifacts import ALL_KINDS

    # 刻意**不进** ALL_KINDS：那份清单是跨轨冻结口径，单轨往里加会和别人撞。
    for kind in ALL_RTV_KINDS:
        assert kind not in ALL_KINDS, (
            f"{kind} 进了 maos/artifacts.py 的 ALL_KINDS —— 那是跨轨冻结面，本域不许加")


def test_every_task_produced_its_contracted_kind(happy):
    """五个节点各交出一份本域的 artifact，且不掺任何代码类产物。"""
    store = happy["store"]
    seen = set()
    for task_id in (s11.TASK_INTAKE, s11.TASK_DISPOSE, s11.TASK_SHIP,
                    s11.TASK_RECONCILE, s11.TASK_OBSERVE):
        kinds = {a["kind"] for a in store.list_artifacts(task_id)}
        assert kinds, f"{task_id} 一份产物都没交"
        assert kinds <= set(ALL_RTV_KINDS), f"{task_id} 交了本域之外的产物 {kinds}"
        seen |= kinds
    assert seen == set(ALL_RTV_KINDS), f"五个 kind 应各出现一次，实际 {sorted(seen)}"


# =========================================================================
# 四、六个 skill（C-R4）与五个工具名（C-R5）—— stub 也必须照契约
# =========================================================================
@pytest.mark.parametrize("name", sorted(CONTRACT_C_R4))
def test_stub_skill_contract_matches_frozen_c_r4(name):
    """stub skill 的 name / version / owner_roles / depends_tools 逐字照 C-R4。

    整合期投放 T63 的真 skill 时，注册表主键（name + version）完全一致，
    调用点零改动就自动升级为真实现 —— 这条用例守的就是「零改动」这句话。
    """
    version, owners, depends = CONTRACT_C_R4[name]
    cls = skill_get(name, version)
    assert cls is not None, f"{name} v{version} 没进注册表"
    assert cls.contract.name == name
    assert cls.contract.version == version
    assert sorted(cls.contract.owner_roles) == sorted(owners)
    assert sorted(cls.contract.depends_tools) == sorted(depends)


def test_stub_skill_owner_roles_match_the_agent_whitelists():
    """契约的 owner_roles 与 identity 的 allowed_skills 必须互相对得上。

    两侧谁也不核对谁正是 `ap.compensate` 那条 `owner-role-unknown` 的来历
    （见 `maos/capability/profiles.py` 抬头）。本域从第一天起就把它钉住。
    """
    for name, (_v, owners, _d) in CONTRACT_C_R4.items():
        for role in owners:
            assert name in CONTRACT_C_R7[role]["allowed_skills"], (
                f"{name} 自述 owner 是 {role}，可 {role} 的白名单里没有它")
        holders = {r for r, spec in CONTRACT_C_R7.items()
                   if name in spec["allowed_skills"]}
        assert holders == set(owners), (
            f"{name} 的实际持有者 {sorted(holders)} 与自述 {sorted(owners)} 不等")


def test_stub_tool_names_match_frozen_c_r5():
    """五个 ToolPort 名逐字照 C-R5，`ap.adjust_query` 只读这条也钉住。"""
    assert set(s11.STUB_TOOL_NAMES) == set(CONTRACT_C_R5)
    # 🔴 RTV 域不许写 AP：AP 系统的 stub 上只有 query，没有任何写方法。
    assert hasattr(s11.StubApSystem, "adjust_query")
    for attr in ("adjust_create", "adjust_post", "write", "submit"):
        assert not hasattr(s11.StubApSystem, attr), (
            f"StubApSystem 出现了写方法 {attr} —— 调整凭单由 AP 侧建，我方只观察")
    # skill 的 depends_tools 只许出现在 C-R5 这五个名字里。
    for _name, (_v, _o, depends) in CONTRACT_C_R4.items():
        assert set(depends) <= set(CONTRACT_C_R5)


def test_stub_clean_list_in_the_file_header_is_complete():
    """🔴 文件头的 stub 清单必须**一个不落**地列全六个 skill 与五个工具。

    漏一个，整合期就会有人以为那处是真的。所以这条用例照着契约逐名 grep 文件头。
    """
    doc = s11.__doc__ or ""
    header = doc.split("### 四、不是 stub 的部分")[0]
    for name in CONTRACT_C_R4:
        assert name in header, f"文件头的 stub 清单漏了 skill {name}"
    for name in CONTRACT_C_R5:
        assert name in header, f"文件头的 stub 清单漏了工具 {name}"
    for owner in ("T61", "T62", "T63"):
        assert owner in header, f"文件头没说清哪些 stub 归 {owner}"


# =========================================================================
# 五、顺利路径 —— 两个权威终态各要一份外部回执
# =========================================================================
def test_happy_path_settles_only_after_both_externals_say_so(happy):
    """`credited` 要供应商开出的贷项通知单，`settled` 要 AP 核销的调整凭单。"""
    store = happy["store"]
    case = s11.get_case(store, s11.TENANT_ID, s11.CASE_OK)
    assert case["biz_status"] == "settled"
    assert case["return_action"] == "credit"

    notes = s11._stub_query(
        store, "SELECT * FROM credit_note WHERE tenant_id=? AND case_id=?",
        (s11.TENANT_ID, s11.CASE_OK))
    assert len(notes) == 1, "credited 必须恰好有一张贷项通知单兜底"
    assert notes[0]["document_type"] == s11.DOC_TYPE_CREDIT_NOTE == "381", (
        "贷项通知单的 UNCL1001 单据类型码恒为 381（Peppol BIS Billing 3.0）")
    assert notes[0]["invocation_id"], "贷项通知单必须带 actor 锚点，否则审计链断了"
    # 供应商开票时间与我方观察到的时间刻意分开 —— 两者不是同一件事。
    assert notes[0]["issued_at"] != notes[0]["observed_at"]

    obs = s11._stub_query(
        store, "SELECT * FROM rtv_settlement_observation WHERE tenant_id=? AND case_id=?",
        (s11.TENANT_ID, s11.CASE_OK))
    assert len(obs) == 1 and obs[0]["observed_state"] == s11.AP_SETTLED
    assert obs[0]["ap_reference"], "终态观察必须带 AP 单号 —— 没有单号的「已退」对不了账"
    assert obs[0]["invocation_id"]


def test_happy_path_all_five_nodes_are_done(happy):
    """五个节点全部 DONE，Plan 收敛到 DONE。"""
    cp, plan_id = happy["cp"], happy["plan_id"]
    tasks = {t["task_id"]: t["state"] for t in cp.store.list_tasks(plan_id)}
    assert len(tasks) == 5
    assert set(tasks.values()) == {TaskState.DONE}, f"五个节点应全 DONE，实际 {tasks}"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE


def test_happy_path_terminal_states_were_asked_for_not_assumed(happy):
    """终态是**问出来的**：两个外部权威分开问，轮询不止一次。"""
    assert happy["advice"]["poll_count"] == s11.EXPECTED_POLLS_OK > 1
    assert happy["advice"]["credit_receipt"]["state"] == s11.SUPPLIER_ISSUED
    assert happy["advice"]["adjustment_receipt"]["state"] == s11.AP_SETTLED
    # 两个权威终态各有各的判据，且同增同减。
    assert set(s11.AUTHORITATIVE_RECEIPT_STATE) == set(s11.AUTHORITATIVE_STATES)
    # 🔴 `acknowledged` 绝不许换来 credited —— 那是「收到退货了」，不是「认了这笔钱」。
    assert s11.SUPPLIER_ACKNOWLEDGED not in s11.AUTHORITATIVE_RECEIPT_STATE["credited"]


def test_happy_path_credits_the_amount_the_supplier_acknowledged(happy):
    """可退的钱取**供应商认的那个**，不取我方自称的。

    两处金额刻意都留着、不合并成一处（C-R1 的 `amount_claimed` 注释）：
    「退货方自称的应退金额」与「贷项通知单认的金额」是两个事实，
    对不上正是本域要拦的事。
    """
    r = happy["reconciliation"]["reconciliation"]
    assert r["reconciled"] == 1
    assert r["creditable_amount"] == s11.AMOUNT_CREDITED_OK
    assert r["creditable_amount"] != s11.AMOUNT_CLAIMED_OK, (
        "两个数写成一样的话，「以外部权威为准」这句话在场景里就没有演出来")
    assert [f["rule_id"] for f in r["findings"]] == ["RTV-REC-01"]
    # 案子上记的仍是我方自称的那个数 —— 两处都留着。
    case = s11.get_case(happy["store"], s11.TENANT_ID, s11.CASE_OK)
    assert case["amount_claimed"] == s11.AMOUNT_CLAIMED_OK


def test_happy_path_intake_left_the_disposition_to_the_next_role(happy):
    """建案时 `return_action` 留空 —— 受理的人不该替裁定的人拍板（C-R1）。"""
    assert happy["intake"]["case"]["return_action"] == ""
    assert happy["intake"]["case"]["biz_status"] == s11.INITIAL_STATUS == "received"
    # 裁定有历史可查：一次裁定一行，按 attempt 区分。
    rows = s11._stub_query(
        happy["store"], "SELECT * FROM rtv_disposition WHERE tenant_id=? AND case_id=?",
        (s11.TENANT_ID, s11.CASE_OK))
    assert len(rows) == 1 and rows[0]["attempt"] == 1
    assert rows[0]["return_action"] == "credit"
    # 业务对象引用指得回源单。
    assert {r["object_table"] for r in happy["intake"]["refs"]} == {
        "rtv_case", "purchase_order", "goods_receipt"}


def test_happy_path_stopped_for_a_human_before_shipping(happy):
    """发运 effect_risk=H，Gate 过了也停过 BLOCKED 等人放行 —— 货发出去就退不回来。"""
    log = happy["cp"].store.list_event_log(happy["plan_id"])
    blocked = {e["task_id"] for e in log
               if e["event_type"] == "StateTransition"
               and e["to_state"] == TaskState.BLOCKED}
    assert blocked == {s11.TASK_SHIP, s11.TASK_OBSERVE}, (
        f"两个不可逆动作都该停下来等人，实际 {sorted(blocked)}")


# =========================================================================
# 六、权威事实边界（C-R3 / 铁律 8）—— 越权不静默失败
# =========================================================================
@pytest.mark.parametrize("to_status,receipt", [("credited", "issued"),
                                               ("settled", "settled")])
def test_only_the_observer_may_write_an_authoritative_state(to_status, receipt):
    """换个写入方去写权威终态：**抛异常 + 落一条事件**，两样都要有。

    只抛不落的话，「有人试图把外部状态写死为终态」这件事就只活在那一次调用栈里，
    进程一退就没了 —— 而它是需要事后查得到的安全事实。
    """
    store = _fresh_case_at(to_status)
    with pytest.raises(s11.AuthoritativeFactViolation) as exc:
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe", to_status=to_status,
            writer="rtv.reconcile", receipt_state=receipt, invocation_id="probe",
            extras={"plan_id": "plan-probe"})
    assert s11.AUTHORITATIVE_WRITER in str(exc.value)

    events = [e for e in store.list_event_log("plan-probe")
              if e["event_type"] == s11.VIOLATION_EVENT]
    assert len(events) == 1, "越权写入必须落一条 AuthoritativeFactViolation"
    assert events[0]["detail"]["domain"] == s11.DOMAIN, (
        "与 ap / refund 域共用事件类型时必须按 detail.domain 区分，否则查不出是哪个域")
    assert events[0]["detail"]["writer"] == "rtv.reconcile"
    # 案子的状态一个字都没动。
    assert s11.get_case(store, s11.TENANT_ID, "case-probe")["biz_status"] != to_status


def test_acknowledged_cannot_buy_credited():
    """🔴 拿 `acknowledged` 冒充 `issued`：被拒。

    两者在回执里字段齐全、形状一样，但差着一次会计确认 ——
    「供应商收到退货了」不是「供应商认了这笔钱」。
    """
    store = _fresh_case_at("credited")
    with pytest.raises(s11.AuthoritativeFactViolation) as exc:
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe", to_status="credited",
            writer=s11.AUTHORITATIVE_WRITER, receipt_state=s11.SUPPLIER_ACKNOWLEDGED,
            invocation_id="probe", extras={"plan_id": "plan-probe"})
    assert s11.SUPPLIER_ACKNOWLEDGED in str(exc.value)
    assert s11.get_case(store, s11.TENANT_ID, "case-probe")["biz_status"] != "credited"


def test_authoritative_write_without_an_actor_anchor_is_refused():
    """没有 actor 锚点（invocation_id）的权威写入被拒 —— 审计链不许断。"""
    store = _fresh_case_at("credited")
    with pytest.raises(s11.AuthoritativeFactViolation):
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe", to_status="credited",
            writer=s11.AUTHORITATIVE_WRITER, receipt_state=s11.SUPPLIER_ISSUED,
            invocation_id="", extras={"plan_id": "plan-probe"})


def _fresh_case_at(target: str):
    """造一套干净的库，把探针案子推到能迁往 `target` 的那个前置状态上。

    推进用的是**非权威**迁移（`disposed` / `shipped` / `credited` 里只有前两个是
    非权威的），所以造数据这一步本身不会先撞上守卫。
    """
    from maos.core.store import SqliteStore

    store = SqliteStore()
    store.init_schema()
    s11.ensure_schema(store)
    s11._guard_create_case(store, tenant_id=s11.TENANT_ID, case_id="case-probe",
                           supplier_id=s11.SUPPLIER_ID, po_id="PO-PROBE", po_version=1,
                           gr_id="GR-PROBE", amount_claimed="100.00", currency="CNY",
                           plan_id="plan-probe")
    for step in ("disposed", "shipped"):
        s11._guard_update_biz_status(store, tenant_id=s11.TENANT_ID,
                                     case_id="case-probe", to_status=step,
                                     writer="probe", extras={"plan_id": "plan-probe"})
    if target == "settled":
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe", to_status="credited",
            writer=s11.AUTHORITATIVE_WRITER, receipt_state=s11.SUPPLIER_ISSUED,
            invocation_id="seed", extras={"plan_id": "plan-probe"})
    return store


# =========================================================================
# 七、失败路径 —— 供应商不认，不是代码抛异常
# =========================================================================
def test_failure_path_is_a_business_fact_not_an_exception(failure):
    """🔴 失败演的是「供应商不认」，**不是**「代码抛了异常」。

    后者是 bug，证明不了任何关于编排的事。判据有三条，缺一不可：
    回执是供应商自己给的 `disputed`、五个 Agent 一个异常都没抛、
    对账的结论挂着真实规则编号。
    """
    advice = failure["advice"]
    assert advice["credit_receipt"]["state"] == s11.SUPPLIER_DISPUTED
    assert "不认" in advice["credit_receipt"]["message"]
    assert advice["poll_count"] == s11.MAX_POLLS_BAD
    # 五个 Agent 全回 ok —— 一个异常都没抛，这正是要演的。
    assert set(failure["agent_status"].values()) == {"ok"}
    # 结论挂着真实规则编号，不是一句自然语言吐槽。
    findings = failure["reconciliation"]["reconciliation"]["findings"]
    assert [f["rule_id"] for f in findings] == ["RTV-REC-03"]
    assert findings[0]["rule_id"] in s11.RULES


def test_failure_path_writes_nothing_when_the_supplier_refuses(failure):
    """**一个字都不写**：没拿到贷项通知单时，两张外部事实表都是空的。

    「我问累了」和「供应商说不认」是两回事，而两者都不该变成一行伪造的记录。
    躺一行假的贷项通知单等于自己给自己开发票。
    """
    store = failure["store"]
    assert s11._count(store, "SELECT COUNT(*) AS n FROM credit_note") == 0
    assert s11._count(
        store, "SELECT COUNT(*) AS n FROM rtv_settlement_observation") == 0
    # 业务状态机上「从没经过 credited / settled」是可查的：状态变更事件里没有它们。
    moves = {e["to_state"] for e in store.list_event_log(failure["plan_id"])
             if e["event_type"] == s11.BIZ_STATUS_EVENT}
    assert not (moves & s11.AUTHORITATIVE_STATES), (
        f"全程不该有任何一次进入权威终态的迁移，实际 {sorted(moves)}")


def test_failure_path_ends_in_compensated(failure):
    """业务状态收在 `compensated`，Plan 收敛到 FAILED。"""
    case = s11.get_case(failure["store"], s11.TENANT_ID, s11.CASE_BAD)
    assert case["biz_status"] == "compensated"
    assert case["biz_status"] in s11.BIZ_STATUS_FLOW
    assert s11.BIZ_STATUS_FLOW["compensated"] == (), "compensated 是终态，没有出边"
    assert failure["cp"].store.get_plan(failure["plan_id"])["state"] == PlanState.FAILED


def test_failure_path_all_five_agents_reported_ok(failure):
    """本轨要买的那句话：**五个 Agent 全回 ok，而案子确实没成**。

    `AgentOutput.status` 说的是「这一步跑完了没有」，不是「业务成功了没有」。
    业务成没成看的是 `rtv_case.biz_status` 与两份外部回执。
    """
    statuses = failure["agent_status"]
    assert len(statuses) == 5, f"五个任务都该有 Agent 自述，实际 {statuses}"
    assert set(statuses.values()) == {"ok"}
    assert set(statuses) == {s11.TASK_INTAKE_B, s11.TASK_DISPOSE_B, s11.TASK_SHIP_B,
                             s11.TASK_RECONCILE_B, s11.TASK_OBSERVE_B}


def test_failure_path_compensation_does_not_declare_the_goods_lost(failure):
    """补偿的语义是「不再推进」，**不是**「这批货或这笔钱确认作废」。

    走到补偿的案子里那批货**已经发出去了**。写成「作废」会让账面上凭空少一批货，
    而供应商那边收到了它 —— 下个月盘点时这批差额没有人查得清是哪来的。
    """
    comp = failure["compensation"]
    # 判据写成**字面量**，不写成 `== s11.SUPPLIER_DISPUTED`：后者两边同源，
    # 把常量改掉时断言仍然成立（ap 域的变异检验 M8 就是这么漏过去的）。
    assert comp["last_supplier_state"] == "disputed", (
        f"供应商明确说了不认，最后观察就该如实记，实际 {comp['last_supplier_state']!r}")
    assert comp["last_supplier_state"] != s11.UNOBSERVED, (
        "供应商说了话，就不该记成「一次都没观察到」")
    assert "不要" in comp["ticket"]["todo"], "工单必须明说不许凭它断定货或钱作废"
    assert comp["ticket"]["assignee"] == s11.APPROVER
    assert comp["rma"]["rma_id"], "补偿必须留下 RMA 申请的痕迹"

    rows = s11._stub_query(
        failure["store"], "SELECT * FROM rtv_compensation_record WHERE tenant_id=?"
        " AND case_id=? ORDER BY seq", (s11.TENANT_ID, s11.CASE_BAD))
    assert len(rows) == 2, "补偿必须同时留下 RMA 留痕与交涉工单"
    events = [e for e in failure["cp"].store.list_event_log(failure["plan_id"])
              if e["event_type"] == "CompensationExecuted"]
    assert len(events) == 1
    assert events[0]["detail"]["domain"] == s11.DOMAIN


def test_settled_cases_refuse_compensation(happy):
    """已经到权威终态的案子拒绝补偿 —— 那是数据被改坏的信号，不是一次空操作。"""
    from maos.skills.invoker import SkillInvoker

    res = SkillInvoker(s11.COMPENSATION_IDENTITY, happy["store"]).invoke(
        "rtv.compensate", {"tenant_id": s11.TENANT_ID, "case_id": s11.CASE_OK,
                           "supplier_portal": s11.PORTAL_OK, "operator": "someone",
                           "reason": "试图对已结清的案子补偿"})
    assert res.status == "failed" and "不许补偿" in res.error


# =========================================================================
# 八、铁律 9：业务状态不进 Task 状态机
# =========================================================================
@pytest.mark.parametrize("which", ["happy", "failure"])
def test_business_status_never_becomes_a_task_state(request, which):
    """两条路径都不许出现表外 Task 状态或表外迁移。

    断言两件事而不是一件：只查状态集合挡不住「用既有的两个状态连一条新边」。
    """
    out = request.getfixturevalue(which)
    cp, plan_id = out["cp"], out["plan_id"]
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    states = {t["state"] for t in cp.store.list_tasks(plan_id)}
    assert states <= known, f"出现了表外 Task 状态：{sorted(states - known)}"
    for biz in s11.BIZ_STATUS_FLOW:
        assert biz not in states, f"{biz} 是 rtv_case 的字段，不许变成 Task 状态"
    moves = {(e["from_state"], e["to_state"])
             for e in cp.store.list_event_log(plan_id)
             if e["event_type"] == "StateTransition"}
    assert moves <= set(TASK_TRANSITIONS), (
        f"出现了表外 Task 迁移：{sorted(moves - set(TASK_TRANSITIONS))}")


def test_no_new_state_was_added_to_the_frozen_contract():
    """铁律 9：本域七个业务状态一个都没进 `contracts/states.py`。"""
    known = {v for k, v in vars(TaskState).items()
             if not k.startswith("_") and isinstance(v, str)}
    assert not (set(s11.BIZ_STATUS_FLOW) & known), (
        "业务状态是 rtv_case 自己的字段，不是 Task 状态")
    assert s11.INITIAL_STATUS == "received"
    assert s11.AUTHORITATIVE_STATES == frozenset({"credited", "settled"}), (
        "🔴 两个权威终态是本域相对 ap / refund 域的增量（C-R3）")


# =========================================================================
# 九、🔴 缺省证据束零污染
# =========================================================================
def test_scenario_11_is_not_wired_into_default_scenarios():
    """本场景刻意不进缺省序列 —— 证据束恒为 8 束是跨轨冻结口径。

    与 `maos/tests/test_ap_flow.py::test_scenario_10_is_not_wired_into_default_scenarios`
    同构。这条断言是给整合轮看的：接进缺省序列时它会红，那正是提醒改这里的时机。
    """
    from maos.main import ALL_SCENARIOS, DEFAULT_SCENARIOS

    assert 11 not in ALL_SCENARIOS, (
        "场景 11 接进 ALL_SCENARIOS 了 —— 若是整合轮有意为之，把本条断言一并改掉")
    assert 11 not in DEFAULT_SCENARIOS
    assert ALL_SCENARIOS == (1, 2, 3, 4, 5, 6, 7), (
        f"缺省证据束恒为 8 束（scenario-1..7 + scenario-R5），实际 {ALL_SCENARIOS}")

    root = _AGENT_DIR.parent.parent.parent
    for rel in ("run.py", "scripts/make_evidence.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "scenario_11" not in src, (
            f"{rel} 里出现了 scenario_11 —— 缺省证据束零污染是跨轨冻结口径")
