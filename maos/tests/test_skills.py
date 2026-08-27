"""Task-A 的机器验收 —— 五条断言，一条钉一个冻结契约点。

C-1 注册 / A-4 取版本 / 附录B 越权 / A-5 retry / A-5 落库。

这些不是「跑一遍看看」的冒烟：每条都在守一件后面会被人无意改坏的事 ——
把动态发现改回显式清单、把取版本改成字典序、把越权吞成 failed、
把重试次数写死、把审计改成往总线上加事件类型。
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from maos.agents.base import PermissionDenied
from maos.agents.coding import CodingAgent
from maos.agents.manager import ManagerAgent
from maos.contracts.events import EventType
from maos.core.store import SqliteStore
from maos.model.client import ScriptedModelClient
from maos.skills import builtin, registry
from maos.skills.builtin.code_repo_patch import CodeRepoPatchSkill
from maos.skills.builtin.req_normalize import ReqNormalizeSkill
from maos.skills.invoker import SkillInvoker
from maos.skills.registry import SKILL_REGISTRY, register_skill

BUILTIN_DIR = pathlib.Path(builtin.__file__).parent


class BoomModel(ScriptedModelClient):
    """每次 complete 都炸，并记下被调了几次 —— 重试次数靠它数出来。"""

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def complete(self, *, system: str, user: str, tier: str):
        self.n += 1
        raise RuntimeError("模型炸了")


# --- C-1 投放即注册 ---------------------------------------------------------
def test_new_builtin_skills_register_without_touching_init():
    """两个新 skill 只靠「放进 builtin/」就进注册表，__init__.py 里没有它们的名字。

    只断言 registry.get 拿得到还不够：显式 import 清单同样能让它拿到。
    所以要连 __init__.py 的正文一起断言 —— 清单一旦出现，A/B/D 三轨合并必冲突。
    """
    found = builtin.discover()

    assert {"req_normalize", "code_repo_patch"} <= set(found), f"discover() 没扫到：{found}"
    assert registry.get("req.normalize") is ReqNormalizeSkill
    assert registry.get("code.repo-patch") is CodeRepoPatchSkill

    init_src = (BUILTIN_DIR / "__init__.py").read_text(encoding="utf-8")
    for module_name in ("req_normalize", "code_repo_patch"):
        assert module_name not in init_src, (
            f"builtin/__init__.py 里出现了 {module_name}：动态发现被改回显式清单（C-1）"
        )


# --- A-4 取版本 -------------------------------------------------------------
def test_get_defaults_to_highest_version_and_honours_explicit_version():
    """默认取最高版本，按版本取拿到当年那一个。

    刻意用 1.9.0 与 1.10.0：字典序下 "1.9.0" > "1.10.0"，数值序反过来。
    谁把 _semver_key 换成字符串比较，只有这一对能把他抓住。
    """
    v19 = register_skill(type("ReqNormalizeV19", (ReqNormalizeSkill,),
                              {"contract": replace(ReqNormalizeSkill.contract, version="1.9.0")}))
    v110 = register_skill(type("ReqNormalizeV110", (ReqNormalizeSkill,),
                               {"contract": replace(ReqNormalizeSkill.contract, version="1.10.0")}))
    try:
        assert registry.versions("req.normalize") == ["1.0.0", "1.9.0", "1.10.0"]
        assert registry.get("req.normalize") is v110, "默认必须取最高版本，1.10.0 > 1.9.0"
        assert registry.get("req.normalize", "1.9.0") is v19
        assert registry.get("req.normalize", "1.0.0") is ReqNormalizeSkill
        assert registry.get("req.normalize", "9.9.9") is None, "取不到的版本返回 None，不许回落最高版"
    finally:
        for stale in ("1.9.0", "1.10.0"):
            SKILL_REGISTRY["req.normalize"].pop(stale, None)


# --- 附录B 越权 -------------------------------------------------------------
def test_invoke_outside_whitelist_raises_permission_denied():
    """白名单先于注册表生效：已注册的 skill 也照样拦。

    req.normalize 现在是真存在的（manager 的 skill），coding 调它必须抛 ——
    要是白名单校验被挪到注册表查询之后，这条就会变成一次真调用。
    """
    assert registry.get("req.normalize") is not None, "本条的前提：该 skill 确实已注册"
    assert "req.normalize" not in CodingAgent.identity.allowed_skills

    inv = SkillInvoker(CodingAgent.identity, None)
    with pytest.raises(PermissionDenied):
        inv.invoke("req.normalize", {"goal": "越权调用一次看看"})


# --- A-5 retry --------------------------------------------------------------
def test_retry_policy_runs_exactly_max_retries_plus_one():
    """failure_policy=retry 跑 max_retries+1 次；escalate 只跑 1 次。

    两个 skill 对照着断言，才能证明次数来自 contract，而不是 invoker 里写死的常数。
    """
    contract = ReqNormalizeSkill.contract
    assert contract.failure_policy == "retry" and contract.max_retries >= 1

    model = BoomModel()
    res = SkillInvoker(ManagerAgent.identity, None).invoke(
        "req.normalize", {"goal": "g"}, extras={"model": model})

    assert res.status == "failed"
    assert res.error is not None and res.error.startswith("RuntimeError")
    assert model.n == contract.max_retries + 1, f"期望调 {contract.max_retries + 1} 次，实际 {model.n}"

    escalate_model = BoomModel()
    res2 = SkillInvoker(CodingAgent.identity, None).invoke(
        "code.repo-patch", {"title": "t", "inputs": {}, "acceptance": []},
        extras={"model": escalate_model})

    assert CodeRepoPatchSkill.contract.failure_policy == "escalate"
    assert res2.status == "failed"
    assert escalate_model.n == 1, "escalate 不许重试：安全违规重试等于多试几次绕过"


# --- A-5 落库 ---------------------------------------------------------------
def test_invocation_appends_event_log_row_with_eight_detail_fields():
    """一次 invoke 落一条 event_log 行，detail 八字段齐；不许为它新增总线事件类型。"""
    store = SqliteStore()
    store.init_schema()

    res = SkillInvoker(ManagerAgent.identity, store).invoke(
        "req.normalize", {"goal": "  给 token 校验补上缺失分支  "},
        extras={"plan_id": "plan-skills", "task_id": "task-1", "trace_id": "tr-1"})

    assert res.status == "ok"
    assert res.output["normalized_goal"] == "给 token 校验补上缺失分支", "无 model 时走规则兜底"

    rows = store.list_event_log("plan-skills")
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "SkillInvoked", "落的是 event_log 行，不是总线事件"
    assert row["from_state"] == "" and row["to_state"] == ""
    assert set(row["detail"]) == {
        "skill", "version", "status", "duration_ms",
        "input_digest", "output_hash", "usage", "invocation_id",
    }
    assert row["detail"]["skill"] == "req.normalize"
    assert row["detail"]["version"] == ReqNormalizeSkill.contract.version
    assert row["detail"]["status"] == "ok"
    assert row["detail"]["usage"] is None, "没传 usage 时留 null，不许伪造用量"

    frozen_types = [v for k, v in vars(EventType).items() if not k.startswith("_")]
    assert "SkillInvoked" not in frozen_types, (
        "SkillInvoked 是 event_log 的行类型，不是总线事件类型；"
        "冻结的 contracts/events.py 不许为它加成员"
    )
