"""场景 12 端到端 + RTV 域五个薄壳 Agent 的机器验收（T64）。

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
import json
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
from maos.flows import scenario_12 as s11
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
    """五个 Agent 靠 `@register` 自动进 `AGENT_POOL`（冻结口径 C-2）。

    池数在 T112 整合期重对过：T64 单轨基线上是 27（22 + 本域 5），
    主干合入 T55–T83 那批之后是 29。这条断言钉的是「本域投了 5 个」这件事，
    总数只是它的副产品 —— 对不上先看是不是别的轨也动了池。
    """
    from maos.agents import AGENT_POOL

    for role, cls in ((r, CONTRACT_C_R7[r]["cls"]) for r in CONTRACT_C_R7):
        assert AGENT_POOL.get(role) is cls, f"{role} 没按 role 注册进 AGENT_POOL"
    assert sorted(r for r in AGENT_POOL if r.startswith("rtv_")) == sorted(CONTRACT_C_R7)
    assert len(AGENT_POOL) == 29, (
        f"本域 5 个 Agent + 主干其余 24 个，池应为 29，实际 {len(AGENT_POOL)} —— "
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
        # 按**词**扫而不是按子串：`amount_credited` / `settled_observed` 是
        # `rtv.reconcile` 产物里的字段名（供应商认的金额、AP 侧有没有核销观察），
        # 搬运它们不是「把权威终态当值来判定」，而搬不动它们就等于摘要里丢掉了
        # 「以外部权威为准」那半句。`\b` 卡住下划线：`_credited` 的前一个字符是
        # 词字符，不成词边界，所以字段名不误报，而 `out["settled"]` 这种
        # 把状态当值取的写法照样红。
        hit = re.search(rf"\b{word}\b", scrubbed)
        assert hit is None, (
            f"{name} 第 {scrubbed[:hit.start()].count(chr(10)) + 1} 行出现了权威终态"
            f"字面量 {word!r} —— 权威状态归 {s11.AUTHORITATIVE_WRITER}，"
            f"Agent 只搬运，不许自己判（铁律 8）")


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
    """六个节点各交出一份本域的 artifact，且不掺任何代码类产物。

    节点六个、kind 五个：观察那一步在 DAG 上是两跳（`rtv.observe` 一次只问一个
    外部权威），两跳交的是同一个 kind。
    """
    store = happy["store"]
    seen = set()
    for task_id in (s11.TASK_INTAKE, s11.TASK_DISPOSE, s11.TASK_SHIP,
                    s11.TASK_OBSERVE, s11.TASK_OBSERVE_2, s11.TASK_RECONCILE):
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
    # 🔴 RTV 域不许写 AP：AP 系统实现上只有 query，没有任何写方法。
    # 方法名是 `query` 而不是 `adjust_query` —— 后者是 **port 的名字**（C-R5），
    # 前者是 `ApSystemPort` 协议上那个唯一的动作（`maos/tools/rtv.py`）。
    assert hasattr(s11.StubApSystem, "query")
    for attr in ("adjust_create", "adjust_post", "write", "submit", "post", "settle"):
        assert not hasattr(s11.StubApSystem, attr), (
            f"AP 系统实现上出现了写方法 {attr} —— 调整凭单由 AP 侧建，我方只观察")
    # skill 的 depends_tools 只许出现在 C-R5 这五个名字里。
    for _name, (_v, _o, depends) in CONTRACT_C_R4.items():
        assert set(depends) <= set(CONTRACT_C_R5)


def test_integration_source_table_in_the_file_header_is_complete():
    """🔴 文件头的整合来源表必须**一个不落**地列全六个 skill 与五个工具。

    T64 交付时这里是一张 stub 清单（哪几段是假的）；T112 把三段 stub 全换成真件之后
    它变成一张来源表（哪一段来自哪条轨）。两版守的是同一件事：
    **别让下一个人去猜这段代码的出处**。漏一个，下一个人就得回去翻 git。
    """
    doc = s11.__doc__ or ""
    header = doc.split("### 四、本来就是真的那部分")[0]
    for name in CONTRACT_C_R4:
        assert name in header, f"文件头的整合来源表漏了 skill {name}"
    for name in CONTRACT_C_R5:
        assert name in header, f"文件头的整合来源表漏了工具 {name}"
    for owner in ("T61", "T62", "T63"):
        assert owner in header, f"文件头没说清哪一段来自 {owner}"


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


def test_happy_path_all_six_nodes_are_done(happy):
    """六个节点全部 DONE，Plan 收敛到 DONE。"""
    cp, plan_id = happy["cp"], happy["plan_id"]
    tasks = {t["task_id"]: t["state"] for t in cp.store.list_tasks(plan_id)}
    assert len(tasks) == 6, (
        f"SOP 五步在 DAG 上是六个节点（观察拆两跳），实际 {len(tasks)} 个")
    assert set(tasks.values()) == {TaskState.DONE}, f"六个节点应全 DONE，实际 {tasks}"
    assert cp.store.get_plan(plan_id)["state"] == PlanState.DONE


def test_happy_path_terminal_states_were_asked_for_not_assumed(happy):
    """终态是**问出来的**：两个外部权威分开问、分两跳写，各自轮询不止一次。"""
    credit, advice = happy["credit"], happy["advice"]
    assert credit["poll_count"] == s11.EXPECTED_POLLS_CREDIT > 1
    assert advice["poll_count"] == s11.EXPECTED_POLLS_SETTLE > 1
    assert credit["observed_state"] == s11.SUPPLIER_ISSUED
    assert advice["observed_state"] == s11.AP_SETTLED
    # 🔴 两跳问的是**两个不同的外部系统** —— 一个都不许由另一个推定。
    assert credit["system"] == "supplier" and advice["system"] == "ap"
    assert credit["reference"] and advice["reference"], (
        "两跳都要带回一个可对账的外部单号，没有单号的「已退」对不了账")
    # 承运商那一步同样是问出来的：建单不等于送达。
    assert happy["shipment"]["poll_count"] == s11.EXPECTED_POLLS_SHIP > 1
    # 两个权威终态各有各的判据，且同增同减。
    assert set(s11.AUTHORITATIVE_RECEIPT_STATE) == set(s11.AUTHORITATIVE_STATES)
    # 🔴 `acknowledged` 绝不许换来 credited —— 那是「收到退货了」，不是「认了这笔钱」。
    assert s11.SUPPLIER_ACKNOWLEDGED not in s11.AUTHORITATIVE_RECEIPT_STATE["credited"]


def test_happy_path_keeps_both_amounts_apart(happy):
    """我方算的应退金额与**供应商认的**金额，两处都留着，不合并成一处。

    C-R1 给 `amount_claimed` 那条注释要的正是这个：「退货方自称的应退金额」与
    「贷项通知单认的金额」是两个事实，对不上正是本域要拦的事。

    `rtv.reconcile` 把它们放在两个字段里：`amount_claimed` 是按 `rtv_line` 现算的，
    `amount_credited` 是库里贷项通知单的合计（**只认 `rtv.observe` 落下的那份**）。
    `creditable_amount` 是第三个东西 —— 对上之后可以拿去动账的金额，
    按 T63 的口径取我方算的那个，因为差额已经在容差内核过了。
    """
    r = happy["reconciliation"]
    assert r["reconciled"] is True
    assert r["findings"] == [], f"三条腿都齐了就该没有 findings，实际 {r['findings']}"
    assert r["amount_claimed"] == s11.AMOUNT_CLAIMED_OK
    assert r["amount_credited"] == s11.AMOUNT_CREDITED_OK
    assert r["amount_credited"] != r["amount_claimed"], (
        "两个数写成一样的话，「以外部权威为准」这句话在场景里就没有演出来")
    assert r["settled_observed"] is True, "第三条腿：库里得有 AP 侧的 settled 观察"
    # 案子上记的仍是我方自称的那个数 —— 两处都留着。
    case = s11.get_case(happy["store"], s11.TENANT_ID, s11.CASE_OK)
    assert case["amount_claimed"] == s11.AMOUNT_CLAIMED_OK


def test_happy_path_reconciles_only_after_both_externals_spoke(happy):
    """🔴 对账排在两跳观察**之后**，三条腿才齐得了。

    `rtv.reconcile` 的第 ② / ③ 条腿只认库里由 `rtv.observe` 落下的贷项通知单与
    到账观察，不认它自己问到的回执（问到的只用来解释缺口）。所以它排在观察之前的话
    必然对不上，findings 恒为 `RTV-R-11` + `RTV-R-12`——那是如实报告，
    但把「三方对账」演成了「一方独白」。这条断言钉住 DAG 的这个顺序。
    """
    log = happy["cp"].store.list_event_log(happy["plan_id"])
    done_at = {}
    for idx, e in enumerate(log):
        if e["event_type"] == "StateTransition" and e["to_state"] == TaskState.DONE:
            done_at[e["task_id"]] = idx
    assert done_at[s11.TASK_OBSERVE] < done_at[s11.TASK_RECONCILE], (
        "对账必须排在第 1 跳观察之后")
    assert done_at[s11.TASK_OBSERVE_2] < done_at[s11.TASK_RECONCILE], (
        "对账必须排在第 2 跳观察之后")


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
    """三个 effect_risk=H 的动作，Gate 过了也各停过一次 BLOCKED 等人放行。

    发运：货发出去就退不回来。两跳观察：确认收口是不可逆动作，
    而且**两跳各要一次人的放行** —— 「供应商认了」与「钱到账了」是两个决定。
    """
    log = happy["cp"].store.list_event_log(happy["plan_id"])
    blocked = {e["task_id"] for e in log
               if e["event_type"] == "StateTransition"
               and e["to_state"] == TaskState.BLOCKED}
    assert blocked == {s11.TASK_SHIP, s11.TASK_OBSERVE, s11.TASK_OBSERVE_2}, (
        f"三个不可逆动作都该停下来等人，实际 {sorted(blocked)}")
    # 对账是只读推断，不是不可逆动作 —— 它不该停人。
    assert s11.TASK_RECONCILE not in blocked, (
        "对账只出结论、不动任何外部系统，给它挂 effect_risk=H 等于把闸当流程控制用")


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
    # 字段名是 `actor`（`guard._log_violation`），不是 T64 那版的 `writer`。
    assert events[0]["detail"]["actor"] == "rtv.reconcile"
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
    """没有 actor 锚点（invocation_id）的写入被拒 —— 审计链不许断。

    抛的是 `ValueError` 而不是 `AuthoritativeFactViolation`：整合后守卫把这条判据
    提到了**每一次** `update_biz_status` 的最前面（`guard._require_invocation_id`），
    不再只管权威终态 —— 非权威迁移少了锚点同样断链。判据变严了，异常类型跟着变，
    所以这里收两种。
    """
    store = _fresh_case_at("credited")
    with pytest.raises((s11.AuthoritativeFactViolation, ValueError)):
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe", to_status="credited",
            writer=s11.AUTHORITATIVE_WRITER, receipt_state=s11.SUPPLIER_ISSUED,
            invocation_id="", extras={"plan_id": "plan-probe"})
    # 非权威迁移也一样要锚点 —— 这是整合后新多出来的那一格。
    with pytest.raises(ValueError):
        s11._guard_update_biz_status(
            store, tenant_id=s11.TENANT_ID, case_id="case-probe",
            to_status="compensated", writer="probe", invocation_id="",
            extras={"plan_id": "plan-probe"})


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
    # 造数据这几跳也要带锚点：整合后守卫对**每一次**写入都要 actor 锚点，
    # 不只是权威终态那两跳（`guard._require_invocation_id`）。
    for step in ("disposed", "shipped"):
        s11._guard_update_biz_status(store, tenant_id=s11.TENANT_ID,
                                     case_id="case-probe", to_status=step,
                                     writer="probe", invocation_id="seed",
                                     extras={"plan_id": "plan-probe"})
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
    credit = failure["credit"]
    assert credit["observed_state"] == s11.SUPPLIER_DISPUTED
    assert "不认" in credit["message"]
    # 供应商第 ISSUE_AFTER_BAD 次就给出终态，到不了轮询上限 —— 终态是终态，不再变。
    assert credit["poll_count"] == s11.EXPECTED_POLLS_BAD < s11.MAX_POLLS_BAD
    # 拿到的是**终态**，可它不是好消息，所以业务状态一个字都没动。
    assert credit["advanced"] is False
    assert credit["needs_compensation"] is True
    # 跑到的 Agent 全回 ok —— 一个异常都没抛，这正是要演的。
    assert set(failure["agent_status"].values()) == {"ok"}
    # 🔴 第 2 跳仍问供应商侧：案子没到 credited，就轮不到问 AP。
    assert failure["advice"]["system"] == "supplier"
    assert failure["advice"]["observed_state"] == s11.SUPPLIER_DISPUTED


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
    # 🔴 **但不许不留痕**：「供应商说不认这笔」必须查得到。
    # 一个字都不写 ≠ 什么都没发生 —— 后者会让这件事只活在日志里，
    # 而下个月有人问「这笔退货当初到底怎么了」时，日志早就轮转掉了。
    adverse = [e for e in store.list_event_log(failure["plan_id"])
               if e["event_type"] == s11.ADVERSE_EVENT]
    assert len(adverse) == 2, (
        f"两跳观察各问到一次 disputed，各该留一条痕，实际 {len(adverse)} 条")
    for e in adverse:
        assert e["detail"]["domain"] == s11.DOMAIN
        assert e["detail"]["observed_state"] == s11.SUPPLIER_DISPUTED


def test_failure_path_ends_in_compensated(failure):
    """业务状态收在 `compensated`，Plan 收敛到 FAILED。"""
    case = s11.get_case(failure["store"], s11.TENANT_ID, s11.CASE_BAD)
    assert case["biz_status"] == "compensated"
    assert case["biz_status"] in s11.BIZ_STATUS_FLOW
    assert s11.BIZ_STATUS_FLOW["compensated"] == (), "compensated 是终态，没有出边"
    assert failure["cp"].store.get_plan(failure["plan_id"])["state"] == PlanState.FAILED


def test_failure_path_all_agents_that_ran_reported_ok(failure):
    """本轨要买的那句话：**跑到的 Agent 全回 ok，而案子确实没成**。

    `AgentOutput.status` 说的是「这一步跑完了没有」，不是「业务成功了没有」。
    业务成没成看的是 `rtv_case.biz_status` 与两份外部回执。

    五个而不是六个：链路在第 2 跳观察那里被人截停了（`effect_risk=H` -> BLOCKED
    -> `human_reject`），对账任务压根没执行 —— 那也是要演的一半：
    人在不可逆动作前截停之后，后面的步骤不该照跑。
    """
    statuses = failure["agent_status"]
    assert len(statuses) == 5, f"跑到的五个任务都该有 Agent 自述，实际 {statuses}"
    assert set(statuses.values()) == {"ok"}
    assert set(statuses) == {s11.TASK_INTAKE_B, s11.TASK_DISPOSE_B, s11.TASK_SHIP_B,
                             s11.TASK_OBSERVE_B, s11.TASK_OBSERVE_2_B}
    assert s11.TASK_RECONCILE_B not in statuses, (
        "人截停之后对账不该照跑 —— 它跑了说明截停没截住")


def test_failure_path_compensation_does_not_declare_the_goods_lost(failure):
    """补偿的语义是「不再推进」，**不是**「这批货或这笔钱确认作废」。

    走到补偿的案子里那批货**已经发出去了**。写成「作废」会让账面上凭空少一批货，
    而供应商那边收到了它 —— 下个月盘点时这批差额没有人查得清是哪来的。
    """
    comp = failure["compensation"]
    rows = s11._stub_query(
        failure["store"], "SELECT * FROM rtv_compensation_record WHERE tenant_id=?"
        " AND case_id=? ORDER BY seq", (s11.TENANT_ID, s11.CASE_BAD))
    assert len(rows) == 1, f"一次补偿收口留一行记录，实际 {len(rows)} 行"
    detail = json.loads(rows[0]["detail_json"])

    # 判据写成**字面量**，不写成 `== s11.SUPPLIER_DISPUTED`：后者两边同源，
    # 把常量改掉时断言仍然成立（ap 域的变异检验 M8 就是这么漏过去的）。
    assert detail["observed_state"] == "disputed", (
        f"供应商明确说了不认，补偿记录就该如实记，实际 {detail['observed_state']!r}")
    assert detail["observed_state"] != s11.UNOBSERVED, (
        "供应商说了话，就不该记成「一次都没观察到」")
    assert "不要" in rows[0]["reason"], "收口理由必须明说不许凭它断定货或钱作废"
    assert detail["from_status"] == "shipped", (
        "补偿是从「货已发出、供应商不认」那一档收的口 —— 这批货已经在对方仓库里了")
    # 供应商门户上要留一条痕：我方还在追这笔退货，不是就此不管了。
    assert comp["resubmitted"] is True
    assert comp["rma"]["rma_id"], "补偿必须留下 RMA 申请的痕迹"
    # 🔴 **幂等键不变**：补提拿回的是同一张退货授权，不是第二张。
    # 开出第二张授权会让「这批货挂在哪张单下」失去唯一答案。
    assert comp["rma"]["idempotency_key"] == f"{s11.TENANT_ID}:{s11.CASE_BAD}"

    # 收口这件事本身要留痕：业务状态变更事件里有那一跳。
    moves = [e for e in failure["cp"].store.list_event_log(failure["plan_id"])
             if e["event_type"] == s11.BIZ_STATUS_EVENT
             and e["to_state"] == "compensated"]
    assert len(moves) == 1
    assert moves[0]["detail"]["domain"] == s11.DOMAIN
    assert moves[0]["detail"]["actor"] == "rtv.compensate"


def test_settled_cases_refuse_compensation(happy):
    """已经到权威终态的案子拒绝补偿 —— 那是数据被改坏的信号，不是一次空操作。

    拦它的是业务状态机本身：`BIZ_STATUS_FLOW["settled"] == ()`，终态没有出边，
    所以 `guard.update_biz_status` 抛 `BizStatusTransitionError`。判据落在守卫里
    而不是 skill 的一句 `if`：后者改一次分支顺序就没了。
    """
    from maos.skills.invoker import SkillInvoker

    res = SkillInvoker(s11.COMPENSATION_IDENTITY, happy["store"]).invoke(
        "rtv.compensate", {"tenant_id": s11.TENANT_ID, "case_id": s11.CASE_OK,
                           "reason": "试图对已结清的案子补偿"})
    assert res.status == "failed", f"已结清的案子不该补得动，实际 {res.status}"
    assert "settled" in res.error and "compensated" in res.error, (
        f"错误里要说清是哪一跳被拦下的，实际 {res.error!r}")
    # 案子一个字都没动。
    assert s11.get_case(happy["store"], s11.TENANT_ID, s11.CASE_OK)["biz_status"] == "settled"


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
def test_scenario_12_is_not_wired_into_default_scenarios():
    """本场景刻意不进缺省序列 —— 证据束恒为 8 束是跨轨冻结口径。

    与 `maos/tests/test_ap_flow.py::test_scenario_10_is_not_wired_into_default_scenarios`
    同构。这条断言是给整合轮看的：接进缺省序列时它会红，那正是提醒改这里的时机。
    """
    from maos.main import ALL_SCENARIOS, DEFAULT_SCENARIOS

    assert 12 not in ALL_SCENARIOS, (
        "场景 12 接进 ALL_SCENARIOS 了 —— 若是整合轮有意为之，把本条断言一并改掉")
    assert 12 not in DEFAULT_SCENARIOS
    # 钉的是**缺省序列**，不是 ALL_SCENARIOS：后者已经长到 11（8=理赔 / 9=银行差错 /
    # 10=应付账款 / 11=跨域协同），而跨轨冻结的那条口径说的是缺省跑几束。
    # T64 基线上两者恰好相等，整合期各归各的。
    assert DEFAULT_SCENARIOS == (1, 2, 3, 4, 5, 6, 7), (
        f"缺省证据束恒为 8 束（scenario-1..7 + scenario-R5），实际 {DEFAULT_SCENARIOS}")

    root = _AGENT_DIR.parent.parent.parent
    for rel in ("run.py", "scripts/make_evidence.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "scenario_12" not in src, (
            f"{rel} 里出现了 scenario_12 —— 缺省证据束零污染是跨轨冻结口径")
