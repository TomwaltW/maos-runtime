"""调用面的两条守卫：actor 锚点三处同值、同名同版本覆盖必告警（T79）。

**这一份是回归守卫，不是新功能的验收。**

锚点那半边：`contract.py` 的 `SkillResult` docstring 承诺「调用方拿到的 SkillResult
与落库那行由它对上号，缺了就断链」。这条承诺在 `1ac85b3` 之后成立 —— invoker 把
`invocation_id` 塞进 `SkillContext.extras`，于是 skill 写业务表用的 actor 锚点、
`SkillResult`、落库那行的 `SkillInvoked.detail` 是同一个值。成立之后一直没有单测
钉住它，本文件补上：`test_skill_sees_...` 与 `test_logged_row_anchors_...` 两条
分别守 skill 侧与落库侧，缺一条都只能证明「断链换了个位置」。

刻意钉死的一条反直觉行为：**invoker 故意覆盖调用方传进来的 `extras["invocation_id"]`**
（`test_caller_supplied_..._is_overridden` / `test_two_invocations_sharing_one_extras_dict_...`）。
放宽成 `setdefault` 看着更客气，实测会当场打红 `scripts/verify.py` 第 1 项
hash-integrity —— 调用方**允许**把同一个 extras dict 复用给相邻两次 invoke
（`agents/refund/payment_agent.py` 的 execute → observe 就是这么写的），
setdefault 会让这两次共用一个 id，而「一次调用一个新 id」是那一项的硬判据。

覆盖告警那半边：`registry.py` 的模块开篇承诺「按名 + 版本取则拿到当年那一个」。
同版本被后 import 的悄悄换掉，这条承诺就不成立，而以前一声不吭。
告警是 warning 不是 raise —— skill 是 import 注册的，raise 会让一次误 import
掀掉整个进程启动。
"""

from __future__ import annotations

import contextlib
import logging
import types

import pytest

from maos.core.store import SqliteStore
from maos.skills import registry
from maos.skills.contract import Skill, SkillContract
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY, register_skill

PROBE = "t79.anchor-probe"

#: 四个业务域的兜底件都长同一个样，第 6 条测试挨个只读地验一遍。
DOMAIN_COMMONS = ("refund", "ap", "claim", "investigation")


class AnchorProbeSkill(Skill):
    """只把 ctx 里看到的 actor 锚点原样回填 —— 本文件全靠它把 skill 侧看到的值取出来。"""

    contract = SkillContract(
        name=PROBE, version="1.0.0",
        purpose="T79 回归守卫：回填 ctx.extras 里的 invocation_id",
    )

    def run(self, payload: dict, ctx) -> dict:
        extras = getattr(ctx, "extras", None) or {}
        return {"seen_invocation_id": extras.get("invocation_id")}


def _identity():
    """够 invoker 用的最小 identity：它只 getattr 这两样。"""
    return types.SimpleNamespace(agent_id="t79-probe", allowed_skills=frozenset({PROBE}))


@contextlib.contextmanager
def _registered(*classes: type[Skill]):
    """注册后**必须**摘干净：`gen_docs.py` 是从 SKILL_REGISTRY 现场渲染
    `docs/skill-catalog.md` 的，探针留在表里会把 test_generated_docs 打红。"""
    try:
        for cls in classes:
            register_skill(cls)
        yield
    finally:
        for cls in classes:
            versions = SKILL_REGISTRY.get(cls.contract.name)
            if versions is not None:
                versions.pop(cls.contract.version, None)
                if not versions:
                    SKILL_REGISTRY.pop(cls.contract.name, None)


def _store() -> SqliteStore:
    store = SqliteStore()
    store.init_schema()
    return store


# --- 锚点：三处同值 ---------------------------------------------------------
def test_skill_sees_the_same_invocation_id_as_the_result():
    """skill 在 `ctx.extras` 里拿到的 id == 调用方拿到的 `SkillResult.invocation_id`。

    这是整条 actor 溯源链的第一节：拿不到就只能自己另生成一个，
    于是业务表里的 actor 锚点与本次调用再也对不上号。
    """
    with _registered(AnchorProbeSkill):
        res = SkillInvoker(_identity(), _store()).invoke(PROBE, {})

    assert res.status == "ok"
    assert res.invocation_id, "invocation_id 恒非空 —— guard._require_invocation_id 空了就抛"
    assert res.output["seen_invocation_id"] == res.invocation_id, (
        "skill 侧与 SkillResult 不是同一个 id：actor 锚点断链"
    )


def test_logged_row_anchors_the_same_invocation_id_the_skill_saw():
    """落库那行 `SkillInvoked.detail` 里的 id 也是同一个。

    只做上一条等于把断链从 skill 侧移到了审计侧：`scripts/verify.py` 第 3 项
    authoritative-fact 按事件对账，两侧不同值就判「权威事实边界被绕过」。
    """
    store = _store()
    with _registered(AnchorProbeSkill):
        res = SkillInvoker(_identity(), store).invoke(
            PROBE, {}, extras={"plan_id": "plan-t79", "task_id": "task-t79"})

    rows = store.list_event_log("plan-t79")
    assert len(rows) == 1 and rows[0]["event_type"] == "SkillInvoked"
    logged = rows[0]["detail"]["invocation_id"]
    assert logged == res.invocation_id == res.output["seen_invocation_id"], (
        f"三处必须同值：落库 {logged} / SkillResult {res.invocation_id} / "
        f"skill 侧 {res.output['seen_invocation_id']}"
    )


def test_caller_supplied_invocation_id_is_overridden_so_three_sides_stay_one_value():
    """调用方传了同名键也以 invoker 那个为准 —— **故意覆盖，不是漏了 setdefault**。

    官方 id 只有 invoker 生成的那一个：它才是落进 `SkillInvoked` 事件的那个。
    采信调用方那个会让「事件里的 id」与「SkillResult 的 id」分叉，
    要留住调用方自己的标识请另起键名。
    """
    store = _store()
    caller_id = "caller-supplied-0001"
    with _registered(AnchorProbeSkill):
        res = SkillInvoker(_identity(), store).invoke(
            PROBE, {}, extras={"plan_id": "plan-t79b", "invocation_id": caller_id})

    seen = res.output["seen_invocation_id"]
    logged = store.list_event_log("plan-t79b")[0]["detail"]["invocation_id"]
    assert seen != caller_id, "调用方传入的 id 覆盖不掉官方 id（见本条 docstring）"
    assert seen == res.invocation_id == logged, "覆盖之后三处仍必须是同一个值"


def test_two_invocations_sharing_one_extras_dict_do_not_collide():
    """同一个 extras dict 复用给两次 invoke，两次的 id 必须不同。

    这是把上一条的「覆盖」放宽成 `setdefault` 会踩的雷：调用方**允许**复用
    extras（`agents/refund/payment_agent.py` 的 execute → observe），一旦 setdefault
    生效，第二次会捡起第一次留下的 id，两次调用共用一个锚点 ——
    `scripts/verify.py` 第 1 项 hash-integrity 直接红（实测 scenario-6/7/R5 共 6 处）。
    """
    store = _store()
    shared_extras = {"plan_id": "plan-t79c", "task_id": "task-t79c"}
    with _registered(AnchorProbeSkill):
        invoker = SkillInvoker(_identity(), store)
        first = invoker.invoke(PROBE, {"n": 1}, extras=shared_extras)
        second = invoker.invoke(PROBE, {"n": 2}, extras=shared_extras)

    assert first.invocation_id != second.invocation_id, "一次 invoke 一个新 id，不许两次共用"
    logged = [r["detail"]["invocation_id"] for r in store.list_event_log("plan-t79c")]
    assert logged == [first.invocation_id, second.invocation_id]
    assert first.output["seen_invocation_id"] == first.invocation_id
    assert second.output["seen_invocation_id"] == second.invocation_id
    assert "invocation_id" not in shared_extras, "调用方那份 extras 不许被 invoker 就地改写"


def test_failed_invocation_still_anchors_a_logged_row():
    """未注册这条早退路径也带锚点 —— 失败调用同样是要被追溯的事实。"""
    store = _store()
    identity = types.SimpleNamespace(
        agent_id="t79-probe", allowed_skills=frozenset({"t79.never-implemented"}))
    res = SkillInvoker(identity, store).invoke(
        "t79.never-implemented", {}, extras={"plan_id": "plan-t79d"})

    assert res.status == "failed" and res.error == "skill_not_found:t79.never-implemented"
    assert res.invocation_id, "早退路径也必须生成 id"
    assert store.list_event_log("plan-t79d")[0]["detail"]["invocation_id"] == res.invocation_id


# --- 注册表：同名同版本覆盖 -------------------------------------------------
def _twin(suffix: str, version: str = "1.0.0") -> type[Skill]:
    """造一个与 AnchorProbeSkill 同名（可指定版本）的另一个类。"""
    return type(f"AnchorProbeTwin{suffix}", (AnchorProbeSkill,),
                {"contract": SkillContract(name=PROBE, version=version,
                                           purpose=f"T79 双胞胎 {suffix}")})


def test_duplicate_version_warns_but_does_not_raise(caplog):
    """同名同版本注册两次：告警、**不抛**、后者生效，且告警串定位得动。

    只说「重复注册」的告警没人查得动，所以断言里逐样点名：skill 名、版本、
    被顶掉的类的模块名与新类的模块名。
    """
    twin = _twin("A")
    with caplog.at_level(logging.WARNING, logger="maos.skills"):
        with _registered(AnchorProbeSkill, twin):
            assert registry.get(PROBE, "1.0.0") is twin, "后 import 的赢（行为不变，只是不再静默）"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"应恰好一条告警，实际 {len(warnings)}"
    msg = warnings[0].getMessage()
    for needle in (PROBE, "1.0.0", AnchorProbeSkill.__module__,
                   AnchorProbeSkill.__qualname__, twin.__qualname__):
        assert needle in msg, f"告警串里缺 {needle!r}，定位不动：{msg}"


def test_distinct_versions_register_without_warning(caplog):
    """不同版本共存是设计意图（旧 Plan 可复现），一声告警都不许有。"""
    other = _twin("B", version="1.1.0")
    with caplog.at_level(logging.WARNING, logger="maos.skills"):
        with _registered(AnchorProbeSkill, other):
            assert registry.versions(PROBE) == ["1.0.0", "1.1.0"]
            assert registry.get(PROBE) is other, "默认取最高版本"
            assert registry.get(PROBE, "1.0.0") is AnchorProbeSkill, "按版本取拿到当年那一个"

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_registering_the_same_class_twice_stays_silent(caplog):
    """同一个类再注册一次是空操作（模块被重复 import），不是撞名，不告警。"""
    with caplog.at_level(logging.WARNING, logger="maos.skills"):
        with _registered(AnchorProbeSkill, AnchorProbeSkill):
            assert registry.get(PROBE, "1.0.0") is AnchorProbeSkill

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


# --- 四个域的兜底件：只读地验一遍行为未变 -----------------------------------
@pytest.mark.parametrize("domain", DOMAIN_COMMONS)
def test_invocation_id_of_stays_unchanged_in_every_domain(domain):
    """`invocation_id_of` 的两条分支都还在：给了用给的，没给本地生成且非空。

    第二条分支**不是死代码** —— 单测直调 skill（不经 invoker）走的正是它。
    本条只 import 四个域的 `_common` 来断言，一个字都不改它们（跨轨契约 §5.2）。
    """
    module = __import__(f"maos.skills.builtin.{domain}._common", fromlist=["_common"])
    invocation_id_of = module.invocation_id_of

    given = invocation_id_of(types.SimpleNamespace(extras={"invocation_id": "given-0001"}))
    assert given == "given-0001", "调用方给了就用调用方的（合并 invoker 后走的是这条）"

    local = invocation_id_of(types.SimpleNamespace(extras={}))
    assert local and local != "given-0001", "没给就本地生成，恒非空"
    assert invocation_id_of(types.SimpleNamespace(extras={"invocation_id": "  "})), (
        "空白串按没给处理，仍然恒非空"
    )
