"""Reviewer Gate —— 质量门禁。

四道闸按顺序跑，任何一道不过就出 rework，findings 必须结构化
（Coding Agent 要能直接消费，不能是一段自然语言吐槽）。

刻意做成规则驱动而不是模型驱动：Gate 的判定必须可复现、可解释、可审计。
需要模型参与的语义审查，走 Reviewer Agent，不放在 Gate 里。
"""

from __future__ import annotations

import logging

from maos.contracts import events as E
from maos.contracts.events import Topic
from maos.contracts.states import Risk, TaskState
from maos.core.control_plane import ControlPlane
from maos.core.eventbus import EventBus
from maos.core.store import Store

log = logging.getLogger("maos.gate")


class ReviewerGate:
    """轮询 AWAITING_REVIEW 的任务，跑四道闸，发 ReviewVerdict。"""

    def __init__(self, store: Store, bus: EventBus, cp: ControlPlane) -> None:
        self.store = store
        self.bus = bus
        self.cp = cp

    def review_pending(self, plan_id: str) -> int:
        n = 0
        for task in self.store.list_tasks(plan_id):
            if task["state"] != TaskState.AWAITING_REVIEW:
                continue
            self._review(task)
            n += 1
        return n

    def _review(self, task: dict) -> None:
        artifacts = [a for a in self.store.list_artifacts(task["task_id"])
                     if a["version"] == task["attempt"]]
        findings: list[dict] = []
        results: dict[str, str] = {}

        for name, check in (
            ("schema", self._gate_schema),
            ("acceptance", self._gate_acceptance),
            ("security", self._gate_security),
            ("evidence", self._gate_evidence),
        ):
            fs = check(task, artifacts)
            results[name] = "fail" if fs else "pass"
            findings.extend(fs)

        verdict = "pass" if not findings else "rework"
        log.info("[%s] Gate %s -> %s", task["task_id"], results, verdict)

        self.bus.publish(Topic.REVIEW_VERDICT, E.review_verdict(
            plan_id=task["plan_id"], task_id=task["task_id"], attempt=task["attempt"],
            trace_id=task["trace_id"], verdict=verdict, findings=findings,
            gate_results=results,
        ))

    # -- 四道闸 -----------------------------------------------------------
    @staticmethod
    def _gate_schema(task, artifacts) -> list[dict]:
        if not artifacts:
            return [{"gate": "schema", "severity": "blocker", "path": None,
                     "message": "本轮没有产出任何 artifact"}]
        out = []
        for a in artifacts:
            if a["kind"] == "patch_set" and "files" not in a["content"]:
                out.append({"gate": "schema", "severity": "blocker",
                            "path": None, "message": "patch_set 缺少 files 字段"})
        return out

    @staticmethod
    def _gate_acceptance(task, artifacts) -> list[dict]:
        """MVP 版：只判"自检是否通过"。Track A 补完时接真实测试报告。

        口径是"非 pass 即 finding"，不是"只认字面 fail"：
          · self_check 缺失 = 没自检过，不是自检过了，必须判 finding；
          · self_check 不是 dict（None / 字符串）一律按"缺失"处理，**不抛异常** ——
            Gate 是独立判定面，不能假设上游已经把形状收敛好（skill 侧用的是
            setdefault，键在则原样保留）；而 review_pending() 在
            flows/common.py 的驱动循环里是裸调用，异常逃出去会把整个 plan 掀掉，
            连退化成一次 rework 都做不到。
        """
        out = []
        for a in artifacts:
            check = a["content"].get("self_check")
            if not isinstance(check, dict):
                check = {}
            for k in ("build", "lint"):
                if check.get(k) != "pass":
                    out.append({"gate": "acceptance", "severity": "major", "path": None,
                                "message": f"本地自检 {k} 未通过，需修复后重新提交"})
        return out

    @staticmethod
    def _gate_security(task, artifacts) -> list[dict]:
        out = []
        for a in artifacts:
            for f in a["content"].get("files", []):
                diff = f.get("diff", "")
                if any(k in diff for k in ("AKIA", "-----BEGIN", "password=", "api_key=")):
                    out.append({"gate": "security", "severity": "blocker",
                                "path": f["path"], "message": "补丁中疑似出现明文凭证"})
        return out

    @staticmethod
    def _gate_evidence(task, artifacts) -> list[dict]:
        out = []
        for a in artifacts:
            if not a["content"].get("summary"):
                out.append({"gate": "evidence", "severity": "minor", "path": None,
                            "message": "缺少变更说明，无法形成审计证据"})
        return out


class HumanApprovalQueue:
    """人工审批队列。高风险任务 Gate 过了也停在 BLOCKED，等这里放行。"""

    def __init__(self, store: Store, cp: ControlPlane) -> None:
        self.store = store
        self.cp = cp

    def pending(self, plan_id: str) -> list[dict]:
        return [t for t in self.store.list_tasks(plan_id)
                if t["state"] == TaskState.BLOCKED and t["effect_risk"] == Risk.HIGH]

    def decide(self, task_id: str, approved: bool, operator: str, note: str = "") -> None:
        self.cp.human_decision(task_id, approved, operator, note)
