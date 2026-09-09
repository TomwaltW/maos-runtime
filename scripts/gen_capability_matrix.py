#!/usr/bin/env python3
"""生成 evidence/capability-matrix.json —— 角色 x skill x 工具 x MCP x 算力档。

回答的是一个此前只能靠人读代码回答的问题：**每个 agent 按职责真的拿到它该有的东西
了吗**。矩阵的每一行都来自真实运行时（``AGENT_POOL`` / ``SKILL_REGISTRY`` /
扫出来的 ToolPort / 一次真装配），一个字段都不许手写 —— 手抄的矩阵在下一次改
identity 时就开始撒谎，而没人会发现。

出处口径直接复用 ``scripts/make_evidence.py``（``HEADER_PREFIX`` / ``git_sha`` /
``header_line``），不另起一套：两份证据的首行必须能被同一个正则读懂。取不到 sha
就拒绝生成，不给「unknown」兜底 —— 没有出处的证据不是证据。

**不写绝对路径**（铁律 6，口径同 ``maos/tools/mcp/git_tool.py`` 里那段）：模块一律
记点号形式的模块名。绝对路径会把 ``/Users/<某人>/…`` 原样落进证据束，而且换台机器
就不可比。

不改 ``evidence/INDEX.json``：本文件产的是一份新证据，入不入索引由整合期定。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.make_evidence import header_line, pin_sha   # noqa: E402

from maos.agents import AGENT_POOL                       # noqa: E402
from maos.runtime.assembly import (                      # noqa: E402
    build_report,
    collect_local_ports,
    port_modules,
)
from maos.skills import registry                         # noqa: E402

OUT = os.path.join(ROOT, "evidence", "capability-matrix.json")


def _backing(module: str) -> str:
    """工具是本地背书还是 MCP 背书 —— 按定义它的模块判，不靠名字猜。"""
    return "mcp" if ".mcp." in f".{module}." else "local"


def _force_skill_discovery() -> None:
    """先摸一次注册表，触发 builtin 动态发现。

    不摸的话 ``registry.names()`` 只报「当前已显式注册的」（见 registry.get 的
    docstring：names() 不带动态发现兜底），矩阵会把一整批有实现的 skill 记成没实现。
    """
    registry.get("__trigger_builtin_discovery__")


def build_matrix() -> dict:
    _force_skill_discovery()
    ports = collect_local_ports()
    modules = port_modules()

    tool_rows = [
        {
            "name": name,
            "module": modules.get(name, ""),
            "backing": _backing(modules.get(name, "")),
            "owner": port.owner,
            "purpose": port.purpose,
            "rate_limit": port.rate_limit,
        }
        for name, port in sorted(ports.items())
    ]
    skill_rows = [
        {"name": name, "versions": registry.versions(name),
         "latest": registry.versions(name)[-1]}
        for name in registry.names()
    ]

    roles = []
    for role, cls in sorted(AGENT_POOL.items()):
        identity = cls.identity
        report = build_report(identity, ports=ports)
        roles.append({
            "role": role,
            "agent_id": identity.agent_id,
            "duty": identity.duty,
            "model_tier": identity.model_tier,
            "max_risk": identity.max_risk,
            "max_self_repair": identity.max_self_repair,
            "write_scope": sorted(identity.write_scope),
            "allowed_skills": [
                {"name": name, "implemented": version is not None, "version": version}
                for name, version in sorted(report.skills.items())
            ],
            "allowed_tools": [
                {
                    "name": name,
                    "implemented": name in report.ports,
                    "backing": (_backing(modules[name]) if name in modules else None),
                    "module": modules.get(name, ""),
                }
                for name in sorted(identity.allowed_tools)
            ],
            "assembly": {
                "resolved": list(report.resolved_tools),
                "resolved_count": len(report.ports),
                "authorized_without_impl": list(report.authorized_without_impl),
                "authorized_without_impl_count": len(report.authorized_without_impl),
                "implemented_without_authorization_count":
                    len(report.implemented_without_authorization),
                "skills_without_impl": list(report.skills_without_impl),
            },
        })

    drift_tools = sorted({
        name for row in roles
        for name in row["assembly"]["authorized_without_impl"]})
    drift_skills = sorted({
        name for row in roles for name in row["assembly"]["skills_without_impl"]})

    return {
        "summary": {
            "roles": len(roles),
            "skills_registered": len(skill_rows),
            "tool_ports": len(tool_rows),
            "mcp_backed_tool_ports": sum(
                1 for t in tool_rows if t["backing"] == "mcp"),
            "roles_with_tool_drift": sum(
                1 for r in roles if r["assembly"]["authorized_without_impl"]),
            "authorized_tools_without_impl": drift_tools,
            "authorized_skills_without_impl": drift_skills,
        },
        "tool_ports": tool_rows,
        "skills": skill_rows,
        "roles": roles,
    }


def main(argv: list[str] | None = None) -> int:
    """``--out`` 只给测试用：让测试**真跑**本脚本，又不把 evidence/ 的正本改脏。

    正本必须由人在干净工作区里跑一次落盘（否则首行的 sha 会带 -dirty），
    而测试每跑一次就改一次正本的话，跑完测试 ``git status`` 永远是脏的。
    """
    ap = argparse.ArgumentParser(description="生成能力矩阵证据")
    ap.add_argument("--out", default=OUT, help="产物路径（缺省 evidence/capability-matrix.json）")
    ns = ap.parse_args(argv)

    matrix = build_matrix()
    sha = pin_sha()                          # 取不到 sha 会抛 EvidenceError，不兜底
    body = json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=False)
    os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
    with open(ns.out, "w", encoding="utf-8") as fh:
        fh.write(header_line(sha) + "\n")
        fh.write(body + "\n")
    rel = os.path.relpath(ns.out, ROOT)
    s = matrix["summary"]
    print(f"[OK] {rel}: {s['roles']} 个角色 / {s['skills_registered']} 个 skill / "
          f"{s['tool_ports']} 个 ToolPort（MCP 背书 {s['mcp_backed_tool_ports']} 个）")
    if s["authorized_tools_without_impl"]:
        print(f"[WARN] 有授权无实现的工具: {s['authorized_tools_without_impl']}"
              f"（{s['roles_with_tool_drift']} 个角色受影响；本脚本只记账，不修）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
