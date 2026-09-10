"""职责能力档案与声明一致性闸的机器验收（T58）。

## 为什么基线是「快照」而不是 ``assert check_consistency() == []``

体检机今天就抓得到 12 条 finding，而这些漂移**不是本轨引入的**，本轨也不许修
（铁律 4：发现问题记账，不当场改，明细在 ``docs/BACKLOG.md`` 的 task-T58 小节）。
写成 ``== []`` 会让全仓一起红，红灯从此变成常态，也就没人再看它了。

所以断言的是**不多不少**：

* 多出一条 → 红。有人又引入了新的漂移，这正是这道闸要挡的。
* 少了一条 → 也红。有人修好了却没更新基线 —— 红灯在提醒他把 BACKLOG 那条一起划掉，
  而不是让基线悄悄变成一个没人对得上的数字。

断言的粒度是 ``(kind, subject)``，**不含 detail 文案**：文案是给人看的，改一版措辞
就假红一次，那种红灯只会训练人去改测试。
"""

from __future__ import annotations

import dataclasses

import pytest

from maos.agents import AGENT_POOL
from maos.capability.profiles import (
    KINDS,
    PROFILES,
    CapabilityProfile,
    Finding,
    check_consistency,
    identities,
    profile_of,
    skill_contracts,
    tool_names,
)

# 已知基线：2026-09-01 在 d386387 上实跑 check_consistency() 的全量结果。
# 每一组的出处见 docs/BACKLOG.md 的 ## task-T58 小节（甲/乙/丙/丁四组）。
#
# 甲 identity 白名单放行了查不到的实体
#   coding / testing 的 allowed_tools 里写着 ``sandbox``，全仓没有这个 ToolPort
#   （真端口叫 sandbox.git_apply / sandbox.pytest_run）。
# 丙 契约依赖了同一个不存在的名字（与甲同源）
#   code.repo-patch / test.verify 的 depends_tools 里同样是 ``sandbox``。
# 乙 owner_roles 与实际持有者不符，三种性质分开报：
#   owner-role-unknown  owner 指向全仓不存在的角色（ap_compensation）
#   skill-unowned       skill 有实现，但没有任何角色被授权调它
#   owner-roles-mismatch 有持有者，但与自述不等
# 丁 depends-tool-not-allowed 当前 0 条 —— 所以它不出现在基线里。
BASELINE: frozenset[tuple[str, str]] = frozenset({
    ("tool-not-declared", "coding"),
    ("tool-not-declared", "testing"),
    ("depends-tool-missing", "code.repo-patch"),
    ("depends-tool-missing", "test.verify"),
    ("owner-role-unknown", "ap.compensate"),
    ("skill-unowned", "ap.compensate"),
    ("skill-unowned", "claim.compensate"),
    ("skill-unowned", "kb.sink"),
    ("skill-unowned", "refund.compensate"),
    # T116：`refund.snapshot_check` 与上面四条同类 —— 它由 `flows/*` 经一个
    # 专用 identity 直接调（口径同 `scenario_7.COMPENSATION_IDENTITY`），
    # 那个 identity 不进 AGENT_POOL，所以体检机看不到持有者。
    # 处置记在 `docs/BACKLOG.md` 的 `## task-t116`：要真正有主，得让某个
    # 退款域 Agent 把它收进 allowed_skills，而那是整合期的事。
    ("skill-unowned", "refund.snapshot_check"),
    ("owner-roles-mismatch", "issue.aggregate"),
    ("owner-roles-mismatch", "policy.match"),
    ("owner-roles-mismatch", "req.normalize"),
})

#: 档案（应然）与 identity（实然）今天的全部差异。空 = 该角色两侧一致。
#: 形状是 role -> (identity 多出的工具, 档案多出的工具)。
#: 这两条与 BASELINE 的 tool-not-declared 是同一个洞的两面：identity 写了一个
#: 不存在的 ``sandbox``，档案写的是实际调用点上那个真端口。
TOOL_DIFF_BASELINE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # code_repo_patch.py 唯一的 invoke_tool 实参是 GIT_MCP_PORT；真端口
    # sandbox.git_apply 至今没有任何生产调用方，所以档案里也没有它。
    "coding": (frozenset({"sandbox"}), frozenset()),
    # test_verify.py 调的是 PYTEST_RUN_PORT，实名 sandbox.pytest_run。
    "testing": (frozenset({"sandbox"}), frozenset({"sandbox.pytest_run"})),
}


def _pairs(findings: list[Finding]) -> frozenset[tuple[str, str]]:
    return frozenset((f.kind, f.subject) for f in findings)


# ---- 档案表本身 ----------------------------------------------------------

def test_profile_is_frozen() -> None:
    """档案是判据面，不许在运行期被改大。"""
    assert dataclasses.is_dataclass(CapabilityProfile)
    assert CapabilityProfile.__dataclass_params__.frozen is True
    assert Finding.__dataclass_params__.frozen is True

    profile = next(iter(PROFILES.values()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.tools = frozenset({"bank.pay"})       # type: ignore[misc]


def test_profiles_cover_every_pooled_role() -> None:
    """22 个在池角色一个不落 —— 角色名从 AGENT_POOL 动态取，不手写。"""
    covered: set[str] = set()
    for profile in PROFILES.values():
        covered |= profile.roles
    missing = sorted(set(AGENT_POOL) - covered)
    assert not missing, f"这些在池角色没有职责档案：{missing}"

    for role in AGENT_POOL:
        assert profile_of(role) is not None


def test_profiles_cover_manager_too() -> None:
    """未注册进池、但有 identity 的角色同样要有档案。

    ``manager`` 刻意不进 ``AGENT_POOL``（冻结口径 C-2），可它的白名单是真的、
    三道闸对它一样生效。漏掉它，它的能力面就永远没人核对。
    """
    covered: set[str] = set()
    for profile in PROFILES.values():
        covered |= profile.roles
    assert sorted(set(identities()) - covered) == []
    assert sorted(covered - set(identities())) == [], "档案里有全仓不存在的角色"


def test_profile_duty_key_matches_dict_key() -> None:
    """字典键与 duty 字段不许对不上 —— 两个都写着职责名，漂了就没人知道信哪个。"""
    for key, profile in PROFILES.items():
        assert profile.duty == key


def test_profile_capabilities_all_exist() -> None:
    """档案里的 skill / tool 名必须是真实存在的实体。

    档案记应然，但应然不等于可以凭空写一个名字 —— 那就退化成第 24 份手写白名单了。
    """
    skills = set(skill_contracts())
    tools = tool_names()
    for key, profile in PROFILES.items():
        assert not (profile.skills - skills), f"{key} 的 skills 有查不到的：{profile.skills - skills}"
        assert not (profile.tools - tools), f"{key} 的 tools 有查不到的：{profile.tools - tools}"


def test_profile_model_tier_matches_identity() -> None:
    """算力档与 identity 一致 —— 这一项当前没有漂移，所以直接断严的。"""
    ids = identities()
    for key, profile in PROFILES.items():
        for role in profile.roles:
            assert profile.model_tier == ids[role].model_tier, key


def test_profile_skills_match_identity() -> None:
    """技能面当前应然 == 实然：甲组里 skill 侧的漂移是 0 处，所以这条可以断严。"""
    ids = identities()
    for key, profile in PROFILES.items():
        for role in profile.roles:
            assert profile.skills == frozenset(ids[role].allowed_skills), key


def test_profile_tool_diff_against_identity_is_the_known_drift() -> None:
    """档案与 identity 的工具面差集，恰好等于已知的那一处漂移（§6 自检 6）。

    这条是本轨「应然 vs 实然」的守卫：差出来的每一样都必须解释得清。今天解释是
    「identity 写了一个不存在的 ``sandbox``」；哪天有人修了 identity，这里会红，
    提醒他把 TOOL_DIFF_BASELINE 一起清掉。
    """
    ids = identities()
    diff: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for profile in PROFILES.values():
        for role in profile.roles:
            declared = frozenset(ids[role].allowed_tools)
            only_identity = declared - profile.tools
            only_profile = profile.tools - declared
            if only_identity or only_profile:
                diff[role] = (only_identity, only_profile)
    assert diff == TOOL_DIFF_BASELINE


# ---- 体检机 --------------------------------------------------------------

def test_findings_match_known_baseline() -> None:
    """不多不少：多一条说明有人引入了新漂移，少一条说明有人修了但没更新基线。"""
    actual = _pairs(check_consistency())
    assert actual - BASELINE == frozenset(), "出现了基线之外的新漂移"
    assert BASELINE - actual == frozenset(), "基线里的漂移消失了，请连同 BACKLOG 一起更新"


def test_findings_group_counts() -> None:
    """按 kind 的计数，对应 docs/BACKLOG.md task-T58 的甲/乙/丙/丁四组。"""
    counts: dict[str, int] = {}
    for finding in check_consistency():
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    assert counts == {
        "tool-not-declared": 2,          # 甲：coding / testing 的 sandbox
        "depends-tool-missing": 2,       # 丙：code.repo-patch / test.verify（与甲同源）
        "owner-role-unknown": 1,         # 乙：ap.compensate -> ap_compensation
        "skill-unowned": 5,              # 乙：五个 skill 没有任何角色持有（T116 +1）
        "owner-roles-mismatch": 3,       # 乙：自述与实际持有者不等
        # 丁 depends-tool-not-allowed 当前 0 条，所以不出现在这张表里
    }


def test_no_finding_is_of_unknown_kind() -> None:
    """kind 与 severity 一律出自 KINDS —— 不许有临时拍脑袋的类型。"""
    for finding in check_consistency():
        assert finding.kind in KINDS
        assert finding.severity == KINDS[finding.kind]
        assert finding.detail.strip(), "finding 必须说清是什么问题"


def test_findings_are_stable_and_sorted() -> None:
    """两次调用结果一致且有序 —— 报告要能逐条 diff，不能每跑一次换个顺序。"""
    first = check_consistency()
    second = check_consistency()
    assert [dataclasses.astuple(f) for f in first] == [dataclasses.astuple(f) for f in second]
    order = list(KINDS)
    keys = [(order.index(f.kind), f.subject) for f in first]
    assert keys == sorted(keys)


# ---- 事实源采集本身 ------------------------------------------------------

def test_fact_sources_are_non_empty() -> None:
    """三个事实源都得真取到东西。

    体检机最坏的失效方式不是报错，是**什么都没取到然后报告一切正常** ——
    skill 注册表在 builtin 发现之前是空的，那时任何比对都恒等于「没问题」。
    """
    ids = identities()
    assert len(ids) >= len(AGENT_POOL), "identity 扫描漏了在池角色"
    assert set(AGENT_POOL) <= set(ids)
    assert "manager" in ids, "未注册但有 identity 的角色也要被扫到"
    assert len(skill_contracts()) >= 30
    assert len(tool_names()) >= 11
    assert "sandbox" not in tool_names(), "全仓没有叫 sandbox 的 ToolPort —— 有了就该更新基线"
