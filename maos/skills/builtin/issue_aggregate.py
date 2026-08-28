"""issue.aggregate —— 把多源信号里的 findings 聚合去重成 issue 清单。

投放即注册（C-1）：本文件放进 builtin/ 就会被 discover() 扫到，不改 __init__.py。

IO 契约（附录 B B-4，逐字段）：
  入：{"findings": list[dict]}
  出：{"issues": [{"id","severity","title","detail","source"}], "summary": str}

**零模型**：聚合是治理路径的入口，判定必须可复现、可解释（phase-4.md 原则
「replan、补偿、审批是控制面行为，其正确性不得依赖模型的智力表现」）。
同一批 findings 在任何机器任何时刻必须出同一份 issue 清单，所以这里
一行模型调用都没有 —— 归一化 + 分组 + 取最高严重度，全是规则。

去重键是**归一化后的 title**，不含 source：多源信号里同一个问题会从
issue.json 和 feedback 两个口子各进来一次，那正是要被合并掉的重复。
合并后 source 保留全部来源（排序后逗号连接），这样「这个问题几个渠道都在报」
这一事实不会在去重中丢失 —— 丢了它，聚合就退化成了单纯的删行。
"""

from __future__ import annotations

import json
import os
from typing import Any

from maos.skills.contract import Skill, SkillContext, SkillContract
from maos.skills.registry import register_skill

# 严重度序：数值只用于组内取最高，不对外暴露。未知取值按最低处理，
# 不抛 —— 上游 findings 来自 Gate、外部 issue、日志三种口子，取值域不由本 skill 定。
SEVERITY_ORDER = {"blocker": 3, "major": 2, "minor": 1}
DEFAULT_SEVERITY = "major"


def _norm(text: str) -> str:
    """归一化去重键：折叠空白 + casefold。标点保留 —— 它常是语义的一部分。"""
    return " ".join(str(text).split()).casefold()


def _rank(severity: str) -> int:
    return SEVERITY_ORDER.get(str(severity).lower(), 0)


def _as_issue_fields(finding: Any) -> dict:
    """把一条 finding 收敛成 issue 的四个字段。

    刻意宽容：Gate 出的 finding 是 ``{gate, severity, path, message}``，
    外部信号出的是 ``{source, severity, title, detail}``，两种形状都要能吃 ——
    聚合器如果只认一种，多源输入这件事本身就不成立了。
    """
    if not isinstance(finding, dict):
        return {"title": str(finding), "detail": "", "severity": DEFAULT_SEVERITY,
                "source": "unknown"}
    title = finding.get("title") or finding.get("message") or finding.get("summary") or ""
    detail = finding.get("detail") or finding.get("message") or ""
    severity = finding.get("severity") or DEFAULT_SEVERITY
    source = finding.get("source") or finding.get("gate") or finding.get("path") or "unknown"
    return {
        "title": " ".join(str(title).split()) or "(无标题)",
        "detail": " ".join(str(detail).split()),
        "severity": str(severity),
        "source": str(source),
    }


@register_skill
class IssueAggregateSkill(Skill):
    contract = SkillContract(
        name="issue.aggregate",
        version="1.0.0",
        purpose="把多源信号里的 findings 聚合去重成结构化 issue 清单",
        input_schema={"findings": "list[dict]"},
        output_schema={
            "issues": "list[{id:str,severity:str,title:str,detail:str,source:str}]",
            "summary": "str",
        },
        preconditions=["findings"],
        depends_tools=[],
        # 纯函数，没有可重试的失败形态：同样入参重跑必然同样结果。
        failure_policy="escalate",
        max_retries=0,
        security_boundary="只读入参，不写任何资源、不调用任何工具、不调用模型；不落盘",
        reuse_note="任何角色要把多源 findings 收成 issue 都复用它；判定零模型，结果可复现",
        owner_roles=["manager"],
    )

    def run(self, payload: dict, ctx: SkillContext) -> Any:
        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise ValueError(
                f"issue.aggregate 入参 findings 必须是 list，实际 {type(findings).__name__}")

        groups: dict[str, dict] = {}
        order: list[str] = []
        for raw in findings:
            item = _as_issue_fields(raw)
            key = _norm(item["title"])
            if key not in groups:
                groups[key] = {"severity": item["severity"], "title": item["title"],
                               "detail": item["detail"], "sources": {item["source"]}}
                order.append(key)
                continue
            g = groups[key]
            g["sources"].add(item["source"])
            if _rank(item["severity"]) > _rank(g["severity"]):
                g["severity"] = item["severity"]        # 组内取最高严重度
            if not g["detail"]:
                g["detail"] = item["detail"]            # 先到的空 detail 由后到的补上

        issues = []
        for i, key in enumerate(order, start=1):
            g = groups[key]
            issues.append({
                "id": f"issue-{i:02d}",                 # 按首次出现序编号，确定性
                "severity": g["severity"],
                "title": g["title"],
                "detail": g["detail"],
                "source": ",".join(sorted(g["sources"])),
            })

        counts = {s: sum(1 for it in issues if it["severity"] == s)
                  for s in ("blocker", "major", "minor")}
        return {
            "issues": issues,
            "summary": (
                f"聚合 {len(findings)} 条 findings -> {len(issues)} 个 issue"
                f"（合并重复 {len(findings) - len(issues)} 条）；"
                f"blocker {counts['blocker']} / major {counts['major']} / minor {counts['minor']}"
            ),
        }


# ----------------------------------------------------------------------
# 多源信号读取（scenarios/inputs/）—— 不属于 skill IO 契约，是喂给它的上游。
#
# 刻意放在本模块而不是 flows/：flows/scenario_1.py 归 Task-C，接线由它决定；
# 读取逻辑先在这里就位，谁要用 import 即可，不占别人的文件。
# ----------------------------------------------------------------------
# 相对包位置定位，不用相对 cwd：测试、run.py、别的轨各自在什么目录下跑不由本模块决定，
# 而「读不到信号」的退化形态是**返回空清单**，不报错 —— 用 cwd 会静默聚合出 0 个 issue。
_MAOS_PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_INPUT_DIR = os.path.join(os.path.dirname(_MAOS_PKG), "scenarios", "inputs")

# 只认这三种扩展名。白名单而不是黑名单：目录里迟早会多出 README.md、.DS_Store
# 这类非信号文件，兜底读成「反馈」会把说明文字的每一行都变成一条 finding。
SIGNAL_SUFFIXES = (".json", ".log", ".txt")


def load_signal_findings(input_dir: str = DEFAULT_INPUT_DIR) -> list[dict]:
    """把 scenarios/inputs/ 下的多源信号读成 findings 列表（按文件名排序，确定性）。

    三种口子各自的形状不同，在这里统一成 findings；**不在这里去重** ——
    去重是 issue.aggregate 的职责，读取器抢着做会让「聚合去掉了几条重复」
    这个可观测量凭空消失。
    """
    out: list[dict] = []
    if not os.path.isdir(input_dir):
        return out
    for name in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, name)
        if not os.path.isfile(path) or not name.endswith(SIGNAL_SUFFIXES):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if name.endswith(".json"):
            out.extend(_from_json(name, text))
        elif name.endswith(".log"):
            out.extend(_from_log(name, text))
        else:
            out.extend(_from_feedback(name, text))
    return out


def _from_json(name: str, text: str) -> list[dict]:
    data = json.loads(text)
    items = data if isinstance(data, list) else [data]
    return [{
        "source": name,
        "severity": it.get("severity", DEFAULT_SEVERITY),
        "title": it.get("title", ""),
        "detail": it.get("body") or it.get("detail") or "",
    } for it in items if isinstance(it, dict)]


def _from_feedback(name: str, text: str) -> list[dict]:
    """一行一条反馈；``#`` 开头是注释，空行跳过。"""
    return [{"source": name, "severity": "major", "title": line.strip(), "detail": ""}
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _from_log(name: str, text: str) -> list[dict]:
    """日志里只取 ERROR 行，取「级别与时间戳之后」的部分作为标题。

    时间戳必须剥掉：同一个错误每次出现时间都不同，留着它去重键就永不相等，
    聚合会把 N 次同样的报错报成 N 个 issue。
    """
    out = []
    for line in text.splitlines():
        if "ERROR" not in line:
            continue
        _, _, tail = line.partition("ERROR")
        out.append({"source": name, "severity": "blocker",
                    "title": tail.strip(" -:\t"), "detail": line.strip()})
    return out
