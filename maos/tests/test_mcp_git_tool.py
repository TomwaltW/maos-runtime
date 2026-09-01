"""``git-mcp`` 这个 ToolPort 的守卫 —— 九要素、命名一致、审计行同形、真被调到。

这一份守的不是传输层（那在 ``test_mcp_transport.py``），而是**接进来之后的三件事**：

1. 九要素声明完整 —— ⑥失败形态与⑦安全边界是评审会逐条对的东西，空着等于没声明；
2. ``ToolPort.name`` 与 Identity 的 ``allowed_tools`` 是同一套名字 ——
   此前 ``git-mcp`` 在白名单里放行了一个**不存在**的工具，这份测试守着它别再走回去；
3. entry 跨进程之后，``ToolInvoked`` 审计行与本地工具**逐字段同形**。
   这一条是「迁移到 MCP = 换传输层，schema 与审计不变」那句话的实际判据。
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from maos.agents.coding import CodingAgent
from maos.core.store import SqliteStore
from maos.model.client import ScriptedModelClient
from maos.skills.builtin.code_repo_patch import CodeRepoPatchSkill
from maos.skills.invoker import SkillInvoker
from maos.tools.mcp.git_tool import FIXTURE_ROOT, GIT_MCP_PORT, OPS, _abs_root, git_mcp
from maos.tools.port import invoke_tool
from maos.tools.sandbox import FIXTURE_REPO

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _store():
    s = SqliteStore()
    s.init_schema()
    return s


# ---------------------------------------------------------------------------
# 1. 九要素与命名
# ---------------------------------------------------------------------------

def test_nine_elements_are_all_declared():
    """rate_limit 允许为空（未设限），其余八项一个都不许空着。"""
    for f in dataclasses.fields(GIT_MCP_PORT):
        value = getattr(GIT_MCP_PORT, f.name)
        if f.name == "rate_limit":
            continue
        assert value, f"九要素缺项：{f.name}"


def test_failure_modes_cover_the_cross_process_failures():
    """跨进程比进程内多出来的四类失败，声明里必须点到名。"""
    text = " ".join(GIT_MCP_PORT.failure_modes)
    for keyword in ("超时", "协议版本", "提前退出", "越出 root"):
        assert keyword in text, f"failure_modes 没覆盖：{keyword}"


def test_name_matches_the_identity_whitelist():
    """白名单里的名字必须真有实现 —— 这正是本轨要补的那个洞。"""
    assert GIT_MCP_PORT.name == "git-mcp"
    assert GIT_MCP_PORT.name in CodingAgent.identity.allowed_tools
    assert GIT_MCP_PORT.name in CodeRepoPatchSkill.contract.depends_tools


def test_fixture_root_does_not_drift_from_sandbox():
    """FIXTURE_ROOT 与 sandbox.FIXTURE_REPO 必须指同一个目录。

    两处各写一份路径，漂了也不会有人发现 —— 直到某天 git-mcp 报的基线
    和沙箱真正打补丁的那个仓库不是同一个。
    """
    assert _abs_root(FIXTURE_ROOT) == str(FIXTURE_REPO)


def test_entry_is_not_a_local_function():
    """这一条就是「已迁移」的判据：entry 的实现落在 maos.tools.mcp 里。"""
    assert GIT_MCP_PORT.entry.__module__.startswith("maos.tools.mcp")


# ---------------------------------------------------------------------------
# 2. 三个 op 真跑得通
# ---------------------------------------------------------------------------

def test_baseline_returns_a_real_head():
    out = git_mcp(op="baseline", root=FIXTURE_ROOT)
    assert len(out["head"]) == 40
    assert out["head_short"] == out["head"][:7]
    assert isinstance(out["dirty"], bool)


def test_ls_files_prefix_filters():
    everything = git_mcp(op="ls_files", root=FIXTURE_ROOT)["files"]
    only_auth = git_mcp(op="ls_files", root=FIXTURE_ROOT, prefix="auth/")["files"]
    assert "auth/session.py" in only_auth
    assert set(only_auth) < set(everything)


def test_show_file_reads_head_version():
    out = git_mcp(op="show_file", root=FIXTURE_ROOT, path="auth/session.py")
    assert out["truncated"] is False
    assert "def is_session_valid" in out["content"]


def test_unknown_op_fails_at_the_call_site():
    """op 写错是调用点的 bug，不是对端的问题：不必起进程就该炸。"""
    with pytest.raises(ValueError, match="未知的 git-mcp 操作"):
        git_mcp(op="rm_rf", root=FIXTURE_ROOT)
    assert set(OPS) == {"baseline", "ls_files", "show_file"}


# ---------------------------------------------------------------------------
# 3. 审计行与本地工具同形
# ---------------------------------------------------------------------------

def test_tool_invoked_row_is_shaped_like_a_local_tool():
    store, plan_id = _store(), "plan-mcp"
    out = invoke_tool(GIT_MCP_PORT, {"op": "baseline", "root": FIXTURE_ROOT},
                      store=store, extras={"plan_id": plan_id, "trace_id": "tr-mcp"})

    assert out["head"]
    rows = [r for r in store.list_event_log(plan_id) if r["event_type"] == "ToolInvoked"]
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert detail["tool"] == "git-mcp"
    assert detail["status"] == "ok"
    assert detail["error"] is None
    assert SHA256_HEX.match(detail["params_digest"]), "params_digest 不是 64 位 hex"
    assert detail["duration_ms"] >= 0


def test_params_digest_is_machine_independent():
    """params 里只放相对路径，同一次调用在任何机器上摘要一致。

    传绝对路径的话，``/Users/<某人>/...`` 会原样落进证据束，既不可比也没必要。
    """
    from maos.tools.port import _digest

    assert not FIXTURE_ROOT.startswith("/")
    assert _digest({"op": "baseline", "root": FIXTURE_ROOT}) == _digest(
        {"root": FIXTURE_ROOT, "op": "baseline"})


def test_failure_still_lands_an_audit_row():
    """工具失败先落审计再抛 —— 出事之后查得到是谁、什么参数、跑了多久。"""
    store, plan_id = _store(), "plan-mcp-fail"
    with pytest.raises(ValueError):
        invoke_tool(GIT_MCP_PORT, {"op": "nope", "root": FIXTURE_ROOT},
                    store=store, extras={"plan_id": plan_id})
    rows = [r for r in store.list_event_log(plan_id) if r["event_type"] == "ToolInvoked"]
    assert len(rows) == 1
    assert rows[0]["detail"]["status"] == "failed"
    assert rows[0]["detail"]["error"].startswith("ValueError")


# ---------------------------------------------------------------------------
# 4. code.repo-patch 真的调了它
# ---------------------------------------------------------------------------

PATCH = json.dumps({
    "files": [{"path": "auth/session.py", "diff": "--- a\n+++ b\n"}],
    "summary": "示例",
    "self_check": {"build": "pass", "lint": "pass"},
}, ensure_ascii=False)


def test_code_repo_patch_invokes_git_mcp_and_puts_baseline_in_the_prompt():
    """声明了 depends_tools 就得真调 —— 声明与实现分家是本轨要消掉的那个洞。"""
    store, plan_id = _store(), "plan-skill"
    model = ScriptedModelClient({"补丁基线": PATCH})

    res = SkillInvoker(CodingAgent.identity, store).invoke(
        "code.repo-patch",
        {"title": "修一个时区 bug", "inputs": {}, "acceptance": ["build 通过"]},
        extras={"model": model, "plan_id": plan_id, "trace_id": "tr-skill"},
    )

    assert res.status == "ok", res.error
    # 模型只在提示词里出现「补丁基线」时才会命中脚本 —— 命中即证明基线进了提示词。
    assert res.output["files"], "脚本没命中：基线没进提示词"

    rows = [r for r in store.list_event_log(plan_id)
            if r["event_type"] == "ToolInvoked" and r["detail"]["tool"] == "git-mcp"]
    assert len(rows) == 1, "code.repo-patch 没有经 invoke_tool 调 git-mcp"
    assert rows[0]["detail"]["status"] == "ok"
