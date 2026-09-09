"""职责能力档案 + 声明一致性体检机。

## 这张表解决什么

「什么职责该配什么 skill、什么工具」此前只存在于 23 处**互不相认的手写白名单**里：
每个 Agent 文件自己写一份 ``allowed_skills`` / ``allowed_tools``，每个 SkillContract
自己写一份 ``owner_roles`` / ``depends_tools``，两侧谁也不核对谁。于是漂移是必然的，
而且是**无声的**：白名单里放行一个根本不存在的工具名（``sandbox``），运行期要等到
真去调它才炸；契约把 owner 写成一个全仓不存在的角色（``ap_compensation``），
永远不会有任何一条测试红。

``PROFILES`` 把这层关系收成一张表，``check_consistency()`` 是照着事实源核对它的体检机。

## 应然，不是实然

档案记的是**该职责应该有什么**，不是**identity 现在声明了什么**。两者今天就不一致
（见 ``maos/tests/test_capability_profiles.py`` 的基线快照），照抄实然等于把 bug
抄进档案，这张表也就失去了全部意义。

应然的依据一律是**实际调用点**，不是推测：``coding`` 的工具写 ``git-mcp``，因为
``code_repo_patch.py`` 里 ``invoke_tool`` 的实参就是 ``GIT_MCP_PORT``；``testing``
写 ``sandbox.pytest_run``，因为 ``test_verify.py`` 调的是 ``PYTEST_RUN_PORT``。
两处 identity 里那个 ``sandbox`` 全仓没有对应的 ToolPort，所以档案里没有它。

**本轨不修那两处 identity**（铁律 4，漂移逐条记在 ``docs/BACKLOG.md`` 的 task-T58
小节）。档案与 identity 的差集本身就是修改单，人类照着裁定。

## 三个事实源全部动态取

一处手写清单都没有 —— 手写的必然跟着漂，而这台机器存在的全部理由就是抓漂移：

* 角色与白名单：扫 ``maos.agents`` 包内所有 ``BaseAgent`` 子类的 ``identity``
* skill 与契约：``maos.skills.registry``（先触发 builtin 动态发现）
* 本地工具名：import ``maos.tools`` 下每个模块，按 ``isinstance(obj, ToolPort)`` 收集

事实源采集时的 import 异常**一律往上抛，不吞**：吞掉之后体检机会少看见一批事实，
然后报告「一切正常」—— 那比直接红掉危险得多。
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

import maos.agents
import maos.tools
from maos.agents.base import AgentIdentity, BaseAgent
from maos.model.client import Tier
from maos.skills import registry
from maos.skills.contract import SkillContract
from maos.tools.port import ToolPort


@dataclass(frozen=True)
class CapabilityProfile:
    """一个职责的能力档案。

    ``frozen=True`` 不是洁癖：档案是运行期被反复读的判据面，可变就意味着
    某个调用方能在跑到一半时把自己的权限改大，而且改完没有任何痕迹。
    """

    duty: str                                    # 职责键，形如 <域>.<职能>
    roles: frozenset[str]                        # 承担该职责的角色
    skills: frozenset[str]                       # 该职责应有的 skill
    tools: frozenset[str]                        # 该职责应有的本地 ToolPort
    mcp_servers: frozenset[str] = frozenset()    # 该职责应挂的 MCP server 名
    model_tier: str = ""                         # 该职责的算力档（对齐 Tier）


@dataclass(frozen=True)
class Finding:
    """一条体检结论。结构化返回，不打印 —— 打印的东西没法被测试断言。"""

    kind: str        # 见本模块 KINDS
    subject: str     # 角色名或 skill 名
    detail: str
    severity: str    # error | warning


#: 体检机会产出的 finding 类型，以及各自的判级理由。
#:
#: error 与 warning 的分界线是「会不会让一次调用走到错误的地方」：
#: 白名单放行一个不存在的工具、契约指向一个不存在的角色，都是**指向落空**——
#: 装配期照着它接线必然接不上；而 owner_roles 多一个少一个是**文档漂移**，
#: 接线仍然接得上，只是自述与事实不符。
KINDS = {
    "tool-not-declared": "error",        # allowed_tools 里的名字查不到 ToolPort
    "skill-not-registered": "error",     # allowed_skills 里的名字查不到注册
    "depends-tool-missing": "error",     # depends_tools 里的名字查不到 ToolPort
    "depends-tool-not-allowed": "error",  # 持有者 allowed_tools 缺 skill 依赖的工具
    "owner-role-unknown": "error",       # owner_roles 指向全仓不存在的角色
    "skill-unowned": "warning",          # skill 有实现，但没有任何角色被授权调它
    "owner-roles-mismatch": "warning",   # owner_roles 与实际持有者不等（但有持有者）
}


# --------------------------------------------------------------------------
# 档案表：23 个职责，一职责一角色
# --------------------------------------------------------------------------
#
# 为什么不把四个域的 *_intake 合成一个「受理」职责：合并之后这条职责的 skills
# 只能取并集，而 T60 的装配是照着档案发权限的 —— refund_intake 会因此拿到
# ap.intake，四个域的受理彼此越权。最小权限比表的行数少几行值钱。
#
# 跨域同职能没有丢：职责键统一写成 ``<域>.<职能>``，``.intake`` 后缀一 grep 就聚齐。
# ``roles`` 保持集合类型（而不是单个 str），是留给「两个角色共担一个职责」的将来 ——
# 那天不必再改这张表的形状。
#
# ``mcp_servers`` 本轮**全部留空**：MCP server 的注册表与命名口径是 T59 的产出，
# 在本轨的基线里还不存在。字段形状现在定死，值等 T59 落地后再填 —— 先猜一个名字
# 填进去，等于给整合期埋一次 23 行的返工。
PROFILES: dict[str, CapabilityProfile] = {
    # ---- 平台域：跨业务域的通用工序 --------------------------------------
    "platform.planning": CapabilityProfile(
        duty="platform.planning",
        roles=frozenset({"manager"}),
        skills=frozenset({"kb.retrieve", "req.normalize"}),
        tools=frozenset(),
        model_tier=Tier.STRONG,
    ),
    "platform.requirement": CapabilityProfile(
        duty="platform.requirement",
        roles=frozenset({"requirement"}),
        skills=frozenset({"req.normalize"}),
        tools=frozenset(),
        model_tier=Tier.STRONG,
    ),
    "platform.architecture": CapabilityProfile(
        duty="platform.architecture",
        roles=frozenset({"architecture"}),
        skills=frozenset(),
        tools=frozenset(),
        model_tier=Tier.STRONG,
    ),
    "platform.code-change": CapabilityProfile(
        duty="platform.code-change",
        roles=frozenset({"coding"}),
        skills=frozenset({"code.repo-patch", "kb.retrieve"}),
        # 只有 git-mcp：code_repo_patch.py 里唯一的 invoke_tool 实参是 GIT_MCP_PORT。
        # identity 里那个 ``sandbox`` 全仓无对应端口，而真端口 sandbox.git_apply
        # 至今没有任何生产调用方 —— 两边都不该进档案。
        tools=frozenset({"git-mcp"}),
        model_tier=Tier.MEDIUM,
    ),
    "platform.test-verification": CapabilityProfile(
        duty="platform.test-verification",
        roles=frozenset({"testing"}),
        skills=frozenset({"test.verify"}),
        # test_verify.py 调的是 PYTEST_RUN_PORT，端口实名 sandbox.pytest_run。
        tools=frozenset({"sandbox.pytest_run"}),
        model_tier=Tier.MEDIUM,
    ),
    "platform.review": CapabilityProfile(
        duty="platform.review",
        roles=frozenset({"reviewer"}),
        skills=frozenset(),
        tools=frozenset(),
        model_tier=Tier.STRONG,
    ),

    # ---- 应付账款域 ------------------------------------------------------
    "ap.intake": CapabilityProfile(
        duty="ap.intake",
        roles=frozenset({"ap_intake"}),
        skills=frozenset({"ap.intake"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "ap.matching": CapabilityProfile(
        duty="ap.matching",
        roles=frozenset({"ap_match"}),
        skills=frozenset({"ap.match"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "ap.payment-planning": CapabilityProfile(
        duty="ap.payment-planning",
        roles=frozenset({"ap_control"}),
        skills=frozenset({"ap.plan-payment"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "ap.payment-execution": CapabilityProfile(
        duty="ap.payment-execution",
        roles=frozenset({"ap_treasury"}),
        skills=frozenset({"ap.execute", "ap.observe"}),
        tools=frozenset({"bank.pay", "bank.query"}),
        model_tier=Tier.LIGHT,
    ),

    # ---- 保险理赔域 ------------------------------------------------------
    "claim.intake": CapabilityProfile(
        duty="claim.intake",
        roles=frozenset({"claim_intake"}),
        skills=frozenset({"claim.intake", "issue.aggregate"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "claim.adjudication": CapabilityProfile(
        duty="claim.adjudication",
        roles=frozenset({"claim_adjudicator"}),
        skills=frozenset({"claim.adjudicate"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "claim.settlement": CapabilityProfile(
        duty="claim.settlement",
        roles=frozenset({"claim_settlement"}),
        skills=frozenset({"claim.settle"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "claim.payment-execution": CapabilityProfile(
        duty="claim.payment-execution",
        roles=frozenset({"claim_payment"}),
        skills=frozenset({"claim.observe", "claim.pay"}),
        tools=frozenset({"payer.query", "payer.submit"}),
        model_tier=Tier.LIGHT,
    ),

    # ---- 支付差错处理域 --------------------------------------------------
    "investigation.intake": CapabilityProfile(
        duty="investigation.intake",
        roles=frozenset({"investigation_intake"}),
        skills=frozenset({"investigation.file"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "investigation.classification": CapabilityProfile(
        duty="investigation.classification",
        roles=frozenset({"investigation_classify"}),
        skills=frozenset({"investigation.classify"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "investigation.cancellation": CapabilityProfile(
        duty="investigation.cancellation",
        roles=frozenset({"investigation_cancel"}),
        skills=frozenset({"investigation.cancel"}),
        tools=frozenset({"clearing.cancel"}),
        model_tier=Tier.LIGHT,
    ),
    "investigation.observation": CapabilityProfile(
        duty="investigation.observation",
        roles=frozenset({"investigation_observe"}),
        skills=frozenset({"investigation.compensate", "investigation.observe"}),
        tools=frozenset({"clearing.resolution"}),
        model_tier=Tier.LIGHT,
    ),

    # ---- 退款域 ----------------------------------------------------------
    "refund.intake": CapabilityProfile(
        duty="refund.intake",
        roles=frozenset({"refund_intake"}),
        # T101 / T102 给受理岗加的两个 skill（诉求类型分类、表头映射），整合 T55-T83 时补齐
        skills=frozenset({"issue.aggregate", "notify.customer", "refund.intake",
                          "refund.reason_classify", "sheet.header_map"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.policy-judgement": CapabilityProfile(
        duty="refund.policy-judgement",
        roles=frozenset({"refund_policy"}),
        skills=frozenset({"policy.match"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.evidence-check": CapabilityProfile(
        duty="refund.evidence-check",
        roles=frozenset({"refund_evidence"}),
        skills=frozenset({"refund.evidence_check"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.risk-screen": CapabilityProfile(
        duty="refund.risk-screen",
        roles=frozenset({"refund_risk"}),
        skills=frozenset({"refund.risk_screen"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.finance-settlement": CapabilityProfile(
        duty="refund.finance-settlement",
        roles=frozenset({"refund_finance"}),
        # policy.match 是刻意的：财务侧要自行复核规则，不接受政策侧的结论口述。
        skills=frozenset({"finance.settle", "policy.match"}),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.channel-writeoff": CapabilityProfile(
        duty="refund.channel-writeoff",
        roles=frozenset({"refund_channel"}),
        skills=frozenset(),
        tools=frozenset(),
        model_tier=Tier.LIGHT,
    ),
    "refund.payment-execution": CapabilityProfile(
        duty="refund.payment-execution",
        roles=frozenset({"refund_payment"}),
        skills=frozenset({"payment.execute", "payment.observe"}),
        tools=frozenset({"gateway.query", "gateway.refund"}),
        model_tier=Tier.LIGHT,
    ),
}


# --------------------------------------------------------------------------
# 事实源采集
# --------------------------------------------------------------------------

def identities() -> dict[str, AgentIdentity]:
    """全仓所有 ``BaseAgent`` 子类的 identity，role -> AgentIdentity。

    取的是**声明面**而不是 ``AGENT_POOL``：``manager`` 有完整的 identity、
    三道闸对它一样生效，只是刻意不注册进池（冻结口径 C-2）。只认池里的 22 个，
    ``manager`` 的白名单就永远没人核对，而它恰好是 ``req.normalize`` /
    ``kb.retrieve`` 两个 skill 声明的 owner。
    """
    out: dict[str, AgentIdentity] = {}
    for mod in pkgutil.walk_packages(maos.agents.__path__, "maos.agents."):
        module = importlib.import_module(mod.name)
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, BaseAgent)
                    and obj is not BaseAgent):
                identity = getattr(obj, "identity", None)
                if identity is not None:
                    out[identity.role] = identity
    return out


def skill_contracts() -> dict[str, SkillContract]:
    """全部已注册 skill 的契约，name -> SkillContract（按名取最高版本）。

    先 ``registry.get`` 一次触发 builtin 动态发现：``names()`` 不带这层兜底，
    直接调它会在 builtin 尚未 import 时报「一个 skill 都没有」。
    """
    registry.get("__trigger_builtin_discovery__")
    return {name: registry.get(name).contract for name in registry.names()}


def tool_names() -> frozenset[str]:
    """全仓本地 ToolPort 的实名。

    import 模块后按 ``isinstance(obj, ToolPort)`` 收，而不是正则扫源码：正则拿到的
    是**源码里的字面量**，对 ``ToolPort(name=...)`` 的写法敏感，端口若由工厂函数
    造出来或名字来自常量就整个漏掉；isinstance 拿到的是运行期真值，
    ``invoke_tool`` 拿到的是哪个名字，这里收到的就是哪个名字。
    """
    names: set[str] = set()
    for mod in pkgutil.walk_packages(maos.tools.__path__, "maos.tools."):
        module = importlib.import_module(mod.name)
        for obj in vars(module).values():
            if isinstance(obj, ToolPort):
                names.add(obj.name)
    return frozenset(names)


# --------------------------------------------------------------------------
# 体检机
# --------------------------------------------------------------------------

def check_consistency() -> list[Finding]:
    """核对声明面之间的一致性，返回结构化报告（不打印、不抛）。

    四类核对，与 ``docs/BACKLOG.md`` task-T58 小节的甲/乙/丙/丁一一对应：

    甲 identity 的白名单里有查不到实体的名字（skill / tool）
    乙 SkillContract.owner_roles 与实际持有该 skill 的角色不一致
    丙 SkillContract.depends_tools 指向不存在的 ToolPort
    丁 skill 依赖的工具没出现在持有者的 allowed_tools 里（运行期必 PermissionDenied）

    返回顺序稳定（先按 kind 的登记序，再按 subject 字典序），便于逐条比对。
    """
    ids = identities()
    skills = skill_contracts()
    tools = tool_names()
    found: list[Finding] = []

    # ---- 甲：白名单放行了查不到的实体 ----------------------------------
    for role in sorted(ids):
        identity = ids[role]
        for name in sorted(set(identity.allowed_skills) - set(skills)):
            found.append(Finding(
                kind="skill-not-registered", subject=role,
                detail=f"allowed_skills 里的 {name!r} 在 skill 注册表里查不到",
                severity=KINDS["skill-not-registered"]))
        for name in sorted(set(identity.allowed_tools) - tools):
            found.append(Finding(
                kind="tool-not-declared", subject=role,
                detail=f"allowed_tools 里的 {name!r} 全仓没有对应的 ToolPort",
                severity=KINDS["tool-not-declared"]))

    # ---- 乙 / 丙 / 丁：契约侧 -------------------------------------------
    for name in sorted(skills):
        contract = skills[name]
        holders = sorted(r for r, i in ids.items() if name in i.allowed_skills)
        declared = sorted(set(contract.owner_roles))

        # 乙-1：owner 指向一个全仓不存在的角色 —— 装配期照着它接线必然接不上。
        for ghost in sorted(set(contract.owner_roles) - set(ids)):
            found.append(Finding(
                kind="owner-role-unknown", subject=name,
                detail=f"owner_roles 里的 {ghost!r} 全仓没有对应的 AgentIdentity",
                severity=KINDS["owner-role-unknown"]))

        # 乙-2：skill 有实现，却没有任何角色被授权调它 —— 这条路根本走不到。
        if not holders:
            found.append(Finding(
                kind="skill-unowned", subject=name,
                detail=(f"没有任何角色的 allowed_skills 含它"
                        f"（契约自述 owner_roles={declared}）"),
                severity=KINDS["skill-unowned"]))
        # 乙-3：有持有者，但与自述不等 —— 自述漂了，接线仍接得上。
        elif declared != holders:
            found.append(Finding(
                kind="owner-roles-mismatch", subject=name,
                detail=f"owner_roles={declared}，实际持有={holders}",
                severity=KINDS["owner-roles-mismatch"]))

        # 丙：依赖了一个不存在的 ToolPort。
        missing = sorted(set(contract.depends_tools) - tools)
        if missing:
            found.append(Finding(
                kind="depends-tool-missing", subject=name,
                detail=f"depends_tools 里的 {missing} 全仓没有对应的 ToolPort",
                severity=KINDS["depends-tool-missing"]))

        # 丁：持有者无权调它依赖的工具 —— 真跑到这一步会被 check_tool 拦下。
        for role in holders:
            lacking = sorted(set(contract.depends_tools) - set(ids[role].allowed_tools))
            if lacking:
                found.append(Finding(
                    kind="depends-tool-not-allowed", subject=name,
                    detail=f"持有者 {role} 的 allowed_tools 缺 {lacking}",
                    severity=KINDS["depends-tool-not-allowed"]))

    order = list(KINDS)
    return sorted(found, key=lambda f: (order.index(f.kind), f.subject, f.detail))


def profile_of(role: str) -> CapabilityProfile | None:
    """按角色取档案。取不到返回 None —— 调用方自己决定这算不算错。"""
    for profile in PROFILES.values():
        if role in profile.roles:
            return profile
    return None
