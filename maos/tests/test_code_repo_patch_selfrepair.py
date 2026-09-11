"""T125 —— 补丁预检自修复，与 ``MAOS_FORCE_SCRIPTED`` 的口径机器化。

两件事在一个文件里，因为它们治的是同一个病：**这台演示机的 ``~/.bash_profile``
export 了 ``MAOS_LLM_*``**，于是

* 「证据束走 ScriptedModelClient」这条口径全靠人记得加 ``env -u`` 前缀
  （``scripts/demo_preflight.sh`` 声称零出网，实际每一步都在打真模型）；
* 真模型产的 diff 常常打不上，而 ``code.repo-patch`` 交出去之前**没有任何人校验过
  它是不是合法补丁** —— 第一次碰 git 是 ``flows/common.py:255`` 直接真打，
  报出来的是 ``corrupt patch`` / ``No valid patches in input``，然后三次 attempt
  拿同一份提示词重跑三遍。

前者由 ``MAOS_FORCE_SCRIPTED`` 变成机器缺省，后者由一次 ``git apply --check`` 干跑
加最多 ``identity.max_self_repair`` 次重问来治。

**本文件最该被读到的两条判据**（改坏了别的地方不一定红）：

1. ``test_repair_gives_up_but_still_hands_the_patch_over`` —— 次数用尽**不判死**。
   预检只增加机会，判定权仍在 Gate。
2. ``test_scripted_demo_patches_come_out_byte_identical`` —— Scripted 路径的输出
   逐字节不变，连 ``self_repair_rounds`` 这个键都不该多出来。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import sys

import pytest

from maos.agents.coding import CodingAgent
from maos.flows.common import BAD_PATCH, GOOD_PATCH
from maos.core.store import SqliteStore
from maos.model.client import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_FORCE_SCRIPTED,
    ENV_MODEL,
    GatewayModelClient,
    ModelResponse,
    ScriptedModelClient,
    forced_scripted,
    select_model_client,
)
from maos.skills.builtin.code_repo_patch import (
    CALL_SITE,
    SYSTEM,
    CodeRepoPatchSkill,
    ProtectedPathViolation,
)
from maos.skills.contract import SkillContext
from maos.tests import conftest

ROOT = pathlib.Path(__file__).resolve().parents[2]

TRACE = "trace-t125"
PLAN = "plan-t125"

#: 没有任何 hunk 的空壳 diff。git 对它的原话是
#: ``error: No valid patches in input (allow with "--allow-empty")``，stage=validate。
#: 真模型实测产出的正是这一类（见模块 docstring）。
BROKEN_DIFF = "--- a\n+++ b\n"

#: hunk 头声明了 5 行原文却只给了 1 行上下文 —— git 报 ``corrupt patch at line 6``，
#: 同样落在 stage=validate。**这就是 BACKLOG:2290 记的那个症状本身**，
#: 也是提示词里「不许省略上下文行」那一句要防的。
CORRUPT_DIFF = (
    "--- a/auth/session.py\n"
    "+++ b/auth/session.py\n"
    "@@ -1,5 +1,6 @@\n"
    " import time\n"
    "+ONE = 1\n"
)

#: 形状完全合法、行号却是猜的（靶场里没有第 999 行）—— git 报
#: ``patch does not apply``，落在 **stage=apply**。三种坏法覆盖两个可修复 stage：
#: 「压根不是补丁」「像补丁但自相矛盾」「像补丁但对不上文件」，
#: 最后这个正是提示词里「行号与上下文必须与文件真实内容对得上」那一句要防的。
MISMATCHED_DIFF = (
    "--- a/auth/session.py\n"
    "+++ b/auth/session.py\n"
    "@@ -999,3 +999,4 @@ def whatever():\n"
    "     ctx = 1\n"
    "     ctx = 2\n"
    "+    ctx = 3\n"
    "     ctx = 4\n"
)


def _patch_json(diff: str, *, path: str = "auth/session.py", summary: str = "s") -> str:
    return json.dumps(
        {"files": [{"path": path, "diff": diff}], "summary": summary,
         "self_check": {"build": "pass", "lint": "pass"}},
        ensure_ascii=False,
    )


class _SeqModel:
    """按调用次序依次吐出预置响应，并记下每次收到的 user。

    比 ``ScriptedModelClient`` 多的就是「第几次」这个维度 —— 自修复要验的恰恰是
    「第二次问的时候提示词变了没有、答案换了没有」，而按关键字匹配答不了这个问题。
    """

    model = "fake-seq"

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.users: list[str] = []
        self.systems: list[str] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.users.append(user)
        self.systems.append(system)
        # 问的次数超过预置响应数时，重复最后一个 —— 「模型怎么问都改不对」
        # 那一支要靠它。
        text = self.responses[min(len(self.users) - 1, len(self.responses) - 1)]
        return ModelResponse(text=text, tokens_in=len(user) // 4,
                             tokens_out=len(text) // 4, model=self.model)


def _identity(rounds: int):
    """真 ``CodingAgent.identity``，只把自修复上限换掉（frozen dataclass）。"""
    return dataclasses.replace(CodingAgent.identity, max_self_repair=rounds)


def _run(model, *, rounds: int = 2, store=None, payload: dict | None = None):
    """直接驱动 skill，不经 Agent / invoker —— 判定就在 skill 里。

    口径同 ``test_contracts.py::_run_patch_skill``：经 invoker 只能断言「失败了」，
    断言不到是哪条路径、因为什么失败。
    """
    return CodeRepoPatchSkill().run(
        payload or {"title": "修一个时区 bug", "inputs": {}, "acceptance": ["build 通过"]},
        SkillContext(model=model, store=store, identity=_identity(rounds),
                     extras={"trace_id": TRACE, "plan_id": PLAN}),
    )


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    s = SqliteStore(str(tmp_path / "t125.db"))
    s.init_schema()
    return s


# ===========================================================================
# 1. 预检本身：过了不重问，没过才重问
# ===========================================================================
def test_a_valid_patch_is_asked_once_and_carries_no_repair_marker():
    """预检一次过 -> 模型只被问一次，输出里不该多出任何键。

    这条同时钉住「零重问时不加 ``self_repair_rounds``」—— 加了的话，
    ``SkillInvoked.detail`` 的 ``output_hash`` 会在每一次正常产出上都变，
    而那是证据束里用来对账的那个值。
    """
    model = _SeqModel(GOOD_PATCH)
    out = _run(model)

    assert len(model.users) == 1, "预检过了还重问，等于白花一次模型调用"
    assert "self_repair_rounds" not in out
    assert out["files"][0]["path"] == "auth/session.py"


def test_a_broken_diff_is_repaired_inside_one_invoke():
    """第一版打不上、第二版好了 -> 一次 invoke 之内收口，记 1 轮自修复。

    **「一次 invoke 之内」是判据的全部重点**：``max_retries=0`` 一个字没动，
    attempt 计数不许因为自修复而失真（见 contract 里那段注释）。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF), GOOD_PATCH)
    out = _run(model)

    assert len(model.users) == 2
    assert out["self_repair_rounds"] == 1
    assert out["files"][0]["diff"] == json.loads(GOOD_PATCH)["files"][0]["diff"]


@pytest.mark.parametrize("bad, stage, phrase", [
    (CORRUPT_DIFF, "validate", "corrupt patch"),
    (MISMATCHED_DIFF, "apply", "patch does not apply"),
], ids=["corrupt-hunk", "wrong-line-numbers"])
def test_both_repairable_stages_actually_get_repaired(bad, stage, phrase):
    """两个可修复 stage 各走一遍 —— ``validate`` 与 ``apply`` 都要真的触发重问。

    只测一个的话，另一个从 ``_REPAIRABLE_STAGES`` 里掉出去也没人会红：
    症状是「某一类坏补丁悄悄不再自修复」，而它与「模型这次恰好没修好」
    在日志里长得一模一样。
    """
    model = _SeqModel(_patch_json(bad), GOOD_PATCH)
    out = _run(model)

    assert len(model.users) == 2
    assert out["self_repair_rounds"] == 1
    assert f"stage={stage}" in model.users[1]
    assert phrase in model.users[1], f"git 的原话没进重问提示词：{model.users[1][-300:]}"


def test_repair_never_exceeds_max_self_repair():
    """怎么问都改不对时，重问次数恰好等于 ``identity.max_self_repair``。

    上限来自**岗位**而不是调用点：改的人该去改那个 Agent 的 identity。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF))
    out = _run(model, rounds=2)

    assert len(model.users) == 3, "1 次首问 + 2 次重问，多一次就是预算没守住"
    assert out["self_repair_rounds"] == 2


def test_repair_gives_up_but_still_hands_the_patch_over():
    """**次数用尽不判死** —— 补丁照旧交出去，由 Gate 真打一次再判。

    判「补丁打不上」是 Gate 的活：``flows/common.py:255-264`` 真打、拿 tool_error
    包成 test_report、Gate 据此出 blocker finding、``AWAITING_REVIEW -> REWORK``。
    skill 在这里抛，这条链一步都走不到，证据里从此没有那份带
    ``sandbox_mode=not-run`` 的报告，只剩一个没有上下文的 skill failed。

    自修复要买的是「多两次机会」，不是「多一道闸」：原先没有预检时这份补丁本来
    就会被交出去，现在只是交出去之前先问过两遍 —— 行为只增不减。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF))
    out = _run(model, rounds=2)

    assert out["files"][0]["diff"] == BROKEN_DIFF, "用尽次数后不许改写模型的产出"
    assert out["self_check"] == {"build": "pass", "lint": "pass"}
    assert out["self_repair_rounds"] == 2, "交出去归交出去，重问过几次要留痕"


def test_zero_budget_skips_the_precheck_altogether():
    """``max_self_repair == 0`` 的岗位一次预检都不做，路径逐字节回到 T125 之前。

    预检存在的全部理由是驱动自修复；不修的话它只剩 80ms 开销和一道抢在 Gate
    前面的闸。``test_contracts.py`` 那批拿占位 diff 判 self_check 收敛的用例
    （``SkillContext(model=...)``，identity 是 None）走的正是这一支。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF))
    out = _run(model, rounds=0)

    assert len(model.users) == 1
    assert "self_repair_rounds" not in out
    assert out["files"][0]["diff"] == BROKEN_DIFF


def test_identity_none_behaves_like_zero_budget():
    """``ctx.identity`` 压根没有时按 0 算，不许在 getattr 上炸。"""
    model = _SeqModel(_patch_json(BROKEN_DIFF))
    out = CodeRepoPatchSkill().run(
        {"title": "t", "inputs": {}, "acceptance": []},
        SkillContext(model=model))

    assert len(model.users) == 1
    assert "self_repair_rounds" not in out


# ===========================================================================
# 2. 空补丁集：T114 立的那条合法结论不许被预检重新判死
# ===========================================================================
def test_an_empty_patch_set_with_summary_survives_the_precheck():
    """「查过了，没有要改的」是合法结论，预检不许把它判成失败。

    ``sandbox_git_apply`` 对空 files 在碰 workdir **之前**就早返回 ok
    （sandbox.py:450-457），判据与 skill 这一侧同源（docs/BACKLOG.md:2293）。
    """
    empty = json.dumps({"files": [], "summary": "查过了，时区处理本来就是对的",
                        "self_check": {"build": "pass", "lint": "pass"}},
                       ensure_ascii=False)
    model = _SeqModel(empty)
    out = _run(model, rounds=2)

    assert len(model.users) == 1, "空补丁集被预检判死了，T114 那条结论白立"
    assert out["files"] == []
    assert "self_repair_rounds" not in out


def test_an_empty_patch_set_without_summary_is_still_an_error():
    """空补丁集 + 空 summary 仍是错误 —— 分不清结论与「模型没产出」。

    这条判据本身没变，放在这里是因为预检很容易顺手把它一起改掉。
    """
    model = _SeqModel(json.dumps({"files": [], "summary": "   "}))
    with pytest.raises(ValueError, match="没有 summary"):
        _run(model, rounds=2)


def test_an_empty_patch_set_costs_no_sandbox_workdir(monkeypatch):
    """空补丁集不该为了预检去造一份靶场副本（~80ms）。"""
    import maos.skills.builtin.code_repo_patch as mod

    calls = []
    monkeypatch.setattr(mod, "prepare_sandbox_workdir",
                        lambda *a, **kw: calls.append(1) or "/nonexistent")
    _run(_SeqModel(json.dumps({"files": [], "summary": "没有要改的"})), rounds=2)

    assert calls == [], "空补丁集造了工作目录，白付一次 copytree"


# ===========================================================================
# 3. 重问的提示词：递回去的必须是 git 自己的原话
# ===========================================================================
def test_the_repair_prompt_carries_gits_own_words():
    """转译成一句「格式不对」会丢掉 stage / path / hunk —— 而那正是模型修得动的信息。"""
    model = _SeqModel(_patch_json(CORRUPT_DIFF), GOOD_PATCH)
    _run(model)

    second = model.users[1]
    assert "【自修复 1/2】" in second
    assert "stage=validate" in second
    assert "corrupt patch" in second, f"git 的原话没进重问提示词：{second[-400:]}"
    assert model.users[0] in second, "重问要带上原任务，不能只发一句错误"
    assert "完整的 JSON" in second, "要完整重产，不是让模型给增量"


def test_the_system_prompt_spells_out_the_unified_diff_contract():
    """三句硬约束（文件头 / hunk 头行号真实 / 不许省略上下文）必须在 SYSTEM 里。

    ``code_repo_patch.py`` 的 SYSTEM 原先对 diff 格式**一个字都没写**，于是模型
    产出什么形状全凭运气 —— 自修复只是第二道网，提示词才是第一道。
    """
    for must in ("--- a/", "+++ b/", "@@ -", "unified diff", "换行", "省略"):
        assert must in SYSTEM, f"SYSTEM 里没有「{must}」这条约束"


# ===========================================================================
# 4. 记账：usage 行数 = 1 + 重问次数
# ===========================================================================
def test_usage_rows_equal_one_plus_the_repair_rounds(store):
    """每一轮重问都记一行 ``model_usage``，且都落在**同一个** call_site 上。

    不新登记 call_site 是刻意的：重问不是一个新的调用点，是同一个调用点问了几次。
    于是「这次补丁烧了多少 token」仍是把这个 call_site 的行加起来（成本视图一行
    不用改），而轮数由行数 - 1 数出来 —— 与产物里的 ``self_repair_rounds`` 互为对账。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF), _patch_json(MISMATCHED_DIFF), GOOD_PATCH)
    out = _run(model, rounds=2, store=store)

    rows = [r for r in store.list_model_usage(trace_id=TRACE)
            if r["call_site"] == CALL_SITE]
    assert out["self_repair_rounds"] == 2
    assert len(rows) == 1 + out["self_repair_rounds"] == 3
    assert {r["plan_id"] for r in rows} == {PLAN}


def test_a_one_shot_patch_records_exactly_one_usage_row(store):
    """零重问时仍恰好一行 —— 别让预检顺手多记一笔。"""
    _run(_SeqModel(GOOD_PATCH), store=store)

    rows = [r for r in store.list_model_usage(trace_id=TRACE)
            if r["call_site"] == CALL_SITE]
    assert len(rows) == 1


# ===========================================================================
# 5. 安全：预检逮住的绕过口不重问，直接终止
# ===========================================================================
def test_a_path_escape_caught_by_the_precheck_is_a_security_event():
    """diff 正文指向 workdir 之外 -> ProtectedPathViolation，不重问、不降级。

    声明的 ``path`` 干干净净、正文却写别处，是 ``_reject_protected_paths``
    （只看声明）漏得掉而 sandbox 看得见的那一类。重问等于给绕过三次机会。
    """
    escaping = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
    )
    model = _SeqModel(_patch_json(escaping, path="auth/session.py"))
    with pytest.raises(ProtectedPathViolation):
        _run(model, rounds=2)

    assert len(model.users) == 1, "安全事件被重问了 —— 那是给绕过更多次机会"


def test_the_precheck_workdir_is_always_cleaned_up(monkeypatch):
    """预检现造的靶场副本用完必须删，失败路径上也一样。"""
    import maos.skills.builtin.code_repo_patch as mod

    made: list[str] = []
    real = mod.prepare_sandbox_workdir

    def _spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(mod, "prepare_sandbox_workdir", _spy)
    _run(_SeqModel(_patch_json(BROKEN_DIFF)), rounds=2)

    assert made, "有补丁却没造工作目录，预检根本没跑"
    assert [p for p in made if os.path.exists(p)] == [], "预检的临时目录没删干净"


# ===========================================================================
# 6. Scripted 路径逐字节不变
# ===========================================================================
@pytest.mark.parametrize("raw", [GOOD_PATCH, BAD_PATCH], ids=["good", "bad"])
def test_scripted_demo_patches_come_out_byte_identical(raw):
    """场景 1/2 的两份演示补丁：预检必过、一次不重问、输出不多一个键。

    ``GOOD_PATCH`` / ``BAD_PATCH`` 由 ``flows/common.py`` 在导入时用真 ``git diff``
    现造，所以它们**天然**打得上 —— ``BAD_PATCH`` 坏的是「修得不对」，不是
    「打不上」，那正是场景 2 返工链要的样本。预检若把它判死，返工链会从
    「回归挂了」变成「补丁没落进沙箱」，那是另一条完全不同的剧情。
    """
    model = _SeqModel(raw)
    out = _run(model, rounds=2)

    assert len(model.users) == 1
    assert "self_repair_rounds" not in out
    assert out == json.loads(raw)


# ===========================================================================
# 7. MAOS_FORCE_SCRIPTED 的优先级与不设的那两处
# ===========================================================================
def _with_live_env(monkeypatch):
    for name, value in ((ENV_BASE_URL, "https://gw.example/v1"),
                        (ENV_API_KEY, "sk-not-a-real-key-000"),
                        (ENV_MODEL, "deepseek-chat")):
        monkeypatch.setenv(name, value)


def test_force_scripted_outranks_a_fully_configured_gateway(monkeypatch):
    """三个 ``MAOS_LLM_*`` 都配齐了，``MAOS_FORCE_SCRIPTED=1`` 仍然一律 Scripted。

    这就是本轨要买的东西：演示机上 export 了 key 也不会把证据束悄悄变成真模型产的。
    """
    _with_live_env(monkeypatch)
    monkeypatch.setenv(ENV_FORCE_SCRIPTED, "1")

    assert forced_scripted() is True
    assert isinstance(select_model_client(), ScriptedModelClient)


def test_the_room_path_still_gets_a_real_gateway(monkeypatch):
    """**不设**该变量时行为一个字节不变 —— 房间入口与 ``--live-model`` 靠的就是这条。

    ``hiclaw/room_ingress.py`` 与 ``scripts/make_case_bundle.py --live-model``
    刻意不设它（契约 §G）。本轨不碰那两处，这条正面证明它们没被影响。
    """
    _with_live_env(monkeypatch)
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)

    assert forced_scripted() is False
    assert isinstance(select_model_client(), GatewayModelClient)


@pytest.mark.parametrize("off", ["0", "false", "no", "off", "", "  "])
def test_the_switch_defaults_to_off_and_honours_the_off_values(monkeypatch, off):
    """缺省是**关**，与 kb 那几个旋钮相反 —— 默认开的话真模型就永远打不通了。"""
    _with_live_env(monkeypatch)
    monkeypatch.setenv(ENV_FORCE_SCRIPTED, off)

    assert forced_scripted() is False
    assert isinstance(select_model_client(), GatewayModelClient)


def test_an_explicit_force_scripted_argument_still_wins(monkeypatch):
    """``force_scripted=True`` 这条老路原样还在（A-12 冻结的签名语义）。"""
    _with_live_env(monkeypatch)
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)

    assert isinstance(select_model_client(force_scripted=True), ScriptedModelClient)


# ===========================================================================
# 8. 那个变量名在四个地方拼得一模一样
# ===========================================================================
def test_the_env_name_is_spelled_identically_in_all_four_places():
    """``run.py`` / ``conftest.py`` / ``make_evidence.py`` 各自硬编码了这个名字。

    硬编码是三处各自的取舍（薄入口不多拉一层 import；conftest 在 collection 期
    执行，不该把 ``maos.model`` 整条链先拉起来；``scripts/`` 不是包）。代价是
    要人工同步 —— 拼错一个字母的症状是「那一处悄悄不生效了」，屏幕上没有任何提示。
    这条断言就是那个代价的对冲。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_t125_make_evidence", ROOT / "scripts" / "make_evidence.py")
    make_evidence = importlib.util.module_from_spec(spec)
    sys.modules["_t125_make_evidence"] = make_evidence
    spec.loader.exec_module(make_evidence)

    run_py = (ROOT / "run.py").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "demo_preflight.sh").read_text(encoding="utf-8")

    assert conftest.FORCE_SCRIPTED_ENV == ENV_FORCE_SCRIPTED
    assert make_evidence.FORCE_SCRIPTED_ENV == ENV_FORCE_SCRIPTED
    assert f'FORCE_SCRIPTED_ENV = "{ENV_FORCE_SCRIPTED}"' in run_py
    assert f"export {ENV_FORCE_SCRIPTED}=1" in preflight


def test_run_py_defaults_to_scripted_and_live_model_opts_out():
    """``run.py`` 缺省替人设上，``--live-model`` 摘掉旗标且不设。

    直接跑 ``_apply_model_mode``，不去起一整个场景：要验的是这两行分岔本身。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_t125_run", ROOT / "run.py")
    run_mod = importlib.util.module_from_spec(spec)
    sys.modules["_t125_run"] = run_mod
    spec.loader.exec_module(run_mod)

    os.environ.pop(ENV_FORCE_SCRIPTED, None)
    try:
        assert run_mod._apply_model_mode(["--scenario", "1"]) == ["--scenario", "1"]
        assert os.environ[ENV_FORCE_SCRIPTED] == "1"

        os.environ.pop(ENV_FORCE_SCRIPTED, None)
        assert run_mod._apply_model_mode(["--live-model", "--scenario", "1"]) \
            == ["--scenario", "1"], "--live-model 必须被摘掉，maos/main.py 不认识它"
        assert ENV_FORCE_SCRIPTED not in os.environ
    finally:
        os.environ[ENV_FORCE_SCRIPTED] = "1"        # 还给 conftest 的起跑线


def test_run_py_does_not_override_an_explicit_choice():
    """人显式 ``MAOS_FORCE_SCRIPTED=0`` 时 ``run.py`` 不许把它按回去。

    ``setdefault`` 而不是赋值 —— 否则这就是第二个「说了不算」的开关。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_t125_run2", ROOT / "run.py")
    run_mod = importlib.util.module_from_spec(spec)
    sys.modules["_t125_run2"] = run_mod
    spec.loader.exec_module(run_mod)

    os.environ[ENV_FORCE_SCRIPTED] = "0"
    try:
        run_mod._apply_model_mode([])
        assert os.environ[ENV_FORCE_SCRIPTED] == "0"
    finally:
        os.environ[ENV_FORCE_SCRIPTED] = "1"


def test_the_two_places_that_must_not_set_it_really_do_not():
    """契约 §G 点名**不设**该变量的两处，静态核对。

    房间入口（``hiclaw/room_ingress.py``）与 ``make_case_bundle.py --live-model``
    要的就是真模型 —— 本轨一个字节都没碰它们，这条从源码正面确认没有被顺手加上。
    行为侧由 ``test_the_room_path_still_gets_a_real_gateway`` 从另一头钉住。
    """
    for relpath in ("hiclaw/room_ingress.py", "scripts/make_case_bundle.py"):
        text = (ROOT / relpath).read_text(encoding="utf-8")
        assert ENV_FORCE_SCRIPTED not in text, (
            f"{relpath} 设了 {ENV_FORCE_SCRIPTED}，真模型那条路被堵上了（契约 §G）")


def test_ap_smoke_was_already_setting_it_and_now_it_finally_works():
    """``scripts/ap_smoke.py`` **在 T125 之前就设了这个变量**，而当时没人读。

    那一行（``env["MAOS_FORCE_SCRIPTED"] = "1"``，紧跟在剥掉 ``*API_KEY`` /
    ``*BASE_URL`` / ``*_TOKEN`` 之后）把「这个子进程要确定性」写得明明白白，
    只是 ``select_model_client()`` 从来没读过它 —— 与 ``AgentIdentity.max_self_repair``
    是同一种东西：**语义早就写下了，只是没有读取点**。所以本轨用的不是一个新发明的
    名字，是把仓库里已有的那个意图接上电。

    这条测试的用处是：谁要是把 ``ENV_FORCE_SCRIPTED`` 改名，``ap_smoke.py`` 会
    **静默**退回「设了没人读」的老样子 —— 它剥 key 那几行仍在，所以它照样不打网络，
    症状只有「ap_smoke 的子进程在有 key 时行为变了」，而没有任何红灯。
    """
    text = (ROOT / "scripts" / "ap_smoke.py").read_text(encoding="utf-8")
    assert f'env["{ENV_FORCE_SCRIPTED}"] = "1"' in text, (
        f"ap_smoke.py 不再设 {ENV_FORCE_SCRIPTED} 了 —— 要么它改了写法，"
        f"要么这个变量被改名了，两种都要人看一眼")


def test_make_evidence_labels_the_bundle_with_the_model_mode(monkeypatch):
    """``INDEX.json`` 的 ``model_mode`` 与跑的时候读的是同一个变量，不会分叉。

    字面值与 ``scripts/make_case_bundle.py`` 逐字一致（它先立的口径）——
    值不一样的话，「哪些束是真模型产的」跨束筛一次就漏。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_t125_make_evidence2", ROOT / "scripts" / "make_evidence.py")
    make_evidence = importlib.util.module_from_spec(spec)
    sys.modules["_t125_make_evidence2"] = make_evidence
    spec.loader.exec_module(make_evidence)

    monkeypatch.setenv(ENV_FORCE_SCRIPTED, "1")
    assert make_evidence.model_mode() == "scripted"
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)
    assert make_evidence.model_mode() == "live"

    case_bundle = (ROOT / "scripts" / "make_case_bundle.py").read_text(encoding="utf-8")
    assert '"live" if live else "scripted"' in case_bundle, (
        "make_case_bundle.py 的 model_mode 字面值变了，两边口径已分叉")
