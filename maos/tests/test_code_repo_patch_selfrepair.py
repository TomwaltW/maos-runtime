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
3. ``test_a_model_blowup_in_the_repair_round_still_hands_the_previous_patch_over``
   （T131）—— 重问轮自己塌了也**不判死**。上一轮那份「合法但打不上」的补丁照旧
   交给 Gate，否则一次失败的重问会比根本不重问更坏，而自修复买的是机会不是风险。

T131 还补了第二件事：递回模型的不只是 git 那句原话，还有**上一版补丁正文**。
git 说的是 ``corrupt patch at line 6``，而那个行号是相对喂给 ``git apply`` 的
那份输入数的 —— 模型手里没有那份输入，「第 6 行」对它没有意义。
``test_the_repair_prompt_carries_the_previous_diff_under_gits_own_line_numbers``
把「两套行号是同一套」这件事钉成断言，而不是留成一句注释里的但愿。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
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

    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.users: list[str] = []
        self.systems: list[str] = []

    def complete(self, *, system: str, user: str, tier: str) -> ModelResponse:
        self.users.append(user)
        self.systems.append(system)
        # 问的次数超过预置响应数时，重复最后一个 —— 「模型怎么问都改不对」
        # 那一支要靠它。
        text = self.responses[min(len(self.users) - 1, len(self.responses) - 1)]
        # 预置项给一个异常实例 = 这一轮模型自己塌了（超时 / 网关 5xx / 连接断）。
        # 放在这里而不是另写一个假客户端：塌掉的那一轮**仍要记进 users**，
        # 「第几次问的时候塌的」正是 T131 那几条要断言的东西。
        if isinstance(text, Exception):
            raise text
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


def test_zero_budget_never_touches_the_sandbox_at_all(monkeypatch):
    """``max_self_repair == 0``：预检那条路上的两个函数**一次都不许被调到**。

    上一条只断言「模型只被问一次」，而那在「预检跑了、结果被忽略」时同样是绿的 ——
    于是「逐字节回到 T125 之前」这句话就没有判据。这条把它钉死：
    ``prepare_sandbox_workdir`` 零调用（那 80ms 的 copytree 一次都不付），
    ``sandbox_git_apply`` 零调用（一次 git 子进程都不起）。两者都是 T125 才出现在
    这条路径上的东西，零调用即等价于它们不存在。

    T131 在循环里加了一层 try/except，这条同时守住「加的那层没有把 budget==0
    那条路拐进预检」。
    """
    import maos.skills.builtin.code_repo_patch as mod

    made: list[int] = []
    applied: list[int] = []
    monkeypatch.setattr(mod, "prepare_sandbox_workdir",
                        lambda *a, **kw: made.append(1) or "/nonexistent")
    monkeypatch.setattr(mod, "sandbox_git_apply",
                        lambda *a, **kw: applied.append(1) or {"ok": True, "error": None})

    out = _run(_SeqModel(_patch_json(BROKEN_DIFF)), rounds=0)

    assert made == [], "budget=0 还造了工作目录 —— 白付一次 copytree"
    assert applied == [], "budget=0 还跑了预检 —— 那是一道 T125 之前没有的闸"
    assert out["files"][0]["diff"] == BROKEN_DIFF
    assert "self_repair_rounds" not in out


def test_zero_budget_still_propagates_a_model_blowup():
    """``max_self_repair == 0`` 时模型塌了仍照旧抛，兜底不许伸到这条路上来。

    budget=0 的岗位压根没有「上一轮」，T131 那层 try/except 在这里必须是透明的。
    """
    with pytest.raises(TimeoutError):
        _run(_SeqModel(TimeoutError("504")), rounds=0)


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


def test_the_repair_prompt_carries_the_previous_diff_under_gits_own_line_numbers():
    """git 说 ``corrupt patch at line 6`` —— 第 6 行长什么样，得让模型看得见。

    T125 那版只递错误原话。模型于是拿到一个**它无法定位的行号**：它手里只有
    自己吐的那份 JSON，而 ``git apply`` 读到的是所有 diff 首尾相接后的那份输入，
    行号按后者数。差一个文件，整个行号就错位。

    所以这条断言分两截，第二截才是硬的：
      ① 上一版 diff 的正文真的进了重问提示词；
      ② 提示词里那份正文的行号与 ``sandbox_git_apply`` 报的行号**是同一套** ——
         拿 git 原话里的行号去提示词里查，查到的必须是那一行本身。
    只验 ① 的话，拼法哪天与 sandbox 漂了也不会红，而症状是模型照着错的行去改。
    """
    import re

    from maos.tools.sandbox import prepare_sandbox_workdir, sandbox_git_apply

    model = _SeqModel(_patch_json(CORRUPT_DIFF), GOOD_PATCH)
    _run(model)
    second = model.users[1]

    # ① 正文进来了：上一版 diff 的每一行都能在提示词里找到
    for line in CORRUPT_DIFF.rstrip("\n").split("\n"):
        assert line in second, f"上一版 diff 的这一行没进重问提示词：{line!r}"
    assert "上一版补丁的正文" in second

    # ② 行号同源：git 报的行号 -> 提示词里的编号行
    workdir = prepare_sandbox_workdir()
    try:
        checked = sandbox_git_apply(
            {"files": [{"path": "auth/session.py", "diff": CORRUPT_DIFF}], "summary": "s"},
            workdir, check_only=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    git_says = checked["error"]["message"]
    assert "corrupt patch at line" in git_says, f"靶场的 git 换了说法：{git_says}"
    reported = int(re.search(r"at line (\d+)", git_says).group(1))

    numbered = dict(re.findall(r"^\s*(\d+) \| (.*)$", second, flags=re.M))
    payload_lines = CORRUPT_DIFF.split("\n")[:-1]          # 末尾换行不是一行
    for i, line in enumerate(payload_lines, 1):
        assert numbered.get(str(i)) == line, (
            f"提示词第 {i} 行与喂给 git 的第 {i} 行对不上："
            f"{numbered.get(str(i))!r} != {line!r}")
    # CORRUPT_DIFF 的病就是「hunk 头说有 5 行，正文只给到第 5 行」——
    # git 数到第 6 行时输入已经没了，所以报的行号恰好落在正文末尾之后一行。
    assert reported == len(payload_lines) + 1, (
        f"git 报的是第 {reported} 行，而递回去的正文只有 {len(payload_lines)} 行 —— "
        f"两套行号漂了")


def test_the_repair_prompt_still_demands_a_full_rewrite_not_a_diff_of_the_diff():
    """带上正文之后，「重新输出完整的 JSON」那句必须还在。

    递回正文最容易招来的误解正是「那就给个增量吧」，而模型上一版恰恰是错的：
    让它在一份坏 diff 上打补丁，比重产一份贵也比重产一份不稳。
    """
    model = _SeqModel(_patch_json(CORRUPT_DIFF), GOOD_PATCH)
    _run(model)

    second = model.users[1]
    assert "完整的 JSON" in second
    assert "不是增量" in second


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
# 4b. 重问轮自己塌了：上一轮那份补丁不许跟着陪葬（T131）
# ===========================================================================
def test_a_model_blowup_in_the_repair_round_still_hands_the_previous_patch_over(store):
    """第一轮产出合法但打不上，第二轮模型超时 -> **交出第一轮那份**，不判死。

    丢掉它的话，一次失败的重问比根本不重问更坏：没有预检时这份补丁本来就会被
    交出去，由 Gate 真打一次、拿 ``tool_error`` 包成 test_report、走返工链。
    在这里抛，Gate 连见都见不到，证据里只剩一个没有上下文的 skill failed ——
    与「次数用尽仍照旧交出去」是同一条取向（``test_repair_gives_up_but_still_
    hands_the_patch_over`` 那条写的理由，一字不改地适用于这里）。

    顺带钉住记账：塌掉那一轮走的是 ``record_model_failure`` 而不是
    ``record_model_usage``，所以「usage 行数 = 1 + rounds」在这条路上**不成立**，
    对账式是 usage + failure = 1 + rounds。这是事实不是缺陷，写出来免得有人
    照着那条注释去「修」一个不存在的漏账。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF), TimeoutError("网关 504"))
    out = _run(model, rounds=2, store=store)

    assert len(model.users) == 2, "塌了之后还接着问 —— 预算没守住"
    assert out["files"][0]["diff"] == BROKEN_DIFF, "上一轮那份补丁被丢掉了"
    assert out["self_check"] == {"build": "pass", "lint": "pass"}
    assert out["self_repair_rounds"] == 1, "重问过一次就是一次，哪怕那一次塌了"

    usage = [r for r in store.list_model_usage(trace_id=TRACE)
             if r["call_site"] == CALL_SITE]
    assert len(usage) == 1, "塌掉那一轮不该记成一次成功调用"


def test_garbage_json_in_the_repair_round_still_hands_the_previous_patch_over():
    """第二轮吐回一坨连 JSON 都不是的东西 -> 同样交出第一轮那份。

    与上一条是同一条出口的两个入口：上一条是模型没答上来，这条是答了但形状不成立
    （``_parse`` 抛 ValueError）。两者对「手上那份补丁还在不在」没有任何区别。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF), "这不是 JSON，是一段解释文字")
    out = _run(model, rounds=2)

    assert len(model.users) == 2
    assert out["files"][0]["diff"] == BROKEN_DIFF
    assert out["self_repair_rounds"] == 1


def test_a_repair_round_that_touches_a_protected_path_is_still_a_security_event():
    """重问轮吐出一份碰受保护路径的补丁 -> 照旧 ``ProtectedPathViolation``。

    上面两条的兜底**不许把这一条也吃掉**。第二次试就放行，等于把
    ``max_self_repair`` 变成「绕过口可以多试两次」—— 与
    ``test_a_path_escape_caught_by_the_precheck_is_a_security_event`` 同一条取向。
    """
    model = _SeqModel(_patch_json(BROKEN_DIFF),
                      _patch_json(CORRUPT_DIFF, path="tests/test_session.py"))
    with pytest.raises(ProtectedPathViolation):
        _run(model, rounds=2)

    assert len(model.users) == 2, "安全事件之后还接着重问"


@pytest.mark.parametrize("blowup", [
    TimeoutError("首问就 504"),
    "这不是 JSON",
], ids=["model-blows-up", "garbage-json"])
def test_a_first_round_blowup_still_propagates(blowup):
    """**首轮**塌了仍照旧抛 —— 手上一份补丁都没有，兜底无从兜起。

    这条与上面三条一起才把边界划全：兜底的条件是「上一轮留下了东西」，
    不是「只要塌了就吞掉」。少了它，``patch is None`` 那一支哪天被写成
    「返回手上这个 None」，症状会是 Gate 收到 None 然后崩在 ``.get`` 上，
    离这里很远。行为与 T125 之前逐字不变。
    """
    model = _SeqModel(blowup)
    with pytest.raises((TimeoutError, ValueError)):
        _run(model, rounds=2)

    assert len(model.users) == 1


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
        # 显式写 "0" 而不是留空：留空与「从来没设过」不可区分，而这一跑是**刻意**
        # 关掉强制的。行为上两者等价（"0" 在 `_FORCE_OFF_VALUES` 里），
        # 区别在于它还能压过继承来的 =1 —— 下一条测试守着那件事。
        assert os.environ[ENV_FORCE_SCRIPTED] == "0"
    finally:
        os.environ[ENV_FORCE_SCRIPTED] = "1"        # 还给 conftest 的起跑线


def test_live_model_outranks_an_inherited_force_scripted(monkeypatch):
    """``--live-model`` 必须压得过**继承来的** ``MAOS_FORCE_SCRIPTED=1``。

    原先 ``run.py`` 带这个旗标时只把它从 argv 里摘掉，**不动环境**。于是在
    export 过该变量的 shell 里（演示机的 ``.bash_profile`` 就 export 着
    ``MAOS_LLM_*`` 那一串，``demo_preflight.sh`` 也 ``export MAOS_FORCE_SCRIPTED=1``）
    ``--live-model`` 静默失效、照走 Scripted —— 而日志还在提示「要真模型请用
    ``--live-model``」。人照做了，什么也没变，且没有任何红灯。

    显式开关压过环境，这是它之所以叫显式。这里从 ``forced_scripted()`` 与
    ``select_model_client()`` 两头判，不打一行真网络（key 是假的，只构造不调用）。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_t125_run3", ROOT / "run.py")
    run_mod = importlib.util.module_from_spec(spec)
    sys.modules["_t125_run3"] = run_mod
    spec.loader.exec_module(run_mod)

    _with_live_env(monkeypatch)
    monkeypatch.setenv(ENV_FORCE_SCRIPTED, "1")
    assert forced_scripted() is True, "起跑线：环境把这一跑钉死在 Scripted 上"

    assert run_mod._apply_model_mode(["--live-model"]) == []
    assert os.environ[ENV_FORCE_SCRIPTED] == "0"
    assert forced_scripted() is False
    assert isinstance(select_model_client(), GatewayModelClient), \
        "--live-model 没能压过继承来的 =1，这一跑仍然是脚本回放"


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


# ---------------------------------------------------------------------------
# 7b. make_evidence 的父子进程：子进程继承决定，不自己重新判
# ---------------------------------------------------------------------------
def _load_make_evidence(name: str):
    """把 ``scripts/make_evidence.py`` 当模块装进来（它平时是 ``__main__``）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "make_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_make_evidence_child_process_inherits_and_does_not_decide(monkeypatch, tmp_path):
    """``--_child`` 子进程**不许**自己设 ``MAOS_FORCE_SCRIPTED``。

    父进程用 ``[sys.executable, __file__, "--_child", n, "--_db", db]`` 递归调起
    自己，**argv 里不带 ``--live-model``**。子进程要是照着 ``not args.live_model``
    自己判，父进程带 ``--live-model`` 的那一跑就会变成：父进程不设、``model_mode()``
    标 ``live``，子进程却把自己钉死成 Scripted —— **实跑脚本回放，索引却写着真模型**。
    标签说谎比跑错模型更坏（铁律 3），而从产物上看不出来，除非去比 token 数。

    传递靠的是 ``subprocess.run`` 不传 ``env=``（父进程已把 ``os.environ`` 设好），
    所以子进程正确的动作是**什么都不做**。这条就钉这个「什么都不做」。
    """
    make_evidence = _load_make_evidence("_t125_me_child")
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)

    seen = []
    monkeypatch.setattr(make_evidence, "run_child",
                        lambda n, db: (seen.append((n, db)), 0)[1])

    db = str(tmp_path / "child.db")
    assert make_evidence.main(["--_child", "1", "--_db", db]) == 0
    assert seen == [(1, db)]
    assert ENV_FORCE_SCRIPTED not in os.environ, (
        f"子进程自己把 {ENV_FORCE_SCRIPTED} 设成了 "
        f"{os.environ.get(ENV_FORCE_SCRIPTED)!r} —— 父进程带 --live-model 时，"
        f"这一束会标 live 而实跑 Scripted")
    assert make_evidence.model_mode() == "live"


def test_make_evidence_contrast_child_also_inherits(monkeypatch, tmp_path):
    """``--_contrast`` 走的是第二个递归入口，同一条豁免必须也覆盖它。

    只修 ``--_child`` 的话，八束是真模型、三组对照束悄悄是脚本回放，
    而两边的 ``model_mode`` 都写着 live。
    """
    make_evidence = _load_make_evidence("_t125_me_contrast")
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)

    seen = []
    monkeypatch.setattr(make_evidence, "run_child_contrast",
                        lambda g, db: (seen.append((g, db)), 0)[1])

    db = str(tmp_path / "contrast.db")
    assert make_evidence.main(["--_contrast", "R3", "--_db", db]) == 0
    assert seen == [("R3", db)]
    assert ENV_FORCE_SCRIPTED not in os.environ


def test_make_evidence_parent_still_defaults_to_scripted(monkeypatch):
    """父进程（没有 ``--_child`` / ``--_contrast``）缺省照旧钉死 Scripted。

    上面那条豁免不许把缺省一起放掉 —— 那才是这个开关的主业。
    ``--contrast`` 只是个不落盘的挂载点，真正跑的那一段打了桩。
    """
    make_evidence = _load_make_evidence("_t125_me_parent")
    monkeypatch.delenv(ENV_FORCE_SCRIPTED, raising=False)
    monkeypatch.setattr(make_evidence, "main_contrast", lambda args: 0)

    assert make_evidence.main(["--contrast"]) == 0
    assert os.environ[ENV_FORCE_SCRIPTED] == "1"
    assert make_evidence.model_mode() == "scripted"


def test_make_evidence_live_model_outranks_an_inherited_force_scripted(monkeypatch):
    """``make_evidence.py --live-model`` 同样要压得过继承来的 ``=1``。

    与 ``run.py`` 那条同源（见 ``test_live_model_outranks_an_inherited_force_scripted``）：
    ``setdefault`` 在环境里已经有值时是空操作，于是 preflight ``export`` 过之后
    ``--live-model`` 静默失效，产出的束标着 live、实跑却是脚本回放。
    """
    make_evidence = _load_make_evidence("_t125_me_live")
    monkeypatch.setenv(ENV_FORCE_SCRIPTED, "1")
    monkeypatch.setattr(make_evidence, "main_contrast", lambda args: 0)

    assert make_evidence.main(["--contrast", "--live-model"]) == 0
    assert os.environ[ENV_FORCE_SCRIPTED] == "0"
    assert make_evidence.model_mode() == "live"
    assert forced_scripted() is False, "make_evidence 与 client.py 对 \"0\" 的判定必须一致"
